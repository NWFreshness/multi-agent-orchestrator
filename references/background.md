# Background Run Mode (F3.5)

Source of truth for the dispatcher's detached run mode. References:
PRD §10 (cron/background invocation path); flags on
`scripts/dispatch.py`; the F3.4 progress sink as the polling surface;
the trace as the completion surface.

Long dispatches run detached: `scripts/dispatch.py PLAN.json
--background` re-execs itself detached (new session, stdio to a log
file), prints a one-line handle, and exits 0 immediately. The user
polls with `tail -f` on the F3.4 progress sink (or the trace) and owns
the process with normal shell tools. No status/kill API in the skill
— the shell owns the process.

## 1. Handle-line contract (D53)

Parent prints exactly one line to stdout and exits 0:

`background: pid=<n> log=<log path> trace=<trace path>`

`<n>` is the detached child's pid, `<log path>` is
`logs/background-<UTC-ts>.log`, `<trace path>` is
`logs/orchestration-trace.jsonl`. The child is a normal foreground
dispatch (one code path) — re-exec keeps behavior identical.

## 2. Log path

`logs/background-<UTC-ts>.log` (UTC `YYYYMMDDTHHMMSSZ`, parent dirs
created). The child's stdout+stderr go here (`stderr=STDOUT`). The
parent closes its handle immediately — the child holds its own fd, so
the parent never hangs on exit.

## 3. Polling recipe

Pair `--background` with `--progress PATH` (recommended):

`tail -f <sink>` → `tail -f logs/orchestration-trace.jsonl` →
`scripts/write_log.py` for the human-readable log.

The sink fills per batch as the child runs (pollable mid-run, not
only at end). An invalid/over-budget plan never detaches — it fails
in the foreground with fast, visible errors.

## 4. Ownership rule (D55)

The shell owns the process. `kill <pid>` stops a background run;
there is no skill-side `--status` / `--wait` / `--kill`, no PID
files. The handle line + log path suffice — no new state files.

## 5. Exclusivity (D54)

`--background` is mutually exclusive with `--dry-run` and
`--self-test` (exit 2 + `error:`). Silent flag-ignoring is banned: a
user who asks for background + dry-run has a wrong mental model and
must hear about it.

## 6. POSIX note

Detach uses `start_new_session=True` (POSIX-only). No Windows
`creationflags` branch in F3.5 (repo runs macOS; YAGNI). The child's
cwd equals the parent's (`cwd=os.getcwd()`) so relative `logs/` +
`.cache/` resolve identically.
