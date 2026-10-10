# Synthesis Contract (F1.6)

Source of truth for how the Orchestrator merges specialist outputs into one
final answer. Captures the carry-forward from F1.1 (quality minor 3, output
schema deferral in `references/archetypes.md` §3 now closes here) and gives
F1.7 (recovery) and F1.8 (log writer) a stable, locked output shape.

References: PRD §§6 (R7, R8), 8; `references/orchestrator.md` §6 (re-invocation
model); `references/archetypes.md` §3 (output schema deferral closed here);
F1.5 stub output shape (`logs/specialists/<id>.<n>.json`).

Decisions locked here:
- **D11** — four canonical output fields are LOCKED: `answer`, `key_points`,
  `confidence`, `gaps`. Optional fields (`sources`, `failed_specialists`) are
  NOT required in v0.1; they are mentioned as future additions only.
- **D12** — `confidence` is a literal enum, exactly one of `high`, `medium`,
  `low`. It is NEVER null and NEVER a number. The Orchestrator may override
  the rubric-derived value with a one-sentence reasoning appended to
  `answer`.

## 1. Re-invocation pattern

After the parent dispatches every batch via F1.5 (see
`scripts/dispatch.py PLAN.json` → `logs/orchestration-trace.jsonl`), the
parent **re-invokes the Orchestrator** as a single, fresh subagent call,
handing it the aggregated specialist outputs (the files written under
`logs/specialists/` plus the trace).

This is the actor model already stated in `references/orchestrator.md` §6:
"After the parent dispatches batches and collects outputs, it re-invokes the
Orchestrator with the results; the Orchestrator then: 1. Recovery per PRD R8,
2. Synthesis, 3. Log." F1.6 LOCKS that re-invocation as the only entry point
for synthesis — there is no separate "synthesizer agent", no in-band merge
inside the dispatch loop, and no parent-side merge that bypasses the
Orchestrator. This does not introduce any new conflict with
`references/orchestrator.md` §6.

The re-invocation prompt carries exactly the input shape described in §2
below. The Orchestrator's only duty in response is to produce one output of
the shape described in §3 below, populated per the rules in §4 and §5.

## 2. Synthesizer input shape

The Orchestrator's re-invocation prompt (parent → Orchestrator) carries a
single object with **exactly** these fields:

```json
{
  "task_summary": "<short slug, copied from the plan>",
  "specialists": [
    {
      "id": "<specialist id, e.g. r1>",
      "role": "<archetype name from references/archetypes.md>",
      "goal": "<the goal string from the plan>",
      "output": "<parsed JSON object the F1.5 stub wrote, OR null>"
    }
  ],
  "trace": "<absolute path to logs/orchestration-trace.jsonl, or the trace lines verbatim>",
  "errors": [],
  "warnings": []
}
```

Field rules:

- `task_summary` (string) — copied verbatim from `plan.task_summary` so the
  Synthesizer can frame the answer in the original task's terms.
- `specialists` (array, length = number of specialists in the plan) — one
  entry per specialist, in plan order. Each entry carries `id`, `role`,
  `goal`, and `output`. `output` is the parsed JSON body of the file the
  F1.5 stub wrote at `logs/specialists/<id>.<n>.json`. If the trace marked
  the specialist `failed`, `output` is `null` AND the entry carries an
  extra `failure_reason` string (e.g. `"timeout after 30s"`,
  `"non-zero exit"`, `"malformed payload"`).
- `trace` (string) — the Orchestrator must be able to look at status,
  elapsed_ms, and batch index for every specialist. The parent may pass
  either an absolute path (`"<workspace>/logs/orchestration-trace.jsonl"`)
  or the trace lines verbatim inline; either is allowed as long as the
  status flags are reachable.
- `errors` (array) — orchestrator-side errors the parent observed during
  dispatch (re-validation failures, IO problems, missing log dir, etc.).
  May be empty.
- `warnings` (array) — non-fatal issues (a plan re-validated warning that
  still dispatched, a stub that completed but with a deprecation notice,
  etc.). May be empty.

The synthesizer never invents fields beyond these five. If the parent
omits `errors`/`warnings`, they default to `[]`. If `trace` is a path, the
Orchestrator reads it; if it's inline lines, the Orchestrator parses them
in place.

## 3. Output JSON schema

The Orchestrator returns **exactly one** JSON object with four canonical
fields. All four are required. The shape:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `answer` | string | yes | synthesized result the user reads; always non-empty; Markdown allowed |
| `key_points` | list of string | yes | 0+ concise findings |
| `confidence` | enum | yes | one of: `high`, `medium`, `low` (never null, never a number) |
| `gaps` | list of string | yes | 0+ entries; see gap-flagging rules in §4 |

Field rules:

- `answer` (string) — the synthesized result the user actually reads. It is
  always non-empty. Markdown is allowed and preferred when the task
  warrants it. The Orchestrator may append a one-sentence override note
  here (see §5) — that note is part of `answer`, not a separate field.
- `key_points` (array of strings, 0+) — short bullets distilling the
  answer. May be empty when the answer itself is the bullet.
- `confidence` (string, enum: exactly `"high"`, `"medium"`, or `"low"`) —
  never null, never a number. Default is the rubric value from §5.
- `gaps` (array of strings, 0+) — facts or perspectives the synthesis
  could not confirm (failed specialists, missing citations, dissent). May
  be empty. Populated per §4.

Optional fields, **NOT required in v0.1** (mentioned as future, will NOT
appear in this contract):

- `sources` (array of strings) — URLs / file paths cited, drawn from a
  researcher/reviewer specialist. F1.7 may add; F1.10 may add a JSON
  Schema for it.
- `failed_specialists` (array of strings) — ids of specialists the trace
  marked failed, even when `gaps` already covers them. F1.7 may add as a
  convenience for the recovery layer.

### Worked example, high confidence

```json
{
  "answer": "Synthesized result text the user actually reads.",
  "key_points": ["First concise finding.", "Second concise finding."],
  "confidence": "high",
  "gaps": []
}
```

### Worked example, low confidence (one failed specialist)

```json
{
  "answer": "Best-effort result; the benchmark numbers are not included.",
  "key_points": ["Recommendation: postgres.js."],
  "confidence": "low",
  "gaps": ["bench-runner specialist failed; benchmark numbers missing."]
}
```

Both examples MUST parse as valid JSON and MUST satisfy §4's gap-flagging
rules.

## 4. Gap-flagging rules

The Orchestrator populates `gaps` per this locked, ordered list. Rules
(a)–(c) are mandatory; (d) is a soft flag.

(a) **Mandatory** — every specialist in `failed_specialists` (or, in v0.1,
every specialist whose `output` is `null` because the trace marked them
`failed`) MUST appear as a `gaps` entry. The entry names the specialist id
and the failure reason in plain language, e.g. `"bench-runner specialist
failed; benchmark numbers missing."`. This applies even when `answer` does
not depend on that specialist. Schema-mismatch failures (F2.4) count as
failed specialists for the purpose of this rule.

(b) **Mandatory** — contradictions between specialists' `output` values
are surfaced in `gaps` with both sides quoted verbatim in the same entry,
so a downstream reader can see the dissent without re-parsing every
specialist file. Format: `'<id1>: "<quote1>"; <id2>: "<quote2>"'`.

(c) **Mandatory** — any specialist whose `output` is `null` for a
NON-failure reason (e.g. file unreadable, JSON malformed, output_schema
violation) MUST appear in `gaps` with the reason. This is distinct from
(a): here the failure is a payload problem, not an execution failure.

(d) **Soft flag (MAY)** — low-citation claims (any claim in `answer` or
`key_points` that no researcher/reviewer specialist backed with a
`sources` entry, in the future when `sources` is implemented) MAY appear
as a `gaps` entry. This is non-binding in v0.1; F1.10 may tighten it once
a JSON Schema exists.

The combined effect of rule (a) and the confidence rubric (§5) is: a
single failed specialist forces BOTH a `gaps` entry AND `confidence: low`.
This is intentional and is documented here so F1.7 does not re-litigate
the pairing.

`gaps` entries are short strings. They are not structured objects in v0.1.

## 5. Confidence rubric

`confidence` is set by default from this locked table. Exactly four
conditions.

| Condition                | Confidence |
| ------------------------ | ---------- |
| 0 gaps, 0 failed         | `high`     |
| 1–2 gaps, 0 failed       | `medium`   |
| > 2 gaps, 0 failed       | `low`      |
| any failed specialist    | `low`      |

Rules derived from the table:

- "failed" counts specialists whose `output` is `null` BECAUSE the trace
  marked them `failed` (rule (a)). It does NOT count null output from rule
  (c) alone — that case follows the `gaps`-count rows above.
- The four rows are evaluated in priority order: a single failed
  specialist locks `low` regardless of how many gaps exist.
- An Orchestrator override is permitted: the Orchestrator MAY set
  `confidence` to a different literal than the rubric produces, but only
  if it appends a one-sentence reasoning to `answer` explaining the
  override (e.g. `"Confidence raised to medium: the dissenting specialist
  was a reviewer whose rubric conflicts with three coder outputs; the
  codebase reality favors the coder consensus."`). The override note is
  part of `answer`, not a separate field.

## 6. What the synthesizer is NOT

Out-of-scope, explicitly. Each line points to the responsible feature; the
synthesizer never overlaps with it.

- The synthesizer does **not dispatch** specialists — the parent does,
  via `scripts/dispatch.py PLAN.json` (F1.5).
- The synthesizer does **not retry** failed specialists — F1.7
  (`phase-1/1.7-error-recovery.md`) owns the retry-once rule per PRD R8
  and surfaces `failed_specialists` for the synthesizer's §4(a) to consume.
- The synthesizer does **not write the orchestration log file** — F1.8
  (`phase-1/1.8-orchestration-log.md`) owns the human-readable
  `orchestration-log-<timestamp>.md` and consumes the §3 output directly.
- The synthesizer does **not validate the plan** — F1.4
  (`scripts/validate_plan.py`) owns plan validation; the parent runs it
  before any dispatch, and the synthesizer receives only already-validated
  inputs.

This list is normative: a future implementation that does any of these
four things is in violation of F1.6, regardless of how cleanly it does
them.
