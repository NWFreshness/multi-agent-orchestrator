# CURRENT_FEATURE

## In progress

### 1.2 — Orchestrator spawn contract (`references/orchestrator.md`)

- Spec: `docs/specs/phase-1/1.2-orchestrator-spawn.md`
- Branch: `feat/1.2-orchestrator-spawn` (implementer creates from `main`)
- Status: implementer dispatch pending
- Gate: `references/orchestrator.md` exists with spawn preamble (exact model pin),
  plan JSON schema (7 specialist fields + batches + dependencies), plan rules
  (3–15/~10, reviewer-required, edges required, depth≤2); SKILL.md §4 pointer line
  present; no archetype content; controller structural checks green; two-stage
  review passed; squash-merged via PR.

## Completion log (append-only — newest at bottom)

- 2026-10-10 — 1.1 done: SKILL.md scaffold merged as d4dd0b5 via PR #1 (squash).
  Controller checks green + spec PASS + quality APPROVED (3 minor nits; 2 applied
  in-branch, 1 carried). Carry-forward: F1.6 spec must explicitly cover
  output-schema + synthesis contract (quality minor 3).
