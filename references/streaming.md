# Streaming Progress (F3.4)

Source of truth for the dispatcher's per-batch progress sink. References:
PRD R18 (file half); flags on `scripts/dispatch.py`; line shape = the
trace-record dict verbatim (the same dicts the final
`logs/orchestration-trace.jsonl` carries, in harvest form).

While a dispatch runs, progress streams out — one JSON line per completed
specialist record, appended per batch to a `--progress PATH` sink a
consumer can `tail -f`.

## 1. Sink contract (D49)

Local append-only JSONL file at an explicit `--progress PATH` (default
`None` = disabled; order-independent with the other flags). Parent dirs
auto-created. Disabled path or `--dry-run` touches nothing — zero
behavior change for existing users.

## 2. Line shape (D52)

Each line is `json.dumps(record, sort_keys=True)` of the trace-record
dict verbatim — no projection, no second schema. Lines stream in harvest
form: appended per batch BEFORE the F2.4 schema check / F2.6 token stamp
passes, so a line may lack `schema_ok` / `token_estimate`. The final
trace file remains the enriched source of truth. Order within a batch =
harvest (completion) order, not plan order — it is a stream. Retried
intermediate attempts get their own lines like any other record.

Appends are per-batch open-append-close INSIDE the batch loop (not one
handle for the run, not a dump at the end): a `tail -f` consumer sees
flushed lines per batch, a crashed run keeps everything through the last
completed batch, and `fail_mode=raise` still streams the batches that
completed before the abort.

## 3. Consumer recipe

`tail -f <sink>`, then `json.loads` per line; filter on
`id` / `batch` / `status`. Line count == completed-record count;
batch-0 lines precede batch-1 lines.

## 4. Layer rule (D50)

Skill scripts NEVER speak Telegram/Slack — the file is the handoff.
Hermes `deliver` targets (cronjob, gateway) consume it downstream.
Reuse, don't reinvent (PRD §1).

## 5. Never-blocks rule (D51)

Sink IO NEVER fails a run: miss/permission/readonly → one
`WARNING: progress sink ...` to stderr, dispatch continues, exit code
unaffected.
