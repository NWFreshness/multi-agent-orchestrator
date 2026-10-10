"""Per-Run orchestration log writer for the multi-agent-orchestrator skill (F1.8).

Stdlib-only CLI: reads the dispatcher's trace
(``logs/orchestration-trace.jsonl``, F1.5/F1.7 format) and writes the
human-readable per-Run orchestration log required by PRD R9 — the file the
user can read to see roles, status, timing, attempts, and (in F1.10) the
synthesized answer.

D15 (controller-locked): the log writer is a FORMATTER, not an analyzer.
It reads the trace and produces a markdown file. No re-derivation, no
intelligence — if the trace says ``retries: 1`` that's what the log shows.

D16 (controller-locked): F1.5's trace does NOT carry per-specialist
goal/context. The log writer therefore OMITS those fields entirely — no
TODO line, no placeholder, no synthesis. If F1.10 needs goal in the log,
that's a F1.5 trace-extension request, not F1.8 scope creep.

Usage:
    python3 scripts/write_log.py [--trace PATH] [--out DIR]
        [--task-summary TEXT] [--workspace PATH] [--self-test]

Exit codes: 0 = success, 1 = trace missing/malformed, 2 = usage error.

The log filename is ``<out>/orchestration-log-<UTC-timestamp>.md`` where the
UTC timestamp is ``YYYYMMDDTHHMMSSZ`` (sortable, no colons — safe in any
filename). The same string is stamped in the file's H1 header.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# F1.8 §"Log file shape" — sections 1–5 in this order, locked H-levels.
# Header (H1) → Plan (H2) → Specialists (H2) → Aggregate (H2) → Synthesis (H2).
# The H3 specialist subsections are produced under ## Specialists.
DEFAULT_TRACE = Path("logs") / "orchestration-trace.jsonl"
DEFAULT_OUT = Path(".")
DEFAULT_WORKSPACE = Path(".")
SYNTHESIS_PLACEHOLDER = "[Awaiting F1.6 synthesizer output]"


def _utc_timestamp() -> str:
    """``YYYYMMDDTHHMMSSZ`` — sortable, no colons (filename-safe on every OS)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_trace(path: Path) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Read the JSONL trace. Returns (records, error_msg).

    Each line is one trace record (F1.5/F1.7). Empty / blank lines are
    silently skipped. A non-empty file with zero parseable lines is
    treated as malformed (the dispatcher always emits at least one line
    per specialist, so zero lines means corruption or a wrong file).
    """
    if not path.is_file():
        return ([], "trace not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    except OSError as exc:
        return ([], "cannot read %s: %s" % (path, exc))

    records: List[Dict[str, Any]] = []
    for line_number, raw in enumerate(raw_lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            return ([], "malformed JSON at %s line %d: %s"
                    % (path, line_number, exc))
        if not isinstance(obj, dict):
            return ([], "trace line %d is not a JSON object (got %s)"
                    % (line_number, type(obj).__name__))
        records.append(obj)
    if not records:
        return ([], "trace %s contains no parseable records" % path)
    return (records, None)


def _unique_specialists_in_order(
    records: List[Dict[str, Any]],
) -> List[Tuple[str, str]]:
    """First-encounter order of ``(id, role)`` pairs across the trace.

    The spec locks "one H3 per unique specialist id in first-encounter
    order". We use the (id, role) pair so a future trace extension that
    records the role differently across attempts still groups correctly
    (today every attempt for one id carries the same role).
    """
    seen: Dict[str, str] = {}
    for rec in records:
        sid = rec.get("id")
        role = rec.get("role", "")
        if not isinstance(sid, str) or not sid:
            continue
        if sid not in seen:
            seen[sid] = role if isinstance(role, str) else ""
    return [(sid, seen[sid]) for sid in seen]


def _final_record_per_id(
    records: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Last record per specialist id (chronological)."""
    final: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        sid = rec.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        prev = final.get(sid)
        if prev is None:
            final[sid] = rec
        else:
            # Chronological order is the F1.5 contract (one file write at
            # the end, in dispatch order). Use ``started_ts`` when present
            # (F1.7 added it), else ``ts``, else fall back to "later in
            # file" by overwriting — the F1.5/F1.7 dispatchers always
            # emit in dispatch order so later == later.
            prev_key = prev.get("started_ts") or prev.get("ts") or ""
            new_key = rec.get("started_ts") or rec.get("ts") or ""
            if new_key >= prev_key:
                final[sid] = rec
    return final


def _format_attempt_row(attempt_index: int, rec: Dict[str, Any]) -> str:
    """One bullet row per attempt, per the locked template in F1.8.

    Format mirrors the spec example:
        - Attempt 1: ok, elapsed=142ms, output=logs/specialists/r1.1.json, retries=0
    ``error`` is appended only when present; ``retries`` is the count of
    ADDITIONAL attempts AFTER the first one (F1.7 contract).

    F2.6: when the trace record carries a ``token_estimate`` > 0, append
    ``, tokens=<N>`` to the bullet. When ``token_estimate`` is 0 or
    absent, the ``tokens=`` part is OMITTED — the writer never prints
    ``tokens=0`` noise. Single-line format (markdown renderer handles
    wrapping).
    """
    status = rec.get("status", "unknown")
    elapsed = rec.get("elapsed_ms", 0)
    output_path = rec.get("output_path") or "(none)"
    retries = rec.get("retries", attempt_index)
    line = ("- Attempt %d: %s, elapsed=%dms, output=%s, retries=%d"
            % (attempt_index, status, elapsed, output_path, retries))
    if "error" in rec and rec["error"]:
        line += ", error=%s" % rec["error"]
    # F2.6: token_estimate is observation; render only when > 0.
    raw_tokens = rec.get("token_estimate")
    if isinstance(raw_tokens, int) and not isinstance(raw_tokens, bool):
        if raw_tokens > 0:
            line += ", tokens=%d" % raw_tokens
    return line


def _format_specialist(
    sid: str,
    role: str,
    attempts: List[Tuple[int, Dict[str, Any]]],
) -> str:
    """One ``### <id> (<role>)`` section with one bullet row per attempt."""
    lines: List[str] = ["### %s (%s)" % (sid, role), ""]
    # attempts is already chronologically ordered (caller passes sorted list)
    for attempt_index, rec in attempts:
        lines.append(_format_attempt_row(attempt_index, rec))
    lines.append("")  # trailing blank between H3 sections
    return "\n".join(lines)


def _format_plan_summary(records: List[Dict[str, Any]]) -> str:
    """``## Plan`` — N specialists, B batches (extracted from the trace).

    N = count of unique specialist ids. B = the highest batch index in
    the trace + 1 (F1.5 writes one record per (batch, id); under F1.7 a
    specialist that retried has multiple records but the batch index
    is the same across all of them, so ``max(batch)+1`` is correct).
    """
    unique_ids = {rec.get("id") for rec in records
                  if isinstance(rec.get("id"), str)}
    n_specialists = len(unique_ids)
    batch_indices: List[int] = [int(rec["batch"]) for rec in records
                                 if isinstance(rec.get("batch"), int)]
    n_batches = (max(batch_indices) + 1) if batch_indices else 0
    lines = ["## Plan", "",
             "- %d specialists" % n_specialists,
             "- %d batches" % n_batches,
             ""]
    return "\n".join(lines)


def _format_aggregate(records: List[Dict[str, Any]]) -> str:
    """``## Aggregate`` — total elapsed, succeeded, failed, retried, tokens.

    F1.8 spec: "total elapsed (sum of elapsed_ms across final-per-spec
    records)" — sum the elapsed_ms of each specialist's FINAL record
    (chronologically last). Succeeded / failed count unique specialists
    by final status. Retried counts unique specialists whose final
    ``retries`` field is > 0.

    F2.6: adds ``- Total estimated tokens: N`` line, where N is the sum
    of ``token_estimate`` across the same FINAL records. ``tokens`` is
    ALWAYS rendered (even when N == 0) so users see the aggregate
    presence regardless of whether any spec carried estimates. The
    per-attempt row, by contrast, omits ``tokens=0`` to keep the
    output clean (locked F2.6 contract).
    """
    final = _final_record_per_id(records)
    total_elapsed = sum(int(rec.get("elapsed_ms", 0))
                        for rec in final.values())
    succeeded = sum(1 for rec in final.values()
                    if rec.get("status") == "ok")
    failed = sum(1 for rec in final.values()
                 if rec.get("status") != "ok")
    retried = sum(1 for rec in final.values()
                  if int(rec.get("retries", 0)) > 0)
    total_tokens = sum(int(rec.get("token_estimate", 0) or 0)
                       for rec in final.values())
    lines = ["## Aggregate", "",
             "- Total elapsed: %dms" % total_elapsed,
             "- Succeeded: %d" % succeeded,
             "- Failed: %d" % failed,
             "- Retried: %d" % retried,
             "- Total estimated tokens: %d" % total_tokens,
             ""]
    return "\n".join(lines)


def _format_synthesis() -> str:
    """``## Synthesis`` — literal placeholder. F1.10 will inject real content."""
    return "## Synthesis\n\n%s\n" % SYNTHESIS_PLACEHOLDER


def _format_header(
    timestamp: str,
    task_summary: Optional[str],
    workspace: Path,
) -> str:
    """``# Orchestration Log`` + run timestamp + optional task summary + workspace."""
    lines: List[str] = ["# Orchestration Log", ""]
    lines.append("- Run timestamp (UTC): %s" % timestamp)
    if task_summary:
        lines.append("- Task summary: %s" % task_summary)
    lines.append("- Workspace: %s" % str(workspace.resolve()))
    lines.append("")
    return "\n".join(lines)


def build_log_markdown(
    records: List[Dict[str, Any]],
    timestamp: str,
    task_summary: Optional[str],
    workspace: Path,
) -> str:
    """Compose the full log markdown from the parsed trace records.

    Sections appear in the locked order: Header → Plan → Specialists →
    Aggregate → Synthesis. Each section is separated by a single blank
    line (the trailing blank lines inside each section already include
    the separator between them).
    """
    parts: List[str] = []
    parts.append(_format_header(timestamp, task_summary, workspace))
    parts.append(_format_plan_summary(records))

    # ## Specialists — one H3 per unique id in first-encounter order,
    # one bullet row per attempt chronologically.
    unique_in_order = _unique_specialists_in_order(records)
    by_id: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for rec in records:
        sid = rec.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        if sid not in by_id:
            by_id[sid] = []
            order.append(sid)
        by_id[sid].append(rec)
    # Sort each id's attempts chronologically. The trace is in dispatch
    # order so a simple stable sort by (started_ts, ts) is enough.
    def _attempt_key(rec: Dict[str, Any]) -> str:
        return (rec.get("started_ts") or rec.get("ts") or "")
    for sid in order:
        by_id[sid].sort(key=_attempt_key)

    parts.append("## Specialists\n")
    role_by_id = dict(unique_in_order)
    for sid in unique_in_order:
        sid_id = sid[0]
        role = sid[1] or role_by_id.get(sid_id, "")
        attempts = list(enumerate(by_id.get(sid_id, []), start=1))
        parts.append(_format_specialist(sid_id, role, attempts))

    parts.append(_format_aggregate(records))
    parts.append(_format_synthesis())
    return "\n".join(parts)


def _write_log(
    markdown: str,
    out_dir: Path,
    timestamp: str,
) -> Tuple[Path, Optional[str]]:
    """Write ``<out>/orchestration-log-<timestamp>.md`` (atomic-ish)."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (out_dir, "cannot create %s: %s" % (out_dir, exc))
    filename = "orchestration-log-%s.md" % timestamp
    out_path = out_dir / filename
    try:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(markdown)
    except OSError as exc:
        return (out_path, "cannot write %s: %s" % (out_path, exc))
    return (out_path, None)


def _self_test_fixture_trace(tmp_dir: Path) -> Path:
    """Write a deterministic fixture trace under ``tmp_dir``.

    Two specialists: ``r1`` succeeds on the first try (1 record, 0 retries,
    F2.6 ``token_estimate=42``), ``a2`` fails once then succeeds
    (2 records, retries: 0 then 1; the FINAL record carries
    ``token_estimate=180`` so the F2.6 rendering assertion can prove
    the last-attempt estimate surfaces in the attempt row and the
    aggregate aggregates the FINAL per-id values — the failed
    intermediate attempt has no ``token_estimate`` so the bullet
    must OMIT the ``tokens=`` part). The fixture exercises both the
    "unique-in-order" rule and the multi-attempt chronological
    rendering, plus the F2.6 per-attempt conditional rendering and
    the ``Total estimated tokens: N`` aggregate line (42 + 180 = 222).
    """
    trace_path = tmp_dir / "orchestration-trace.jsonl"
    lines = [
        # r1: success first try (F2.6 token_estimate=42 on the final).
        {"ts": "2026-10-09T20:15:00Z", "started_ts": "2026-10-09T20:14:59Z",
         "batch": 0, "id": "r1", "role": "researcher",
         "status": "ok", "output_path": "logs/specialists/r1.1.json",
         "elapsed_ms": 142, "returncode": 0, "retries": 0,
         "token_estimate": 42},
        # a2: first attempt fails (no token_estimate → bullet must omit).
        {"ts": "2026-10-09T20:15:01Z", "started_ts": "2026-10-09T20:15:00Z",
         "batch": 0, "id": "a2", "role": "critic",
         "status": "failed", "output_path": None,
         "elapsed_ms": 50, "returncode": 1, "retries": 0,
         "error": "transient timeout"},
        # a2: second (final) attempt succeeds (token_estimate=180).
        {"ts": "2026-10-09T20:15:03Z", "started_ts": "2026-10-09T20:15:02Z",
         "batch": 0, "id": "a2", "role": "critic",
         "status": "ok", "output_path": "logs/specialists/a2.2.json",
         "elapsed_ms": 200, "returncode": 0, "retries": 1,
         "token_estimate": 180},
    ]
    with open(trace_path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, sort_keys=True))
            handle.write("\n")
    return trace_path


def _self_test() -> bool:
    """--self-test: fixture → log → assert locked sections + shape.

    Writes a deterministic trace to a temp dir under /tmp, runs the log
    writer, then asserts:
      - output file exists with the spec'd filename pattern
      - 5 top-level sections present in the locked order (H1 → H2×4)
      - both specialist H3 sections present
      - synthesis placeholder is present
      - elapsed_ms is summed correctly across the FINAL records
        (r1=142, a2=200 → 342ms total)
    """
    failures = 0
    tmp_root = Path("/tmp") / ("mao_write_log_selftest_%d" % os.getpid())
    # stdlib-only ``rm -rf`` equivalent (no shutil top-import, no
    # recursive-ok unlink). Wipe any prior run.
    if tmp_root.exists():
        for entry in tmp_root.rglob("*"):
            if entry.is_file():
                entry.unlink()
        for entry in sorted(tmp_root.glob("**/*"), reverse=True):
            if entry.is_dir():
                entry.rmdir()
        tmp_root.rmdir()
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = tmp_root
    try:
        _run_self_test_assertions(tmp_dir, failures_holder := [0])
        failures = failures_holder[0]
    finally:
        # Wipe the temp dir on exit (best-effort; on failure leave /tmp
        # alone rather than raise from a teardown path).
        try:
            if tmp_root.exists():
                for entry in tmp_root.rglob("*"):
                    if entry.is_file():
                        entry.unlink()
                for entry in sorted(tmp_root.glob("**/*"), reverse=True):
                    if entry.is_dir():
                        entry.rmdir()
                tmp_root.rmdir()
        except OSError:
            pass
    if failures == 0:
        print("SELF_TEST: 8/8 passed")
    return failures == 0


def _run_self_test_assertions(tmp_dir: Path, failures_holder: List[int]) -> None:
    """Body of the self-test (factored out so the temp-dir cleanup lives
    in a single ``finally``). ``failures_holder`` is a one-element list
    used to return the failure count without ``nonlocal`` (the locked
    stdlib set keeps the file simple; walrus + list is enough).
    """
    failures = failures_holder
    trace_path = _self_test_fixture_trace(tmp_dir)
    out_dir = tmp_dir / "out"
    timestamp = _utc_timestamp()
    records, err = _load_trace(trace_path)
    if err is not None:
        failures[0] += 1
        print("SELF_TEST FAIL: fixture load: %s" % err)
        return
    markdown = build_log_markdown(
        records, timestamp=timestamp,
        task_summary="self-test fixture",
        workspace=tmp_dir)
    out_path, werr = _write_log(markdown, out_dir, timestamp)
    if werr is not None:
        failures[0] += 1
        print("SELF_TEST FAIL: write: %s" % werr)
        return

    # Filename pattern: orchestration-log-YYYYMMDDTHHMMSSZ.md.
    # Built without ``re`` (the locked stdlib set does not include it).
    # Stamp shape: 8 digits + 'T' + 6 digits + 'Z' = 16 chars.
    name = out_path.name
    prefix = "orchestration-log-"
    suffix = ".md"
    if (not name.startswith(prefix)
            or not name.endswith(suffix)
            or len(name) != len(prefix) + 16 + len(suffix)):
        failures[0] += 1
        print("SELF_TEST FAIL: filename pattern (got %s)" % name)
    else:
        stamp = name[len(prefix):-len(suffix)]
        valid_stamp = (len(stamp) == 16 and stamp[8] == "T"
                       and stamp[15] == "Z"
                       and stamp[:8].isdigit() and stamp[9:15].isdigit())
        if not valid_stamp:
            failures[0] += 1
            print("SELF_TEST FAIL: timestamp shape (got %s)" % stamp)
        else:
            print("SELF_TEST PASS: filename pattern %s" % name)

    text = out_path.read_text(encoding="utf-8")

    # Section presence + order. Locked H1 → H2×4 sequence.
    sections = [
        ("# Orchestration Log", "H1 header"),
        ("## Plan", "Plan section"),
        ("## Specialists", "Specialists section"),
        ("## Aggregate", "Aggregate section"),
        ("## Synthesis", "Synthesis section"),
    ]
    last_idx = -1
    for marker, label in sections:
        idx = text.find(marker)
        if idx < 0:
            failures[0] += 1
            print("SELF_TEST FAIL: missing section %r (%s)"
                  % (marker, label))
            continue
        if idx < last_idx:
            failures[0] += 1
            print("SELF_TEST FAIL: %s appears before the prior section"
                  % label)
        last_idx = idx
    if all(text.find(m) >= 0 for m, _ in sections):
        print("SELF_TEST PASS: 5 sections present in locked order")

    # Specialist H3 sections
    for sid in ("r1", "a2"):
        marker = "### %s" % sid
        if marker not in text:
            failures[0] += 1
            print("SELF_TEST FAIL: missing specialist section %s"
                  % marker)
    if "### r1 (researcher)" in text and "### a2 (critic)" in text:
        print("SELF_TEST PASS: both specialist H3 sections present")

    # Synthesis placeholder
    if SYNTHESIS_PLACEHOLDER not in text:
        failures[0] += 1
        print("SELF_TEST FAIL: synthesis placeholder missing")
    else:
        print("SELF_TEST PASS: synthesis placeholder present")

    # Aggregate correctness: total elapsed = 142 (r1 final) + 200 (a2 final)
    if "- Total elapsed: 342ms" not in text:
        failures[0] += 1
        print("SELF_TEST FAIL: total elapsed (expected 342ms)")
    else:
        print("SELF_TEST PASS: total elapsed sums final records only")

    # a2 should show TWO attempt rows (retry history); r1 shows one.
    a2_attempts = text.count("- Attempt")
    if a2_attempts != 3:
        failures[0] += 1
        print("SELF_TEST FAIL: attempt-row count (expected 3, got %d)"
              % a2_attempts)
    else:
        print("SELF_TEST PASS: retry history shown (3 attempt rows)")

    # ---- F2.6 token-estimate rendering (additive) ----
    # Per-attempt row checks: r1's row carries tokens=42; a2's first
    # (failed) row has NO tokens= part (no field); a2's second (final)
    # row carries tokens=180. Aggregate line sums the FINAL records:
    # r1 (42) + a2 final (180) = 222.
    has_r1_tokens = ("tokens=42" in text)
    has_a2_final_tokens = ("tokens=180" in text)
    has_zero_noise = ("tokens=0" not in text)
    has_aggregate_tokens = ("- Total estimated tokens: 222" in text)
    # The failed a2 attempt (no token_estimate) must render WITHOUT a
    # tokens= part. We assert that the row carrying the ``transient
    # timeout`` error string contains no tokens= substring.
    failed_line_idx = text.find("error=transient timeout")
    failed_line_no_tokens = False
    if failed_line_idx >= 0:
        # Find the end of that bullet (next newline) and check the
        # substring is tokens-free.
        line_end = text.find("\n", failed_line_idx)
        if line_end < 0:
            line_end = len(text)
        failed_line = text[failed_line_idx:line_end]
        failed_line_no_tokens = ("tokens=" not in failed_line)
    if (not has_r1_tokens or not has_a2_final_tokens
            or not has_zero_noise or not has_aggregate_tokens
            or not failed_line_no_tokens):
        failures[0] += 1
        print("SELF_TEST FAIL: F2.6 token rendering "
              "(r1_has=%s, a2_final_has=%s, no_zero=%s, agg='%s', "
              "failed_no_tokens=%s)"
              % (has_r1_tokens, has_a2_final_tokens, has_zero_noise,
                 has_aggregate_tokens, failed_line_no_tokens))
    else:
        print("SELF_TEST PASS: F2.6 token rendering "
              "(tokens=42 on r1 row, tokens=180 on a2 final row, "
              "no tokens= on a2 failed row, "
              "Total estimated tokens: 222 aggregate)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Format logs/orchestration-trace.jsonl into the per-Run "
                    "orchestration log (PRD R9, F1.8). The writer is a "
                    "formatter only — it reads the trace as-is and does "
                    "NOT re-derive, summarize, or synthesize.")
    parser.add_argument("--trace", type=Path, default=DEFAULT_TRACE,
                        help="Path to the dispatcher's trace JSONL "
                             "(default: logs/orchestration-trace.jsonl)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="Directory to write the log file into "
                             "(default: current directory)")
    parser.add_argument("--task-summary", type=str, default=None,
                        help="Optional task summary to record in the H1 header")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                        help="Workspace path to record in the H1 header "
                             "(default: current directory)")
    parser.add_argument("--self-test", action="store_true",
                        help="Run the built-in self-test (writes a fixture "
                             "log to a temp dir and asserts the 5 sections + "
                             "2 specialist sections + placeholder).")
    args = parser.parse_args(argv)

    if args.self_test:
        return 0 if _self_test() else 1

    if not args.trace or not args.out or not args.workspace:
        parser.print_usage(sys.stderr)
        return 2

    timestamp = _utc_timestamp()
    records, err = _load_trace(args.trace)
    if err is not None:
        print("error: %s" % err, file=sys.stderr)
        return 1

    markdown = build_log_markdown(
        records, timestamp=timestamp,
        task_summary=args.task_summary, workspace=args.workspace)
    out_path, werr = _write_log(markdown, args.out, timestamp)
    if werr is not None:
        print("error: %s" % werr, file=sys.stderr)
        return 1
    print("wrote %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
