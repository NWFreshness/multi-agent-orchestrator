# Multi-Agent Orchestrator — Feature Spec Index

Source of truth for build order. One spec file per independently-shippable feature,
numbered `phase.feature` (maps to the writing-plans `features/phase-N-name/N.M-*`
convention, rooted here under `docs/specs/` to sit next to `docs/prd/`).
Status values: `planned | in-progress | in-review | done`.

PRD: [`docs/prd/multi-agent-orchestrator.md`](../prd/multi-agent-orchestrator.md)

## Phase 1 — MVP, fixed models (Kimi K3 orchestrator + DeepSeek V4.1 Flash subagents)

Phase acceptance (PRD §9): `/orchestrate "compare ORMs"` returns a synthesized
answer with ≥2 specialist outputs visible in the orchestration log.

| Feature | Spec | Status | Depends on | Blocks | Lands as |
| ------- | ---- | ------ | ---------- | ------ | -------- |
| 1.1 | [phase-1/1.1-skill-scaffold.md](phase-1/1.1-skill-scaffold.md) — SKILL.md + frontmatter, repo layout, README status | done | nothing | 1.2, 1.3 | PR #1 (merged d4dd0b5) |
| 1.2 | [phase-1/1.2-orchestrator-spawn.md](phase-1/1.2-orchestrator-spawn.md) — Orchestrator spawn contract + plan JSON schema | done | 1.1 | 1.4 | PR #2 (merged 9db85d8) |
| 1.3 | [phase-1/1.3-archetype-catalog.md](phase-1/1.3-archetype-catalog.md) — Static archetype catalog (`references/archetypes.md`) | done | 1.1 | 1.4 | PR #3 (merged 43264df) |
| 1.4 | [phase-1/1.4-plan-parse-validate.md](phase-1/1.4-plan-parse-validate.md) — Plan validator CLI (`scripts/validate_plan.py`, stdlib-only, `--self-test`) | done | 1.2, 1.3 | 1.5 | PR #4 (merged 5f5440f) |
| 1.5 | [phase-1/1.5-batch-dispatch.md](phase-1/1.5-batch-dispatch.md) — Batch dispatcher CLI (`scripts/dispatch.py` + `scripts/run_specialist.py`, stdlib-only, parallel batches) | done | 1.4 | 1.6, 1.7, 1.8 | PR #5 (merged 4f2c3ee) |
| 1.6 | [phase-1/1.6-synthesis.md](phase-1/1.6-synthesis.md) — Synthesis contract (`references/synthesis.md`; 4-field output JSON, gap rules, confidence rubric) | done | 1.5 | 1.7, 1.8 | PR #6 (merged 0be17ed) |
| 1.7 | [phase-1/1.7-error-recovery.md](phase-1/1.7-error-recovery.md) — Error recovery (`scripts/dispatch.py` retry-once + gap flagging per F1.6) | done | 1.5, 1.6 | 1.9 | PR #7 (merged fd3ac2e) |
| 1.8 | [phase-1/1.8-orchestration-log.md](phase-1/1.8-orchestration-log.md) — Orchestration log writer (`scripts/write_log.py`, stdlib-only, markdown output per PRD R9) | done | 1.5, 1.6 | 1.9 | PR #8 (merged cbd1039) |
| 1.9 | [phase-1/1.9-slash-command.md](phase-1/1.9-slash-command.md) — `/orchestrate` slash command registration (Hermes `commands.py` + `cli.py`) | done | 1.1–1.8 | 1.10 | PR #9 (merged 903a233) |
| 1.10 | [phase-1/1.10-tests.md](phase-1/1.10-tests.md) — Test driver (`scripts/run_tests.py`, stdlib-only, runs each --self-test and aggregates) | done | 1.1–1.9 | nothing | PR #10 (merged e9b6ff3) |

Critical path: 1.1 → 1.2 → 1.4 → 1.5 → 1.6 → 1.9 → 1.10. The long pole is the
1.4→1.5→1.6 chain (plan contract → dispatch → synthesis); 1.3 and 1.7/1.8 can
run as soon as their deps clear.

Parallelization notes: only ONE feature in-progress at a time (see
CURRENT_FEATURE.md) — this project paces "slow and controlled", so the table's
parallel options are intentionally unused until the user says otherwise.

## Phase 2 — Model selection & overrides (specs drafting; F2.1 + F2.2 docs-only)

| Feature | Spec | Status | Depends on | Blocks | Lands as |
| ------- | ---- | ------ | ---------- | ------ | -------- |
| 2.1 | [phase-2/2.1-model-resolution.md](phase-2/2.1-model-resolution.md) — 4-tier resolution chain (options > env → config → default) + `resolve_model()` helper contract | done | nothing | 2.3, 2.5 | PR #11 (merged f94a4c6) |
| 2.2 | [phase-2/2.2-independent-model-overrides.md](phase-2/2.2-independent-model-overrides.md) — D21: Orchestrator ⊥ subagent overrides (extends `references/model-resolution.md`) | done | nothing | 2.3, 2.5 | PR #11 (merged f94a4c6) |
| 2.3 | [phase-2/2.3-dry-run.md](phase-2/2.3-dry-run.md) — `--dry-run` plan preview (`scripts/plan_preview.py` + dispatch flag) | done | 2.1, 2.2 | 2.4 | PR #12 (merged 49104f3) |
| 2.4 | [phase-2/2.4-output-schema-enforcement.md](phase-2/2.4-output-schema-enforcement.md) — `output_schema` content enforcement (per-specialist JSON Schema, F1.6 gap rule integration) | done | 1.6 | 2.5 | PR #13 (merged ab8c7b5) |
| 2.5 | [phase-2/2.5-cost-budget-and-real-resolver.md](phase-2/2.5-cost-budget-and-real-resolver.md) — Cost-budget abort + real 4-tier `resolve_model` (`scripts/plan_preview.py` + dispatch flags) | done | 2.1, 2.2, 2.4 | nothing | PR #14 (merged dae6e66) |
| 2.6 | [phase-2/2.6-token-estimates.md](phase-2/2.6-token-estimates.md) — Per-call token estimate surfaced in trace + log (`--tokens-per-char N`, `Total estimated tokens: N` in aggregate) | done | nothing | nothing | PR #15 (merged fbcc855) |

Critical path: 2.1 + 2.2 → 2.3 → (2.5 in parallel with 2.3). F2.1 and F2.2 are
both docs-only and share the same `references/model-resolution.md`; the
implementer of either must read the other's spec before editing.

## Phase 3 — Advanced

Phase acceptance (PRD §9 + §10): pools reusable across sessions;
custom archetypes discoverable; cache hit path; streaming progress;
background run mode. Features are independent surfaces — build in
listed order.

| Feature | Spec | Status | Depends on | Blocks | Lands as |
| ------- | ---- | ------ | ---------- | ------ | -------- |
| 3.1 | [phase-3/3.1-specialist-pools.md](phase-3/3.1-specialist-pools.md) — Persistent specialist pools contract + example (`references/pools.md`, `pools/research-trio.json`; advisory, no script changes) | in-progress | nothing | nothing | TBD |
| 3.2 | (unwritten) — Custom archetypes via `~/.hermes/agents/*.yaml` | planned | nothing | nothing | TBD |
| 3.3 | (unwritten) — Cache layer (in-session + named namespaces) | planned | nothing | nothing | TBD |
| 3.4 | (unwritten) — Streaming progress to external channels | planned | nothing | nothing | TBD |
| 3.5 | (unwritten) — Async / background orchestration run mode | planned | nothing | nothing | TBD |

## Conventions

- One in-progress feature at a time (CURRENT_FEATURE.md is the lock).
- Each feature: implementer subagent → spec-compliance reviewer → quality reviewer
  → controller verification (structural checks / pytest from the controller session;
  subagent self-reports are never accepted as completion evidence) → squash-merge
  PR → append-only entry in CURRENT_FEATURE.md → spec status flipped to `done`.
- Controller bookkeeping (this index, CURRENT_FEATURE.md, PRD status) commits
  directly to `main`. Feature work always lands via feature-branch PR.
- This repo has no npm build. The project equivalent of "green" is: structural
  checks (frontmatter/layout/schema) + `pytest` once F1.10 lands.
