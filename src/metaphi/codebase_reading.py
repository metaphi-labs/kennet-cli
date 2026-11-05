from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

Report = Literal["junit", "summary"]
Why = Literal["timeout", "dependency", "toolchain", "compile", "other"]

TAIL = 256_000


def _text(log: Path) -> str:
    try:
        with log.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - TAIL))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _n(m: re.Match | None, group: str) -> int:
    return int(m.group(group)) if m and m.group(group) else 0


def _pytest(t: str) -> tuple[int, int] | None:
    lines = re.findall(r"^=+ (.*\d+ (?:passed|failed|errors?).*) in [\d.]+s.*=+$", t, re.MULTILINE)
    if not lines:
        return None
    got = dict.fromkeys(("passed", "failed", "error", "errors"), 0)
    for n, kind in re.findall(r"(\d+) (passed|failed|errors?)\b", lines[-1]):
        got[kind] += int(n)
    return got["passed"] + got["failed"] + got["error"] + got["errors"], got["passed"]


def _unittest(t: str) -> tuple[int, int] | None:
    ran = re.findall(r"^Ran (\d+) tests? in [\d.]+s\s*\n\s*\n(OK|FAILED \(([^)]*)\))", t, re.MULTILINE)
    if not ran:
        return None
    total, _, detail = ran[-1]
    bad = sum(int(n) for _, n in re.findall(r"(failures|errors)=(\d+)", detail))
    return int(total), int(total) - bad


def _summed(pattern: str, t: str, total: str, passed: str) -> tuple[int, int] | None:
    found = list(re.finditer(pattern, t, re.MULTILINE))
    return (sum(_n(m, total) for m in found), sum(_n(m, passed) for m in found)) if found else None


def _last(pattern: str, t: str, total: str, *failed: str) -> tuple[int, int] | None:
    found = list(re.finditer(pattern, t, re.MULTILINE))
    if not found:
        return None
    m = found[-1]
    return _n(m, total), _n(m, total) - sum(_n(m, f) for f in failed)


def _go(t: str) -> tuple[int, int] | None:
    passed, failed = len(re.findall(r"^\s*--- PASS: ", t, re.MULTILINE)), len(re.findall(r"^\s*--- FAIL: ", t, re.MULTILINE))
    if passed + failed:
        return passed + failed, passed
    ok, bad = len(re.findall(r"^ok\s+\S+\s+[\d.]+s", t, re.MULTILINE)), len(re.findall(r"^FAIL\s+\S+\s+[\d.]+s", t, re.MULTILINE))
    return (ok + bad, ok) if ok + bad else None


def _meson(t: str) -> tuple[int, int] | None:
    ok = list(re.finditer(r"^Ok:\s+(\d+)\s*$", t, re.MULTILINE))
    if not ok:
        return None
    rest = t[ok[-1].start():]
    bad = sum(int(m.group(1)) for m in re.finditer(r"^(?:Fail|Unexpected Pass|Timeout):\s+(\d+)\s*$", rest, re.MULTILINE))
    return int(ok[-1].group(1)) + bad, int(ok[-1].group(1))


def _cargo(t: str) -> tuple[int, int] | None:
    found = re.findall(r"^test result: \w+\. (\d+) passed; (\d+) failed", t, re.MULTILINE)
    if not found:
        return None
    p, f = sum(int(a) for a, _ in found), sum(int(b) for _, b in found)
    return p + f, p


def _jest(t: str) -> tuple[int, int] | None:
    m = list(re.finditer(r"^Tests:\s+(?:(?P<f>\d+) failed, )?(?:\d+ skipped, )?(?:\d+ todo, )?(?:(?P<p>\d+) passed, )?"
                         r"(?P<n>\d+) total", t, re.MULTILINE))
    return (_n(m[-1], "p") + _n(m[-1], "f"), _n(m[-1], "p")) if m else None


def _maven(t: str) -> tuple[int, int] | None:
    found = re.findall(r"Results:\s*\n(?:.*\n){0,40}?.*Tests run: (\d+), Failures: (\d+), Errors: (\d+), "
                       r"Skipped: (\d+)", t)
    if not found:
        found = re.findall(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$", t,
                           re.MULTILINE)[-1:]
    if not found:
        return None
    ran = sum(int(n) - int(k) for n, _, _, k in found)
    return ran, ran - sum(int(f) + int(e) for _, f, e, _ in found)


def _tap(t: str) -> tuple[int, int] | None:
    p, f = re.findall(r"^# pass\s+(\d+)\s*$", t, re.MULTILINE), re.findall(r"^# fail\s+(\d+)\s*$", t, re.MULTILINE)
    if not p:
        return None
    return int(p[-1]) + int(f[-1] if f else 0), int(p[-1])


READERS = (
    _pytest,
    _unittest,
    lambda t: _last(r"^\d+% tests passed, (?P<f>\d+) tests? failed out of (?P<n>\d+)", t, "n", "f"),
    _meson,
    lambda t: _summed(r"^# TOTAL: (?P<n>\d+)\s*\n# PASS: +(?P<p>\d+)", t, "n", "p"),
    _cargo,
    _go,
    _maven,
    _jest,
    _tap,
)


def counted(log: Path) -> tuple[int, int] | None:
    t = _text(log)
    for read in READERS:
        got = read(t)
        if got and got[0] > 0:
            return got
    return None


WHY: tuple[tuple[Why, re.Pattern[str]], ...] = (
    ("dependency", re.compile(
        r"Could not resolve|No matching distribution|Could not find (?:artifact|a version)|unable to access '|"
        r"Failed to fetch|404 Not Found|failed to download|no matching version|ERESOLVE|unknown revision|"
        r"Temporary failure in name resolution|Could not resolve host|failed to select a version|"
        r"ResolutionImpossible|checksum mismatch", re.IGNORECASE)),
    ("toolchain", re.compile(
        r"command not found|: not found\s*$|No CMAKE_\w+_COMPILER could be found|Could NOT find \w+|"
        r"No package '[^']+' found|Package '?[\w.+-]+'?,? .*not found|is not installed|"
        r"requires? (?:at least )?(?:CMake|Python|gcc|clang|go|rustc|Java|JDK|node)\b.*version|"
        r"unsupported (?:compiler|version)|No such file or directory.*(?:gcc|g\+\+|cc|clang|gfortran|nasm|yasm)\b",
        re.MULTILINE | re.IGNORECASE)),
    ("compile", re.compile(
        r"\berror(?:\[\w+\])?:|\bError:|fatal error|undefined reference|cannot find symbol|"
        r"error TS\d+|SyntaxError|compilation failed|\*\*\* \[.*\] Error \d+", re.IGNORECASE)),
)


def why(code: int | None, log: Path) -> Why:
    if code is None:
        return "timeout"
    t = _text(log)[-20000:]
    for name, pattern in WHY:
        if pattern.search(t):
            return name
    return "other"


__all__ = ["READERS", "WHY", "Report", "Why", "counted", "why"]
