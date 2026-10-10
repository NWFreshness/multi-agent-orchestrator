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
        [--tokens-per-char N]
        [--cache-namespace NAME] [--cache-ttl-hours H]
        [--progress PATH]

Exit codes: 0 = every specialist ended ``ok`` OR
``failed-with-retries-exhausted`` (continue/simulate path; raise also writes
the trace and is then shadowed by retry-exhaustion), 1 = invalid plan /
defensive dep-order violation / ``--fail-mode raise`` triggered (and raise
also cancels remaining retries per F1.7), 2 = usage / IO error.

D13/D14 LOCKED: ``--max-retries 1``, ``--retry-strategy continue``. Set
``--max-retries 0`` to recover F1.5 byte-for-byte behavior (no retry at all).

F2.6 LOCKED: ``--tokens-per-char 0.25`` default. Per-specialist
``token_estimate`` is computed by the dispatcher from the stub's
``task`` + ``context`` field lengths and stamped onto every final
trace record (independent of schema-check pass/fail). Set ``--tokens-per-char 0``
to disable (the field is still present and 0).
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


def _read_specialist_output(output_path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read the JSON written by ``run_specialist.py``.

    Returns ``(parsed_body, error)``. ``parsed_body`` is the dict shape
    on success and ``None`` on any error. ``error`` is a short,
    human-readable reason for the failure (None on success).
    """
    try:
        with open(output_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return (None, "specialist output file not found: %s" % output_path)
    except json.JSONDecodeError as exc:
        return (None, "specialist output is not valid JSON: %s" % exc)
    except OSError as exc:
        return (None, "cannot read specialist output: %s" % exc)
    if not isinstance(data, dict):
        return (None, "specialist output is not a JSON object")
    return (data, None)


def _compute_token_estimate(body: Optional[Dict[str, Any]],
                            tokens_per_char: float) -> int:
    """F2.6: token estimate from the stub's task + context string lengths.

    D36 default is ``tokens_per_char = 0.25`` (≈ 4 chars per token, the
    standard English approximation — inverse of the spec's ``// 4`` form).
    D37: cast to int after the product; when ``tokens_per_char > 0`` and
    either string has length > 0, the minimum is 1 (even a tiny prompt
    is at least one token). When ``tokens_per_char == 0`` the field is
    0 (effectively disabled — the trace still carries the field so
    downstream readers can rely on its existence). Negative or zero
    inputs are floored to 0; the ``int(...)`` cast naturally floors.

    The stub body shape is the F1.5 echo: ``{"task": ..., "context": ...,
    ...}``. If either key is missing or non-string (malformed stub),
    treat as empty string — don't crash, just yield 0 (with the
    min-1 floor guarded by ``any_text``).
    """
    if tokens_per_char <= 0:
        return 0
    if not isinstance(body, dict):
        return 0
    task = body.get("task", "")
    context = body.get("context", "")
    if not isinstance(task, str):
        task = ""
    if not isinstance(context, str):
        context = ""
    any_text = (len(task) > 0) or (len(context) > 0)
    total_chars = len(task) + len(context)
    raw = int(total_chars * tokens_per_char)
    if raw < 1 and any_text:
        return 1
    if raw < 0:
        return 0
    return raw


def _apply_token_estimates(
    trace: List[Dict[str, Any]],
    tokens_per_char: float,
) -> List[Dict[str, Any]]:
    """F2.6: stamp ``token_estimate`` on every final trace record.

    Runs after harvest but independently of ``_apply_schema_check`` so
    that ``--no-schema-check`` still gets token-estimate stamps. Walks
    the trace, finds the final record per ``(batch, id)``, and for each
    record that has an existing output_path reads the stub JSON once
    via ``_read_specialist_output``, then calls
    ``_compute_token_estimate(body, tokens_per_char)`` and stamps the
    result. The ``tokens_per_char == 0`` case yields 0 for every record
    (disabled — the field is still present, see D37 / spec §"Locked
    behavior").

    Mutates each final record in place by adding / overwriting
    ``token_estimate: int``. Idempotent — running twice yields the same
    values.
    """
    # Last trace line per (batch, id) — same rule as
    # ``_final_records_by_id``.
    final_indices: Dict[Tuple[int, str], int] = {}
    for i, r in enumerate(trace):
        key = (r.get("batch", -1), r.get("id", ""))
        if key not in final_indices:
            final_indices[key] = i
        else:
            ts_old = trace[final_indices[key]].get("ts", "")
            ts_new = r.get("ts", "")
            if ts_new >= ts_old:
                final_indices[key] = i
    # Read body once per record. We avoid the cache for clarity (the
    # stub JSON file is tiny and we're not on a hot path). When
    # tokens_per_char == 0 we still want the field stamped as 0, so we
    # don't short-circuit on that — _compute_token_estimate already
    # returns 0 in that case without reading the file. So when the
    # conversion factor is 0, skip the file read entirely (saves I/O on
    # the disabled path).
    if tokens_per_char <= 0:
        for (b, sid), idx in final_indices.items():
            trace[idx]["token_estimate"] = 0
        return trace
    for (b, sid), idx in final_indices.items():
        record = trace[idx]
        output_path = record.get("output_path")
        if not output_path:
            # No output file to measure — no estimate possible.
            record["token_estimate"] = 0
            continue
        body, _err = _read_specialist_output(output_path)
        record["token_estimate"] = _compute_token_estimate(
            body, tokens_per_char)
    return trace


def _apply_schema_check(
    trace: List[Dict[str, Any]],
    specialists: List[Dict[str, Any]],
    validate_fn,
    tokens_per_char: float = 0.25,
) -> List[Dict[str, Any]]:
    """F2.4 post-dispatch per-specialist schema check (D30).

    For each specialist's FINAL trace record (the last entry per
    ``(batch, id)`` — i.e. the post-retry result, not the intermediate
    retry attempts), open its ``output_path``, parse the JSON, and
    validate the ``output`` field against the specialist's declared
    ``output_schema``. ``validate_fn`` is the project-local
    ``validate_against_schema`` callable injected at call time to keep
    this module's top-level imports stdlib-only.

    Mutates each final record in place:
      - successful match → add ``schema_ok: true``
      - failure → set ``status: failed`` + ``error: "output_schema
        mismatch: <reason>"``. ``schema_ok`` is NOT set on failure so
        a single boolean answers "did the output match?".

    Other trace records (retry attempts) are left untouched — D30
    specifies the check is on the FINAL output per specialist. Per
    D30, a failed check does NOT cancel the rest of the trace. The
    F2.6 token estimate is stamped by ``_apply_token_estimates`` in a
    separate, always-on pass (so ``--no-schema-check`` still gets the
    token field).
    """
    schema_by_id = {spec.get("id"): spec.get("output_schema")
                    for spec in specialists if isinstance(spec, dict)}
    # Last trace line per (batch, id) — same rule as ``_final_records_by_id``.
    final_indices: Dict[Tuple[int, str], int] = {}
    for i, r in enumerate(trace):
        key = (r.get("batch", -1), r.get("id", ""))
        if key not in final_indices:
            final_indices[key] = i
        else:
            ts_old = trace[final_indices[key]].get("ts", "")
            ts_new = r.get("ts", "")
            if ts_new >= ts_old:
                final_indices[key] = i
    for (b, sid), idx in final_indices.items():
        record = trace[idx]
        if record.get("status") != "ok":
            # The specialist's run already failed; no need to re-check.
            continue
        schema = schema_by_id.get(sid)
        if not isinstance(schema, dict):
            # No declared schema (or non-dict — already flagged by E3).
            # Skip without stamping schema_ok.
            continue
        output_path = record.get("output_path")
        if not output_path:
            # No file to validate; nothing to check.
            continue
        body, err = _read_specialist_output(output_path)
        if err is not None:
            record["status"] = "failed"
            record["error"] = "output_schema mismatch: %s" % err
            continue
        # F1.5 stub writes the specialist payload fields PLUS ``output`` is
        # NOT one of them — the stub's body is the metadata echo (id, role,
        # task, context, output_schema, timestamp, stub). So for a schema
        # requiring ``answer``, this WILL fail the match — that's the
        # intended "real check" behavior.
        assert body is not None  # narrowed by the err != None check above
        output_value = body.get("output", body)
        ok, mismatch = validate_fn(output_value, schema)
        if ok:
            record["schema_ok"] = True
        else:
            record["status"] = "failed"
            record["error"] = "output_schema mismatch: %s" % (
                mismatch or "validation error")
    # ``tokens_per_char`` is accepted for forward-compat with the
    # F2.4->F2.6 call sites; F2.6's own stamp lives in
    # ``_apply_token_estimates``.
    _ = tokens_per_char
    return trace


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


def _try_cache_lookup(
    spec: Dict[str, Any],
    sid: str,
    role: str,
    output_path: Path,
    scripts_dir: Path,
    namespace: str,
    ttl_hours: float,
) -> Optional[Dict[str, Any]]:
    """F3.3: cache hit path — return an ``ok`` + ``cached: True`` record, else None.

    Never raises: any import / key / IO problem is a miss and the caller
    falls through to the normal subprocess run (D47).
    """
    try:
        scripts_dir_str = str(scripts_dir)
        if scripts_dir_str not in sys.path:
            sys.path.insert(0, scripts_dir_str)
        specialist_cache = __import__("specialist_cache")
        key = specialist_cache.cache_key(
            spec.get("role", ""), spec.get("goal", ""),
            spec.get("context", ""), spec.get("model", ""),
            spec.get("output_schema", {}))
        blob = specialist_cache.lookup(namespace, key, ttl_hours)
        if blob is None:
            return None
        start_ts = _utcnow_iso()
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as handle:
                handle.write(blob)
        except OSError:
            return None
        return {"ts": _utcnow_iso(), "started_ts": start_ts,
                "id": sid, "role": role,
                "status": "ok", "output_path": str(output_path),
                "elapsed_ms": 0, "returncode": 0, "cached": True}
    except Exception:  # noqa: BLE001
        return None


def _try_cache_store(
    spec: Dict[str, Any],
    output_path: Path,
    scripts_dir: Path,
    namespace: str,
) -> None:
    """F3.3: miss-store path — persist a fresh stub output, best-effort.

    Never raises; a False from ``store`` is ignored (D47).
    """
    try:
        scripts_dir_str = str(scripts_dir)
        if scripts_dir_str not in sys.path:
            sys.path.insert(0, scripts_dir_str)
        specialist_cache = __import__("specialist_cache")
        key = specialist_cache.cache_key(
            spec.get("role", ""), spec.get("goal", ""),
            spec.get("context", ""), spec.get("model", ""),
            spec.get("output_schema", {}))
        try:
            with open(str(output_path), "rb") as handle:
                blob = handle.read()
        except OSError:
            return
        specialist_cache.store(namespace, key, blob)
    except Exception:  # noqa: BLE001
        return


def _run_one(
    spec: Dict[str, Any],
    seq: int,
    specialists_dir: Path,
    scripts_dir: Path,
    force_fail: bool,
    cache_namespace: str = "",
    cache_ttl_hours: float = 24.0,
) -> Dict[str, Any]:
    """Worker target: shell out to run_specialist.py for one specialist.

    Always returns a record dict for the orchestration trace — never raises.
    ``status`` is ``"ok"`` or ``"failed"``; ``output_path`` is the absolute
    path the runner was supposed to write (or ``None`` on pre-launch failure).

    ``retries`` is NOT set here — it's stamped on by the per-specialist loop
    in ``dispatch_plan`` after harvest, using the attempt index.

    F3.3: when ``cache_namespace`` is set and this is not a forced
    failure, ``lookup()`` runs BEFORE the subprocess — on a hit the
    cached bytes are written to the seq output_path and an ``ok`` record
    with ``cached: True`` is returned with no subprocess spawned.
    After a successful non-forced run the output bytes are ``store()``d
    best-effort. Forced failures bypass both paths; failed runs are
    never stored.
    """
    sid = spec.get("id", "")
    role = spec.get("role", "")
    payload = _build_specialist_payload(spec)
    output_path = specialists_dir / ("%s.%d.json" % (sid, seq))
    if cache_namespace and not force_fail:
        hit = _try_cache_lookup(spec, sid, role, output_path, scripts_dir,
                                cache_namespace, cache_ttl_hours)
        if hit is not None:
            return hit
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
    # F3.3 miss-store path: after a successful non-forced run, persist
    # the output bytes best-effort (ignored on failure). Forced runs
    # and failed runs never touch the cache.
    if ok and cache_namespace and not force_fail:
        _try_cache_store(spec, output_path, scripts_dir, cache_namespace)
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
    cache_namespace: str = "",
    cache_ttl_hours: float = 24.0,
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
        record = _run_one(spec, seq, specialists_dir, scripts_dir, force,
                          cache_namespace, cache_ttl_hours)
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
    cache_namespace: str = "",
    cache_ttl_hours: float = 24.0,
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
                    retry_test_overrides,
                    cache_namespace, cache_ttl_hours)
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
                    retry_test_overrides,
                    cache_namespace, cache_ttl_hours)
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


def _append_progress_records(
    records: List[Dict[str, Any]],
    progress_path: Optional[str],
) -> None:
    """F3.4: append one batch's harvest records to the progress sink.

    Best-effort (D51): any IO failure → single ``WARNING: progress
    sink ...`` to stderr, dispatch continues, exit code unaffected.
    ``None`` touches nothing. Each line is the trace-record dict
    verbatim via ``json.dumps(record, sort_keys=True)`` (D52); the file
    is opened in append mode and closed per batch so a ``tail -f``
    consumer sees flushed lines, with parent dirs auto-created (D49).
    """
    if not progress_path:
        return
    try:
        sink = Path(progress_path)
        sink.parent.mkdir(parents=True, exist_ok=True)
        with open(sink, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True))
                handle.write("\n")
    except Exception as exc:  # noqa: BLE001 - D51: sink IO never fails a run
        print("WARNING: progress sink %s: %s" % (progress_path, exc),
              file=sys.stderr)


def _spawn_background(argv: List[str]) -> int:
    """F3.5 (D53): re-exec self detached and return 0.

    Rebuilds the child argv as ``[sys.executable, <abspath dispatch.py>]
    + forwarded flags`` — everything except ALL exact ``--background``
    tokens (stripped; ``--self-test`` never reaches here per D54).
    Launches via ``subprocess.Popen(..., start_new_session=True,
    stdout=log_f, stderr=subprocess.STDOUT)`` with
    ``log_f = open(logs/background-<UTC-ts>.log, 'wb')`` (parent dirs
    created) and ``cwd=os.getcwd()`` so relative ``logs/`` + ``.cache/``
    resolve identically. Prints
    ``background: pid=<n> log=<log path> trace=<trace path>`` to stdout
    and returns 0. The parent closes its handle immediately (the child
    holds its own fd — no hang on parent exit). POSIX-only detach
    (``start_new_session``); no Windows ``creationflags`` branch.
    """
    forwarded = [a for a in argv if a != "--background"]
    dispatch_abs = str(Path(__file__).resolve())
    child_argv = [sys.executable, dispatch_abs] + forwarded
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path("logs") / ("background-%s.log" % ts)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print("error: cannot create %s: %s" % (log_path.parent, exc),
              file=sys.stderr)
        return 2
    try:
        log_f = open(log_path, "wb")
    except OSError as exc:
        print("error: cannot write %s: %s" % (log_path, exc),
              file=sys.stderr)
        return 2
    try:
        proc = subprocess.Popen(
            child_argv, start_new_session=True, cwd=os.getcwd(),
            stdout=log_f, stderr=subprocess.STDOUT)
    except OSError as exc:
        try:
            log_f.close()
        except OSError:
            pass
        print("error: background spawn failed: %s" % exc, file=sys.stderr)
        return 2
    try:
        log_f.close()
    except OSError:
        pass
    print("background: pid=%d log=%s trace=%s"
          % (proc.pid, log_path, ORCHESTRATION_TRACE))
    return 0


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
    schema_check: bool = True,
    *,
    orchestrator: Optional[str] = None,
    subagent: Optional[str] = None,
    max_cost: float = 0.0,
    cost_per_call: float = 0.01,
    tokens_per_char: float = 0.25,
    cache_namespace: str = "",
    cache_ttl_hours: float = 24.0,
    progress_path: Optional[str] = None,
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

    ``schema_check`` (default True, F2.4) toggles the post-dispatch
    per-specialist ``output_schema`` content check. When False, the
    check is skipped entirely (no ``schema_ok`` field is added, no
    schema mismatches are recorded).

    F2.5 keyword-only kwargs (after ``*``): ``orchestrator`` and
    ``subagent`` feed the option tier of ``resolve_model()`` (per
    call); ``max_cost`` and ``cost_per_call`` are accepted for signature
    parity with the CLI surface but the cost-budget check itself runs
    in ``main()`` before this function is called (no logs/ created on
    over-budget). These parameters do not change the behavior of this
    function today; they're a forward-compatible seam.

    F2.6 keyword-only kwarg: ``tokens_per_char`` (default 0.25) feeds
    the per-specialist token-estimate stamp applied to each final
    trace record via ``_apply_token_estimates`` (independent of
    ``schema_check``). When 0, the stamp is 0 on every record
    (disabled). See D36 / D37.

    F3.3 keyword-only kwargs: ``cache_namespace`` (default ``''`` =
    disabled) and ``cache_ttl_hours`` (default 24.0) thread down to
    ``_run_one`` for the lookup-before-run / store-after-success
    paths (same threading shape as F2.6 ``tokens_per_char``).

    F3.4 keyword-only kwarg: ``progress_path`` (default None =
    disabled) streams each batch's harvest records to the local
    append-only JSONL sink via ``_append_progress_records`` (same
    threading shape as F2.6 / F3.3). Appends happen per batch INSIDE
    the loop — before the cancel check and before the token/schema
    passes — so ``fail_mode=raise`` still streams completed batches
    and the sink carries harvest-form records (D52). Best-effort
    (D51); ``--dry-run`` never touches the sink.
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
            retry_test_overrides,
            cache_namespace=cache_namespace,
            cache_ttl_hours=cache_ttl_hours)
        trace.extend(records)
        # F3.4: stream this batch's harvest records to the progress
        # sink (open-append-close per batch) BEFORE the cancel check
        # so fail_mode=raise still streams completed batches, and
        # BEFORE the token/schema passes so the sink carries
        # harvest-form records (D52). Best-effort — never raises (D51).
        _append_progress_records(records, progress_path)
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

    # F2.6: stamp ``token_estimate`` on every final record — runs
    # unconditionally so ``--no-schema-check`` still gets the field.
    # Observation only; does NOT change exit codes or status. The
    # schema check below may then flip status to ``failed`` on a
    # mismatch, but the token estimate stays stamped.
    _apply_token_estimates(trace, tokens_per_char)

    # F2.4 post-dispatch schema check (D30). Runs AFTER retries are
    # exhausted but BEFORE the trace is written. Lazily imports the
    # project-local validator so this module's top-level imports stay
    # stdlib-only (controller AST check). When schema_check is False
    # (``--no-schema-check``), this whole block is skipped — no
    # ``schema_ok`` field is added and no schema mismatches are
    # recorded.
    if schema_check:
        try:
            scripts_dir_str = str(scripts_dir)
            if scripts_dir_str not in sys.path:
                sys.path.insert(0, scripts_dir_str)
            import validate_schema as _vs  # type: ignore[import-not-found]
            validate_fn = _vs.validate_against_schema
        except Exception as exc:  # noqa: BLE001
            print("error: cannot run schema check (validate_schema "
                  "import failed: %s)" % exc, file=sys.stderr)
            return (2, trace)
        trace = _apply_schema_check(trace, specialists, validate_fn,
                                    tokens_per_char)
        # Re-derive failed_count so the post-check tally is accurate.
        failed_count = sum(1 for r in trace
                           if r.get("status") != "ok"
                           and r.get("batch", -1) >= 0)
        n_final_records = len({(r["batch"], r["id"]) for r in trace})
        n_schema_failed = sum(
            1 for r in trace
            if r.get("status") == "failed"
            and isinstance(r.get("error"), str)
            and r["error"].startswith("output_schema mismatch"))
        print("dispatch: %d specialists, %d batches, %d failed, %d retried, "
              "%d schema-mismatch"
              % (n_final_records, len(batches), failed_count, n_retried,
                 n_schema_failed))

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

    F3.3 cases (additive, total 15→17):
      - cache-HIT: pre-populate the cache via ``cache_key``, dispatch
        with that namespace, assert the final record carries
        ``cached is True`` and the output file bytes == planted bytes.
      - cache-MISS: fresh namespace → normal run, no ``cached`` flag,
        and the cache entry file is created for the key.

    F3.4 cases (additive, total 17→19):
      - STREAM: 2-batch plan + temp sink → sink line count == trace
        record count, every line parses as JSON with
        ``id``/``batch``/``status``, batch-0 lines precede batch-1
        lines.
      - SILENT: no progress flag → no file created at the would-be
        path. Temp sinks live under ``/tmp`` and are removed in
        ``finally`` (never a repo-relative default).

    F3.5 cases (additive, total 19→21):
      - BG: spawn a real detached child via ``_spawn_background`` with
        a ``--progress`` sink, poll the sink to a 30s ``datetime``
        deadline, reap with ``os.waitpid``, assert exit 0 + sink lines
        parse + trace holds real specialist records.
      - BG_EXCLUSIVE: ``main([plan, --background, --dry-run])`` → 2;
        ``main([--self-test, --background])`` → 2.
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

    # ---- F2.4 post-dispatch schema check (additive) ----
    # Three specialists, three different declared output_schemas:
    #   - ``good_obj`` — requires ``{ type: object }``; the stub body IS an
    #     object → schema_ok: true, status: ok.
    #   - ``needs_answer`` — requires an ``answer`` string property; the
    #     stub body has no ``answer`` field → status flips to failed
    #     with ``output_schema mismatch: ...`` per D30.
    #   - ``wrong_type`` — requires an integer; the stub body is an
    #     object → status flips to failed with a type-mismatch error.
    # The fixture proves the check is REAL (not a tautology) — every
    # schema that the stub output fails to match produces a real error.
    plan_schema = {
        "task_summary": "schema-check",
        "specialists": [
            {"id": "good_obj", "role": "researcher",
             "goal": "g", "context": "c",
             "output_schema": {"type": "object"},
             "tools": ["file"],
             "model": "opencode-go/deepseek-v4.1-flash"},
            {"id": "needs_answer", "role": "critic",
             "goal": "g", "context": "c",
             "output_schema": {"type": "object",
                               "properties": {"answer": {"type": "string"}},
                               "required": ["answer"]},
             "tools": ["file"],
             "model": "opencode-go/deepseek-v4.1-flash"},
            {"id": "wrong_type", "role": "writer",
             "goal": "g", "context": "c",
             "output_schema": {"type": "integer"},
             "tools": ["file"],
             "model": "opencode-go/deepseek-v4.1-flash"},
        ],
        "batches": [[0, 1, 2]],
        "dependencies": {},
    }
    _wipe_logs()
    specialists_dir_sc = SPECIALIST_LOG_DIR
    specialists_dir_sc.mkdir(parents=True, exist_ok=True)
    sc_exit_code, sc_trace = dispatch_plan(
        plan_schema, max_parallel=4, fail_mode="continue",
        simulate_n=0, scripts_dir=scripts_dir,
        specialists_dir=specialists_dir_sc,
        max_retries=0, retry_strategy="continue",
        retry_test_overrides=None,
        schema_check=True)
    sc_final = _final_records_by_id(sc_trace)
    good = sc_final.get("good_obj") or {}
    bad = sc_final.get("needs_answer") or {}
    wrong = sc_final.get("wrong_type") or {}
    good_ok = (good.get("status") == "ok"
               and good.get("schema_ok") is True)
    bad_failed = (bad.get("status") == "failed"
                  and isinstance(bad.get("error"), str)
                  and bad["error"].startswith("output_schema mismatch")
                  and "answer" in bad["error"])
    wrong_failed = (wrong.get("status") == "failed"
                    and isinstance(wrong.get("error"), str)
                    and wrong["error"].startswith("output_schema mismatch"))
    if sc_exit_code != 0:
        failures += 1
        print("SELF_TEST FAIL: schema-check exit code (expected 0, got %d)"
              % sc_exit_code)
    elif not good_ok:
        failures += 1
        print("SELF_TEST FAIL: schema-check good_obj not ok+schema_ok "
              "(got %s)" % good)
    elif not bad_failed:
        failures += 1
        print("SELF_TEST FAIL: schema-check needs_answer should be failed "
              "with output_schema mismatch (got %s)" % bad)
    elif not wrong_failed:
        failures += 1
        print("SELF_TEST FAIL: schema-check wrong_type should be failed "
              "with output_schema mismatch (got %s)" % wrong)
    elif "schema_ok" in bad:
        failures += 1
        print("SELF_TEST FAIL: schema-check schema_ok must NOT be set on "
              "mismatch (got %s)" % bad)
    elif "schema_ok" in wrong:
        failures += 1
        print("SELF_TEST FAIL: schema-check schema_ok must NOT be set on "
              "mismatch (got %s)" % wrong)
    else:
        print("SELF_TEST PASS: post-dispatch schema check "
              "(good=ok, mismatch=failed with output_schema mismatch error)")

    # ---- F2.5 cost-budget + option-tier resolution (additive) ----
    # Two fixtures: (a) --max-cost 0.001 with a 3-spec plan → exit 2,
    # PLAN_OVER_BUDGET line printed, no logs/ created; (b) --max-cost
    # 100 with the same plan → exit 0, normal dispatch. These exercise
    # the F2.5 budget check WITHOUT going through the live
    # ``run_specialist`` path's trace file, so we drive ``main()``
    # directly with a stubbed argv. We use a tmp plan JSON in /tmp so
    # it survives the ``_wipe_logs()`` calls (which recursively remove
    # anything under the repo's ``logs/`` directory).

    cost_over_plan = _self_test_plan()  # 3 specialists, default config
    cost_over_path = Path("/tmp") / "_f25_cost_over.json"
    with open(cost_over_path, "w", encoding="utf-8") as _h:
        json.dump(cost_over_plan, _h)
    _wipe_logs()
    over_exit = main([
        str(cost_over_path),
        "--max-cost", "0.001",
    ])
    over_logs = Path("logs").is_dir()
    if over_exit != 2:
        failures += 1
        print("SELF_TEST FAIL: cost-over-budget exit code "
              "(expected 2, got %d)" % over_exit)
    elif over_logs:
        failures += 1
        print("SELF_TEST FAIL: cost-over-budget created logs/ "
              "(should NOT)")
    else:
        print("SELF_TEST PASS: --max-cost 0.001 + 3-spec plan → "
              "exit 2, no logs/ created")

    # Fixture F2.5-B: cost under budget → exit 0.
    _wipe_logs()
    under_exit = main([
        str(cost_over_path),
        "--max-cost", "100",
    ])
    if under_exit != 0:
        failures += 1
        print("SELF_TEST FAIL: cost-under-budget exit code "
              "(expected 0, got %d)" % under_exit)
    else:
        print("SELF_TEST PASS: --max-cost 100 + 3-spec plan → "
              "exit 0 (under budget)")

    # Fixture F2.5-C: --max-cost 0 (D34 default) → no check, exit 0.
    _wipe_logs()
    no_budget_exit = main([str(cost_over_path)])
    if no_budget_exit != 0:
        failures += 1
        print("SELF_TEST FAIL: --max-cost 0 (default) exit code "
              "(expected 0, got %d)" % no_budget_exit)
    else:
        print("SELF_TEST PASS: --max-cost 0 (D34 default) → no budget check, "
              "exit 0")

    # Fixture F2.5-D: --dry-run + --max-cost 0.001 → dry-run still exits 2,
    # no logs/ created (cost check fires before the preview shape is printed).
    _wipe_logs()
    dry_over_exit = main([
        str(cost_over_path),
        "--dry-run",
        "--max-cost", "0.001",
    ])
    dry_over_logs = Path("logs").is_dir()
    if dry_over_exit != 2:
        failures += 1
        print("SELF_TEST FAIL: dry-run + cost-over-budget exit code "
              "(expected 2, got %d)" % dry_over_exit)
    elif dry_over_logs:
        failures += 1
        print("SELF_TEST FAIL: dry-run + cost-over-budget created logs/")
    else:
        print("SELF_TEST PASS: --dry-run + --max-cost 0.001 → exit 2, "
              "no logs/ created")

    # Fixture F2.5-E: --dry-run + --orchestrator-model + --subagent-model
    # → preview shape shows option-tier values for both targets.
    _wipe_logs()
    dry_models_buf: List[str] = []
    saved_stdout = sys.stdout

    class _DryCap(list):
        def write(self, s: str) -> int:
            self.append(s)
            return len(s)

    cap_d = _DryCap()
    sys.stdout = cap_d
    try:
        dry_models_exit = main([
            str(cost_over_path),
            "--dry-run",
            "--orchestrator-model", "opencode-go/cli-orch",
            "--subagent-model", "opencode-go/cli-sub",
        ])
    finally:
        sys.stdout = saved_stdout
    dry_models_text = "".join(dry_models_buf + [s for s in cap_d])
    has_orch_option = ("opencode-go/cli-orch" in dry_models_text
                       and "(source: option)" in dry_models_text)
    if dry_models_exit != 0:
        failures += 1
        print("SELF_TEST FAIL: --dry-run + model flags exit code "
              "(expected 0, got %d)" % dry_models_exit)
    elif not has_orch_option:
        failures += 1
        print("SELF_TEST FAIL: --dry-run + --orchestrator-model did NOT "
              "print option source in preview")
    else:
        print("SELF_TEST PASS: --dry-run + --orchestrator-model + "
              "--subagent-model → preview shows option source")

    # Cleanup the temp plan so the repo's working tree stays clean.
    if cost_over_path.is_file():
        cost_over_path.unlink()
    _wipe_logs()

    # ---- F2.6 per-call token estimate (additive) ----
    # Two fixtures proving the dispatcher stamps ``token_estimate`` on the
    # final trace record from the stub's ``task`` + ``context`` lengths.
    # Both fixtures use a 3-spec plan (the F1.4 validator requires 3-15
    # specialists and at least one reviewer-* or critic role).
    # Fixture A: default ``--tokens-per-char 0.25`` with t1 carrying
    # task='a'*100 / context='b'*100 → 200 chars × 0.25 = 50 tokens; the
    # other 2 specs are filler and don't influence the assertion.
    # Fixture B: override ``--tokens-per-char 0.5`` on the same plan →
    # t1 token_estimate = 100.
    def _spec_tokens(sid: str, role: str,
                     task: str, context: str) -> Dict[str, Any]:
        return {"id": sid, "role": role,
                "goal": task, "context": context,
                "output_schema": {"type": "object"},
                "tools": ["file"],
                "model": "opencode-go/deepseek-v4.1-flash"}

    tokens_long_task = "a" * 100
    tokens_long_ctx = "b" * 100
    plan_tokens = {"task_summary": "token-fixture",
                   "specialists": [
                       _spec_tokens("t1", "researcher",
                                    tokens_long_task, tokens_long_ctx),
                       _spec_tokens("t2", "critic",
                                    "short task", "short context"),
                       _spec_tokens("t3", "writer",
                                    "filler task", "filler context")],
                   "batches": [[0, 1, 2]],
                   "dependencies": {}}
    tokens_path = Path("/tmp") / "_f26_tokens.json"
    with open(tokens_path, "w", encoding="utf-8") as _h:
        json.dump(plan_tokens, _h)

    _wipe_logs()
    tokens_default_exit = main([str(tokens_path)])
    if tokens_default_exit != 0:
        failures += 1
        print("SELF_TEST FAIL: token-fixture default exit "
              "(expected 0, got %d)" % tokens_default_exit)
    else:
        # Read the trace; the final record for t1 should carry
        # ``token_estimate == 50``.
        try:
            trace_lines = [json.loads(l) for l in
                           ORCHESTRATION_TRACE.read_text().splitlines()
                           if l.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            trace_lines = []
            failures += 1
            print("SELF_TEST FAIL: token-fixture trace read: %s" % exc)
        else:
            final_t1 = _final_records_by_id(trace_lines).get("t1") or {}
            est = final_t1.get("token_estimate")
            if not isinstance(final_t1, dict) or final_t1.get("id") != "t1":
                failures += 1
                print("SELF_TEST FAIL: token-fixture default — final "
                      "record for t1 missing")
            elif est != 50:
                failures += 1
                print("SELF_TEST FAIL: token-fixture default — "
                      "token_estimate (expected 50, got %s)" % est)
            else:
                print("SELF_TEST PASS: --tokens-per-char default 0.25 → "
                      "token_estimate=50 on t1 (100+100 chars)")

    _wipe_logs()
    tokens_half_exit = main([str(tokens_path), "--tokens-per-char", "0.5"])
    if tokens_half_exit != 0:
        failures += 1
        print("SELF_TEST FAIL: token-fixture override exit "
              "(expected 0, got %d)" % tokens_half_exit)
    else:
        try:
            trace_lines = [json.loads(l) for l in
                           ORCHESTRATION_TRACE.read_text().splitlines()
                           if l.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            trace_lines = []
            failures += 1
            print("SELF_TEST FAIL: token-fixture override trace read: %s"
                  % exc)
        else:
            final_t1 = _final_records_by_id(trace_lines).get("t1") or {}
            est = final_t1.get("token_estimate")
            if est != 100:
                failures += 1
                print("SELF_TEST FAIL: token-fixture override — "
                      "token_estimate (expected 100, got %s)" % est)
            else:
                print("SELF_TEST PASS: --tokens-per-char 0.5 → "
                      "token_estimate=100 on t1 (100+100 chars)")

    if tokens_path.is_file():
        tokens_path.unlink()
    _wipe_logs()

    # ---- F3.3 specialist output cache (additive) ----
    # Fixture HIT: pre-populate the cache via ``cache_key``, dispatch
    # with that namespace, and assert the final record carries
    # ``cached is True`` with output bytes == the planted body. The
    # planted body carries a ``_cache_probe`` marker the stub would
    # never produce, merged over a stub-shaped body with
    # ``output_schema {"type": "object"}`` so the F2.4 check passes.
    # Fixture MISS: a fresh namespace runs normally (no ``cached``
    # flag) and the cache entry file is created for the key.
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        specialist_cache = __import__("specialist_cache")
    except ImportError as exc:
        specialist_cache = None  # type: ignore[assignment]
        failures += 1
        print("SELF_TEST FAIL: cannot import specialist_cache: %s" % exc)
    if specialist_cache is not None:
        hit_ns = "selftest-hit-%d" % os.getpid()
        hit_spec = {"id": "hitme", "role": "researcher",
                    "goal": "g-hitme", "context": "c-hitme",
                    "output_schema": {"type": "object"},
                    "tools": ["file"],
                    "model": "opencode-go/deepseek-v4.1-flash"}
        plan_hit = {"task_summary": "cache-hit",
                    "specialists": [hit_spec],
                    "batches": [[0]],
                    "dependencies": {}}
        hit_key = specialist_cache.cache_key(
            "researcher", "g-hitme", "c-hitme",
            "opencode-go/deepseek-v4.1-flash", {"type": "object"})
        planted_body = {"id": "hitme", "role": "researcher",
                        "task": "g-hitme", "context": "c-hitme",
                        "output_schema": {"type": "object"},
                        "timestamp": "2026-01-01T00:00:00Z",
                        "stub": False, "_cache_probe": "HIT-hitme"}
        planted_bytes = (json.dumps(planted_body, sort_keys=True)
                         + "\n").encode("utf-8")
        if not specialist_cache.store(hit_ns, hit_key, planted_bytes):
            failures += 1
            print("SELF_TEST FAIL: cache HIT fixture could not pre-populate")
        else:
            _wipe_logs()
            SPECIALIST_LOG_DIR.mkdir(parents=True, exist_ok=True)
            hit_exit, hit_trace = dispatch_plan(
                plan_hit, max_parallel=4, fail_mode="continue",
                simulate_n=0, scripts_dir=scripts_dir,
                specialists_dir=SPECIALIST_LOG_DIR,
                max_retries=0, retry_strategy="continue",
                retry_test_overrides=None,
                cache_namespace=hit_ns, cache_ttl_hours=24.0)
            hit_final = _final_records_by_id(hit_trace).get("hitme") or {}
            try:
                with open(hit_final.get("output_path") or "", "rb") as _h:
                    hit_out = _h.read()
            except OSError:
                hit_out = b""
            if hit_exit != 0:
                failures += 1
                print("SELF_TEST FAIL: cache HIT exit "
                      "(expected 0, got %d)" % hit_exit)
            elif hit_final.get("cached") is not True:
                failures += 1
                print("SELF_TEST FAIL: cache HIT record missing cached=True "
                      "(got %s)" % (hit_final,))
            elif hit_out != planted_bytes:
                failures += 1
                print("SELF_TEST FAIL: cache HIT output != planted bytes")
            else:
                print("SELF_TEST PASS: cache HIT "
                      "(cached=True, output == planted marker)")

        miss_ns = "selftest-miss-%d" % os.getpid()
        miss_spec = {"id": "missme", "role": "researcher",
                     "goal": "g-missme", "context": "c-missme",
                     "output_schema": {"type": "object"},
                     "tools": ["file"],
                     "model": "opencode-go/deepseek-v4.1-flash"}
        plan_miss = {"task_summary": "cache-miss",
                     "specialists": [miss_spec],
                     "batches": [[0]],
                     "dependencies": {}}
        _wipe_logs()
        SPECIALIST_LOG_DIR.mkdir(parents=True, exist_ok=True)
        miss_exit, miss_trace = dispatch_plan(
            plan_miss, max_parallel=4, fail_mode="continue",
            simulate_n=0, scripts_dir=scripts_dir,
            specialists_dir=SPECIALIST_LOG_DIR,
            max_retries=0, retry_strategy="continue",
            retry_test_overrides=None,
            cache_namespace=miss_ns, cache_ttl_hours=24.0)
        miss_final = _final_records_by_id(miss_trace).get("missme") or {}
        miss_key = specialist_cache.cache_key(
            "researcher", "g-missme", "c-missme",
            "opencode-go/deepseek-v4.1-flash", {"type": "object"})
        miss_entry = (Path(".cache") / "orchestrator" / miss_ns
                      / ("%s.json" % miss_key))
        try:
            miss_cached_blob = miss_entry.read_bytes()
        except OSError:
            miss_cached_blob = b""
        try:
            with open(miss_final.get("output_path") or "", "rb") as _h:
                miss_out = _h.read()
        except OSError:
            miss_out = b""
        if miss_exit != 0:
            failures += 1
            print("SELF_TEST FAIL: cache MISS exit "
                  "(expected 0, got %d)" % miss_exit)
        elif "cached" in miss_final:
            failures += 1
            print("SELF_TEST FAIL: cache MISS record must NOT carry cached "
                  "(got %s)" % (miss_final,))
        elif not miss_cached_blob or miss_cached_blob != miss_out:
            failures += 1
            print("SELF_TEST FAIL: cache MISS entry not created for the key")
        else:
            print("SELF_TEST PASS: cache MISS "
                  "(normal run, entry stored for the key)")

    # ---- F3.4 streaming progress sink (additive, total 17→19) ----
    # Fixture STREAM: 2-batch plan + temp sink → sink line count ==
    # trace record count, every line parses as JSON with
    # id/batch/status, batch-0 lines precede batch-1 lines.
    stream_sink = Path("/tmp") / ("_f34_progress_%d.jsonl" % os.getpid())
    try:
        if stream_sink.is_file():
            stream_sink.unlink()
        plan_stream = {"task_summary": "stream",
                       "specialists": [_spec_factory("r1", "researcher"),
                                       _spec_factory("cr", "critic"),
                                       _spec_factory("wr", "writer")],
                       "batches": [[0, 1], [2]],
                       "dependencies": {"wr": ["r1"]}}
        _wipe_logs()
        SPECIALIST_LOG_DIR.mkdir(parents=True, exist_ok=True)
        stream_exit, stream_trace = dispatch_plan(
            plan_stream, max_parallel=4, fail_mode="continue",
            simulate_n=0, scripts_dir=scripts_dir,
            specialists_dir=SPECIALIST_LOG_DIR,
            max_retries=0, retry_strategy="continue",
            retry_test_overrides=None,
            progress_path=str(stream_sink))
        try:
            stream_lines = [json.loads(l) for l in
                            stream_sink.read_text().splitlines()
                            if l.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            stream_lines = []
            failures += 1
            print("SELF_TEST FAIL: progress STREAM sink read: %s" % exc)
        else:
            stream_batches = [l.get("batch") for l in stream_lines]
            if stream_exit != 0:
                failures += 1
                print("SELF_TEST FAIL: progress STREAM exit "
                      "(expected 0, got %d)" % stream_exit)
            elif len(stream_lines) != len(stream_trace):
                failures += 1
                print("SELF_TEST FAIL: progress STREAM line count "
                      "(expected %d, got %d)"
                      % (len(stream_trace), len(stream_lines)))
            elif any(not isinstance(l, dict) or "id" not in l
                     or "batch" not in l or "status" not in l
                     for l in stream_lines):
                failures += 1
                print("SELF_TEST FAIL: progress STREAM lines must carry "
                      "id/batch/status")
            elif stream_batches != sorted(stream_batches):
                failures += 1
                print("SELF_TEST FAIL: progress STREAM batch-0 lines must "
                      "precede batch-1 lines (got %s)" % (stream_batches,))
            else:
                print("SELF_TEST PASS: progress STREAM "
                      "(%d lines, batches in order)" % len(stream_lines))
    finally:
        if stream_sink.is_file():
            stream_sink.unlink()
        _wipe_logs()

    # Fixture SILENT: no progress flag → no file created at the
    # would-be path.
    silent_sink = Path("/tmp") / ("_f34_progress_silent_%d.jsonl"
                                  % os.getpid())
    try:
        if silent_sink.is_file():
            silent_sink.unlink()
        plan_silent = {"task_summary": "silent",
                       "specialists": [_spec_factory("r1", "researcher"),
                                       _spec_factory("cr", "critic"),
                                       _spec_factory("wr", "writer")],
                       "batches": [[0, 1], [2]],
                       "dependencies": {"wr": ["r1"]}}
        _wipe_logs()
        SPECIALIST_LOG_DIR.mkdir(parents=True, exist_ok=True)
        silent_exit, _silent_trace = dispatch_plan(
            plan_silent, max_parallel=4, fail_mode="continue",
            simulate_n=0, scripts_dir=scripts_dir,
            specialists_dir=SPECIALIST_LOG_DIR,
            max_retries=0, retry_strategy="continue",
            retry_test_overrides=None)
        if silent_exit != 0:
            failures += 1
            print("SELF_TEST FAIL: progress SILENT exit "
                  "(expected 0, got %d)" % silent_exit)
        elif silent_sink.exists():
            failures += 1
            print("SELF_TEST FAIL: progress SILENT created a sink file "
                  "without the flag")
        else:
            print("SELF_TEST PASS: progress SILENT "
                  "(no flag → no sink file)")
    finally:
        if silent_sink.is_file():
            silent_sink.unlink()
        _wipe_logs()

    # ---- F3.5 async background run mode (additive, total 19→21) ----
    # Fixture BG: spawn a real detached child via _spawn_background
    # with a --progress sink, poll the sink to a 30s datetime deadline,
    # reap with os.waitpid, assert exit 0 + sink lines parse + trace
    # holds real specialist records (tripwire against --self-test
    # recursion — D54's exclusivity plus stripping makes it
    # unreachable, but the trace ids prove the child ran a real
    # dispatch). Every spawned child is reaped in-test — no zombies,
    # no leaked children.
    bg_plan = _self_test_plan()
    bg_plan["task_summary"] = "background"
    bg_plan_path = Path("/tmp") / ("_f35_bg_%d.json" % os.getpid())
    bg_sink = Path("/tmp") / ("_f35_bg_sink_%d.jsonl" % os.getpid())
    try:
        if bg_sink.is_file():
            bg_sink.unlink()
        with open(bg_plan_path, "w", encoding="utf-8") as _h:
            json.dump(bg_plan, _h)
        _wipe_logs()
        bg_argv = [str(bg_plan_path), "--background",
                   "--progress", str(bg_sink)]

        class _BgCap(list):
            def write(self, s: str) -> int:
                self.append(s)
                return len(s)

        cap_b = _BgCap()
        _saved_out = sys.stdout
        sys.stdout = cap_b  # type: ignore[assignment]
        try:
            bg_spawn_ret = _spawn_background(bg_argv)
        finally:
            sys.stdout = _saved_out
        bg_text = "".join(cap_b)
        bg_pid = -1
        bg_log = ""
        for _tok in bg_text.strip().split():
            if _tok.startswith("pid="):
                try:
                    bg_pid = int(_tok[len("pid="):])
                except ValueError:
                    bg_pid = -1
            elif _tok.startswith("log="):
                bg_log = _tok[len("log="):]
        if bg_spawn_ret != 0:
            failures += 1
            print("SELF_TEST FAIL: background BG spawn exit "
                  "(expected 0, got %d)" % bg_spawn_ret)
            bg_pid = -1
        elif bg_pid <= 0:
            failures += 1
            print("SELF_TEST FAIL: background BG handle line unparseable "
                  "(got %r)" % bg_text.strip())
            bg_pid = -1
        else:
            _time_mod = __import__("time")
            _start = datetime.now(timezone.utc)
            _sink_ready = False
            while ((datetime.now(timezone.utc) - _start)
                   .total_seconds() < 30):
                try:
                    _lines = [l for l in bg_sink.read_text().splitlines()
                              if l.strip()]
                except OSError:
                    _lines = []
                if len(_lines) >= 3:
                    _sink_ready = True
                    break
                _time_mod.sleep(0.2)
            _status = 0
            _exited = False
            try:
                if not _sink_ready:
                    try:
                        os.kill(bg_pid, 9)
                    except OSError:
                        pass
                _wpid, _status = os.waitpid(bg_pid, 0)
                _exited = (os.WIFEXITED(_status)
                           and os.WEXITSTATUS(_status) == 0)
            except (OSError, ChildProcessError) as exc:
                failures += 1
                print("SELF_TEST FAIL: background BG waitpid: %s" % exc)
                _exited = False
                _sink_ready = False
            if not _sink_ready:
                failures += 1
                print("SELF_TEST FAIL: background BG sink timeout "
                      "(<3 lines in 30s)")
            elif not _exited:
                failures += 1
                print("SELF_TEST FAIL: background BG child exit "
                      "(expected 0, got status %r)" % (_status,))
            else:
                try:
                    _sink_recs = [json.loads(l) for l in
                                  bg_sink.read_text().splitlines()
                                  if l.strip()]
                except (OSError, json.JSONDecodeError) as exc:
                    _sink_recs = []
                    failures += 1
                    print("SELF_TEST FAIL: background BG sink parse: %s"
                          % exc)
                else:
                    if any(not isinstance(r, dict) or "id" not in r
                           or "batch" not in r or "status" not in r
                           for r in _sink_recs):
                        failures += 1
                        print("SELF_TEST FAIL: background BG sink lines "
                              "must carry id/batch/status")
                    else:
                        try:
                            _trace_recs = [json.loads(l) for l in
                                           ORCHESTRATION_TRACE.read_text()
                                           .splitlines() if l.strip()]
                        except (OSError, json.JSONDecodeError) as exc:
                            _trace_recs = []
                            failures += 1
                            print("SELF_TEST FAIL: background BG trace "
                                  "read: %s" % exc)
                        else:
                            _ids = {r.get("id") for r in _trace_recs
                                    if isinstance(r, dict)}
                            if _ids != {"r1", "wr", "cr"}:
                                failures += 1
                                print("SELF_TEST FAIL: background BG trace "
                                      "ids (expected {'r1','wr','cr'}, got "
                                      "%s)" % (_ids,))
                            elif (not bg_log
                                  or not Path(bg_log).is_file()):
                                failures += 1
                                print("SELF_TEST FAIL: background BG log "
                                      "file missing (log=%r)" % (bg_log,))
                            else:
                                print("SELF_TEST PASS: background BG "
                                      "(detached child exit 0, sink + "
                                      "trace + log present)")
    finally:
        try:
            if bg_plan_path.is_file():
                bg_plan_path.unlink()
        except OSError:
            pass
        try:
            if bg_sink.is_file():
                bg_sink.unlink()
        except OSError:
            pass
        _wipe_logs()

    # Fixture BG_EXCLUSIVE: --background is mutually exclusive with
    # --dry-run and --self-test (D54 — exit 2 + error:, never silently
    # ignored).
    excl_plan = _self_test_plan()
    excl_plan["task_summary"] = "background-exclusive"
    excl_path = Path("/tmp") / ("_f35_excl_%d.json" % os.getpid())
    try:
        with open(excl_path, "w", encoding="utf-8") as _h:
            json.dump(excl_plan, _h)
        _wipe_logs()
        excl_dry = main([str(excl_path), "--background", "--dry-run"])
        excl_dry_logs = Path("logs").is_dir()
        _wipe_logs()
        excl_self = main(["--self-test", "--background"])
        if excl_dry != 2:
            failures += 1
            print("SELF_TEST FAIL: background EXCLUSIVE --dry-run exit "
                  "(expected 2, got %d)" % excl_dry)
        elif excl_dry_logs:
            failures += 1
            print("SELF_TEST FAIL: background EXCLUSIVE --dry-run created "
                  "logs/ (should NOT)")
        elif excl_self != 2:
            failures += 1
            print("SELF_TEST FAIL: background EXCLUSIVE --self-test exit "
                  "(expected 2, got %d)" % excl_self)
        else:
            print("SELF_TEST PASS: background EXCLUSIVE (--background + "
                  "--dry-run/--self-test → exit 2)")
    finally:
        try:
            if excl_path.is_file():
                excl_path.unlink()
        except OSError:
            pass
        _wipe_logs()

    total = 21
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
    parser.add_argument("--orchestrator-model", default=None,
                        help="F2.5: option-tier orchestrator model override. "
                             "Bypasses env / config / default for the "
                             "orchestrator only (subagent unaffected, F2.2).")
    parser.add_argument("--subagent-model", default=None,
                        help="F2.5: option-tier subagent model override. "
                             "Bypasses env / config / default for the "
                             "subagent only (orchestrator unaffected, F2.2).")
    parser.add_argument("--max-cost", type=float, default=0.0,
                        help="F2.5: maximum estimated cost in USD. 0 (default) "
                             "disables the budget abort. The estimate is "
                             "specialists × --cost-per-call; if it exceeds "
                             "--max-cost the dispatcher aborts with "
                             "PLAN_OVER_BUDGET and exits 2 (or, with --dry-run, "
                             "prints the estimate and exits 2).")
    parser.add_argument("--cost-per-call", type=float, default=0.01,
                        help="F2.5: estimated USD cost per specialist call "
                             "(default 0.01). Multiplied by specialists to "
                             "produce the --max-cost estimate.")
    parser.add_argument("--no-schema-check", action="store_true", default=False,
                        help="F2.4: skip the post-dispatch per-specialist "
                             "output_schema content check. Order-independent "
                             "with the other flags. By default the check "
                             "is ON (a schema mismatch marks the specialist "
                             "status: failed with an output_schema mismatch "
                             "error).")
    parser.add_argument("--tokens-per-char", type=float, default=0.25,
                        help="F2.6: tokens-per-character factor for the "
                             "per-call token estimate (default 0.25 ≈ 4 "
                             "chars/token). estimate = max(1, int("
                             "(len(task) + len(context)) * factor)). "
                             "Set to 0 to disable (the trace still carries "
                             "token_estimate=0). Order-independent with the "
                             "other flags.")
    parser.add_argument("--cache-namespace", default="",
                        help="F3.3: cache namespace enabling the specialist "
                             "output cache (default '' = disabled: no cache "
                             "import, no .cache/ writes, no cached stamps). "
                             "Must match ^[A-Za-z0-9][A-Za-z0-9_-]*$ "
                             "(max 64 chars) or dispatch aborts with exit 2.")
    parser.add_argument("--cache-ttl-hours", type=float, default=24.0,
                        help="F3.3: cache entry TTL in hours (default 24.0). "
                             "Entries older than this are re-run + re-stored. "
                             "<= 0 means entries never expire. "
                             "Order-independent with the other flags.")
    parser.add_argument("--progress", default=None,
                        help="F3.4: streaming progress sink — append one JSON "
                             "line per completed specialist record per batch "
                             "to PATH (local append-only JSONL, parents "
                             "auto-created; default None = disabled). "
                             "Best-effort: IO failures warn only, never fail "
                             "the run. Never touched by --dry-run. "
                             "Order-independent with the other flags.")
    parser.add_argument("--background", action="store_true", default=False,
                        help="F3.5: detach — re-exec self detached "
                             "(POSIX-only start_new_session, stdio to "
                             "logs/background-<UTC-ts>.log), print "
                             "'background: pid=<n> log=<path> trace=<path>' "
                             "and exit 0. Only a dispatchable plan detaches "
                             "(validation + cost check run first in the "
                             "foreground). Mutually exclusive with --dry-run "
                             "and --self-test. Pair with --progress for "
                             "polling. Order-independent with the other "
                             "flags.")
    parser.add_argument("--self-test", action="store_true",
                        help="Run built-in end-to-end fixture")
    args = parser.parse_args(argv)

    # F3.5 D54: --background is mutually exclusive with --self-test —
    # reject explicitly (exit 2 + error:) rather than silently ignoring.
    # The self-test branch runs first, so this check precedes it.
    if args.self_test and args.background:
        print("error: --background cannot be combined with --self-test",
              file=sys.stderr)
        return 2

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
    if args.cost_per_call < 0:
        print("error: --cost-per-call must be >= 0 (got %r)"
              % args.cost_per_call, file=sys.stderr)
        return 2
    if args.max_cost < 0:
        print("error: --max-cost must be >= 0 (got %r)" % args.max_cost,
              file=sys.stderr)
        return 2
    if args.tokens_per_char < 0:
        print("error: --tokens-per-char must be >= 0 (got %r)"
              % args.tokens_per_char, file=sys.stderr)
        return 2
    # F3.5 D54: --background is mutually exclusive with --dry-run —
    # backgrounding a no-dispatch preview is meaningless (exit 2 +
    # error:, never silently ignored). Checked with the other flag
    # validation so no plan load, no detach, no logs/ either way.
    if args.background and args.dry_run:
        print("error: --background cannot be combined with --dry-run",
              file=sys.stderr)
        return 2
    # F3.3: invalid namespace is a usage error (F1.5 contract) — error:
    # + exit 2 before any plan load or dispatch. The cache module is
    # imported lazily so the disabled path (namespace '') never imports
    # it (D46).
    if args.cache_namespace:
        scripts_dir_ns = str(Path(__file__).resolve().parent)
        if scripts_dir_ns not in sys.path:
            sys.path.insert(0, scripts_dir_ns)
        try:
            _cache_mod = __import__("specialist_cache")
            _cache_mod.check_namespace(args.cache_namespace)
        except ValueError as exc:
            print("error: --cache-namespace invalid: %s" % exc,
                  file=sys.stderr)
            return 2
        except ImportError as exc:
            print("error: cannot import specialist_cache from %s: %s"
                  % (scripts_dir_ns, exc), file=sys.stderr)
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

    # F2.5 cost-budget check — runs AFTER plan validation, BEFORE any
    # dispatch (or dry-run print). Plan invalid → exit 1 (already
    # handled above); cost over budget → exit 2. ``--max-cost 0`` (the
    # D34 default) disables the check entirely. ``--dry-run`` honors the
    # same check: a budget-breach on the preview path prints the
    # PLAN_OVER_BUDGET line to stderr and exits 2 — no logs/ is created.
    specialists = plan.get("specialists") if isinstance(plan, dict) else None
    n_spec = len(specialists) if isinstance(specialists, list) else 0
    cost_estimate = n_spec * args.cost_per_call
    if args.max_cost > 0 and cost_estimate > args.max_cost:
        print("PLAN_OVER_BUDGET: estimated $%.2f > max $%.2f"
              % (cost_estimate, args.max_cost), file=sys.stderr)
        return 2

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
        resolved = {"orchestrator": plan_preview.resolve_model(
                        "orchestrator",
                        orchestrator=args.orchestrator_model),
                    "subagent": plan_preview.resolve_model(
                        "subagent",
                        subagent=args.subagent_model)}
        preview_warnings = plan_preview._compute_warnings(
            plan, warnings, resolved["subagent"][0])
        plan_preview.print_preview(
            plan, args.plan, resolved, preview_warnings,
            cost_per_call=args.cost_per_call,
            max_cost=args.max_cost)
        return 0

    # F3.5 (D53): detach AFTER validation + cost-budget check + dry-run
    # handling, BEFORE dispatch — an invalid/over-budget plan fails in
    # the FOREGROUND (fast, visible errors); only a dispatchable plan
    # detaches. The child is a normal foreground dispatch (one code
    # path); the parent prints the handle line and exits 0.
    if args.background:
        raw = argv if argv is not None else sys.argv[1:]
        return _spawn_background(list(raw))

    scripts_dir = Path(__file__).resolve().parent
    specialists_dir = SPECIALIST_LOG_DIR
    assert plan is not None  # _load_plan returned err=None above
    exit_code, _ = dispatch_plan(
        plan, max_parallel=args.max_parallel,
        fail_mode=fail_mode, simulate_n=simulate_n,
        scripts_dir=scripts_dir, specialists_dir=specialists_dir,
        max_retries=args.max_retries,
        retry_strategy=args.retry_strategy,
        schema_check=not args.no_schema_check,
        tokens_per_char=args.tokens_per_char,
        cache_namespace=args.cache_namespace,
        cache_ttl_hours=args.cache_ttl_hours,
        progress_path=args.progress)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
