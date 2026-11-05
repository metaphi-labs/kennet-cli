from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from metaphi.steps import NONE, Steps

MAX_TURNS = 40
MINUTES = 45.0
COMMAND_MINUTES = 20.0
SHOWN = 6000
TOKENS = 4096


CACHE = "/kennet/cache"
CACHES = {"CCACHE_DIR": f"{CACHE}/ccache", "PIP_CACHE_DIR": f"{CACHE}/pip", "UV_CACHE_DIR": f"{CACHE}/uv",
          "npm_config_cache": f"{CACHE}/npm", "GOMODCACHE": f"{CACHE}/gomod", "GOCACHE": f"{CACHE}/gobuild",
          "GRADLE_USER_HOME": f"{CACHE}/gradle", "MAVEN_OPTS": f"-Dmaven.repo.local={CACHE}/m2",
          "CARGO_HOME": f"{CACHE}/cargo"}


@dataclass(frozen=True)
class Recipe:
    setup: str
    build: str
    test: str
    image: str | None = None
    uncounted: str = ""


def engine() -> str | None:
    for name in ("docker", "podman"):
        if shutil.which(name):
            try:
                if subprocess.run([name, "info"], capture_output=True, timeout=30, check=False).returncode == 0:
                    return name
            except (OSError, subprocess.TimeoutExpired):
                continue
    return None


class Shell:

    def __init__(self, engine: str | None, image: str | None, src: Path, junit: Path, jobs: int = 0,
                 cache: Path | None = None):
        self.engine, self.image, self.src, self.junit, self.name = engine, image, src, junit, None
        self.jobs = jobs or os.cpu_count() or 1
        if engine and image:
            name = f"kennet-setup-{uuid.uuid4().hex[:12]}"
            shared = ["-v", f"{cache}:{CACHE}"] if cache else []
            got = subprocess.run([engine, "run", "-d", "--name", name, "-v", f"{src}:/work", "-v",
                                  f"{junit}:/kennet/junit", *shared, "-w", "/work", "--entrypoint", "sh", image, "-c",
                                  "sleep infinity"], capture_output=True, timeout=1800, check=False)
            if got.returncode != 0:
                raise RuntimeError(got.stderr.decode("utf-8", "replace")[-2000:] or f"{engine} run failed")
            self.name = name

    def line(self, command: str, tests: str) -> str:
        if not self.name:
            return command
        given = {"KENNET_TESTS": tests, "KENNET_JUNIT": "/kennet/junit", "KENNET_JOBS": str(self.jobs), **CACHES}
        return shlex.join([self.engine, "exec", "-w", "/work", *[x for k, v in given.items() for x in ("-e", f"{k}={v}")],
                           self.name, "sh", "-c", command])

    def env(self, tests: str) -> dict[str, str]:
        return {**os.environ, "KENNET_TESTS": tests, "KENNET_JUNIT": str(self.junit), "KENNET_JOBS": str(self.jobs)}

    def stop(self) -> None:
        if self.name:
            subprocess.run([self.engine, "exec", self.name, "sh", "-c", "kill -9 -1"], capture_output=True,
                           timeout=60, check=False)

    def empty(self) -> None:
        if self.name:
            subprocess.run([self.engine, "exec", self.name, "sh", "-c",
                            "rm -rf /kennet/junit/* /kennet/junit/.[!.]* 2>/dev/null; true"],
                           capture_output=True, timeout=120, check=False)
        for x in self.junit.iterdir():
            shutil.rmtree(x, ignore_errors=True) if x.is_dir() else x.unlink(missing_ok=True)

    def keep(self) -> str | None:
        if not self.name:
            return None
        tag = f"kennet-setup:{uuid.uuid4().hex[:12]}"
        got = subprocess.run([self.engine, "commit", self.name, tag], capture_output=True, timeout=1800, check=False)
        return tag if got.returncode == 0 else None

    def close(self) -> None:
        if self.name:
            subprocess.run([self.engine, "exec", self.name, "chmod", "-R", "a+rwX", "/work", "/kennet/junit"],
                           capture_output=True, timeout=600, check=False)
            subprocess.run([self.engine, "rm", "-f", self.name], capture_output=True, timeout=600, check=False)
            self.name = None


def forget(engine: str | None, images: list[str]) -> None:
    for tag in images:
        if engine and tag.startswith("kennet-setup:"):
            subprocess.run([engine, "rmi", "-f", tag], capture_output=True, timeout=600, check=False)


SYSTEM = """You set up a software repository so its own build and its own tests run, unattended, from fresh files.

The repository's files at one past commit are in {where}. {box}
Find out how it builds and tests by reading it (build files, CI configuration, READMEs, scripts), then try.

When it works, call `propose` with three shell commands, each run with `sh -c` in that directory:
- setup: installs the toolchains and system packages the build needs; {setup_kept}
- build: fetches the dependencies the repository's own files name and compiles; `true` where nothing builds.
  Build in parallel with $KENNET_JOBS jobs (make -j, cmake --build -j, ninja, mvn -T...).
- test: runs the repository's whole test suite, the way its CI would, in under {minutes:g} minutes.
  Where the runner can, it writes JUnit XML into the directory $KENNET_JUNIT (pytest --junitxml, Maven Surefire's
  reports copied there, go-junit-report, jest-junit, ctest --output-junit...): tests are told apart by their names.
  $KENNET_TESTS lists, one per line, the test files the commit changed; the command must still run the whole suite.
`propose` runs setup and build and then test from fresh files {fresh}. It is taken when the build passes and the tests
ran and were counted, at least {floor:.0%} of them passing (a few failing at this commit is normal). They are counted
from the JUnit XML in $KENNET_JUNIT, else from the summary line the runner prints (pytest, unittest, CTest, Meson,
Automake's test-suite, Cargo, `go test -v`, Maven, Jest, TAP): keep that line in the output. Only where the suite can
give neither, say why in `uncounted`; then the test command's exit code alone decides, and it must be 0. Otherwise you
see what happened and go on. The same commands will run at older commits too, so name versions the repository itself asks for, not ones only
this commit happens to suit. No command may change a file the repository holds (no sed -i, no patch): what is
built is the code as it is; generated files and a build directory are fine. Be quick: read the CI configuration and
build files first, and propose as soon as you have a plausible recipe, since `propose` is the real test.

Each command you run stops after {command_minutes:g} minutes. Keep output short (tail, grep). Call `give_up` with the
reason if the build needs something that cannot be had here (a licensed toolchain, a service, credentials, hardware)."""

BOX = ("Your commands run in a Linux container started from the image you `start` (an official one such as "
       "ubuntu:24.04, python:3.12, maven:3-eclipse-temurin-21, node:22, golang:1.23, rust:1); you are root and "
       "the network is open. Start one first.")
HOST = ("Your commands run on the person's own machine ({os}), in a scratch copy of the files: install nothing "
        "system-wide, use no sudo, and write nothing outside that directory except the toolchains' own caches. "
        "Use what is installed; where something is missing, say so with `give_up`.")

FLOOR = 0.5
GAVE_UP = ("toolchain", "service", "credentials", "license", "hardware", "other")
Why = Literal["build_failed", "tests_timeout", "tests_failed", "no_test_count", "pass_fraction",
              "tracked_file_changed", "no_image", "no_container", "turns", "minutes", "none_found",
              "gave_up_toolchain", "gave_up_service", "gave_up_credentials", "gave_up_license", "gave_up_hardware",
              "gave_up_other"]

TOOLS = [
    {"name": "start", "description": "Start a fresh container from an image, the files mounted at /work; replaces "
     "the one running.", "input_schema": {"type": "object", "properties": {"image": {"type": "string"}},
                                          "required": ["image"]}},
    {"name": "run", "description": "Run a shell command where the files are; answers its exit code and the end of "
     "what it printed.", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                                         "required": ["command"]}},
    {"name": "propose", "description": "Run setup, build and test from fresh files; taken if the build passes and "
     "the tests ran and were counted, enough of them passing.", "input_schema": {"type": "object", "properties": {
         "setup": {"type": "string"}, "build": {"type": "string"}, "test": {"type": "string"},
         "uncounted": {"type": "string", "description": "Only where the suite can write neither JUnit nor a "
                       "summary line a runner prints: why. Then exit 0 alone takes it."}},
         "required": ["setup", "build", "test"]}},
    {"name": "give_up", "description": "Stop: what this needs cannot be had here.",
     "input_schema": {"type": "object", "properties": {
         "why": {"type": "string", "enum": list(GAVE_UP)}, "reason": {"type": "string"}},
         "required": ["why", "reason"]}},
]


class Model:

    def __init__(self, reach: dict[str, Any], client: httpx.Client | None = None):
        self.model = reach["model"]
        self.http = client or httpx.Client(base_url=reach["base_url"].rstrip("/"), timeout=600,
                                           headers={"x-api-key": reach["token"],
                                                    "anthropic-version": "2023-06-01"})
        self.tokens = 0

    def turn(self, system: str, messages: list[dict]) -> dict:
        body = {"model": self.model, "max_tokens": TOKENS, "tools": TOOLS,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": _cached(messages)}
        for wait in (5, 20, 60, None):
            r = self.http.post("/v1/messages", json=body)
            if r.status_code < 500 and r.status_code != 429 or wait is None:
                break
            time.sleep(wait)
        r.raise_for_status()
        got = r.json()
        u = got.get("usage") or {}
        self.tokens += sum(u.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                   "cache_creation_input_tokens"))
        return got


def _cached(messages: list[dict]) -> list[dict]:
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last["content"] if isinstance(last["content"], list) else [{"type": "text", "text": last["content"]}]
    last["content"] = [*content[:-1], {**content[-1], "cache_control": {"type": "ephemeral"}}]
    return [*messages[:-1], last]


def _shown(code: int | None, log: Path) -> str:
    try:
        said = log.read_bytes()[-SHOWN:].decode("utf-8", "replace")
    except OSError:
        said = ""
    return f"exit: {'stopped (too long)' if code is None else code}\n{said}"


def find(write: Callable[[Path], None], tests: str, model: Model, engine: str | None, logs: Path,
         say: Callable[[str], None], run_minutes: float, cache: Path | None = None, steps: Steps = NONE,
         later: tuple[Recipe, str] | None = None) -> tuple[Recipe | None, dict[str, Any]]:
    from metaphi.codebase_checks import _junit, _run
    started, turns, tokens = time.monotonic(), 0, model.tokens
    snaps: list[str] = []
    logs.mkdir(parents=True, exist_ok=True)
    talk = logs / "conversation.jsonl"
    turns_log = logs / "turns.jsonl"
    scratch = Path(tempfile.mkdtemp(prefix="kennet-setup-"))
    shells: list[Shell] = []
    found: Recipe | None = None
    why: Why | None = None
    report: str | None = None

    def fresh(tag: str) -> tuple[Path, Path]:
        src, junit = scratch / tag / "src", scratch / tag / "junit"
        write(src)
        junit.mkdir(parents=True)
        return src, junit

    def say_to(log_name: str, shell: Shell, command: str, minutes: float) -> tuple[int | None, Path]:
        log = logs / log_name
        with log.open("wb") as f:
            code, _ = _run(shell.line(command, tests), shell.src, shell.env(tests), minutes * 60, f)
        return code, log

    src, junit = fresh("work")
    shell = Shell(None, None, src, junit)
    shells.append(shell)
    system = SYSTEM.format(where="/work" if engine else str(src),
                           box=BOX if engine else HOST.format(os=platform.system()),
                           fresh="in a new container from the image you started" if engine else "",
                           setup_kept=("it runs once and is kept in the image every commit's build starts from."
                                       if engine else "it runs before every build, so keep it to what is needed."),
                           minutes=run_minutes, command_minutes=COMMAND_MINUTES, floor=FLOOR)
    ask = "Set it up."
    if later:
        recipe, failed = later
        ask = ("At a later commit of this repository these commands built and tested it:\n"
               + json.dumps({"setup": recipe.setup, "build": recipe.build, "test": recipe.test}, indent=1)
               + "\nHere, at an older commit, the build failed; the end of what it printed:\n" + failed[-SHOWN:]
               + (f"\nThe image {recipe.image} has the later commit's setup installed; starting from it saves "
                  "the installs." if engine and recipe.image else "")
               + "\nSet it up at this commit: keep what still holds, change what this commit's own files ask for.")
    messages: list[dict] = [{"role": "user", "content": ask}]
    try:
        while turns < MAX_TURNS and time.monotonic() - started < MINUTES * 60 and not found:
            turns += 1
            with steps.step("setup_model", turn=turns):
                got = model.turn(system, messages)
            messages.append({"role": "assistant", "content": got["content"]})
            with talk.open("a") as f:
                f.write(json.dumps(messages[-1]) + "\n")
            with turns_log.open("a") as f:
                f.write(json.dumps(messages[-1]) + "\n")
            calls = [b for b in got["content"] if b.get("type") == "tool_use"]
            if not calls:
                messages.append({"role": "user", "content": "Go on with the tools: run, propose or give_up."})
                continue
            answers, stop = [], False
            for c in calls:
                name, args = c["name"], c.get("input") or {}
                if name == "start" and engine:
                    try:
                        shell.close()
                        shell = Shell(engine, str(args.get("image", "")), src, junit, cache=cache)
                        shells.append(shell)
                        said = f"started {args.get('image')}"
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                        said, why = f"could not start it: {e}", "no_container"
                elif name == "start":
                    said = "no container engine here: your commands run on this machine"
                elif name == "run":
                    if engine and not shell.name:
                        said = "start a container first"
                    else:
                        with steps.step("setup_commands", turn=turns):
                            code, log = say_to(f"run-{turns:02d}.log", shell, str(args.get("command", "")),
                                               COMMAND_MINUTES)
                        if code is None:
                            shell.stop()
                        said = _shown(code, log)
                elif name == "propose" and engine and not shell.name:
                    said = "start a container first"
                elif name == "propose":
                    snap = shell.keep() if engine else None
                    if snap:
                        snaps.append(snap)
                    recipe = Recipe(str(args.get("setup", "")), str(args.get("build", "")),
                                    str(args.get("test", "")), (snap or shell.image) if engine else None,
                                    str(args.get("uncounted", "")))
                    say("    trying commands")
                    with steps.step("setup_proposals", turn=turns):
                        said, kept, why, report = _tried(recipe, engine, fresh(f"try-{turns:02d}"), tests, logs,
                                                         turns, run_minutes, _run, _junit, cache)
                    if kept is not False:
                        found = Recipe(recipe.setup, recipe.build, recipe.test, kept or None, recipe.uncounted)
                        stop = True
                elif name == "give_up":
                    gave = str(args.get("why", "other"))
                    said, stop, why = "stopped", True, f"gave_up_{gave if gave in GAVE_UP else 'other'}"
                    with (logs / "gave-up.txt").open("w") as f:
                        f.write(str(args.get("reason", "")))
                else:
                    said = f"no tool {name}"
                answers.append({"type": "tool_result", "tool_use_id": c["id"], "content": said})
            messages.append({"role": "user", "content": answers})
            with talk.open("a") as f:
                f.write(json.dumps(messages[-1]) + "\n")
            with turns_log.open("a") as f:
                f.write(json.dumps({"role": "user", "content": [
                    {**a, "content": str(a["content"]).split("\n", 1)[0]} for a in answers]}) + "\n")
            if stop:
                break
    finally:
        for s in shells:
            s.close()
        forget(engine, snaps)
        shutil.rmtree(scratch, ignore_errors=True)
    if found:
        (logs / "recipe.json").write_text(json.dumps(found.__dict__, indent=1))
    elif why is None or not why.startswith("gave_up"):
        why = why or ("turns" if turns >= MAX_TURNS else "minutes")
    return found, {"by": "model", "worked": found is not None, "steps": turns, "seconds": round(time.monotonic() - started),
                   "tokens": model.tokens - tokens, "container": engine is not None, "why": None if found else why,
                   "report": report if found else None}


def _held(src: Path, only: dict[str, str] | None = None) -> dict[str, str]:
    def digest(f: Path) -> str:
        try:
            return hashlib.blake2b(f.read_bytes(), digest_size=16).hexdigest()
        except OSError:
            return ""
    if only is not None:
        return {p: digest(src / p) if (src / p).is_file() else "" for p in only}
    return {str(f.relative_to(src)): digest(f) for f in src.rglob("*") if f.is_file() and not f.is_symlink()}


def _tried(recipe: Recipe, engine: str | None, dirs: tuple[Path, Path], tests: str, logs: Path, turn: int,
           run_minutes: float, run: Callable, junit_of: Callable, cache: Path | None = None
           ) -> tuple[str, str | bool, Why | None, str | None]:
    from metaphi.codebase_reading import counted
    src, junit = dirs
    held = _held(src)
    try:
        shell = Shell(engine, recipe.image, src, junit, cache=cache)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        return f"could not start {recipe.image}: {e}", False, "no_container", None
    said: list[str] = []

    def no(why: Why, text: str) -> tuple[str, bool, Why, None]:
        return "\n".join(said) + f"\nnot taken: {text}", False, why, None
    try:
        logged = {}
        for step, command in (("setup+build", f"({recipe.setup or 'true'}) && ({recipe.build or 'true'})"),
                              ("test", recipe.test)):
            log = logged[step] = logs / f"try-{turn:02d}.{step}.log"
            with log.open("wb") as f:
                code, _ = run(shell.line(command, tests), shell.src, shell.env(tests), run_minutes * 60, f)
            said.append(f"{step}: {_shown(code, log)}")
            if step != "test" and code != 0:
                return no("build_failed", "the build did not pass")
        if code is None:
            return no("tests_timeout", "the tests ran too long")
        now = _held(src, held)
        if changed := sorted(p for p, h in held.items() if now.get(p) != h)[:5]:
            return no("tracked_file_changed", "it changed the repository's own files: " + ", ".join(changed))
        cases = junit_of(junit)
        report, counts = ("junit", (len(cases), sum(cases.values()))) if cases else (None, None)
        if not counts and (counts := counted(logged["test"])):
            report = "summary"
        if counts:
            ran, passed = counts
            if passed < FLOOR * ran:
                return no("pass_fraction", f"{passed} of {ran} tests passed, under {FLOOR:.0%}: a test command or "
                          "environment problem, not this commit's failures")
            note = f"{ran} tests counted ({report}), {passed} passed"
        elif not recipe.uncounted:
            return no("tests_failed" if code else "no_test_count",
                      "no tests were counted: write JUnit into $KENNET_JUNIT or keep the runner's summary line in the "
                      "output; only where the suite can give neither, say why in `uncounted`")
        elif code != 0:
            return no("tests_failed", "the tests failed, and nothing counted them")
        else:
            note = "not counted: the exit code alone tells runs apart"
        image = shell.keep() if engine else ""
        if engine and not image:
            return no("no_image", "the container could not be kept as an image")
        return "\n".join(said) + f"\ntaken. {note}", image or "", None, report
    finally:
        shell.close()


__all__ = ["Model", "Recipe", "Shell", "engine", "find", "forget"]
