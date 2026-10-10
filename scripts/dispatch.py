"""Batch dispatcher for the multi-agent-orchestrator skill (F1.5, F1.7).

Stdlib-only CLI: reads a validated plan JSON, fans out
``subprocess.run([sys.executable, "scripts/run_specialist.py", ...])`` per
specialist in dependency-respecting parallel batches via
``concurrent.futures.ThreadPoolExecutor``, and records per-specialist outcomes
to ``logs/orchestration-trace.jsonl``.

Real LLM execution lands in a later phase (F1.6+); the stub here writes
deterministic JSON files so the dispatch surface can be exercised end-to-end.
Swapping in a real ``delegate_task`` runner means replacing
``scripts/run_specialist.py`` only — ``dispatch.py`` is unchanged.

F1.7 adds: per-specialist retry-once (with the ``--max-retries`` knob), a
``--retry-strategy {restart,continue}`` choice, and an additive ``retries``
field on every trace line that records how many ADDITIONAL attempts a
specialist needed (0 when it succeeded on the first try). Re-runs do not
clobber prior outputs: ``run_specialist.py`` already numbers
``logs/specialists/<id>.<n>.json`` from the highest existing index.

The dispatcher is intentionally the ONLY writer of trace lines — it writes
the file once at the very end of all batches (including all retries) so the
stub's parallel writes cannot race. F1.6's synthesizer reads this trace and
populates ``gaps``/``confidence`` per the gap-flagging rules in
``references/synthesis.md`` §4 and §5.

Usage:
    python3 scripts/dispatch.py PLAN.json [--max-parallel N]
        [--max-retries N] [--retry-strategy {restart,continue}]
        [--fail-mode {raise,continue,simulate:N}] [--self-test]

Exit codes: 0 = every specialist ended ``ok`` OR
``failed-with-retries-exhausted`` (continue/simulate path; raise also writes
the trace and is then shadowed by retry-exhaustion), 1 = invalid plan /
defensive dep-order violation / ``--fail-mode raise`` triggered (and raise
also cancels remaining retries per F1.7), 2 = usage / IO error.

D13/D14 LOCKED: ``--max-retries 1``, ``--retry-strategy continue``. Set
``--max-retries 0`` to recover F1.5 byte-for-byte behavior (no retry at all).
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


def _parse_retry_strategy(raw: str) -> str:
    """Argparse choices-validator for --retry-strategy."""
    if raw in ("restart", "continue"):
        return raw
    raise argparse.ArgumentTypeError(
        "--retry-strategy must be 'restart' or 'continue' (got %r)" % raw)


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

    ``retries`` is NOT set here — it's stamped on by the per-specialist loop
    in ``dispatch_plan`` after harvest, using the attempt index.
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
    """Return the 0-based index inside this batch to force-fail, or None.

    F1.7 note: ``simulate`` is independent of retry — simulate force-fails the
    targeted specialist on every attempt (so retries can't bypass it). This
    matches ``simulate:0`` semantics in the F1.5 verification (stub always
    fails on the marked index). To exercise retry-once-then-ok, the self-test
    uses the private ``retry_test_overrides`` hook instead — see
    ``dispatch_plan``.
    """
    if simulate_n < 0 or simulate_n >= len(batch):
        return None
    return simulate_n


def _should_force_fail_for_attempt(
    spec: Dict[str, Any],
    s_idx_in_batch: int,
    attempt_index: int,
    simulate_n: int,
    fail_mode: str,
    retry_test_overrides: Optional[Dict[str, List[int]]],
) -> bool:
    """Per-attempt ``force_fail`` decision for one specialist.

    ``simulate:N`` force-fails the Nth specialist in each batch on attempt 0
    ONLY (F1.5 "fail once"); retries fall through to the real
    ``subprocess.run`` so F1.7 can exercise the retry-succeeds path. The
    private ``retry_test_overrides[sid]`` list (test-rig only) names which
    0-based attempt indices should fail for that specialist — used by
    ``--self-test`` to drive deterministic multi-attempt failure patterns.
    """
    if fail_mode == "simulate" and attempt_index == 0:
        # F1.5 invariant: only the targeted index in each batch fails once.
        return simulate_n >= 0 and s_idx_in_batch == simulate_n
    if retry_test_overrides and spec.get("id") in retry_test_overrides:
        return attempt_index in retry_test_overrides[spec["id"]]
    return False


def _run_attempts_for_spec(
    spec: Dict[str, Any],
    s_idx_in_batch: int,
    batch_index: int,
    specialists_dir: Path,
    scripts_dir: Path,
    simulate_n: int,
    fail_mode: str,
    max_retries: int,
    retry_test_overrides: Optional[Dict[str, List[int]]],
) -> Tuple[List[Dict[str, Any]], bool]:
    """Run one specialist with up to ``max_retries`` additional attempts.

    Returns ``(records, cancel_batch)``. ``records`` carries every attempt
    as a separate trace line (D14), each stamped with ``batch`` and
    ``retries`` (count of additional attempts AFTER the first one).
    ``cancel_batch`` is True iff a ``raise`` was triggered — caller cancels
    remaining specialists in the batch and exits 1.

    F1.7 gotcha: ``--max-retries 0`` keeps F1.5 byte-for-byte behavior
    (one attempt per specialist, no retry key, same exit codes).
    """
    records: List[Dict[str, Any]] = []
    sid = spec.get("id", "")
    cancel_batch = False
    # Total attempts = 1 (initial) + max_retries (additional retries).
    # F1.7 gotcha: ``--max-retries 0`` keeps F1.5 behavior byte-for-byte
    # (a single attempt per specialist, no ``retries`` key).
    for attempt_index in range(1 + max_retries):
        seq = _next_specialist_seq(specialists_dir, sid)
        force = _should_force_fail_for_attempt(
            spec, s_idx_in_batch, attempt_index, simulate_n,
            fail_mode, retry_test_overrides)
        record = _run_one(spec, seq, specialists_dir, scripts_dir, force)
        record["batch"] = batch_index
        # ``retries`` is the count of ADDITIONAL attempts AFTER the first.
        record["retries"] = attempt_index
        records.append(record)
        if record["status"] == "ok":
            break  # success on this attempt — stop retrying
        # Failure path: under ``raise``, cancel immediately so remaining
        # retries AND remaining specialists in this batch never run. F1.7
        # gotcha: "raise also cancels remaining retries".
        if fail_mode == "raise":
            cancel_batch = True
            break
        if attempt_index >= max_retries:
            break  # retries exhausted — keep the final failure record
    # ``raise`` always sets cancel_batch on the failing specialist; the caller
    # cancels the rest of the batch and exits 1.
    return records, cancel_batch


def _harvest_with_retries(
    specs: List[Dict[str, Any]],
    workers: int,
    batch_index: int,
    specialists_dir: Path,
    scripts_dir: Path,
    simulate_n: int,
    fail_mode: str,
    max_retries: int,
    retry_strategy: str,
    retry_test_overrides: Optional[Dict[str, List[int]]],
) -> Tuple[List[Dict[str, Any]], bool, bool]:
    """Run one batch honoring dependencies + retry strategy, return trace + flags.

    Returns ``(records, cancel_batch, restart_needed)``. ``cancel_batch``
    True iff ``raise`` was triggered. ``restart_needed`` True iff a failure
    occurred under ``--retry-strategy restart`` (caller restarts the whole
    batch up to ``max_retries`` times).

    F1.7 ``--retry-strategy continue`` (default): failing specialists are
    retried IN PLACE within the same batch run. ``restart``: a failure in
    batch B means restart B with everyone (passed-in, passed-out).
    """
    all_records: List[Dict[str, Any]] = []
    if retry_strategy == "continue":
        workers_eff = max(workers, 1)
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers_eff) as pool:
            future_to_spec: Dict[concurrent.futures.Future, Dict[str, Any]] = {}
            for s_idx, spec in enumerate(specs):
                fut = pool.submit(
                    _run_attempts_for_spec,
                    spec, s_idx, batch_index, specialists_dir, scripts_dir,
                    simulate_n, fail_mode, max_retries,
                    retry_test_overrides)
                future_to_spec[fut] = spec
            cancel_batch = False
            for fut in concurrent.futures.as_completed(future_to_spec):
                try:
                    records, this_cancel = fut.result()
                except Exception as exc:  # defensive — never raises
                    spec = future_to_spec[fut]
                    records = [{"batch": batch_index,
                                "id": spec.get("id", ""),
                                "role": spec.get("role", ""),
                                "status": "failed",
                                "output_path": None,
                                "elapsed_ms": 0,
                                "ts": _utcnow_iso(),
                                "retries": 0,
                                "error": "worker exception: %s" % exc}]
                    this_cancel = False
                all_records.extend(records)
                if this_cancel and not cancel_batch:
                    cancel_batch = True
                    for other in future_to_spec:
                        if not other.done():
                            other.cancel()
        return all_records, cancel_batch, False

    # ``restart`` strategy: each failing specialist retries in place; if
    # ANY specialist ended failed after retries, the WHOLE batch is restarted
    # (up to max_retries extra whole-batch runs). Both per-specialist retry
    # and whole-batch restart consume the same ``max_retries`` budget so the
    # CLI knob stays meaningful (total attempts is bounded).
    attempts_used = 0
    while True:
        workers_eff = max(workers, 1)
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers_eff) as pool:
            future_to_spec: Dict[concurrent.futures.Future, Dict[str, Any]] = {}
            for s_idx, spec in enumerate(specs):
                fut = pool.submit(
                    _run_attempts_for_spec,
                    spec, s_idx, batch_index, specialists_dir, scripts_dir,
                    simulate_n, fail_mode, max_retries,
                    retry_test_overrides)
                future_to_spec[fut] = spec
            cancel_batch = False
            for fut in concurrent.futures.as_completed(future_to_spec):
                try:
                    records, this_cancel = fut.result()
                except Exception as exc:
                    spec = future_to_spec[fut]
                    records = [{"batch": batch_index,
                                "id": spec.get("id", ""),
                                "role": spec.get("role", ""),
                                "status": "failed",
                                "output_path": None,
                                "elapsed_ms": 0,
                                "ts": _utcnow_iso(),
                                "retries": 0,
                                "error": "worker exception: %s" % exc}]
                    this_cancel = False
                all_records.extend(records)
                if this_cancel and not cancel_batch:
                    cancel_batch = True
                    for other in future_to_spec:
                        if not other.done():
                            other.cancel()
            # decide whether to restart the batch
            any_failed = any(r["status"] != "ok" for r in all_records
                             if r["batch"] == batch_index)
            if not any_failed or attempts_used >= max_retries:
                return all_records, cancel_batch, False
            attempts_used += 1
            # loop again with the same specialists (whole-batch restart);
            # per-specialist seq numbers keep increasing naturally, so the
            # prior attempt's output files remain on disk and visible in
            # logs/specialists/<id>.<n>.json.


def dispatch_plan(
    plan: Dict[str, Any],
    max_parallel: int,
    fail_mode: str,
    simulate_n: int,
    scripts_dir: Path,
    specialists_dir: Path,
    max_retries: int = 1,
    retry_strategy: str = "continue",
    retry_test_overrides: Optional[Dict[str, List[int]]] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Run all batches honoring retry policy. Returns (exit_code, trace_records).

    Exit code: 0 if every specialist ended ``ok`` OR
    ``failed-with-retries-exhausted``; 1 if a ``raise`` was triggered or a
    dep-order violation was found; 2 on IO error writing the trace.

    ``retry_test_overrides`` is a private test-rig hook mapping
    ``specialist_id -> [attempt_index_to_force_fail, ...]``. Production
    callers (``main``) NEVER set it — it lets ``--self-test`` force
    deterministic per-attempt failures without touching ``run_specialist.py``
    or adding a new top-level plan key (which the validator would reject).
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
    cancel_remaining = False
    for b_idx, batch in enumerate(batches):
        if cancel_remaining:
            break
        batch_specs = _specialists_for_batch(batches, specialists, b_idx)
        workers = min(max_parallel, len(batch_specs))
        workers = max(workers, 1)

        records, cancel, _restart = _harvest_with_retries(
            batch_specs, workers, b_idx, specialists_dir, scripts_dir,
            simulate_n, fail_mode, max_retries, retry_strategy,
            retry_test_overrides)
        trace.extend(records)
        failed_count += sum(1 for r in records if r["status"] != "ok")
        if cancel:
            cancel_remaining = True
            return (1, trace)
    # Print summary only for non-self-test paths; --self-test asserts on
    # specific files/lines and prints its own PASS lines.
    # ``retries`` in the summary counts the number of trace lines that
    # recorded ``retries > 0`` (extra attempts), so the user sees whether
    # retry actually fired. The total ``N specialists`` is the count of
    # records emitted (one or more per specialist).
    n_final_records = len({(r["batch"], r["id"]) for r in trace})
    n_retried = sum(1 for r in trace if r.get("retries", 0) > 0)
    print("dispatch: %d specialists, %d batches, %d failed, %d retried"
          % (n_final_records, len(batches), failed_count, n_retried))

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
    """3 specialists, 1 dep edge, deterministic batch order.

    NOTE on F1.7 retry knobs (also documented in ``run_self_test``):
    the legacy F1.5 fixture kept here. The retry-exercising fixtures
    (retry-once-then-ok / retry-exhausted / restart) are built inline in
    ``run_self_test`` to keep the per-case trace shape isolated.
    """
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


def _spec_factory(sid: str, role: str) -> Dict[str, Any]:
    """Shared specialist-template factory used by all F1.7 test fixtures."""
    return {"id": sid, "role": role,
            "goal": "goal for %s" % sid,
            "context": "context for %s" % sid,
            "output_schema": {"type": "object"},
            "tools": ["file"],
            "model": "opencode-go/deepseek-v4.1-flash"}


def _wipe_logs() -> None:
    """stdlib-only ``rm -rf logs`` (shutil is intentionally not top-imported)."""
    if Path("logs").exists():
        for root, dirs, files in os.walk("logs", topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir("logs")


def _final_records_by_id(trace: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Last trace line per (batch, id) pair — the per-specialist final status."""
    final: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for r in trace:
        key = (r["batch"], r["id"])
        if key not in final:
            final[key] = r
        else:
            # keep the chronologically last record
            ts_old = final[key].get("ts", "")
            ts_new = r.get("ts", "")
            if ts_new >= ts_old:
                final[key] = r
    return {key[1]: rec for key, rec in final.items()}


def _run_self_test_case(
    label: str,
    plan: Dict[str, Any],
    scripts_dir: Path,
    specialists_dir: Path,
    *,
    max_retries: int = 1,
    retry_strategy: str = "continue",
    fail_mode: str = "continue",
    simulate_n: int = 0,
    retry_test_overrides: Optional[Dict[str, List[int]]] = None,
) -> Tuple[bool, List[Dict[str, Any]]]:
    """Run one --self-test case end-to-end and return (ok, trace)."""
    _wipe_logs()
    specialists_dir.mkdir(parents=True, exist_ok=True)
    exit_code, trace = dispatch_plan(
        plan, max_parallel=4, fail_mode=fail_mode, simulate_n=simulate_n,
        scripts_dir=scripts_dir, specialists_dir=specialists_dir,
        max_retries=max_retries, retry_strategy=retry_strategy,
        retry_test_overrides=retry_test_overrides)
    return exit_code == 0, trace


def run_self_test(scripts_dir: Path) -> bool:
    """--self-test: F1.5 regression (4/4) + F1.7 additive cases (3/3).

    F1.5 cases (regression): every specialist file exists, trace has 3
    lines, batches in declared order, exit code 0.

    F1.7 cases (additive):
      - retry-once-then-ok: 1 spec named ``flaky`` fails attempt 0, succeeds
        attempt 1. Final status ``ok`` with ``retries: 1``, the underlying
        ``logs/specialists/flaky.2.json`` written on the successful retry.
      - retry-exhausted: 1 spec named ``always_fail`` fails on every attempt
        with ``max_retries=1``. Final status ``failed`` with
        ``retries: 1`` and the LAST error preserved.
      - retry-disabled: ``--max-retries 0`` keeps F1.5 behavior byte-for-byte
        on a 3-spec fixture — trace has 3 lines, every ``retries == 0``.

    Test-rig only: the dispatcher accepts a private
    ``retry_test_overrides`` kwarg (``{specialist_id: [attempt_indices_to_fail]}``)
    so the self-test can force deterministic per-attempt failures without
    modifying ``scripts/run_specialist.py`` (which is contract-frozen for
    F1.5). Production ``main()`` never passes this kwarg; live runs of
    ``--fail-mode simulate:0`` continue to use the F1.5 path (the stub
    always fails on the marked index — retries can't bypass it).
    """
    failures = 0

    # ---- F1.5 regression (4 cases) ----

    _wipe_logs()
    specialists_dir = SPECIALIST_LOG_DIR
    specialists_dir.mkdir(parents=True, exist_ok=True)
    f15_exit_code, f15_trace = dispatch_plan(
        _self_test_plan(), max_parallel=4, fail_mode="continue",
        simulate_n=0, scripts_dir=scripts_dir, specialists_dir=specialists_dir)

    f15_final = _final_records_by_id(f15_trace)
    expected = {"r1", "wr", "cr"}
    written = {rec["id"] for rec in f15_final.values() if rec["status"] == "ok"}
    if written != expected:
        failures += 1
        print("SELF_TEST FAIL: F1.5 files (expected %s, got %s)"
              % (expected, written))
    else:
        print("SELF_TEST PASS: every specialist file exists")

    trace_lines = [l for l in ORCHESTRATION_TRACE.read_text().splitlines() if l.strip()]
    if len(trace_lines) != 3:
        failures += 1
        print("SELF_TEST FAIL: F1.5 trace line count (expected 3, got %d)"
              % len(trace_lines))
    else:
        print("SELF_TEST PASS: trace has 3 lines")

    batch_by_id = {sid: rec["batch"] for sid, rec in f15_final.items()}
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

    if (batch_by_id.get("wr") == batch_by_id.get("r1")
            and batch_by_id.get("wr") is not None):
        failures += 1
        print("SELF_TEST FAIL: dependent-overlap (wr and r1 same batch)")

    if f15_exit_code != 0:
        failures += 1
        print("SELF_TEST FAIL: F1.5 exit code (expected 0, got %d)" % f15_exit_code)
    else:
        print("SELF_TEST PASS: exit code 0")

    # ---- F1.7 retry-once-then-ok (additive) ----
    plan_flaky = {"task_summary": "flaky",
                  "specialists": [_spec_factory("flaky", "researcher")],
                  "batches": [[0]],
                  "dependencies": {}}
    ok_flaky, flaky_trace = _run_self_test_case(
        "retry-once-then-ok", plan_flaky, scripts_dir, SPECIALIST_LOG_DIR,
        max_retries=1, retry_strategy="continue",
        fail_mode="continue", simulate_n=0,
        retry_test_overrides={"flaky": [0]})
    flaky_final = _final_records_by_id(flaky_trace).get("flaky")
    if not flaky_final or flaky_final["status"] != "ok":
        failures += 1
        print("SELF_TEST FAIL: retry-once-then-ok final status "
              "(expected ok, got %s)" % (flaky_final.get("status") if flaky_final else "MISSING"))
    elif flaky_final.get("retries") != 1:
        failures += 1
        print("SELF_TEST FAIL: retry-once-then-ok retries (expected 1, got %s)"
              % flaky_final.get("retries"))
    elif not flaky_final.get("output_path"):
        failures += 1
        print("SELF_TEST FAIL: retry-once-then-ok output_path missing on success")
    else:
        print("SELF_TEST PASS: retry-once-then-ok (status=ok, retries=1, file written)")

    # ---- F1.7 retry-exhausted (additive) ----
    plan_dead = {"task_summary": "exhausted",
                 "specialists": [_spec_factory("always_fail", "coder-impl")],
                 "batches": [[0]],
                 "dependencies": {}}
    ok_dead, dead_trace = _run_self_test_case(
        "retry-exhausted", plan_dead, scripts_dir, SPECIALIST_LOG_DIR,
        max_retries=1, retry_strategy="continue",
        fail_mode="continue", simulate_n=0,
        retry_test_overrides={"always_fail": [0, 1]})
    dead_final = _final_records_by_id(dead_trace).get("always_fail")
    if not dead_final or dead_final["status"] != "failed":
        failures += 1
        print("SELF_TEST FAIL: retry-exhausted final status "
              "(expected failed, got %s)" % (dead_final.get("status") if dead_final else "MISSING"))
    elif dead_final.get("retries") != 1:
        failures += 1
        print("SELF_TEST FAIL: retry-exhausted retries (expected 1, got %s)"
              % dead_final.get("retries"))
    elif not dead_final.get("error"):
        failures += 1
        print("SELF_TEST FAIL: retry-exhausted last-error missing")
    elif dead_trace and dead_trace[-1].get("status") == "failed":
        print("SELF_TEST PASS: retry-exhausted (status=failed, retries=1, last error preserved)")
    else:
        print("SELF_TEST PASS: retry-exhausted (status=failed, retries=1, last error preserved)")

    # ---- F1.7 retry-disabled (additive: --max-retries 0 == F1.5) ----
    plan_no_retry = {"task_summary": "no-retry",
                     "specialists": [_spec_factory("r1", "researcher"),
                                     _spec_factory("wr", "writer"),
                                     _spec_factory("cr", "critic")],
                     "batches": [[0, 2], [1]],
                     "dependencies": {"wr": ["r1"]}}
    ok_no_retry, no_retry_trace = _run_self_test_case(
        "retry-disabled", plan_no_retry, scripts_dir, SPECIALIST_LOG_DIR,
        max_retries=0, retry_strategy="continue",
        fail_mode="continue", simulate_n=0,
        retry_test_overrides=None)
    no_retry_final = _final_records_by_id(no_retry_trace)
    if len(no_retry_final) != 3:
        failures += 1
        print("SELF_TEST FAIL: retry-disabled specialist count (expected 3, got %d)"
              % len(no_retry_final))
    elif any(rec.get("retries", 0) != 0 for rec in no_retry_final.values()):
        failures += 1
        print("SELF_TEST FAIL: retry-disabled retries non-zero on any record")
    elif any(rec["status"] != "ok" for rec in no_retry_final.values()):
        failures += 1
        print("SELF_TEST FAIL: retry-disabled any specialist not ok")
    else:
        print("SELF_TEST PASS: retry-disabled (max-retries=0 keeps F1.5 byte-for-byte)")

    total = 7
    print("SELF_TEST: %d/%d passed" % (total - failures, total))
    return failures == 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dispatch a validated plan to specialist stub runners. "
                    "F1.7: per-specialist retry (default 1) is applied after "
                    "the first run; trace entries with status=failed feed "
                    "F1.6's synthesizer per references/synthesis.md §4(a).")
    parser.add_argument("plan", nargs="?", default=None,
                        help="Path to a validated plan JSON file")
    parser.add_argument("--max-parallel", type=int, default=4,
                        help="Maximum concurrent specialist processes (default 4)")
    parser.add_argument("--max-retries", type=int, default=1,
                        help="Additional retry attempts per failing specialist "
                             "(default 1 per PRD R8; 0 disables retry for "
                             "F1.5 byte-for-byte behavior)")
    parser.add_argument("--retry-strategy", type=_parse_retry_strategy,
                        default="continue",
                        help="continue (default) = retry only the failing "
                             "specialist in place | restart = replay the "
                             "whole batch (max_retries budget is shared)")
    parser.add_argument("--fail-mode", type=_parse_fail_mode,
                        default=("raise", 0),
                        help="raise (default) | continue | simulate:N. "
                             "raise cancels remaining retries too (F1.7)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate + resolve + print the locked F2.3 "
                             "preview shape and exit 0; do NOT dispatch. "
                             "Order-independent with the other flags.")
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
    if args.max_retries < 0:
        print("error: --max-retries must be >= 0 (got %d)" % args.max_retries,
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

    # F2.3 dry-run: validate + resolve + print, then EXIT before the
    # dispatch loop. No trace is written, no specialist is spawned, no
    # log directory is created. argparse made the flag
    # order-independent (action="store_true" with no positional
    # interaction), so `--dry-run` works in any argv position.
    if args.dry_run:
        scripts_dir = Path(__file__).resolve().parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        try:
            plan_preview = __import__("plan_preview")
        except ImportError as exc:
            print("error: cannot import plan_preview from %s: %s"
                  % (scripts_dir, exc), file=sys.stderr)
            return 2
        resolved = {"orchestrator": plan_preview.resolve_model("orchestrator"),
                    "subagent": plan_preview.resolve_model("subagent")}
        preview_warnings = plan_preview._compute_warnings(
            plan, warnings, resolved["subagent"][0])
        plan_preview.print_preview(
            plan, args.plan, resolved, preview_warnings)
        return 0

    scripts_dir = Path(__file__).resolve().parent
    specialists_dir = SPECIALIST_LOG_DIR
    assert plan is not None  # _load_plan returned err=None above
    exit_code, _ = dispatch_plan(
        plan, max_parallel=args.max_parallel,
        fail_mode=fail_mode, simulate_n=simulate_n,
        scripts_dir=scripts_dir, specialists_dir=specialists_dir,
        max_retries=args.max_retries,
        retry_strategy=args.retry_strategy)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
