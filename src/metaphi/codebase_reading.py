from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

Report = Literal["junit", "summary"]
Why = Literal["timeout", "service", "config", "dependency", "toolchain", "compile", "crash", "failing", "other"]

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


def _all(pattern: str, t: str) -> list[tuple[str, ...]]:
    return re.findall(pattern, t, re.MULTILINE)


def _lein(t: str) -> tuple[int, int] | None:
    m = _all(r"^Ran (\d+) tests? containing \d+ assertions?\.\s*\n(\d+) failures?, (\d+) errors?", t)
    return (int(m[-1][0]), max(0, int(m[-1][0]) - int(m[-1][1]) - int(m[-1][2]))) if m else None


def _julia(t: str) -> tuple[int, int] | None:
    head = list(re.finditer(r"^Test Summary:\s*\|([^\n]*)\n([^\n]*)", t, re.MULTILINE))
    if not head:
        return None
    cols = [c for c in head[-1][1].split() if c != "Time"]
    nums = [int(x) for x in head[-1][2].split("|", 1)[-1].split() if x.isdigit()]
    got = dict(zip(cols, nums)) if nums and len(nums) <= len(cols) else {}
    total = got.get("Total") or sum(v for k, v in got.items() if k in ("Pass", "Fail", "Error", "Broken"))
    return (total, got.get("Pass", 0)) if total else None


def _mix(t: str) -> tuple[int, int] | None:
    if not re.search(r"^Finished in [\d.]+ seconds", t, re.MULTILINE):
        return None
    m = _all(r"^(?:\d+ doctests?, )?(?:\d+ propert(?:y|ies), )?(\d+) tests?, (\d+) failures?", t)
    return (int(m[-1][0]), int(m[-1][0]) - int(m[-1][1])) if m else None


def _dotnet(t: str) -> tuple[int, int] | None:
    m = _all(r"^\s*(?:Passed|Failed)!\s+- Failed:\s+(\d+), Passed:\s+(\d+), Skipped:\s+\d+, Total:\s+\d+", t)
    return (sum(int(f) + int(p) for f, p in m), sum(int(p) for _, p in m)) if m else None


def _xctest(t: str) -> tuple[int, int] | None:
    m = _all(r"Executed (\d+) tests?, with (\d+) failures?", t)
    return (int(m[-1][0]), int(m[-1][0]) - int(m[-1][1])) if m else None


def _testthat(t: str) -> tuple[int, int] | None:
    m = _all(r"\[ FAIL (\d+) \| WARN \d+ \| SKIP \d+ \| PASS (\d+) \]", t)
    return (int(m[-1][0]) + int(m[-1][1]), int(m[-1][1])) if m else None


def _phpunit(t: str) -> tuple[int, int] | None:
    if m := _all(r"^OK \((\d+) tests?, \d+ assertions?\)", t):
        return int(m[-1]), int(m[-1])
    m = _all(r"^Tests: (\d+), Assertions: \d+(?:, Errors: (\d+))?(?:, Failures: (\d+))?", t)
    return (int(m[-1][0]), int(m[-1][0]) - int(m[-1][1] or 0) - int(m[-1][2] or 0)) if m else None


def _sbt(t: str) -> tuple[int, int] | None:
    m = _all(r"(?:Passed|Failed): Total (\d+), Failed (\d+), Errors (\d+)", t)
    return (sum(int(n) for n, _, _ in m), sum(int(n) - int(f) - int(e) for n, f, e in m)) if m else None


def _dart(t: str) -> tuple[int, int] | None:
    m = _all(r"\+(\d+)(?: ~\d+)?(?: -(\d+))?: (?:All tests passed!|Some tests failed\.)", t)
    return (int(m[-1][0]) + int(m[-1][1] or 0), int(m[-1][0])) if m else None


def _busted(t: str) -> tuple[int, int] | None:
    m = _all(r"^(\d+) successes? / (\d+) failures? / (\d+) errors?", t)
    return (sum(map(int, m[-1])), int(m[-1][0])) if m else None


def _scalatest(t: str) -> tuple[int, int] | None:
    m = _all(r"Tests: succeeded (\d+), failed (\d+), canceled (\d+)", t)
    return (sum(int(a) + int(b) + int(c) for a, b, c in m), sum(int(a) for a, _, _ in m)) if m else None


def _mocha(t: str) -> tuple[int, int] | None:
    runs = _all(r"^\s*(\d+) passing \([^)]*\)\s*\n(?:\s*\d+ pending\s*\n)?(?:\s*(\d+) failing)?", t)
    return (sum(int(p) + int(f or 0) for p, f in runs), sum(int(p) for p, _ in runs)) if runs else None


def _ounit(t: str) -> tuple[int, int] | None:
    runs = _all(r"^Ran: (\d+) tests? in: [\d.]+ seconds\.\s*\n(?:OK|FAILED: Cases: \d+ Tried: \d+ Errors: (\d+) "
                r"Failures: (\d+))", t)
    return (sum(int(n) for n, _, _ in runs), sum(int(n) - int(e or 0) - int(f or 0) for n, e, f in runs)) if runs else None


def _alcotest(t: str) -> tuple[int, int] | None:
    runs = _all(r"^(?:Test Successful|(\d+) failures?!) in [\d.]+s\. (\d+) tests? run\.", t)
    return (sum(int(n) for _, n in runs), sum(int(n) - int(f or 0) for f, n in runs)) if runs else None


def _mtp(t: str) -> tuple[int, int] | None:
    m = _all(r"^\s*total: (\d+)\s*\n\s*failed: (\d+)\s*\n\s*succeeded: (\d+)", t)
    return (sum(int(n) for n, _, _ in m), sum(int(p) for _, _, p in m)) if m else None


def _ruby(t: str) -> tuple[int, int] | None:
    unit = _all(r"^(\d+) (?:tests|runs), \d+ assertions, (\d+) failures, (\d+) errors", t)
    if unit:
        return sum(int(n) for n, _, _ in unit), sum(int(n) - int(f) - int(e) for n, f, e in unit)
    spec = _all(r"^(\d+) examples?, (\d+) failures?", t)
    return (sum(int(n) for n, _ in spec), sum(int(n) - int(f) for n, f in spec)) if spec else None


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
    _lein,
    _julia,
    _mix,
    _dotnet,
    _xctest,
    _testthat,
    _phpunit,
    _sbt,
    _dart,
    _busted,
    _scalatest,
    _mocha,
    _ounit,
    _alcotest,
    _mtp,
    _ruby,
)


def counted(log: Path) -> tuple[int, int] | None:
    t = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", _text(log)).replace("\r\n", "\n")
    for read in READERS:
        got = read(t)
        if got and got[0] > 0:
            return got
    return None


# what a failed run's log says stopped it, first match wins: a service it could not reach, configuration it was not
# given, then the build's own causes; a test run that counted nothing crashed, one that counted failures is failing
SERVICE = re.compile(
    r"connection refused|ECONNREFUSED|dial tcp \S+: connect|could not connect to (?:server|database|redis|broker)|"
    r"Can't connect to (?:local )?MySQL|getaddrinfo ENOTFOUND|no such host|Name or service not known|"
    r"Communications link failure|Is the server running|run out of available brokers|NoBrokersAvailable|"
    r"Could not connect to Redis|MongoServerSelectionError|ServerSelectionTimeoutError|Connection reset by peer",
    re.IGNORECASE)
CONFIG = re.compile(
    r"(?:environment variable|env var|\bconfig(?:uration)?\b|_CONFIG\b|\.env\b)[^\n]{0,60}"
    r"(?:not set|is missing|missing|not proper|not found|is required|required|invalid|undefined)|"
    r"(?:missing|required|not set)[^\n]{0,30}(?:environment variable|env var)|"
    r"no such file or directory[^\n]*\.(?:ya?ml|json|toml|env|ini|conf|properties)\b|KeyError: '[A-Z][A-Z0-9_]+'",
    re.IGNORECASE)
CRASH = re.compile(r"^panic: |Traceback \(most recent call last\)|Exception in thread \"main\"|Unhandled exception|"
                   r"segmentation fault|core dumped|SIGSEGV|fatal error: ", re.MULTILINE | re.IGNORECASE)
WHY: tuple[tuple[Why, re.Pattern[str]], ...] = (
    ("service", SERVICE),
    ("config", CONFIG),
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
        r"error TS\d+|SyntaxError|compilation failed|\*\*\* \[.*\] Error \d+|build failed", re.IGNORECASE)),
)


def why(code: int | None, log: Path, counts: tuple[int, int] | None = None, test: bool = False) -> Why:
    """`test`: a test run that did not pass in full; `counts` what counted it (tests, passed), None: nothing did."""
    if code is None:
        return "timeout"
    t = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", _text(log)[-60000:])
    for name, pattern in WHY[:2] if test else WHY:
        if pattern.search(t):
            return name
    if test:
        if counts and counts[1] > 0:
            return "failing"
        if CRASH.search(t):
            return "crash"
        for name, pattern in WHY[2:]:
            if pattern.search(t):
                return name
        return "failing" if counts else "other"
    return "other"


__all__ = ["CONFIG", "CRASH", "READERS", "SERVICE", "WHY", "Report", "Why", "counted", "why"]
