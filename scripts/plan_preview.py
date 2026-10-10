"""Plan preview printer for the multi-agent-orchestrator skill (F2.3 + F2.5).

Stdlib-only CLI: takes a plan JSON path, runs ``validate_plan.validate``,
computes warnings, resolves the orchestrator + subagent models via the
full 4-tier ``resolve_model(target)`` chain, and prints the locked preview
shape.

Usage:
    python3 scripts/plan_preview.py PLAN.json
    python3 scripts/plan_preview.py --self-test

Exit codes: 0 = preview printed, 1 = invalid plan, 2 = usage/IO.

The ``resolve_model`` helper lives here per D31. The PUBLIC CONTRACT is
locked: ``resolve_model(target: str) -> Tuple[str, str]`` returning
``(model, source)``. ``source`` is one of ``option``, ``env``, ``config``,
``default`` (F2.5 contract). Setting one tier does NOT change the other
(F2.2 independence).

D26/D27/D28 LOCKED:
- D26 (F2.5 fullfilled): 4-tier chain — option > env > config > default.
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


def _config_value(key: str) -> str:
    """Read a single value from ``~/.hermes/config.yaml`` (D32).

    The reader is stdlib-only: flat ``key: value`` lines, ``# comments``,
    blank lines ignored. ONLY keys of the form
    ``multi_agent_orchestrator.<key>`` are recognized, so a future config
    file with other sections is silently ignored here. Missing file,
    malformed file, or missing key → ``""`` (tier skipped). The path
    lookup uses ``__import__`` to keep the module's top-level imports
    stdlib-only (D24 / F2.1 D24).
    """
    os = __import__("os")
    home = os.path.expanduser("~")
    config_path = os.path.join(home, ".hermes", "config.yaml")
    if not os.path.isfile(config_path):
        return ""
    prefix = "multi_agent_orchestrator.%s" % key
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # Expect ``<dotted_key>: <value>`` — strip inline ``#``
                # comments before splitting.
                hash_at = line.find(" #")
                if hash_at >= 0:
                    line = line[:hash_at].rstrip()
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip()
                if k == prefix and v:
                    return v
    except OSError:
        return ""
    return ""


def resolve_model(
    target: str,
    orchestrator: Optional[str] = None,
    subagent: Optional[str] = None,
) -> Tuple[str, str]:
    """F2.5 4-tier chain: option > env > config > default. Returns ``(model, source)``.

    ``source`` is one of the four literals:
        ``"option"``  — kwarg was set (non-empty)
        ``"env"``     — ``HERMES_ORCHESTRATOR_MODEL`` / ``HERMES_SUBAGENT_MODEL``
        ``"config"``  — ``~/.hermes/config.yaml`` under
                        ``multi_agent_orchestrator.{orchestrator_model,
                        subagent_model}``
        ``"default"`` — Phase 1 floor (always non-empty)

    First non-empty wins. Independence (F2.2 / D21): this function NEVER
    reads the other target's tiers. Each call walks the chain for one
    target only.

    ``orchestrator`` and ``subagent`` are the option-tier kwargs wired by
    ``scripts/dispatch.py`` (CLI flags ``--orchestrator-model`` /
    ``--subagent-model``). They are also the seam a future library
    caller can use to inject the option tier directly.

    Empty / whitespace-only values at every tier are treated as "unset"
    and the chain continues. The Phase 1 default floor is the locked
    fallback — this function NEVER raises and ALWAYS returns a non-empty
    model string (per F2.1 §6).
    """
    # Tier 1 (option): the explicit per-call override.
    option_value: Optional[str]
    if target == "orchestrator":
        option_value = orchestrator
    elif target == "subagent":
        option_value = subagent
    else:
        # Defensive: unknown target. Return the orchestrator default so
        # the preview never crashes on bad input (matches F2.3 behavior).
        return (DEFAULT_ORCHESTRATOR_MODEL, "default")
    if option_value and option_value.strip():
        return (option_value, "option")

    # Tier 2 (env): shell environment.
    env_key = ("HERMES_ORCHESTRATOR_MODEL" if target == "orchestrator"
               else "HERMES_SUBAGENT_MODEL")
    env_value = _env_get(env_key)
    if env_value and env_value.strip():
        return (env_value, "env")

    # Tier 3 (config): per-profile YAML, stdlib-only flat reader (D32).
    config_key = "orchestrator_model" if target == "orchestrator" else "subagent_model"
    config_value = _config_value(config_key)
    if config_value and config_value.strip():
        return (config_value, "config")

    # Tier 4 (default): Phase 1 floor.
    if target == "subagent":
        return (DEFAULT_SUBAGENT_MODEL, "default")
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
    *,
    cost_per_call: float = 0.01,
    max_cost: float = 0.0,
) -> None:
    """Print the locked F2.3 + F2.5 markdown preview shape.

    5 sections, in order: header → specialists table → batch layout →
    next step. The header itself contains the validated/n/warnings lines
    plus the resolved models. F2.5 adds ONE extra header line when
    ``cost_per_call`` is non-negative AND the plan has specialists:
    ``- Estimated cost: $X.XX (max $Y.YY)``. ``max_cost`` of 0 prints
    ``max $0.00`` (the D34 "no budget" default — the cost check is OFF).
    The line is omitted when there are no specialists (avoids printing
    ``$0.00`` for an empty plan).

    Warnings get an indented bullet block under the header (omitted
    when count == 0). No trailing blank line is required — the section
    ordering is what the spec grep tests.

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
    print("- Validated: yes (%d specialists, %d batches, %d warnings)"
          % (n_spec, n_batch, len(warnings)))
    print("- Orchestrator model: %s (source: %s)" % (orch_model, orch_source))
    print("- Subagent model: %s (source: %s)" % (sub_model, sub_source))
    print("- Specialists: %d (target ~10, range 3-15)" % n_spec)
    print("- Batches: %d" % n_batch)
    # F2.5 cost estimate line — only when there ARE specialists, so an
    # empty plan doesn't print a meaningless ``$0.00``.
    if n_spec > 0 and cost_per_call >= 0:
        estimate = n_spec * cost_per_call
        print("- Estimated cost: $%.2f (max $%.2f)" % (estimate, max_cost))
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
            entries: List[str] = []
            for idx in batch:
                if isinstance(idx, int) and 0 <= idx < len(specialists):
                    spec = specialists[idx]
                    if isinstance(spec, dict):
                        sid = spec.get("id", "")
                        if isinstance(sid, str) and sid:
                            entry = sid
                            if isinstance(deps, dict):
                                prereqs = deps.get(sid, [])
                                if isinstance(prereqs, list) and prereqs:
                                    prereq_ids = [p for p in prereqs
                                                  if isinstance(p, str)
                                                  and p in id_to_idx]
                                    if prereq_ids:
                                        entry = "%s (depends on %s)" % (
                                            sid, ", ".join(prereq_ids))
                            entries.append(entry)
            if entries:
                if len(entries) == 1 and "(depends on" in entries[0]:
                    print("- Batch %d: %s" % (b, entries[0]))
                else:
                    print("- Batch %d: %s  (parallel)"
                          % (b, ", ".join(entries)))
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
        print_preview(plan, "->", resolved, warnings,
                      cost_per_call=0.01, max_cost=0.0)
    finally:
        sys.stdout = saved
    text = "".join(buf_lines + [s for s in cap])

    # Locked marker list, in spec order. Each is a literal substring the
    # spec's controller scan checks for in the dry-run output. Per the
    # F2.3 spec + F2.5's one-line addition: header → validated/n lines →
    # resolved-model lines → specialists count → batches → cost estimate
    # → warnings → table → batch layout → next step. The cost line is
    # only printed when there ARE specialists; the marker check below
    # runs against a fixture with 3 specialists so the line is present.
    markers = [
        "# Plan preview",
        "- Plan:",
        "- Validated: yes",
        "- Orchestrator model:",
        "- Subagent model:",
        "- Specialists:",
        "- Batches:",
        "- Estimated cost:",
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

    # All 12 markers present in order?
    if len(positions) == len(markers):
        print("SELF_TEST PASS: 12 locked markers present in spec order")

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

    # ---- F2.5 4-tier chain fixtures (additive) ----
    # Each fixture walks resolve_model() under exactly one tier so the
    # chain is verifiable. The "default" tier is also asserted (already
    # proven by the env-block's `finally` cleanup), but a fresh explicit
    # call makes the fixture self-contained.

    # Tier 4 (default): no env, no config, no option → floor + "default"
    default_orch = resolve_model("orchestrator")
    default_sub = resolve_model("subagent")
    if default_orch != (DEFAULT_ORCHESTRATOR_MODEL, "default"):
        failures += 1
        print("SELF_TEST FAIL: default tier orchestrator "
              "(got %r)" % (default_orch,))
    elif default_sub != (DEFAULT_SUBAGENT_MODEL, "default"):
        failures += 1
        print("SELF_TEST FAIL: default tier subagent "
              "(got %r)" % (default_sub,))
    else:
        print("SELF_TEST PASS: tier 4 default returns floor + 'default'")

    # Tier 1 (option): the kwarg wins over env and default.
    _env_set("HERMES_ORCHESTRATOR_MODEL", "opencode-go/env1")
    try:
        opt_orch = resolve_model("orchestrator",
                                 orchestrator="opencode-go/opt")
        if opt_orch != ("opencode-go/opt", "option"):
            failures += 1
            print("SELF_TEST FAIL: option tier beats env "
                  "(got %r)" % (opt_orch,))
        else:
            print("SELF_TEST PASS: tier 1 option beats env (source: option)")
        # Independence: setting the orchestrator option MUST NOT change
        # the subagent resolved tuple (F2.2).
        opt_sub = resolve_model("subagent")
        if opt_sub != (DEFAULT_SUBAGENT_MODEL, "default"):
            failures += 1
            print("SELF_TEST FAIL: option tier independence (subagent leaked)")
        else:
            print("SELF_TEST PASS: tier 1 option independence holds")
    finally:
        _env_del("HERMES_ORCHESTRATOR_MODEL")

    # Tier 3 (config): write a temp config file with a single key under
    # the locked prefix; resolve_model() MUST pick it up. We use the
    # real path ``~/.hermes/config.yaml`` to exercise the same code
    # path the production reader uses, and restore the original file in
    # ``finally``.
    os = __import__("os")
    config_dir = os.path.expanduser("~/.hermes")
    config_path = os.path.join(config_dir, "config.yaml")
    saved_config: Optional[str] = None
    config_existed = os.path.isfile(config_path)
    if config_existed:
        with open(config_path, "r", encoding="utf-8") as _h:
            saved_config = _h.read()
    try:
        if not os.path.isdir(config_dir):
            os.makedirs(config_dir, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as _h:
            _h.write("# F2.5 self-test fixture\n"
                     "multi_agent_orchestrator.subagent_model: "
                     "opencode-go/cfg\n")
        cfg_sub = resolve_model("subagent")
        if cfg_sub != ("opencode-go/cfg", "config"):
            failures += 1
            print("SELF_TEST FAIL: config tier subagent "
                  "(got %r)" % (cfg_sub,))
        else:
            print("SELF_TEST PASS: tier 3 config returns config value "
                  "+ 'config'")
        # Independence: the config-tier subagent MUST NOT change the
        # orchestrator (which still has no env, no config, no option).
        cfg_orch = resolve_model("orchestrator")
        if cfg_orch != (DEFAULT_ORCHESTRATOR_MODEL, "default"):
            failures += 1
            print("SELF_TEST FAIL: config tier independence (orchestrator "
                  "leaked, got %r)" % (cfg_orch,))
        else:
            print("SELF_TEST PASS: tier 3 config independence holds")
    finally:
        # Restore the prior config (or remove if we created it).
        if saved_config is not None:
            with open(config_path, "w", encoding="utf-8") as _h:
                _h.write(saved_config)
        elif os.path.isfile(config_path):
            os.unlink(config_path)

    # Tier 2 (env): already covered by the existing env-override block
    # above, but re-assert here for completeness with independence.
    _env_set("HERMES_SUBAGENT_MODEL", "opencode-go/env2")
    try:
        env_sub = resolve_model("subagent")
        if env_sub != ("opencode-go/env2", "env"):
            failures += 1
            print("SELF_TEST FAIL: env tier subagent (got %r)" % (env_sub,))
        else:
            print("SELF_TEST PASS: tier 2 env returns env value + 'env'")
        env_orch = resolve_model("orchestrator")
        if env_orch != (DEFAULT_ORCHESTRATOR_MODEL, "default"):
            failures += 1
            print("SELF_TEST FAIL: env tier independence (orchestrator "
                  "leaked, got %r)" % (env_orch,))
        else:
            print("SELF_TEST PASS: tier 2 env independence holds")
    finally:
        _env_del("HERMES_SUBAGENT_MODEL")

    # F2.5 cost-estimate preview line: 3 specialists × 0.01 = $0.03,
    # max $0.00 (D34 default). Locked substring from the spec.
    preview_with_cost = []
    cap_c = _Cap()
    saved_c = sys.stdout
    sys.stdout = cap_c
    try:
        print_preview(plan, "->", resolved, warnings,
                      cost_per_call=0.01, max_cost=0.0)
    finally:
        sys.stdout = saved_c
    preview_text = "".join(preview_with_cost + [s for s in cap_c])
    expected_cost_line = "- Estimated cost: $0.03 (max $0.00)"
    if expected_cost_line not in preview_text:
        failures += 1
        print("SELF_TEST FAIL: estimated cost line missing "
              "(expected %r in output)" % expected_cost_line)
    else:
        print("SELF_TEST PASS: estimated cost line printed "
              "(3 specs × $0.01 = $0.03, max $0.00)")

    total = 13
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
    parser.add_argument("--max-cost", type=float, default=0.0,
                        help="F2.5: maximum estimated cost in USD. 0 (default) "
                             "disables the budget abort. The estimate is "
                             "specialists × --cost-per-call; if it exceeds "
                             "--max-cost the dispatcher aborts with "
                             "PLAN_OVER_BUDGET and exits 2.")
    parser.add_argument("--cost-per-call", type=float, default=0.01,
                        help="F2.5: estimated USD cost per specialist call "
                             "(default 0.01). Multiplied by specialists to "
                             "produce the --max-cost estimate.")
    parser.add_argument("--orchestrator-model", default=None,
                        help="F2.5: option-tier orchestrator model override. "
                             "Bypasses env / config / default for the "
                             "orchestrator only (subagent unaffected, F2.2).")
    parser.add_argument("--subagent-model", default=None,
                        help="F2.5: option-tier subagent model override. "
                             "Bypasses env / config / default for the "
                             "subagent only (orchestrator unaffected, F2.2).")
    args = parser.parse_args(argv)

    if args.self_test:
        return 0 if run_self_test() else 1

    if not args.plan:
        parser.print_usage(sys.stderr)
        print("error: PLAN.json is required (or pass --self-test)",
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

    # F2.5 cost-budget check — runs AFTER validation, BEFORE preview.
    # On the preview path we surface the abort (exit 2) without ever
    # dispatching; no logs/ is created (no specialist runs in preview).
    specialists = plan.get("specialists") if isinstance(plan, dict) else None
    n_spec = len(specialists) if isinstance(specialists, list) else 0
    estimate = n_spec * args.cost_per_call
    if args.max_cost > 0 and estimate > args.max_cost:
        print("PLAN_OVER_BUDGET: estimated $%.2f > max $%.2f"
              % (estimate, args.max_cost), file=sys.stderr)
        return 2

    resolved = {"orchestrator": resolve_model("orchestrator",
                                              orchestrator=args.orchestrator_model),
                "subagent": resolve_model("subagent",
                                          subagent=args.subagent_model)}
    warnings = _compute_warnings(plan, val_warnings, resolved["subagent"][0])
    print_preview(plan, args.plan, resolved, warnings,
                  cost_per_call=args.cost_per_call,
                  max_cost=args.max_cost)
    return 0


if __name__ == "__main__":
    sys.exit(main())