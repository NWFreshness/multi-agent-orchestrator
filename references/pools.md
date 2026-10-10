# Persistent Specialist Pools (F3.1)

Source of truth for named, reusable specialist sets. References: PRD R16,
PRD §9 Phase 3 list; plan schema `references/orchestrator.md` §2;
archetype names `references/archetypes.md` §1.

A pool is a recurring team (e.g. researcher + critic + writer) stored as
JSON in the skill repo so it survives across sessions. The Orchestrator
copies/adapts pool members into a plan's `specialists[]` instead of
rebuilding the team every run.

## 1. Location (D38)

Pools live in the skill repo at `pools/<name>.json`. Repo-local files
travel with the skill via git — zero new config plumbing. Users add their
own pool files in the same directory. There is no `~/.hermes` pool
discovery in F3.1 (that pattern belongs to F3.2 custom archetypes).

## 2. Naming

`<name>` is lowercase alphanumeric + dashes and matches the filename stem:
`my-pool` ↔ `pools/my-pool.json`. The `name` field inside the file must
equal the stem.

## 3. File shape

```json
{"name": "research-trio", "description": "...", "members": [...]}
```

- `"name"`: pool name, equals the filename stem (§2).
- `"description"`: one line stating what recurring task the pool serves.
- `"members"`: array of specialist entries (§4).

## 4. Member shape (D39)

Each member is EXACTLY the F1.2 specialist 7-field entry, verbatim —
`id, role, goal, context, output_schema, tools, model`. No new schema to
learn; any pool member can be pasted into a plan's `specialists[]` as-is.
Field semantics are defined in `references/orchestrator.md` §2.

F3.1 members are CONCRETE (D40): every field is a finished string / object
/ list. No `{placeholders}`, no templates, no variable substitution —
templates are a later feature if ever.

- Member `role` values must exist in `references/archetypes.md`.
- Member `id` values need uniqueness WITHIN the pool file only; the
  Orchestrator re-points ids when composing a plan.
- `output_schema` is a real JSON Schema object (`{"type": "object"}`
  minimum), not a string — follow the F2.4 form enforced by the validator.

## 5. Advisory use (D40)

Pools are ADVISORY in F3.1. The Orchestrator copies members and adapts
`goal`/`context` per task, re-pointing `id`s and composing `batches` and
`dependencies` fresh for the plan at hand (the pool file carries neither).
Nothing resolves pools automatically.

The plan JSON gains NO new key in F3.1 — there is no `pool` key. The
validator and dispatcher are untouched; `scripts/` changes are out of
scope for pools.

## 6. Example

`pools/research-trio.json`: researcher + critic + writer for a recurring
topic-brief task. Three members sit exactly at the F1.4 3-specialist floor
and satisfy the reviewer/critic rule.
