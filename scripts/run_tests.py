#!/usr/bin/env python3
"""Phase 1 test driver — runs every existing per-script ``--self-test`` and
reports one combined pass/fail so the user has a single command to verify
the whole Phase 1 surface.

Stdlib-only. Per-spec:
  * Locked order: validate_plan.py, dispatch.py, write_log.py.
  * Locked expected counts: 15, 7, 7 (hard-coded — the driver is a
    tripwire, not a discovery mechanism).
  * Match strategy: grep per-script stdout for the exact pattern
    ``<N>/<N> passed``. If the pattern is missing, that suite is red
    even if the subprocess returned 0 (per D18 in the spec).
  * Capture both stdout AND stderr so a per-script crash to stderr is
    classified as red.

CLI:
  python3 scripts/run_tests.py            # run all three; exit 0/1
  python3 scripts/run_tests.py --self-test # run validate_plan only
                                          # (smoke check the driver)

Exit codes: 0 = all green, 1 = any red, 2 = usage/IO.
"""

import argparse
import subprocess
import sys


# Generous bound: the per-script self-tests are deterministic and finish
# in well under a second on real hardware. 60s prevents a hang from
# blocking the driver forever.
PER_SCRIPT_TIMEOUT_SEC = 60.0


# Locked by F1.10 spec §"Hard-coded expectations" + D19 + F2.4
# (validate_plan gained an E3 sub-rule fixture; dispatch gained a
# post-dispatch schema-check fixture). DO NOT reorder or recompute; the
# order + counts are the contract.
SUITES = (
    ("validate_plan.py", 16),
    ("dispatch.py",       8),
    ("write_log.py",      7),
)

SCRIPTS_DIR = "scripts"

EXPECTED_TOTAL = sum(n for _, n in SUITES)  # 31


def _run_one(script_name: str, expected: int):
    """Run ``scripts/<script_name> --self-test`` and classify: True = green,
    False = red. Captures stdout AND stderr (per D18 / F1.4 C1 lesson:
    a per-script crash to stderr must be classified as red even if the
    subprocess somehow returned 0)."""
    cmd = [sys.executable, f"{SCRIPTS_DIR}/{script_name}", "--self-test"]
    pattern = f"{expected}/{expected} passed"

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PER_SCRIPT_TIMEOUT_SEC)
    except FileNotFoundError as e:
        # Missing interpreter or script — surface as IO.
        print(f"{script_name}: IO error launching self-test: {e}", file=sys.stderr)
        return False, "", str(e)
    except subprocess.TimeoutExpired:
        # Per-script self-test hung past the safety bound. Treat as red
        # and surface the timeout to the user.
        msg = f"timeout after {PER_SCRIPT_TIMEOUT_SEC:.0f}s"
        print(f"{script_name}: {msg}", file=sys.stderr)
        return False, "", msg

    captured = (proc.stdout or "") + (proc.stderr or "")
    if pattern in captured:
        return True, captured, ""
    return False, captured, ""


def _print_table(rows):
    """Print the locked markdown table (column order: suite | expected |
    passed | status)."""
    print()
    print("| suite              | expected | passed | status |")
    print("|--------------------|----------|--------|--------|")
    for script, expected, passed, status in rows:
        print(f"| {script:<18} | {expected:<8} | {passed:<6} | {status:<6} |")
    print()


def run_all():
    """Run every locked suite, print the table + TOTAL + STATUS, return
    the process exit code (0/1)."""
    rows = []
    total_passed = 0
    all_green = True

    for script, expected in SUITES:
        green, captured, err = _run_one(script, expected)
        # If green, the locked pattern was found; report the expected
        # count as the passed count (the driver is a tripwire on that
        # exact N/N string — see D18). If red, surface 0/expected so the
        # user sees how far the suite got.
        passed = expected if green else 0
        status = "green" if green else "red"
        rows.append((script, expected, passed, status))
        total_passed += passed
        if not green:
            all_green = False
            # Dump captured output on failure so the user can see why
            # (per "STATUS: red" gotcha: still print all three rows, but
            # surface the failing suite's output for debugging).
            if captured.strip():
                print(f"--- {script} output ---", file=sys.stderr)
                print(captured.rstrip(), file=sys.stderr)
                print(f"--- end {script} ---", file=sys.stderr)

    _print_table(rows)
    print(f"TOTAL: {total_passed}/{EXPECTED_TOTAL} passed")
    print(f"STATUS: {'green' if all_green else 'red'}")
    return 0 if all_green else 1


def run_self_test():
    """Smoke check: find + invoke validate_plan --self-test. Per spec:
    exit 0 if it was found and ran, 1 if it wasn't, 2 on IO."""
    script, expected = SUITES[0]  # validate_plan.py
    cmd = [sys.executable, f"{SCRIPTS_DIR}/{script}", "--self-test"]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PER_SCRIPT_TIMEOUT_SEC)
    except FileNotFoundError as e:
        print(f"run_tests --self-test: IO error: {e}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print(f"run_tests --self-test: timeout after {PER_SCRIPT_TIMEOUT_SEC:.0f}s",
              file=sys.stderr)
        return 2

    captured = (proc.stdout or "") + (proc.stderr or "")
    pattern = f"{expected}/{expected} passed"

    if pattern in captured:
        print(f"run_tests --self-test: {script} {expected}/{expected} passed")
        return 0

    # Couldn't find/confirm the expected line. If the subprocess returned
    # non-zero, it's a hard failure; classify as 1 per spec ("couldn't
    # find and run"). If it returned 0 but the locked pattern is missing,
    # that's a contract violation — also 1.
    print(f"run_tests --self-test: {script} did not report '{pattern}'",
          file=sys.stderr)
    if captured.strip():
        print(captured.rstrip(), file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(
        description="Phase 1 test driver — runs per-script --self-tests.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Smoke check: run validate_plan --self-test only.",
    )
    args = parser.parse_args()

    try:
        if args.self_test:
            return run_self_test()
        return run_all()
    except (OSError, IOError) as e:
        # 2 = usage/IO per spec.
        print(f"run_tests: IO error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())