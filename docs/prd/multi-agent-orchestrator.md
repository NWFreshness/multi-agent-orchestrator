# PRD: Multi-Agent Orchestrator Skill for Hermes Agent

**Owner:** Crash (builder profile) / Tyler Mayfield
**Status:** In Build
**Last updated:** 2026-10-10
**Target release:** Phase 1 — v0.1 (skill MVP)
**Links:** `hermes-agent` skill, `subagent-driven-development` skill, `dispatching-parallel-agents` skill

---

## Change History

| Date       | Author | Change                              | Reason / Decision              |
| ---------- | ------ | ----------------------------------- | ------------------------------ |
| 2026-10-09 | Crash  | Initial PRD drafted from user brief | Working spec for Phase 1 build |
| 2026-10-10 | Crash  | Phase 1 build started; PRD → In Build | User green-lit F1.1–F1.10; build scaffolding (specs INDEX, CURRENT_FEATURE) on main |

---

## 1. Executive Summary

A Hermes skill that, given a user task, automatically spawns a dedicated **Orchestrator** agent which analyzes the task, dynamically chooses an optimal set of ~10 specialized subagents (researcher, coder, reviewer, planner, critic, etc.), dispatches them through Hermes' native `delegate_task` mechanism in parallel batches, coordinates their work, and returns a synthesized result. Phase 1 ships with fixed models (Kimi K3 for Orchestrator, DeepSeek V4.1 Flash for subagents via the OpenCode Go provider). Phase 2 surfaces user-visible model selection and overrides. Phase 3 introduces persistent specialist pools and custom templates.

The skill is the user-facing front door to multi-agent work in Hermes; everything below it should reuse — never reinvent — Hermes' existing delegation primitives.

---

## 2. Problem Statement

### User problem
Tyler (and any Hermes user) has many tasks that are **multi-domain** (a real example: "compare three Postgres migration tools, write a benchmark, run it against a local DB, draft a PR, and review the diff"). To get a high-quality answer today they must manually:

1. Decompose the task into sub-tasks.
2. Decide which agents / roles handle each piece.
3. Launch each via `delegate_task` (often by hand), passing context to each.
4. Read N raw subagent outputs, reconcile conflicts, de-dupe, and synthesize a final answer.

Manual orchestration is **expensive** (in attention and prompt engineering), **error-prone** (a missing specialist leaves a hole in the final answer), and **inconsistent** (different sessions produce different decompositions for the same task).

### Business / opportunity
A reusable orchestrator skill turns multi-agent work into a single-command experience, gives the builder profile a force-multiplier on every phased build, and makes Hermes' delegation substrate visible and useful to users who do not want to write orchestrator prompts by hand.

### Current workaround
Manually write orchestrator prompts in the controller session, invoke `delegate_task` once per specialist, then synthesize inline. Existing skills (`dispatching-parallel-agents`, `subagent-driven-development`) describe the pattern but do not execute it.

### Why now
- `delegate_task` is mature (parallel batches, max_spawn_depth, background mode with delivery).
- OpenCode Go provider (`opencode-go/kimi-k3`, `opencode-go/deepseek-v4.1-flash`) gives cheap, fast inference well-suited to high-volume specialist work.
- Builder profile is running 2+ multi-phase projects (Atrium, business-acceleration-2027) where a "one command → high-quality multi-agent output" workflow would immediately pay back.

---

## 3. Goals & Non-Goals

### Goals
- **G1.** A user invokes the skill with a task description and receives one **synthesized** result, not a dump of raw subagent outputs.
- **G2.** The skill decomposes the task into a **specialized** (non-identical) set of subagents chosen for the task's actual shape, target ≈ 10 specialists.
- **G3.** Subagents run in **parallel batches** wherever the task allows, never sequentially where the work is independent.
- **G4.** Phase 1 uses **fixed** OpenCode Go models for cost / simplicity: `opencode-go/kimi-k3` (Orchestrator), `opencode-go/deepseek-v4.1-flash` (subagents).
- **G5.** Phase 2 lets the user **override** the Orchestrator model and the subagent model independently, at minimum via env vars or skill options.
- **G6.** The skill is **observable**: per-subagent role, status, token/cost estimate, and elapsed time are surfaced.
- **G7.** The skill reuses Hermes primitives (`delegate_task`, `process_manage`, `browser_exec`, `terminal`, etc.) rather than inventing parallel infrastructure.
- **G8.** The skill works both **interactively** (a user typed `orchestrate …`) and in **autonomous** mode (a cron job or another agent invoked it).

### Non-Goals
- Implementing a general-purpose multi-agent runtime. Hermes already has one; we orchestrate on top of it.
- Training or fine-tuning any model.
- Building a visual workflow editor / DAG UI.
- Cross-process / cross-host orchestration. v1 runs inside a single Hermes session tree.
- Replacing `subagent-driven-development` for plans that need SDD's two-stage review loop (the orchestrator may **call into** SDD for code-heavy tasks, but it is not itself a SDD replacement).
- Phase 1: user-overridable models, persistent specialist pools, custom user-defined archetypes.

---

## 4. Users & Personas

### Primary persona: the builder (Tyler)
- Context: working on multi-phase projects (Atrium, business-acceleration-2027, Cascade). Time-boxed to ~20 hrs/wk for the business side.
- Jobs-to-be-done: turn a fuzzy multi-domain ask into a single structured output without hand-orchestrating subagents.
- Pain: prompt-writing fatigue, missed-decomposition holes, inconsistent output quality between runs.
- Success criterion: one command → a final answer he can paste into a PR description, a client email, or a Linear ticket with light editing.

### Secondary persona: another Hermes agent
- Context: an autonomous workflow needs a high-quality multi-domain answer as one step in a larger pipeline (e.g., weekly review, lead-research, code migration).
- Jobs-to-be-done: invoke the skill programmatically with a structured task, receive a structured result back.
- Success criterion: deterministic invocation shape, predictable output schema, no interactive prompts.

---

## 5. User Stories / Example Tasks (concrete)

The user types (or programmatically invokes):

> `/orchestrate <task>`

**Story 1 — Research synthesis (low complexity).**
> "Compare the top 5 ORMs for Node.js in 2026 across performance, DX, and Postgres support. Cite sources."

Specialists chosen: `researcher-1`, `researcher-2`, `researcher-3`, `comparator`, `citation-checker`, `writer`, `critic`. Synthesis: ranked table + 1-page recommendation.

**Story 2 — Code change + benchmark (medium complexity).**
> "Migrate `src/db.ts` from `pg.Pool` to `postgres.js`, add a benchmark that loads 10k rows, and open a draft PR."

Specialists: `planner`, `coder-impl`, `coder-tests`, `bench-runner`, `reviewer-spec`, `reviewer-quality`, `pr-drafter`. Synthesis: diff summary + benchmark numbers + PR URL.

**Story 3 — Multi-domain consulting deliverable (high complexity).**
> "Build a 2-week onboarding plan for a 12-person accounting firm moving from QuickBooks Desktop to a SaaS stack. Include roles, timeline, risks, and tooling."

Specialists: `domain-expert-accounting`, `domain-expert-saas-migration`, `planner`, `risk-analyst`, `researcher-tools`, `writer-proposal`, `critic`, `formatter-doc`. Synthesis: Markdown deliverable with sections.

**Story 4 — Autonomous / scheduled.**
> A weekly cron job invokes `/orchestrate "Summarize my last 7 days of completed Linear tickets, group by project, flag blocked items"`.

Specialists: `data-extractor`, `grouper`, `blocker-detector`, `writer-summary`. Synthesis: short report delivered to the configured channel.

**Story 5 — Failure recovery scenario.**
A `researcher` subagent hits a web rate limit and times out. The skill: (a) marks the subagent failed in the orchestration log, (b) asks the Orchestrator to either retry once with a different prompt or substitute a cached result if available, (c) returns a synthesized answer that **flags the gap explicitly** rather than hiding it.

---

## 6. Functional Requirements

| ID      | Priority | Requirement                                                                                                                                                                                     | Acceptance criteria                                                                                                                                                                 |
| ------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **R1**  | Must     | The skill is invokable via Hermes slash command (`/orchestrate <task>`), `skill_view` / `skill_manage`, and programmatically (returns a structured result).                                   | All three invocation paths work; structured output schema documented in `references/output-schema.md`.                                                                              |
| **R2**  | Must     | The skill spawns exactly one Orchestrator subagent with the model `opencode-go/kimi-k3` in Phase 1.                                                                                             | Orchestrator subagent's `delegation.provider` / `delegation.model` resolves to `opencode-go/kimi-k3`; verified by re-reading config and the Orchestrator's transcript header.       |
| **R3**  | Must     | The Orchestrator analyzes the task and chooses a **specialized** set of subagents (target ≈ 10, allow 3–15) drawn from the archetype catalog (Appendix).                                       | Orchestrator returns a JSON plan with: chosen roles, per-role goal, per-role context, dependency edges, batch grouping. Plan is validated before any `delegate_task` is issued. |
| **R4**  | Must     | The skill dispatches subagents using Hermes' `delegate_task`, with `toolsets` and `context` provided per specialist.                                                                           | Each specialist is launched via `delegate_task(goal=…, context=…, output_schema=…)`. No reinvention of dispatch.                                                                     |
| **R5**  | Must     | Subagents run in **parallel batches** wherever the dependency graph allows it.                                                                                                                 | Orchestrator's plan includes `batches: number[][]` (batches of parallelizable subagent IDs). Skill dispatches each batch in a single `delegate_task` array call.                   |
| **R6**  | Must     | Orchestrator respects `max_spawn_depth = 2`: specialists may themselves spawn helpers, but the Orchestrator cannot recurse into a fourth level.                                                | A specialist attempting to spawn a sub-sub-sub-agent is rejected with a clear error; logged in the orchestration trace.                                                            |
| **R7**  | Must     | The skill aggregates subagent outputs into a single synthesized answer, never returning a raw dump.                                                                                          | Final output passes through an explicit "synthesizer" step (the Orchestrator, or a designated synthesizer role). Output contains: answer, key points, confidence, gaps, sources. |
| **R8**  | Must     | **Error handling**: timeouts, partial failures, and aborts are handled. At minimum: 1 retry per specialist with adjusted prompt; final output flags any specialist that ultimately failed. | Given an injected failure in 1 of 10 specialists, the skill returns a synthesized result within the configured timeout and an explicit `failed_specialists: []` list in the output. |
| **R9**  | Must     | Each specialist's role, prompt, status, elapsed time, and token/cost estimate is recorded in an orchestration log the user can read.                                                          | `references/orchestration-log.md` is created at `<workspace>/orchestration-log-<timestamp>.md` after every run.                                                                    |
| **R10** | Must     | Phase 1 uses fixed models: Orchestrator = `opencode-go/kimi-k3`; subagents = `opencode-go/deepseek-v4.1-flash`.                                                                                 | Verified via skill unit tests (see §11).                                                                                                                                            |
| **R11** | Should   | Phase 2: user can override the Orchestrator model and subagent model independently via (a) skill options, (b) `HERMES_ORCHESTRATOR_MODEL` / `HERMES_SUBAGENT_MODEL` env vars, (c) per-profile config. | A user setting `HERMES_ORCHESTRATOR_MODEL=minimax/MiniMax-M3` causes the Orchestrator to spawn under that model; subagents remain on the configured model unless overridden.       |
| **R12** | Should   | The skill **caches** prior specialist outputs for repeated subtasks within a session (or across sessions if a cache key namespace is provided).                                                  | Re-running `/orchestrate "compare X vs Y"` within 24h reuses the `researcher` outputs from the prior run if the cache key matches.                                                  |
| **R13** | Should   | User can preview the Orchestrator's plan **before** specialists are spawned (a `--plan-only` or `--dry-run` flag).                                                                              | With dry-run, the skill returns the plan JSON and exits without dispatching any specialist.                                                                                        |
| **R14** | Should   | Output schema is enforceable via JSON Schema (`output_schema` on the synthesizer call).                                                                                                        | A test enforces the schema on a canned task.                                                                                                                                       |
| **R15** | Should   | The skill detects when the task is **already small** (≤1 domain, ≤3 obvious steps) and short-circuits to a single subagent or runs it inline.                                                   | For "what is 2+2" the skill returns immediately without spawning the Orchestrator; logged as "short_circuit".                                                                      |
| **R16** | Could    | Phase 3: persistent specialist pools (named experts that survive across sessions).                                                                                                              | A user can define `my-research-pool: [arxiv-fetcher, citation-checker, technical-writer]` and reference it by name.                                                                |
| **R17** | Could    | Phase 3: custom specialist archetypes created via YAML files in `~/.hermes/agents/`.                                                                                                            | Skill discovers and offers them in plan output alongside defaults.                                                                                                                  |
| **R18** | Could    | Phase 3: streaming progress to a configured channel (Telegram, log file).                                                                                                                       | Orchestration log streams line-by-line to the configured `deliver` target.                                                                                                          |

---

## 7. Non-Functional Requirements

| Concern         | Requirement                                                                                                                                                                                                                  |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Cost control** | Phase 1: total tokens per orchestration run must fit inside a configurable budget (default 500k input / 200k output, abort-with-warning if exceeded). Subagent model is intentionally a flash-tier model for cost.            |
| **Latency**     | p50 end-to-end orchestration for a 5-specialist task ≤ 90 s wall; p90 ≤ 240 s. Phase 1 is not latency-optimized; we surface elapsed time per specialist so the user sees where time goes.                                       |
| **Context mgmt** | Each specialist receives a trimmed context bundle (task, relevant upstream outputs, role instructions) — never the full conversation. Controller keeps an orchestration trace, not a content dump.                              |
| **Reliability**  | A failed or timed-out specialist must not block the final synthesis. Synthesis runs against whatever completed, with explicit gap flags.                                                                                       |
| **Safety**       | No specialist may run `dangerous_command` confirmation prompts unilaterally. The Orchestrator inherits the parent session's approval posture; specialists must call `browser_vault_*` / `vetted-tool` paths only when authorized. |
| **Observability** | Every run produces: `orchestration-log.md`, optional `trace.jsonl`, cost estimate, per-specialist status. Errors are emitted as actionable messages, not stack traces.                                                       |
| **Composability** | The skill is one tool a parent agent can call. It does not fork the world; it returns a structured result suitable for further steps.                                                                                         |
| **Testability**  | Unit tests cover: plan parsing, batch dispatching, schema validation, short-circuit detection, model override resolution. Integration tests use a deterministic mock Orchestrator for CI.                                       |
| **Privilege**    | Specialists inherit the parent session's tool approvals only when the parent explicitly grants; otherwise they get a reduced toolset. Model-level approval never escalates privilege.                                          |

---

## 8. High-Level Architecture / Flow

```
                         ┌──────────────────────────┐
   user / parent agent → │  /orchestrate <task>     │
                         └──────────┬───────────────┘
                                    │
                                    ▼
                      ┌────────────────────────────────┐
                      │  short-circuit check (R15)     │
                      └────────────┬───────────────────┘
                                   │ non-trivial
                                   ▼
                      ┌────────────────────────────────┐
                      │  Spawn Orchestrator            │
                      │  model=opencode-go/kimi-k3     │
                      │  tools=[file,delegate_task,…]  │
                      └────────────┬───────────────────┘
                                   │ plan JSON
                                   ▼
                      ┌────────────────────────────────┐
                      │  Validate plan (R3, R6)        │
                      │   roles / deps / batches /     │
                      │   depth=2                      │
                      └────────────┬───────────────────┘
                                   │ batch[i]
                                   ▼
                      ┌────────────────────────────────┐
                      │  For each batch in parallel    │
                      │   delegate_task(...) × N       │
                      └────────────┬───────────────────┘
                                   │ raw outputs
                                   ▼
                      ┌────────────────────────────────┐
                      │  Error-recovery loop (R8)      │
                      │   retry-once / substitute      │
                      └────────────┬───────────────────┘
                                   │ recovered outputs
                                   ▼
                      ┌────────────────────────────────┐
                      │  Synthesizer (Orchestrator or  │
                      │  dedicated synthesizer role):  │
                      │   deltas, dedup, gaps, format   │
                      └────────────┬───────────────────┘
                                   │ final answer
                                   ▼
                      ┌────────────────────────────────┐
                      │  Output: structured result +   │
                      │  orchestration-log.md          │
                      └────────────────────────────────┘
```

### Delegation call shape (illustrative)

```yaml
# Orchestrator returns this plan
plan:
  task_summary: "compare-top-5-orms-node-2026"
  specialists:
    - { id: r1, role: researcher, goal: "list top 5 ORMs with 2025-2026 evidence" }
    - { id: r2, role: researcher, goal: "collect benchmark data per ORM" }
    - { id: cmp, role: comparator, goal: "score ORMs on perf/dx/postgres" }
    - { id: cite, role: citation-checker, goal: "verify each claim has a source" }
    - { id: wr, role: writer, goal: "draft 1-page recommendation" }
    - { id: cr, role: critic, goal: "stress-test recommendation against counter-arguments" }
  batches:
    - [r1, r2]
    - [cmp]
    - [cite, cr]
    - [wr]
```

```python
# Skill dispatches each batch in one delegate_task call
delegate_task(tasks=[
  { "goal": "<r1 prompt>", "context": "...", "output_schema": <research v1> },
  { "goal": "<r2 prompt>", "context": "...", "output_schema": <research v1> },
])
```

---

## 9. Phased Delivery

### Phase 1 — MVP, fixed models
**Outcome:** running skill, default models, end-to-end loop, orchestration log, basic error handling.

- F1.1: SKILL.md + frontmatter (name, description, version, model defaults).
- F1.2: Orchestrator spawn with `opencode-go/kimi-k3`.
- F1.3: Static archetype catalog (Appendix).
- F1.4: Plan parsing + validation.
- F1.5: Batch dispatch via `delegate_task`.
- F1.6: Synthesis step.
- F1.7: Error / timeout / partial-failure handling.
- F1.8: Orchestration log writer.
- F1.9: `/orchestrate` slash command registration.
- F1.10: Unit + integration tests with deterministic mocks.

**Acceptance:** `/orchestrate "compare ORMs"` returns a synthesized answer with ≥2 specialist outputs visible in the log, defaulting to Kimi K3 / DeepSeek V4.1 Flash.

### Phase 2 — Model selection & overrides
- F2.1: Resolve model from options → env vars → config → defaults.
- F2.2: Independent override for Orchestrator model and subagent model.
- F2.3: `--dry-run` plan preview.
- F2.4: Output schema enforcement via `output_schema`.
- F2.5: Cost-budget abort with warning.
- F2.6: Per-call token estimate surfaced in log.

### Phase 3 — Advanced
- F3.1: Persistent specialist pools (named experts).
- F3.2: Custom archetypes via `~/.hermes/agents/*.yaml`.
- F3.3: Cache layer (in-session + named cache namespaces).
- F3.4: Streaming progress to external channels (Telegram, Slack, log).
- F3.5: Async / background orchestration run mode.

---

## 10. Success Metrics

| Metric                                | Target (Phase 1)                  | Target (Phase 3)                       |
| ------------------------------------- | --------------------------------- | -------------------------------------- |
| Synthesized answer produced           | 100% of non-trivial runs          | 100%                                   |
| Specialist specialization score       | ≥70% of chosen specialists are non-redundant (manual spot-check on 20 runs) | ≥85%                  |
| p50 latency for 5-specialist task     | ≤90s wall                         | ≤60s                                   |
| Cost per orchestration                | bounded by configurable budget    | bounded; budget auto-tuned             |
| User override of model (Phase 2+)      | n/a                               | ≥10% of runs use a non-default model    |
| Skill invocation paths                | 3 (slash, programmatic, skill call) | 3 + cron / background                   |

**Qualitative gate:** the builder reports that `/orchestrate` reduced his prompt-writing time on a 2-feature build by ≥50%.

---

## 11. Testing & Verification

- **Unit:** plan parser, batch builder, model override resolver, short-circuit detector, output schema validator.
- **Integration:** canned task with deterministic mock Orchestrator → assert dispatched specialist IDs, batches, and synthesis call.
- **Live smoke:** 1 research task + 1 code task + 1 short-circuited task, observed end-to-end against the configured provider.
- **Failure injection:** force a specialist timeout, assert recovery + gap flagging.
- **Budget injection:** force a model that reports a high token count, assert budget-abort path.
- **Verification posture:** after every implementation claim, controller runs the project's test/build commands itself — subagent self-reports are not trusted for completion.

---

## 12. Assumptions, Constraints, Dependencies

**Assumptions**
- `delegate_task` with parallel arrays, `output_schema`, and `max_spawn_depth` semantics are stable in the deployed Hermes version.
- `opencode-go/kimi-k3` and `opencode-go/deepseek-v4.1-flash` are available as named models in the target profile.
- The Orchestrator model is strong enough to produce a structured plan with roles, dependencies, and batches reliably.

**Constraints**
- Phase 1 fixed models — no UI for selection.
- Specialists cannot exceed `max_spawn_depth = 2`.
- Cost budget per run is hard-coded in Phase 1 (env-overridable later).

**Dependencies**
- Hermes `delegate_task`, `process_manage`, slash-command registration, skill frontmatter conventions.
- OpenCode Go provider config.
- Optional (Phase 2+): web/email/messaging channels for streaming delivery.

---

## 13. Risks & Mitigations

| Risk                                                   | Impact             | Likelihood | Mitigation                                                                                              | Owner |
| ------------------------------------------------------ | ------------------ | ---------- | ------------------------------------------------------------------------------------------------------- | ----- |
| Orchestrator's plan is too coarse or too granular     | Quality drops      | Medium     | Validate plan: min 3, max 15 specialists; require at least one `reviewer`/`critic`; require dependency edges | Crash |
| Specialists churn through tokens on a long task       | Cost / latency     | High       | Configurable budget; per-specialist input trim; abort-with-warning above budget                          | Crash |
| `max_spawn_depth = 2` is hit by a specialist          | Run fails mid-way  | Medium     | Pre-validate plan; reject plans with depth > 2 before dispatch; clear error message                     | Crash |
| Subagent hallucinates conflicting facts               | Synthesis noisy    | Medium     | Dedicated `critic` + `citation-checker` specialists; synthesizer must call out contradictions           | Crash |
| Provider outage / rate limit                           | Run fails          | Medium     | 1 retry per specialist; flag gaps in output; document fallback to in-line execution in skill error msg | Crash |
| Skill becomes a magic black box                        | Trust erosion      | Medium     | Mandatory orchestration log per run; `--dry-run` to inspect plan; structured output schema enforced    | Crash |
| User override of model breaks assumed capability      | Garbage output     | Medium     | Output schema validates shape even if content is junk; budget abort path                                | Crash |
| Long-running orchestration exceeds controller focus    | Attention drain    | Medium     | Phase 3 background mode + delivery channel; Phase 1 surfaces elapsed time so user can interrupt        | Crash |

---

## 14. Open Questions & Decisions

### Open questions

| Question                                                                                        | Owner | Status   |
| ----------------------------------------------------------------------------------------------- | ----- | -------- |
| What is the right max specialist count for a 1-line task vs. a 2-week project? Static or dynamic? | Crash | Open     |
| Should the synthesizer be the Orchestrator itself or a separate role?                           | Crash | Open     |
| Where does the archetype catalog live — inside the skill or in a separate referenced file?      | Crash | Resolved: inside the skill as `references/archetypes.md`. |
| Should `output_schema` be defined by the Orchestrator or by the skill (per archetype)?          | Crash | Open     |
| How do we surface per-specialist cost without depending on provider-side accounting?            | Crash | Open     |

### Decisions

| Date       | Decision                                                              | Rationale                                                      |
| ---------- | --------------------------------------------------------------------- | -------------------------------------------------------------- |
| 2026-10-09 | Phase 1 fixed models (Kimi K3 / DeepSeek V4.1 Flash)                  | Cost control; avoids premature configuration surface area      |
| 2026-10-09 | Reuse `delegate_task`, do not invent a new dispatcher                 | Hermes already provides parallel arrays, schemas, depth limits |
| 2026-10-09 | Target ≈ 10 specialists, allow 3–15                                    | Matches user's stated target; allows flexibility               |
| 2026-10-09 | Default `max_spawn_depth = 2`                                         | Prevents runaway recursion; aligns with SDD precedent          |
| 2026-10-09 | Synthesizer must produce structured output, not free-form dump      | User said output must be synthesized, not a raw dump           |
| 2026-10-10 | Skill name `multi-agent-orchestrator`, slash `/orchestrate`         | `orchestrate` / `swarm` / `chorus` as skill name               | User confirmed primary proposal; PRD §15 `name:` corrected in F1.1 spec |

---

## 15. Suggested Skill Metadata (for SKILL.md frontmatter)

```yaml
---
name: orchestrate
description: Use when the user asks for a multi-domain, multi-agent task — spawns an Orchestrator that selects ~10 specialized subagents, dispatches them in parallel via Hermes delegate_task, and returns a synthesized answer. Triggers on /orchestrate, "run a multi-agent", "orchestrate this", or any task that obviously spans research, coding, review, and synthesis.
version: 0.1.0
metadata:
  hermes:
    tags: [orchestration, multi-agent, delegation, synthesis, parallel]
    related_skills: [subagent-driven-development, dispatching-parallel-agents, hermes-agent]
---
```

**Naming note:** the user offered "Multi-Agent Task Orchestrator Skill" as a working name. The skill name should be short and slash-command-friendly; I propose **`orchestrate`** for the slash command and **`multi-agent-orchestrator`** as the canonical skill name. Frontmatter `description` must be self-contained within the first 57 characters per skill rules — the first sentence above does that.

---

## Appendix A — Recommended Default Specialist Archetypes

The Orchestrator chooses **from** this catalog; it does not need to use every one, and it may invent task-specific roles (the catalog also documents the *shape* of a good archetype so the Orchestrator can synthesize new ones safely).

### Core roles (always-consider)
| Archetype    | Purpose                                                                             | Default tools                  |
| ------------ | ----------------------------------------------------------------------------------- | ------------------------------ |
| `planner`    | Decomposes the user task into sub-questions; produces the dependency graph          | file, terminal (read)          |
| `researcher` | Pulls fresh or cited information from sources (web, files, repos, docs)             | web_search, web_extract, file, terminal (read) |
| `comparator` | Lays out N options side-by-side on user-chosen axes                                 | file, terminal (read)          |
| `coder-impl` | Writes code against a precise spec                                                  | file, terminal                 |
| `coder-test` | Writes / runs tests for `coder-impl`'s output                                       | file, terminal                 |
| `reviewer-spec`   | Verifies the implementation matches the original spec                          | file, terminal                 |
| `reviewer-quality` | Reviews code quality, security, performance                                    | file, terminal                 |
| `critic`     | Steelmans / attacks the emerging synthesis                                          | file                           |
| `risk-analyst` | Identifies failure modes, edge cases, mitigations                                  | file, terminal (read)          |
| `writer`     | Produces the final user-facing artifact (Markdown, doc, PR description)             | file                           |
| `formatter-doc` | Cleans up structure, linting, formatting                                          | file, terminal                 |
| `pr-drafter` | Opens a PR / commit / file with a draft description                                 | terminal, github               |
| `data-extractor` | Pulls structured data from a source (Linear, DB, file)                          | file, terminal, integrations   |

### Optional / task-specific roles
- `domain-expert-<field>` — invoked by the Orchestrator when a task is clearly tied to a non-generic domain (e.g., `domain-expert-accounting`).
- `citation-checker` — verifies each claim's source.
- `blocker-detector` — finds blocked work items / dead-ends.
- `bench-runner` — runs benchmarks and reports numbers.
- `summarizer` — collapses long outputs to a TL;DR.

### Archetype shape (the contract)
Every chosen archetype gets, at minimum:
- `id` (unique within the plan)
- `role` (one of the above or synthesized)
- `goal` (one sentence, success criterion)
- `context` (input bundle: task, relevant upstream outputs, role instructions)
- `output_schema` (JSON schema, defined per archetype or per Orchestrator plan)
- `tools` (subset of the parent session's tools)
- `model` (default: `opencode-go/deepseek-v4.1-flash` in Phase 1)

The Orchestrator's plan must also include:
- `batches: number[][]` — each inner array is dispatched in a single `delegate_task` call.
- `dependencies: Record<id, id[]>` — for the synthesizer to know ordering.

---

## Appendix B — Glossary

- **Orchestrator** — the single dedicated agent that analyzes the task, picks specialists, and synthesizes the final answer.
- **Specialist / subagent** — any agent spawned by the Orchestrator (or by another specialist up to `max_spawn_depth = 2`).
- **Synthesizer** — step that merges specialist outputs. By default the Orchestrator itself; Phase 3 may allow a dedicated `synthesizer` role.
- **Plan** — the JSON document the Orchestrator produces describing what specialists to dispatch, with what dependencies and batches.
- **Orchestration log** — the per-run human-readable trace written to `<workspace>/orchestration-log-<timestamp>.md`.
- **Archetype** — a named specialist role with a known purpose and toolset.
- **OpenCode Go** — the Hermes provider namespace `opencode-go/`, used for Phase 1 models.
