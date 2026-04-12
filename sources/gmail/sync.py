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
  FSEARCH_GMAIL_STATE_DB     sqlite DB of fetched messages (Phase 5.1)
                             (default: <output>/.gmail_state.sqlite)
  FSEARCH_GMAIL_MIRROR       if "true"/"1"/"yes", honor upstream Gmail
                             deletes by removing the corresponding .eml
                             file locally. Default is archive mode: local
                             files survive upstream deletes. See DESIGN.md.
  FSEARCH_GMAIL_LOG_LEVEL    DEBUG | INFO | WARNING (default INFO)

One-time setup: see DESIGN.md "Setup checklist" section.

Special invocation:
  ./sync.py --auth       Run the OAuth consent flow interactively
                         and save the refresh token. Required once
                         before the first headless run.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sqlite3
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
STATE_DB_FILENAME = ".gmail_state.sqlite"

# Filter out drafts and chat — see DESIGN.md "What this source does NOT do"
EXCLUDED_LABELS = {"DRAFT", "CHAT", "SPAM", "TRASH"}

# Env-var truthy values for FSEARCH_GMAIL_MIRROR
_TRUTHY = {"1", "true", "yes", "on"}


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
    state_db_path = Path(os.environ.get(
        "FSEARCH_GMAIL_STATE_DB", output_root / STATE_DB_FILENAME))

    # Archive mode is the default. Mirror mode (opt-in) propagates
    # Gmail deletes to the local .eml tree. See DESIGN.md for the
    # causal-chain rationale.
    mirror_mode = os.environ.get("FSEARCH_GMAIL_MIRROR", "").lower() in _TRUTHY

    output_root.mkdir(parents=True, exist_ok=True)
    if not os.access(output_root, os.W_OK):
        log.error(f"Output dir not writable: {output_root}")
        sys.exit(2)

    return {
        "output_root":   output_root,
        "creds_path":    creds_path,
        "token_path":    token_path,
        "state_path":    state_path,
        "state_db_path": state_db_path,
        "mirror_mode":   mirror_mode,
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


# ── State DB (Phase 5.1: skip-if-known ledger) ──────────────────────────────
#
# Separate from the JSON cursor state file: the JSON carries the Gmail
# history cursor (a single int, rewritten every run), the sqlite is the
# "what's on disk" ledger (append-mostly, one row per fetched message).
# Matches the Outlook COM exporter's pattern for the same reason:
# tens-of-thousands of entries make JSON atomic writes painful.

def _open_state_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetched (
            msg_id      TEXT PRIMARY KEY,
            relpath     TEXT NOT NULL,
            internal_ms INTEGER,
            fetched_at  TEXT
        )
    """)
    conn.commit()
    return conn


def _is_known(conn: sqlite3.Connection, msg_id: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM fetched WHERE msg_id = ? LIMIT 1", (msg_id,))
    return cur.fetchone() is not None


def _record_fetched(conn: sqlite3.Connection, msg_id: str,
                    relpath: str, internal_ms: int) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO fetched
            (msg_id, relpath, internal_ms, fetched_at)
        VALUES (?, ?, ?, ?)
    """, (msg_id, relpath, internal_ms,
          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))


def _forget_fetched(conn: sqlite3.Connection, msg_id: str) -> str | None:
    """
    Remove a message from the state DB. Returns the relpath that was
    recorded for it (caller uses this to delete the actual .eml file
    in mirror mode). Returns None if the msg_id wasn't tracked.
    """
    cur = conn.execute(
        "SELECT relpath FROM fetched WHERE msg_id = ?", (msg_id,))
    row = cur.fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM fetched WHERE msg_id = ?", (msg_id,))
    return row[0]


def _apply_delete(state_conn: sqlite3.Connection, output_root: Path,
                  msg_id: str, manifest_entries: dict) -> bool:
    """
    Mirror-mode delete handler: remove the state DB row AND the .eml
    file on disk AND the manifest entry, so the next fs_indexer run's
    purge pass notices the file is gone and deletes the Solr doc.

    Archive mode never calls this — see DESIGN.md "Mirror vs archive"
    for the causal-chain argument. Returns True if anything was
    removed.
    """
    relpath = _forget_fetched(state_conn, msg_id)
    if relpath is None:
        return False
    eml_path = output_root / relpath
    try:
        eml_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f"Could not delete {eml_path}: {e}")
    manifest_entries.pop(relpath, None)
    return True


# Filename pattern: "<yyyy-mm-dd>_<16-char-msgid>.eml"
# Gmail message IDs are 16-hex-char strings across current API output,
# so the filename suffix is losslessly round-trippable to the msg_id.
# See DESIGN.md "Migration helper" for the edge cases.
_EML_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_([A-Za-z0-9]{1,32})\.eml$")


def _msg_id_from_filename(filename: str) -> str | None:
    m = _EML_FILENAME_RE.match(filename)
    return m.group(1) if m else None


def _migrate_existing_tree(conn: sqlite3.Connection,
                           output_root: Path) -> int:
    """
    Backfill the state DB from an existing on-disk .eml tree. Used on
    first invocation after upgrading from Phase 5 to 5.1. Walks
    output_root, parses each filename for its embedded msg_id, and
    inserts a row. Returns the number of rows inserted.

    Idempotent: rows for already-known msg_ids are left untouched
    (INSERT OR IGNORE). Safe to re-run even if the DB is partially
    populated.
    """
    n_inserted = 0
    for eml in output_root.rglob("*.eml"):
        msg_id = _msg_id_from_filename(eml.name)
        if not msg_id:
            continue
        try:
            rel = eml.relative_to(output_root).as_posix()
        except ValueError:
            continue
        # Derive a plausible internal_ms from the filename date prefix
        # for sanity — this is only used for debug; fresh fetches will
        # overwrite with the real Gmail internalDate value.
        try:
            date_part = eml.name[:10]
            dt = datetime.strptime(date_part, "%Y-%m-%d")
            internal_ms = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
        except (ValueError, OSError):
            internal_ms = 0
        try:
            cur = conn.execute("""
                INSERT OR IGNORE INTO fetched
                    (msg_id, relpath, internal_ms, fetched_at)
                VALUES (?, ?, ?, ?)
            """, (msg_id, rel, internal_ms, "migrated"))
            if cur.rowcount > 0:
                n_inserted += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return n_inserted


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


def _list_history_changes(service, start_id: str
                          ) -> tuple[list[str], list[str], str | None]:
    """
    Return (added_message_ids, deleted_message_ids, latest_history_id).
    Returns ([], [], None) on cursor-expired (HTTP 404), signaling the
    caller to fall back to a full re-list.

    Phase 5.1: also captures `messagesDeleted` events so mirror mode
    can prune local files when Gmail state changes. In archive mode
    (the default) the caller ignores the deleted list.
    """
    added: list[str] = []
    deleted: list[str] = []
    latest_history_id: str | None = None
    page_token = None
    try:
        while True:
            req = service.users().history().list(
                userId="me",
                startHistoryId=start_id,
                historyTypes=["messageAdded", "messageDeleted"],
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
                for m in h.get("messagesDeleted", []):
                    msg = m.get("message", {})
                    if msg and msg.get("id"):
                        deleted.append(msg["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        if e.resp.status == 404:
            log.warning(
                "History cursor expired (older than ~7 days). "
                "Will fall back to a full re-list on next run.")
            return [], [], None
        raise
    return added, deleted, latest_history_id


def _fetch_and_save(service, msg_id: str, output_root: Path,
                    manifest_entries: dict,
                    state_conn: sqlite3.Connection) -> str:
    """
    Fetch one message in raw form, base64url-decode it, write the .eml,
    append a manifest entry, and record the fetch in the state DB.

    Returns one of:
      "fetched"  — new API fetch succeeded, file + DB row written
      "skipped"  — already in state DB, no API call made
      "failed"   — any error, including transient API failures

    The Phase-5 predecessor returned a bool (True/False); the three-way
    result lets main() distinguish "skipped for free" from "actually
    worked" for accurate reporting.
    """
    # Skip-if-known check — Phase 5.1 core value. For a 26k-message
    # mailbox this turns a ~4h foreground recovery into a ~5s no-op.
    if _is_known(state_conn, msg_id):
        return "skipped"

    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="raw").execute()
    except HttpError as e:
        log.debug(f"Fetch failed for {msg_id}: {e}")
        return "failed"

    raw = msg.get("raw", "")
    if not raw:
        return "failed"
    try:
        eml_bytes = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception as e:
        log.debug(f"Base64 decode failed for {msg_id}: {e}")
        return "failed"

    internal_date_ms = int(msg.get("internalDate", "0") or "0")
    eml_path = _eml_path_for(output_root, internal_date_ms, msg_id)
    eml_path.parent.mkdir(parents=True, exist_ok=True)

    tmp = eml_path.with_suffix(eml_path.suffix + ".tmp")
    tmp.write_bytes(eml_bytes)
    tmp.replace(eml_path)

    # Manifest enrichment: parse only the headers (cheap), and pull
    # Gmail-specific labels/threadId from the API response.
    entry = _build_manifest_entry(eml_bytes, msg, eml_path, output_root)
    relpath = ""
    if entry:
        try:
            relpath = eml_path.resolve().relative_to(
                output_root.resolve()).as_posix()
            manifest_entries[relpath] = entry
        except (ValueError, OSError):
            pass

    # Record the fetch in the state DB. This is what makes skip-if-known
    # work on the next run. Commit happens in batches in main() to avoid
    # one fsync per message.
    _record_fetched(state_conn, msg_id, relpath, internal_date_ms)
    return "fetched"


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


# ── Prune (Phase 5.1.5a: curation hard-delete tool) ────────────────────────
#
# Takes a list of filepaths (one per line, absolute paths under the
# configured output root) and atomically removes each message from the
# local archive:
#
#   1. state DB row (via msg_id looked up from relpath)
#   2. .eml file on disk
#   3. manifest entry
#
# Does NOT touch Gmail's servers — the user is expected to have already
# deleted the messages from Gmail via its web UI. If they haven't, the
# next incremental sync will happily re-download them because Gmail
# still reports them as present.
#
# Does NOT touch Solr — fs_indexer's per-source purge pass (scoped by
# source_name) handles that on the next indexer run.
#
# Source-agnostic philosophy: this function takes a list of paths, not
# a query. Whatever produced the list (GUI clipboard export, manual
# editing, CLI pipe from `fsearch`) is outside its scope.


def _load_prune_list(source: str) -> list[str]:
    """
    Read a list of paths from a file or stdin. `source == "-"` means
    stdin. Blank lines and lines starting with "#" are ignored. Leading
    and trailing whitespace on each line is stripped.
    """
    if source == "-":
        lines = sys.stdin.readlines()
    else:
        try:
            with open(source) as f:
                lines = f.readlines()
        except OSError as e:
            log.error(f"Cannot read prune list {source}: {e}")
            sys.exit(2)

    paths: list[str] = []
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        paths.append(s)
    return paths


def _prune_one(output_root: Path, state_conn: sqlite3.Connection,
               manifest_entries: dict, raw_path: str,
               dry_run: bool) -> str:
    """
    Process one path from the prune list. Returns one of:
      "pruned"        — fully removed (state DB + file + manifest)
      "would_prune"   — dry-run; no changes made
      "skipped"       — nothing to do (path doesn't exist anywhere)
      "rejected"      — path outside source root, or some safety check fired
      "failed"        — unexpected error during removal

    Safety checks:
      - Rejects paths that aren't under output_root (guards against
        fat-fingered copy-paste deleting random files)
      - Rejects non-absolute paths (forces the caller to be explicit)
    """
    try:
        p = Path(raw_path).resolve(strict=False)
    except (OSError, ValueError) as e:
        log.warning(f"Rejected (cannot resolve): {raw_path}: {e}")
        return "rejected"

    # Safety: must be under output_root
    try:
        rel = p.relative_to(output_root.resolve())
    except ValueError:
        log.warning(
            f"Rejected (outside source root): {raw_path} "
            f"(not under {output_root})")
        return "rejected"
    rel_posix = rel.as_posix()

    # Look up the state DB row
    cur = state_conn.execute(
        "SELECT msg_id FROM fetched WHERE relpath = ?", (rel_posix,))
    row = cur.fetchone()
    msg_id = row[0] if row else None

    file_exists = p.exists()

    if msg_id is None and not file_exists:
        # Nothing to do — neither the DB nor the disk knows about this path
        log.debug(f"Skipped (no trace on disk or state DB): {rel_posix}")
        return "skipped"

    if dry_run:
        parts = []
        if msg_id:
            parts.append("state DB row")
        if file_exists:
            parts.append(".eml file")
        if rel_posix in manifest_entries:
            parts.append("manifest entry")
        log.info(f"[dry-run] would remove {rel_posix} ({', '.join(parts)})")
        return "would_prune"

    try:
        if msg_id is not None:
            state_conn.execute(
                "DELETE FROM fetched WHERE msg_id = ?", (msg_id,))
        if file_exists:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        manifest_entries.pop(rel_posix, None)
    except (OSError, sqlite3.Error) as e:
        log.error(f"Failed to prune {rel_posix}: {e}")
        return "failed"

    return "pruned"


def _run_prune(prune_file: str, dry_run: bool, yes: bool) -> int:
    """
    Orchestrator for --prune / --prune-dry-run. Returns the exit code
    that main() should use.
    """
    cfg = _load_config()
    output_root = cfg["output_root"]
    paths = _load_prune_list(prune_file)

    if not paths:
        log.warning("Prune list is empty — nothing to do")
        return 0

    log.info(f"Prune list: {len(paths)} path(s) from "
             f"{prune_file if prune_file != '-' else '<stdin>'}")

    # Interactive confirmation for >10 paths (unless --yes or reading
    # from stdin/pipe, where prompting doesn't work anyway).
    if (not dry_run and not yes and len(paths) > 10
            and sys.stdin.isatty() and prune_file != "-"):
        log.warning(
            f"About to PERMANENTLY remove {len(paths)} message(s) from "
            f"the local archive under {output_root}.")
        log.warning("Type 'yes' to proceed, anything else to abort:")
        ans = input("> ").strip().lower()
        if ans != "yes":
            log.info("Aborted by user")
            return 0

    state_conn = _open_state_db(cfg["state_db_path"])
    manifest_entries = _load_manifest(output_root)

    counts = {"pruned": 0, "would_prune": 0, "skipped": 0,
              "rejected": 0, "failed": 0}

    try:
        for raw_path in paths:
            result = _prune_one(
                output_root, state_conn, manifest_entries,
                raw_path, dry_run)
            counts[result] = counts.get(result, 0) + 1

        if not dry_run:
            state_conn.commit()
    finally:
        state_conn.close()

    # Rewrite the manifest only if we made real changes. Atomic via
    # .tmp + rename. If this fails after the per-path removals were
    # committed, we log loudly and exit nonzero — the individual
    # removals stay but the manifest is stale. The Phase 3 manifest
    # reader handles missing-file entries gracefully, so fsearch's
    # correctness is preserved; only cleanliness suffers.
    if not dry_run and counts["pruned"] > 0:
        try:
            # Rebuild the full manifest dict (preserves version /
            # source_name / generated_at metadata)
            manifest = {
                "version":      1,
                "source_name":  "gmail",
                "generated_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "entries":      manifest_entries,
            }
            _write_manifest(output_root, manifest)
        except OSError as e:
            log.error(
                f"Manifest rewrite failed after pruning {counts['pruned']} "
                f"message(s). State DB and disk files are already updated; "
                f"the manifest may have stale entries. Error: {e}")
            return 2

    log.info(
        f"Prune summary: "
        f"pruned={counts['pruned']} "
        f"would_prune={counts['would_prune']} "
        f"skipped={counts['skipped']} "
        f"rejected={counts['rejected']} "
        f"failed={counts['failed']}"
        + (" (DRY-RUN)" if dry_run else "")
    )

    # Exit codes: 0 on full success, 1 on partial, 2 on hard failure.
    if counts["failed"] > 0:
        return 1
    if counts["rejected"] > 0 and counts["pruned"] == 0 and counts["would_prune"] == 0:
        return 2
    if counts["rejected"] > 0:
        return 1
    return 0


# ── Main ──────────────────────────────────────────────────────────────────

def _parse_argv() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Gmail pull source for fsearch/solrIndexer",
        epilog="See sources/gmail/DESIGN.md for the full rationale.")
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--auth", action="store_true",
        help="Run the OAuth consent flow interactively (one-time setup)")
    group.add_argument(
        "--prune", metavar="FILE",
        help="Hard-delete a list of messages from the local archive. "
             "FILE is a text file with one absolute path per line "
             "(use '-' to read from stdin). Does not touch Gmail or "
             "Solr — see DESIGN.md 'Destructive operations' section.")
    group.add_argument(
        "--prune-dry-run", metavar="FILE", dest="prune_dry_run",
        help="Like --prune but reports what would be removed without "
             "touching disk, state DB, or manifest.")
    p.add_argument(
        "--yes", action="store_true",
        help="Skip the interactive confirmation for --prune (>10 paths)")
    return p.parse_args()


def main() -> int:
    args = _parse_argv()

    # --auth is the only sub-command that works without full deps just
    # to fail cleanly; the sync and prune paths need the Google libs.
    if args.auth:
        if not _DEPS_OK:
            log.error(f"Google API libraries missing: {_DEPS_ERR}\n"
                      f"  pip install google-api-python-client google-auth-oauthlib")
            return 2
        cfg = _load_config()
        _ensure_creds(cfg, interactive=True)
        return 0

    if args.prune or args.prune_dry_run:
        # Prune doesn't need Google API libs — it's a local state
        # operation. But we still need the other stdlib imports we
        # already have.
        prune_file = args.prune or args.prune_dry_run
        return _run_prune(prune_file, dry_run=bool(args.prune_dry_run),
                          yes=args.yes)

    # Default path: sync
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

    # ── State DB (Phase 5.1) ─────────────────────────────────────────────
    # Skip-if-known ledger: one row per msg_id we've already fetched.
    # On first invocation after upgrading from Phase 5 (DB absent but
    # .eml files exist), backfill the DB from the on-disk tree so the
    # upgrade is transparent — no forced re-fetch of 26k messages.
    db_existed = cfg["state_db_path"].exists()
    state_conn = _open_state_db(cfg["state_db_path"])

    if not db_existed:
        log.info("State DB absent — checking for existing .eml tree to migrate")
        n_migrated = _migrate_existing_tree(state_conn, output_root)
        if n_migrated:
            log.info(
                f"Migrated {n_migrated:,} existing files to state DB "
                f"(Phase 5.0 → 5.1 upgrade path)")

    if cfg["mirror_mode"]:
        log.info("Mirror mode enabled (FSEARCH_GMAIL_MIRROR) — "
                 "Gmail deletes will prune local files")

    # ── Determine what to fetch ──────────────────────────────────────────
    history_id = state.get("history_id")
    first_done = state.get("first_sync_completed", False)
    deleted_ids: list[str] = []

    if history_id and first_done:
        log.info(f"Incremental sync from history_id={history_id}")
        added, deleted_ids, new_history_id = _list_history_changes(
            service, history_id)
        if new_history_id is None:
            # cursor expired — fall through to full re-list
            log.info("Falling back to full message list")
            ids_to_fetch = _list_all_message_ids(service)
            deleted_ids = []   # can't trust history deletes after cursor loss
        else:
            ids_to_fetch = added
    else:
        log.info("First sync — listing all messages")
        ids_to_fetch = _list_all_message_ids(service)

    log.info(f"Fetching {len(ids_to_fetch)} message(s)")

    n_fetched = 0
    n_skipped = 0
    n_fail = 0
    try:
        for i, mid in enumerate(ids_to_fetch, 1):
            result = _fetch_and_save(
                service, mid, output_root, manifest_entries, state_conn)
            if result == "fetched":
                n_fetched += 1
            elif result == "skipped":
                n_skipped += 1
            else:
                n_fail += 1

            # Commit the state DB periodically so a Ctrl-C doesn't lose
            # the last few hundred fetches. Balance between durability
            # and fsync overhead.
            if i % 100 == 0:
                state_conn.commit()
                log.info(
                    f"  progress {i}/{len(ids_to_fetch)} "
                    f"(fetched={n_fetched} skipped={n_skipped} failed={n_fail})")
    finally:
        state_conn.commit()

    # ── Mirror-mode delete processing ────────────────────────────────────
    n_deleted_locally = 0
    if cfg["mirror_mode"] and deleted_ids:
        log.info(
            f"Processing {len(deleted_ids)} upstream delete(s) (mirror mode)")
        for mid in deleted_ids:
            if _apply_delete(state_conn, output_root, mid, manifest_entries):
                n_deleted_locally += 1
        state_conn.commit()
        log.info(f"Mirrored {n_deleted_locally} delete(s) locally")
    elif deleted_ids and not cfg["mirror_mode"]:
        log.info(
            f"Archive mode: ignored {len(deleted_ids)} upstream delete(s). "
            f"Set FSEARCH_GMAIL_MIRROR=true to propagate deletes locally.")

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
        state_conn.close()
        return 2

    state_conn.close()

    log.info(
        f"Done: fetched={n_fetched} skipped_known={n_skipped} "
        f"failed={n_fail} deleted_local={n_deleted_locally} "
        f"total_in_manifest={len(manifest_entries)}"
    )

    # Only count real work when deciding partial/full failure:
    # "skipped_known" is the happy path for incremental runs.
    if n_fail and n_fetched == 0 and n_skipped == 0:
        return 2
    if n_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
