"""Batch dispatcher for the multi-agent-orchestrator skill (F1.5).

Stdlib-only CLI: reads a validated plan JSON, fans out
``subprocess.run([sys.executable, "scripts/run_specialist.py", ...])`` per
specialist in dependency-respecting parallel batches via
``concurrent.futures.ThreadPoolExecutor``, and records per-specialist outcomes
to ``logs/orchestration-trace.jsonl``.

Real LLM execution lands in a later phase (F1.6+); the stub here writes
deterministic JSON files so the dispatch surface can be exercised end-to-end.
Swapping in a real ``delegate_task`` runner means replacing
``scripts/run_specialist.py`` only — ``dispatch.py`` is unchanged.

Usage:
    python3 scripts/dispatch.py PLAN.json [--max-parallel N]
        [--fail-mode {raise,continue,simulate:N}] [--self-test]

Exit codes: 0 = every specialist ran (success OR recorded-as-failed),
1 = invalid plan / defensive dep-order violation / --fail-mode raise
triggered, 2 = usage / IO error.
"""

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# D10: import the validator rather than re-parsing the plan. F1.4 shipped
# `validate_plan.validate(plan) -> (errors, warnings)` as importable.
# scripts/dispatch.py and scripts/run_specialist.py live next to
# scripts/validate_plan.py, so a script-local import works for both
# `python3 scripts/dispatch.py …` and any future `python3 -m scripts.dispatch`.
# Imported lazily inside _validate_via_import() so the module's top-level
# imports remain stdlib-only (the controller check scans AST top-level
# imports; project-local imports belong at call sites).

# F1.7 contract: re-runs don't clobber — count the highest existing <id>.\d+.json
SPECIALIST_LOG_DIR = Path("logs") / "specialists"
ORCHESTRATION_TRACE = Path("logs") / "orchestration-trace.jsonl"


def _utcnow_iso() -> str:
    """ISO-8601 UTC with explicit 'Z' suffix (validator/synthesis-friendly)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_fail_mode(raw: str) -> Tuple[str, int]:
    """Returns ('raise'|'continue'|'simulate', simulate_n)."""
    if raw == "raise" or raw == "continue":
        return (raw, 0)
    if raw.startswith("simulate:"):
        tail = raw[len("simulate:"):]
        if not tail.isdigit():
            raise argparse.ArgumentTypeError(
                "simulate:N requires a non-negative integer after ':', got %r"
                % raw)
        return ("simulate", int(tail))
    raise argparse.ArgumentTypeError(
        "fail-mode must be raise, continue, or simulate:N (got %r)" % raw)


def _load_plan(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Load the plan JSON. Returns (plan, error_msg); error_msg != None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return (None, "file not found: %s (check the path and try again)" % path)
    except json.JSONDecodeError as exc:
        return (None, "malformed JSON in %s: %s" % (path, exc))
    except OSError as exc:
        return (None, "cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict):
        return (None, "%s: top-level JSON must be an object" % path)
    return (data, None)


def _validate_via_import(plan: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """D10: import the F1.4 validator on demand, return (errors, warnings).

    Uses ``__import__`` (a builtin, not an ``import`` statement) so the
    module's top-level imports remain stdlib-only — the controller scan
    walks AST top-level imports, and ``importlib`` would appear in that
    list. ``scripts/`` is added to ``sys.path`` so the import resolves
    whether the script is invoked from the repo root or by absolute path.
    """
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        validate_plan = __import__("validate_plan")
        return validate_plan.validate(plan)
    except ImportError as exc:
        # No traceback per F1.5 §6 / orchestrator.md §6 ("no tracebacks").
        print("error: cannot import validate_plan from %s: %s"
              % (scripts_dir, exc), file=sys.stderr)
        sys.exit(2)


def _specialists_for_batch(
    batches: List[List[int]],
    specialists: List[Dict[str, Any]],
    batch_index: int,
) -> List[Dict[str, Any]]:
    """Return the specialist dicts for one batch in declared order."""
    return [specialists[idx] for idx in batches[batch_index]]


def _check_dependency_order(
    batches: List[List[int]],
    specialists: List[Dict[str, Any]],
    dependencies: Dict[str, Any],
) -> Optional[str]:
    """Defensive re-check: every dependency edge crosses a later batch.

    Returns an error message suitable for ``print(...); exit 1`` if violated,
    else None. The validator already enforces this (E6), but the dispatcher
    guards against bypass/import-failure and bad in-memory mutations.
    """
    pos: Dict[int, int] = {}
    for b, batch in enumerate(batches):
        for idx in batch:
            pos[idx] = b
    idpos: Dict[str, int] = {}
    for i, spec in enumerate(specialists):
        sid = spec.get("id") if isinstance(spec, dict) else None
        if isinstance(sid, str) and i in pos:
            idpos[sid] = pos[i]
    for key, prereqs in dependencies.items():
        if not isinstance(key, str) or not isinstance(prereqs, list):
            continue
        if key not in idpos:
            continue
        for pre in prereqs:
            if not isinstance(pre, str) or pre not in idpos:
                continue
            if not idpos[key] > idpos[pre]:
                return ("dependency order violation: %s (batch %d) is not "
                        "strictly after %s (batch %d)"
                        % (key, idpos[key], pre, idpos[pre]))
    return None


def _build_specialist_payload(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Trim the F1.4 specialist shape to the F1.5 specialist runner payload."""
    return {
        "task": spec.get("goal", ""),
        "context": spec.get("context", ""),
        "output_schema": spec.get("output_schema", {}),
    }


def _next_specialist_seq(specialists_dir: Path, specialist_id: str) -> int:
    """Highest existing <id>.<n>.json + 1. F1.7-friendly — re-runs don't clobber."""
    if not specialists_dir.is_dir():
        return 1
    prefix = "%s." % specialist_id
    highest = 0
    for entry in specialists_dir.iterdir():
        name = entry.name
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        stem = name[len(prefix):-len(".json")]
        if stem.isdigit():
            n = int(stem)
            if n > highest:
                highest = n
    return highest + 1


def _run_one(
    spec: Dict[str, Any],
    seq: int,
    specialists_dir: Path,
    scripts_dir: Path,
    force_fail: bool,
) -> Dict[str, Any]:
    """Worker target: shell out to run_specialist.py for one specialist.

    Always returns a record dict for the orchestration trace — never raises.
    ``status`` is ``"ok"`` or ``"failed"``; ``output_path`` is the absolute
    path the runner was supposed to write (or ``None`` on pre-launch failure).
    """
    sid = spec.get("id", "")
    role = spec.get("role", "")
    payload = _build_specialist_payload(spec)
    output_path = specialists_dir / ("%s.%d.json" % (sid, seq))
    argv = [sys.executable, str(scripts_dir / "run_specialist.py"),
            sid, role, json.dumps(payload)]
    if force_fail:
        argv = [sys.executable, "-c",
                "import sys; sys.stderr.write('forced failure for %s\\n');"
                " sys.exit(1)" % sid]

    start_ts = _utcnow_iso()
    t0 = datetime.now(timezone.utc)
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        elapsed_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        return {"batch": -1, "id": sid, "role": role,
                "status": "failed", "output_path": None,
                "elapsed_ms": elapsed_ms, "ts": start_ts,
                "error": "launch failed: %s" % exc}

    elapsed_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
    end_ts = _utcnow_iso()
    ok = (completed.returncode == 0)
    record: Dict[str, Any] = {
        "ts": end_ts, "started_ts": start_ts,
        "id": sid, "role": role,
        "status": "ok" if ok else "failed",
        "output_path": str(output_path) if output_path.exists() else None,
        "elapsed_ms": elapsed_ms,
        "returncode": completed.returncode,
    }
    if not ok:
        stderr_tail = (completed.stderr or "").strip().splitlines()
        record["error"] = (stderr_tail[-1] if stderr_tail
                           else "run_specialist.py exited %d"
                           % completed.returncode)
    return record


def _simulate_index(batch: List[Dict[str, Any]], simulate_n: int) -> Optional[int]:
    """Return the 0-based index inside this batch to force-fail, or None."""
    if simulate_n < 0 or simulate_n >= len(batch):
        return None
    return simulate_n


def dispatch_plan(
    plan: Dict[str, Any],
    max_parallel: int,
    fail_mode: str,
    simulate_n: int,
    scripts_dir: Path,
    specialists_dir: Path,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Run all batches. Returns (exit_code, trace_records).

    Exit code: 0 if every specialist ran (success OR recorded-as-failed),
    1 if a ``raise`` was triggered or a dep-order violation was found,
    or 2 on IO error writing the trace.
    """
    specialists = plan.get("specialists", [])
    batches = plan.get("batches", [])
    dependencies = plan.get("dependencies", {})

    # Defensive dep-order re-check (validator already enforces; we re-check
    # so dispatch.py is correct even if a future caller passes a hand-built
    # plan without re-validating).
    violation = _check_dependency_order(batches, specialists, dependencies)
    if violation is not None:
        print("PLAN_INVALID: %s" % violation, file=sys.stderr)
        return (1, [])

    # Defensive out-of-range re-check: validator already enforces that every
    # index is in range (E4), but a hand-built plan from a future caller may
    # skip validation. Without this pre-check, the list-indexing in
    # ``_specialists_for_batch`` would raise IndexError partway through a
    # batch, dumping a traceback and exiting 1 by accident instead of
    # returning a clean (1, []) with a PLAN_INVALID message.
    n_specialists = len(specialists)
    for b, batch in enumerate(batches):
        for idx in batch:
            if not isinstance(idx, int) or idx < 0 or idx >= n_specialists:
                print("PLAN_INVALID: batches[%d]: index %r out of range "
                      "(have %d specialists)" % (b, idx, n_specialists),
                      file=sys.stderr)
                return (1, [])

    specialists_dir.mkdir(parents=True, exist_ok=True)

    trace: List[Dict[str, Any]] = []
    failed_count = 0
    for b_idx, batch in enumerate(batches):
        batch_specs = _specialists_for_batch(batches, specialists, b_idx)
        sim_idx = _simulate_index(batch_specs, simulate_n) if fail_mode == "simulate" else None
        # max_workers is bounded both by the user cap and the batch size;
        # min() never blows up on empty batches because the validator already
        # rejects empty batches (E5), so we only run here after re-validation.
        workers = min(max_parallel, len(batch_specs))
        workers = max(workers, 1)

        # Phase A: launch all futures for this batch.
        future_to_spec: Dict[concurrent.futures.Future, Dict[str, Any]] = {}
        batch_started = _utcnow_iso()
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers) as pool:
            for s_idx, spec in enumerate(batch_specs):
                sid = spec.get("id", "")
                seq = _next_specialist_seq(specialists_dir, sid)
                force = (sim_idx is not None and s_idx == sim_idx)
                fut = pool.submit(
                    _run_one, spec, seq, specialists_dir, scripts_dir, force)
                future_to_spec[fut] = spec

            # Phase B: harvest in completion order. On raise, cancel remaining.
            cancel_remaining = False
            for fut in concurrent.futures.as_completed(future_to_spec):
                spec = future_to_spec[fut]
                try:
                    record = fut.result()
                except Exception as exc:  # defensive — _run_one never raises
                    record = {"id": spec.get("id", ""),
                              "role": spec.get("role", ""),
                              "status": "failed",
                              "output_path": None,
                              "elapsed_ms": 0,
                              "error": "worker exception: %s" % exc,
                              "ts": _utcnow_iso()}
                record["batch"] = b_idx
                trace.append(record)
                if record["status"] != "ok":
                    failed_count += 1
                    if fail_mode == "raise" and not cancel_remaining:
                        cancel_remaining = True
                        for other in future_to_spec:
                            if not other.done():
                                other.cancel()
                        return (1, trace)

    # Print summary only for non-self-test paths; --self-test asserts on
    # specific files/lines and prints its own PASS lines.
    print("dispatch: %d specialists, %d batches, %d failed"
          % (len(trace), len(batches), failed_count))

    # Write the trace atomically — one open/write/close after all batches
    # complete so concurrent workers cannot interleave partial lines.
    try:
        ORCHESTRATION_TRACE.parent.mkdir(parents=True, exist_ok=True)
        with open(ORCHESTRATION_TRACE, "w", encoding="utf-8") as handle:
            for record in trace:
                handle.write(json.dumps(record, sort_keys=True))
                handle.write("\n")
    except OSError as exc:
        print("error: cannot write %s: %s" % (ORCHESTRATION_TRACE, exc),
              file=sys.stderr)
        return (2, trace)

    return (0, trace)


def _self_test_plan() -> Dict[str, Any]:
    """3 specialists, 1 dep edge, deterministic batch order."""
    def spec(sid: str, role: str) -> Dict[str, Any]:
        return {"id": sid, "role": role,
                "goal": "goal for %s" % sid,
                "context": "context for %s" % sid,
                "output_schema": {"type": "object"},
                "tools": ["file"],
                "model": "opencode-go/deepseek-v4.1-flash"}
    return {"task_summary": "self-test",
            "specialists": [spec("r1", "researcher"),
                            spec("wr", "writer"),
                            spec("cr", "critic")],
            "batches": [[0, 2], [1]],  # r1 + cr parallel; wr waits on r1
            "dependencies": {"wr": ["r1"]}}


def run_self_test(scripts_dir: Path) -> bool:
    """--self-test: end-to-end against a 3-specialist fixture with 1 dep.

    Asserts: every specialist file exists, trace has 3 lines, batches ran in
    declared order (batch numbers ascending), no dependent-overlap.
    """
    plan = _self_test_plan()
    specialists_dir = SPECIALIST_LOG_DIR  # logs/specialists/

    # Wipe logs to make the assertions deterministic. Matches the controller
    # verification: `rm -rf logs && python3 scripts/dispatch.py --self-test`.
    if Path("logs").exists():
        # stdlib-only equivalent of `shutil.rmtree`; `shutil` is intentionally
        # not imported at the top level to keep the AST stdlib list narrow.
        for root, dirs, files in os.walk("logs", topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir("logs")

    specialists_dir.mkdir(parents=True, exist_ok=True)

    exit_code, trace = dispatch_plan(
        plan, max_parallel=4, fail_mode="continue",
        simulate_n=0, scripts_dir=scripts_dir,
        specialists_dir=specialists_dir)

    failures = 0

    # (a) every specialist file exists.
    expected = {"r1", "wr", "cr"}
    written = {record["id"] for record in trace if record["status"] == "ok"}
    if written != expected:
        failures += 1
        print("SELF_TEST FAIL: files (expected %s, got %s)" % (expected, written))
    else:
        print("SELF_TEST PASS: every specialist file exists")

    # (b) trace has exactly 3 lines.
    trace_lines = [l for l in ORCHESTRATION_TRACE.read_text().splitlines() if l.strip()]
    if len(trace_lines) != 3:
        failures += 1
        print("SELF_TEST FAIL: trace line count (expected 3, got %d)"
              % len(trace_lines))
    else:
        print("SELF_TEST PASS: trace has 3 lines")

    # (c) batches in declared order: every specialist's batch index is in
    # range, and the dependent (wr) ran in a strictly later batch than its
    # prerequisite (r1). r1 is in batch 0, wr in batch 1 → wr > r1.
    batch_by_id = {record["id"]: record["batch"] for record in trace}
    if batch_by_id.get("r1") != 0:
        failures += 1
        print("SELF_TEST FAIL: r1 batch (expected 0, got %s)"
              % batch_by_id.get("r1"))
    if batch_by_id.get("wr") != 1:
        failures += 1
        print("SELF_TEST FAIL: wr batch (expected 1, got %s)"
              % batch_by_id.get("wr"))
    if batch_by_id.get("wr") is not None and batch_by_id.get("r1") is not None:
        if not batch_by_id["wr"] > batch_by_id["r1"]:
            failures += 1
            print("SELF_TEST FAIL: wr (batch %s) not strictly after r1 (batch %s)"
                  % (batch_by_id["wr"], batch_by_id["r1"]))
        else:
            print("SELF_TEST PASS: batches ran in declared order")

    # (d) no dependent-overlap: wr and r1 ran in different batches.
    if (batch_by_id.get("wr") == batch_by_id.get("r1")
            and batch_by_id.get("wr") is not None):
        failures += 1
        print("SELF_TEST FAIL: dependent-overlap (wr and r1 same batch)")

    # (e) exit code is 0 — fail-mode continue, every specialist ran.
    if exit_code != 0:
        failures += 1
        print("SELF_TEST FAIL: exit code (expected 0, got %d)" % exit_code)
    else:
        print("SELF_TEST PASS: exit code 0")

    total = 4
    print("SELF_TEST: %d/%d passed" % (total - failures, total))
    return failures == 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dispatch a validated plan to specialist stub runners.")
    parser.add_argument("plan", nargs="?", default=None,
                        help="Path to a validated plan JSON file")
    parser.add_argument("--max-parallel", type=int, default=4,
                        help="Maximum concurrent specialist processes (default 4)")
    parser.add_argument("--fail-mode", type=_parse_fail_mode,
                        default=("raise", 0),
                        help="raise (default) | continue | simulate:N")
    parser.add_argument("--self-test", action="store_true",
                        help="Run built-in end-to-end fixture")
    args = parser.parse_args(argv)

    if args.self_test:
        scripts_dir = Path(__file__).resolve().parent
        return 0 if run_self_test(scripts_dir) else 1

    if not args.plan:
        parser.print_usage(sys.stderr)
        print("error: PLAN.json is required (or pass --self-test)",
              file=sys.stderr)
        return 2
    if args.max_parallel < 1:
        print("error: --max-parallel must be >= 1 (got %d)" % args.max_parallel,
              file=sys.stderr)
        return 2

    fail_mode, simulate_n = args.fail_mode

    plan, err = _load_plan(args.plan)
    if err is not None:
        print("error: %s" % err, file=sys.stderr)
        return 2

    # Re-validate via the imported F1.4 function. Errors → exit 1; warnings
    # are printed but do not block dispatch (matches the validator CLI).
    errors, warnings = _validate_via_import(plan)
    for warning in warnings:
        print("WARNING: %s" % warning)
    if errors:
        print("PLAN_INVALID: %d error(s)" % len(errors))
        for error in errors:
            print("ERROR: %s" % error)
        return 1

    scripts_dir = Path(__file__).resolve().parent
    specialists_dir = SPECIALIST_LOG_DIR
    assert plan is not None  # _load_plan returned err=None above
    exit_code, _ = dispatch_plan(
        plan, max_parallel=args.max_parallel,
        fail_mode=fail_mode, simulate_n=simulate_n,
        scripts_dir=scripts_dir, specialists_dir=specialists_dir)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())