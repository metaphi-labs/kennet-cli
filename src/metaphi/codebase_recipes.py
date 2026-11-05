from __future__ import annotations

import json
import re
import shlex
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from metaphi.codebase_setup import Recipe, Why, _tried
from metaphi.steps import NONE, Steps

try:
    import tomllib
except ImportError:   # Python 3.10
    tomllib = None

TRIES = 6
MINUTES = 60.0

PY = "python:3.12"
GO = "golang:1.23"
RUST = "rust:1"
NODE = "node:22"
JDK = "21"
C = "gcc:14"
BASE = "buildpack-deps:noble"
JOBS = "${KENNET_JOBS:-2}"
VENV = ".kennet-venv"

TESTS = re.compile(r"\b(test|tests|check|pytest|ctest|tox|nox|jest|vitest|mocha|phpunit|rspec|unittest)\b")
BUILDS = re.compile(r"\b(build|compile|make|cmake|meson|ninja|configure|setup\.py|bootstrap|autoreconf|gradlew|mvn)\b")
INSTALLS = re.compile(r"\b(apt-get|apt|yum|dnf|apk|brew|pip|pip3|uv|poetry|npm|yarn|pnpm|corepack|go get|go install|"
                      r"rustup|cargo install|gem|bundle|conda)\b")
LINT = re.compile(r"\b(lint|ruff|flake8|pylint|mypy|pyright|black|isort|eslint|prettier|clippy|fmt|gofmt|"
                  r"golangci-lint|codespell|pre-commit|typos|codecov|coveralls|upload|deploy|publish|twine|docs?)\b")
SYSTEM = re.compile(r"(^|[\s;&|(])(sudo|apt-get|apt|yum|dnf|apk|brew|choco|snap|pacman|zypper)\s")
SUDO = re.compile(r"\bsudo\s+(-E\s+)?")
EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
NO_TESTS = "no test specified"
PYTEST = "python -m pytest -q -p no:cacheprovider --junitxml=\"$KENNET_JUNIT\"/pytest.xml"


def _first(v: Any) -> Any:
    return v[0] if isinstance(v, list) and v else v


def _subst(text: str, matrix: dict) -> str | None:
    def one(m: re.Match) -> str:
        key = m.group(1)
        if key.startswith("matrix."):
            got: Any = matrix
            for part in key[7:].split("."):
                got = got.get(part) if isinstance(got, dict) else None
            got = _first(got)
            if isinstance(got, (str, int, float)) and not isinstance(got, bool):
                return str(got)
        raise KeyError(key)
    try:
        return EXPR.sub(one, text)
    except KeyError:
        return None


def _version(v: Any, matrix: dict) -> str | None:
    got = _subst(str(_first(v)), matrix) if _first(v) is not None else None
    if not got or not re.fullmatch(r"[0-9][0-9.]*(\.x)?", got):
        return None
    return got.removesuffix(".x").rstrip(".")


def _yaml(body: bytes) -> Any:
    try:
        return yaml.safe_load(body.decode("utf-8", "replace"))
    except yaml.YAMLError:
        return None


def _script(lines: list[str]) -> str:
    return " && ".join(f"bash -ec {shlex.quote(x.strip())}" for x in lines if x and x.strip()) or "true"


def _split(lines: list[str]) -> tuple[list[str], list[str], str] | None:
    lines = [x for x in lines if not LINT.search(x)]
    at = next((i for i in range(len(lines) - 1, -1, -1) if TESTS.search(lines[i])), None)
    if at is None:
        return None
    before = lines[:at]
    build = [x for x in before if BUILDS.search(x) and not INSTALLS.search(x)]
    return [x for x in before if x not in build], build, lines[at]


def _fallback(paths: set[str]) -> str:
    return (GO if "go.mod" in paths else RUST if "Cargo.toml" in paths else
            f"maven:3-eclipse-temurin-{JDK}" if "pom.xml" in paths else f"eclipse-temurin:{JDK}" if "gradlew" in paths
            else NODE if "package.json" in paths else
            PY if paths & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"} else BASE)


def _image_of(uses: list[tuple[str, dict]], matrix: dict, paths: set[str]) -> str:
    for action, args in uses:
        name = action.split("@")[0].lower()
        if name == "actions/setup-python" and (v := _version(args.get("python-version"), matrix)):
            return f"python:{v}"
        if name == "actions/setup-go" and (v := _version(args.get("go-version"), matrix)):
            return f"golang:{v}"
        if name == "actions/setup-node" and (v := _version(args.get("node-version"), matrix)):
            return f"node:{v}"
        if name == "actions/setup-java" and (v := _version(args.get("java-version"), matrix)):
            return f"maven:3-eclipse-temurin-{v}" if "pom.xml" in paths else f"eclipse-temurin:{v}"
        if name in ("dtolnay/rust-toolchain", "actions-rs/toolchain"):
            return RUST
    return _fallback(paths)


def _github(body: bytes, paths: set[str]) -> list[Recipe]:
    doc = _yaml(body)
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    out = []
    for job in jobs.values() if isinstance(jobs, dict) else []:
        if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
            continue
        runs_on = str(_first(job.get("runs-on")) or "")
        if runs_on and "${{" not in runs_on and not re.search(r"ubuntu|linux", runs_on, re.IGNORECASE):
            continue
        strategy = job.get("strategy") if isinstance(job.get("strategy"), dict) else {}
        matrix = strategy.get("matrix") if isinstance(strategy.get("matrix"), dict) else {}
        runs: list[str] = []
        uses: list[tuple[str, dict]] = []
        for s in job["steps"]:
            if not isinstance(s, dict) or s.get("if") is not None and "${{" in str(s.get("if")):
                continue
            if isinstance(s.get("uses"), str):
                uses.append((s["uses"], s.get("with") if isinstance(s.get("with"), dict) else {}))
            if isinstance(s.get("run"), str) and (text := _subst(s["run"], matrix)) is not None:
                wd = s.get("working-directory")
                runs.append(f"cd {shlex.quote(wd)}\n{text}" if isinstance(wd, str) and "${{" not in wd else text)
        if not (got := _split(runs)):
            continue
        container = job.get("container")
        image = container.get("image") if isinstance(container, dict) else container
        if not isinstance(image, str) or "${{" in image:
            image = _image_of(uses, matrix, paths)
        setup, build, test = got
        out.append(Recipe(_script(setup), _script(build), _script([test]), image))
    return out


def _gitlab(body: bytes, paths: set[str]) -> list[Recipe]:
    doc = _yaml(body)
    if not isinstance(doc, dict):
        return []

    def named(v: Any) -> str | None:
        v = v.get("name") if isinstance(v, dict) else v
        return v if isinstance(v, str) and "$" not in v else None

    def lines(v: Any) -> list[str]:
        return [x for x in (v if isinstance(v, list) else [v] if isinstance(v, str) else []) if isinstance(x, str)]
    default = doc.get("default") if isinstance(doc.get("default"), dict) else {}
    image = named(default.get("image")) or named(doc.get("image"))
    head = lines(default.get("before_script") or doc.get("before_script"))
    out = []
    for name, job in doc.items():
        if not isinstance(job, dict) or "script" not in job or str(name).startswith("."):
            continue
        script = lines(job.get("before_script")) or head
        script = [*script, *lines(job["script"])]
        if any(re.search(r"\$\{?CI_", x) for x in script) or not (got := _split(script)):
            continue
        setup, build, test = got
        out.append(Recipe(_script(setup), _script(build), _script([test]),
                          named(job.get("image")) or image or _fallback(paths)))
    return out


def _travis(body: bytes, paths: set[str]) -> list[Recipe]:
    doc = _yaml(body)
    if not isinstance(doc, dict):
        return []

    def lines(key: str) -> list[str]:
        v = doc.get(key)
        return [x for x in (v if isinstance(v, list) else [v] if isinstance(v, str) else []) if isinstance(x, str)]
    test = [x for x in lines("script") if not LINT.search(x)]
    if not test:
        return []
    lang = str(doc.get("language") or "")
    image = {"python": f"python:{_version(doc.get('python'), {}) or '3.12'}", "go": GO, "rust": RUST,
             "node_js": f"node:{_version(doc.get('node_js'), {}) or '22'}", "java": f"maven:3-eclipse-temurin-{JDK}",
             "c": C, "cpp": C}.get(lang) or _fallback(paths)
    return [Recipe(_script(lines("before_install") + lines("install")), _script(lines("before_script")),
                   _script(test), image)]


def _targets(body: bytes) -> set[str]:
    return set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", body.decode("utf-8", "replace"), re.MULTILINE))


def _python(paths: set[str], read: Callable[[str], bytes | None]) -> list[Recipe]:
    packaged = bool(paths & {"pyproject.toml", "setup.py", "setup.cfg"})
    if not packaged and "requirements.txt" not in paths:
        return []
    image, extras, groups = PY, [], []
    if tomllib and (body := read("pyproject.toml")):
        try:
            got = tomllib.loads(body.decode("utf-8", "replace"))
        except tomllib.TOMLDecodeError:
            got = {}
        project = got.get("project") if isinstance(got.get("project"), dict) else {}
        least = re.search(r">=\s*3\.(\d+)", str(project.get("requires-python") or ""))
        if least and int(least.group(1)) > 12:
            image = f"python:3.{least.group(1)}"
        optional = project.get("optional-dependencies") if isinstance(project.get("optional-dependencies"), dict) else {}
        extras = [x for x in ("test", "tests", "testing", "dev") if x in optional]
        declared = got.get("dependency-groups") if isinstance(got.get("dependency-groups"), dict) else {}
        groups = [x for x in ("test", "tests", "dev") if x in declared]
    reqs = sorted(p for p in paths if re.fullmatch(r"requirements[\w.-]*\.txt|requirements/[\w.-]+\.txt", p)
                  and "doc" not in p)
    pip = ["pip install -q -U pip", *(f"pip install -q -r {shlex.quote(r)}" for r in reqs)]
    if packaged:
        pip.append("pip install -q -e " + shlex.quote(f".[{extras[0]}]" if extras else "."))
    pip += [f"pip install -q --group {g}" for g in groups[:1]]
    pip.append("pip install -q pytest")
    return [Recipe(" && ".join(pip), "true", PYTEST, image)]


def _node(paths: set[str], read: Callable[[str], bytes | None]) -> list[Recipe]:
    try:
        pkg = json.loads(read("package.json") or b"{}")
    except ValueError:
        return []
    scripts = pkg.get("scripts") if isinstance(pkg, dict) and isinstance(pkg.get("scripts"), dict) else {}
    if not isinstance(scripts.get("test"), str) or NO_TESTS in scripts["test"]:
        return []
    run, install = (("pnpm", "corepack enable && pnpm install --frozen-lockfile") if "pnpm-lock.yaml" in paths else
                    ("yarn", "corepack enable && (yarn install --immutable || yarn install)") if "yarn.lock" in paths
                    else ("npm", "npm ci" if "package-lock.json" in paths else "npm install"))
    build = install + (f" && {run} run build" if isinstance(scripts.get("build"), str) else "")
    return [Recipe("true", build, f"CI=true {run} test", NODE)]


def _markers(paths: set[str], read: Callable[[str], bytes | None]) -> list[Recipe]:
    out: list[Recipe] = []
    if "go.mod" in paths:
        v = re.search(rb"^go\s+1\.(\d+)", read("go.mod") or b"", re.MULTILINE)
        out.append(Recipe("true", "go build ./...", "go test -v ./...",
                          f"golang:1.{max(int(v.group(1)), 21)}" if v else GO))
    if "Cargo.toml" in paths:
        out.append(Recipe("true", f"cargo build --tests -j {JOBS}", "cargo test --no-fail-fast", RUST))
    if "pom.xml" in paths:
        copy = "s=$?; find . -path '*/surefire-reports/*.xml' -exec cp {} \"$KENNET_JUNIT\"/ \\; ; exit $s"
        out.append(Recipe("true", f"mvn -B -q -T {JOBS} -DskipTests install", f"mvn -B -fae test; {copy}",
                          f"maven:3-eclipse-temurin-{JDK}"))
    if "gradlew" in paths:
        copy = "s=$?; find . -path '*/test-results/*' -name '*.xml' -exec cp {} \"$KENNET_JUNIT\"/ \\; ; exit $s"
        out.append(Recipe("true", "sh ./gradlew --no-daemon testClasses",
                          f"sh ./gradlew --no-daemon test --continue; {copy}", f"eclipse-temurin:{JDK}"))
    if "package.json" in paths:
        out += _node(paths, read)
    out += _python(paths, read)
    if "CMakeLists.txt" in paths:
        out.append(Recipe("command -v cmake >/dev/null || (apt-get update -qq && apt-get install -y -qq cmake)",
                          f"cmake -S . -B build -DBUILD_TESTING=ON && cmake --build build -j {JOBS}",
                          f"ctest --test-dir build --output-on-failure -j {JOBS} --output-junit \"$KENNET_JUNIT\"/ctest.xml",
                          C))
    if "meson.build" in paths:
        out.append(Recipe("command -v meson >/dev/null || (apt-get update -qq && apt-get install -y -qq meson ninja-build)",
                          "meson setup build && meson compile -C build", "meson test -C build", C))
    targets = _targets(read("Makefile") or read("GNUmakefile") or b"")
    if paths & {"configure", "configure.ac"}:
        conf = "./configure" if "configure" in paths else "autoreconf -fi && ./configure"
        tools = "command -v autoreconf >/dev/null || (apt-get update -qq && apt-get install -y -qq autoconf automake libtool)"
        out.append(Recipe("true" if "configure" in paths else tools, f"{conf} && make -j {JOBS}",
                          "make check" if "check" in targets or "configure.ac" in paths else "make test", C))
    elif targets & {"test", "check"}:
        out.append(Recipe("true", f"make -j {JOBS}" if targets - {"test", "check", "clean"} else "true",
                          "make check" if "check" in targets else "make test", C))
    return out


def candidates(paths: set[str], read: Callable[[str], bytes | None]) -> list[Recipe]:
    got: list[Recipe] = []
    for p in sorted(p for p in paths if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))):
        if body := read(p):
            got += _github(body, paths)
    if ".gitlab-ci.yml" in paths and (body := read(".gitlab-ci.yml")):
        got += _gitlab(body, paths)
    if ".travis.yml" in paths and (body := read(".travis.yml")):
        got += _travis(body, paths)
    got += _markers(paths, read)
    seen, out = set(), []
    for r in got:
        key = (r.setup, r.build, r.test, r.image)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def contained(r: Recipe) -> Recipe:
    return Recipe(SUDO.sub("", r.setup), SUDO.sub("", r.build), SUDO.sub("", r.test), r.image)


def hosted(r: Recipe) -> Recipe | None:
    setup = "true" if r.setup.startswith("command -v") else r.setup
    if SYSTEM.search(f" {setup}") or SYSTEM.search(f" {r.build}"):
        return None
    path = f'export PATH="$PWD/{VENV}/bin:$PATH"'
    if "pip" in setup or "python" in r.test or "pytest" in r.test:
        return Recipe(f"python3 -m venv {VENV} && {path} && {setup}", f"{path} && {r.build}", f"{path} && {r.test}")
    return Recipe(setup, r.build, r.test)


def find(write: Callable[[Path], None], read: Callable[[str], bytes | None], paths: set[str], tests: str,
         engine: str | None, logs: Path, say: Callable[[str], None], run_minutes: float, cache: Path | None = None,
         steps: Steps = NONE, skip: Recipe | None = None) -> tuple[Recipe | None, dict[str, Any]]:
    from metaphi.codebase_checks import _junit, _run
    started = time.monotonic()
    logs.mkdir(parents=True, exist_ok=True)
    every = candidates(paths, read)
    chosen = [contained(r) for r in every] if engine else [h for r in every if (h := hosted(r))]
    chosen = [r for r in chosen if not skip or (r.setup, r.build, r.test) != (skip.setup, skip.build, skip.test)]
    (logs / "candidates.json").write_text(json.dumps([r.__dict__ for r in chosen], indent=1))
    found: Recipe | None = None
    why: Why | None = None if chosen else "none_found"
    report: str | None = None
    tried = 0
    scratch = Path(tempfile.mkdtemp(prefix="kennet-setup-"))
    try:
        for r in chosen[:TRIES]:
            if time.monotonic() - started > MINUTES * 60:
                break
            tried += 1
            src, junit = scratch / f"try-{tried:02d}" / "src", scratch / f"try-{tried:02d}" / "junit"
            write(src)
            junit.mkdir(parents=True)
            say(f"    trying commands ({tried} of {min(len(chosen), TRIES)})")
            with steps.step("setup_proposals", turn=tried):
                _said, kept, why, report = _tried(r, engine, (src, junit), tests, logs, tried, run_minutes, _run,
                                                  _junit, cache)
            if kept is not False:
                found = Recipe(r.setup, r.build, r.test, kept or None, r.uncounted)
                break
            shutil.rmtree(scratch / f"try-{tried:02d}", ignore_errors=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if found:
        (logs / "recipe.json").write_text(json.dumps(found.__dict__, indent=1))
    return found, {"by": "local", "worked": found is not None, "steps": tried,
                   "seconds": round(time.monotonic() - started), "tokens": 0, "container": engine is not None,
                   "why": None if found else why, "report": report if found else None}


__all__ = ["candidates", "contained", "find", "hosted"]
