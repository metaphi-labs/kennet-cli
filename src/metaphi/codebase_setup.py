from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SHOWN = 6000


CACHE = "/kennet/cache"
NETWORK: list[str] = []
PIDS = 8192
CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID")
KEPT_ENV = frozenset(
    [
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "TERM",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
        "XDG_RUNTIME_DIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY",
        "CONTAINER_HOST",
        "CONTAINER_CONNECTION",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "JAVA_HOME",
        "GOPATH",
        "GOROOT",
        "GOCACHE",
        "GOMODCACHE",
        "GOFLAGS",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "PYENV_ROOT",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "NVM_DIR",
        "SDKMAN_DIR",
        "GRADLE_USER_HOME",
        "MAVEN_HOME",
        "M2_HOME",
        "ANDROID_HOME",
        "CC",
        "CXX",
        "CFLAGS",
        "CXXFLAGS",
        "LDFLAGS",
        "CPPFLAGS",
        "PKG_CONFIG_PATH",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "LIBRARY_PATH",
        "CPATH",
        "CCACHE_DIR",
    ]
)
KEPT_PREFIX = ("LC_", "KENNET_")
DROPPED_ENV = re.compile(
    r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|^METAPHI_|^AWS_|^GOOGLE_|^AZURE_|^GH_|^GITHUB_", re.IGNORECASE
)
CACHES = {
    "CCACHE_DIR": f"{CACHE}/ccache",
    "PIP_CACHE_DIR": f"{CACHE}/pip",
    "UV_CACHE_DIR": f"{CACHE}/uv",
    "npm_config_cache": f"{CACHE}/npm",
    "GOMODCACHE": f"{CACHE}/gomod",
    "GOCACHE": f"{CACHE}/gobuild",
    "GOFLAGS": "-count=1 -buildvcs=false",
    "GRADLE_USER_HOME": f"{CACHE}/gradle",
    "MAVEN_OPTS": f"-Dmaven.repo.local={CACHE}/m2 -Dmaven.javadoc.skip=true -Dgpg.skip=true",
    "CARGO_HOME": f"{CACHE}/cargo",
}


@dataclass(frozen=True)
class Recipe:
    setup: str
    build: str
    test: str
    image: str | None = None
    uncounted: str = ""


def allowed(env: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in env.items()
        if (k in KEPT_ENV or k.startswith(KEPT_PREFIX)) and (k.startswith("KENNET_") or not DROPPED_ENV.search(k))
    }


@functools.cache
def limits(engine: str) -> tuple[str, ...]:
    try:
        got = subprocess.run([engine, "info", "--format", "{{json .}}"], capture_output=True, timeout=60, check=False)
        info = json.loads(got.stdout or b"{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ()
    host = info.get("host") if isinstance(info.get("host"), dict) else {}
    cpus, memory = info.get("NCPU") or host.get("cpus"), info.get("MemTotal") or host.get("memTotal")
    out = []
    if isinstance(cpus, int) and cpus > 1:
        out.append(f"--cpus={cpus - 1}")
    if isinstance(memory, int) and memory > 1 << 30:
        out.append(f"--memory={memory * 3 // 4}")
    return tuple(out)


def hardened(engine: str) -> list[str]:
    return [
        "--init",
        "--cap-drop=ALL",
        *(f"--cap-add={c}" for c in CAPS),
        "--security-opt=no-new-privileges",
        f"--pids-limit={PIDS}",
        *limits(engine),
    ]


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
    def __init__(
        self, engine: str | None, image: str | None, src: Path, junit: Path, jobs: int = 0, cache: Path | None = None
    ):
        self.engine, self.image, self.src, self.junit, self.name = engine, image, src, junit, None
        self.extra: dict[str, str] = {}
        self.jobs = jobs or os.cpu_count() or 1
        if engine and image:
            name = f"kennet-setup-{uuid.uuid4().hex[:12]}"
            shared = ["-v", f"{cache}:{CACHE}"] if cache else []
            got = subprocess.run(
                [
                    engine,
                    "run",
                    "-d",
                    "--name",
                    name,
                    *hardened(engine),
                    "-v",
                    f"{src}:/work",
                    "-v",
                    f"{junit}:/kennet/junit",
                    *shared,
                    *NETWORK,
                    "-w",
                    "/work",
                    "--entrypoint",
                    "sh",
                    image,
                    "-c",
                    "sleep infinity",
                ],
                capture_output=True,
                timeout=1800,
                check=False,
            )
            if got.returncode != 0:
                raise RuntimeError(got.stderr.decode("utf-8", "replace")[-2000:] or f"{engine} run failed")
            self.name = name

    def line(self, command: str, tests: str) -> str:
        if not self.name:
            return command
        given = {
            "KENNET_TESTS": tests,
            "KENNET_JUNIT": "/kennet/junit",
            "KENNET_JOBS": str(self.jobs),
            **CACHES,
            **self.extra,
        }
        return shlex.join(
            [
                self.engine,
                "exec",
                "-w",
                "/work",
                *[x for k, v in given.items() for x in ("-e", f"{k}={v}")],
                self.name,
                "sh",
                "-c",
                command,
            ]
        )

    def env(self, tests: str) -> dict[str, str]:
        return {
            **allowed(dict(os.environ)),
            "KENNET_TESTS": tests,
            "KENNET_JUNIT": str(self.junit),
            "KENNET_JOBS": str(self.jobs),
            **self.extra,
        }

    def _exec(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                [str(self.engine), *args], capture_output=True, timeout=timeout, check=False, text=True
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def memory_peak(self) -> int | None:
        if not self.name:
            return None
        got = self._exec("exec", self.name, "cat", "/sys/fs/cgroup/memory.peak")
        return int(got.stdout) if got and got.returncode == 0 and got.stdout.strip().isdigit() else None

    def offline(self) -> bool:
        if not self.name:
            return False
        got = self._exec("inspect", "-f", "{{json .NetworkSettings.Networks}}", self.name)
        if not got or got.returncode != 0:
            return False
        try:
            networks = list(json.loads(got.stdout) or {})
        except ValueError:
            return False
        for net in networks:
            if net == "none":
                continue
            out = self._exec("network", "disconnect", "-f", net, self.name)
            if not out or out.returncode != 0:
                return False
        return bool(networks)

    def stop(self) -> None:
        if self.name:
            subprocess.run(
                [self.engine, "exec", self.name, "sh", "-c", "kill -9 -1"], capture_output=True, timeout=60, check=False
            )

    def empty(self) -> None:
        if self.name:
            subprocess.run(
                [
                    self.engine,
                    "exec",
                    self.name,
                    "sh",
                    "-c",
                    "rm -rf /kennet/junit/* /kennet/junit/.[!.]* 2>/dev/null; true",
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
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
            subprocess.run(
                [self.engine, "exec", self.name, "chmod", "-R", "a+rwX", "/work", "/kennet/junit"],
                capture_output=True,
                timeout=600,
                check=False,
            )
            subprocess.run([self.engine, "rm", "-f", self.name], capture_output=True, timeout=600, check=False)
            self.name = None


def forget(engine: str | None, images: list[str]) -> None:
    for tag in images:
        if engine and tag.startswith("kennet-setup:"):
            subprocess.run([engine, "rmi", "-f", tag], capture_output=True, timeout=600, check=False)


FLOOR = 0.5
Why = Literal[
    "build_failed",
    "tests_timeout",
    "tests_failed",
    "no_test_count",
    "pass_fraction",
    "tracked_file_changed",
    "no_image",
    "no_container",
    "none_found",
]


def _shown(code: int | None, log: Path) -> str:
    try:
        said = log.read_bytes()[-SHOWN:].decode("utf-8", "replace")
    except OSError:
        said = ""
    return f"exit: {'stopped (too long)' if code is None else code}\n{said}"


def _held(src: Path, only: dict[str, str] | None = None) -> dict[str, str]:
    def digest(f: Path) -> str:
        try:
            return hashlib.blake2b(f.read_bytes(), digest_size=16).hexdigest()
        except OSError:
            return ""

    if only is not None:
        return {p: digest(src / p) if (src / p).is_file() else "" for p in only}
    return {
        str(f.relative_to(src)): digest(f)
        for f in src.rglob("*")
        if f.is_file() and not f.is_symlink() and ".git" not in f.relative_to(src).parts
    }


def _tried(
    recipe: Recipe,
    engine: str | None,
    dirs: tuple[Path, Path],
    tests: str,
    logs: Path,
    turn: int,
    run_minutes: float,
    run: Callable,
    junit_of: Callable,
    cache: Path | None = None,
    record: list[dict[str, Any]] | None = None,
) -> tuple[str, str | bool, Why | None, str | None]:
    from metaphi.codebase_checks import _ran, _tested

    src, junit = dirs
    ran: dict[str, Any] = {}
    if record is not None:
        record.append(ran)
    held = _held(src)
    try:
        shell = Shell(engine, recipe.image, src, junit, cache=cache)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        return f"could not start {recipe.image}: {e}", False, "no_container", None
    said: list[str] = []

    def no(why: Why, text: str) -> tuple[str, bool, Why, None]:
        return "\n".join(said) + f"\nnot taken: {text}", False, why, None

    try:
        for step, command in (
            ("setup+build", f"({recipe.setup or 'true'}) && ({recipe.build or 'true'})"),
            ("test", recipe.test),
        ):
            log = logs / f"try-{turn:02d}.{step}.log"
            with log.open("wb") as f:
                code, took = run(shell.line(command, tests), shell.src, shell.env(tests), run_minutes * 60, f)
            if step == "test":
                ran["test"] = _tested(code, took, junit_of(junit), log)
            else:
                ran["build"] = _ran(code, took)
            said.append(f"{step}: {_shown(code, log)}")
            if step != "test" and code != 0:
                return no("build_failed", "the build did not pass")
        if code is None:
            return no("tests_timeout", "the tests ran too long")
        now = _held(src, held)
        if changed := sorted(p for p, h in held.items() if now.get(p) != h)[:5]:
            return no("tracked_file_changed", "it changed the repository's own files: " + ", ".join(changed))
        report, counted, passed = ran["test"]["report"], ran["test"]["reported"], ran["test"]["passed"]
        if counted:
            if passed < FLOOR * counted:
                return no("pass_fraction", f"{passed} of {counted} tests passed, under {FLOOR:.0%}")
            note = f"{counted} tests counted ({report}), {passed} passed"
        elif not recipe.uncounted:
            return no(
                "tests_failed" if code else "no_test_count",
                "no tests were counted: write JUnit into $KENNET_JUNIT or keep the runner's summary line in the "
                "output; only where the suite can give neither, say why in `uncounted`",
            )
        elif code != 0:
            return no("tests_failed", "the tests failed, and nothing counted them")
        else:
            note = "not counted: the exit code alone is used"
        image = shell.keep() if engine else ""
        if engine and not image:
            return no("no_image", "the container could not be kept as an image")
        return "\n".join(said) + f"\ntaken. {note}", image or "", None, report
    finally:
        shell.close()


__all__ = ["Recipe", "Shell", "engine", "forget"]
