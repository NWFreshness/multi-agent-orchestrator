"""Single-specialist stub runner for the multi-agent-orchestrator skill (F1.5).

Stdlib-only CLI: takes a specialist id, role, and JSON payload on argv,
validates the payload shape, and writes a deterministic output file to
``logs/specialists/<id>.<n>.json``. NO LLM call — the stub only writes a
stub marker so the dispatcher surface can be exercised end-to-end without
the real ``delegate_task`` integration (F1.6+).

Usage:
    python3 scripts/run_specialist.py <specialist_id> <role> <json_payload>

Where ``json_payload`` is a JSON object with keys ``task`` (str),
``context`` (str), ``output_schema`` (object). The dispatcher shells out
once per specialist, so payload round-tripping through argv is intentional
(it preserves the process boundary a real LLM runner would need).

Exit codes: 0 = success, 1 = bad input, 2 = IO error.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple

# Path-safe specialist id charset: alphanumerics plus `.`, `_`, `-`. The
# set is built without importing `string` to keep the AST stdlib-import
# list narrow (controller scan only allows a small stdlib set).
_VALID_ID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789._-")

# Per spec: logs/specialists/<id>.<n>.json, where <n> = max(existing
# <id>.\d+ in dir) + 1. F1.7 re-runs don't clobber prior outputs.
SPECIALIST_LOG_DIR = Path("logs") / "specialists"


def _utcnow_iso() -> str:
    """ISO-8601 UTC with explicit 'Z' suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_seq(specialists_dir: Path, specialist_id: str) -> int:
    """Highest existing <id>.<n>.json + 1."""
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


def _validate_payload(payload: Any) -> Tuple[bool, Any]:
    """Returns (ok, parsed_or_msg). ok=True → parsed_or_msg is the payload dict."""
    if not isinstance(payload, dict):
        return (False, "payload must be a JSON object, got %s"
                % type(payload).__name__)
    task = payload.get("task")
    if not isinstance(task, str) or not task:
        return (False, "payload.task must be a non-empty string")
    context = payload.get("context")
    if not isinstance(context, str) or not context:
        return (False, "payload.context must be a non-empty string")
    schema = payload.get("output_schema")
    if not isinstance(schema, dict):
        return (False, "payload.output_schema must be an object")
    return (True, payload)


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) != 3:
        print("usage: run_specialist.py <specialist_id> <role> <json_payload>",
              file=sys.stderr)
        return 1

    specialist_id, role, raw_payload = argv

    if not specialist_id or any(c not in _VALID_ID_CHARS for c in specialist_id):
        print("error: specialist_id must be non-empty and path-safe "
              "(alnum / . _ -), got %r" % specialist_id, file=sys.stderr)
        return 1
    if not role or not isinstance(role, str):
        print("error: role must be a non-empty string, got %r" % role,
              file=sys.stderr)
        return 1

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        print("error: malformed JSON payload: %s" % exc, file=sys.stderr)
        return 1

    ok, parsed_or_msg = _validate_payload(payload)
    if not ok:
        print("error: %s" % parsed_or_msg, file=sys.stderr)
        return 1
    payload = parsed_or_msg

    try:
        SPECIALIST_LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print("error: cannot create %s: %s" % (SPECIALIST_LOG_DIR, exc),
              file=sys.stderr)
        return 2

    seq = _next_seq(SPECIALIST_LOG_DIR, specialist_id)
    output_path = SPECIALIST_LOG_DIR / ("%s.%d.json" % (specialist_id, seq))

    record = {
        "id": specialist_id,
        "role": role,
        "task": payload["task"],
        "context": payload["context"],
        "output_schema": payload["output_schema"],
        "timestamp": _utcnow_iso(),
        "stub": True,
    }

    try:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        print("error: cannot write %s: %s" % (output_path, exc),
              file=sys.stderr)
        return 2

    print("%s: ok" % specialist_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())