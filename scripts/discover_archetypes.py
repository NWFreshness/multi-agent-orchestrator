"""Custom archetype discovery for the multi-agent-orchestrator skill (F3.2).

Lists and validates user-defined specialist archetypes from
``~/.hermes/agents/*.yaml`` so the Orchestrator can offer them alongside
the F1.3 static catalog (``references/archetypes.md``).

D41 (controller-locked): directory resolution is
``--agents-dir`` > ``HERMES_AGENTS_DIR`` env > ``~/.hermes/agents``
default. An empty-string env value counts as unset. ``~`` is resolved
via ``pathlib.Path.home()`` — never shell-expanded, never auto-created.

D42: files use the flat ``key: value`` subset only (F2.5 precedent —
stdlib-only, no PyYAML). One archetype per file. Required keys: ``name``,
``purpose``. Optional: ``tools`` (comma-separated on one line, default
``file``), ``model`` (default ``""`` = Phase 2 resolved subagent model).
``#`` comments + blank lines allowed; nesting, lists, and multi-doc
markers are silently ignored.

D43: malformed files (missing ``name``/``purpose``) are SKIPPED with a
``WARNING:`` line to stderr — never fatal. A missing agents dir is the
common first-run state: exit 0 with ``no custom archetypes``.

D44: the validator is untouched — custom roles already dispatch via the
F1.4 E4 warning path. This script only discovers.

Usage:
    python3 scripts/discover_archetypes.py [--agents-dir PATH] [--json]
    python3 scripts/discover_archetypes.py --self-test

Exit codes: 0 = success (even zero archetypes), 1 = --self-test failure,
2 = usage/IO (unreadable dir that EXISTS, i.e. permission).
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_TOOLS = ["file"]

RECOGNIZED_KEYS = ("name", "purpose", "tools", "model")


def resolve_agents_dir(cli_value: Optional[str] = None) -> Path:
    """D41 chain: ``--agents-dir`` > ``HERMES_AGENTS_DIR`` > default.

    Empty-string env counts as unset. The ``~`` default resolves via
    ``pathlib.Path.home()`` (no shell expansion).
    """
    if cli_value:
        return Path(cli_value)
    env_value = os.environ.get("HERMES_AGENTS_DIR", "")
    if env_value and env_value.strip():
        return Path(env_value)
    return Path.home() / ".hermes" / "agents"


def _parse_flat_yaml(path: Path) -> Dict[str, str]:
    """Read the flat ``key: value`` subset from one archetype file (D42).

    ``#`` comments and blank lines are skipped; inline `` #`` comments
    are stripped (F2.5 ``_config_value`` precedent). Indented (nested)
    lines, list items, and multi-doc markers are silently ignored —
    only top-level ``key: value`` lines populate the result.
    """
    values: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("---") or stripped.startswith("..."):
                continue
            if stripped.startswith("-"):
                continue
            if line[:1] in (" ", "\t"):
                continue  # nested content — not part of the flat subset
            hash_at = stripped.find(" #")
            if hash_at >= 0:
                stripped = stripped[:hash_at].rstrip()
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if key in RECOGNIZED_KEYS and key not in values:
                values[key] = value
    return values


def _split_tools(raw: str) -> List[str]:
    """Comma-split the ``tools`` line, dropping empties (D42)."""
    return [piece.strip() for piece in raw.split(",") if piece.strip()]


def discover(agents_dir: Path) -> Tuple[List[Dict[str, object]], List[str]]:
    """Read ``*.yaml`` under ``agents_dir``.

    Returns ``(archetypes, warnings)`` with archetypes sorted by name.
    Each archetype is ``{"name", "purpose", "tools" (list), "model",
    "source" (filename)}``. Malformed files contribute a warning, never
    an exception — one bad file must not hide the good ones (D43).
    """
    archetypes: List[Dict[str, object]] = []
    warnings: List[str] = []
    for path in sorted(agents_dir.glob("*.yaml")):
        try:
            values = _parse_flat_yaml(path)
        except OSError as exc:
            warnings.append("WARNING: skipping %s: unreadable (%s)"
                            % (path.name, exc))
            continue
        missing = [k for k in ("name", "purpose") if not values.get(k)]
        if missing:
            warnings.append("WARNING: skipping %s: missing required %s"
                            % (path.name, "/".join(missing)))
            continue
        name = values["name"]
        if path.stem != name:
            warnings.append("WARNING: %s: filename stem %r does not match "
                            "name %r (name field wins)" % (path.name,
                                                           path.stem, name))
        tools = _split_tools(values["tools"]) if values.get("tools") \
            else list(DEFAULT_TOOLS)
        archetypes.append({
            "name": name,
            "purpose": values["purpose"],
            "tools": tools,
            "model": values.get("model", ""),
            "source": path.name,
        })
    archetypes.sort(key=lambda item: str(item["name"]))
    return archetypes, warnings


def format_line(archetype: Dict[str, object]) -> str:
    """One ``name | purpose | tools | model | source-file`` line."""
    tools = archetype["tools"]
    tools_str = ", ".join(tools) if isinstance(tools, list) else str(tools)
    return "%s | %s | %s | %s | %s" % (
        archetype["name"], archetype["purpose"], tools_str,
        archetype["model"], archetype["source"])


def _self_test() -> bool:
    """--self-test: 5 fixtures in a temp dir under /tmp (D41–D43).

    Fixtures 1–3 and 5 drive ``main([...])`` with the ``--agents-dir``
    argv form; fixture 4 sets ``HERMES_AGENTS_DIR`` with save/restore
    in ``finally``. ``tempfile`` is outside the locked import set, so
    the dir is built with ``os``/``pathlib`` directly. Capture helpers
    come via ``__import__`` to keep top-level imports in the locked set
    (F2.1 D24 precedent).
    """
    contextlib = __import__("contextlib")
    io = __import__("io")

    failures = 0
    tmp_root = Path("/tmp") / ("_f32_agents_%d" % os.getpid())
    if tmp_root.exists():
        for entry in tmp_root.rglob("*"):
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
        for entry in sorted(tmp_root.glob("**/*"), reverse=True):
            if entry.is_dir():
                entry.rmdir()
        tmp_root.rmdir()
    tmp_root.mkdir(parents=True, exist_ok=True)

    def run_main(args: List[str]) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    try:
        # Fixture 1: valid file → listed with tools split + model passthrough.
        (tmp_root / "arxiv-fetcher.yaml").write_text(
            "name: arxiv-fetcher\n"
            "purpose: Pulls papers and metadata from arXiv.\n"
            "tools: web_search, web_extract, file\n"
            "model: opencode-go/deepseek-v4.1-flash\n",
            encoding="utf-8")
        code, out, _ = run_main(["--agents-dir", str(tmp_root)])
        line = "arxiv-fetcher | Pulls papers and metadata from arXiv. | " \
               "web_search, web_extract, file | " \
               "opencode-go/deepseek-v4.1-flash | arxiv-fetcher.yaml"
        if code == 0 and line in out and "1 custom archetype(s)" in out:
            print("SELF_TEST PASS: valid file listed with split tools + "
                  "model passthrough")
        else:
            failures += 1
            print("SELF_TEST FAIL: valid file listing "
                  "(exit=%s, out=%r)" % (code, out))

        # Fixture 2: missing purpose → skipped with WARNING, sibling listed.
        (tmp_root / "broken.yaml").write_text(
            "name: broken\ntools: file\n", encoding="utf-8")
        code, out, err = run_main(["--agents-dir", str(tmp_root)])
        if (code == 0 and "WARNING:" in err and "broken.yaml" in err
                and "arxiv-fetcher |" in out
                and "broken |" not in out):
            print("SELF_TEST PASS: malformed file skipped with WARNING, "
                  "valid sibling still listed")
        else:
            failures += 1
            print("SELF_TEST FAIL: malformed-skip "
                  "(exit=%s, out=%r, err=%r)" % (code, out, err))

        # Fixture 3: missing dir → exit 0 + `no custom archetypes`.
        code, out, _ = run_main(
            ["--agents-dir", str(tmp_root / "does-not-exist")])
        if code == 0 and "no custom archetypes" in out:
            print("SELF_TEST PASS: missing dir exits 0 with "
                  "'no custom archetypes'")
        else:
            failures += 1
            print("SELF_TEST FAIL: missing dir "
                  "(exit=%s, out=%r)" % (code, out))

        # Fixture 4: --agents-dir override respected over env/default.
        dir_a = tmp_root / "envdir"
        dir_b = tmp_root / "flagdir"
        dir_a.mkdir(exist_ok=True)
        dir_b.mkdir(exist_ok=True)
        (dir_a / "env-only.yaml").write_text(
            "name: env-only\npurpose: From the env dir.\n",
            encoding="utf-8")
        (dir_b / "flag-only.yaml").write_text(
            "name: flag-only\npurpose: From the flag dir.\n",
            encoding="utf-8")
        saved = os.environ.get("HERMES_AGENTS_DIR")
        try:
            os.environ["HERMES_AGENTS_DIR"] = str(dir_a)
            code, out, _ = run_main(["--agents-dir", str(dir_b)])
            flag_wins = (code == 0 and "flag-only |" in out
                         and "env-only |" not in out)
            code2, out2, _ = run_main([])
            env_beats_default = (code2 == 0 and "env-only |" in out2)
            if flag_wins and env_beats_default:
                print("SELF_TEST PASS: --agents-dir beats env, "
                      "env beats default")
            else:
                failures += 1
                print("SELF_TEST FAIL: dir-resolution chain "
                      "(flag_out=%r, env_out=%r)" % (out, out2))
        finally:
            if saved is None:
                os.environ.pop("HERMES_AGENTS_DIR", None)
            else:
                os.environ["HERMES_AGENTS_DIR"] = saved

        # Fixture 5: absent tools/model → defaults (file / empty).
        defaults_dir = tmp_root / "defaultsdir"
        defaults_dir.mkdir(exist_ok=True)
        (defaults_dir / "plain.yaml").write_text(
            "name: plain\npurpose: No tools or model given.\n",
            encoding="utf-8")
        found, _ = discover(defaults_dir)
        if (len(found) == 1 and found[0]["tools"] == ["file"]
                and found[0]["model"] == ""):
            print("SELF_TEST PASS: absent tools/model default to "
                  "file / empty")
        else:
            failures += 1
            print("SELF_TEST FAIL: defaults (got %r)" % (found,))
    finally:
        try:
            if tmp_root.exists():
                for entry in tmp_root.rglob("*"):
                    if entry.is_file() or entry.is_symlink():
                        entry.unlink()
                for entry in sorted(tmp_root.glob("**/*"), reverse=True):
                    if entry.is_dir():
                        entry.rmdir()
                tmp_root.rmdir()
        except OSError:
            pass
    if failures == 0:
        print("SELF_TEST: 5/5 passed")
    else:
        print("SELF_TEST: %d/5 passed" % (5 - failures))
    return failures == 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover user-defined specialist archetypes from "
                    "~/.hermes/agents/*.yaml (F3.2). Malformed files are "
                    "skipped with a WARNING; a missing dir prints "
                    "'no custom archetypes'. Exit 0 on the success path.")
    parser.add_argument("--agents-dir", type=str, default=None,
                        help="Archetype directory (overrides "
                             "HERMES_AGENTS_DIR and the "
                             "~/.hermes/agents default)")
    parser.add_argument("--json", action="store_true",
                        help="Emit the archetype list as JSON instead of "
                             "one 'name | purpose | tools | model | "
                             "source-file' line per archetype")
    parser.add_argument("--self-test", action="store_true",
                        help="Run the built-in self-test (5 fixtures in a "
                             "temp dir under /tmp).")
    args = parser.parse_args(argv)

    if args.self_test:
        return 0 if _self_test() else 1

    agents_dir = resolve_agents_dir(args.agents_dir)
    if not agents_dir.exists():
        print("no custom archetypes")
        return 0
    try:
        archetypes, warnings = discover(agents_dir)
    except OSError as exc:
        print("error: cannot read agents dir %s: %s" % (agents_dir, exc),
              file=sys.stderr)
        return 2
    for warning in warnings:
        print(warning, file=sys.stderr)
    if not archetypes:
        print("no custom archetypes")
        return 0
    if args.json:
        print(json.dumps(archetypes, indent=2, sort_keys=True))
    else:
        for archetype in archetypes:
            print(format_line(archetype))
    print("%d custom archetype(s)" % len(archetypes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
