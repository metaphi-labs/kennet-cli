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
    assert record[0]["build"]["exit"] == 0 and isinstance(record[0]["build"]["seconds"], int)
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


def test_time_is_a_day_after_the_first_commit_and_the_authors_hour_of_week(tmp_path):
    r = tmp_path / "falcon_time"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.py").write_text("x = 1\n")
    git(r, "add", ".")
    when = {"GIT_AUTHOR_DATE": "2024-03-04T10:30:00+0200", "GIT_COMMITTER_DATE": "2024-03-04T10:30:00+0200"}
    git(r, "commit", "-qm", "a", **when)
    (r / "a.py").write_text("x = 2\n")
    git(r, "add", ".")
    later = {"GIT_AUTHOR_DATE": "2024-03-10T23:00:00-0500", "GIT_COMMITTER_DATE": "2024-03-11T09:00:00-0500"}
    git(r, "commit", "-qm", "b\n\nCo-authored-by: Claude <noreply@anthropic.com>", **later)
    doc, _ = _profile(r)
    a, b = doc["commits"]
    assert doc["first_day"] == 19786 and (a["day"], a["hour"], a["same_time"]) == (0, 10, True)
    assert (b["day"], b["hour"], b["same_time"]) == (7, 6 * 24 + 23, False)
    assert b["trailers"] == [{"key": "co-authored-by", "tool": "anthropic"}]
    assert "0200" not in json.dumps(doc) and "0500" not in json.dumps(doc)


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


def test_tags_carry_their_day_and_release_shape_but_not_their_names(repo):
    git(repo, "tag", "falcon-launch", "HEAD~1")
    git(repo, "tag", "-a", "v2.1.0", "-m", "billing", "HEAD", GIT_COMMITTER_DATE="2030-03-01T00:00:00Z")
    doc, _ = _profile(repo)
    tags = doc["git"]["tags"]
    assert [(t["commit"], t["annotated"], t["release"]) for t in tags] == [(2, False, False), (3, True, True)]
    assert tags[0]["day"] is None and tags[1]["day"] > 0
    assert "falcon-launch" not in json.dumps(doc) and "v2.1.0" not in json.dumps(doc)


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


STEP = {"exit": 1, "seconds": 3, "error": "test_failure", "log": "0" * 64}


@pytest.mark.parametrize(
    "name, body",
    [
        ("Step", {**STEP, "note": "it broke in ledger.py"}),
        ("Step", {**STEP, "error": "AssertionError in test_ledger"}),
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
    return {
        "batch": 1,
        "tip": True,
        "commits": [2],
        "mutants": [{"file": calc, "op": "arith", "nth": 0}],
        "blobs": [calc],
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
    assert mutant["op"] == "arith" and mutant["post"]["tests"][0]["error"] == "test_failure"
    assert sent["blobs"] == [
        {"file": asked["blobs"][0], "blob": codebase_checks._tree(tested, local.tip)["calc.py"][1]}
    ]
    assert sent["tip"]["tests"][0]["passed"] == 2
    wire = gzip.decompress(results.calls[0].request.content).decode()
    for word in ("hunter2", "test_mul", "calc.py", "falcon_calc", str(tmp_path), "pytest", "PYTHONPATH", "assert"):
        assert word not in wire, word
    assert (local.home / "sent" / "results-1.json").read_text() == wire
    assert (local.home / "sent" / "profile.json").read_bytes() == gzip.decompress(posted.calls[0].request.content)
    env = (tmp_path / "env.txt").read_text()
    assert "AWS_SECRET_ACCESS_KEY" not in env and "hunter2" not in env and "METAPHI_API_KEY" not in env


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
    assert dict(parse_qsl(start.calls[0].request.content.decode()))["scope"] == "kennet:codeval:write"
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
