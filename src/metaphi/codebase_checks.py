from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from metaphi import codebase_reading as reading
from metaphi import live
from metaphi.codebase_setup import Recipe, Shell, allowed
from metaphi.steps import NONE, Steps

RUN_MINUTES = 30.0
HOURS = 12.0
SALT = secrets.token_bytes(32)
RANK = {"skipped": 0, "passed": 1, "failed": 2, "error": 3}


@dataclass(frozen=True)
class Plan:
    test: str | None = None
    build: str | None = None
    run_minutes: float = RUN_MINUTES
    hours: float = HOURS
    logs: Path | None = None
    cache: Path | None = None
    steps: Steps = NONE
    deep: bool = False
    ledger: Any = None


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
    files = sorted(d.rglob("*.xml"))
    if not files:
        return None
    got: dict[str, str] = {}
    for f in files:
        try:
            root = ET.parse(f).getroot()
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
            got[key] = max(got.get(key, outcome), outcome, key=RANK.__getitem__)
    return got


def outcomes(cases: dict[str, str] | None) -> dict[str, str]:
    return {hashlib.sha256(SALT + k.encode()).hexdigest()[:16]: v for k, v in (cases or {}).items()}


def _run(command: str, cwd: Path, env: dict[str, str], seconds: float, log: IO[bytes]) -> tuple[int | None, float]:
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
    try:
        code: int | None = p.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        code = None
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


def _ran(code: int | None, took: float) -> dict[str, Any]:
    return {"exit": code, "seconds": round(took)}


def _tested(code: int | None, took: float, cases: dict[str, str] | None, log: Path) -> dict[str, Any]:
    ran = [v for v in (cases or {}).values() if v != "skipped"]
    if ran:
        report, counts = "junit", (len(ran), ran.count("passed"))
    else:
        counts = reading.counted(log)
        report = "summary" if counts else None
    return {
        **_ran(code, took),
        "report": report,
        "reported": counts[0] if counts else None,
        "passed": counts[1] if counts else None,
        "outcomes": outcomes(cases),
    }


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
) -> dict[str, Any]:
    _write(repo, tree, into / "src")
    if edit:
        edit(into / "src")
    junit = into / "junit"
    junit.mkdir()
    recipe, engine = found or (None, None)
    shell = Shell(engine, recipe.image if recipe else None, into / "src", junit, jobs=jobs, cache=plan.cache)
    try:
        return _ran_side(shell, plan, {**env, "KENNET_JOBS": str(jobs)}, runs, logs, label, say, recipe, measure)
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
) -> dict[str, Any]:
    junit = shell.junit
    if not recipe:
        build = plan.build
    elif recipe.image:
        build = (
            f"({recipe.setup or 'true'}) || echo 'setup failed at this commit; building on the set-up image' >&2; "
            f"({recipe.build or 'true'})"
        )
    else:
        build = f"({recipe.setup or 'true'}) && ({recipe.build or 'true'})"
    test = recipe.test if recipe else plan.test
    tests = env.get("KENNET_TESTS", "")
    side: dict[str, Any] = {"tests": []}
    seconds = plan.run_minutes * 60

    def shown(step: str, code: int | None, path: Path) -> None:
        if code != 0:
            head = f"    {label} {step}: {'stopped (too long)' if code is None else f'exit {code}'} ({path})"
            say("\n".join([head, *_tail(path)]))

    before = _installed(shell.src) if measure is not None else 0
    if build:
        with logs.open(f"{label}.build.log") as log, plan.steps.step("build", at=label):
            path = Path(log.name)
            code, took = _run(
                shell.line(build, tests),
                shell.src,
                shell.env(tests) if recipe else {**env, "KENNET_JUNIT": str(junit), **shell.extra},
                seconds,
                log,
            )
        side["build"] = _ran(code, took)
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
        with logs.open(f"{label}.test-{n + 1}.log") as log, plan.steps.step("test", at=label):
            path = Path(log.name)
            code, took = _run(
                shell.line(test, tests),
                shell.src,
                shell.env(tests) if recipe else {**env, "KENNET_JUNIT": str(junit), **shell.extra},
                seconds,
                log,
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
        side["tests"].append(_tested(code, took, _junit(junit), path))
        shown(f"test {n + 1}", code, path)
        if measure is not None and n == 0:
            shell.extra = {}
            if go:
                side["coverage"] = go_coverage(junit / COVER, shell.src, measure)
        if code is None:
            shell.stop()
            break
    if measure is not None and side["tests"] and side["tests"][0]["exit"] is not None:
        if shell.offline():
            shell.empty()
            with logs.open(f"{label}.offline.log") as log, plan.steps.step("offline", at=label):
                path = Path(log.name)
                code, took = _run(shell.line(test, tests), shell.src, shell.env(tests), seconds, log)
            side["offline"] = _tested(code, took, _junit(junit), path)
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
            {**os.environ, "KENNET_TESTS": ""},
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
    env = {**os.environ, "KENNET_TESTS": in_code, "KENNET_JOBS": str(jobs)}
    recipe, engine = found or (None, None)
    with tempfile.TemporaryDirectory(prefix="kennet-test-", ignore_cleanup_errors=True) as scratch:
        src, junit = Path(scratch) / "src", Path(scratch) / "junit"
        _write(repo, pre_tree, src)
        junit.mkdir()
        shell = Shell(engine, recipe.image if recipe else None, src, junit, jobs=jobs, cache=plan.cache)
        try:
            pre = _ran_side(shell, plan, env, 1, logs, f"{label}.pre", say, recipe)
            if recipe is None:
                shell.close()
                src, junit = Path(scratch) / "post" / "src", Path(scratch) / "post" / "junit"
                _write(repo, after, src)
                junit.mkdir(parents=True)
                shell = Shell(None, None, src, junit, jobs=jobs)
            else:
                for gone in set(pre_tree) - set(after):
                    (src / gone).unlink(missing_ok=True)
                _write(repo, {p: v for p, v in after.items() if pre_tree.get(p) != v}, src)
            post = _ran_side(shell, plan, env, runs, logs, f"{label}.post", say, recipe)
        finally:
            shell.close()
    return {"pre": pre, "post": post}


def workers(n: int) -> tuple[int, int]:
    cores = os.cpu_count() or 2
    at_once = max(1, min(n, cores // 4))
    return at_once, max(1, cores // at_once)


Repair = Callable[[int, str, str], "tuple[Recipe, str | None] | None"]


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


def _neither_built(row: dict) -> bool:
    return not row.get("broke") and all((row[s].get("build") or {}).get("exit", 0) != 0 for s in ("pre", "post"))


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
    eras: list[int | None] | None = None,
    repair: Repair | None = None,
    edits: list[dict] | None = None,
    paths: dict[int, str] | None = None,
    tipped: dict[str, Any] | None = None,
    stream: IO[str] | None = None,
) -> dict[str, Any]:
    board = live.Board(name, stream, say)
    try:
        return _checks(
            repo,
            name,
            tip,
            commits,
            order,
            targets,
            runs,
            plan,
            board.say,
            found,
            eras,
            repair,
            edits,
            paths,
            tipped,
            board,
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
    eras: list[int | None] | None,
    repair: Repair | None,
    edits: list[dict] | None,
    paths: dict[int, str] | None,
    tipped: dict[str, Any] | None,
    board: live.Board,
) -> dict[str, Any]:
    shas = {i: sha for sha, i in order.items()}
    by_id = {c["commit"]: c for c in commits}
    deadline = time.monotonic() + plan.hours * 3600
    logs = _Logs((plan.logs or Path.cwd() / "kennet-test-logs") / name)
    given = plan.test is not None
    out: dict[str, Any] = {
        "from": "tip",
        "runs": runs,
        "build_given": plan.build is not None,
        "test_given": given,
        "stopped": "done",
        "commits": [],
        "edits": [],
    }
    at_once, jobs = (1, os.cpu_count() or 1) if given else workers(len(targets))
    if not given and found is None:
        out["commits"] = [{"commit": i, "broke": True} for i in targets]
        out["edits"] = [{**b, "broke": True} for b in edits or []]
        return out
    tip_s: float | None = None
    ledger = plan.ledger
    kept_tip = ledger.get(name, "tip") if ledger else None
    if kept_tip:
        out["tip"], tip_s = kept_tip["tip"], kept_tip["seconds"]
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
                    {**os.environ, "KENNET_TESTS": ""},
                    1,
                    logs,
                    "000-tip",
                    say,
                    found,
                    jobs,
                    measure=paths or {},
                )
            tip_s = time.monotonic() - began
            out["tip"] = (
                {**tipped, **{k: v for k, v in measured.items() if k not in MEASURED_RUNS}} if tipped else measured
            )
            if ledger:
                ledger.put(name, "tip", "", {"tip": out["tip"], "seconds": tip_s})
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
        except Exception as e:  # noqa: BLE001
            say(f"\u2717 {name} could not run ({type(e).__name__}: {e})")

    edits = edits or []
    before = (
        sum(ledger.get(name, "commit", i) is not None for i in targets)
        + sum(ledger.get(name, "edit", [b.get("file"), b.get("op"), b.get("nth")]) is not None for b in edits)
        if ledger
        else 0
    )
    if before:
        say(f"{name}: {before} of {len(targets) + len(edits)} past commits and edits were run before; running the rest")
    eta = Eta(len(targets) + len(edits) - before, at_once, tip_s, deadline)
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
            + f"; it stops after {plan.hours:g} h"
        )

    def label_of(n: int, i: int) -> str:
        return f"{n:03d}-{shas[i][:12]}" if i in shas else f"{n:03d}"

    def one(n: int, i: int, with_: tuple[Recipe, str | None] | None = found, again: str = "") -> dict | None:
        if not again and ledger and (kept := ledger.get(name, "commit", i)) is not None:
            return kept
        if time.monotonic() > deadline:
            return None
        label = label_of(n, i) + again
        began = time.monotonic()
        if not again:
            eta.begin(n)
            board.begin(label)
        try:
            c = by_id[i]
            row = {
                "commit": i,
                **_commit(repo, plan, runs, logs, label, shas[c["parents"][0]], shas[i], lambda _s: None, with_, jobs),
            }
            b = (row["post"].get("build") or {}).get("exit", 0)
            line = f"{name}: {label} build {'ok' if b == 0 else 'failed'} ({n}/{len(targets)})"
            if again:
                say(line)
            else:
                board.end(label, b == 0, line, eta.end(n, time.monotonic() - began, b == 0))
                if ledger:
                    ledger.put(name, "commit", i, row)
            return row
        except Exception as e:  # noqa: BLE001
            say(f"    {label}: could not run ({type(e).__name__}: {e})")
            if not again:
                board.end(
                    label,
                    False,
                    f"{name}: {label} could not run ({n}/{len(targets)})",
                    eta.end(n, time.monotonic() - began, False),
                )
            return {"commit": i, "broke": True}

    rows: list[dict | None] = []
    if targets:
        board.stage("past commits", len(targets))
        with concurrent.futures.ThreadPoolExecutor(at_once) as pool, plan.steps.step("commits", at_once=at_once):
            rows = list(pool.map(one, range(1, len(targets) + 1), targets))
        board.close(_summary(name, "past commits", rows, len(targets), board))
    if None in rows:
        out["stopped"] = "hours"
        rows = rows[: rows.index(None)]
    if repair and eras and not given:
        by_era: dict[int, list[int]] = {}
        for k, row in enumerate(rows):
            if eras[k] is not None:
                by_era.setdefault(eras[k], []).append(k)
        for era, ks in sorted(by_era.items()):
            ran = [k for k in ks if _neither_built(rows[k])]
            if not ran or time.monotonic() > deadline:
                continue
            k = ran[0]
            tail = "\n".join(
                line[6:] for line in _tail(logs.root / f"{label_of(k + 1, targets[k])}.post.build.log", 60)
            )
            say(f"{name}: {len(ran)} builds failed; setting up again")
            try:
                with plan.steps.step("setup", era=era):
                    fixed = repair(era, shas[targets[k]], tail)
            except Exception as e:  # noqa: BLE001
                say(f"    setup failed ({type(e).__name__}: {e})")
                continue
            if not fixed:
                continue
            with concurrent.futures.ThreadPoolExecutor(at_once) as pool, plan.steps.step("commits", era=era):
                again = list(
                    pool.map(
                        one,
                        [k + 1 for k in ran],
                        [targets[k] for k in ran],
                        [fixed] * len(ran),
                        [f".era{era}"] * len(ran),
                    )
                )
            for k, row in zip(ran, again, strict=True):
                if row is not None and not _neither_built(row):
                    rows[k] = row
                    if ledger and not row.get("broke"):
                        ledger.put(name, "commit", targets[k], row)
    out["commits"] = rows
    if edits:
        tip_tree, paths = _tree(repo, tip), paths or {}

        def edited(n: int, b: dict) -> dict | None:
            key = [b.get("file"), b.get("op"), b.get("nth")]
            if ledger and (kept := ledger.get(name, "edit", key)) is not None:
                return kept
            if time.monotonic() > deadline:
                return None
            label = f"{n:03d}-tip"
            began = time.monotonic()
            eta.begin(n)
            board.begin(label)
            try:
                row = {
                    **b,
                    **_edited(
                        repo, tip_tree, plan, runs, logs, label, paths[b["file"]], b["op"], b["nth"], found, jobs
                    ),
                }
                built = (row["post"].get("build") or {}).get("exit", 0)
                line = f"{name}: {label} build {'ok' if built == 0 else 'failed'} ({n - len(targets)}/{len(edits)})"
                board.end(label, built == 0, line, eta.end(n, time.monotonic() - began, built == 0))
                if ledger:
                    ledger.put(name, "edit", key, row)
                return row
            except Exception as e:  # noqa: BLE001
                say(f"    {label}: could not run ({type(e).__name__}: {e})")
                board.end(
                    label,
                    False,
                    f"{name}: {label} could not run ({n - len(targets)}/{len(edits)})",
                    eta.end(n, time.monotonic() - began, False),
                )
                return {**b, "broke": True}

        board.stage("edits", len(edits))
        with concurrent.futures.ThreadPoolExecutor(at_once) as pool, plan.steps.step("edits", at_once=at_once):
            done = list(pool.map(edited, range(len(targets) + 1, len(targets) + len(edits) + 1), edits))
        board.close(_summary(name, "edits", done, len(edits), board))
        if None in done:
            out["stopped"] = "hours"
            done = done[: done.index(None)]
        out["edits"] = done
    return out


__all__ = ["Eta", "Plan", "about", "between", "outcomes", "run"]
