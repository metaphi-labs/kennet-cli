"""The closed schema `kennet codeval` sends against, and the check every outgoing body passes before it leaves.

`data/codeval_schema.json` is the server's own export (kennet `tests/data/codeval_schema.json`): the profile, the
results and the instructions the server answers with. `check` reads it strictly. A keyword it does not know is an error,
and a string is accepted only where an enum, a const or a pattern bounds it: any free text, as a value or as a key no
schema names, is refused. What the schema cannot say cannot be sent.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

from metaphi import errors

PATH = Path(__file__).with_name("data") / "codeval_schema.json"
PROFILE = "kennet.codeval.profile.v1"
RESULTS = "kennet.codeval.results.v1"
KNOWN = frozenset(
    [
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "default",
        "description",
        "enum",
        "items",
        "maxItems",
        "maxProperties",
        "maximum",
        "minItems",
        "minimum",
        "pattern",
        "patternProperties",
        "properties",
        "propertyNames",
        "required",
        "title",
        "type",
    ]
)


class Refused(errors.MetaphiError):
    """A body that does not fit the schema; nothing was sent."""

    def __init__(self, where: str, why: str):
        super().__init__(f"kennet codeval refused to send {where}: {why}", code="schema_refused")
        self.where, self.why = where, why


@functools.cache
def defs() -> dict[str, Any]:
    return json.loads(PATH.read_text())["$defs"]


def check(name: str, body: Any) -> None:
    """Raise `Refused` unless `body` is a `name` document (Profile, Results or Next) the schema holds exactly."""
    _check(defs()[name], body, name)


def _is(kind: str, value: Any) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}[kind])


def _bounded(s: dict[str, Any]) -> bool:
    return "enum" in s or "const" in s or "pattern" in s


def _check(s: dict[str, Any], v: Any, at: str) -> None:
    if unknown := set(s) - KNOWN:
        raise Refused(at, f"the schema uses {min(unknown)}, which this client does not read")
    if "$ref" in s:
        ref = s["$ref"]
        if not ref.startswith("#/$defs/") or ref[8:] not in defs():
            raise Refused(at, f"the schema names {ref}, which it does not hold")
        _check(defs()[ref[8:]], v, at)
        return
    if "anyOf" in s:
        near: Refused | None = None
        for option in s["anyOf"]:
            try:
                _check(option, v, at)
                return
            except Refused as e:
                if near is None or "type" not in option or _is(option["type"], v):
                    near = e
        raise near or Refused(at, "the schema allows nothing here")
    if "const" in s and v != s["const"]:
        raise Refused(at, f"it must be {s['const']!r}")
    if "enum" in s and v not in s["enum"]:
        raise Refused(at, "it is not one of the values the schema lists")
    kind = s.get("type")
    if kind is not None and not _is(kind, v):
        raise Refused(at, f"it must be {kind}")
    if isinstance(v, str):
        if not _bounded(s):
            raise Refused(at, "free text is never sent")
        if "pattern" in s and not re.search(s["pattern"], v):
            raise Refused(at, "it does not match the schema's pattern")
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            raise Refused(at, f"it is below {s['minimum']}")
        if "maximum" in s and v > s["maximum"]:
            raise Refused(at, f"it is above {s['maximum']}")
    elif isinstance(v, list):
        if len(v) < s.get("minItems", 0) or len(v) > s.get("maxItems", len(v)):
            raise Refused(at, "it holds too few or too many items")
        if "items" not in s:
            raise Refused(at, "the schema does not say what it holds")
        for i, x in enumerate(v):
            _check(s["items"], x, f"{at}[{i}]")
    elif isinstance(v, dict):
        _object(s, v, at)


def _object(s: dict[str, Any], v: dict[str, Any], at: str) -> None:
    if len(v) > s.get("maxProperties", len(v)):
        raise Refused(at, "it holds too many keys")
    props = s.get("properties", {})
    missing = [k for k in s.get("required", []) if k not in v]
    if missing:
        raise Refused(at, f"it lacks {missing[0]}")
    names = s.get("propertyNames")
    extra = s.get("additionalProperties", True)
    for k, x in v.items():
        where = f"{at}.{k}"
        if not isinstance(k, str):
            raise Refused(where, "a key must be a string")
        if k in props:
            _check(props[k], x, where)
            continue
        matched = [p for p in s.get("patternProperties", {}) if re.search(p, k)]
        if matched:
            for p in matched:
                _check(s["patternProperties"][p], x, where)
            continue
        if names is None or extra is False or extra is True:
            raise Refused(where, "a key the schema does not name is never sent")
        _check(names, k, f"{where} (key)")
        _check(extra, x, where)
