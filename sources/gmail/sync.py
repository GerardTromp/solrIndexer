#!/usr/bin/env python3
"""
sources/gmail/sync.py — pull source for Gmail mailboxes.

Uses the Gmail API with OAuth2 user consent (refresh-token flow) to
incrementally sync messages into per-message .eml files plus a
.manifest.json that fs_indexer's manifest reader picks up. Same on-disk
shape as the PST source, so the indexer treats Gmail messages exactly
like archived PST messages — one mental model for everything email.

Designed to run as a sources.yaml hook command.

See DESIGN.md in this directory for the rationale behind every choice
(auth mode, sync strategy, on-disk format, path layout).

Environment variables (all required for normal runs):
  FSEARCH_GMAIL_OUTPUT       output root, also the fsearch source root
  FSEARCH_GMAIL_CREDENTIALS  path to OAuth client JSON from GCP console
                             (default: ~/.config/fsearch/gmail_credentials.json)
  FSEARCH_GMAIL_TOKEN        path to refresh-token cache
                             (default: ~/.config/fsearch/gmail_token.json)
  FSEARCH_GMAIL_STATE        sync state file (history cursor)
                             (default: <output>/.gmail_state.json)
  FSEARCH_GMAIL_LOG_LEVEL    DEBUG | INFO | WARNING (default INFO)

One-time setup: see DESIGN.md "Setup checklist" section.

Special invocation:
  ./sync.py --auth       Run the OAuth consent flow interactively
                         and save the refresh token. Required once
                         before the first headless run.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from pathlib import Path

# Lazy imports — these dependencies are only required at runtime, so
# we tolerate them being absent at module import time so that --help
# and the design doc can be inspected without a full install.
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    _DEPS_OK = True
except ImportError as _e:
    _DEPS_OK = False
    _DEPS_ERR = str(_e)

logging.basicConfig(
    level=os.environ.get("FSEARCH_GMAIL_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("gmail-sync")

# Read-only is the only scope we need or want. If this ever changes,
# the user will be prompted to re-authenticate (existing tokens
# become invalid).
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

MANIFEST_FILENAME = ".manifest.json"
STATE_FILENAME = ".gmail_state.json"

# Filter out drafts and chat — see DESIGN.md "What this source does NOT do"
EXCLUDED_LABELS = {"DRAFT", "CHAT", "SPAM", "TRASH"}


# ── Config ──────────────────────────────────────────────────────────────────

def _default_creds_dir() -> Path:
    return Path.home() / ".config" / "fsearch"


def _load_config() -> dict:
    out = os.environ.get("FSEARCH_GMAIL_OUTPUT")
    if not out:
        log.error("Missing required env var: FSEARCH_GMAIL_OUTPUT")
        sys.exit(2)

    output_root = Path(out).resolve()
    creds_dir = _default_creds_dir()

    creds_path = Path(os.environ.get(
        "FSEARCH_GMAIL_CREDENTIALS", creds_dir / "gmail_credentials.json"))
    token_path = Path(os.environ.get(
        "FSEARCH_GMAIL_TOKEN", creds_dir / "gmail_token.json"))
    state_path = Path(os.environ.get(
        "FSEARCH_GMAIL_STATE", output_root / STATE_FILENAME))

    output_root.mkdir(parents=True, exist_ok=True)
    if not os.access(output_root, os.W_OK):
        log.error(f"Output dir not writable: {output_root}")
        sys.exit(2)

    return {
        "output_root": output_root,
        "creds_path":  creds_path,
        "token_path":  token_path,
        "state_path":  state_path,
    }


# ── Auth ───────────────────────────────────────────────────────────────────

def _ensure_creds(cfg: dict, interactive: bool = False) -> "Credentials":
    """
    Load saved Gmail credentials, refreshing if expired. With
    interactive=True, runs the consent flow from scratch (used by
    the --auth subcommand on first install).
    """
    creds_path = cfg["creds_path"]
    token_path = cfg["token_path"]

    if not creds_path.exists():
        log.error(
            f"OAuth client credentials missing: {creds_path}\n"
            f"  See sources/gmail/DESIGN.md for the one-time setup steps.")
        sys.exit(2)

    creds: Credentials | None = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(token_path), SCOPES)
        except (ValueError, OSError) as e:
            log.warning(f"Saved token unreadable, will re-auth: {e}")
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(token_path, creds)
            log.info("Refreshed access token")
            return creds
        except Exception as e:
            log.warning(f"Token refresh failed, will re-auth: {e}")

    if not interactive:
        log.error(
            f"No valid token at {token_path}. Run with --auth to authorize.\n"
            f"  python3 sync.py --auth")
        sys.exit(2)

    log.info("Starting interactive OAuth consent flow")
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    # run_local_server opens a browser; for headless WSL fall back to
    # the console flow if no browser is available.
    try:
        creds = flow.run_local_server(port=0, open_browser=True)
    except Exception as e:
        log.warning(f"Local server flow failed ({e}), falling back to console")
        creds = flow.run_console()
    _save_token(token_path, creds)
    log.info(f"Authorization complete; token saved to {token_path}")
    return creds


def _save_token(token_path: Path, creds: "Credentials") -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass


# ── State ──────────────────────────────────────────────────────────────────

def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"history_id": None, "first_sync_completed": False}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.warning(f"State file unreadable, starting fresh: {e}")
        return {"history_id": None, "first_sync_completed": False}


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


# ── On-disk message paths ──────────────────────────────────────────────────

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def _safe_msgid_suffix(msg_id: str) -> str:
    """
    Short, filesystem-safe slug derived from a Gmail message ID.
    Gmail IDs are already alphanumeric, but we truncate to keep file
    names short.
    """
    return _SAFE_ID.sub("_", msg_id)[:16] or "msg"


def _eml_path_for(output_root: Path, internal_date_ms: int,
                  msg_id: str) -> Path:
    """
    Return the on-disk path where a message should be stored, based
    on its internal Gmail timestamp. See DESIGN.md "Path layout".
    """
    dt = datetime.fromtimestamp(internal_date_ms / 1000.0, tz=timezone.utc)
    return (output_root
            / f"{dt.year:04d}"
            / f"{dt.month:02d}"
            / f"{dt.strftime('%Y-%m-%d')}_{_safe_msgid_suffix(msg_id)}.eml")


# ── Sync ──────────────────────────────────────────────────────────────────

def _list_all_message_ids(service) -> list[str]:
    """
    Initial-sync helper: page through every message ID in the mailbox.
    Excludes labels we don't care about (DRAFT, CHAT, SPAM, TRASH).
    """
    ids: list[str] = []
    page_token = None
    while True:
        req = service.users().messages().list(
            userId="me",
            maxResults=500,
            pageToken=page_token,
            q="-in:spam -in:trash -in:drafts -in:chats",
        )
        resp = req.execute()
        for m in resp.get("messages", []):
            ids.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
        if len(ids) % 5000 == 0:
            log.info(f"  listed {len(ids)} messages so far...")
    return ids


def _list_history_changes(service, start_id: str) -> tuple[list[str], str | None]:
    """
    Return (added_message_ids, latest_history_id). Returns ([], None)
    on cursor-expired (HTTP 404), signaling the caller to fall back
    to a full re-list.
    """
    added: list[str] = []
    latest_history_id: str | None = None
    page_token = None
    try:
        while True:
            req = service.users().history().list(
                userId="me",
                startHistoryId=start_id,
                historyTypes=["messageAdded"],
                pageToken=page_token,
            )
            resp = req.execute()
            latest_history_id = resp.get("historyId", latest_history_id)
            for h in resp.get("history", []):
                for m in h.get("messagesAdded", []):
                    msg = m.get("message", {})
                    if not msg:
                        continue
                    label_ids = set(msg.get("labelIds", []))
                    if label_ids & EXCLUDED_LABELS:
                        continue
                    added.append(msg["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        if e.resp.status == 404:
            log.warning(
                "History cursor expired (older than ~7 days). "
                "Will fall back to a full re-list on next run.")
            return [], None
        raise
    return added, latest_history_id


def _fetch_and_save(service, msg_id: str, output_root: Path,
                    manifest_entries: dict) -> bool:
    """
    Fetch one message in raw form, base64url-decode it, write the .eml,
    and append a manifest entry. Returns True on success.
    """
    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="raw").execute()
    except HttpError as e:
        log.debug(f"Fetch failed for {msg_id}: {e}")
        return False

    raw = msg.get("raw", "")
    if not raw:
        return False
    try:
        eml_bytes = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception as e:
        log.debug(f"Base64 decode failed for {msg_id}: {e}")
        return False

    internal_date_ms = int(msg.get("internalDate", "0") or "0")
    eml_path = _eml_path_for(output_root, internal_date_ms, msg_id)
    eml_path.parent.mkdir(parents=True, exist_ok=True)

    tmp = eml_path.with_suffix(eml_path.suffix + ".tmp")
    tmp.write_bytes(eml_bytes)
    tmp.replace(eml_path)

    # Manifest enrichment: parse only the headers (cheap), and pull
    # Gmail-specific labels/threadId from the API response.
    entry = _build_manifest_entry(eml_bytes, msg, eml_path, output_root)
    if entry:
        try:
            rel = eml_path.resolve().relative_to(output_root.resolve())
            manifest_entries[rel.as_posix()] = entry
        except (ValueError, OSError):
            pass

    return True


_HEADER_PARSER = BytesHeaderParser()


def _build_manifest_entry(eml_bytes: bytes, gmail_msg: dict,
                          eml_path: Path, output_root: Path) -> dict | None:
    try:
        msg = _HEADER_PARSER.parsebytes(eml_bytes)
    except Exception:
        return None

    def _hdr(name: str) -> str:
        v = msg.get(name)
        return str(v).strip() if v else ""

    metadata = {
        "from":       _hdr("From"),
        "to":         _hdr("To"),
        "cc":         _hdr("Cc"),
        "subject":    _hdr("Subject"),
        "message_id": _hdr("Message-ID"),
        "thread_id":  gmail_msg.get("threadId", ""),
        "labels":     [str(l) for l in gmail_msg.get("labelIds", [])],
    }
    metadata = {k: v for k, v in metadata.items() if v}

    # Source timestamp: prefer Gmail's internalDate (always present and
    # tz-correct), fall back to Date header.
    source_ts = None
    internal_ms = int(gmail_msg.get("internalDate", "0") or "0")
    if internal_ms:
        dt = datetime.fromtimestamp(internal_ms / 1000.0, tz=timezone.utc)
        source_ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        date_hdr = msg.get("Date")
        if date_hdr:
            try:
                dt = parsedate_to_datetime(date_hdr)
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    source_ts = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError):
                pass

    entry: dict = {"metadata": metadata}
    if source_ts:
        entry["source_timestamp"] = source_ts
    return entry


# ── Manifest ──────────────────────────────────────────────────────────────

def _build_manifest_from_disk(output_root: Path) -> dict:
    """
    Walk all .eml files under output_root and rebuild the manifest
    from on-disk state. Used after every sync to guarantee no drift.

    NOTE: this only sets source_timestamp from the .eml's Date header
    and metadata fields from RFC822 headers. Gmail-specific fields
    (labels, threadId) that we recorded during _fetch_and_save are
    LOST in this rebuild path. We accept that for now: a re-walk only
    happens after a deliberate state reset.
    """
    entries: dict[str, dict] = {}
    for eml in output_root.rglob("*.eml"):
        try:
            with eml.open("rb") as f:
                msg = _HEADER_PARSER.parse(f)
        except OSError:
            continue

        def _hdr(name: str) -> str:
            v = msg.get(name)
            return str(v).strip() if v else ""

        metadata = {
            "from":       _hdr("From"),
            "to":         _hdr("To"),
            "cc":         _hdr("Cc"),
            "subject":    _hdr("Subject"),
            "message_id": _hdr("Message-ID"),
        }
        metadata = {k: v for k, v in metadata.items() if v}

        source_ts = None
        date_hdr = msg.get("Date")
        if date_hdr:
            try:
                dt = parsedate_to_datetime(date_hdr)
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    source_ts = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError):
                pass

        try:
            rel = eml.resolve().relative_to(output_root.resolve())
        except (ValueError, OSError):
            continue
        entry: dict = {"metadata": metadata}
        if source_ts:
            entry["source_timestamp"] = source_ts
        entries[rel.as_posix()] = entry

    return {
        "version":      1,
        "source_name":  "gmail",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entries":      entries,
    }


def _write_manifest(output_root: Path, manifest: dict) -> None:
    target = output_root / MANIFEST_FILENAME
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(target)
    log.info(f"Wrote manifest: {target} ({len(manifest['entries'])} entries)")


def _load_manifest(output_root: Path) -> dict:
    """Load existing manifest entries (for incremental updates)."""
    target = output_root / MANIFEST_FILENAME
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text())
        return data.get("entries", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> int:
    if "--auth" in sys.argv[1:]:
        if not _DEPS_OK:
            log.error(f"Google API libraries missing: {_DEPS_ERR}\n"
                      f"  pip install google-api-python-client google-auth-oauthlib")
            return 2
        cfg = _load_config()
        _ensure_creds(cfg, interactive=True)
        return 0

    if not _DEPS_OK:
        log.error(f"Google API libraries missing: {_DEPS_ERR}\n"
                  f"  pip install google-api-python-client google-auth-oauthlib")
        return 2

    cfg = _load_config()
    creds = _ensure_creds(cfg, interactive=False)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    state = _load_state(cfg["state_path"])
    output_root = cfg["output_root"]

    # Load existing manifest entries; we'll add/update in place rather
    # than rebuilding from disk, so the rich Gmail-specific metadata
    # we captured at fetch time survives the sync.
    manifest_entries = _load_manifest(output_root)

    # ── Determine what to fetch ──────────────────────────────────────────
    history_id = state.get("history_id")
    first_done = state.get("first_sync_completed", False)

    if history_id and first_done:
        log.info(f"Incremental sync from history_id={history_id}")
        added, new_history_id = _list_history_changes(service, history_id)
        if new_history_id is None:
            # cursor expired — fall through to full re-list
            log.info("Falling back to full message list")
            ids_to_fetch = _list_all_message_ids(service)
        else:
            ids_to_fetch = added
    else:
        log.info("First sync — listing all messages")
        ids_to_fetch = _list_all_message_ids(service)

    log.info(f"Fetching {len(ids_to_fetch)} message(s)")

    n_ok = 0
    n_fail = 0
    for i, mid in enumerate(ids_to_fetch, 1):
        if _fetch_and_save(service, mid, output_root, manifest_entries):
            n_ok += 1
        else:
            n_fail += 1
        if i % 100 == 0:
            log.info(f"  fetched {i}/{len(ids_to_fetch)}")

    # ── Persist state ────────────────────────────────────────────────────
    # Get the current profile's historyId so the next run knows where to
    # pick up. Always do this even if zero messages were fetched.
    try:
        profile = service.users().getProfile(userId="me").execute()
        new_history_id = str(profile.get("historyId", ""))
    except HttpError as e:
        log.warning(f"Could not fetch current historyId: {e}")
        new_history_id = history_id

    if new_history_id:
        state["history_id"] = new_history_id
    state["first_sync_completed"] = True
    state["last_sync_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_state(cfg["state_path"], state)

    # ── Manifest write ───────────────────────────────────────────────────
    manifest = {
        "version":      1,
        "source_name":  "gmail",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entries":      manifest_entries,
    }
    try:
        _write_manifest(output_root, manifest)
    except OSError as e:
        log.error(f"Manifest write failed: {e}")
        return 2

    log.info(f"Done: ok={n_ok} failed={n_fail} total_in_manifest={len(manifest_entries)}")

    if n_fail and n_ok == 0:
        return 2
    if n_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
