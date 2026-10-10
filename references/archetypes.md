# Static Specialist Archetype Catalog (F1.3)

Source of truth: PRD Appendix A — transcribed, not reinvented.
Tool defaults below match the PRD table verbatim (D6).
Normative plan/shape schema: `references/orchestrator.md` §2.

The Orchestrator chooses **from** this catalog; it does not need to use every
role on every task. The Orchestrator may also synthesize task-specific roles by
following the archetype shape in §3 — it is not limited to this list.

## 1. Core roles (always-consider)

| Archetype | Purpose | Default tools |
| --------- | ------- | ------------- |
| `planner` | Decomposes the user task into sub-questions; produces the dependency graph | file, terminal (read) |
| `researcher` | Pulls fresh or cited information from sources (web, files, repos, docs) | web_search, web_extract, file, terminal (read) |
| `comparator` | Lays out N options side-by-side on user-chosen axes | file, terminal (read) |
| `coder-impl` | Writes code against a precise spec | file, terminal |
| `coder-test` | Writes / runs tests for `coder-impl`'s output | file, terminal |
| `reviewer-spec` | Verifies the implementation matches the original spec | file, terminal |
| `reviewer-quality` | Reviews code quality, security, performance | file, terminal |
| `critic` | Steelmans / attacks the emerging synthesis | file |
| `risk-analyst` | Identifies failure modes, edge cases, mitigations | file, terminal (read) |
| `writer` | Produces the final user-facing artifact (Markdown, doc, PR description) | file |
| `formatter-doc` | Cleans up structure, linting, formatting | file, terminal |
| `pr-drafter` | Opens a PR / commit / file with a draft description | terminal, github |
| `data-extractor` | Pulls structured data from a source (Linear, DB, file) | file, terminal, integrations |

Role notes (PRD distinctions, do not merge):

- `writer` produces the artifact; `summarizer` (optional, §2) collapses long
  outputs to a TL;DR; `formatter-doc` cleans structure/lint. Three distinct jobs.
- `reviewer-spec` checks spec conformance; `reviewer-quality` checks quality,
  security, performance. Both default to `file, terminal`.
- `critic` is read-only by default (`file`); it attacks the synthesis, it does
  not rewrite it.

## 2. Optional / task-specific roles

| Archetype | Purpose |
| --------- | ------- |
| `domain-expert-<field>` | PATTERN, not a fixed role — invoked when a task is clearly tied to a non-generic domain (e.g. `domain-expert-accounting` for a QuickBooks-to-SaaS migration). Substitute `<field>` with the domain. |
| `citation-checker` | Verifies each claim's source. |
| `blocker-detector` | Finds blocked work items / dead-ends. |
| `bench-runner` | Runs benchmarks and reports numbers. |
| `summarizer` | Collapses long outputs to a TL;DR. |

## 3. Archetype shape (the contract)

Every chosen archetype gets, at minimum:

- `id` — unique within the plan.
- `role` — archetype name (one of the above, or synthesized per the statement above).
- `goal` — one sentence stating what to produce, including its success criterion.
- `context` — input bundle for this specialist: task slice, relevant upstream
  outputs, role instructions — never the full conversation.
- `output_schema` — the JSON schema the specialist's result must conform to
  (content defined in F1.6, not here).
- `tools` — least-privilege subset of the parent session's tools granted to this
  specialist (defaults in §§1–2 are the starting point; narrow further per task).
- `model` — specialist model; default `opencode-go/deepseek-v4.1-flash` in Phase 1.

The Orchestrator's plan additionally carries:

- `batches` (`number[][]`) — index arrays into `specialists[]`; each inner array
  is dispatched as one `delegate_task` call.
- `dependencies` (`Record<id, id[]>`) — maps specialist id → prerequisite ids for
  the synthesizer's ordering.

Normative schema: `references/orchestrator.md` §2. Per-archetype output-schema
content is F1.6 scope, not here.
