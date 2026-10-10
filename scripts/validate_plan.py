"""Plan validator for the multi-agent-orchestrator skill (F1.4).

Stdlib-only CLI: parses an Orchestrator plan JSON file and rejects invalid
plans with actionable errors before any ``delegate_task`` is issued.

Usage:
    python3 scripts/validate_plan.py PLAN.json [--quiet]
    python3 scripts/validate_plan.py --self-test

Exit codes: 0 = dispatchable, 1 = invalid plan, 2 = usage/file error.
"""

import argparse
import json
import sys
from typing import Any, Dict, List, Tuple

# Human source of truth for this catalog: references/archetypes.md
# (D8: hardcoded here on purpose — never markdown-parse prose at runtime;
# update this frozenset when the catalog changes.)
KNOWN_ROLES = frozenset([
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

DOMAIN_EXPERT_PREFIX = "domain-expert-"
DEFAULT_MODEL = "opencode-go/deepseek-v4.1-flash"

TOP_LEVEL_KEYS = frozenset(["task_summary", "specialists", "batches", "dependencies"])
SPECIALIST_FIELDS = ("id", "role", "goal", "context", "output_schema", "tools", "model")


def _role_known(role: str) -> bool:
    return role in KNOWN_ROLES or role.startswith(DOMAIN_EXPERT_PREFIX)


def _find_cycle(dependencies: Dict[str, Any], idset: set) -> List[str]:
    """Iterative DFS over valid dep edges. Returns a cycle path or []."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in idset}

    def neighbors(node: str) -> List[str]:
        vals = dependencies.get(node, [])
        if not isinstance(vals, list):
            return []
        return sorted([p for p in vals if p in idset and p != node])

    for root in sorted(idset):
        if color[root] != WHITE:
            continue
        color[root] = GRAY
        stack = [root]
        work = [(root, iter(neighbors(root)))]
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


def _check_nesting(node: Any, path: str, errors: List[str], seen: set) -> None:
    """E8: flag any nested 'specialists' key below the top level."""
    if isinstance(node, dict):
        if id(node) in seen:
            return
        seen.add(id(node))
        if "specialists" in node:
            errors.append("%s.specialists: nested 'specialists' key not allowed" % path)
        for key, value in node.items():
            _check_nesting(value, "%s.%s" % (path, key), errors, seen)
    elif isinstance(node, list):
        if id(node) in seen:
            return
        seen.add(id(node))
        for j, value in enumerate(node):
            _check_nesting(value, "%s[%d]" % (path, j), errors, seen)


def validate(plan: Any) -> Tuple[List[str], List[str]]:
    """Validate a plan object. Returns (errors, warnings).

    Error strings are "<path>: <message>"; callers prefix "ERROR: ".
    Warning strings are "<path>: <message>"; callers prefix "WARNING: ".
    Pure function — safe to import (F1.10 will import it).
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(plan, dict):
        return (["plan: must be a JSON object"], warnings)

    # E1: unknown top-level keys are warnings (forward-compat), never errors.
    for key in plan:
        if key not in TOP_LEVEL_KEYS:
            warnings.append("%s: unknown top-level key (forward-compat, ignored)" % key)

    # E1: shape.
    task_summary = plan.get("task_summary")
    if not isinstance(task_summary, str) or not task_summary.strip():
        errors.append("task_summary: must be a non-empty string")
    specialists = plan.get("specialists")
    if not isinstance(specialists, list):
        errors.append("specialists: must be a list")
        specialists = None
    batches = plan.get("batches")
    if not isinstance(batches, list):
        errors.append("batches: must be a list")
        batches = None
    dependencies = plan.get("dependencies")
    if not isinstance(dependencies, dict):
        errors.append("dependencies: must be an object")
        dependencies = None

    # E2 + E3: count and per-specialist fields.
    by_id: Dict[str, int] = {}
    if specialists is not None:
        if not 3 <= len(specialists) <= 15:
            errors.append(
                "specialists: must have 3-15 entries, got %d" % len(specialists))
        seen_ids = set()
        for i, spec in enumerate(specialists):
            path = "specialists[%d]" % i
            if not isinstance(spec, dict):
                errors.append("%s: must be an object" % path)
                continue
            for field in SPECIALIST_FIELDS:
                if field not in spec:
                    errors.append("%s.%s: missing required field" % (path, field))
            sid = spec.get("id")
            if not isinstance(sid, str) or not sid.strip():
                errors.append("%s.id: must be a non-empty string" % path)
            else:
                if sid in seen_ids:
                    errors.append("%s.id: duplicate id %r" % (path, sid))
                else:
                    seen_ids.add(sid)
                    by_id[sid] = i
            for field in ("goal", "context"):
                if field in spec and (
                        not isinstance(spec[field], str) or not spec[field].strip()):
                    errors.append("%s.%s: must be a non-empty string" % (path, field))
            if "output_schema" in spec and not isinstance(spec["output_schema"], dict):
                errors.append("%s.output_schema: must be an object" % path)
            if "tools" in spec:
                tools = spec["tools"]
                if (not isinstance(tools, list) or not tools
                        or any(not isinstance(t, str) or not t for t in tools)):
                    errors.append(
                        "%s.tools: must be a non-empty list of strings" % path)
            if "model" in spec:
                model = spec["model"]
                if not isinstance(model, str) or not model.strip():
                    errors.append("%s.model: must be a non-empty string" % path)
                elif model != DEFAULT_MODEL:
                    warnings.append(
                        "%s.model: non-default model %r (forward-compat note)"
                        % (path, model))
            if "role" in spec:
                role = spec["role"]
                if not isinstance(role, str) or not role:
                    errors.append("%s.role: must be a non-empty string" % path)
                elif not _role_known(role):
                    # E4: unknown roles are warnings — synthesized roles allowed.
                    warnings.append(
                        "%s.role: unknown role %r (synthesized roles allowed)"
                        % (path, role))

    # E5: batches — non-empty int lists, indices in range, covered EXACTLY once.
    n = len(specialists) if specialists is not None else 0
    pos: Dict[int, int] = {}
    if batches is not None:
        counts: Dict[int, int] = {}
        for b, batch in enumerate(batches):
            bpath = "batches[%d]" % b
            if not isinstance(batch, list):
                errors.append("%s: must be a list of specialist indices" % bpath)
                continue
            if not batch:
                errors.append("%s: must be non-empty" % bpath)
                continue
            for idx in batch:
                if isinstance(idx, bool) or not isinstance(idx, int):
                    errors.append(
                        "%s: %r is not a specialist index (must be an int)"
                        % (bpath, idx))
                    continue
                if not 0 <= idx < n:
                    errors.append(
                        "%s: index %d out of range (0..%d)" % (bpath, idx, n - 1))
                    continue
                counts[idx] = counts.get(idx, 0) + 1
                if idx not in pos:
                    pos[idx] = b
        if specialists is not None:
            for idx in range(n):
                if counts.get(idx, 0) == 0:
                    errors.append(
                        "batches: specialist index %d not covered by any batch" % idx)
                elif counts[idx] > 1:
                    errors.append(
                        "batches: specialist index %d covered %d times "
                        "(must be exactly once)" % (idx, counts[idx]))

    # E6: dependencies.
    if dependencies is not None:
        idset = set(by_id)
        for key, prereqs in dependencies.items():
            kpath = "dependencies.%s" % key
            if key not in idset:
                errors.append("%s: unknown specialist id %r" % (kpath, key))
            if not isinstance(prereqs, list):
                errors.append("%s: must be a list of specialist ids" % kpath)
                continue
            for pre in prereqs:
                if pre not in idset:
                    errors.append("%s: unknown prerequisite id %r" % (kpath, pre))
                elif pre == key:
                    errors.append("%s: self-dependency on %r" % (kpath, pre))
        cycle = _find_cycle(dependencies, idset)
        if cycle:
            errors.append("dependencies: cycle detected: %s" % " -> ".join(cycle))
        if specialists is not None and batches is not None:
            idpos = {sid: pos[i] for sid, i in by_id.items() if i in pos}
            for key, prereqs in dependencies.items():
                if not isinstance(prereqs, list):
                    continue
                for pre in prereqs:
                    if key in idpos and pre in idpos and not idpos[key] > idpos[pre]:
                        errors.append(
                            "dependencies.%s: batch position %d not strictly after "
                            "prerequisite %r (batch %d)"
                            % (key, idpos[key], pre, idpos[pre]))

    # E7: reviewer rule.
    if specialists is not None:
        def _is_reviewer(spec: Any) -> bool:
            role = spec.get("role") if isinstance(spec, dict) else None
            return (isinstance(role, str)
                    and (role == "critic" or role.startswith("reviewer-")))

        if not any(_is_reviewer(spec) for spec in specialists):
            errors.append(
                "specialists: plan must include at least one reviewer-* or critic role")

    # E8: nesting guard.
    for top_key, top_val in plan.items():
        _check_nesting(top_val, top_key, errors, set())

    return (errors, warnings)


def _spec(sid: str, role: str = "researcher", **overrides: Any) -> Dict[str, Any]:
    entry = {"id": sid, "role": role, "goal": "goal for %s" % sid,
             "context": "context for %s" % sid,
             "output_schema": {"type": "object"}, "tools": ["file"],
             "model": DEFAULT_MODEL}
    entry.update(overrides)
    return entry


def _base_plan() -> Dict[str, Any]:
    return {"task_summary": "smoke",
            "specialists": [_spec("r1", "researcher"), _spec("cr", "critic"),
                            _spec("wr", "writer")],
            "batches": [[0], [1], [2]],
            "dependencies": {"wr": ["r1"]}}


def _self_test_cases() -> List[Tuple[str, Dict[str, Any], bool]]:
    """(name, plan, expect_valid) — one fixture per E-rule."""
    cases: List[Tuple[str, Dict[str, Any], bool]] = []

    cases.append(("valid", _base_plan(), True))

    def mod(name: str, fn) -> None:
        plan = _base_plan()
        fn(plan)
        cases.append((name, plan, False))

    def set_count(plan: Dict[str, Any]) -> None:
        plan["specialists"] = [_spec("only", "critic")]
        plan["batches"] = [[0]]
        plan["dependencies"] = {}
    mod("count<3", set_count)

    def set_dup_id(plan: Dict[str, Any]) -> None:
        plan["specialists"][2]["id"] = "r1"
    mod("duplicate-id", set_dup_id)

    def set_oor(plan: Dict[str, Any]) -> None:
        plan["batches"] = [[0], [1], [9]]
    mod("out-of-range-index", set_oor)

    def set_missing(plan: Dict[str, Any]) -> None:
        plan["batches"] = [[0], [1]]
    mod("missing-coverage", set_missing)

    def set_dupcov(plan: Dict[str, Any]) -> None:
        plan["batches"] = [[0], [1], [1, 2]]
    mod("duplicate-coverage", set_dupcov)

    def set_unknowndep(plan: Dict[str, Any]) -> None:
        plan["dependencies"] = {"wr": ["ghost"]}
    mod("unknown-dep-id", set_unknowndep)

    def set_selfdep(plan: Dict[str, Any]) -> None:
        plan["dependencies"] = {"wr": ["wr"]}
    mod("self-dep", set_selfdep)

    def set_cycle(plan: Dict[str, Any]) -> None:
        plan["dependencies"] = {"r1": ["wr"], "wr": ["r1"]}
    mod("cycle", set_cycle)

    def set_order(plan: Dict[str, Any]) -> None:
        plan["dependencies"] = {"r1": ["wr"]}
    mod("batch-order-violation", set_order)

    def set_noreviewer(plan: Dict[str, Any]) -> None:
        plan["specialists"] = [_spec("r1", "researcher"), _spec("w1", "writer"),
                               _spec("c1", "coder-impl")]
    mod("missing-reviewer", set_noreviewer)

    def set_schema(plan: Dict[str, Any]) -> None:
        plan["specialists"][0]["output_schema"] = "not-an-object"
    mod("non-object-output_schema", set_schema)

    def set_nested(plan: Dict[str, Any]) -> None:
        plan["specialists"][0]["specialists"] = [{"id": "deep"}]
    mod("nested-specialists", set_nested)

    return cases


def run_self_test() -> bool:
    """Exercise every E-rule through validate(). True iff all asserts pass."""
    failures = 0
    for name, plan, expect_valid in _self_test_cases():
        errors, warnings = validate(plan)
        valid = not errors
        extra_ok = True
        if name == "valid" and warnings:
            extra_ok = False
        if name == "cycle" and not any("->" in e for e in errors):
            extra_ok = False
        if valid == expect_valid and extra_ok:
            print("SELF_TEST PASS: %s" % name)
        else:
            failures += 1
            print("SELF_TEST FAIL: %s (expect_valid=%s, got_valid=%s, errors=%s)"
                  % (name, expect_valid, valid, errors))
    total = len(_self_test_cases())
    print("SELF_TEST: %d/%d passed" % (total - failures, total))
    return failures == 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a multi-agent-orchestrator plan JSON file.")
    parser.add_argument("plan", nargs="?", default=None,
                        help="Path to the plan JSON file")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress PLAN_VALID chatter (never errors/warnings)")
    parser.add_argument("--self-test", action="store_true",
                        help="Run built-in fixtures covering every E-rule")
    args = parser.parse_args(argv)

    if args.self_test:
        return 0 if run_self_test() else 1

    if not args.plan:
        parser.print_usage(sys.stderr)
        print("error: PLAN.json is required (or pass --self-test)", file=sys.stderr)
        return 2

    try:
        with open(args.plan, "r", encoding="utf-8") as handle:
            plan = json.load(handle)
    except FileNotFoundError:
        print("error: file not found: %s (check the path and try again)" % args.plan,
              file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print("error: malformed JSON in %s: %s" % (args.plan, exc), file=sys.stderr)
        return 2
    except OSError as exc:
        print("error: cannot read %s: %s" % (args.plan, exc), file=sys.stderr)
        return 2

    errors, warnings = validate(plan)

    if errors:
        print("PLAN_INVALID: %d error(s)" % len(errors))
        for warning in warnings:
            print("WARNING: %s" % warning)
        for error in errors:
            print("ERROR: %s" % error)
        return 1

    n_specialists = 0
    n_batches = 0
    if isinstance(plan, dict):
        if isinstance(plan.get("specialists"), list):
            n_specialists = len(plan["specialists"])
        if isinstance(plan.get("batches"), list):
            n_batches = len(plan["batches"])
    if not args.quiet:
        print("PLAN_VALID: %d specialists in %d batches" % (n_specialists, n_batches))
    for warning in warnings:
        print("WARNING: %s" % warning)
    return 0


if __name__ == "__main__":
    sys.exit(main())
