# CURRENT_FEATURE

## In progress

### 1.7 — Error recovery (retry-once, gap flagging) in `scripts/dispatch.py`

- Spec: `docs/specs/phase-1/1.7-error-recovery.md`
- Branch: `feat/1.7-error-recovery` (implementer creates from `main`)
- Status: implementer dispatch pending
- Gate: extends `scripts/dispatch.py` with `--max-retries 1` (default 1) and
  `--retry-strategy {restart,continue}`; retry on `failed` specialists
  (status from F1.5 trace) is one additional `subprocess.run` with the same
  argv; `--self-test` covers retry-once, retry-exhausted-still-failed, and
  the no-retry-double-dispatch case; F1.6 contract integration: every
  `failed_specialists` MUST surface in `gaps` (delegated to F1.8 log writer
  if needed); existing F1.5 self-test still 4/4; two-stage review passed;
  squash-merged via PR.

## Completion log (append-only — newest at bottom)

- 2026-10-10 — 1.1 done: SKILL.md scaffold merged as d4dd0b5 via PR #1 (squash).
  Controller checks green + spec PASS + quality APPROVED (3 minor nits; 2 applied
  in-branch, 1 carried). Carry-forward: F1.6 spec must explicitly cover
  output-schema + synthesis contract (quality minor 3).
- 2026-10-10 — 1.2 done: orchestrator spawn contract merged as 9db85d8 via PR #2 (squash).
  Spec PASS + quality APPROVED after one fix cycle (1 Critical + 2 Important + 4 Minor,
  all fixed in ccc2274; controller one-token pre-fix missed the §3 twin — quality caught it).
  Lesson persisted to subagent-driven-development skill (grep twins on reviewer-dictated fixes).
- 2026-10-10 — 1.3 done: archetype catalog merged as 43264df via PR #3 (squash).
  Spec PASS + quality APPROVED (2 non-blocking minors applied in-branch 1be2afe +
  README drift fix). Toolchain note: host python has no pytest (→ 1.4 ships
  --self-test; 1.10 decides unittest-vs-pytest); jsonschema present but D7 locks
  stdlib-only for skill scripts.
- 2026-10-10 — 1.4 done: plan validator merged as 5f5440f via PR #4 (squash).
  Spec PASS after one fix (unhashable dep crash c184be2) + quality APPROVED
  after second fix (C1 iterative _check_nesting + I2/M1/M2/M4 polish 1b504e2).
  C1 was a real contract violation: deep nested JSON (depth ≥1500) would
  RecursionError out of validate() and dump a Python traceback — the
  pre-dispatch gate must NEVER traceback (SKILL.md §6). Fix: iterative DFS with
  MAX_NESTING_DEPTH=64, clean ERROR on overflow, sibling scan continues.
  Lesson: controller probe on depth-200 silently returned PLAN_VALID (no crash,
  no error) — that itself was wrong and the reviewer caught it. Persisted to
  subagent-driven-development: a self-test that returns PASS for adversarial
  input is a false positive, not a green.
- 2026-10-10 — 1.5 done: batch dispatcher merged as 4f2c3ee via PR #5 (squash).
  Spec PASS; quality REQUEST_CHANGES (1 Important: IndexError on out-of-range
  batch index; 4 Minor) → fix ff322ba closed the gap + 2 free wins; quality
  re-review APPROVED. Two probes the controller got wrong (via `| tail` pipe
  capturing tail's exit) cost a false-positive cycle: real ExitCode capture
  must be `cmd; echo $?`, not `cmd | tail; echo $?`. The skill docs are the
  contract; deferred polish (DEFAULT_MAX_PARALLEL, silent simulate:N overflow)
  is tracked, not lost.
- 2026-10-10 — 1.6 done: synthesis contract merged as 0be17ed via PR #6 (squash).
  Spec PASS; quality APPROVED after one polish commit (2be298f moved a
  conflated "high|medium|low" annotation out of a json fence into a markdown
  table). Both worked examples parse as real JSON; D11/D12 contracts preserved.
  5/5 quality re-review PASS.
