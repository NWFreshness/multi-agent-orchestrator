# Specialist Output Cache (F3.3)

Source of truth for the dispatcher's content-hash cache. References: PRD
R12 (24h reuse); implementation `scripts/specialist_cache.py`; flags on
`scripts/dispatch.py`; the trace carries the stamp (log rendering of
`cached` is deferred).

Re-running a plan reuses prior specialist outputs when the cache key
matches — identical `role + goal + context + model + output_schema`
within the TTL skips the stub run entirely.

## 1. Key recipe (D45)

`cache_key(role, goal, context, model, output_schema)` = sha256 hex of
`json.dumps(canonical, sort_keys=True)` where canonical is exactly the
5-field dict above (`default=str` for non-serializable schema values).
Any input change — including the schema — busts the cache.

## 2. Layout (D46)

`.cache/orchestrator/<namespace>/<key>.json` relative to the repo root
at runtime. The directory is gitignored (see `.gitignore`).

## 3. Flags

- `--cache-namespace NAME` (default `''` = cache fully disabled).
- `--cache-ttl-hours H` (type=float, default 24.0).

Invalid namespace → `error:` + exit 2, no dispatch. Namespace rule:
`^[A-Za-z0-9][A-Za-z0-9_-]*$`, max 64 chars (`check_namespace()`
raises `ValueError`).

## 4. TTL rule (D47)

Age comes from the entry file's mtime; expired iff age > ttl*3600.
`ttl_hours <= 0` = entries never expire. Lookup misses (never raises)
on absent / expired / corrupt / unreadable entries; store returns False
(never raises) on IO failure. The cache never breaks a run; the
dispatcher ignores a failed store.

## 5. Hit behavior (D48)

Hits skip the subprocess, write the cached bytes to the normal seq
output path (`logs/specialists/<id>.<n>.json`), and stamp the final
trace record `cached: true`. Hits still pass through the F2.4 schema
check and the F2.6 token stamp unchanged.

Bypasses: empty namespace disables everything (the dispatcher does not
even import the cache module); force-fail (`simulate` /
`retry_test_overrides`) skips both lookup and store; failed runs are
never stored; `--dry-run` never touches the cache.
