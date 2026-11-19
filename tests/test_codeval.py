import gzip
import json
import re
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import respx

from metaphi import accounts, cli, codebase_checks, codeval, codeval_runs, config, errors
from metaphi import codeval_extract as extract
from metaphi import codeval_schema as schema

API = config.DEFAULT_API
A = config.DEFAULT_ACCOUNTS
SECRET = [
    "Falcon",
    "falcon_core",
    "hunter2",
    "seller@acme.test",
    "Sam Seller",
    "git.acme.test",
    "ledger.py",
    "internal_payments",
    "billing",
]


def git(repo, *args, **env):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "Sam Seller",
            "GIT_AUTHOR_EMAIL": "seller@acme.test",
            "GIT_COMMITTER_NAME": "Sam Seller",
            "GIT_COMMITTER_EMAIL": "seller@acme.test",
            **env,
        },
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "falcon_core"
    (r / "internal_payments" / "billing").mkdir(parents=True)
    (r / "tests").mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "remote", "add", "origin", "https://git.acme.test/falcon_core.git")
    (r / "internal_payments/billing/ledger.py").write_text(
        "import os\nimport internal_payments.billing.ledger2\n# hunter2\nprint(1)\n"
    )
    (r / "internal_payments/billing/other.py").write_text("x = 1\n")
    (r / "tests/test_ledger.py").write_text("import os\ndef test_a():\n    os.environ['TOKEN']\n")
    (r / "LICENSE").write_text("Permission is hereby granted, free of charge, to any person")
    git(r, "add", ".")
    git(r, "commit", "-qm", "Falcon: start hunter2\n\nSigned-off-by: Sam Seller <seller@acme.test>\nProject-Falcon: x")
    (r / "internal_payments/billing/other.py").write_text("x = 2\n+++ b/fake\n@@ -1 +1 @@\ny = 2\n")
    (r / "tests/test_ledger.py").write_text("import os\ndef test_a():\n    pass\ndef test_b():\n    pass\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "fix FAL-12 in Falcon")
    git(r, "mv", "internal_payments/billing/other.py", "internal_payments/billing/ledger2.py")
    git(r, "commit", "-qm", "move")
    return r


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("KENNET_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_" + "a" * 43)
    for var in ("METAPHI_ACCOUNTS", "METAPHI_API", "METAPHI_ALLOW_CUSTOM_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize(
    ("given", "url"),
    [
        ("https://github.com/acme/core.git", True),
        ("git@github.com:acme/core.git", True),
        ("ssh://git@h/acme/core", True),
        ("./core", False),
        ("core", False),
        ("/srv/core", False),
    ],
)
def test_is_url(given, url):
    assert codeval.is_url(given) is url


def test_blank_lines():

    started = time.monotonic()
    codeval._content(b"\n" * 500_000, "go", "source", lambda a, b: False)
    assert time.monotonic() - started < 5


@pytest.fixture
def tested(tmp_path):
    r = tmp_path / "falcon_calc"
    (r / "tests").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "money.py").write_text("".join(f"RATE_{n} = {n}\n" for n in range(120)))
    (r / "tests/test_calc.py").write_text("from calc import *\ndef test_add():\n    assert add(1, 2) == 3\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "add")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\ndef mul(a, b):\n    return a * b\n")
    (r / "tests/test_calc.py").write_text(
        "from calc import *\ndef test_add():\n    assert add(1, 2) == 3\n"
        "def test_mul_hunter2():\n    assert mul(2, 3) == 6\n"
    )
    git(r, "add", ".")
    git(r, "commit", "-qm", "mul")
    (r / "calc.py").write_text("def add(a, b):\n    return sum((a, b))\ndef mul(a, b):\n    return a * b\n")
    (r / "tests/test_calc.py").write_text(
        "from calc import *\ndef test_add():\n    assert add(1, 2) == 3\n"
        "def test_mul_hunter2():\n    assert mul(2, 3) == 6\n# again\n"
    )
    git(r, "add", ".")
    git(r, "commit", "-qm", "refactor")
    return r


PYTEST = "PYTHONPATH=. {py} -m pytest -q -p no:cacheprovider --junitxml=$KENNET_JUNIT/r.xml tests"


def test_recipe_edits_files():
    from metaphi import codebase_setup

    d = _dirs()
    (d[0] / "configure.ac").write_text("AM_INIT\n")
    said, kept, why, _ = codebase_setup._tried(
        codebase_setup.Recipe("", "echo XX > configure.ac", "true"), None, d, "", d[0].parent, 1, 1, *_runners()
    )
    assert kept is False and "configure.ac" in said and why == "tracked_file_changed"


def test_recipe_build_fails():
    from metaphi import codebase_setup

    record: list = []
    said, kept, why, _ = codebase_setup._tried(
        codebase_setup.Recipe("", "exit 3", "true"), None, _dirs(), "", _dirs()[0], 1, 1, *_runners(), None, record
    )
    assert kept is False and "exit: 3" in said and "build did not pass" in said and why == "build_failed"
    assert record[0]["build"]["exit"] == 3 and "test" not in record[0]


def test_recipe_records_build_beside_failing_tests():
    from metaphi import codebase_setup

    d, record = _dirs(), []
    _, kept, why, _ = codebase_setup._tried(
        codebase_setup.Recipe("", "true", _junit_of(0, 2) + "; exit 1"),
        None,
        d,
        "",
        d[0].parent,
        1,
        1,
        *_runners(),
        None,
        record,
    )
    assert kept is False and why == "pass_fraction"
    assert record[0]["build"]["exit"] == 0 and isinstance(record[0]["build"]["seconds"], float)
    assert (record[0]["test"]["exit"], record[0]["test"]["reported"], record[0]["test"]["passed"]) == (1, 2, 0)


def _junit_of(passed: int, failed: int) -> str:
    cases = "".join(f'<testcase classname="a" name="p{n}"/>' for n in range(passed)) + "".join(
        f'<testcase classname="a" name="f{n}"><failure/></testcase>' for n in range(failed)
    )
    return f"echo '<testsuite>{cases}</testsuite>' > $KENNET_JUNIT/r.xml"


@pytest.mark.parametrize(
    ("test", "uncounted", "taken", "why", "report"),
    [
        ("exit 1", "", False, "tests_failed", None),
        ("true", "", False, "no_test_count", None),
        ("true", "a shell harness that prints no count", True, None, None),
        ("exit 1", "a shell harness that prints no count", False, "tests_failed", None),
        (_junit_of(0, 1) + "; exit 1", "", False, "pass_fraction", None),
        (_junit_of(1, 1) + "; exit 1", "", True, None, "junit"),
        ("printf '==== 3 passed, 1 failed in 0.20s ====\\n'; exit 1", "", True, None, "summary"),
    ],
)
def test_recipe_tests(test, uncounted, taken, why, report):
    from metaphi import codebase_setup

    d = _dirs()
    said, kept, got_why, got_report = codebase_setup._tried(
        codebase_setup.Recipe("", "true", test, None, uncounted), None, d, "", d[0].parent, 1, 1, *_runners()
    )
    assert (kept == "") is taken and got_why == why and got_report == report, said


def _dirs():
    import tempfile

    root = Path(tempfile.mkdtemp())
    (root / "src").mkdir()
    (root / "junit").mkdir()
    return root / "src", root / "junit"


def _runners():
    from metaphi.codebase_checks import _junit, _run

    return _run, _junit


def test_folder_of_clones(repo, tested, tmp_path):
    (tmp_path / "notes").mkdir()
    assert codeval.repositories([str(tmp_path)]) == sorted([str(repo), str(tested)])
    assert codeval.repositories([str(repo)]) == [str(repo)]


def test_sites():
    from metaphi import codebase_sites as cs

    go = 'x := "a == b" // c < d\nif a == b && c < d { return a + b > 3 && true }\nch <- 1; i++; m[k]--\n/* || */\n'
    assert cs.counts(go, "go") == {"cmp": 3, "logic": 2, "arith": 1, "bool": 1}
    py = 'def f(a):\n    """a == b"""\n    # a or b\n    return a - 1 if a and not b else -a\n'
    assert cs.counts(py, "py") == {"cmp": 0, "logic": 1, "arith": 1, "bool": 0}
    assert cs.apply_edit(py, "py", "logic", 0).endswith("if a or not b else -a\n")
    assert cs.apply_edit(go, "go", "cmp", 2) == go.replace("+ b > 3", "+ b >= 3")
    assert cs.apply_edit(go, "go", "cmp", 3) is None and cs.counts("a == b", "md") is None
    rs = "fn f<'a>(x: &'a str) -> bool { let c = 'x'; x.len() > 1 && true }"
    assert cs.counts(rs, "rs") == {"cmp": 1, "logic": 1, "arith": 0, "bool": 1}


@pytest.fixture
def planted(tmp_path):
    r = tmp_path / "falcon_sums"
    (r / "tests").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    (r / "sums.py").write_text(
        "def add(a, b):\n    return a + b\ndef pos(a):\n    return a > 0\n"
        "def neg(a):\n    return a - 1  # hunter2 == x\n"
    )
    (r / "tests/test_sums.py").write_text(
        "from sums import *\ndef test_add():\n    assert add(1, 2) == 3\n"
        "def test_pos():\n    assert pos(1) and not pos(0)\n"
    )
    git(r, "add", ".")
    git(r, "commit", "-qm", "sums")
    return r


WORKFLOW = """
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ruff check .
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - run: sudo apt-get install -y libfoo-dev
      - run: |
          pip install -e .[test]
      - run: make build
      - run: pytest -x tests
      - run: codecov upload
  win:
    runs-on: windows-latest
    steps:
      - run: pytest
"""


def test_candidates():
    from metaphi import codebase_recipes as cr

    files = {
        ".github/workflows/ci.yml": WORKFLOW.encode(),
        "pyproject.toml": b"[project]\nname='x'\n",
        "package.json": b'{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}',
        "go.mod": b"module x\n\ngo 1.22\n",
        "Makefile": b"all:\n\tcc x.c\ncheck:\n\t./t\n",
    }
    got = cr.candidates(set(files), files.get)
    ci = got[0]
    assert len([r for r in got if "codecov" in r.test or "ruff" in r.test]) == 0
    assert ci.image == "python:3.11" and "pip install -e .[test]" in ci.setup and "apt-get" in ci.setup
    assert "make build" in ci.build and "pytest -x tests" in ci.test and "codecov" not in ci.setup + ci.build
    images = [r.image for r in got[1:]]
    assert images == ["golang:1.22", "python:3.12", "gcc:14"] and got[-1].test == "make check"
    assert "sudo" not in cr.contained(ci).setup and cr.hosted(ci) is None
    py = cr.hosted(got[2])
    assert py.setup.startswith("python3 -m venv .kennet-venv") and ".kennet-venv/bin" in py.test and py.image is None
    assert cr.candidates(set(), {}.get) == []


def test_candidates_gitlab_travis():
    from metaphi import codebase_recipes as cr

    files = {
        ".gitlab-ci.yml": b"image: maven:3-eclipse-temurin-17\nbuild:\n  script: [mvn -B compile]\n"
        b"test:\n  script: [mvn -B test]\n.hidden:\n  script: [make test]\n"
        b"deploy:\n  script: [echo $CI_JOB_TOKEN]\n",
        ".travis.yml": b"language: python\npython: ['3.9']\ninstall: pip install -r requirements.txt\n"
        b"script: [flake8, python -m unittest]\n",
    }
    got = cr.candidates(set(files), files.get)
    assert [(r.image, r.test) for r in got] == [
        ("maven:3-eclipse-temurin-17", "bash -ec 'mvn -B test'"),
        ("python:3.9", "bash -ec 'python -m unittest'"),
    ]
    assert "requirements.txt" in got[1].setup


@pytest.mark.parametrize(
    "files,image,test",
    [
        ({"project.clj": b"(defproject x)"}, "clojure:lein", "lein test"),
        ({"mix.exs": b""}, "elixir:1.17", "mix test"),
        ({"x.sln": b"", "src/x.csproj": b""}, "mcr.microsoft.com/dotnet/sdk:8.0", "dotnet test --no-build x.sln"),
        ({"Package.swift": b""}, "swift:6.0", "swift test"),
        ({"Project.toml": b"", "test/runtests.jl": b""}, "julia:1.11", "julia --project=. -e 'using Pkg; Pkg.test()'"),
        (
            {"DESCRIPTION": b"", "tests/testthat/test-a.R": b""},
            "rocker/r-ver:4.4",
            "Rscript -e 'testthat::test_local(reporter=\"check\")'",
        ),
        ({"composer.json": b"{}", "phpunit.xml.dist": b""}, "composer:2", "vendor/bin/phpunit"),
        ({"build.sbt": b""}, "eclipse-temurin:21", "sbt -batch test"),
        ({"dune-project": b""}, "ocaml/opam:debian-12-ocaml-5.2", "opam exec -- dune runtest"),
        ({"pubspec.yaml": b"name: x\n"}, "dart:stable", "dart test"),
        (
            {"pubspec.yaml": b"dependencies:\n  flutter:\n    sdk: flutter\n"},
            "ghcr.io/cirruslabs/flutter:stable",
            "flutter test",
        ),
        ({"x-1.0-1.rockspec": b""}, "buildpack-deps:noble", "busted"),
    ],
)
def test_candidates_by_ecosystem(files, image, test):
    from metaphi import codebase_recipes as cr

    got = cr.candidates(set(files), files.get)
    assert (got[0].image, got[0].test) == (image, test)


def test_ci_test_workflows_first():
    from metaphi import codebase_recipes as cr

    flow = (
        b"on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        b"      - uses: erlef/setup-beam@v1\n        with: {elixir-version: '1.16'}\n      - run: mix test\n"
    )
    files = {
        ".github/workflows/a-release.yml": flow.replace(b"mix test", b"mix test --only slow"),
        ".github/workflows/tests.yml": flow,
    }
    got = cr.candidates(set(files), files.get)
    assert got[0].image == "elixir:1.16" and got[0].test == "bash -ec 'mix test'"


def test_find_keeps_going_past_a_thin_suite(tmp_path, monkeypatch):
    from metaphi import codebase_recipes as cr

    thin, full = cr.Recipe("true", "true", "thin", None), cr.Recipe("true", "true", "full", None)
    monkeypatch.setattr(cr, "candidates", lambda paths, read: [thin, full])
    counts = {"thin": 1, "full": 40}
    monkeypatch.setattr(
        cr,
        "_tried",
        lambda r, *a: (
            f"ran\ntaken. {counts[r.test]} tests counted (junit), {counts[r.test]} passed",
            "img",
            None,
            "junit",
        ),
    )
    paths = {f"tests/test_{i}.py" for i in range(8)}
    got, how = cr.find(
        lambda d: d.mkdir(parents=True), lambda p: None, paths, "", "docker", tmp_path, lambda s: None, 1.0
    )
    assert got.test == "full" and how["steps"] == 2
    counts["thin"] = 9
    got, how = cr.find(
        lambda d: d.mkdir(parents=True), lambda p: None, paths, "", "docker", tmp_path, lambda s: None, 1.0
    )
    assert got.test == "thin" and how["steps"] == 1


def test_python_unittest_after_pytest():
    from metaphi import codebase_recipes as cr

    files = {"pyproject.toml": b"[project]\nname='x'\n", "tests/test_core.py": b""}
    got = cr.candidates(set(files), files.get)
    assert [r.test for r in got][1:] == ["python -m unittest discover -s tests -t ."] and "pytest" in got[0].test


def test_dotnet_elixir_and_node_shims():
    from metaphi import codebase_recipes as cr

    files = {
        "src/App/App.csproj": b"<TargetFramework>net8.0</TargetFramework>",
        "global.json": b'{"sdk": {"version": "10.0.100"}}',
        "tests/App.Tests/App.Tests.csproj": b"<TargetFramework>net10.0</TargetFramework>",
    }
    got = cr.candidates(set(files), files.get)[0]
    assert (
        got.image == "mcr.microsoft.com/dotnet/sdk:10.0"
        and got.test == "dotnet test --no-build tests/App.Tests/App.Tests.csproj"
    )
    files = {"mix.exs": b'def project, do: [app: :x, elixir: "~> 1.18"]'}
    assert cr.candidates(set(files), files.get)[0].image == "elixir:1.18"
    files = {"package.json": b'{"scripts": {"test": "vitest"}}', "pnpm-lock.yaml": b""}
    got = cr.candidates(set(files), files.get)[0]
    assert "corepack enable --install-directory .kennet-bin" in got.setup and got.test.endswith("CI=true pnpm test")
    assert ".kennet-bin" in cr.hosted(got).build


def test_work_dir_is_a_git_repo_with_its_submodules(tmp_path):
    import subprocess

    from metaphi import codebase_checks as cc

    def git(*a, cwd):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "protocol.file.allow=always", *a],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    lib, top = tmp_path / "lib", tmp_path / "top"
    for d in (lib, top):
        d.mkdir()
        git("init", "-q", cwd=d)
    (lib / "a.c").write_text("int a;\n")
    git("add", "-A", cwd=lib)
    git("commit", "-qm", "lib", cwd=lib)
    (top / "main.c").write_text("int main;\n")
    git("submodule", "add", "-q", str(lib), "vendor/lib", cwd=top)
    git("commit", "-qam", "top", cwd=top)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=top, capture_output=True, text=True, check=True
    ).stdout.strip()
    tree = cc._tree(top, sha)
    assert "vendor/lib" in tree.links and "vendor/lib" not in tree
    into = tmp_path / "w"
    cc._write(top, tree, into)
    assert (into / "vendor/lib/a.c").read_text() == "int a;\n" and (into / ".git").is_dir()
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=into, capture_output=True, text=True, check=True
        ).stdout.strip()
        == ""
    )


@respx.mock
def test_a_signed_in_key_is_checked_from_its_saved_scopes_with_no_call(monkeypatch):
    access = respx.get(f"{API}/codeval/access").mock(return_value=httpx.Response(204))
    monkeypatch.delenv("METAPHI_API_KEY", raising=False)
    key = config.KEY_PREFIX + "login"
    cfg = {"token": "t", "api_key": key, "api_key_from": "login", "scopes": [codeval.SCOPE]}
    monkeypatch.setattr(config, "load", lambda: cfg)
    codeval.key_ready()
    assert access.call_count == 0
    cfg["scopes"] = ["kennet:twin"]
    with pytest.raises(errors.AuthError) as e:
        codeval.key_ready()
    assert e.value.code == "insufficient_scope" and e.value.next_step == "kennet login" and access.call_count == 0
    monkeypatch.setenv("METAPHI_API_KEY", config.KEY_PREFIX + "elsewhere")
    codeval.key_ready()
    assert access.call_count == 1


@pytest.mark.parametrize(
    "text,n",
    [
        ("[TestFixture]\npublic class A {\n  [Test]\n  public void B() {}\n  [Fact]\n  public void C() {}\n}\n", 2),
        ("[TestClass]\nclass A {\n  [TestMethod]\n  void B() {}\n}\n", 1),
        ("@ParameterizedTest\nvoid a() {}\n@Test\nvoid b() {}\n", 2),
        ("func testLogin() {}\nfunc TestX(t *testing.T) {}\n", 2),
        ("describe Foo do\n  it 'works' do\n  end\n  it \"also\" do\n  end\nend\n", 2),
        ("TEST_P(Suite, Case) {}\nTEST(A, B) {}\n", 2),
    ],
)
def test_test_fns_by_language(text, n):
    assert len(codeval.TEST_FN.findall(text)) == n


def test_a_contained_tip_run_is_rerun_with_its_networks_detached_and_reports_peak_memory(tmp_path):
    from metaphi.codebase_setup import Shell

    calls: list[list[str]] = []

    def fake(args, **_kw):
        calls.append(args)
        out = {"inspect": '{"bridge": {}}', "cat": "734003200\n"}.get(args[1] if args[1] != "exec" else args[3], "")
        return subprocess.CompletedProcess(args, 0, out, "")

    shell = Shell(None, None, tmp_path, tmp_path)
    shell.engine, shell.name = "docker", "box"
    with pytest.MonkeyPatch.context() as m:
        m.setattr(subprocess, "run", fake)
        assert shell.offline() and shell.memory_peak() == 734003200
    assert ["docker", "network", "disconnect", "-f", "bridge", "box"] in calls
    assert not Shell(None, None, tmp_path, tmp_path).offline()


def test_time_left():
    assert [codebase_checks.about(s) for s in (30, 600, 3600, 4000, 9000)] == [
        "under a minute",
        "about 10 min",
        "about 1 h",
        "about 1 h 5 min",
        "about 2 h 30 min",
    ]
    now = [0.0]
    clock = lambda: now[0]
    assert codebase_checks.Eta(10, 2, 60.0, 3600, clock).first() == "about 2 min to 12 min"
    assert codebase_checks.Eta(10, 1, None, 3600, clock).first() is None
    assert codebase_checks.Eta(1000, 1, 60.0, 600, clock).first() == "about 10 min"

    eta = codebase_checks.Eta(10, 2, 600.0, 10**6, clock)
    eta.begin(1)
    eta.begin(2)
    now[0] = 2
    assert eta.end(1, 2, False) == "about 18 min left"
    eta.begin(3)
    now[0] = 600
    assert eta.end(2, 600, True) == "about 18 min left"
    shown = []
    for k in range(3, 11):
        eta.begin(k + 1)
        now[0] += 300
        shown.append(eta.end(k, 300, True))
    minutes = [int(x.split()[1]) for x in shown[:-1]]
    assert minutes == sorted(minutes, reverse=True) and shown[-1] == "", shown


def test_every_timed_step_is_a_known_step():
    from metaphi import steps

    src = Path(codeval.__file__).parent
    timed = {m for f in src.glob("*.py") for m in re.findall(r"\.step\(\s*\"([a-z_]+)\"", f.read_text())}
    assert timed and timed <= steps.NAMES, sorted(timed - steps.NAMES)


def test_the_board_redraws_on_a_terminal_and_prints_lines_elsewhere():
    import io

    from metaphi import live

    class Tty(io.StringIO):
        def isatty(self):
            return True

    now = [0.0]
    said: list[str] = []
    tty = Tty()
    board = live.Board("calc", tty, said.append, clock=lambda: now[0], tick=3600)
    board.stage("past commits", 4)
    board.begin("001-aaaa")
    board.begin("002-bbbb")
    now[0] = 75
    board.end("001-aaaa", True, "calc: 001-aaaa build ok (1/4)", "about 3 min left")
    board.say("calc: 2 builds failed; setting up again")
    shown = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", tty.getvalue())
    assert "calc · past commits" in shown and "1/4 · 1 built" in shown
    assert "002-bbbb  1m 15s" in shown and "1m 15s in · about 3 min left" in shown
    assert said == ["calc: 2 builds failed; setting up again"]
    board.close("✓ calc: 4 of 4 past commits run, 3 built (2m 00s)")
    assert said[-1].startswith("✓ calc") and tty.getvalue().endswith("\x1b[J")

    plain: list[str] = []
    board = live.Board("calc", io.StringIO(), plain.append)
    board.stage("past commits", 2)
    board.begin("001-aaaa")
    board.end("001-aaaa", False, "calc: 001-aaaa build failed (1/2)", "about 1 min left")
    board.close()
    assert plain == ["calc: 001-aaaa build failed (1/2) · about 1 min left"]


def test_each_commit_installs_its_own_dependencies_on_a_set_up_image(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from metaphi import codebase_checks
    from metaphi.codebase_setup import Recipe

    ran = []
    monkeypatch.setattr(codebase_checks, "_run", lambda line, *a: (ran.append(line), (0, 0.1))[1])
    (tmp_path / "src").mkdir()
    (tmp_path / "junit").mkdir()
    shell = SimpleNamespace(
        src=tmp_path / "src",
        junit=tmp_path / "junit",
        name="box",
        extra={},
        line=lambda cmd, tests: cmd,
        env=lambda tests: {},
        empty=lambda: None,
        stop=lambda: None,
    )
    recipe = Recipe("npm ci", "true", "npm test", "kennet-setup:abc")
    plan = codebase_checks.Plan(logs=tmp_path)
    codebase_checks._ran_side(shell, plan, {}, 1, codebase_checks._Logs(tmp_path / "logs"), "001", print, recipe)
    assert ran[0].startswith("(npm ci) || ") and ran[0].endswith("; (true)") and ran[1] == "npm test"
    ran.clear()
    host = Recipe("npm ci", "true", "npm test", None)
    codebase_checks._ran_side(shell, plan, {}, 1, codebase_checks._Logs(tmp_path / "logs"), "002", print, host)
    assert ran[0] == "(npm ci) && (true)"


class _Started:
    def __init__(self, calls, pid):
        self.calls, self.pid = calls, pid

    def __call__(self, command, **kw):
        self.calls.append((command, kw))
        return self


# -- the profile -------------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("KENNET_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(codeval_runs, "awake", lambda pid: None)
    return tmp_path / "state"


def _profile(path) -> tuple[dict, codeval.Local]:
    local = next(codeval.opened([str(path)], Path("/nonexistent"), lambda s: None))
    return codeval.profile(local), local


def test_the_profile_fits_the_schema_and_names_nothing(repo):
    doc, local = _profile(repo)
    schema.check("Profile", doc)
    sent = json.dumps(doc)
    for word in SECRET:
        assert word not in sent, word
    assert doc["schema"] == "kennet.codeval.profile.v1" and doc["tip"] == local.tip and len(doc["commits"]) == 3
    first = doc["commits"][0]
    assert first["trailers"] == [{"key": "signed-off-by", "tool": None}, {"key": "other", "tool": None}]
    assert re.fullmatch(r"[0-9a-f]{16}", first["subject_key"]) and doc["licenses"] == ["MIT"]
    assert all(re.fullmatch(r"[0-9a-f]{16}", f["blob_key"]) for f in doc["files"] if "blob_key" in f)
    again, _ = _profile(repo)
    assert again == doc, "the same repository reads the same, under the key kept for it"
    assert (local.home / "key").stat().st_mode & 0o777 == 0o600


def _raw_commit(r, message: str, author: str, committer: str, extra: str = "") -> None:
    """A commit written byte for byte, as an import or a signing tool would: git's own commands normalise `-0000`."""

    def out(*args: str, given: bytes | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(r), *args], input=given, capture_output=True, check=True
        ).stdout.decode()

    tree, parent = out("write-tree").strip(), out("rev-parse", "HEAD").strip()
    head = f"tree {tree}\nparent {parent}\nauthor A <a@x.org> {author}\ncommitter A <a@x.org> {committer}\n"
    made = out(
        "hash-object", "--literally", "-t", "commit", "-w", "--stdin", given=f"{head}{extra}\n{message}\n".encode()
    ).strip()
    git(r, "update-ref", "HEAD", made)


def test_times_and_zones_are_sent_as_git_holds_them(tmp_path):
    r = tmp_path / "falcon_time"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.py").write_text("x = 1\n")
    git(r, "add", ".")
    when = {"GIT_AUTHOR_DATE": "2024-03-04T10:30:00+0200", "GIT_COMMITTER_DATE": "2024-03-04T10:31:07+0200"}
    git(r, "commit", "-qm", "a", **when)
    (r / "a.py").write_text("x = 2\n")
    git(r, "add", ".")
    later = {"GIT_AUTHOR_DATE": "2024-03-10T23:00:00-0530", "GIT_COMMITTER_DATE": "2024-03-11T09:00:00-0530"}
    git(r, "commit", "-qm", "b\n\nCo-authored-by: Claude <noreply@anthropic.com>", **later)
    _raw_commit(r, "imported", "1700000200 -0000", "1700000300 +0000")
    sig = "gpgsig -----BEGIN SSH SIGNATURE-----\n U1NIU0lH\n -----END SSH SIGNATURE-----\n"
    _raw_commit(r, "signed", "-5 +1400", "4102444800 -1200", sig)
    doc, local = _profile(r)
    schema.check("Profile", doc)
    a, b, c, d = doc["commits"]
    stamp = ("author_time", "author_tz", "author_tz_neg0", "committer_time", "committer_tz", "committer_tz_neg0")
    assert [a[k] for k in stamp] == [1709541000, 120, False, 1709541067, 120, False]
    assert [b[k] for k in stamp] == [1710131400, -330, False, 1710167400, -330, False]
    assert [c[k] for k in stamp] == [1700000200, 0, True, 1700000300, 0, False], "-0000 is kept apart from +0000"
    assert [d[k] for k in stamp] == [-5, 840, False, 4102444800, -720, False], "no clamping of odd times"
    assert [x["signed"] for x in (a, b, c, d)] == [False, False, False, True]
    assert d["commit"] == len(doc["commits"]) and doc["tip"] == local.tip, "the tip is the last commit sent"
    assert b["trailers"] == [{"key": "co-authored-by", "tool": "anthropic"}]
    for gone in ("day", "hour", "same_time", "first_parent"):
        assert gone not in a
    assert "first_day" not in doc and "U1NIU0lH" not in json.dumps(doc)


def test_hunks_are_their_header_numbers_on_edits_renames_and_binaries(tmp_path):
    r = tmp_path / "falcon_hunks"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "ledger.py").write_text("".join(f"line {n}\n" for n in range(1, 21)))
    (r / "logo.png").write_bytes(b"\x89PNG\0\1\2")
    (r / ".github/workflows").mkdir(parents=True)
    (r / ".github/workflows/ci.yml").write_text("on: push\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "a")
    lines = [f"line {n}\n" for n in range(1, 21)]
    lines[2], lines[15] = "three\n", "sixteen\n"
    (r / "ledger.py").write_text("".join(lines))
    git(r, "mv", "ledger.py", "books.py")
    (r / "logo.png").write_bytes(b"\x89PNG\0\3\4\5")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "b")
    doc, local = _profile(r)
    schema.check("Profile", doc)
    ids = {p: i for i, p in local.kept["paths"].items()}
    first, second = doc["commits"]
    by = {x["file"]: x for x in second["files"]}
    moved = by[ids["books.py"]]
    assert moved["status"] == "R" and moved["old_file"] not in ids.values()
    assert moved["hunks"] == [[3, 1, 3, 1], [16, 1, 16, 1]] and (moved["added"], moved["deleted"]) == (2, 2)
    png = by[ids["logo.png"]]
    assert png["hunks"] == [] and (png["added"], png["deleted"]) == (-1, -1)
    added = {x["file"]: x for x in first["files"]}[ids[".github/workflows/ci.yml"]]
    assert added["hunks"] == [[0, 0, 1, 1]]
    ci = {f["file"]: f["ci"] for f in doc["files"]}
    assert ci[ids[".github/workflows/ci.yml"]] and not ci[ids["books.py"]]
    assert "gaps" not in moved and "ledger" not in json.dumps(doc) and "books" not in json.dumps(doc)


def test_dependencies_are_named_only_when_the_public_registry_resolved_them(tmp_path):
    r = tmp_path / "falcon_deps"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    lock = {
        "packages": {
            "": {"name": "falcon"},
            "node_modules/left-pad": {"resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz"},
            "node_modules/@acme/ledger": {"resolved": "https://npm.acme.test/@acme/ledger/-/ledger-1.0.0.tgz"},
            "node_modules/billing": {"resolved": "file:../billing", "link": True},
        }
    }
    (r / "package-lock.json").write_text(json.dumps(lock))
    (r / "AGENTS.md").write_text("x\n")
    (r / ".cursorrules").write_text("x\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "deps")
    doc, _ = _profile(r)
    assert doc["deps"] == [{"eco": "npm", "name": "left-pad"}] and doc["private_deps"] == 1
    assert "acme" not in json.dumps(doc) and "billing" not in json.dumps(doc)
    assert {k for k, v in doc["agents"].items() if v} == {"agents_md", "cursor_rules"}


def test_annotated_tags_carry_the_taggers_time_and_lightweight_tags_none(repo):
    git(repo, "tag", "falcon-launch", "HEAD~1")
    git(repo, "tag", "-a", "v2.1.0", "-m", "billing", "HEAD", GIT_COMMITTER_DATE="2030-03-01T00:00:00+0545")
    doc, _ = _profile(repo)
    schema.check("Profile", doc)
    light, annotated = doc["git"]["tags"]
    assert (light["commit"], light["annotated"], light["release"]) == (2, False, False)
    assert (light["tagger_time"], light["tagger_tz"], light["tagger_tz_neg0"]) == (None, None, None)
    assert (annotated["commit"], annotated["annotated"], annotated["release"]) == (3, True, True)
    assert (annotated["tagger_time"], annotated["tagger_tz"], annotated["tagger_tz_neg0"]) == (1898532900, 345, False)
    assert "falcon-launch" not in json.dumps(doc) and "v2.1.0" not in json.dumps(doc)
    with pytest.raises(schema.Refused, match="lightweight"):
        schema.check("Profile", {**doc, "git": {**doc["git"], "tags": [{**light, "tagger_tz": 0}]}})


def test_operators_are_counted_by_class_and_nothing_of_the_line_is_kept(planted):
    doc, _ = _profile(planted)
    assert [f["ops"] for f in doc["files"] if "ops" in f] == [{"cmp": 1, "logic": 0, "arith": 2, "bool": 0}]
    assert "hunter2" not in json.dumps(doc)


def test_a_trailer_value_with_carriage_returns_is_one_trailer():
    block = (
        "Stacktrace: at A.B(String s) in c:\\Projects\\a.cs:line 1\r at C.D() in c:\\Projects\\b.cs\n"
        "Signed-off-by: A <a@x.org>\nAssisted-by: Bot <bot@sub.openai.com>"
    )
    assert codeval._trailers(block) == [
        {"key": "other", "tool": None},
        {"key": "signed-off-by", "tool": None},
        {"key": "assisted-by", "tool": "openai"},
    ]
    named = schema.defs()["Trailer"]["properties"]["tool"]["anyOf"][0]["enum"]
    assert {t for t, _ in codeval.VENDORS} <= set(named)
    assert codeval._trailers("Co-authored-by: A <1+a@users.noreply.github.com>") == [
        {"key": "co-authored-by", "tool": None}
    ]


# -- the schema: the allowlist ----------------------------------------------------------------------------------------


STEP = {"step": "test", "class": "test_assertion", "exit": 1, "took": 40, "log": "0" * 64}


@pytest.mark.parametrize(
    "name, body",
    [
        ("Step", {**STEP, "note": "it broke in ledger.py"}),
        ("Step", {**STEP, "class": "AssertionError in test_ledger"}),
        ("Step", {**STEP, "codes": ["E0308 mismatched SecretCo"]}),
        ("Step", {**STEP, "took": 1234}),
        ("Step", {**STEP, "class": "ok"}),
        ("Step", {**STEP, "exit": None}),
        ("Step", {**STEP, "compile_kind": "syntax"}),
        ("Step", {**STEP, "codes": ["E0308", "E0308"]}),
        ("Side", {"build": STEP, "tests": []}),
        ("Step", {**STEP, "log": "FAILED tests/test_ledger.py"}),
        ("Step", {**STEP, "outcomes": {"test_ledger": "failed"}}),
        ("Step", {**STEP, "outcomes": {"0" * 16: "failed: hunter2"}}),
        ("Toolchain", {"tool": "python", "version": "3.12.1+falcon"}),
        ("Machine", {"os": "linux", "arch": "arm64", "cpus": 8, "memory_bytes": 1, "runtime": "host", "image": "acme"}),
    ],
)
def test_free_text_is_refused_wherever_it_is(name, body):
    with pytest.raises(schema.Refused) as e:
        schema.check(name, body)
    assert e.value.code == "schema_refused"


def test_the_vendored_schema_is_read_strictly(monkeypatch):
    schema.check("Step", STEP)
    defs = json.loads(schema.PATH.read_text())["$defs"]
    assert {"Profile", "Results", "Next"} <= set(defs)
    loose = {**defs, "Step": {**defs["Step"], "if": {}}}
    monkeypatch.setattr(schema, "defs", lambda: loose)
    with pytest.raises(schema.Refused, match="does not read"):
        schema.check("Step", STEP)


# -- the whole flow, against a stub of the server ---------------------------------------------------------------------


def _signed_in():
    config.save({"email": "sam@acme.test", "org": "org_acme", "org_name": "Acme"})
    respx.get(f"{API}/codeval/access").respond(204)


def _next(local_paths: dict, **more) -> dict:
    calc = next(i for i, p in local_paths.items() if p == "calc.py")
    money = next(i for i, p in local_paths.items() if p == "money.py")
    return {
        "batch": 1,
        "tip": True,
        "commits": [2],
        "mutants": [{"file": calc, "op": "arith", "nth": 0}],
        "blobs": [money],
        "runs": 2,
        "run_minutes": 5,
        "hours_left": 1.5,
        **more,
    }


def _bodies(route) -> list[dict]:
    out = []
    for call in route.calls:
        assert call.request.headers["content-encoding"] == "gzip"
        out.append(json.loads(gzip.decompress(call.request.content)))
    return out


def _command(tmp_path) -> str:
    import sys

    return f"env > {tmp_path}/env.txt; " + PYTEST.format(py=sys.executable)


@respx.mock
def test_a_signed_in_run_with_no_terminal_goes_end_to_end(tested, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI-hunter2")
    (tested / "calc.py").write_text("def add(a, b):\n    return a + b\ndef mul(a, b):\n    return a * b\n")
    git(tested, "commit", "-qam", "plain")
    _signed_in()
    _, local = _profile(tested)
    asked = _next(local.kept["paths"])
    posted = respx.post(f"{API}/codeval").respond(json={"run": "cv_1", "object": "codeval", "next": asked})
    results = respx.post(f"{API}/codeval/cv_1/results").respond(json={"done": True})
    assert cli.main(["codeval", str(tested), "--json", "--", _command(tmp_path)]) == 0
    out, err = capsys.readouterr()
    said = json.loads(out)
    assert said == {
        "org": "org_acme",
        "repos": [{"name": "falcon_calc", "run": "cv_1", "state": "done", "sent": str(local.home / "sent")}],
    }
    assert err.startswith(f"CodeVal measures {tested} here and sends Metaphi AI typed measurements")
    assert codeval.DOCS in err and "Signed in as sam@acme.test, sending for Acme (org_acme)" in err
    assert "falcon_calc: batch 1: building and testing the latest commit, 1 commit, 1 mutant, 1 file id" in err
    assert "Sent cv_1 for org_acme. Metaphi AI will reach out if there is a commercial fit." in err

    (profile,), (sent,) = _bodies(posted), _bodies(results)
    schema.check("Profile", profile)
    schema.check("Results", sent)
    assert sent["batch"] == 1 and sent["commands"]["by"] == "seller" and sent["commands"]["build"] is None
    (commit,) = sent["commits"]
    assert commit["commit"] == 2 and len(commit["post"]["tests"]) == 2 and len(commit["pre"]["tests"]) == 1
    assert commit["post"]["tests"][0]["exit"] == 0 and commit["post"]["tests"][0]["report"] == "junit"
    outcomes = commit["post"]["tests"][0]["outcomes"]
    assert len(outcomes) == 2 and all(re.fullmatch(r"[0-9a-f]{16}", k) for k in outcomes)
    (mutant,) = sent["mutants"]
    assert mutant["op"] == "arith" and mutant["post"]["tests"][0]["class"] == "test_assertion"
    assert mutant["post"]["tests"][0]["step"] == "test" and sent["rules"] == extract.rules_hash()
    assert all(isinstance(t["took"], int) for t in commit["post"]["tests"]) and isinstance(sent["took"], int)
    assert sent["blobs"] == [
        {"file": asked["blobs"][0], "blob": codebase_checks._tree(tested, local.tip)["money.py"][1]}
    ]
    assert sent["tip"]["tests"][0]["passed"] == 2
    wire = gzip.decompress(results.calls[0].request.content).decode()
    for word in ("hunter2", "test_mul", "calc.py", "falcon_calc", str(tmp_path), "pytest", "PYTHONPATH", "assert "):
        assert word not in wire, word
    assert (local.home / "sent" / "results-1.json").read_text() == wire
    assert (local.home / "sent" / "profile-1.json").read_bytes() == gzip.decompress(posted.calls[0].request.content)
    env = (tmp_path / "env.txt").read_text()
    assert "AWS_SECRET_ACCESS_KEY" not in env and "hunter2" not in env and "METAPHI_API_KEY" not in env


def _measured_side(log: str) -> dict:
    """A side as codebase_checks returns it, holding every field any part could hold, and more runs than any takes."""
    run = {"exit": 1, "seconds": 2.5, "log": log, "step": "test", "class": "test_assertion", "codes": ["E0308"] * 3}
    run |= {"reported": 2, "passed": 1, "report": "junit", "cases": {"test_add": "passed", "test_mul": "failed"}}
    build = {"exit": 0, "seconds": 61.0, "log": log, "step": "compile", "class": "ok"}
    return {
        "build": {**build, "reported": 3, "passed": 3, "report": "summary", "cases": {"test_x": "passed"}},
        "tests": [dict(run) for _ in range(5)],
        "offline": dict(run),
        "installed_bytes": 1 << 30,
        "memory_peak_bytes": 1 << 28,
        "coverage": [{"file": 1, "unit": "statement", "total": 10, "covered": 7}],
    }


@respx.mock
def test_a_full_stubbed_run_validates_part_by_part(tested, tmp_path, capsys, monkeypatch):
    log = tmp_path / "run.log"
    log.write_text("FAILED tests/test_calc.py::test_mul - assert 6 == 5\n")
    raw = {
        "commits": [{"pre": _measured_side(str(log)), "post": _measured_side(str(log))}] * 5,
        "mutants": [{"post": _measured_side(str(log))}] * 4 + [{"broke": True}],
        "tip": _measured_side(str(log)),
    }
    monkeypatch.setattr(codebase_checks, "run", lambda *a, **k: raw)
    _signed_in()
    _, local = _profile(tested)
    calc = next(i for i, p in local.kept["paths"].items() if p == "calc.py")
    mutant = {"file": calc, "op": "arith", "nth": 0}
    money = next(i for i, p in local.kept["paths"].items() if p == "money.py")
    test = next(i for i, p in local.kept["paths"].items() if p == "tests/test_calc.py")
    raw["tip"]["coverage"] = [{"file": f, "unit": "line", "total": 9, "covered": 4} for f in (money, test)]
    asked = _next(local.kept["paths"], commits=[1, 2, 3, 4, 5], mutants=[mutant] * 5, blobs=[money] * 64, runs=3)
    posted = respx.post(f"{API}/codeval").respond(json={"run": "cv_1", "object": "codeval", "next": asked})
    results = respx.post(f"{API}/codeval/cv_1/results").respond(json={"done": True})
    assert cli.main(["codeval", str(tested), "--json", "--", _command(tmp_path)]) == 0
    (_,), (sent,) = _bodies(posted), _bodies(results)
    schema.check("Results", sent)
    assert len(sent["commits"]) == 5 and len(sent["mutants"]) == 5 and len(sent["blobs"]) == 64
    for c in sent["commits"]:
        assert len(c["pre"]["tests"]) == 1 and len(c["post"]["tests"]) == 3
        for part in (c["pre"], c["post"]):
            assert set(part) == {"build", "tests"}, "only the tip is measured beyond pass and fail"
            assert set(part["build"]) == {"step", "class", "exit", "took", "log"}, "a build counts no tests"
    assert sent["mutants"][-1] == {**mutant, "error": "unknown"}
    tip = sent["tip"]
    assert {"offline", "installed_bytes", "memory_peak_bytes", "coverage"} <= set(tip) and len(tip["tests"]) == 3
    run = tip["tests"][0]
    assert run["codes"] == ["E0308"] and run["took"] == codeval.took(2.5) and tip["build"]["took"] == codeval.took(61)
    assert [c["file"] for c in tip["coverage"]] == [money], "coverage is the tip's source files only"


@respx.mock
def test_a_batch_larger_than_results_can_answer_is_refused(tested, tmp_path, capsys):
    _signed_in()
    _, local = _profile(tested)
    asked = _next(local.kept["paths"], commits=[1, 2, 3, 4, 5, 6])
    respx.post(f"{API}/codeval").respond(json={"run": "cv_1", "object": "codeval", "next": asked})
    results = respx.post(f"{API}/codeval/cv_1/results").respond(json={"done": True})
    assert cli.main(["codeval", str(tested), "--json", "--", _command(tmp_path)]) != 0
    assert not results.called


@respx.mock
def test_a_decline_is_said_plainly_and_nothing_more_is_run_or_sent(tested, tmp_path, capsys):
    _signed_in()
    respx.post(f"{API}/codeval").respond(json={"run": "cv_2", "object": "codeval", "decision": "decline"})
    results = respx.post(f"{API}/codeval/cv_2/results").respond(json={"done": True})
    assert cli.main(["codeval", str(tested), "--", "exit 9"]) == 0
    out = capsys.readouterr().out
    assert "Metaphi AI declined falcon_calc (cv_2); nothing more is sent for it." in out
    assert not results.called and "Sent cv_2" not in out


@respx.mock
def test_a_run_killed_part_way_goes_on_from_the_server_and_runs_nothing_twice(tested, tmp_path, capsys, monkeypatch):
    _signed_in()
    _, local = _profile(tested)
    asked = _next(local.kept["paths"])
    posted = respx.post(f"{API}/codeval").respond(json={"run": "cv_3", "object": "codeval", "next": asked})
    looked = respx.get(f"{API}/codeval/cv_3").respond(json={"state": "measuring", "next": asked})
    results = respx.post(f"{API}/codeval/cv_3/results").respond(json={"done": True})
    ran = []
    real = codebase_checks._commit
    monkeypatch.setattr(codebase_checks, "_commit", lambda *a, **k: ran.append(a[4]) or real(*a, **k))
    sent = codeval.send_results

    def killed(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(codeval, "send_results", killed)
    command = _command(tmp_path)
    assert cli.main(["codeval", str(tested), "--json", "--", command]) == 130
    assert ran and not results.called and posted.call_count == 1
    monkeypatch.setattr(codeval, "send_results", sent)
    first = len(ran)
    capsys.readouterr()
    assert cli.main(["codeval", str(tested), "--json", "--", command]) == 0
    assert len(ran) == first, "a finished commit is read back, never run again"
    assert posted.call_count == 1 and looked.call_count == 1 and results.call_count == 1
    schema.check("Results", _bodies(results)[0])
    assert json.loads(capsys.readouterr().out)["repos"][0]["state"] == "done"


@respx.mock
def test_not_signed_in_with_no_terminal_starts_the_code_and_stops_at_once(tested, capsys, monkeypatch):
    from urllib.parse import parse_qsl

    monkeypatch.delenv("METAPHI_API_KEY")
    start = respx.post(f"{A}/device/code").respond(
        json={
            "device_code": "dev",
            "user_code": "WDJB-MJHT",
            "verification_uri": f"{A}/device",
            "verification_uri_complete": f"{A}/device?user_code=WDJB-MJHT",
            "interval": 5,
            "expires_in": 600,
        }
    )
    token = respx.post(f"{A}/oauth/token").respond(400, json={"error": "authorization_pending"})
    profile = respx.post(f"{API}/codeval")
    for _ in range(2):
        assert cli.main(["codeval", str(tested), "--json"]) == cli.AUTH
        said = json.loads(capsys.readouterr().out)
        assert said["error"]["code"] == "sign_in_required" and said["error"]["type"] == "auth_error"
        assert said["error"]["next_step"] == "kennet login --wait 300 --json" and said["sends"] == codeval.DOCS
        assert said["sign_in"]["user_code"] == "WDJB-MJHT" and said["sign_in"]["verification_uri"] == f"{A}/device"
        assert said["sign_in"]["verification_uri_complete"].endswith("user_code=WDJB-MJHT")
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", said["sign_in"]["expires_at"])
    assert start.call_count == 1, "a rerun reuses the code still pending"
    assert dict(parse_qsl(start.calls[0].request.content.decode())) == {"client_id": "metaphi-cli"}
    assert token.call_count == 1 and not profile.called

    monkeypatch.setattr(accounts, "_sleep", lambda s: None)
    assert cli.main(["login", "--wait", "0", "--json"]) == cli.LATER
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "sign_in_pending"
    respx.post(f"{A}/oauth/token").respond(json={"access_token": "tok"})
    respx.post(f"{A}/keys/cli").respond(
        json={
            "email": "sam@acme.test",
            "org": "org_acme",
            "org_name": "Acme",
            "scopes": ["kennet:codeval:write"],
            "api_key": "mai_sk_live_x",
            "test_api_key": "mai_sk_test_x",
        }
    )
    assert cli.main(["login", "--wait", "300", "--json"]) == 0
    said = json.loads(capsys.readouterr().out)
    assert said == {
        "email": "sam@acme.test",
        "org": "org_acme",
        "org_name": "Acme",
        "scopes": ["kennet:codeval:write"],
        "next_step": f"kennet codeval {tested} --json",
    }
    assert accounts.pending() is None


@respx.mock
def test_a_pending_code_is_reused_whatever_it_was_started_for(tested, capsys, monkeypatch):
    monkeypatch.delenv("METAPHI_API_KEY")
    left = {"device_code": "d", "user_code": "OLD-CODE", "url": "u", "page": "u", "interval": 5}
    config.save({accounts.PENDING: {**left, "expires_at": time.time() + 600, "scope": "kennet:other"}})
    start = respx.post(f"{A}/device/code")
    respx.post(f"{A}/oauth/token").respond(400, json={"error": "authorization_pending"})
    assert cli.main(["codeval", str(tested), "--json"]) == cli.AUTH
    assert json.loads(capsys.readouterr().out)["sign_in"]["user_code"] == "OLD-CODE" and not start.called


@respx.mock
def test_a_sign_in_whose_keys_lack_codeval_says_so_and_sends_nothing(tested, capsys, monkeypatch):
    monkeypatch.delenv("METAPHI_API_KEY")
    left = {"device_code": "d", "user_code": "C", "url": "u", "page": "u", "interval": 5}
    config.save({accounts.PENDING: {**left, "expires_at": time.time() + 600}})
    respx.post(f"{A}/oauth/token").respond(json={"access_token": "tok"})
    respx.post(f"{A}/keys/cli").respond(
        json={
            "email": "sam@acme.test",
            "org": "org_acme",
            "org_name": "Acme",
            "scopes": ["kennet:twin"],
            "api_key": "mai_sk_live_x",
            "test_api_key": "mai_sk_test_x",
        }
    )
    profile = respx.post(f"{API}/codeval")
    assert cli.main(["codeval", str(tested), "--json"]) == cli.AUTH
    said = json.loads(capsys.readouterr().out)["error"]
    assert said["code"] == "insufficient_scope" and "Acme do not hold CodeVal" in said["message"]
    assert "kennet:codeval:write" in said["message"] and not profile.called


@respx.mock
def test_a_denied_code_says_nothing_was_sent(tested, capsys, monkeypatch):
    monkeypatch.delenv("METAPHI_API_KEY")
    respx.post(f"{A}/device/code").respond(json={"device_code": "d", "user_code": "C", "verification_uri": "u"})
    respx.post(f"{A}/oauth/token").respond(400, json={"error": "authorization_pending"})
    assert cli.main(["codeval", str(tested), "--json"]) == cli.AUTH
    capsys.readouterr()
    respx.post(f"{A}/oauth/token").respond(400, json={"error": "access_denied"})
    assert cli.main(["login", "--wait", "10", "--json"]) == cli.AUTH
    said = json.loads(capsys.readouterr().out)["error"]
    assert said["message"] == "Sign-in denied. Nothing was sent. Run the same command for a new code."


@pytest.mark.parametrize(
    "words, named",
    [
        (["--yes"], "--yes"),
        (["--dry-run"], "--dry-run"),
        (["--detach"], "--detach"),
        (["--deep"], "--deep"),
        (["--build", "make"], "--build"),
        (["--test=make test"], "--test"),
        (["status"], "kennet codeval status"),
        (["send"], "kennet codeval send"),
        (["--"], "after --"),
    ],
)
def test_what_codeval_no_longer_takes_is_named_cleanly(words, named, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["codeval", ".", *words] if words[0].startswith("-") else ["codeval", *words])
    assert e.value.code == 2 and named in capsys.readouterr().err


def test_a_command_after_the_double_dash_is_one_shell_line():
    got = cli.parser().parse_args(["codeval", "a", "b", "--json", "--", "make", "test", "--fast"])
    assert got.paths == ["a", "b"] and got.json and got.run_command == "make test --fast"


def test_the_client_holds_no_selection_or_scoring():
    import metaphi

    here = Path(metaphi.__file__).parent
    assert not (here / "codeval_draws.py").exists()
    code = "\n".join(p.read_text() for p in sorted(here.glob("*.py")) if p.name.startswith(("codeval", "codebase_")))
    for pattern in (
        r"\bimport random\b",
        r"\brandom\.",
        r"secrets\.(choice|randbelow|SystemRandom)",
        r"\bshuffle\(",
        r"codeval_draws",
        r"def \w*(pick|draw|score|select|sample)\w*\(",
        r"\"/codeval/[^\"]*/(picks|plan|checks|logs|setup|progress)\"",
        r"f\"/codeval/\{\w+\}/(picks|plan|checks|logs|setup|progress)\"",
    ):
        assert not re.search(pattern, code), pattern


# -- the review of #13 ------------------------------------------------------------------------------------------------


@respx.mock
def test_a_run_goes_on_against_its_filed_profile_when_the_tip_moves(tested, tmp_path, capsys, monkeypatch):
    _signed_in()
    _, local = _profile(tested)
    filed_tip, money = local.tip, next(i for i, p in local.kept["paths"].items() if p == "money.py")
    asked = _next(local.kept["paths"], commits=[], mutants=[])
    posted = respx.post(f"{API}/codeval").respond(json={"run": "cv_4", "object": "codeval", "next": asked})
    respx.get(f"{API}/codeval/cv_4").respond(json={"state": "measuring", "next": asked})
    results = respx.post(f"{API}/codeval/cv_4/results").respond(json={"done": True})
    command = _command(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(codeval, "send_results", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
        assert cli.main(["codeval", str(tested), "--json", "--", command]) == 130
    (tested / "money.py").write_text("RATE = 0\n" * 200)
    git(tested, "commit", "-qam", "moved on")
    capsys.readouterr()
    assert cli.main(["codeval", str(tested), "--json", "--", command]) == 0
    assert posted.call_count == 1, "the tip moved: the run goes on, no new profile is filed"
    (sent,) = _bodies(results)
    assert sent["blobs"] == [{"file": money, "blob": codebase_checks._tree(tested, filed_tip)["money.py"][1]}]
    assert "measured against the profile it was filed with" in capsys.readouterr().err


@respx.mock
def test_a_run_whose_filed_tip_was_rewritten_away_restarts_at_the_tip(tested, tmp_path, capsys, monkeypatch):
    _signed_in()
    _, local = _profile(tested)
    asked = _next(local.kept["paths"], commits=[], mutants=[])
    posted = respx.post(f"{API}/codeval").mock(
        side_effect=[
            httpx.Response(200, json={"run": "cv_5", "object": "codeval", "next": asked}),
            httpx.Response(200, json={"run": "cv_5", "object": "codeval", "done": True}),
        ]
    )
    with monkeypatch.context() as m:
        m.setattr(codeval, "measured", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
        assert cli.main(["codeval", str(tested), "--json", "--", "true"]) == 130
    codeval_runs.Run(local.home).put("falcon_calc", "commit", 2, {"commit": 2})
    git(tested, "reset", "-q", "--hard", "HEAD~1")
    git(tested, "reflog", "expire", "--expire=now", "--all")
    git(tested, "gc", "-q", "--prune=now")
    capsys.readouterr()
    assert cli.main(["codeval", str(tested), "--json", "--", "true"]) == 0
    first, second = _bodies(posted)
    head = codeval._text(tested, "rev-parse", "HEAD")
    assert "restart" not in first and second["restart"] is True and second["tip"] == head
    out, err = capsys.readouterr()
    assert json.loads(out)["repos"][0]["state"] == "done"
    assert err.count("replaces the earlier measurements") == 1 and "no longer in this clone" in err
    assert codeval_runs.kept_profile(local.home)["tip"] == head
    assert codeval_runs.Run(local.home).get("falcon_calc", "commit", 2) is None, "the old ids are never read again"
    assert (local.home / "replaced" / "checks-1.jsonl").exists()


@respx.mock
def test_blob_ids_are_revealed_only_for_source_files_of_at_least_1_kib(tested, tmp_path, monkeypatch):
    _signed_in()
    _, local = _profile(tested)
    calc = next(i for i, p in local.kept["paths"].items() if p == "calc.py")
    test = next(i for i, p in local.kept["paths"].items() if p == "tests/test_calc.py")
    ran = []
    monkeypatch.setattr(codebase_checks, "run", lambda *a, **k: ran.append(a) or {})
    for small in (calc, test):
        respx.post(f"{API}/codeval").respond(
            json={"run": "cv_6", "object": "codeval", "next": _next(local.kept["paths"], blobs=[small])}
        )
        results = respx.post(f"{API}/codeval/cv_6/results").respond(json={"done": True})
        codeval_runs.keep_run(local.home, run=None)
        assert cli.main(["codeval", str(tested), "--json", "--", _command(tmp_path)]) == 1
        assert not ran and not results.called
    assert codeval.blobs(local, [calc, test]) == []


def test_the_tip_says_why_it_failed_rather_than_nothing():
    key = b"k" * 32
    assert codeval.tip(None, key) == {"tests": [], "error": "unknown"}
    broke = {"build": {"exit": 1, "seconds": 3, "step": "dep_resolve", "class": "dep_unresolved"}, "tests": []}
    assert codeval.tip(broke, key) == {"tests": [], "error": "dep_unresolved"}, "an error sends no runs"
    with pytest.raises(schema.Refused, match="never both"):
        schema.check("Tip", {**codeval.side(broke, key, "Tip"), "error": "dep_unresolved"})
    ran = {"tests": [{"exit": 0, "seconds": 1, "class": "ok"}]}
    assert "error" not in codeval.tip(ran, key)
    for got in (codeval.tip(None, key), codeval.tip(broke, key)):
        schema.check("Tip", got)


@pytest.mark.parametrize(
    "more, why",
    [
        ({"runs": 4}, "follow Next.runs: it is above 3"),
        ({"run_minutes": 241}, "above 240"),
        ({"hours_left": 25}, "above 24"),
        ({"batch": 7}, "above 6"),
        ({"commits": [1, 2, 3, 4, 5, 6]}, "too many"),
        ({"blobs": list(range(65))}, "too many"),
    ],
)
def test_instructions_beyond_the_schema_are_refused_before_anything_runs(tested, more, why):
    _, local = _profile(tested)
    with pytest.raises(schema.Refused, match=why):
        codeval.instructed(_next(local.kept["paths"], **more))


def test_scoped_npm_packages_and_poetry_behind_an_own_index_are_counted_not_named(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for k in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_CONFIG_FILE", "POETRY_CONFIG_DIR"):
        monkeypatch.delenv(k, raising=False)
    lock = {"packages": {"node_modules/@acme/ledger": {"resolved": "https://registry.npmjs.org/@acme/ledger.tgz"}}}
    poetry = b'[[package]]\nname = "billing-core"\nversion = "1"\n'
    files = {"package-lock.json": json.dumps(lock).encode(), "poetry.lock": poetry}
    tree = {p: ("100644", "0" * 40, 1) for p in files}
    assert codeval._dependencies(tree, files.get) == ([{"eco": "pypi", "name": "billing-core"}], 1)
    files["pyproject.toml"] = b'[[tool.poetry.source]]\nname = "acme"\nurl = "https://pypi.acme.test/simple"\n'
    tree["pyproject.toml"] = ("100644", "0" * 40, 1)
    assert codeval._dependencies(tree, files.get) == ([], 2)
    del files["pyproject.toml"], tree["pyproject.toml"]
    monkeypatch.setenv("PIP_INDEX_URL", "https://pypi.acme.test/simple")
    assert codeval._dependencies(tree, files.get) == ([], 2)


def test_the_disclosure_is_one_literal_line():
    (line,) = codeval.disclosure(["./core", "./ledger"])
    assert line == (
        "CodeVal measures ./core ./ledger here and sends Metaphi AI typed measurements: no source code, logs, file "
        "paths, author names or commit text. Public dependency names and standard build-file names are included. "
        f"Exactly what: {codeval.DOCS}"
    )


@respx.mock
def test_the_key_id_names_the_key_without_it_and_a_mismatch_says_where_the_run_goes_on(
    tested, tmp_path, capsys, monkeypatch
):
    doc, local = _profile(tested)
    want = hmac_hex(local.key, "kennet-codeval-key")
    assert doc["key_id"] == want and local.key.hex() not in json.dumps(doc)
    schema.check("Profile", doc)
    _signed_in()
    asked = _next(local.kept["paths"], commits=[], mutants=[])
    mismatch = {"error": {"type": "invalid_request_error", "code": "key_mismatch", "message": "x"}}
    posted = respx.post(f"{API}/codeval").mock(
        side_effect=[
            httpx.Response(409, json=mismatch),
            httpx.Response(200, json={"run": "cv_7", "object": "codeval", "next": asked}),
        ]
    )
    results = respx.post(f"{API}/codeval/cv_7/results").respond(json={"done": True})
    assert cli.main(["codeval", str(tested), "--json", "--", _command(tmp_path)]) == 0
    first, second = _bodies(posted)
    assert "restart" not in first and second["restart"] is True and second["key_id"] == want
    assert _bodies(results)[0]["key_id"] == want
    err = capsys.readouterr().err
    assert err.count("replaces the earlier measurements of this repository") == 1
    assert "started on another machine" in err
    assert sorted(p.name for p in (local.home / "sent").iterdir()) == [
        "profile-1.json",
        "profile-2.json",
        "results-1.json",
    ]


def hmac_hex(key: bytes, label: str) -> str:
    import hashlib
    import hmac

    return hmac.new(key, label.encode(), hashlib.sha256).hexdigest()[:16]


def test_an_image_the_schema_does_not_name_is_counted_not_named():
    from metaphi import codebase_env

    assert codebase_env.public("mcr.microsoft.com/mssql/server:2022-latest") is None
    got = codeval._env({"service_images": ["postgres", "acme-db"], "other_images": 1})
    assert (got["service_images"], got["other_images"]) == (["postgres"], 2)


@pytest.mark.parametrize(
    "name, body",
    [
        ("Step", {**STEP, "log": "0" * 64 + "\n"}),
        ("Step", {**STEP, "codes": ["E0308\n"]}),
        ("Toolchain", {"tool": "python", "version": "3.12.1\n"}),
    ],
)
def test_a_value_with_a_trailing_newline_is_refused(name, body):
    with pytest.raises(schema.Refused):
        schema.check(name, body)


def test_value_patterns_end_at_the_end_of_the_value():
    assert not codeval.is_url("https://git.acme.test/x.git\n") or Path("https://git.acme.test/x.git\n").exists()
    assert not codeval.TRAILER_KEY.match("signed-off-by\n")
    assert not codeval.PUBLIC_NAME.match("left-pad\n")


def test_headers_pass_over_objects_a_partial_clone_does_not_hold(repo):
    tip = codeval._text(repo, "rev-parse", "HEAD")
    got = codeval._headers(repo, ["0" * 40, tip, "f" * 40], "author")
    assert got["0" * 40] == {} and got["f" * 40] == {} and got[tip]["author"].startswith("Sam Seller")


@respx.mock
def test_a_failed_tip_answers_the_batch_alone_and_nothing_else_runs(tested, tmp_path, monkeypatch):
    calls = []
    broke = {"build": {"exit": 1, "seconds": 3, "step": "dep_resolve", "class": "dep_unresolved"}, "tests": []}
    monkeypatch.setattr(
        codebase_checks,
        "run",
        lambda *a, **k: calls.append((a[5], a[13])) or {"tip": broke, "commits": [], "mutants": []},
    )
    _signed_in()
    _, local = _profile(tested)
    respx.post(f"{API}/codeval").respond(json={"run": "cv_9", "object": "codeval", "next": _next(local.kept["paths"])})
    results = respx.post(f"{API}/codeval/cv_9/results").respond(json={"done": True})
    assert cli.main(["codeval", str(tested), "--json", "--", _command(tmp_path)]) == 0
    (sent,) = _bodies(results)
    schema.check("Results", sent)
    assert sent["tip"] == {"tests": [], "error": "dep_unresolved"}
    assert (sent["commits"], sent["mutants"], sent["blobs"]) == ([], [], [])
    assert calls == [([], [])], "only the tip was run"


def test_a_post_side_short_of_its_runs_is_not_sent_as_run():
    key, ok = b"k" * 32, {"exit": 0, "seconds": 1, "class": "ok"}
    pre = {"tests": [ok]}
    assert codeval._unit({"pre": pre, "post": {"tests": [ok]}}, key, ("pre", "post"), 2) == {"error": "unknown"}
    full = codeval._unit({"pre": pre, "post": {"tests": [ok, ok]}}, key, ("pre", "post"), 2)
    assert len(full["post"]["tests"]) == 2
    stopped = {"exit": None, "seconds": 9, "class": "test_timeout"}
    assert "post" in codeval._unit({"post": {"tests": [stopped]}}, key, ("post",), 3), "a timeout stops the rest"
    with pytest.raises(schema.Refused, match="never both"):
        schema.check("CommitResult", {"commit": 1, "error": "unknown", **full})


@pytest.mark.parametrize(
    "change, why",
    [
        (lambda cs: cs.reverse(), "parents come before"),
        (lambda cs: cs[0].update(reverts=99), "revert names"),
        (lambda cs: cs[1].update(commit=cs[0]["commit"]), "its own id"),
    ],
)
def test_the_history_sent_runs_parents_first(tested, change, why):
    doc, _ = _profile(tested)
    assert doc["commits"][-1]["commit"] == len(doc["commits"]) and doc["commits"][0]["parents"] == []
    change(doc["commits"])
    with pytest.raises(schema.Refused, match=why):
        schema.check("Profile", doc)


@respx.mock
def test_a_profile_of_another_tip_is_filed_again_at_the_tip_the_server_holds(tested, tmp_path, capsys):
    _signed_in()
    _, local = _profile(tested)
    filed = codeval._text(tested, "rev-parse", "HEAD~1")
    refused = {
        "error": {
            "type": "invalid_request_error",
            "code": "profile_mismatch",
            "message": "run cv_10 is measuring another tip; measure that tip and the run goes on where it stopped",
            "filed_tip": filed,
        }
    }
    posted = respx.post(f"{API}/codeval").mock(
        side_effect=[httpx.Response(409, json=refused), httpx.Response(200, json={"run": "cv_10", "done": True})]
    )
    assert cli.main(["codeval", str(tested), "--json", "--", "true"]) == 0
    first, second = _bodies(posted)
    assert first["tip"] == local.tip and second["tip"] == filed
    assert codeval_runs.kept_profile(local.home)["tip"] == filed


@respx.mock
def test_a_client_too_old_for_the_service_says_so(tested, capsys):
    _signed_in()
    respx.post(f"{API}/codeval").respond(
        426, json={"error": {"type": "invalid_request_error", "code": "upgrade_required", "message": "old"}}
    )
    assert cli.main(["codeval", str(tested), "--json", "--", "true"]) == 5
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "upgrade_required"


@respx.mock
@pytest.mark.parametrize(
    "cls, word",
    [("env_network", "network"), ("env_resource", "memory"), ("env_timeout", "time"), ("env_permission", "permission")],
)
def test_a_tip_that_ends_in_the_environment_is_sent_once_then_stops_and_a_rerun_measures_again(
    tested, tmp_path, capsys, monkeypatch, cls, word
):
    ran = []
    ended = {"build": {"exit": 1, "seconds": 3, "step": "dep_resolve", "class": cls}, "tests": []}
    monkeypatch.setattr(codebase_checks, "_side", lambda *a, **k: ran.append(1) or json.loads(json.dumps(ended)))
    _signed_in()
    _, local = _profile(tested)
    asked = _next(local.kept["paths"], commits=[], mutants=[], blobs=[])
    respx.post(f"{API}/codeval").respond(json={"run": "cv_11", "object": "codeval", "next": asked})
    respx.get(f"{API}/codeval/cv_11").respond(json={"state": "measuring", "next": asked})
    results = respx.post(f"{API}/codeval/cv_11/results").respond(json={"run": "cv_11", "next": asked})
    assert cli.main(["codeval", str(tested), "--json", "--", "true"]) == 6, "the server asks again; the CLI stops"
    said = json.loads(capsys.readouterr().out)["error"]
    assert said["code"] == "environment" and cls in said["message"] and word in said["message"] + said["next_step"]
    assert results.call_count == 1 and _bodies(results)[0]["tip"] == {"tests": [], "error": cls}
    assert codeval_runs.Run(local.home).get("falcon_calc", "tip") is None, "an environment's end is never kept"
    assert cli.main(["codeval", str(tested), "--json", "--", "true"]) == 6
    assert len(ran) == 2 and results.call_count == 2, "a rerun measures the tip again"


@respx.mock
def test_every_body_sent_is_kept_under_its_own_name_and_a_kept_profile_is_filed_before_head(tested, tmp_path, capsys):
    _signed_in()
    _, local = _profile(tested)
    filed = local.tip
    codeval.file(local)
    (tested / "money.py").write_text("RATE = 0\n" * 200)
    git(tested, "commit", "-qam", "moved on")
    posted = respx.post(f"{API}/codeval").respond(json={"run": "cv_12", "object": "codeval", "done": True})
    assert cli.main(["codeval", str(tested), "--json", "--", "true"]) == 0
    assert codeval_runs.run_state(local.home)["run"] == "cv_12"
    (profile,) = _bodies(posted)
    assert profile["tip"] == filed and posted.call_count == 1, "the profile kept here is filed, not HEAD's"
    for n in (1, 2, 3):
        assert codeval_runs.sent(local.home, "results", b"{}").name == f"results-{n}.json"
    assert (local.home / "sent" / "profile-1.json").exists()
    assert codeval_runs.sent(local.home, "profile", b"{}").name == "profile-2.json"


@respx.mock
def test_a_mismatch_at_a_tip_this_clone_does_not_hold_restarts_at_the_tip(tested, tmp_path, capsys):
    _signed_in()
    _, local = _profile(tested)
    refused = {"type": "invalid_request_error", "code": "profile_mismatch", "message": "m", "filed_tip": "ab" * 20}
    posted = respx.post(f"{API}/codeval").mock(
        side_effect=[
            httpx.Response(409, json={"error": refused}),
            httpx.Response(200, json={"run": "cv_13", "done": True}),
        ]
    )
    assert cli.main(["codeval", str(tested), "--json", "--", "true"]) == 0
    first, second = _bodies(posted)
    assert first["tip"] == second["tip"] == local.tip and second["restart"] is True
    assert "is not in this clone" in capsys.readouterr().err


def test_the_profile_is_the_tips_own_history_whatever_other_branches_hold(tested):
    git(tested, "checkout", "-q", "-b", "side", "HEAD~1")
    (tested / "side.py").write_text("SIDE = 1\n")
    git(tested, "add", "side.py")
    git(tested, "commit", "-qm", "side")
    side = codeval._text(tested, "rev-parse", "HEAD")
    git(tested, "checkout", "-q", "-")
    doc, local = _profile(tested)
    assert side not in local.kept["order"] and len(doc["commits"]) == int(
        codeval._text(tested, "rev-list", "--count", "HEAD")
    )
    assert local.kept["order"][local.tip] == doc["commits"][-1]["commit"], "the tip is the last commit"
    stray = {**doc["commits"][-1], "commit": len(doc["commits"]) + 1, "parents": [doc["commits"][0]["commit"]]}
    with pytest.raises(schema.Refused, match="in its history"):
        schema.check("Profile", {**doc, "commits": [*doc["commits"], stray]})


@respx.mock
def test_a_run_the_server_ends_after_the_tip_keeps_failing_in_the_environment_ends_cleanly(
    tested, tmp_path, capsys, monkeypatch
):
    ended = {"build": {"exit": 1, "seconds": 3, "step": "dep_resolve", "class": "env_network"}, "tests": []}
    monkeypatch.setattr(codebase_checks, "_side", lambda *a, **k: json.loads(json.dumps(ended)))
    _signed_in()
    _, local = _profile(tested)
    asked = _next(local.kept["paths"], commits=[], mutants=[], blobs=[])
    respx.post(f"{API}/codeval").respond(json={"run": "cv_14", "object": "codeval", "next": asked})
    respx.get(f"{API}/codeval/cv_14").respond(json={"state": "measuring", "next": asked})
    results = respx.post(f"{API}/codeval/cv_14/results").mock(
        side_effect=[httpx.Response(200, json={"run": "cv_14", "next": asked})] * 2
        + [httpx.Response(200, json={"run": "cv_14", "done": True})]
    )
    for _ in range(2):
        assert cli.main(["codeval", str(tested), "--json", "--", "true"]) == 6
    capsys.readouterr()
    assert cli.main(["codeval", str(tested), "--json", "--", "true"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["repos"][0]["state"] == "done" and results.call_count == 3
    assert "kept ending in this machine's environment (env_network), so Metaphi AI ended the run" in err
