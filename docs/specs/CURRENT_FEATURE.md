# CURRENT_FEATURE

## In progress

### 3.1 — Persistent specialist pools (named experts)

- Spec: `docs/specs/phase-3/3.1-specialist-pools.md` (drafted, in-progress)
- Branch: `feat/3.1-specialist-pools`
- Status: implementer dispatch pending
- Gate: contract-only per F1.3/F2.1 precedent — `references/pools.md` +
  `pools/research-trio.json` (D38 repo-local, D39 F1.2 7-field members,
  D40 advisory + concrete, zero `scripts/` changes). Two-stage review +
  controller green; squash-merged via PR.

## Completion log (append-only — newest at bottom)

- 2026-10-10 — 2.6 done: per-call token estimate merged as fbcc855 via PR #15 (squash).
  Spec PASS, all 5 adversarial probes (a–e). D35–D37 verified. Token-stamp
  pass runs unconditionally so --no-schema-check still gets the field.
  **Phase 2 MVP shipped.** 15 PRs total across Phase 1 (10) + Phase 2 (5);
  full test surface green: validate_plan 16/16, validate_schema 22/22,
  write_log 8/8, plan_preview 13/13, dispatch 15/15, run_tests 39/39.
  Open carry-forwards into Phase 3: (a) F1.6 synthesis output_schema content
  (quality minor 3 from PR #1); (b) the F2.6 token approximation is a stub
  waiting for real provider estimates; (c) the F1.9 /orchestrate slash
  command lives on a local Hermes feature branch, not pushed upstream
  (user approval needed).

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
 - 2026-10-10 — 1.7 done: dispatcher retry-once merged as fd3ac2e via PR #7 (squash).
 Spec PASS 9/9; quality APPROVED after controller-applied spec-body polish
 ca9567f (`retries: <N+1>` → `retries: <N>`, body now matches formal def).
 The `retry_test_overrides` private kwarg is the right call: env var needs
 the stub change (excluded), argv sentinel pollutes argparse, plan key / per-spec
 field fail F1.4 E1 strict validation. 4/4 quality re-review PASS.
 - 2026-10-10 — 1.8 done: orchestration log writer merged as cbd1039 via PR #8 (squash).
 Spec PASS 12/12 (incl. 5 adversarial probes); quality REQUEST_CHANGES for
 README drift (F1.5–F1.8 rows still said "In progress"); controller applied
 all 4 flips in a single amend. No behavior changes. No M1/M2 fixes (cosmetic,
 out of PR scope). Phase 1 is now 8/10 — only the slash command (1.9) and
 the test suite (1.10) remain before MVP ships.
 - 2026-10-10 — 1.9 done: /orchestrate slash command merged as 903a233 via PR #9 (squash).
 Spec PASS 14/14; quality APPROVED with 2 minor follow-ups (restart
 dependency + README status flip) — controller applied both in the same
 amend. Local Hermes registration lives on
 `~/.hermes/hermes-agent/feat/orchestrate-slash-command` @ c646cea433
 (NOT pushed to NousResearch; user approval needed). 1.10 is the last
 feature — test driver. After that, Phase 1 MVP ships.
