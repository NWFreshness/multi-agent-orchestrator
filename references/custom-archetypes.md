# Custom Archetypes via `~/.hermes/agents/*.yaml` (F3.2)

Source of truth for user-defined specialist archetypes. References: PRD
R17; static catalog `references/archetypes.md` §1; discovery script
`scripts/discover_archetypes.py`.

A custom archetype is a recurring specialist the user defines once as a
YAML file and reuses across runs. The Orchestrator offers discovered
custom archetypes alongside the F1.3 static catalog when composing a
plan's `specialists[]`. Discovery is read-only: the skill never creates,
edits, or deletes user files.

No validator changes were needed — the F1.4 E4 warning path already
permits unknown roles, so custom roles dispatch today.

## 1. Location (D41)

Custom archetypes live OUTSIDE the skill repo at
`~/.hermes/agents/*.yaml`. User files travel with the user, not with
git — the opposite of F3.1 pools, which are repo-local.

The discovery directory resolves `--agents-dir` > `HERMES_AGENTS_DIR`
env > `~/.hermes/agents` default (an empty-string env value counts as
unset). A missing directory is the common first-run state and means
zero custom archetypes — not an error. The skill NEVER creates the
directory uninvited.

## 2. File shape (D42)

One archetype per file, flat-`key: value`-subset YAML only (F2.5
precedent — stdlib-only, no PyYAML):

```yaml
name: arxiv-fetcher
purpose: Pulls papers and metadata from arXiv.
tools: web_search, web_extract, file
model: opencode-go/deepseek-v4.1-flash
```

- `name` (required): archetype name, lowercase alphanumeric + dashes.
- `purpose` (required): one line stating what the specialist does.
- `tools` (optional): comma-separated tool names on ONE line.
  Default is `file` (least privilege — grant more only deliberately).
- `model` (optional): model string. Default is empty, meaning the
  Phase 2 resolved subagent model applies.

`#` comments and blank lines are allowed. Anything else — nesting,
lists, multi-document markers — is silently ignored per the flat-subset
rule. Unknown top-level keys are ignored (forward-compatible).

## 3. Naming

The filename stem SHOULD match the `name` field:
`arxiv-fetcher` ↔ `~/.hermes/agents/arxiv-fetcher.yaml`. On mismatch
the discovery script emits a `WARNING:` but the `name` field wins —
the file is still listed under its declared name.

## 4. Malformed files (D43)

A file missing `name` or `purpose` is SKIPPED with a `WARNING:` line to
stderr — never fatal. One bad file must not blind the Orchestrator to
the good ones; discovery always exits 0 on the success path, even with
zero valid archetypes (prints `no custom archetypes`).

## 5. Name clashes with the static catalog

If a custom archetype shares a name with an F1.3 static-catalog role,
the CUSTOM definition wins at orchestration time. This is an
orchestrator-side rule, stated not enforced: the static catalog is
markdown, not machine-readable, so no script can arbitrate it.

## 6. Discovery output

`scripts/discover_archetypes.py` prints one line per valid archetype,
sorted by name:

```
name | purpose | tools | model | source-file
```

followed by an `N custom archetype(s)` summary on stdout. Warnings go
to stderr. Pass `--json` for a JSON array instead of the pipe-delimited
lines. The Orchestrator reads this output directly; there is no
plan-preview shape change (pool ↔ custom-archetype linking is out of
scope — pools reference role names only).
