# Multi-Agent Orchestrator

A Hermes Agent skill that turns a multi-domain task into a synthesized answer by dispatching ~10 specialized subagents through Hermes' native `delegate_task`.

See [`docs/prd/multi-agent-orchestrator.md`](./docs/prd/multi-agent-orchestrator.md) for the full product spec.

## Status

Phase 1 — foundation: **PRD only**. Skill implementation has not started.

| Phase | Scope                                            | Status     |
| ----- | ------------------------------------------------ | ---------- |
| 1     | MVP, fixed models (Kimi K3 / DeepSeek V4.1 Flash) | Not started |
| 2     | Model selection & overrides                      | Not started |
| 3     | Advanced (specialist pools, custom archetypes)   | Not started |

## Layout

```
docs/
└── prd/
    └── multi-agent-orchestrator.md   # current PRD (foundation)
```

## License

MIT. See [LICENSE](./LICENSE).
