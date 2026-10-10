# Orchestrator Spawn Contract (F1.2)

Source of truth for how any parent session spawns the Orchestrator identically.
Copy-paste §1 as the spawn prompt; the plan shape in §2 is normative.
References: PRD §§6 (R2, R3, R6, R8, R15), 8; Appendix A (archetype names only).

## 1. Spawn preamble — copy-paste exactly

```text
You are the Orchestrator for the multi-agent-orchestrator skill (PRD §8).
Model: opencode-go/kimi-k3 (pinned for Phase 1 — do not substitute; if
unavailable, fail with an actionable message rather than substituting).
Toolset: file access, terminal (read-only), delegate_task for subagent
dispatch, web_search / web_extract as the task requires.
Depth: max_spawn_depth = 2 — specialists may spawn helpers, but nothing may
recurse deeper; plans requiring depth > 2 are invalid.
Required inputs: TASK: <the user task verbatim> and WORKSPACE: <absolute
workspace path for logs and artifacts>.
Duty: analyze TASK, return a plan in exactly the JSON shape defined in
references/orchestrator.md §2, obeying the plan rules in §4. Do not dispatch
specialists yourself — the parent validates the plan and dispatches batches.
Specialist model default: opencode-go/deepseek-v4.1-flash (Phase 1, fixed).
```

## 2. Plan JSON schema

Field names are locked. The Orchestrator must return exactly this shape:

```json
{
  "task_summary": "short-slug",
  "specialists": [
    {"id": "r1", "role": "researcher", "goal": "one sentence + success criterion",
     "context": "task-relevant background and inputs",
     "output_schema": "{...json schema...}",
     "tools": ["file", "terminal"],
     "model": "opencode-go/deepseek-v4.1-flash"}
  ],
  "batches": [[0, 1], [2]],
  "dependencies": {"wr": ["r1"]}
}
```

- `"task_summary"`: short slug naming the task (e.g. `compare-top-5-orms`).
- `"specialists"`: array of specialist entries, each with all 7 fields:
  - `"id"`: unique string within the plan (e.g. `"r1"`, `"cmp"`, `"cr"`).
  - `"role"`: archetype name (e.g. `researcher`, `coder-impl`, `reviewer-spec`,
    `critic`). Names only — archetypes are defined in
    `references/archetypes.md` (F1.3), never here.
  - `"goal"`: one sentence stating what to produce, including its success criterion.
  - `"context"`: task-relevant background and inputs for this specialist
    (task slice, relevant upstream outputs, role instructions) — never the full
    conversation.
  - `"output_schema"`: the JSON schema the specialist's result must conform to
    (schema content is defined in F1.6, not here).
  - `"tools"`: subset of the parent session's tools granted to this specialist
    (least privilege; e.g. researcher gets read/search, not shell or write).
  - `"model"`: specialist model; defaults to
    `opencode-go/deepseek-v4.1-flash` in Phase 1. Omit only to take the default.
- `"batches"`: `number[][]` — INDEX arrays into `specialists[]`; each inner
  array is dispatched as ONE `delegate_task` call (see §3).
- `"dependencies"`: `Record<id, id[]>` — maps specialist id → prerequisite ids
  for the synthesizer's ordering (see §3).

## 3. Index-vs-ID rule (read carefully)

`"batches"` uses INDICES (numbers positional into `specialists[]`);
`"dependencies"` uses IDS (the `"id"` strings). Mixing them up is the most
likely future bug: `{"batches": [[0, 1]]}` means "first and second specialists
in parallel", while `{"dependencies": {"2": ["r1"]}}` is keyed by id strings.
The F1.4 validator must enforce this; when in doubt, re-check this section.

## 4. Plan rules (normative)

1. 3–15 specialists per plan, target ~10 (PRD R3).
2. At least one `reviewer-*` or `critic` role in every plan (PRD §13 mitigation,
   promoted to a hard rule).
3. Dependency edges required wherever ordering matters; independent specialists
   run in parallel batches.
4. Nesting depth ≤ 2 (`max_spawn_depth = 2`, PRD R6); deeper plans are invalid.
5. Invalid plans are rejected with a clear error and NEVER dispatched (PRD R3).

## 5. Short-circuit (parent-side, before spawn)

Short-circuit (PRD R15) is decided by the PARENT before spawn, never by the
Orchestrator: tasks that are already small (≤1 domain, ≤3 obvious steps) run
inline or via a single subagent, never reach the Orchestrator, and are logged
as `short_circuit`. Only non-trivial tasks are spawned per §1.

## 6. Post-dispatch duties

1. Recovery per PRD R8: each failed or timed-out specialist is retried ONCE
   with an adjusted prompt; persistent failures are recorded and synthesis
   proceeds with available outputs rather than hanging (procedure detail is
   F1.7, not here).
2. Synthesis: merge specialist outputs into a single answer, noting dissent
   and confidence where specialists disagree (schema content is F1.6, not here).
3. Log: every run writes the orchestration log so the user can read roles,
   prompts, status, timing, and cost (format is F1.8, not here).

## 7. Out of scope here

Archetype definitions (F1.3), plan validator code (F1.4), dispatch call
mechanics (F1.5), synthesis/output-schema content (F1.6), recovery procedure
detail (F1.7). This file states the contract only.
