"""Specialist output cache for the multi-agent-orchestrator skill (F3.3).

Stdlib-only library: stores specialist outputs keyed by content hash
under ``.cache/orchestrator/<namespace>/`` with a TTL (default 24h).
The dispatcher consults the cache in ``_run_one`` before spawning the
stub run and stamps ``cached: True`` on the trace record for hits.

Key recipe (D45): sha256 hex of ``json.dumps(canonical, sort_keys=True)``
where canonical is the 5-field dict ``{role, goal, context, model,
output_schema}`` (``default=str`` fallback for non-serializable schema
values).

Layout (D46): ``.cache/orchestrator/<namespace>/<key>.json`` relative
to the cwd (repo root at runtime). Empty namespace disables all cache
paths — callers must not import or touch this module when disabled.

Durability (D47): the cache NEVER breaks a run. ``lookup`` misses
(returns None) on absent / expired / corrupt / unreadable entries and
never raises; ``store`` returns False (never raises) on IO failure.
``ttl_hours <= 0`` means entries never expire.

Usage:
    python3 scripts/specialist_cache.py --self-test
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
NAMESPACE_MAX_LEN = 64

KEY_RE = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_BASE_DIR = Path(".cache") / "orchestrator"

# Per-call uniqueness counter for the mtime probe (GIL-atomic append;
# best-effort uniqueness — worst case is the same D47-safe miss).
_PROBE_SEQ: list = []


def check_namespace(namespace: str) -> str:
    """Validate a cache namespace, returning it unchanged.

    Raises ``ValueError`` on violators (the dispatcher maps this to
    exit 2). Rule: ``^[A-Za-z0-9][A-Za-z0-9_-]*$``, max 64 chars.
    """
    if (not isinstance(namespace, str)
            or len(namespace) > NAMESPACE_MAX_LEN
            or not NAMESPACE_RE.match(namespace)):
        raise ValueError(
            "invalid cache namespace %r: must match %s (max %d chars)"
            % (namespace, NAMESPACE_RE.pattern, NAMESPACE_MAX_LEN))
    return namespace


def cache_key(role: Any, goal: Any, context: Any, model: Any,
              output_schema: Any) -> str:
    """Content-hash key (D45): sha256 hex over the canonical 5-field dict."""
    canonical: Dict[str, Any] = {
        "role": role,
        "goal": goal,
        "context": context,
        "model": model,
        "output_schema": output_schema,
    }
    raw = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _base_dir(base: Any = None) -> Path:
    """Resolve the cache base dir (``base`` override exists for tests)."""
    if base is None:
        return DEFAULT_BASE_DIR
    return Path(base)


def _entry_path(namespace: str, key: str, base: Any = None) -> Path:
    """Full path for one entry: ``<base>/<namespace>/<key>.json``."""
    return _base_dir(base) / namespace / ("%s.json" % key)


def _now_via_probe(base_dir: Path) -> Optional[float]:
    """Wall-clock ``now`` as a fresh file's mtime (no clock import).

    ``specialist_cache`` is limited to its locked stdlib set, so instead
    of ``time.time()`` we touch a per-call-unique probe file and read
    back its mtime. Uniqueness comes from pid + id() of a live
    per-call token + a GIL-atomic append counter, so concurrent
    lookups in one namespace never share a probe path (a shared
    pid-suffixed name raced on touch/stat/unlink across threads).
    Returns None when the probe cannot be created.
    """
    token = object()  # kept alive for the whole call: id() is unique
    _PROBE_SEQ.append(token)
    seq = len(_PROBE_SEQ)
    try:
        probe = base_dir / (".probe-%d-%d-%d"
                            % (os.getpid(), id(token), seq))
        probe.touch(exist_ok=True)
        now = probe.stat().st_mtime
        try:
            probe.unlink()
        except OSError:
            pass
        return now
    except OSError:
        return None


def lookup(namespace: str, key: str, ttl_hours: float,
           base: Any = None) -> Optional[bytes]:
    """Return cached payload bytes on hit, None on any miss. Never raises.

    Miss reasons: absent entry, expired entry (age from file mtime,
    expired iff age > ttl_hours*3600; ttl_hours <= 0 = immortal),
    corrupt (non-JSON bytes) or unreadable file, invalid namespace/key.
    """
    try:
        check_namespace(namespace)
        if not isinstance(key, str) or not KEY_RE.match(key):
            return None
        path = _entry_path(namespace, key, base)
        try:
            entry_mtime = path.stat().st_mtime
        except OSError:
            return None
        if ttl_hours is not None and ttl_hours > 0:
            now = _now_via_probe(path.parent)
            if now is None:
                return None
            if (now - entry_mtime) > ttl_hours * 3600.0:
                return None
        try:
            blob = path.read_bytes()
        except OSError:
            return None
        try:
            json.loads(blob.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return blob
    except Exception:
        return None


def store(namespace: str, key: str, payload: bytes,
          base: Any = None) -> bool:
    """Persist payload bytes; True on success, False (never raise) on failure."""
    try:
        check_namespace(namespace)
        if not isinstance(key, str) or not KEY_RE.match(key):
            return False
        if not isinstance(payload, (bytes, bytearray)):
            return False
        path = _entry_path(namespace, key, base)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(payload))
        return True
    except Exception:
        return False


def _rmtree(root: Path) -> None:
    """stdlib-only ``rm -rf`` for the self-test scratch dir."""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            try:
                os.unlink(os.path.join(dirpath, name))
            except OSError:
                pass
        for name in dirnames:
            try:
                os.rmdir(os.path.join(dirpath, name))
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass


def run_self_test() -> bool:
    """--self-test: 5 fixtures (roundtrip, isolation, expiry, corrupt, key)."""
    failures = 0
    scratch = (Path(os.environ.get("TMPDIR", "/tmp"))
               / ("mao_cache_selftest_%d" % os.getpid()))
    _rmtree(scratch)
    try:
        scratch.mkdir(parents=True, exist_ok=True)

        # 1. roundtrip: store + lookup returns byte-identical payload.
        key_rt = cache_key("researcher", "goal-rt", "ctx-rt",
                           "m", {"type": "object"})
        blob_rt = b'{"hello": "roundtrip"}'
        ok_store = store("nsrt", key_rt, blob_rt, base=scratch)
        got_rt = lookup("nsrt", key_rt, 24.0, base=scratch)
        if ok_store and got_rt == blob_rt:
            print("SELF_TEST PASS: roundtrip "
                  "(store + lookup byte-identical)")
        else:
            failures += 1
            print("SELF_TEST FAIL: roundtrip (stored=%s, got=%s)"
                  % (ok_store, got_rt))

        # 2. namespace isolation: same key, per-namespace payloads + miss.
        key_iso = cache_key("writer", "g", "c", "m", {"type": "object"})
        store("ns-a", key_iso, b'{"v": "a"}', base=scratch)
        store("ns-b", key_iso, b'{"v": "b"}', base=scratch)
        got_a = lookup("ns-a", key_iso, 24.0, base=scratch)
        got_b = lookup("ns-b", key_iso, 24.0, base=scratch)
        got_c = lookup("ns-c", key_iso, 24.0, base=scratch)
        if (got_a == b'{"v": "a"}' and got_b == b'{"v": "b"}'
                and got_c is None):
            print("SELF_TEST PASS: namespace isolation "
                  "(same key, per-namespace payloads)")
        else:
            failures += 1
            print("SELF_TEST FAIL: namespace isolation "
                  "(a=%s, b=%s, c=%s)" % (got_a, got_b, got_c))

        # 3. expiry: backdated mtime (via os.utime) misses at 24h TTL
        # but hits when immortal (ttl_hours <= 0).
        key_ex = cache_key("critic", "g", "c", "m", {"type": "object"})
        blob_ex = b'{"v": "expiry"}'
        store("nsex", key_ex, blob_ex, base=scratch)
        path_ex = scratch / "nsex" / ("%s.json" % key_ex)
        aged = path_ex.stat()
        backdate = 25 * 3600.0
        os.utime(path_ex, (aged.st_atime - backdate,
                           aged.st_mtime - backdate))
        got_old = lookup("nsex", key_ex, 24.0, base=scratch)
        got_immortal = lookup("nsex", key_ex, 0.0, base=scratch)
        if got_old is None and got_immortal == blob_ex:
            print("SELF_TEST PASS: expiry "
                  "(backdated entry misses, ttl<=0 immortal)")
        else:
            failures += 1
            print("SELF_TEST FAIL: expiry "
                  "(expired=%s, immortal=%s)" % (got_old, got_immortal))

        # 4. corrupt file: miss, no crash.
        key_co = cache_key("researcher", "g", "c", "m", {"type": "object"})
        path_co = scratch / "nsco" / ("%s.json" % key_co)
        path_co.parent.mkdir(parents=True, exist_ok=True)
        path_co.write_bytes(b"not-json{{{")
        try:
            got_co = lookup("nsco", key_co, 24.0, base=scratch)
        except Exception as exc:  # noqa: BLE001
            got_co = "RAISED:%s" % exc
        if got_co is None:
            print("SELF_TEST PASS: corrupt file misses without raising")
        else:
            failures += 1
            print("SELF_TEST FAIL: corrupt file (got %s)" % (got_co,))

        # 5. key stability + sensitivity: same inputs same key, any
        # input change (context, schema) a different key.
        key_1 = cache_key("r", "goal", "ctx", "model", {"type": "object"})
        key_2 = cache_key("r", "goal", "ctx", "model", {"type": "object"})
        key_3 = cache_key("r", "goal", "ctx-CHANGED", "model",
                          {"type": "object"})
        key_4 = cache_key("r", "goal", "ctx", "model", {"type": "string"})
        if (key_1 == key_2 and len(key_1) == 64
                and key_3 != key_1 and key_4 != key_1):
            print("SELF_TEST PASS: key stability + sensitivity "
                  "(same inputs same key, changed input different key)")
        else:
            failures += 1
            print("SELF_TEST FAIL: key stability/sensitivity")
    finally:
        _rmtree(scratch)

    total = 5
    print("SELF_TEST: %d/%d passed" % (total - failures, total))
    return failures == 0


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description="Specialist output cache (F3.3) — library; "
                    "--self-test runs the built-in fixtures.")
    parser.add_argument("--self-test", action="store_true",
                        help="Run built-in fixtures (5/5 expected)")
    args = parser.parse_args(argv)
    if args.self_test:
        return 0 if run_self_test() else 1
    parser.print_usage(sys.stderr)
    print("error: specialist_cache.py is a library; pass --self-test",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
