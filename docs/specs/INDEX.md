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
| 1.5 | [phase-1/1.5-batch-dispatch.md](phase-1/1.5-batch-dispatch.md) — Batch dispatcher CLI (`scripts/dispatch.py` + `scripts/run_specialist.py`, stdlib-only, parallel batches) | in-progress | 1.4 | 1.6, 1.7, 1.8 | PR `feat/1.5-batch-dispatch` |
| 1.6 | phase-1/1.6-synthesis.md — synthesizer step, structured output | planned | 1.5 | 1.7, 1.8 | PR `feat/1.6-synthesis` |
| 1.7 | phase-1/1.7-error-recovery.md — retry-once, gap flagging, `failed_specialists` | planned | 1.5, 1.6 | 1.9 | PR `feat/1.7-error-recovery` |
| 1.8 | phase-1/1.8-orchestration-log.md — orchestration-log writer | planned | 1.5, 1.6 | 1.9 | PR `feat/1.8-orchestration-log` |
| 1.9 | phase-1/1.9-slash-command.md — `/orchestrate` registration | planned | 1.1–1.8 | 1.10 | PR `feat/1.9-slash-command` |
| 1.10 | phase-1/1.10-tests.md — unit + integration tests, deterministic mocks | planned | 1.1–1.9 | nothing | PR `feat/1.10-tests` |

Critical path: 1.1 → 1.2 → 1.4 → 1.5 → 1.6 → 1.9 → 1.10. The long pole is the
1.4→1.5→1.6 chain (plan contract → dispatch → synthesis); 1.3 and 1.7/1.8 can
run as soon as their deps clear.

Parallelization notes: only ONE feature in-progress at a time (see
CURRENT_FEATURE.md) — this project paces "slow and controlled", so the table's
parallel options are intentionally unused until the user says otherwise.

## Phase 2 — Model selection & overrides (specs unwritten; author after Phase 1 ships)

F2.1 model resolution (options → env → config → defaults) · F2.2 independent
overrides · F2.3 `--dry-run` · F2.4 `output_schema` enforcement · F2.5 budget
abort · F2.6 token estimates.

## Phase 3 — Advanced (specs unwritten)

F3.1 specialist pools · F3.2 custom archetypes · F3.3 cache · F3.4 streaming ·
F3.5 background mode.

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
