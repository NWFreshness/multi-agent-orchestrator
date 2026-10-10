# Multi-Agent Orchestrator

A Hermes Agent skill that turns a multi-domain task into a synthesized answer by dispatching ~10 specialized subagents through Hermes' native `delegate_task`.

See [`docs/prd/multi-agent-orchestrator.md`](./docs/prd/multi-agent-orchestrator.md) for the full product spec.

## Status

Phase 1 MVP shipped: 10/10 features done. See `docs/specs/INDEX.md` for status and PRs.

| Phase | Scope                                            | Status |
| ----- | ------------------------------------------------ | ------ |
| 1     | MVP, fixed models (Kimi K3 / DeepSeek V4.1 Flash) | Done   |
| 2     | Model selection & overrides                      | Done |
| 3     | Advanced (specialist pools, custom archetypes)   | In progress |

| Feature | Scope                                    | Status |
| ------- | ---------------------------------------- | ------ |
| F1.1    | Skill scaffold (SKILL.md + layout)       | Done   |
| F1.2    | Orchestrator spawn contract + plan       | Done   |
| F1.3    | Static archetype catalog (Appendix A)    | Done   |
| F1.4    | Plan parsing + validation                | Done   |
| F1.5    | Batch dispatch (no real LLM)             | Done   |
| F1.6    | Synthesis contract & output schema       | Done   |
| F1.7    | Error recovery (retry-once + gaps)       | Done   |
| F1.8    | Orchestration log writer (PRD R9)        | Done   |
| F1.9    | /orchestrate slash command registration | Done   |
| F1.10   | Test driver (`scripts/run_tests.py`)    | Done   |
| F2.3    | `--dry-run` plan preview                 | Done   |
| F2.4    | `output_schema` content enforcement     | Done   |
| F2.5    | Cost-budget abort + real `resolve_model` | Done   |
| F2.6    | Per-call token estimate surfaced in log | Done   |
| F3.1    | Persistent specialist pools (named experts) | Done |
| F3.2    | Custom archetypes (`~/.hermes/agents/*.yaml`) | Done |
| F3.3    | Specialist output cache (TTL + namespaces) | Done |
| F3.4    | Streaming progress to a file sink | Done |
| F3.5    | Async background run mode (`--background` detach) | In progress |

> **Note (F1.9):** the `/orchestrate` slash command is registered in the local
> Hermes checkout (`~/.hermes/hermes-agent`, branch
> `feat/orchestrate-slash-command`). It only takes effect on a fresh Hermes
> session — restart `hermes` (or open a new CLI window) after pulling.

## Layout

```
docs/
└── prd/
    └── multi-agent-orchestrator.md   # current PRD (foundation)
```

## License

MIT. See [LICENSE](./LICENSE).
