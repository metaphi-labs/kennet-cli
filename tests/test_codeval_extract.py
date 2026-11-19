import hashlib
import json
import shutil
from pathlib import Path

import pytest

from metaphi import codebase_checks, codeval
from metaphi import codeval_extract as extract
from metaphi import codeval_schema as schema

LOGS = Path(__file__).parent / "data" / "codeval_logs"
SECRET = ("hunter2", "SecretCo", "secretco", "ledger", "Ledger")


def _found(name: str) -> extract.Found:
    return extract.read((LOGS / name).read_text())


@pytest.mark.parametrize(
    "name, step, code, want",
    [
        ("go-test-fail.log", "test", 1, ("test", "test_assertion", None)),
        ("go-test-build.log", "test", 1, ("test_compile", "compile_test", "missing_symbol")),
        ("go-test-timeout.log", "test", 1, ("test", "test_timeout", None)),
        ("cargo-e0308.log", "build", 101, ("compile", "compile_source", "type_mismatch")),
        ("cargo-unknown-code.log", "build", 101, ("compile", "compile_source", "other")),
        ("npm-eresolve.log", "build", 1, ("dep_resolve", "dep_conflict", None)),
        ("pip-missing.log", "build", 1, ("dep_resolve", "dep_unresolved", None)),
        ("maven-deps.log", "build", 1, ("dep_resolve", "dep_unresolved", None)),
        ("ld-undefined.log", "build", 1, ("compile", "link", None)),
        ("cmake-config.log", "build", 1, ("setup", "build_config", None)),
        ("network.log", "build", 128, ("compile", "env_network", None)),
        ("eslint.log", "test", 1, ("test", "quality_gate", None)),
        ("eslint.log", "build", 1, ("lint", "quality_gate", None)),
        ("tsc.log", "build", 2, ("compile", "compile_source", "missing_symbol")),
        ("javac.log", "build", 1, ("compile", "compile_source", "missing_symbol")),
        ("pytest-assert.log", "test", 1, ("test", "test_assertion", None)),
        ("python-traceback.log", "test", 1, ("test", "test_error", None)),
        ("unknown.log", "test", 1, ("test", "unknown", None)),
    ],
)
def test_each_toolchain_log_maps_to_its_class(name, step, code, want):
    log = LOGS / name
    got = extract.classify(step, code, log, extract.read(log.read_text()))
    assert (got["step"], got["class"], got["compile_kind"]) == want, got["rule"]


def test_go_json_gives_per_test_outcomes_and_counts():
    f = _found("go-test-fail.log")
    assert f.counts == (2, 1) and sorted(f.cases.values()) == ["failed", "passed"]


def test_jest_bazel_sarif_and_surefire_reports_are_read_where_the_tools_write_them(tmp_path):
    junit, src = tmp_path / "junit", tmp_path / "src"
    (src / "target/surefire-reports").mkdir(parents=True)
    junit.mkdir()
    shutil.copy(LOGS / "jest.json", junit / "kennet-jest.json")
    f = extract.read("", junit, src)
    assert f.counts == (2, 1) and "test_assertion" in f.classes and len(f.cases) == 3

    shutil.copy(LOGS / "bazel-flaky.json", junit / "kennet-bep.json")
    (junit / "kennet-jest.json").unlink()
    f = extract.read("", junit, src)
    assert f.classes == ["test_flaky"] and f.counts == (2, 2)
    shutil.copy(LOGS / "bazel-oom.json", junit / "kennet-bep.json")
    got = extract.classify("test", 1, None, extract.read("", junit, src))
    assert got["class"] == "env_resource"

    (junit / "kennet-bep.json").unlink()
    shutil.copy(LOGS / "gcc.sarif", src / "ledger.c.sarif")
    shutil.copy(LOGS / "gcc.sarif", src / "conftest.c.sarif")
    f = extract.read("", junit, src)
    assert f.kinds == {"missing_symbol": 1}, "only errors count, and configure's own probes not at all"
    assert extract.classify("build", 1, None, f)["class"] == "compile_source"

    shutil.copy(LOGS / "surefire.xml", src / "target/surefire-reports/TEST-LedgerTest.xml")
    cases = extract.junit_files(extract.reports(src, 0))
    assert sorted(cases.values()) == ["failed", "passed"]
    assert extract.reports(src, 4e9) == [], "reports written before the run are not this run's"


@pytest.mark.parametrize(
    "code, seconds, memory, want",
    [
        (None, 3, None, "env_timeout"),
        (1, 600, None, "env_timeout"),
        (137, 3, None, "env_resource"),
        (-9, 3, None, "env_resource"),
        (1, 3, 990, "env_resource"),
        (127, 3, None, "env_toolchain_missing"),
        (126, 3, None, "env_permission"),
        (130, 3, None, "cancelled"),
        (-15, 3, None, "cancelled"),
        (1, 3, 500, None),
    ],
)
def test_environmental_ends_come_from_how_the_process_ended_not_its_text(code, seconds, memory, want):
    assert extract.environmental(code, seconds, 600, memory, 1000) == want
    if want:
        log = LOGS / "pytest-assert.log"
        assert extract.classify("test", code, log, None, None, seconds, 600, memory, 1000)["class"] == want


def test_a_run_that_passes_once_and_fails_once_is_flaky():
    rows = [{"class": "test_assertion", "exit": 1}, {"class": "ok", "exit": 0}]
    extract.flaky(rows)
    assert rows == [{"class": "test_flaky", "exit": 1, "compile_kind": None}, {"class": "ok", "exit": 0}]
    rows = [{"class": "test_timeout", "exit": None}, {"class": "ok", "exit": 0}]
    extract.flaky(rows)
    assert rows[0]["class"] == "test_timeout", "a run stopped at run_minutes stays a timeout"
    rows = [{"class": "test_assertion", "exit": 1}, {"class": "test_assertion", "exit": 1}]
    extract.flaky(rows)
    assert rows[0]["class"] == "test_assertion"


def test_every_class_kind_and_step_is_one_of_the_packs_words():
    p = extract.pack()
    for r in p["patterns"] + p["templates"]:
        assert r["class"] in (*p["classes"], "compile") and r.get("step", "test") in p["steps"]
    for table in p["codes"].values():
        assert set(table.values()) <= set(p["compile_kinds"])
    props = schema.defs()["Step"]["properties"]
    assert set(p["classes"]) == set(props["class"]["enum"]) and set(p["steps"]) == set(props["step"]["enum"])
    assert set(p["compile_kinds"]) == set(props["compile_kind"]["anyOf"][0]["enum"])
    assert extract.coded("rustc", "E0425") == "missing_symbol" and extract.coded("rustc", "E9999") == "other"


def test_free_text_in_a_log_never_reaches_a_body(tmp_path):
    key = b"k" * 32
    for log in sorted(LOGS.glob("*.log")):
        for step in ("build", "test"):
            why = extract.classify(step, 1, log, extract.read(log.read_text()))
            row = codebase_checks._classed(codebase_checks._ran(1, 3, log), why)
            sent = codeval.step(row, key, tested=step == "test")
            schema.check("TestRun" if step == "test" else "Step", sent)
            wire = json.dumps(sent)
            for word in SECRET:
                assert word not in wire, (log.name, word)
            assert "rule" not in sent and (why["rule"] is None or why["rule"] not in wire)


def test_the_rule_pack_hash_is_stable_and_names_the_shipped_file():
    raw = extract.PATH.read_bytes()
    assert extract.rules_hash() == hashlib.sha256(raw).hexdigest()
    assert json.loads(raw)["pack"] == "kennet.codeval.rules.v1"
    assert extract.rules_hash() == "f3b4d06d2a9cf94474fb921ae2f866d197b82440ef417fe34de6b1c3e0eaca8d", (
        "a changed rule pack is a new release: update this pin with it"
    )


def test_the_server_cannot_send_rules():
    assert "rules" not in schema.defs()["Next"]["properties"]
    nxt = {"batch": 1, "tip": True, "commits": [], "mutants": [], "blobs": [], "runs": 1, "run_minutes": 5,
           "hours_left": 1, "rules": {"patterns": []}}  # fmt: skip
    with pytest.raises(schema.Refused):
        schema.check("Next", nxt)


@pytest.mark.parametrize(
    "command, want",
    [
        ("go test ./...", "go test -json ./..."),
        ("go test -json ./...", "go test -json ./..."),
        ("go vet ./... && go test -race ./...", "go vet ./... && go test -json -race ./..."),
        (
            "cargo test --workspace -- --nocapture",
            "cargo test --message-format=json-diagnostic-rendered-ansi --workspace -- --nocapture",
        ),
        ("python -m pytest -q tests", 'python -m pytest --junit-xml="$KENNET_JUNIT/kennet-pytest.xml" -q tests'),
        ("pytest --junitxml=r.xml", "pytest --junitxml=r.xml"),
        ("npx jest --ci", 'npx jest --json --outputFile="$KENNET_JUNIT/kennet-jest.json" --ci'),
        ("bazel test //...", 'bazel test --build_event_json_file="$KENNET_JUNIT/kennet-bep.json" //...'),
        ("make test", "make test"),
        ("mvn -q test", "mvn -q test"),
    ],
)
def test_structured_output_is_turned_on_for_tools_named_directly(command, want):
    assert extract.structured(command) == want


def test_gcc_sarif_is_asked_for_only_by_cmake_and_configure_builds_keeping_their_flags():
    assert extract.structured("make -j4") == "make -j4"
    cmake = extract.structured("cmake -B b && cmake --build b")
    assert cmake.endswith("cmake -B b && cmake --build b") and "${CFLAGS:-} $f" in cmake and "*clang*" in cmake
    conf = extract.structured("./configure && make")
    assert "${CFLAGS--g -O2} $f" in conf and "-ge 13" in conf


def test_codes_are_sent_only_as_the_shipped_tables_name_them():
    for name, want in (("cargo-e0308.log", ["E0308"]), ("cargo-unknown-code.log", ["other"])):
        log = LOGS / name
        assert extract.classify("build", 101, log, extract.read(log.read_text()))["codes"] == want, name
    ok = extract.classify("build", 0, LOGS / "cargo-e0308.log")
    assert (ok["class"], ok["codes"], ok["compile_kind"]) == ("ok", [], None), "exit 0 is ok, whatever it printed"


@pytest.mark.parametrize(
    "seconds, want",
    [(0, 0), (0.0999, 0), (0.1, 1), (0.10999, 1), (0.11, 2), (0.121, 3), (1e9, 165), (-1, 0)],
)
def test_a_duration_is_sent_as_its_bucket(seconds, want):
    bounds = schema.defs()["Step"]["properties"]["took"]["x-bounds-ms"]
    assert bounds[:4] == [0, 100, 110, 121] and len(bounds) == 166
    assert codeval.took(seconds) == want


def test_flaky_is_a_pass_and_a_fail_of_the_same_side_and_nothing_else():
    rows = [{"class": "test_error", "exit": 2}, {"class": "ok", "exit": 0}]
    extract.flaky(rows)
    assert rows[0] == {"class": "test_flaky", "compile_kind": None, "exit": 2}
    for kept in (
        "env_network",
        "env_resource",
        "env_timeout",
        "env_permission",
        "cancelled",
        "test_timeout",
        "compile_test",
    ):
        rows = [{"class": kept, "exit": 1}, {"class": "ok", "exit": 0}]
        extract.flaky(rows)
        assert rows[0]["class"] == kept, f"{kept} is never relabelled flaky"
    rows = [{"class": "test_assertion", "exit": 1}, {"class": "test_error", "exit": 1}]
    extract.flaky(rows)
    assert [r["class"] for r in rows] == ["test_assertion", "test_error"], "a test_ class alone is never flaky"


def test_the_schemas_code_enum_is_the_packs_codes_and_other():
    items = schema.defs()["Step"]["properties"]["codes"]["items"]
    held = schema.defs()[items["$ref"].rsplit("/", 1)[1]] if "$ref" in items else items
    assert sorted(held["enum"]) == extract.codes() and len(extract.codes()) == 54


def test_codeval_holds_no_numeric_thresholds_but_documented_mechanics():
    """No-intelligence: an ordering comparison in codeval*.py compares against 0, 1, a value it was handed, or a
    named build-mechanics constant that says what it is on its own line. No weights, scores or thresholds."""
    import ast
    import re

    mechanics = {"GIT_FLOOR", "TZ_MOST", "MAX_BLOB", "REVEAL_FLOOR", "TAIL", "TEMPLATE_LINES"}
    src = Path(codeval.__file__).parent
    scanned = [(p, ast.parse(p.read_text())) for p in sorted(src.glob("codeval*.py"))]
    checks = src / "codebase_checks.py"
    # what decides a rerun alone is build mechanics: a build or run that failed, was killed or timed out, or reported
    # fewer tests than its own base; nothing scored
    scanned += [
        (checks, f)
        for f in ast.parse(checks.read_text()).body
        if isinstance(f, ast.FunctionDef) and f.name in ("_suspect", "_env_ended")
    ]
    assert len(scanned) == len(list(src.glob("codeval*.py"))) + 2
    for path, tree in scanned:
        text = ast.unparse(tree)
        assert not re.search(r"(?i)\b\w*(weight|score|threshold|confidence)\w*\s*=", text), path.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or not any(
                isinstance(o, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for o in node.ops
            ):
                continue

            def operands(n):  # the values compared, through arithmetic, not into calls or subscripts
                yield n
                if isinstance(n, ast.BinOp):
                    yield from (*operands(n.left), *operands(n.right))
                elif isinstance(n, ast.UnaryOp):
                    yield from operands(n.operand)

            for side in (node.left, *node.comparators):
                for leaf in operands(side):
                    if isinstance(leaf, ast.Constant) and isinstance(leaf.value, (int, float)):
                        assert leaf.value in (0, 1), f"{path.name}:{node.lineno}: {ast.unparse(node)}"
                    if isinstance(leaf, ast.Name) and leaf.id.isupper():
                        assert leaf.id in mechanics, f"{path.name}:{node.lineno}: {leaf.id}"
    for name in mechanics:
        line = next(
            line
            for path in sorted(src.glob("codeval*.py"))
            for line in path.read_text().splitlines()
            if line.startswith(f"{name} = ")
        )
        assert re.search(r"#\s*(size|format|build mechanics|privacy floor):", line), line
