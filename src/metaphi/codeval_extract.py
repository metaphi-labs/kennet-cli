"""From a build or test run to a few words from fixed lists, on this machine, by rules anyone can read.

CodeVal turns on the toolchain's own structured output where the build lets it (`go test -json`, cargo's
`--message-format=json`, GCC's SARIF, JUnit XML from pytest, Surefire and Gradle, Jest's `--json`, Bazel's build event
stream) and reads it here. What no reporter says is read from the log by the shipped rule pack
(`data/codeval_rules.json`): regular expressions and Drain-style templates, mapped to the classes; environmental ends
from the exit code, the signal, the wall clock and the memory limit, never from text; and code tables, where a
diagnostic code not in the table counts as `other`. The pack ships with this release and its sha256 goes with every
Results body as `rules`; the server never sends rules. No model is used.

Only the class, the step and the kind of compile error leave the machine, and only where the schema has a field for
them. The log, its lines, the codes and the messages stay here.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PATH = Path(__file__).parent / "data" / "codeval_rules.json"
TAIL = 512_000


@functools.cache
def pack() -> dict[str, Any]:
    return json.loads(PATH.read_bytes())


@functools.cache
def rules_hash() -> str:
    """The sha256 of the rule pack exactly as shipped: what every Results body names as `rules`."""
    return hashlib.sha256(PATH.read_bytes()).hexdigest()


@functools.cache
def _compiled() -> tuple[list[tuple[dict, re.Pattern]], list[tuple[dict, list[str]]], list[tuple[str, re.Pattern]]]:
    p = pack()
    patterns = [(r, re.compile(r["re"], re.MULTILINE)) for r in p["patterns"]]
    templates = [(t, t["t"].split()) for t in p["templates"]]
    kinds = [(k["kind"], re.compile(k["re"], re.MULTILINE | re.IGNORECASE)) for k in p["kinds"]]
    return patterns, templates, kinds


# -- turning structured output on ------------------------------------------------------------------------------------

_SEGMENT_END = re.compile(r"&&|\|\||[;|)]|$")
_TOOLS = (
    (re.compile(r"(?<![\w./-])go test(?=\s|$)"), "-json", ("-json",)),
    (
        re.compile(r"(?<![\w./-])cargo (?:\+\S+ )?(?:build|test|check|clippy)(?=\s|$)"),
        "--message-format=json-diagnostic-rendered-ansi",
        ("--message-format",),
    ),
    (
        re.compile(r"(?<![\w./-])(?:python3? -m )?pytest(?=\s|$)"),
        '--junit-xml="$KENNET_JUNIT/kennet-pytest.xml"',
        ("--junitxml", "--junit-xml"),
    ),
    (
        re.compile(r"(?<![\w./-])jest(?=\s|$)"),
        '--json --outputFile="$KENNET_JUNIT/kennet-jest.json"',
        ("--json", "--outputFile"),
    ),
    (
        re.compile(r"(?<![\w./-])bazel(?:isk)? (?:test|build|coverage)(?=\s|$)"),
        '--build_event_json_file="$KENNET_JUNIT/kennet-bep.json"',
        ("--build_event_json_file",),
    ),
)
GCC_SARIF = (
    "v=$(${CC:-cc} -dumpfullversion 2>/dev/null || true); "
    'case "$(${CC:-cc} --version 2>/dev/null)" in *clang*|"") ;; *) '
    'if [ "${v%%.*}" -ge 15 ] 2>/dev/null; then f=-fdiagnostics-add-output=sarif; '
    'elif [ "${v%%.*}" -ge 13 ] 2>/dev/null; then f=-fdiagnostics-format=sarif-file; else f=; fi; '
    '[ -n "$f" ] && export CFLAGS="{c} $f" CXXFLAGS="{cxx} $f";; esac; '
)


def structured(command: str) -> str:
    """`command` with the toolchain's structured output turned on for each tool it names directly, flags added right
    after the subcommand; a segment that already asks for the format is left as it is. Builds by CMake or a
    configure script also get GCC's SARIF (GCC 13 on; Clang's is not stable), with the flags they would have had."""
    out = command
    for pattern, flag, present in _TOOLS:
        pieces, at = [], 0
        for m in pattern.finditer(out):
            end = _SEGMENT_END.search(out, m.end()).start()
            pieces.append(out[at : m.end()])
            pieces.append("" if any(p in out[m.end() : end] for p in present) else " " + flag)
            at = m.end()
        out = "".join(pieces) + out[at:]
    if re.search(r"(?<![\w./-])cmake(?=\s)", out):
        out = GCC_SARIF.replace("{c}", "${CFLAGS:-}").replace("{cxx}", "${CXXFLAGS:-}") + out
    elif re.search(r"(?:^|[\s;&|(])(?:\./|sh )?configure(?=\s|$|;)", out):
        out = GCC_SARIF.replace("{c}", "${CFLAGS--g -O2}").replace("{cxx}", "${CXXFLAGS--g -O2}") + out
    return out


# -- reading what the reporters wrote --------------------------------------------------------------------------------


@dataclass
class Found:
    """What structured output said about one run: the class it names, the kinds of compile error by count, and the
    test outcomes and counts when it has them. `codes` holds each diagnostic code as the shipped table names it, any
    other as `other`."""

    classes: list[str] = field(default_factory=list)
    kinds: Counter = field(default_factory=Counter)
    codes: list[str] = field(default_factory=list)
    cases: dict[str, str] = field(default_factory=dict)
    counts: tuple[int, int] | None = None

    def merge(self, other: Found) -> None:
        self.classes += other.classes
        self.kinds.update(other.kinds)
        self.codes += other.codes
        self.cases.update(other.cases)
        if other.counts and not self.counts:
            self.counts = other.counts


def _lines(text: str, start: str) -> list[dict]:
    got = []
    for line in text.splitlines():
        if line.startswith(start):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                got.append(row)
    return got


def coded(table: str, code: str | None) -> str:
    """The kind a diagnostic code stands for in the shipped table, else `other`."""
    return pack()["codes"].get(table, {}).get(code or "", "other")


def code_word(table: str, code: str | None) -> str:
    """A diagnostic code as it may be sent: itself when the shipped table holds it, else `other`."""
    return code if code and code in pack()["codes"].get(table, {}) else "other"


def kind_of(message: str) -> str:
    """The kind of a compile error from its message, read here: the first kind whose pattern matches, else `other`."""
    _, _, kinds = _compiled()
    return next((k for k, p in kinds if p.search(message)), "other")


def go_events(text: str) -> Found:
    """`go test -json` (test2json): pass and fail per test, a failed build, a panic, a timeout."""
    f, outcomes = Found(), {}
    for e in _lines(text, '{"'):
        action, test, pkg = e.get("Action"), e.get("Test"), e.get("Package") or e.get("ImportPath") or ""
        if action in ("pass", "fail", "skip") and test:
            outcomes[f"{pkg}\0{test}"] = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
        elif action == "build-fail" or (action == "fail" and e.get("FailedBuild")):
            f.classes.append("compile")
        elif action in ("output", "build-output"):
            line = str(e.get("Output") or "")
            if line.startswith("panic: test timed out"):
                f.classes.append("test_timeout")
            elif line.startswith("panic: "):
                f.classes.append("test_error")
            elif m := re.match(r"\S+\.go:\d+:\d+: (.*)", line):
                f.kinds[kind_of(m.group(1))] += 1
    if outcomes:
        f.cases = outcomes
        ran = [o for o in outcomes.values() if o != "skipped"]
        f.counts = (len(ran), ran.count("passed"))
        if "failed" in ran:
            f.classes.append("test_assertion")
    return f


def cargo_messages(text: str) -> Found:
    """cargo's `--message-format=json`: compiler errors by rustc code, and whether the build finished."""
    f = Found()
    for m in _lines(text, '{"reason":'):
        if m.get("reason") == "compiler-message":
            msg = m.get("message") or {}
            if msg.get("level") in ("error", "error: internal compiler error"):
                code = (msg.get("code") or {}).get("code")
                f.kinds[coded("rustc", code) if code else kind_of(str(msg.get("message") or ""))] += 1
                if code:
                    f.codes.append(code_word("rustc", code))
        elif m.get("reason") == "build-finished" and m.get("success") is False:
            f.classes.append("compile")
    if f.kinds and "compile" not in f.classes:
        f.classes.append("compile")
    return f


def sarif(paths: list[Path]) -> Found:
    """GCC's SARIF files: errors by rule id when the table has it, else by message, read here."""
    f = Found()
    for path in paths:
        try:
            doc = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        for run in doc.get("runs") or []:
            for r in run.get("results") or []:
                if r.get("level") != "error":
                    continue
                rule = r.get("ruleId")
                kind = coded("gcc", rule) if rule else "other"
                if rule:
                    f.codes.append(code_word("gcc", rule))
                if kind == "other":
                    kind = kind_of(str((r.get("message") or {}).get("text") or ""))
                f.kinds[kind] += 1
    if f.kinds:
        f.classes.append("compile")
    return f


def jest(path: Path) -> Found:
    """Jest's `--json` report: per-test outcomes, failed tests, and suites that could not run."""
    f = Found()
    try:
        doc = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return f
    for suite in doc.get("testResults") or []:
        for t in suite.get("assertionResults") or []:
            status = {"passed": "passed", "failed": "failed"}.get(t.get("status"), "skipped")
            f.cases[f"{suite.get('name', '')}\0{t.get('fullName', '')}"] = status
    total, passed = doc.get("numTotalTests"), doc.get("numPassedTests")
    if isinstance(total, int) and isinstance(passed, int):
        f.counts = (total - int(doc.get("numPendingTests") or 0), passed)
    if doc.get("numRuntimeErrorTestSuites"):
        f.classes.append("test_error")
    if doc.get("numFailedTests"):
        f.classes.append("test_assertion")
    return f


def bep(path: Path) -> Found:
    """Bazel's JSON build event stream: each test target's overall status and why the build was aborted."""
    f, table = Found(), pack()["bazel"]
    try:
        text = path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return f
    passed = total = 0
    for e in _lines(text, "{"):
        if summary := e.get("testSummary"):
            status = summary.get("overallStatus", "PASSED")
            total += 1
            passed += status in ("PASSED", "FLAKY")
            if status in table["tests"]:
                f.classes.append(table["tests"][status])
        if (aborted := e.get("aborted")) and aborted.get("reason") in table["aborted"]:
            f.classes.append(table["aborted"][aborted["reason"]])
    if total:
        f.counts = (total, passed)
    return f


def junit_files(files: list[Path]) -> dict[str, str]:
    """Outcomes per test case from JUnit XML (pytest, Surefire, Gradle and the rest): error beats failure beats
    skipped beats passed."""
    rank = {"skipped": 0, "passed": 1, "failed": 2, "error": 3}
    got: dict[str, str] = {}
    for path in files:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        for case in root.iter("testcase"):
            outcome = (
                "error"
                if case.find("error") is not None
                else "failed"
                if case.find("failure") is not None
                else "skipped"
                if case.find("skipped") is not None
                else "passed"
            )
            key = f"{case.get('classname', '')}\0{case.get('name', '')}"
            got[key] = max(got.get(key, outcome), outcome, key=rank.__getitem__)
    return got


REPORTS = ("target/surefire-reports/TEST-*.xml", "build/test-results/**/TEST-*.xml")


def reports(src: Path, since: float) -> list[Path]:
    """The JUnit XML Surefire and Gradle write by default in the working copy, written since `since`."""
    out = []
    for pattern in REPORTS:
        for sub in ("", "*/", "*/*/"):
            for p in src.glob(sub + pattern):
                try:
                    if p.stat().st_mtime >= since:
                        out.append(p)
                except OSError:
                    continue
    return sorted(set(out))


def sarif_files(src: Path, since: float) -> list[Path]:
    out = []
    for p in src.rglob("*.sarif"):
        try:
            if p.stat().st_mtime >= since and not p.name.startswith("conftest"):
                out.append(p)
        except OSError:
            continue
    return sorted(out)[:10_000]


def read(text: str, junit: Path | None = None, src: Path | None = None, since: float = 0.0) -> Found:
    """Everything the structured reporters left for one run: in its log, in the reports directory, and in the
    working copy."""
    f = Found()
    f.merge(go_events(text))
    f.merge(cargo_messages(text))
    if junit is not None and junit.is_dir():
        if (j := junit / "kennet-jest.json").is_file():
            f.merge(jest(j))
        if (b := junit / "kennet-bep.json").is_file():
            f.merge(bep(b))
    if src is not None:
        f.merge(sarif(sarif_files(src, since)))
    return f


# -- the class -------------------------------------------------------------------------------------------------------


def _text(log: Path | None) -> str:
    if log is None:
        return ""
    try:
        with log.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - TAIL))
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw).replace("\r\n", "\n")


def environmental(code: int | None, seconds: float | None = None, limit_s: float | None = None,
                  memory: int | None = None, memory_limit: int | None = None) -> str | None:  # fmt: skip
    """The environmental class from how the process ended, never from what it printed: our wall clock ran out, it was
    killed or interrupted by a signal, its shell could not run or find the command, or it held its memory limit."""
    if code is None or (limit_s and seconds is not None and seconds >= limit_s):
        return "env_timeout"
    if memory and memory_limit and memory >= memory_limit * float(pack()["memory_share"]):
        return "env_resource"
    return pack()["exits"].get(str(code))


def _template(line: str, tokens: list[str]) -> bool:
    words = line.split()
    return len(words) == len(tokens) and all(t == "<*>" or t == w for t, w in zip(tokens, words, strict=True))


def codes_in(text: str) -> list[str]:
    """The diagnostic codes a log names (rustc, tsc, csc), as they may be sent, in the order first seen."""
    got = []
    for table, pattern in pack()["code_res"].items():
        got += [code_word(table, c) for c in re.findall(pattern, text)]
    return got


def matched(text: str) -> tuple[dict | None, str | None]:
    """The first rule in the pack that matches the log: a pattern, else a template on one of its lines. Answers the
    rule and the kind of compile error the log names, if any."""
    patterns, templates, _ = _compiled()
    rule = next((r for r, p in patterns if p.search(text)), None)
    if rule is None:
        lines = text.splitlines()[-4000:]
        rule = next((t for t, tokens in templates for line in lines if _template(line, tokens)), None)
    kind = None
    if rule and rule["class"] == "compile":
        for table, pattern in pack()["code_res"].items():
            if m := re.search(pattern, text):
                kind = coded(table, m.group(1))
                break
        kind = kind or kind_of(text)
    return rule, kind


COMPILED = frozenset(["compile_source", "compile_test", "link"])
TEST_STEPS = frozenset(["test_compile", "test"])
MAX_CODES = 16


def classify(
    step: str,
    code: int | None,
    log: Path | None = None,
    found: Found | None = None,
    counts: tuple[int, int] | None = None,
    seconds: float | None = None,
    limit_s: float | None = None,
    memory: int | None = None,
    memory_limit: int | None = None,
) -> dict[str, Any]:
    """Why one build (`step` "build") or test run (`step` "test") ended, as {step, class, compile_kind, codes, rule}.
    Exit 0 is `ok`. Otherwise the order is: how the process ended; what structured output said; the rule pack on the
    log; then what the counts say. A build's step is never a test step, and a test run's always is. `rule` is the id
    of the shipped rule that decided, kept on this machine."""
    found = found or Found()
    text = _text(log) if code != 0 else ""
    codes = list(dict.fromkeys(found.codes + codes_in(text)))[:MAX_CODES]
    if code == 0:
        return _row(step, "ok", None, [], None)
    if env := environmental(code, seconds, limit_s, memory, memory_limit):
        return _row(step, env, None, codes, f"exit.{code}")
    order = ("compile", "test_timeout", "test_error", "test_assertion", "test_flaky", "env_resource", "env_timeout",
             "env_network", "build_config", "cancelled")  # fmt: skip
    if named := next((c for c in order if c in found.classes), None):
        return _row(step, named, found.kinds.most_common(1)[0][0] if found.kinds else None, codes, "structured")
    rule, kind = matched(text)
    if rule is not None:
        return _row(step, rule["class"], kind, codes, rule["id"], rule.get("step"))
    if counts is not None and counts[0] != counts[1]:
        return _row(step, "test_assertion", None, codes, "counts")
    return _row(step, "unknown", None, codes, None)


def _row(
    step: str, cls: str, kind: str | None, codes: list[str], rule: str | None, where: str | None = None
) -> dict[str, Any]:
    test_side = step != "build"
    if cls == "compile":
        cls, where = ("compile_test", "test_compile") if test_side else ("compile_source", "compile")
        kind = kind or "other"
    if test_side:
        where = "test_compile" if where in ("test_compile", "compile") or cls == "compile_test" else "test"
    elif where is None or where in TEST_STEPS:
        where = "compile"
    return {
        "step": where,
        "class": cls,
        "compile_kind": kind if cls in COMPILED else None,
        "codes": codes,
        "rule": rule,
    }


def flaky(rows: list[dict[str, Any]]) -> None:
    """Mark the failed runs of one side `test_flaky` when another run of the same side passed: the same code, the
    same image, and a different end."""
    passed = any(r.get("class") == "ok" for r in rows)
    for r in rows:
        if passed and r.get("exit") is not None and str(r.get("class", "")).startswith("test_"):
            r["class"] = "test_flaky"


__all__ = [
    "COMPILED",
    "MAX_CODES",
    "Found",
    "bep",
    "cargo_messages",
    "classify",
    "code_word",
    "coded",
    "codes_in",
    "environmental",
    "flaky",
    "go_events",
    "jest",
    "junit_files",
    "kind_of",
    "matched",
    "pack",
    "read",
    "reports",
    "rules_hash",
    "sarif",
    "structured",
]
