# Multi-Agent Orchestrator

A Hermes Agent skill that turns a multi-domain task into a synthesized answer by dispatching ~10 specialized subagents through Hermes' native `delegate_task`.

See [`docs/prd/multi-agent-orchestrator.md`](./docs/prd/multi-agent-orchestrator.md) for the full product spec.

## Status

Phase 1 — foundation: **In progress**. Per-feature status lives in `docs/specs/INDEX.md`; PRD is complete.

| Phase | Scope                                            | Status      |
| ----- | ------------------------------------------------ | ----------- |
| 1     | MVP, fixed models (Kimi K3 / DeepSeek V4.1 Flash) | In progress |
| 2     | Model selection & overrides                      | Not started |
| 3     | Advanced (specialist pools, custom archetypes)   | Not started |

| Feature | Scope                                 | Status      |
| ------- | ------------------------------------- | ----------- |
| F1.1    | Skill scaffold (SKILL.md + layout)    | Done        |
| F1.2    | Orchestrator spawn contract + plan    | Done        |
| F1.3    | Static archetype catalog (Appendix A) | Done        |
| F1.4    | Plan parsing + validation             | Done        |
| F1.5    | Batch dispatch (no real LLM)         | In progress |
| F1.6    | Synthesis contract & output schema   | In progress |
| F1.7    | Error recovery (retry-once + gaps)   | In progress |

## Layout

```
docs/
└── prd/
    └── multi-agent-orchestrator.md   # current PRD (foundation)
```

## License

MIT. See [LICENSE](./LICENSE).
