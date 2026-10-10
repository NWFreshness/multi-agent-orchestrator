---
name: multi-agent-orchestrator
description: Use when a task needs multiple specialized subagents — spawns an Orchestrator that selects ~10 specialists, dispatches them in parallel via delegate_task, and returns a synthesized answer.
version: 0.1.0
metadata:
  hermes:
    tags: [orchestration, multi-agent, delegation, synthesis, parallel]
    related_skills: [subagent-driven-development, dispatching-parallel-agents, hermes-agent]
---

# Multi-Agent Orchestrator

## 1. Overview

This skill turns a multi-domain task into a synthesized answer. The parent session spawns exactly one Orchestrator subagent, which analyzes the task, selects a set of specialized subagents (target ~10), dispatches them in parallel batches via Hermes' native `delegate_task`, coordinates their work, and returns a single synthesized result. Every run is recorded in a human-readable orchestration log. Phase 1 uses fixed models and a static archetype catalog; model overrides and custom archetypes are explicitly out of scope until Phases 2–3.

## 2. When to Use

Trigger this skill when a task spans multiple domains or needs several specialized perspectives in parallel — e.g. research-then-build tasks, comparative analysis, or review work that benefits from researcher, coder, reviewer, planner, and critic roles working concurrently.

Do NOT trigger it for single-domain tasks: per PRD R15, tasks that are already small (≤1 domain, ≤3 obvious steps) short-circuit to a single subagent or run inline without spawning the Orchestrator. Simple questions, single-file edits, and anything one specialist could answer directly must never pay the orchestration overhead.

## 3. Phase 1 Pinned Models

Phase 1 uses fixed models — there are no user overrides. The Orchestrator always runs on `opencode-go/kimi-k3`; every specialist subagent always runs on `opencode-go/deepseek-v4.1-flash`. Model selection and override surfaces arrive in Phase 2; do not add configuration knobs for them here. If either pinned model is unavailable in the target profile, fail with an actionable message rather than silently substituting.

## 4. The Loop

Follows PRD §8. Short-circuit: the parent session first checks the task against R15 (≤1 domain, ≤3 obvious steps); small tasks run inline or via a single subagent and are logged as `short_circuit`.

Spawn Orchestrator: otherwise the parent spawns exactly one Orchestrator subagent on `opencode-go/kimi-k3` (PRD R2) with the task and workspace context.

Plan: the Orchestrator analyzes the task and returns a JSON plan — chosen roles, per-role goal, per-role context, dependency edges, and batch grouping (PRD R3).

Validate: the parent validates the plan before any dispatch — 3–15 specialists, `max_spawn_depth = 2`, well-formed `output_schema` per specialist. Invalid plans are rejected with a clear error and never dispatched. Run scripts/validate_plan.py (exit 0 = dispatchable) to gate the dispatch. Add --dry-run to scripts/dispatch.py to print the plan + resolved models without dispatching (exit 0). Specialist outputs are validated against their declared output_schema before trace write; mismatches surface in the orchestration log as failed specialists (per F1.6 gap rule a). Set HERMES_ORCHESTRATOR_MODEL / HERMES_SUBAGENT_MODEL or pass --orchestrator-model / --subagent-model on the CLI; cost estimate = specialists × --cost-per-call; --max-cost N aborts with PLAN_OVER_BUDGET if exceeded. Token estimate is computed per-specialist (≈ chars/4 by default; override with --tokens-per-char N) and surfaced in the orchestration log alongside elapsed_ms.

Batch dispatch: the Orchestrator issues `delegate_task` calls, one array of specialists per batch, respecting dependency edges; independent specialists run in parallel. Run scripts/dispatch.py PLAN.json (exit 0 = every specialist ran, trace in logs/orchestration-trace.jsonl).

Recover: failed or timed-out specialists are retried once with an adjusted prompt per the error-handling rule (PRD R8); persistent failures are recorded and the synthesis proceeds with available outputs rather than hanging. Run scripts/dispatch.py PLAN.json (default retries each failed specialist once before recording status: failed).

Synthesize: the Orchestrator merges specialist outputs into a single answer, noting dissent and confidence where specialists disagree. The synthesis contract and output schema are defined in references/synthesis.md.

Log: every run writes the orchestration log (see §6) so the user can read roles, prompts, status, timing, and cost. Full spawn contract and plan JSON schema: see `references/orchestrator.md`.

## 5. Delegation Contract

Each batch is dispatched as a `delegate_task` call carrying an array of specialists. Every specialist entry specifies its `goal` (what to produce), `context` (task-relevant background and inputs), and `output_schema` (the JSON shape its result must conform to). The Orchestrator must respect `max_spawn_depth = 2` — specialists may spawn helpers, but nothing may recurse a fourth level; deeper plans are rejected at validation time. Each run uses 3–15 specialists with a target of ~10, drawn from the archetype catalog plus task-specific roles the Orchestrator synthesizes following the catalog's archetype shape.

## 6. Observability

Every run produces a human-readable log at `<workspace>/orchestration-log-<timestamp>.md` (PRD R9). It records each specialist's role, prompt, status, elapsed time, and token/cost estimate, plus the final synthesized answer. Errors are emitted as actionable messages, not stack traces. The exact log format is defined in `references/orchestration-log.md` (F1.8). Run scripts/write_log.py (default paths) to produce the per-Run orchestration log at orchestration-log-<UTC-timestamp>.md.

## 7. Safety

No privilege escalation: specialists receive reduced toolsets appropriate to their role (e.g. a researcher gets read/search tools, not shell or write access) unless the parent session explicitly grants more. The Orchestrator cannot widen its own permissions or bypass `max_spawn_depth`. All grants flow downward from the parent session, never upward from a subagent request.

## 8. References Index

- `references/archetypes.md` — the static specialist archetype catalog (defined in F1.3).
- `references/output-schema.md` — per-archetype output schemas and the synthesis contract (defined in F1.6).
- `references/orchestration-log.md` — the orchestration log format (defined in F1.8).
- `references/pools.md` — persistent specialist pool contract + `pools/` examples (defined in F3.1).
- `references/custom-archetypes.md` — user-defined archetype discovery (`~/.hermes/agents/*.yaml`; defined in F3.2).
