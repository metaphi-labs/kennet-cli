from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from metaphi import codebase_reading as reading
from metaphi import codebase_setup, errors, live
from metaphi import codeval_extract as extract
from metaphi.codebase_setup import Recipe, Shell, allowed
from metaphi.steps import NONE, Steps

RUN_MINUTES = 30.0
HOURS = 12.0
RANK = {"skipped": 0, "passed": 1, "failed": 2, "error": 3}


@dataclass(frozen=True)
class Plan:
    test: str | None = None
    run_minutes: float = RUN_MINUTES
    hours: float = HOURS
    logs: Path | None = None
    cache: Path | None = None
    steps: Steps = NONE
    ledger: Any = None
    alone: bool = False  # one unit at a time: services are shared, or the seller's command shares this machine's caches
    memory: int | None = None  # bytes each container may hold


class Tree(dict):
    links: dict[str, str]

    def __init__(self, *a: Any, links: dict[str, str] | None = None, **k: Any) -> None:
        super().__init__(*a, **k)
        self.links = links or {}


def _tree(repo: Path, sha: str) -> Tree:
    from metaphi.codeval import _out

    tree = Tree()
    for entry in _out(repo, "ls-tree", "-r", "-z", sha).split(b"\0"):
        if b"\t" in entry:
            meta, _, path = entry.partition(b"\t")
            mode, kind, blob = meta.decode().split()
            name = path.decode("utf-8", "surrogateescape")
            if kind == "blob":
                tree[name] = (mode, blob)
            elif kind == "commit":
                tree.links[name] = blob
    return tree


def _git(*args: str | Path, cwd: Path) -> bool:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "kennet",
        "GIT_AUTHOR_EMAIL": "kennet@localhost",
        "GIT_COMMITTER_NAME": "kennet",
        "GIT_COMMITTER_EMAIL": "kennet@localhost",
    }
    try:
        return (
            subprocess.run(
                ["git", *map(str, args)], cwd=cwd, env=env, capture_output=True, timeout=600, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _urls(into: Path) -> dict[str, str]:
    try:
        got = subprocess.run(
            ["git", "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|url)$"],
            cwd=into,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    named: dict[str, dict[str, str]] = {}
    for line in got.splitlines():
        key, _, value = line.partition(" ")
        name, _, field = key[len("submodule.") :].rpartition(".")
        named.setdefault(name, {})[field] = value
    return {v["path"]: v["url"] for v in named.values() if "path" in v and "url" in v}


def _linked(repo: Path, links: dict[str, str], into: Path) -> None:
    urls = _urls(into) if links else {}
    for path, sha in links.items():
        local, dst = repo / path, into / path
        source = str(local) if (local / ".git").exists() else urls.get(path, "")
        if not source or source.startswith(("./", "../")) or not dst.resolve().is_relative_to(into.resolve()):
            continue
        dst.mkdir(parents=True, exist_ok=True)
        if _git("init", "-q", cwd=dst) and _git("fetch", "-q", "--depth", "1", source, sha, cwd=dst):
            _git("checkout", "-q", "FETCH_HEAD", cwd=dst)


def _committed(into: Path) -> None:
    if not (into / ".git").exists() and _git("init", "-q", cwd=into):
        _git("add", "-A", cwd=into)
        _git("-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "tip", cwd=into)


def _write(repo: Path, tree: dict[str, tuple[str, str]], into: Path) -> None:
    from metaphi.codeval import _blobs

    fresh = not into.exists() or not any(into.iterdir())
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    by_blob: dict[str, list[tuple[str, str]]] = {}
    for path, (mode, blob) in tree.items():
        by_blob.setdefault(blob, []).append((path, mode))
    for blob, body in _blobs(repo, list(by_blob)):
        for path, mode in by_blob[blob]:
            dst = into / path
            if not dst.resolve().is_relative_to(root):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            if mode == "120000":
                os.symlink(body.decode("utf-8", "surrogateescape"), dst)
            else:
                dst.write_bytes(body)
                if mode == "100755":
                    dst.chmod(0o755)
    if fresh:
        _linked(repo, getattr(tree, "links", {}), into)
        _committed(into)


def _junit(d: Path) -> dict[str, str] | None:
    return extract.junit_files(sorted(d.rglob("*.xml"))) or None


def outcomes(cases: dict[str, str] | None) -> dict[str, str]:
    """Each test case's outcome, by its name as the report gives it: kept on this machine. What is sent names a case
    only by an HMAC under the repository's own key (`codeval_measure`)."""
    return dict(cases or {})


def _run(
    command: str, cwd: Path, env: dict[str, str], seconds: float, log: IO[bytes], peaks: list[int] | None = None
) -> tuple[int | None, float]:
    """Run `command` in its own process group; with `peaks`, append the most its whole tree held at once."""
    started = time.monotonic()
    p = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env=allowed(env),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    sampler = TreePeak(p.pid) if peaks is not None and os.name == "posix" else None
    try:
        code: int | None = p.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        code = None
    if sampler is not None and peaks is not None:
        peaks.append(sampler.close())
    if os.name == "posix":
        try:
            os.killpg(p.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, check=False)
    p.wait()
    if code is not None and not -255 <= code <= 255:
        code = 255
    return code, time.monotonic() - started


class _Logs:
    def __init__(self, root: Path):
        self.root, self.last = root, root

    def open(self, name: str) -> IO[bytes]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.last = self.root / name
        return self.last.open("ab")


def _copied(src: str, dst: str) -> None:
    try:
        shutil.copyfile(src, dst)
    except OSError:
        pass


COVER = "kennet.cover"
MEASURED_RUNS = ("build", "tests")
COVER_LINE = re.compile(r"^(.+\.go):(\d+\.\d+,\d+\.\d+) (\d+) (\d+)$")


def _installed(src: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(src):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _modules(src: Path) -> list[tuple[str, str]]:
    out = []
    for mod in src.rglob("go.mod"):
        try:
            text = mod.read_text("utf-8", "replace")
        except OSError:
            continue
        name = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
        if name:
            rel = mod.parent.relative_to(src).as_posix()
            out.append((name.group(1).strip('"'), "" if rel == "." else rel + "/"))
    return sorted(out, key=lambda m: -len(m[0]))


def go_coverage(profile: Path, src: Path, paths: dict[int, str]) -> list[dict[str, Any]] | None:
    try:
        lines = profile.read_text("utf-8", "replace").splitlines()
    except OSError:
        return None
    ids = {p: i for i, p in paths.items()}
    modules = _modules(src)
    blocks: dict[tuple[str, str], tuple[int, int]] = {}
    for line in lines:
        m = COVER_LINE.match(line)
        if not m:
            continue
        name, at, n, hits = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        rel = next((d + name[len(mod) + 1 :] for mod, d in modules if name.startswith(mod + "/")), None)
        if rel is None:
            continue
        seen = blocks.get((rel, at), (n, 0))
        blocks[rel, at] = (n, max(seen[1], hits))
    per: dict[int, list[int]] = {}
    for (rel, _), (n, hits) in blocks.items():
        if rel in ids:
            row = per.setdefault(ids[rel], [0, 0])
            row[0] += n
            row[1] += n if hits else 0
    return [{"file": f, "unit": "statement", "total": t, "covered": c} for f, (t, c) in sorted(per.items())]


def _tail(path: Path, lines: int = 15) -> list[str]:
    try:
        said = path.read_bytes()[-20000:].decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    return [f"    | {line}" for line in said[-lines:]]


def _ran(code: int | None, took: float, log: Path | None = None) -> dict[str, Any]:
    """One build or test run. `log` is where its output is, on this machine: only its sha256 is sent."""
    return {**({"log": str(log)} if log is not None else {}), "exit": code, "seconds": round(took)}


def _classed(row: dict[str, Any], why: dict[str, Any]) -> dict[str, Any]:
    """A run with why it ended: the class, step and kind of compile error from the shipped rules, and the schema's
    error word for that class. `rule` names the rule that decided; it stays on this machine."""
    return {
        **row,
        "error": extract.error(why["class"]),
        **{k: why[k] for k in ("class", "step", "compile_kind", "rule")},
    }


def _tested(
    code: int | None,
    took: float,
    cases: dict[str, str] | None,
    log: Path,
    found: extract.Found | None = None,
    limit_s: float | None = None,
) -> dict[str, Any]:
    found = found or extract.Found()
    cases = {**found.cases, **(cases or {})} or None
    said = set((cases or {}).values())
    found.classes += [c for o, c in (("error", "test_error"), ("failed", "test_assertion")) if o in said]
    ran = [v for v in (cases or {}).values() if v != "skipped"]
    if ran:
        report, counts = "junit", (len(ran), ran.count("passed"))
    elif found.counts:
        report, counts = "summary", found.counts
    else:
        counts = reading.counted(log)
        report = "summary" if counts else None
    why = extract.classify("test", code, log, found, counts, took, limit_s)
    return _classed(
        {
            **_ran(code, took, log),
            "report": report,
            "reported": counts[0] if counts else None,
            "passed": counts[1] if counts else None,
            "cases": outcomes(cases),
        },
        why,
    )


def _flaky(rows: list[dict[str, Any]]) -> None:
    extract.flaky(rows)
    for r in rows:
        if "class" in r:
            r["error"] = extract.error(r["class"])


def _found(log: Path, junit: Path, src: Path, since: float) -> extract.Found:
    return extract.read(extract._text(log), junit, src, since)


def _side(
    repo: Path,
    tree: dict,
    into: Path,
    plan: Plan,
    env: dict[str, str],
    runs: int,
    logs: _Logs,
    label: str,
    say: Callable[[str], None],
    found: tuple[Recipe, str | None] | None = None,
    jobs: int = 1,
    edit: Callable[[Path], None] | None = None,
    measure: dict[int, str] | None = None,
    peaks: list[int] | None = None,
) -> dict[str, Any]:
    _write(repo, tree, into / "src")
    if edit:
        edit(into / "src")
    junit = into / "junit"
    junit.mkdir()
    recipe, engine = found or (None, None)
    shell = Shell(
        engine, recipe.image if recipe else None, into / "src", junit, jobs=jobs, cache=plan.cache, memory=plan.memory
    )
    try:
        return _ran_side(shell, plan, {**env, "KENNET_JOBS": str(jobs)}, runs, logs, label, say, recipe, measure, peaks)
    finally:
        shell.close()


def _ran_side(
    shell: Shell,
    plan: Plan,
    env: dict[str, str],
    runs: int,
    logs: _Logs,
    label: str,
    say: Callable[[str], None],
    recipe: Recipe | None,
    measure: dict[int, str] | None = None,
    peaks: list[int] | None = None,
) -> dict[str, Any]:
    junit = shell.junit
    peaks = peaks if peaks is not None and not shell.name else None  # in a container, its cgroup counts instead
    if not recipe:
        build = None
    elif recipe.image:
        build = (
            f"({recipe.setup or 'true'}) || echo 'setup failed at this commit; building on the set-up image' >&2; "
            f"({extract.structured(recipe.build or 'true')})"
        )
    else:
        build = f"({recipe.setup or 'true'}) && ({extract.structured(recipe.build or 'true')})"
    test = extract.structured(recipe.test) if recipe else plan.test
    tests = env.get("KENNET_TESTS", "")
    side: dict[str, Any] = {"tests": []}
    seconds = plan.run_minutes * 60

    def shown(step: str, code: int | None, path: Path) -> None:
        if code != 0:
            head = f"    {label} {step}: {'stopped (too long)' if code is None else f'exit {code}'} ({path})"
            say("\n".join([head, *_tail(path)]))

    before = _installed(shell.src) if measure is not None else 0
    if build:
        since = time.time() - 1
        with logs.open(f"{label}.build.log") as log, plan.steps.step("build", at=label):
            path = Path(log.name)
            code, took = _run(
                shell.line(build, tests),
                shell.src,
                shell.env(tests) if recipe else {**env, "KENNET_JUNIT": str(junit), **shell.extra},
                seconds,
                log,
                peaks,
            )
        found = _found(path, junit, shell.src, since)
        side["build"] = _classed(
            _ran(code, took, path), extract.classify("build", code, path, found, None, took, seconds)
        )
        shown("build", code, path)
        if code is None:
            shell.stop()
        if code != 0:
            return side
    go = measure is not None and any(shell.src.rglob("go.mod"))
    if measure is not None:
        side["installed_bytes"] = max(_installed(shell.src) - before, 0)
        if go:
            at = "/kennet/junit" if shell.name else str(junit)
            given = (("" if shell.name else env.get("GOFLAGS", "")) + f" -coverprofile={at}/{COVER}").strip()
            shell.extra = {"GOFLAGS": given}
    for n in range(runs):
        shell.empty()
        since = time.time() - 1
        with logs.open(f"{label}.test-{n + 1}.log") as log, plan.steps.step("test", at=label):
            path = Path(log.name)
            code, took = _run(
                shell.line(test, tests),
                shell.src,
                shell.env(tests) if recipe else {**env, "KENNET_JUNIT": str(junit), **shell.extra},
                seconds,
                log,
                peaks,
            )
        try:
            shutil.copytree(
                junit,
                logs.root / f"{label}.test-{n + 1}.junit",
                dirs_exist_ok=True,
                ignore_dangling_symlinks=True,
                copy_function=_copied,
            )
        except (shutil.Error, OSError):
            pass
        cases = {**(_junit(junit) or {}), **extract.junit_files(extract.reports(shell.src, since))}
        side["tests"].append(_tested(code, took, cases, path, _found(path, junit, shell.src, since), seconds))
        shown(f"test {n + 1}", code, path)
        if measure is not None and n == 0:
            shell.extra = {}
            if go:
                side["coverage"] = go_coverage(junit / COVER, shell.src, measure)
        if code is None:
            shell.stop()
            break
    _flaky(side["tests"])
    if measure is not None and side["tests"] and side["tests"][0]["exit"] is not None:
        if shell.offline():
            shell.empty()
            since = time.time() - 1
            with logs.open(f"{label}.offline.log") as log, plan.steps.step("offline", at=label):
                path = Path(log.name)
                code, took = _run(shell.line(test, tests), shell.src, shell.env(tests), seconds, log)
            found = _found(path, junit, shell.src, since)
            side["offline"] = _tested(code, took, _junit(junit), path, found, seconds)
            if code is None:
                shell.stop()
        side["memory_peak_bytes"] = shell.memory_peak()
    return side


def _apply(path: Path, ext: str, op: str, nth: int) -> None:
    from metaphi.codebase_sites import apply_edit

    got = apply_edit(path.read_bytes().decode("latin-1"), ext, op, nth)
    if got is None:
        raise LookupError(f"{op} {nth}")
    path.write_bytes(got.encode("latin-1"))


def _edited(
    repo: Path,
    tree: dict,
    plan: Plan,
    runs: int,
    logs: _Logs,
    label: str,
    path: str,
    op: str,
    nth: int,
    found: tuple[Recipe, str | None] | None,
    jobs: int,
) -> dict[str, Any]:
    from metaphi.codeval import _ext

    with tempfile.TemporaryDirectory(prefix="kennet-test-", ignore_cleanup_errors=True) as scratch:
        post = _side(
            repo,
            tree,
            Path(scratch),
            plan,
            {**allowed(dict(os.environ)), "KENNET_TESTS": ""},
            runs,
            logs,
            label,
            lambda _s: None,
            found,
            jobs,
            lambda src: _apply(src / path, _ext(path), op, nth),
        )
    return {"post": post}


def _changed(repo: Path, parent: str, sha: str) -> tuple[list[str], str]:
    from metaphi.codeval import SOURCE, _ext, _kind, _out

    changed = _out(repo, "diff-tree", "-r", "-z", "--no-renames", "--name-only", parent, sha).split(b"\0")
    named = [b.decode("utf-8", "surrogateescape") for b in changed if b]
    tests = sorted(p for p in named if _kind(p) == "test")
    after = _tree(repo, sha)
    return tests, "\n".join(p for p in tests if p in after and _ext(p) in SOURCE)


def _commit(
    repo: Path,
    plan: Plan,
    runs: int,
    logs: _Logs,
    label: str,
    parent: str,
    sha: str,
    say: Callable[[str], None],
    found: tuple[Recipe, str | None] | None = None,
    jobs: int = 1,
) -> dict[str, Any]:
    tests, in_code = _changed(repo, parent, sha)
    before, after = _tree(repo, parent), _tree(repo, sha)
    pre_tree = Tree(
        {p: v for p, v in before.items() if p not in tests} | {p: after[p] for p in tests if p in after},
        links=after.links,
    )
    env = {**allowed(dict(os.environ)), "KENNET_TESTS": in_code, "KENNET_JOBS": str(jobs)}
    recipe, engine = found or (None, None)
    with tempfile.TemporaryDirectory(prefix="kennet-test-", ignore_cleanup_errors=True) as scratch:
        src, junit = Path(scratch) / "src", Path(scratch) / "junit"
        _write(repo, pre_tree, src)
        junit.mkdir()
        shell = Shell(
            engine, recipe.image if recipe else None, src, junit, jobs=jobs, cache=plan.cache, memory=plan.memory
        )
        try:
            pre = _ran_side(shell, plan, env, 1, logs, f"{label}.pre", say, recipe)
            if recipe is None:
                # The after side runs at the very path the before side ran at, so a command cannot tell them apart
                # by $PWD: the before side's copy is moved away first.
                shell.close()
                src.rename(Path(scratch) / "pre-src")
                junit.rename(Path(scratch) / "pre-junit")
                _write(repo, after, src)
                junit.mkdir()
                shell = Shell(None, None, src, junit, jobs=jobs)
            else:
                for gone in set(pre_tree) - set(after):
                    (src / gone).unlink(missing_ok=True)
                _write(repo, {p: v for p, v in after.items() if pre_tree.get(p) != v}, src)
            post = _ran_side(shell, plan, env, runs, logs, f"{label}.post", say, recipe)
        finally:
            shell.close()
    return {"pre": pre, "post": post}


AT_ONCE_ENV = "KENNET_CODEVAL_AT_ONCE"  # the most units at once, under what the machine holds
MEMORY_SHARE = 0.8  # of the memory free when units start, what they may take together


def free_memory() -> int | None:
    """Bytes the machine can give without swapping: Linux's MemAvailable; macOS's free, inactive and purgeable
    pages."""
    try:
        if os.path.exists("/proc/meminfo"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        if sys.platform == "darwin":
            said = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10, check=False).stdout
            page = int(re.search(r"page size of (\d+) bytes", said).group(1))  # type: ignore[union-attr]
            pages = dict(re.findall(r"^Pages (free|inactive|speculative|purgeable):\s+(\d+)", said, re.MULTILINE))
            return page * sum(int(v) for v in pages.values()) or None
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        pass
    return None


def _tree_rss(pgid: int) -> int:
    """Bytes resident in every process of a process group: a build's whole tree, compilers and test JVMs together."""
    try:
        said = subprocess.run(
            ["ps", "-A", "-o", "pgid=,rss="], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    total = 0
    for line in said.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == str(pgid) and parts[1].isdigit():
            total += int(parts[1]) * 1024
    return total


class TreePeak:
    """The most a process group held at once, sampled every `every` seconds while it runs."""

    def __init__(self, pgid: int, every: float = 1.0) -> None:
        self.pgid, self.every, self.peak = pgid, every, 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            self.peak = max(self.peak, _tree_rss(self.pgid))
            self.stop.wait(self.every)

    def close(self) -> int:
        self.stop.set()
        self.thread.join(5)
        return self.peak


def workers(n: int, peak: int | None = None, budget: int | None = None) -> tuple[int, int]:
    """(units at once, jobs each): half the cores' worth of units, no more than the memory budget holds at the tip's
    peak each (the container engine's budget, or 80% of the host's free memory), nor than KENNET_CODEVAL_AT_ONCE; the
    cores shared out among them."""
    cores = os.cpu_count() or 2
    at_once = max(1, min(n, cores // 2))
    budget = budget or ((free * MEMORY_SHARE) if (free := free_memory()) else None)
    if peak and budget:
        at_once = min(at_once, max(1, int(budget // peak)))
    try:
        at_once = min(at_once, max(1, int(os.environ.get(AT_ONCE_ENV) or at_once)))
    except ValueError:
        pass
    return at_once, max(1, cores // at_once)


class Gate:
    """Units run together through it; a unit rerun alone waits for the others to end and holds new ones back."""

    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.active: set[object] = set()
        self.alone, self.waiting = False, 0

    @contextlib.contextmanager
    def shared(self, key: object) -> Iterator[None]:
        with self.cv:
            self.cv.wait_for(lambda: not self.alone and not self.waiting)
            self.active.add(key)
        try:
            yield
        finally:
            with self.cv:
                self.active.discard(key)
                self.cv.notify_all()

    @contextlib.contextmanager
    def solo(self, key: object) -> Iterator[None]:
        with self.cv:
            self.waiting += 1
            self.cv.wait_for(lambda: not self.active and not self.alone)
            self.waiting -= 1
            self.alone = True
        try:
            yield
        finally:
            with self.cv:
                self.alone = False
                self.cv.notify_all()


def _runs(side: dict | None) -> list[dict]:
    if not side:
        return []
    return [r for r in (side.get("build"), *side.get("tests", []), side.get("offline")) if r]


def _suspect(row: dict, tip: dict | None) -> bool:
    """Whether a unit run beside others must run again alone before it counts: a build failed, its post side failed,
    a run was killed (exit 137, out of memory) or stopped for time, or its post side reported fewer tests than its
    own base did: a past commit's pre side (its parent, with the commit's tests), an edit's unedited tip. A before
    side's tests failing is expected (the commit's new tests, over its parent) and is no sign."""
    runs = [r for k in ("pre", "post") for r in _runs(row.get(k))]
    if any(r.get("exit") is None or r.get("exit") == KILLED for r in runs):
        return True
    if any((row.get(k) or {}).get("build", {}).get("exit", 0) != 0 for k in ("pre", "post")):
        return True
    if any(r.get("exit") != 0 for r in _runs(row.get("post"))):
        return True
    base = row.get("pre") if row.get("pre") is not None else tip
    most = max((t.get("reported") or 0 for t in (base or {}).get("tests", [])), default=0)
    return any((t.get("reported") or 0) < most for t in (row.get("post") or {}).get("tests", []))


KILLED = 137


def _spent(row: dict | None) -> float:
    """A unit's runs' own seconds, as the service sums them against the hours."""
    if not row:
        return 0.0
    return float(sum(r.get("seconds") or 0 for k in ("pre", "post") for r in _runs(row.get(k))))


def about(seconds: float) -> str:
    if seconds < 60:
        return "under a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min"
    hours, minutes = divmod(5 * round(minutes / 5), 60)
    return f"about {hours} h" + (f" {minutes} min" if minutes else "")


def between(low: float, high: float) -> str:
    a, b = about(low), about(high)
    return a if a == b else f"{a} to {b.removeprefix('about ')}"


class Eta:
    """Time left, in seconds of work rather than units done: a past commit or edit costs about what the tip did,
    one whose build fails costs next to nothing, and work under way counts for what it has run. Before any unit
    ends, the range covers the middle 80% of recorded runs (LOW..HIGH times units x tip / workers)."""

    LOW, HIGH, RISE = 0.5, 2.5, 1.25

    def __init__(self, units: int, at_once: int, unit_s: float | None, deadline: float, clock=time.monotonic):
        self.units, self.at_once, self.unit_s, self.deadline, self.clock = (
            units,
            max(1, at_once),
            unit_s,
            deadline,
            clock,
        )
        self.running: dict[object, float] = {}
        self.built: list[float] = []
        self.failed: list[float] = []
        self.shown: tuple[float, float] | None = None
        self.lock = threading.Lock()

    def _cap(self, seconds: float) -> float:
        return max(0.0, min(seconds, self.deadline - self.clock()))

    def first(self) -> str | None:
        if not self.unit_s or not self.units:
            return None
        base = self.units * self.unit_s / min(self.at_once, self.units)
        return between(self._cap(self.LOW * base), self._cap(self.HIGH * base))

    def begin(self, key: object) -> None:
        with self.lock:
            self.running[key] = self.clock()

    def _left(self, now: float) -> float:
        tip = self.unit_s or 0.0
        cost = (tip + sum(self.built)) / (1 + len(self.built))
        cheap = sorted(self.failed)[len(self.failed) // 2] if self.failed else 0.0
        p = (1 + len(self.built)) / (2 + len(self.built) + len(self.failed))
        each = p * cost + (1 - p) * cheap
        left = self.units - len(self.built) - len(self.failed)
        queued = left - len(self.running)
        under_way = [max(cost - (now - t), 0.1 * cost) for t in self.running.values()]
        return max((queued * each + sum(under_way)) / max(1, min(self.at_once, left)), max(under_way, default=0.0))

    def end(self, key: object, seconds: float, built: bool) -> str:
        with self.lock:
            self.running.pop(key, None)
            (self.built if built else self.failed).append(seconds)
            if len(self.built) + len(self.failed) >= self.units:
                return ""
            now = self.clock()
            left = self._left(now)
            if self.shown:
                counted = self.shown[1] - (now - self.shown[0])
                if left < counted or left > self.RISE * counted:
                    self.shown = (now, left)
            else:
                self.shown = (now, left)
            return about(self._cap(self.shown[1] - (now - self.shown[0]))) + " left"


def _summary(name: str, what: str, rows: list[dict | None], total: int, board: live.Board) -> str:
    ran = [r for r in rows if r is not None]
    built = sum(1 for r in ran if not r.get("broke") and ((r.get("post") or {}).get("build") or {}).get("exit", 0) == 0)
    took = live.clock_text(board.clock() - board.began)
    return f"\u2713 {name}: {len(ran)} of {total} {what} run, {built} built ({took})"


class Stuck(errors.MetaphiError):
    """A person must act before the run can go on: a tool is missing here. What finished is kept."""


def _stuck(where: str, side: dict[str, Any], logs: _Logs, label: str) -> Stuck | None:
    """Stuck when a build or test failed because a tool is not installed on this machine."""
    for step, got in (("build", side.get("build")), *(("test-1", t) for t in side.get("tests", [])[:1])):
        if got and got.get("error") == "toolchain_missing":
            log = logs.root / f"{label}.{step}.log"
            tool = reading.missing(log) or "a tool the build needs"
            return Stuck(
                f"{where}: {tool} is missing on this machine",
                code="toolchain_missing",
                next_step=f"install {tool}, then run the same command again; finished commits are kept",
            )
    return None


def run(
    repo: Path,
    name: str,
    tip: str,
    commits: list[dict],
    order: dict[str, int],
    targets: list[int],
    runs: int,
    plan: Plan,
    say: Callable[[str], None],
    found: tuple[Recipe, str | None] | None = None,
    paths: dict[int, str] | None = None,
    tipped: dict[str, Any] | None = None,
    stream: IO[str] | None = None,
    edits: list[dict] | None = None,
) -> dict[str, Any]:
    board = live.Board(name, stream, say)
    try:
        return _checks(
            repo, name, tip, commits, order, targets, runs, plan, board.say, found, paths, tipped, board, edits or []
        )
    finally:
        board.close()


def _checks(
    repo: Path,
    name: str,
    tip: str,
    commits: list[dict],
    order: dict[str, int],
    targets: list[int],
    runs: int,
    plan: Plan,
    say: Callable[[str], None],
    found: tuple[Recipe, str | None] | None,
    paths: dict[int, str] | None,
    tipped: dict[str, Any] | None,
    board: live.Board,
    edits: list[dict],
) -> dict[str, Any]:
    shas = {i: sha for sha, i in order.items()}
    by_id = {c["commit"]: c for c in commits}
    logs = _Logs((plan.logs or Path.cwd() / "kennet-test-logs") / name)
    given = plan.test is not None
    out: dict[str, Any] = {
        "stopped": "done",
        "commits": [],
        "mutants": [],
    }
    at_once, jobs = 1, os.cpu_count() or 1  # the tip runs alone, on every core
    if not given and found is None:
        out["commits"] = [{"commit": i, "broke": True} for i in targets]
        out["mutants"] = [{**b, "broke": True} for b in edits]
        return out
    tip_s: float | None = None
    ledger = plan.ledger
    kept_tip = ledger.get(name, "tip") if ledger else None
    peak: int | None = None
    tip_peaks: list[int] = []
    if kept_tip:
        out["tip"], tip_s, peak = kept_tip["tip"], kept_tip["seconds"], kept_tip.get("peak")
        say(f"{name}: the latest commit was run before; using that result")
    else:
        try:
            began = time.monotonic()
            with (
                tempfile.TemporaryDirectory(prefix="kennet-test-", ignore_cleanup_errors=True) as scratch,
                plan.steps.step("tip"),
            ):
                measured = _side(
                    repo,
                    _tree(repo, tip),
                    Path(scratch) / "tip",
                    plan,
                    {**allowed(dict(os.environ)), "KENNET_TESTS": ""},
                    1,
                    logs,
                    "000-tip",
                    say,
                    found,
                    jobs,
                    measure=paths or {},
                    peaks=tip_peaks,
                )
            tip_s = time.monotonic() - began
        except Exception as e:  # noqa: BLE001
            say(f"\u2717 {name} could not run ({type(e).__name__}: {e})")
        else:
            if stuck := _stuck(f"{name}, the latest commit", measured, logs, "000-tip"):
                raise stuck
            out["tip"] = (
                {**tipped, **{k: v for k, v in measured.items() if k not in MEASURED_RUNS}} if tipped else measured
            )
            peak = out["tip"].get("memory_peak_bytes") or max(tip_peaks, default=0) or None
            if ledger:
                ledger.put(name, "tip", "", {"tip": out["tip"], "seconds": tip_s, "peak": peak})
            said = out["tip"]
            built = "build" not in said or said["build"]["exit"] == 0
            t = said["tests"][0] if said["tests"] else None
            tested = (
                ""
                if not t
                else (
                    f" \u00b7 {t['passed']} of {t['reported']} tests pass"
                    if t.get("reported")
                    else " \u00b7 tests pass"
                    if t["exit"] == 0
                    else " \u00b7 tests fail"
                )
            )
            say(
                ("\u2713 " if built and t and t["exit"] == 0 else "\u2717 ")
                + name
                + (" builds" if built else " does not build")
                + tested
            )

    before = (
        sum(ledger.get(name, "commit", i) is not None for i in targets)
        + sum(ledger.get(name, "edit", [b.get("file"), b.get("op"), b.get("nth")]) is not None for b in edits)
        if ledger
        else 0
    )
    if before:
        say(f"{name}: {before} of {len(targets) + len(edits)} past commits and edits were run before; running the rest")
    box = found[1] if found else None
    budget = codebase_setup.memory_budget(box) if box else None
    at_once, jobs = (
        (1, os.cpu_count() or 1) if plan.alone else workers(len(targets) + len(edits) - before, peak, budget)
    )
    if box and budget:
        plan = dataclasses.replace(plan, memory=max(budget // at_once, 1 << 30))
    eta = Eta(len(targets) + len(edits) - before, at_once, tip_s, time.monotonic() + plan.hours * 3600 / at_once)
    if targets or edits:
        work = " and ".join(
            f"{k} {what}{'s' if k != 1 else ''}"
            for k, what in ((len(targets), "past commit"), (len(edits), "edit"))
            if k
        )
        took = eta.first()
        say(
            f"{name}: building and testing {work}"
            + (f", {took}" if took else "")
            + f"; it stops after {plan.hours:g} h of build and test time"
        )
    halt: list[Stuck] = []
    gate = Gate()
    tip_side = out.get("tip")
    spent = [0.0]
    budget = plan.hours * 3600
    lock = threading.Lock()

    def spend(row: dict | None) -> None:
        with lock:
            spent[0] += _spent(row)

    def over() -> bool:
        """The hours are the past commits' and edits' own run seconds, summed as the service sums them (the shared
        attempt of a unit rerun alone not counted): what is claimed as stopped for its hours always spent them."""
        with lock:
            return spent[0] >= budget

    def measured(key: object, label: str, attempt: Callable[[str], dict]) -> tuple[dict, float]:
        """A unit run beside the others, and run again alone when what it shows may be the others' doing (`_suspect`);
        both attempts are kept: the alone one as the row, the shared one beside it as `shared`."""
        began = time.monotonic()
        with gate.shared(key):
            row = attempt(label)
        took = time.monotonic() - began
        if at_once > 1 and _suspect(row, tip_side):
            board.say(f"    {name}: {label} ran again alone, to rule out the others' doing")
            shared = {k: row[k] for k in ("pre", "post") if row.get(k) is not None}
            began = time.monotonic()
            with gate.solo(key):
                row = attempt(f"{label}.alone")
            took = time.monotonic() - began
            return {**row, "shared": shared}, took
        return row, took

    def one(n: int, i: int) -> dict | None:
        if ledger and (kept := ledger.get(name, "commit", i)) is not None:
            spend(kept)
            return kept
        if over() or halt:
            return None
        label = f"{n:03d}-{shas[i][:12]}" if i in shas else f"{n:03d}"
        began = time.monotonic()
        eta.begin(n)
        board.begin(label)
        try:
            c = by_id[i]
            parent = shas[c["parents"][0]]

            def attempt(at: str) -> dict:
                return _commit(repo, plan, runs, logs, at, parent, shas[i], lambda _s: None, found, jobs)

            got, _took = measured(("commit", i), label, attempt)
            row = {"commit": i, **got}
        except Exception as e:  # noqa: BLE001
            say(f"    {label}: could not run ({type(e).__name__}: {e})")
            board.end(label, False, f"{name}: {label} could not run ({n}/{len(targets)})", eta.end(n, 0.0, False))
            return {"commit": i, "broke": True}
        if stuck := _stuck(f"{name}, commit {n} of {len(targets)}", row["post"], logs, f"{label}.post"):
            halt.append(stuck)
            board.end(label, False, f"{name}: {label} stopped: {stuck}", "")
            return None
        b = (row["post"].get("build") or {}).get("exit", 0)
        line = f"{name}: {label} build {'ok' if b == 0 else 'failed'} ({n}/{len(targets)})"
        board.end(label, b == 0, line, eta.end(n, time.monotonic() - began, b == 0))
        if ledger:
            ledger.put(name, "commit", i, row)
        spend(row)
        return row

    tip_tree, where = _tree(repo, tip), paths or {}

    def edited(n: int, b: dict) -> dict | None:
        key = [b.get("file"), b.get("op"), b.get("nth")]
        if ledger and (kept := ledger.get(name, "edit", key)) is not None:
            spend(kept)
            return kept
        if over() or halt:
            return None
        label = f"{n:03d}-tip"
        began = time.monotonic()
        eta.begin(n)
        board.begin(label)
        try:

            def attempt(at: str) -> dict:
                return _edited(repo, tip_tree, plan, runs, logs, at, where[b["file"]], b["op"], b["nth"], found, jobs)

            got, _took = measured(("edit", n), label, attempt)
            row = {**b, **got}
        except Exception as e:  # noqa: BLE001
            board.end(label, False, f"{name}: {label} could not run ({type(e).__name__})", eta.end(n, 0.0, False))
            return {**b, "broke": True}
        built = (row["post"].get("build") or {}).get("exit", 0)
        line = f"{name}: {label} build {'ok' if built == 0 else 'failed'} ({n - len(targets)}/{len(edits)})"
        board.end(label, built == 0, line, eta.end(n, time.monotonic() - began, built == 0))
        if ledger:
            ledger.put(name, "edit", key, row)
        spend(row)
        return row

    units = [(one, n, i) for n, i in enumerate(targets, 1)] + [
        (edited, n, b) for n, b in enumerate(edits, len(targets) + 1)
    ]
    rows: list[dict | None] = []
    done: list[dict | None] = []
    if units:
        board.stage(
            "past commits and edits" if targets and edits else "past commits" if targets else "edits", len(units)
        )
        with concurrent.futures.ThreadPoolExecutor(at_once) as pool, plan.steps.step("commits", at_once=at_once):
            got = list(pool.map(lambda u: u[0](u[1], u[2]), units))
        rows, done = got[: len(targets)], got[len(targets) :]
        board.close(
            "; ".join(
                _summary(name, what, part, len(part), board)
                for what, part in (("past commits", rows), ("edits", done))
                if part
            )
        )
    if halt:
        raise halt[0]
    if None in rows or None in done:
        out["stopped"] = "hours"
    out["commits"] = rows  # None: not run, the hours spent first
    out["mutants"] = done
    return out


__all__ = ["Eta", "Plan", "Stuck", "about", "between", "outcomes", "run"]
