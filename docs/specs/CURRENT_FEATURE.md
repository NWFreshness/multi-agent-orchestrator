# CURRENT_FEATURE

## In progress

### 1.3 — Static archetype catalog (`references/archetypes.md`)

- Spec: `docs/specs/phase-1/1.3-archetype-catalog.md`
- Branch: `feat/1.3-archetype-catalog` (implementer creates from `main`)
- Status: implementer dispatch pending
- Gate: `references/archetypes.md` with all 13 core + 5 optional archetypes
  (purpose + default tools per PRD Appendix A) + shape contract (7 fields +
  batches + dependencies); no SKILL.md diff; controller checks green; two-stage
  review passed; squash-merged via PR.

## Completion log (append-only — newest at bottom)

- 2026-10-10 — 1.1 done: SKILL.md scaffold merged as d4dd0b5 via PR #1 (squash).
  Controller checks green + spec PASS + quality APPROVED (3 minor nits; 2 applied
  in-branch, 1 carried). Carry-forward: F1.6 spec must explicitly cover
  output-schema + synthesis contract (quality minor 3).
- 2026-10-10 — 1.2 done: orchestrator spawn contract merged as 9db85d8 via PR #2 (squash).
  Spec PASS + quality APPROVED after one fix cycle (1 Critical + 2 Important + 4 Minor,
  all fixed in ccc2274; controller one-token pre-fix missed the §3 twin — quality caught it).
  Lesson persisted to subagent-driven-development skill (grep twins on reviewer-dictated fixes).
