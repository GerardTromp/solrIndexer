# Checkpoint System — fsearch

## Directory Layout

```
.claude/
├── checkpoints/           # Full and incremental snapshots
├── context/               # Persistent project knowledge
│   ├── 01-architecture.md
│   ├── 02-codebase-map.md
│   ├── 03-data-models.md
│   ├── 04-patterns.md
│   ├── 05-decisions.md
│   ├── 06-dependencies.md
│   ├── 07-gotchas.md
│   └── 08-progress.md
├── sessions/
├── memory-bank/
└── README.md
```

## Quick Recovery

1. Current state: `.claude/context/08-progress.md`
2. Full checkpoint: most recent in `.claude/checkpoints/`
3. Known issues: `.claude/context/07-gotchas.md`
