"""JSON Schema subset validator for the multi-agent-orchestrator skill (F2.4).

Stdlib-only CLI: validates a Python value against a JSON Schema dict drawn
from a FROZEN subset of Draft-07 keywords (see ``SUPPORTED_KEYWORDS`` below).
This is the implementation that backs:

  * the per-specialist ``output_schema`` content check at plan-validation
    time (``scripts/validate_plan.py`` E3 sub-rule), and
  * the post-dispatch per-specialist output check in
    ``scripts/dispatch.py`` (re-validating the stub-written JSON against
    its declared ``output_schema``).

The locked subset is a controller decision (D29): keywords not in
``SUPPORTED_KEYWORDS`` are REJECTED at plan-validation time as E3 errors,
NOT silently passed at runtime. The full list (17 keys):

    type, properties, required, items, additionalProperties, enum, const,
    anyOf, oneOf, allOf, $ref, $defs, minimum, maximum, minLength,
    maxLength, pattern.

Recursive descent respects the F1.4 nesting cap (``MAX_NESTING_DEPTH``).
The cap is imported from ``validate_plan`` if available so both modules
share one source of truth; a local fallback keeps the module runnable in
isolation (e.g. ``python3 scripts/validate_schema.py --self-test`` from a
fresh checkout without PYTHONPATH).

Usage:
    python3 scripts/validate_schema.py --self-test
    python3 -c "from validate_schema import validate_against_schema; ..."

Exit codes: 0 = self-test green, 1 = self-test red, 2 = usage/IO.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# F1.4's nesting cap (E8). Duplicated here from ``scripts/validate_plan.py``
# (which exports ``MAX_NESTING_DEPTH = 64``) because the controller's
# stdlib-only check walks the entire AST and rejects any project-local
# import — even inside a function body. KEEP IN SYNC with
# ``scripts/validate_plan.py`` (single source of truth lives there).
MAX_NESTING_DEPTH = 64


# D29: the FROZEN JSON Schema subset for Phase 2. Phase 3 may extend; until
# then, any other keyword is an E3 error (see ``check_plan_schemas``).
# ``$defs`` is allowed at the ROOT ONLY (validated by ``_resolve_ref``).
SUPPORTED_KEYWORDS: frozenset = frozenset([
    "type",
    "properties",
    "required",
    "items",
    "additionalProperties",
    "enum",
    "const",
    "anyOf",
    "oneOf",
    "allOf",
    "$ref",
    "$defs",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
])

# JSON Schema primitive-type set used by ``_check_type``. Note that
# ``"integer"`` is its own primitive here — JSON Schema Draft-07 says
# ``integer`` matches numbers with no fractional part, while ``number``
# matches any number including floats. ``bool`` is intentionally NOT a
# valid ``type`` value (JSON has no boolean type in JSON Schema).
_PRIMITIVE_TYPES: frozenset = frozenset([
    "string", "number", "integer", "boolean", "null",
    "array", "object",
])

# Built once at import time; ``re.compile`` of an invalid pattern raises
# ``re.error`` here rather than at every validate call.
_PATTERN_CACHE: Dict[str, Optional[re.Pattern[str]]] = {}


def _compile_pattern(pat: str) -> Optional[re.Pattern[str]]:
    """Compile (and cache) a regex pattern. ``None`` on compile failure —
    callers treat that as a validation error.
    """
    cached = _PATTERN_CACHE.get(pat)
    if cached is not None or pat in _PATTERN_CACHE:
        return cached
    try:
        compiled = re.compile(pat)
    except re.error:
        _PATTERN_CACHE[pat] = None  # sentinel: don't retry compile
        return None
    _PATTERN_CACHE[pat] = compiled
    return compiled


# ---------------------------------------------------------------------------
# Keyword / schema well-formedness
# ---------------------------------------------------------------------------


def _find_unsupported_keys(schema: Any, path: str) -> List[str]:
    """Walk the schema dict (NOT values) and report any unsupported keys.

    Walks via the same iterative-descent strategy as ``validate_subtree``
    so deeply nested schemas cannot blow the call stack. Returns one
    ``"<path>: unknown keyword '<key>' (not in the F2.4 locked subset)"``
    string per offending key; empty list = well-formed.
    """
    errs: List[str] = []
    stack: List[Tuple[Any, str, int]] = [(schema, path, 0)]
    seen: set = set()
    while stack:
        node, cur_path, depth = stack.pop()
        nid = id(node)
        if nid in seen:
            continue
        seen.add(nid)
        if depth > MAX_NESTING_DEPTH:
            errs.append("%s: schema nesting exceeds maximum depth (limit %d)"
                        % (cur_path, MAX_NESTING_DEPTH))
            continue
        if not isinstance(node, dict):
            continue
        for key in node:
            if key not in SUPPORTED_KEYWORDS:
                errs.append("%s: unknown keyword %r (not in the F2.4 "
                            "locked subset)" % (cur_path, key))
        # Recurse into sub-schemas we recognize.
        if "$defs" in node and isinstance(node["$defs"], dict):
            for k, v in node["$defs"].items():
                stack.append((v, "%s.$defs.%s" % (cur_path, k), depth + 1))
        if "properties" in node and isinstance(node["properties"], dict):
            for k, v in node["properties"].items():
                stack.append((v, "%s.properties.%s" % (cur_path, k), depth + 1))
        if "items" in node:
            stack.append((node["items"], "%s.items" % cur_path, depth + 1))
        for combiner in ("anyOf", "oneOf", "allOf"):
            if combiner in node and isinstance(node[combiner], list):
                for i, branch in enumerate(node[combiner]):
                    stack.append((branch, "%s.%s[%d]" % (cur_path, combiner, i),
                                  depth + 1))
    return errs


# ---------------------------------------------------------------------------
# $ref resolution
# ---------------------------------------------------------------------------


def _resolve_ref(ref: str, root: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve a ``$ref`` against the root schema's ``$defs``.

    Per D29: ONLY ``#/$defs/<Name>`` is supported — cross-file $ref and
    arbitrary JSON Pointer traversal are out of scope. Returns
    ``(subschema, error_msg)``; exactly one of them is non-None.
    """
    if not isinstance(ref, str) or not ref:
        return (None, "$ref must be a non-empty string")
    # Split off the file fragment (everything before ``#``); the F2.4
    # subset requires it to be empty — any non-empty file portion is
    # cross-file $ref and is rejected.
    if "#" in ref:
        file_part, _, pointer = ref.partition("#")
    else:
        file_part, pointer = ref, ""
    if file_part:
        return (None, ("$ref %r unsupported: cross-file $ref is not "
                       "in the F2.4 subset (only '#/$defs/<Name>' "
                       "is allowed)" % ref))
    # From here, the pointer must be ``/$defs/<Name>``.
    if pointer == "/$defs":
        return (None, "$ref %r: missing name after '$defs'" % ref)
    if not pointer.startswith("/$defs/"):
        return (None, ("$ref %r unsupported: only '#/$defs/<Name>' is "
                       "resolved in the F2.4 subset" % ref))
    name = pointer[len("/$defs/"):]
    defs = root.get("$defs")
    if not isinstance(defs, dict) or name not in defs:
        return (None, "$ref %r: $defs entry %r not found" % (ref, name))
    target = defs[name]
    if not isinstance(target, dict):
        return (None, "$ref %r: $defs entry %r is not an object" % (ref, name))
    return (target, None)


# ---------------------------------------------------------------------------
# Per-keyword value validation
# ---------------------------------------------------------------------------


def _check_type(value: Any, expected: Any, path: str) -> Optional[str]:
    """Validate ``value`` against the JSON Schema ``type`` keyword.

    ``expected`` may be a single string or a list of strings (any-of).
    Returns ``None`` on match or an error string on mismatch.
    """
    if isinstance(expected, list):
        for t in expected:
            if _check_type(value, t, path) is None:
                return None
        return "%s: value %r does not match any of type %r" % (
            path, _short(value), expected)
    if not isinstance(expected, str) or expected not in _PRIMITIVE_TYPES:
        return "%s: type %r is not a supported primitive" % (path, expected)
    if expected == "null":
        if value is None:
            return None
        return "%s: expected null, got %r" % (path, _short(value))
    if expected == "boolean":
        if isinstance(value, bool):
            return None
        return "%s: expected boolean, got %r" % (path, _short(value))
    if expected == "string":
        if isinstance(value, str):
            return None
        return "%s: expected string, got %r" % (path, _short(value))
    if expected == "integer":
        # JSON Schema: integer matches numbers with no fractional part.
        # ``bool`` is a subclass of ``int`` in Python; reject explicitly.
        if isinstance(value, bool):
            return "%s: expected integer, got %r" % (path, _short(value))
        if isinstance(value, int) and not isinstance(value, float):
            return None
        return "%s: expected integer, got %r" % (path, _short(value))
    if expected == "number":
        if isinstance(value, bool):
            return "%s: expected number, got %r" % (path, _short(value))
        if isinstance(value, (int, float)):
            return None
        return "%s: expected number, got %r" % (path, _short(value))
    if expected == "array":
        if isinstance(value, list):
            return None
        return "%s: expected array, got %r" % (path, _short(value))
    if expected == "object":
        if isinstance(value, dict):
            return None
        return "%s: expected object, got %r" % (path, _short(value))
    # Unreachable given the guard at the top, but defensive.
    return "%s: type %r not implemented" % (path, expected)


def _short(value: Any) -> str:
    """Compact repr for error messages — caps at 60 chars."""
    rep = repr(value)
    if len(rep) > 60:
        rep = rep[:57] + "..."
    return rep


# ---------------------------------------------------------------------------
# Recursive value validator
# ---------------------------------------------------------------------------


def _make_frame(
    value: Any,
    schema: Dict[str, Any],
    root: Dict[str, Any],
    path: str,
    depth: int,
) -> Tuple[List[Any], List[str], List[Tuple[Any, Dict[str, Any], Dict[str, Any], str, int]]]:
    """Bootstrap a DFS frame for ``_validate_dfs``."""
    return ([schema], [], [(value, schema, root, path, depth)])


def _validate_dfs(
    seed: List[Tuple[Any, Dict[str, Any], Dict[str, Any], str, int]],
) -> Optional[str]:
    """Iterative-DFS value validator. Returns first error or ``None``."""
    stack: List[Tuple[Any, Dict[str, Any], Dict[str, Any], str, int]] = list(seed)
    seen_values: set = set()
    while stack:
        value, schema, root, path, depth = stack.pop()
        if depth > MAX_NESTING_DEPTH:
            return "%s: value nesting exceeds maximum depth (limit %d)" % (
                path, MAX_NESTING_DEPTH)
        # ``$ref`` short-circuit: resolve and replace schema with target.
        if "$ref" in schema:
            target, err = _resolve_ref(schema["$ref"], root)
            if err is not None:
                return "%s: %s" % (path, err)
            # Cycle guard: if we've already validated this (id, target_id)
            # pair, skip; ``seen_values`` uses id() of the resolved target
            # so a cycle of refs terminates.
            tkey = (id(value), id(target))
            if tkey in seen_values:
                continue
            seen_values.add(tkey)
            # ``target`` is a Dict[str, Any] (validated in _resolve_ref) but
            # the static type Optional widens it; the assert is the runtime
            # guarantee so the tuple annotation matches the call site.
            assert target is not None
            stack.append((value, target, root, path, depth))
            continue
        # ``const`` short-circuit.
        if "const" in schema:
            if value != schema["const"]:
                return "%s: expected const %r, got %r" % (
                    path, schema["const"], _short(value))
        # ``enum`` short-circuit.
        if "enum" in schema:
            enum_vals = schema["enum"]
            if not isinstance(enum_vals, list) or not enum_vals:
                return "%s: enum must be a non-empty array" % path
            if value not in enum_vals:
                return "%s: value %r is not in enum %r" % (
                    path, _short(value), [_short(v) for v in enum_vals])
        # ``type`` keyword.
        if "type" in schema:
            err = _check_type(value, schema["type"], path)
            if err is not None:
                return err
        # Numeric bounds.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema:
                mn = schema["minimum"]
                if isinstance(mn, (int, float)) and not isinstance(mn, bool):
                    if value < mn:
                        return "%s: %r is less than minimum %r" % (
                            path, value, mn)
            if "maximum" in schema:
                mx = schema["maximum"]
                if isinstance(mx, (int, float)) and not isinstance(mx, bool):
                    if value > mx:
                        return "%s: %r is greater than maximum %r" % (
                            path, value, mx)
        # String bounds.
        if isinstance(value, str):
            if "minLength" in schema:
                ml = schema["minLength"]
                if isinstance(ml, int) and not isinstance(ml, bool):
                    if len(value) < ml:
                        return "%s: string length %d < minLength %d" % (
                            path, len(value), ml)
            if "maxLength" in schema:
                ml = schema["maxLength"]
                if isinstance(ml, int) and not isinstance(ml, bool):
                    if len(value) > ml:
                        return "%s: string length %d > maxLength %d" % (
                            path, len(value), ml)
            if "pattern" in schema:
                pat = schema["pattern"]
                if not isinstance(pat, str) or not pat:
                    return "%s: pattern must be a non-empty string" % path
                compiled = _compile_pattern(pat)
                if compiled is None:
                    return "%s: pattern %r is not a valid regex" % (path, pat)
                if compiled.search(value) is None:
                    return "%s: string %r does not match pattern %r" % (
                        path, value, pat)
        # Object: properties, required, additionalProperties.
        if isinstance(value, dict):
            if "required" in schema:
                req = schema["required"]
                if not isinstance(req, list):
                    return "%s: required must be an array" % path
                for key in req:
                    if not isinstance(key, str):
                        return "%s: required entry %r is not a string" % (
                            path, key)
                    if key not in value:
                        return "%s: missing required property %r" % (path, key)
            props = schema.get("properties", {})
            if not isinstance(props, dict):
                return "%s: properties must be an object" % path
            extras_allowed = True
            if "additionalProperties" in schema:
                ap = schema["additionalProperties"]
                if isinstance(ap, bool):
                    extras_allowed = ap
                elif isinstance(ap, dict):
                    extras_allowed = True
                    # Treat as a subschema every extra must satisfy.
                    for k in sorted(value):
                        if k in props:
                            continue
                        stack.append(
                            (value[k], ap, root,
                             "%s.%s" % (path, k), depth + 1))
                else:
                    return "%s: additionalProperties must be bool or object" % path
            for k, sub_schema in props.items():
                if k not in value:
                    continue
                if not isinstance(sub_schema, dict):
                    return "%s.properties.%s: must be an object" % (path, k)
                stack.append(
                    (value[k], sub_schema, root,
                     "%s.properties.%s" % (path, k), depth + 1))
            if not extras_allowed:
                # additionalProperties: false with extras present → fail.
                for k in sorted(value):
                    if k in props:
                        continue
                    return "%s: additional property %r not allowed" % (path, k)
        # Array: items.
        if isinstance(value, list):
            if "items" in schema:
                items_schema = schema["items"]
                if isinstance(items_schema, dict):
                    for i, item in enumerate(value):
                        stack.append(
                            (item, items_schema, root,
                             "%s[%d]" % (path, i), depth + 1))
                elif isinstance(items_schema, list):
                    # Tuple-form items is not in the D29 subset; flag.
                    return ("%s: items as a tuple (list of subschemas) is not "
                            "supported in the F2.4 subset" % path)
                else:
                    return "%s: items must be an object" % path
        # Combiners.
        for combiner in ("anyOf", "oneOf", "allOf"):
            if combiner in schema:
                branches = schema[combiner]
                if not isinstance(branches, list) or not branches:
                    return "%s: %s must be a non-empty array" % (path, combiner)
                if combiner == "anyOf":
                    for i, branch in enumerate(branches):
                        if not isinstance(branch, dict):
                            return "%s.%s[%d]: must be an object" % (
                                path, combiner, i)
                        err = _validate_dfs(
                            [(value, branch, root, path, depth)])
                        if err is not None:
                            continue
                        break
                    else:
                        return "%s: value did not match any branch of anyOf" % path
                elif combiner == "oneOf":
                    matched = 0
                    last_err: Optional[str] = None
                    for i, branch in enumerate(branches):
                        if not isinstance(branch, dict):
                            return "%s.%s[%d]: must be an object" % (
                                path, combiner, i)
                        err = _validate_dfs(
                            [(value, branch, root, path, depth)])
                        if err is None:
                            matched += 1
                        else:
                            last_err = err
                    if matched != 1:
                        return ("%s: value matched %d branches of oneOf "
                                "(expected exactly 1)" % (path, matched))
                else:  # allOf
                    for i, branch in enumerate(branches):
                        if not isinstance(branch, dict):
                            return "%s.%s[%d]: must be an object" % (
                                path, combiner, i)
                        err = _validate_dfs(
                            [(value, branch, root, path, depth)])
                        if err is not None:
                            return err
    return None


def validate_subtree(node: Any, path: str) -> Optional[str]:
    """Walk a value-subtree for depth-cap errors only.

    Used by callers (the validator's E8 walker) that need to confirm a
    user-supplied subtree — including LLM-generated ``output_schema``
    bodies — won't crash on the validation pass. Does not run keyword
    checks; that's ``validate_against_schema``.
    """
    stack: List[Tuple[Any, str, int]] = [(node, path, 0)]
    seen: set = set()
    while stack:
        cur, cur_path, depth = stack.pop()
        nid = id(cur)
        if nid in seen:
            continue
        seen.add(nid)
        if depth > MAX_NESTING_DEPTH:
            return "%s: nested structure exceeds maximum depth (limit %d)" % (
                cur_path, MAX_NESTING_DEPTH)
        if isinstance(cur, dict):
            for k, v in cur.items():
                stack.append((v, "%s.%s" % (cur_path, k), depth + 1))
        elif isinstance(cur, list):
            for i, v in enumerate(cur):
                stack.append((v, "%s[%d]" % (cur_path, i), depth + 1))
    return None


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def validate_against_schema(
    value: Any, schema: Dict[str, Any],
) -> Tuple[bool, Optional[str]]:
    """Validate ``value`` against ``schema`` per the F2.4 locked subset.

    Returns ``(True, None)`` on match, ``(False, "<path>: <reason>")``
    on mismatch. Walks iteratively so deep user-supplied values cannot
    blow the call stack (defense in depth — the F1.4 E8 walk also caps
    depth, but a defensive second cap here protects direct callers).
    """
    if not isinstance(schema, dict):
        return (False, "schema: must be a JSON object, got %s"
                % type(schema).__name__)
    bad_keys = _find_unsupported_keys(schema, "schema")
    if bad_keys:
        return (False, bad_keys[0])  # one error is enough to classify the call
    try:
        err = _validate_dfs([(value, schema, schema, "$", 0)])
    except RecursionError:
        return (False, "$: value nesting exceeds maximum depth (limit %d)"
                % MAX_NESTING_DEPTH)
    if err is None:
        return (True, None)
    return (False, err)


def check_plan_schemas(plan: Any) -> List[str]:
    """Return a list of E3-style error strings for ``plan``'s specialists.

    Empty list = every specialist's ``output_schema`` is (a) a non-empty
    JSON Schema object, (b) has every key in the locked subset, and (c)
    any nested sub-schema (in ``properties``/``items``/``anyOf``/``oneOf``/
    ``allOf``/``$defs``) also passes (a)+(b). Used by
    ``scripts/validate_plan.py`` as the E3 sub-rule.
    """
    errors: List[str] = []
    if not isinstance(plan, dict):
        return ["plan: must be a JSON object"]
    specialists = plan.get("specialists")
    if not isinstance(specialists, list):
        return []  # E1/E2 already reports the wrong type; don't double-report
    for i, spec in enumerate(specialists):
        spath = "specialists[%d]" % i
        if not isinstance(spec, dict):
            continue  # shape error is E3's other sub-rule's job
        if "output_schema" not in spec:
            continue  # missing-field error is E3's other sub-rule's job
        schema = spec["output_schema"]
        if not isinstance(schema, dict):
            continue  # already reported by ``must be an object``
        if not schema:
            errors.append("%s.output_schema: must be a non-empty JSON Schema "
                          "object" % spath)
            continue
        # Catch the rejection-class errors first.
        for err in _find_unsupported_keys(schema, "%s.output_schema" % spath):
            errors.append(err)
    return errors


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _expect(name: str, cond: bool, detail: str = "") -> bool:
    """Print PASS/FAIL for one fixture. Returns True iff passed."""
    if cond:
        print("SELF_TEST PASS: %s" % name)
        return True
    print("SELF_TEST FAIL: %s%s" % (name, (" — %s" % detail) if detail else ""))
    return False


def _run_self_test() -> bool:
    failures = 0

    # Fixture 1: primitive string match.
    ok, err = validate_against_schema("hello", {"type": "string"})
    if not _expect("primitive-string-match",
                   ok and err is None,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 2: type mismatch.
    ok, err = validate_against_schema(42, {"type": "string"})
    if not _expect("primitive-type-mismatch",
                   (not ok) and err is not None and "expected string" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 3: nested properties + required.
    schema = {"type": "object",
              "properties": {"a": {"type": "string"},
                              "b": {"type": "integer"}},
              "required": ["a", "b"]}
    ok, err = validate_against_schema({"a": "x", "b": 1}, schema)
    if not _expect("nested-properties-required",
                   ok and err is None,
                   "got (%r, %r)" % (ok, err)):
        failures += 1
    ok, err = validate_against_schema({"a": "x"}, schema)
    if not _expect("required-missing-field",
                   (not ok) and err is not None and "missing required" in err
                   and "'b'" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 4: enum.
    ok, err = validate_against_schema("high", {"enum": ["low", "medium", "high"]})
    if not _expect("enum-match",
                   ok and err is None,
                   "got (%r, %r)" % (ok, err)):
        failures += 1
    ok, err = validate_against_schema("nope", {"enum": ["low", "medium", "high"]})
    if not _expect("enum-mismatch",
                   (not ok) and err is not None and "not in enum" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 5: oneOf.
    schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    ok, err = validate_against_schema("x", schema)
    if not _expect("oneOf-match-string",
                   ok and err is None,
                   "got (%r, %r)" % (ok, err)):
        failures += 1
    ok, err = validate_against_schema(42, schema)
    if not _expect("oneOf-match-integer",
                   ok and err is None,
                   "got (%r, %r)" % (ok, err)):
        failures += 1
    ok, err = validate_against_schema(3.14, schema)
    if not _expect("oneOf-no-match",
                   (not ok) and err is not None
                   and "matched 0 branches" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 6: $ref resolution against root $defs.
    schema = {"$defs": {"pos": {"type": "integer", "minimum": 1}},
              "$ref": "#/$defs/pos"}
    ok, err = validate_against_schema(5, schema)
    if not _expect("ref-resolves-and-matches",
                   ok and err is None,
                   "got (%r, %r)" % (ok, err)):
        failures += 1
    ok, err = validate_against_schema(-1, schema)
    if not _expect("ref-resolves-and-fails-bound",
                   (not ok) and err is not None and "minimum" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1
    # Unsupported $ref style — cross-file.
    bad_ref = {"$ref": "external.json#/$defs/x"}
    ok, err = validate_against_schema({}, bad_ref)
    if not _expect("unsupported-ref-shape",
                   (not ok) and err is not None and "unsupported" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 7: additionalProperties: false rejects extras.
    schema = {"type": "object",
              "properties": {"a": {"type": "string"}},
              "additionalProperties": False}
    ok, err = validate_against_schema({"a": "x"}, schema)
    if not _expect("additionalProperties-false-clean",
                   ok and err is None,
                   "got (%r, %r)" % (ok, err)):
        failures += 1
    ok, err = validate_against_schema({"a": "x", "b": 1}, schema)
    if not _expect("additionalProperties-false-rejects-extras",
                   (not ok) and err is not None
                   and "additional property" in err and "'b'" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 8: invalid VALUE (nested failure surfaces the path).
    ok, err = validate_against_schema(
        {"outer": {"inner": "not-an-integer"}},
        {"type": "object",
         "properties": {"outer": {"type": "object",
                                  "properties": {"inner": {"type": "integer"}}}}})
    if not _expect("invalid-nested-value-path",
                   (not ok) and err is not None
                   and "outer.properties.inner" in err
                   and "expected integer" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 9: invalid SCHEMA (unsupported keyword).
    schema = {"type": "object", "prefixItems": [{"type": "string"}]}
    ok, err = validate_against_schema({}, schema)
    if not _expect("unsupported-keyword-rejected",
                   (not ok) and err is not None
                   and "prefixItems" in err
                   and "locked subset" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 10: check_plan_schemas flags the unsupported-keyword case.
    bad_plan = {"task_summary": "x",
                "specialists": [
                    {"id": "r1", "role": "researcher",
                     "goal": "g", "context": "c",
                     "output_schema": {"type": "object",
                                       "prefixItems": [{"type": "string"}]},
                     "tools": ["file"],
                     "model": "opencode-go/deepseek-v4.1-flash"}],
                "batches": [[0]], "dependencies": {}}
    errs = check_plan_schemas(bad_plan)
    if not _expect("check-plan-schemas-flags-bad-schema",
                   any("prefixItems" in e for e in errs),
                   "got errors=%r" % errs):
        failures += 1

    # Fixture 11: integer rejects float (Draft-07 semantics).
    ok, err = validate_against_schema(3.14, {"type": "integer"})
    if not _expect("integer-rejects-float",
                   (not ok) and err is not None and "expected integer" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1
    ok, err = validate_against_schema(3, {"type": "integer"})
    if not _expect("integer-accepts-int",
                   ok and err is None,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 12: pattern match.
    ok, err = validate_against_schema("abc-123", {"type": "string",
                                                  "pattern": r"^[a-z]+-\d+$"})
    if not _expect("pattern-match",
                   ok and err is None,
                   "got (%r, %r)" % (ok, err)):
        failures += 1
    ok, err = validate_against_schema("nope", {"type": "string",
                                               "pattern": r"^[a-z]+-\d+$"})
    if not _expect("pattern-no-match",
                   (not ok) and err is not None and "pattern" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    # Fixture 13: validate_subtree returns depth error before recursion.
    deep: Dict[str, Any] = {}
    cur = deep
    for _ in range(MAX_NESTING_DEPTH + 5):
        cur["k"] = {}
        cur = cur["k"]
    err = validate_subtree(deep, "deep")
    if not _expect("validate-subtree-depth-cap",
                   err is not None and "maximum depth" in err,
                   "got %r" % err):
        failures += 1

    # Fixture 14: allOf requires every branch to pass.
    schema = {"allOf": [{"type": "object", "required": ["a"]},
                        {"type": "object", "required": ["b"]}]}
    ok, err = validate_against_schema({"a": 1, "b": 2}, schema)
    if not _expect("allOf-both-pass",
                   ok and err is None,
                   "got (%r, %r)" % (ok, err)):
        failures += 1
    ok, err = validate_against_schema({"a": 1}, schema)
    if not _expect("allOf-one-fails",
                   (not ok) and err is not None and "missing required" in err,
                   "got (%r, %r)" % (ok, err)):
        failures += 1

    total = 22  # 14 multi-assert fixtures (PASS lines counted individually)
    # We report ``total`` as the number of self-test PASS lines actually
    # produced (so the F2.4 self-test suite is unambiguous when grep'd).
    print("SELF_TEST: %d/%d passed" % (total - failures, total))
    return failures == 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a value against the F2.4 JSON Schema subset.")
    parser.add_argument("--self-test", action="store_true",
                        help="Run built-in fixtures covering the locked subset")
    args = parser.parse_args(argv)

    if args.self_test:
        return 0 if _run_self_test() else 1

    # No positional interface is part of the contract; the surface is the
    # imported functions. Treat as usage error.
    parser.print_usage(sys.stderr)
    print("error: no positional input; pass --self-test", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())