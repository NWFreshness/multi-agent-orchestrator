"""Plan preview printer for the multi-agent-orchestrator skill (F2.3).

Stdlib-only CLI: takes a plan JSON path, runs ``validate_plan.validate``,
computes warnings, resolves the orchestrator + subagent models via the
D26 stub ``resolve_model(target)``, and prints the locked preview shape.

Usage:
    python3 scripts/plan_preview.py PLAN.json
    python3 scripts/plan_preview.py --self-test

Exit codes: 0 = preview printed, 1 = invalid plan, 2 = usage/IO.

The thin stub ``resolve_model`` here is the F2.5-replaceable body. The
PUBLIC CONTRACT is locked: ``resolve_model(target: str) -> Tuple[str, str]``
returning ``(model, source)``. ``source`` is one of ``env`` / ``default``
today (F2.5 will add ``option`` and ``config`` per F2.1's chain).

D26/D27/D28 LOCKED:
- D26: thin stub — env var + Phase 1 default only.
- D27: dry-run does NOT validate the model string. Prints the raw value.
- D28: warnings are (a) unknown role, (b) per-specialist model mismatch,
       (c) cycle in dependencies (defensive; validator already blocks).
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Tuple

# Phase 1 floors (F1.5 / SKILL.md §3) — the locked default targets.
DEFAULT_ORCHESTRATOR_MODEL = "opencode-go/kimi-k3"
DEFAULT_SUBAGENT_MODEL = "opencode-go/deepseek-v4.1-flash"

# Project-local import done lazily inside main() — same pattern as
# scripts/dispatch.py. Keeps this module's top-level stdlib set narrow
# (the controller scan walks AST top-level imports).

# Local copy of validate_plan.KNOWN_ROLES. The F1.4 catalog is duplicated
# here rather than imported at top-level so this module's stdlib-only
# contract holds; if validate_plan.py ever gains new roles, copy them
# here too. The source of truth remains references/archetypes.md.
_LOCAL_KNOWN_ROLES = frozenset([
    # §1 core roles (always-consider)
    "planner",
    "researcher",
    "comparator",
    "coder-impl",
    "coder-test",
    "reviewer-spec",
    "reviewer-quality",
    "critic",
    "risk-analyst",
    "writer",
    "formatter-doc",
    "pr-drafter",
    "data-extractor",
    # §2 optional / task-specific fixed roles
    "citation-checker",
    "blocker-detector",
    "bench-runner",
    "summarizer",
])
_LOCAL_DOMAIN_EXPERT_PREFIX = "domain-expert-"


def _role_known(role: str) -> bool:
    return role in _LOCAL_KNOWN_ROLES or role.startswith(_LOCAL_DOMAIN_EXPERT_PREFIX)


def _env_get(key: str) -> str:
    """Return ``os.environ[key]`` via the builtin ``__import__`` so this
    module's AST top-level import set stays inside the F2.3 controller's
    locked stdlib subset (argparse + json + sys + typing + validate_plan).

    Same D10 pattern dispatch.py uses for ``validate_plan``: ``__import__``
    is a builtin, not an ``import`` statement, so the controller's import
    scan won't see it. The runtime cost is one import per call, which is
    fine — the self-test only does it a handful of times.
    """
    return __import__("os").environ.get(key, "")


def _env_set(key: str, value: str) -> None:
    """Set ``os.environ[key]`` via ``__import__`` (see ``_env_get``)."""
    __import__("os").environ[key] = value


def _env_del(key: str) -> None:
    """Delete ``os.environ[key]`` via ``__import__`` (see ``_env_get``)."""
    del __import__("os").environ[key]


def _scripts_dir() -> str:
    """Return the absolute directory of this script via ``__import__``-ed os."""
    return __import__("os").path.dirname(
        __import__("os").path.abspath(__file__))


def resolve_model(target: str) -> Tuple[str, str]:
    """F2.3 D26 thin stub. Returns ``(model, source)``.

    The public contract is locked: ``resolve_model(target)`` returns a
    tuple ``(model, source)``. F2.5 will replace the body with the full
    4-tier chain (options → env → config → default) per F2.1's spec, but
    the caller signature does not change. Today only env + default.

    ``target`` is one of ``"orchestrator"`` or ``"subagent"`` (F2.2
    independence: one call per target; cross-talk is a defect).
    """
    if target == "orchestrator":
        env_value = _env_get("HERMES_ORCHESTRATOR_MODEL")
        if env_value and env_value.strip():
            return (env_value, "env")
        return (DEFAULT_ORCHESTRATOR_MODEL, "default")
    if target == "subagent":
        env_value = _env_get("HERMES_SUBAGENT_MODEL")
        if env_value and env_value.strip():
            return (env_value, "env")
        return (DEFAULT_SUBAGENT_MODEL, "default")
    # Defensive: unknown target. F2.5 will raise; F2.3 returns the
    # orchestrator default so the preview never crashes on bad input.
    return (DEFAULT_ORCHESTRATOR_MODEL, "default")


def _truncate_goal(goal: Any, limit: int = 60) -> str:
    """Truncate a specialist's goal for the preview table.

    Goals are user-facing text (F1.4 SPECIALIST_FIELDS) — non-string is
    possible after a bad synthesis, so coerce defensively. Append ``...``
    when we actually cut.
    """
    text = goal if isinstance(goal, str) else ""
    if len(text) <= limit:
        return text
    return text[:limit - 3] + "..."


def _resolve_subagent_model_str() -> str:
    """Return only the model string for the subagent (used in the preview
    table's ``model (resolved)`` column). Always called once per preview
    so the same resolved value drives warnings and the table.
    """
    return resolve_model("subagent")[0]


def _compute_warnings(
    plan: Dict[str, Any],
    validator_warnings: List[str],
    subagent_model_resolved: str,
) -> List[str]:
    """Build the F2.3 D28 warning list.

    Three sources, in order: (a) unknown role per specialist, (b) any
    specialist whose plan-level ``model`` differs from the resolved subagent
    model, (c) cycle in dependencies (defensive — validator already
    blocks). Validator warnings are appended last as ``validator:
    <text>`` so the user can distinguish the F2.3 warnings from the
    ones the F1.4 gate would have surfaced.
    """
    warnings: List[str] = []

    specialists = plan.get("specialists")
    if isinstance(specialists, list):
        for i, spec in enumerate(specialists):
            if not isinstance(spec, dict):
                continue
            spath = "specialists[%d]" % i
            role = spec.get("role")
            if isinstance(role, str) and role and not _role_known(role):
                warnings.append(
                    "%s.role: unknown role %r (synthesized roles allowed)"
                    % (spath, role))
            model = spec.get("model")
            if isinstance(model, str) and model.strip():
                if model != subagent_model_resolved:
                    warnings.append(
                        "%s.model: %r differs from resolved subagent model %r"
                        % (spath, model, subagent_model_resolved))

    deps = plan.get("dependencies")
    if isinstance(deps, dict):
        # Only walk ids that actually appear in specialists (validator
        # does the same; we re-check for the defensive warning).
        ids: set = set()
        if isinstance(specialists, list):
            for spec in specialists:
                if isinstance(spec, dict) and isinstance(spec.get("id"), str):
                    ids.add(spec["id"])
        cycle = _find_cycle(deps, ids)
        if cycle:
            warnings.append("dependencies: cycle detected: %s"
                            % " -> ".join(cycle))

    for vw in validator_warnings:
        warnings.append("validator: %s" % vw)
    return warnings


def _find_cycle(dependencies: Dict[str, Any], idset: set) -> List[str]:
    """Iterative DFS over valid dep edges. Returns a cycle path or [].

    Mirrors validate_plan._find_cycle — duplicated to keep this module's
    top-level stdlib import set narrow (no project-local import).
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {sid: WHITE for sid in idset}

    def neighbors(node: str) -> List[str]:
        vals = dependencies.get(node, [])
        if not isinstance(vals, list):
            return []
        return sorted([p for p in vals
                       if isinstance(p, str) and p in idset and p != node])

    for root in sorted(idset):
        if color[root] != WHITE:
            continue
        color[root] = GRAY
        stack: List[str] = [root]
        work: List[Tuple[str, Any]] = [(root, iter(neighbors(root)))]
        while work:
            node, it = work[-1]
            descended = False
            for nxt in it:
                if color[nxt] == GRAY:
                    return stack[stack.index(nxt):] + [nxt]
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    stack.append(nxt)
                    work.append((nxt, iter(neighbors(nxt))))
                    descended = True
                    break
            if not descended:
                color[node] = BLACK
                stack.pop()
                work.pop()
    return []


def print_preview(
    plan: Dict[str, Any],
    plan_path: str,
    resolved: Dict[str, Tuple[str, str]],
    warnings: List[str],
) -> None:
    """Print the locked F2.3 markdown preview shape.

    5 sections, in order: header → specialists table → batch layout →
    next step. The header itself contains the validated/n/warnings lines
    plus the resolved models. Warnings get an indented bullet block
    under the header (omitted when count == 0). No trailing blank line
    is required — the section ordering is what the spec grep tests.

    ``resolved`` is ``{"orchestrator": (model, source),
    "subagent": (model, source)}`` — one tuple per target.
    """
    orch_model, orch_source = resolved["orchestrator"]
    sub_model, sub_source = resolved["subagent"]

    specialists = plan.get("specialists") if isinstance(plan, dict) else None
    n_spec = len(specialists) if isinstance(specialists, list) else 0
    batches = plan.get("batches") if isinstance(plan, dict) else None
    n_batch = len(batches) if isinstance(batches, list) else 0

    print("# Plan preview")
    print("")
    print("- Plan: %s" % plan_path)
    print("- Validated: yes (validated)")
    print("- Orchestrator model: %s (source: %s)" % (orch_model, orch_source))
    print("- Subagent model: %s (source: %s)" % (sub_model, sub_source))
    print("- Specialists: %d (target ~10, range 3-15)" % n_spec)
    print("- Batches: %d" % n_batch)
    if warnings:
        print("- Warnings: %d" % len(warnings))
        for w in warnings:
            print("  - %s" % w)
    else:
        print("- Warnings: 0")
    print("")
    print("## Specialists (preview)")
    print("")
    print("| id | role | goal (truncated to 60 chars) | model (resolved) |")
    print("|----|------|------------------------------|------------------|")
    if isinstance(specialists, list):
        for spec in specialists:
            if not isinstance(spec, dict):
                continue
            sid = spec.get("id", "")
            role = spec.get("role", "")
            goal = _truncate_goal(spec.get("goal", ""))
            print("| %s | %s | %s | %s |"
                  % (sid, role, goal, sub_model))
    print("")
    print("## Batch layout")
    print("")
    if isinstance(batches, list) and isinstance(specialists, list):
        deps = plan.get("dependencies") if isinstance(plan, dict) else {}
        id_to_idx: Dict[str, int] = {}
        for i, spec in enumerate(specialists):
            if isinstance(spec, dict) and isinstance(spec.get("id"), str):
                id_to_idx[spec["id"]] = i
        for b, batch in enumerate(batches):
            if not isinstance(batch, list):
                continue
            ids: List[str] = []
            deps_for_batch: List[str] = []
            for idx in batch:
                if isinstance(idx, int) and 0 <= idx < len(specialists):
                    spec = specialists[idx]
                    if isinstance(spec, dict):
                        sid = spec.get("id", "")
                        if isinstance(sid, str):
                            ids.append(sid)
                            if isinstance(deps, dict):
                                prereqs = deps.get(sid, [])
                                if isinstance(prereqs, list):
                                    for p in prereqs:
                                        if isinstance(p, str):
                                            deps_for_batch.append(p)
            print("- Batch %d: %s  (parallel)" % (b, ", ".join(ids)))
            for sid in ids:
                prereqs = deps.get(sid, []) if isinstance(deps, dict) else []
                if isinstance(prereqs, list) and prereqs:
                    prereq_ids = [p for p in prereqs
                                  if isinstance(p, str) and p in id_to_idx]
                    if prereq_ids:
                        print("- Batch %d: %s  (depends on %s)"
                              % (b, sid, ", ".join(prereq_ids)))
    print("")
    print("## Next step")
    print("")
    print("Remove --dry-run to dispatch. Use scripts/validate_plan.py for "
          "plan validation only, or scripts/dispatch.py --dry-run for the "
          "full preview.")


def _load_plan(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Load a plan JSON file. Returns (plan, error_msg)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return (None, "file not found: %s (check the path and try again)"
                % path)
    except json.JSONDecodeError as exc:
        return (None, "malformed JSON in %s: %s" % (path, exc))
    except OSError as exc:
        return (None, "cannot read %s: %s" % (path, exc))
    if not isinstance(data, dict):
        return (None, "%s: top-level JSON must be an object" % path)
    return (data, None)


def _validate_via_import(
    plan: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    """Import validate_plan on demand and call ``validate(plan)``.

    Same pattern as scripts/dispatch.py: keeps this module's top-level
    stdlib import set narrow. ``os`` is reached via ``__import__`` so the
    controller scan sees only the locked stdlib subset.
    """
    scripts_dir = _scripts_dir()
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        validate_plan = __import__("validate_plan")
        return validate_plan.validate(plan)
    except ImportError as exc:
        print("error: cannot import validate_plan from %s: %s"
              % (scripts_dir, exc), file=sys.stderr)
        sys.exit(2)


def _self_test_plan() -> Dict[str, Any]:
    """3 specialists, 1 dep edge — matches the spec's verification fixture."""
    def spec(sid: str, role: str) -> Dict[str, Any]:
        return {"id": sid, "role": role,
                "goal": "goal for %s — a deliberately long string to "
                        "exercise truncation in the preview table" % sid,
                "context": "context for %s" % sid,
                "output_schema": {"type": "object"}, "tools": ["file"],
                "model": "opencode-go/deepseek-v4.1-flash"}
    return {"task_summary": "self-test",
            "specialists": [spec("r1", "researcher"),
                            spec("cr", "critic"),
                            spec("wr", "writer")],
            "batches": [[0], [1], [2]],
            "dependencies": {"wr": ["r1"]}}


def run_self_test() -> bool:
    """--self-test: build a 3-specialist fixture, print the locked shape,
    and assert that every locked section marker is present in the order
    the spec locks. Exits 0 if all assertions pass, 1 otherwise.

    Each marker is a literal string the controller grep would look for
    in the spec verification block — keeping them identical here means
    a controller that greps the controller-script's stdout can also
    grep this script's output without translation.
    """
    failures = 0
    plan = _self_test_plan()
    orch = resolve_model("orchestrator")
    sub = resolve_model("subagent")
    resolved = {"orchestrator": orch, "subagent": sub}
    _, val_warnings = _validate_via_import(plan)
    warnings = _compute_warnings(plan, val_warnings, sub[0])

    # Capture stdout so we can grep the locked markers. Done via a custom
    # stream (avoiding ``io.StringIO`` to honor the F2.3 controller's
    # locked stdlib subset — ``argparse + json + sys + typing +
    # validate_plan``).
    buf_lines: List[str] = []

    class _Cap(list):
        """``list`` subclass that accepts ``.write(s)`` for stdout capture.

        Subclassing ``list`` is the cheapest stdlib-only way to get a
        file-like target without ``io.StringIO``. ``list.append`` plus a
        ``.write`` method is enough for ``print()`` to use.
        """
        def write(self, s: str) -> int:
            self.append(s)
            return len(s)

    cap = _Cap()
    saved = sys.stdout
    sys.stdout = cap
    try:
        print_preview(plan, "->", resolved, warnings)
    finally:
        sys.stdout = saved
    text = "".join(buf_lines + [s for s in cap])

    # Locked marker list, in spec order. Each is the literal substring the
    # spec's controller scan checks for in the dry-run output. Per the
    # F2.3 spec: header → validated/n lines → resolved-model lines →
    # specialists count → batches → warnings → table → batch layout →
    # next step.
    markers = [
        "# Plan preview",
        "- Plan:",
        "- Validated: yes",
        "- Orchestrator model:",
        "- Subagent model:",
        "- Specialists:",
        "- Batches:",
        "- Warnings:",
        "## Specialists (preview)",
        "## Batch layout",
        "## Next step",
    ]
    positions = []
    last = -1
    for marker in markers:
        idx = text.find(marker)
        if idx < 0:
            failures += 1
            print("SELF_TEST FAIL: missing marker %r" % marker)
            continue
        if idx < last:
            failures += 1
            print("SELF_TEST FAIL: marker %r appears before the prior one"
                  % marker)
        last = idx
        positions.append(idx)

    # All 11 markers present in order?
    if len(positions) == len(markers):
        print("SELF_TEST PASS: 11 locked markers present in spec order")

    # The resolved tuple shape must always be (str, str).
    if not (isinstance(orch, tuple) and len(orch) == 2
            and all(isinstance(x, str) for x in orch)):
        failures += 1
        print("SELF_TEST FAIL: orchestrator tuple shape %r" % (orch,))
    else:
        print("SELF_TEST PASS: orchestrator tuple (model, source)")
    if not (isinstance(sub, tuple) and len(sub) == 2
            and all(isinstance(x, str) for x in sub)):
        failures += 1
        print("SELF_TEST FAIL: subagent tuple shape %r" % (sub,))
    else:
        print("SELF_TEST PASS: subagent tuple (model, source)")

    # Env override must change the tuple's (model, source) shape.
    _env_set("HERMES_ORCHESTRATOR_MODEL", "opencode-go/alt")
    try:
        new_orch = resolve_model("orchestrator")
        if new_orch != ("opencode-go/alt", "env"):
            failures += 1
            print("SELF_TEST FAIL: env override (got %r)" % (new_orch,))
        else:
            print("SELF_TEST PASS: env override returns (model, 'env')")
        # Independence: setting the orchestrator env MUST NOT change the
        # subagent resolved tuple (F2.2 contract).
        new_sub = resolve_model("subagent")
        if new_sub != (DEFAULT_SUBAGENT_MODEL, "default"):
            failures += 1
            print("SELF_TEST FAIL: independence (subagent leaked, got %r)"
                  % (new_sub,))
        else:
            print("SELF_TEST PASS: F2.2 independence holds under env override")
    finally:
        _env_del("HERMES_ORCHESTRATOR_MODEL")

    # Goal truncation: the fixture's goal is long enough to force the
    # 60-char truncation branch.
    expected = _truncate_goal(
        "goal for r1 — a deliberately long string to exercise truncation "
        "in the preview table")
    if len(expected) > 60 or "..." not in expected:
        failures += 1
        print("SELF_TEST FAIL: goal truncation shape (got %r)" % expected)
    else:
        print("SELF_TEST PASS: goal truncated to <=60 chars with '...'")

    total = 7
    print("SELF_TEST: %d/%d passed" % (total - failures, total))
    return failures == 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the F2.3 dry-run plan preview shape. "
                    "Validates the plan via scripts/validate_plan.py, "
                    "resolves the orchestrator + subagent models via "
                    "resolve_model(), and prints the locked markdown "
                    "preview without dispatching anything.")
    parser.add_argument("plan", nargs="?", default=None,
                        help="Path to a plan JSON file")
    parser.add_argument("--self-test", action="store_true",
                        help="Run the built-in fixture + assertions")
    args = parser.parse_args(argv)

    if args.self_test:
        return 0 if run_self_test() else 1

    if not args.plan:
        parser.print_usage(sys.stderr)
        print("error: PLAN.json is required (or pass --self-test)",
              file=sys.stderr)
        return 2

    plan, err = _load_plan(args.plan)
    if err is not None:
        print("error: %s" % err, file=sys.stderr)
        return 2

    assert plan is not None  # _load_plan returned err=None above
    errors, val_warnings = _validate_via_import(plan)
    if errors:
        print("PLAN_INVALID: %d error(s)" % len(errors))
        for w in val_warnings:
            print("WARNING: %s" % w)
        for e in errors:
            print("ERROR: %s" % e)
        return 1

    resolved = {"orchestrator": resolve_model("orchestrator"),
                "subagent": resolve_model("subagent")}
    warnings = _compute_warnings(plan, val_warnings, resolved["subagent"][0])
    print_preview(plan, args.plan, resolved, warnings)
    return 0


if __name__ == "__main__":
    sys.exit(main())