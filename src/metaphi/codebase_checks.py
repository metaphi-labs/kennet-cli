from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from metaphi import codebase_reading as reading
from metaphi.codebase_setup import Recipe, Shell
from metaphi.steps import NONE, Steps

RUN_MINUTES = 30.0
HOURS = 12.0


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


class Tree(dict):
    links: dict[str, str]

    def __init__(self, *a: Any, links: dict[str, str] | None = None, **k: Any) -> None:
        super().__init__(*a, **k)
        self.links = links or {}


def _tree(repo: Path, sha: str) -> Tree:
    from metaphi.codebase_profile import _out
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
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "kennet", "GIT_AUTHOR_EMAIL": "kennet@localhost",
           "GIT_COMMITTER_NAME": "kennet", "GIT_COMMITTER_EMAIL": "kennet@localhost"}
    try:
        return subprocess.run(["git", *map(str, args)], cwd=cwd, env=env, capture_output=True, timeout=600, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _urls(into: Path) -> dict[str, str]:
    try:
        got = subprocess.run(["git", "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|url)$"],
                             cwd=into, capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    named: dict[str, dict[str, str]] = {}
    for line in got.splitlines():
        key, _, value = line.partition(" ")
        name, _, field = key[len("submodule."):].rpartition(".")
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
    from metaphi.codebase_profile import _blobs
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


def _junit(d: Path) -> dict[str, bool] | None:
    files = sorted(d.rglob("*.xml"))
    if not files:
        return None
    got: dict[str, bool] = {}
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except (ET.ParseError, OSError):
            continue
        for case in root.iter("testcase"):
            if case.find("skipped") is not None:
                continue
            key = f"{case.get('classname', '')}\0{case.get('name', '')}"
            got[key] = got.get(key, True) and case.find("failure") is None and case.find("error") is None
    return got


def _run(command: str, cwd: Path, env: dict[str, str], seconds: float, log: IO[bytes]) -> tuple[int | None, float]:
    started = time.monotonic()
    p = subprocess.Popen(command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
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


def _tail(path: Path, lines: int = 15) -> list[str]:
    try:
        said = path.read_bytes()[-20000:].decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    return [f"    | {line}" for line in said[-lines:]]


def _ran(code: int | None, took: float) -> dict[str, Any]:
    return {"exit": code, "seconds": round(took)}


def _built(code: int | None, took: float, log: Path) -> dict[str, Any]:
    return {**_ran(code, took), **({} if code == 0 else {"why": reading.why(code, log)})}


def _tested(code: int | None, took: float, cases: dict[str, bool] | None, log: Path) -> dict[str, Any]:
    if cases:
        report, counts = "junit", (len(cases), sum(cases.values()))
    else:
        counts = reading.counted(log)
        report = "summary" if counts else None
    whole = code == 0 and (counts is None or counts[0] == counts[1])
    return {**_ran(code, took), "report": report, "reported": counts[0] if counts else None,
            "passed": counts[1] if counts else None,
            **({} if whole else {"why": reading.why(code, log, counts, test=True)})}


def _side(repo: Path, tree: dict, into: Path, plan: Plan, env: dict[str, str], runs: int, logs: _Logs, label: str,
          say: Callable[[str], None], found: tuple[Recipe, str | None] | None = None, jobs: int = 1,
          edit: Callable[[Path], None] | None = None) -> tuple[dict[str, Any], list[dict[str, bool] | None]]:
    _write(repo, tree, into / "src")
    if edit:
        edit(into / "src")
    junit = into / "junit"
    junit.mkdir()
    recipe, engine = found or (None, None)
    shell = Shell(engine, recipe.image if recipe else None, into / "src", junit, jobs=jobs, cache=plan.cache)
    try:
        return _ran_side(shell, plan, {**env, "KENNET_JOBS": str(jobs)}, runs, logs, label, say, recipe)
    finally:
        shell.close()


def _ran_side(shell: Shell, plan: Plan, env: dict[str, str], runs: int, logs: _Logs, label: str,
              say: Callable[[str], None], recipe: Recipe | None) -> tuple[dict[str, Any], list[dict[str, bool] | None]]:
    junit = shell.junit
    build = (recipe.build if recipe.image else f"({recipe.setup or 'true'}) && ({recipe.build or 'true'})"
             ) if recipe else plan.build
    test = recipe.test if recipe else plan.test
    tests = env.get("KENNET_TESTS", "")
    side: dict[str, Any] = {"tests": []}
    cases: list[dict[str, bool] | None] = []
    seconds = plan.run_minutes * 60

    def shown(step: str, code: int | None, path: Path) -> None:
        if code != 0:
            head = f"    {label} {step}: {'stopped (too long)' if code is None else f'exit {code}'} ({path})"
            say("\n".join([head, *_tail(path)]))
    if build:
        with logs.open(f"{label}.build.log") as log, plan.steps.step("build", at=label):
            path = Path(log.name)
            code, took = _run(shell.line(build, tests), shell.src, shell.env(tests) if recipe else
                              {**env, "KENNET_JUNIT": str(junit)}, seconds, log)
        side["build"] = _built(code, took, path)
        shown("build", code, path)
        if code is None:
            shell.stop()
        if code != 0:
            return side, cases
    for n in range(runs):
        shell.empty()
        with logs.open(f"{label}.test-{n + 1}.log") as log, plan.steps.step("test", at=label):
            path = Path(log.name)
            code, took = _run(shell.line(test, tests), shell.src, shell.env(tests) if recipe else
                              {**env, "KENNET_JUNIT": str(junit)}, seconds, log)
        cases.append(_junit(junit))
        try:
            shutil.copytree(junit, logs.root / f"{label}.test-{n + 1}.junit", dirs_exist_ok=True,
                            ignore_dangling_symlinks=True, copy_function=_copied)
        except (shutil.Error, OSError):
            pass
        side["tests"].append(_tested(code, took, cases[-1], path))
        shown(f"test {n + 1}", code, path)
        if code is None:
            shell.stop()
            break
    return side, cases


def _compared(pre: list[dict[str, bool] | None], post: list[dict[str, bool] | None], runs: int) -> dict[str, int | None]:
    if len(post) < runs or not post or any(r is None for r in post):
        return {"fail_to_pass": None, "pass_to_pass": None, "flaky": None}
    before = (pre or [None])[0] or {}
    keys = set().union(*post)
    steady = {k for k in keys if all(k in r for r in post) and len({r[k] for r in post}) == 1}
    passing = {k for k in steady if post[0][k]}
    f2p = {k for k in passing if not before.get(k, False)}
    return {"fail_to_pass": len(f2p), "pass_to_pass": len(passing - f2p), "flaky": len(keys - steady)}


def _caught(tip: dict[str, bool] | None, post: list[dict[str, bool] | None], runs: int) -> dict[str, int | None]:
    if not tip or len(post) < runs or not post or any(r is None for r in post):
        return {"caught": None, "kept": None, "flaky": None, "gone": None}
    passing = {k for k, v in tip.items() if v}
    seen = set().union(*post)
    steady = {k for k in passing if all(k in r for r in post) and len({r[k] for r in post}) == 1}
    caught = {k for k in steady if not post[0][k]}
    return {"caught": len(caught), "kept": len(steady - caught), "flaky": len(passing & seen - steady),
            "gone": len(passing - seen)}


def _swap(path: Path, ext: str, op: str, nth: int) -> None:
    from metaphi.codebase_sites import swapped
    got = swapped(path.read_bytes().decode("latin-1"), ext, op, nth)
    if got is None:
        raise LookupError(f"{op} {nth}")
    path.write_bytes(got.encode("latin-1"))


def _planted(repo: Path, tree: dict, plan: Plan, runs: int, logs: _Logs, label: str, path: str, op: str, nth: int,
             tip: dict[str, bool] | None, found: tuple[Recipe, str | None] | None, jobs: int) -> dict[str, Any]:
    from metaphi.codebase_profile import _ext
    with tempfile.TemporaryDirectory(prefix="kennet-test-", ignore_cleanup_errors=True) as scratch:
        post, cases = _side(repo, tree, Path(scratch), plan, {**os.environ, "KENNET_TESTS": ""}, runs, logs, label,
                            lambda _s: None, found, jobs, lambda src: _swap(src / path, _ext(path), op, nth))
    return {"post": post, **_caught(tip, cases, runs)}


def _changed(repo: Path, parent: str, sha: str) -> tuple[list[str], str]:
    from metaphi.codebase_profile import SOURCE, _ext, _kind, _out
    changed = _out(repo, "diff-tree", "-r", "-z", "--no-renames", "--name-only", parent, sha).split(b"\0")
    named = [b.decode("utf-8", "surrogateescape") for b in changed if b]
    tests = sorted(p for p in named if _kind(p) == "test")
    after = _tree(repo, sha)
    return tests, "\n".join(p for p in tests if p in after and _ext(p) in SOURCE)


def _commit(repo: Path, plan: Plan, runs: int, logs: _Logs, label: str, parent: str, sha: str,
            say: Callable[[str], None], found: tuple[Recipe, str | None] | None = None,
            jobs: int = 1) -> dict[str, Any]:
    tests, in_code = _changed(repo, parent, sha)
    before, after = _tree(repo, parent), _tree(repo, sha)
    pre_tree = Tree({p: v for p, v in before.items() if p not in tests} | {p: after[p] for p in tests if p in after},
                    links=after.links)
    env = {**os.environ, "KENNET_TESTS": in_code, "KENNET_JOBS": str(jobs)}
    recipe, engine = found or (None, None)
    with tempfile.TemporaryDirectory(prefix="kennet-test-", ignore_cleanup_errors=True) as scratch:
        src, junit = Path(scratch) / "src", Path(scratch) / "junit"
        _write(repo, pre_tree, src)
        junit.mkdir()
        shell = Shell(engine, recipe.image if recipe else None, src, junit, jobs=jobs, cache=plan.cache)
        try:
            pre, pre_cases = _ran_side(shell, plan, env, 1, logs, f"{label}.pre", say, recipe)
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
            post, post_cases = _ran_side(shell, plan, env, runs, logs, f"{label}.post", say, recipe)
        finally:
            shell.close()
    return {"pre": pre, "post": post, **_compared(pre_cases, post_cases, runs)}


def workers(n: int) -> tuple[int, int]:
    cores = os.cpu_count() or 2
    at_once = max(1, min(n, cores // 4))
    return at_once, max(1, cores // at_once)


Repair = Callable[[int, str, str], "tuple[Recipe, str | None] | None"]


def _neither_built(row: dict) -> bool:
    return not row.get("broke") and all((row[s].get("build") or {}).get("exit", 0) != 0 for s in ("pre", "post"))


def run(repo: Path, name: str, tip: str, commits: list[dict], order: dict[str, int], picks: list[int], runs: int,
        plan: Plan, say: Callable[[str], None], found: tuple[Recipe, str | None] | None = None,
        eras: list[int | None] | None = None, repair: Repair | None = None, breaks: list[dict] | None = None,
        paths: dict[int, str] | None = None, tipped: dict[str, Any] | None = None) -> dict[str, Any]:
    """`tipped`: the tip's build and test as setup already ran them; the tip is then not run again."""
    shas = {i: sha for sha, i in order.items()}
    by_id = {c["commit"]: c for c in commits}
    deadline = time.monotonic() + plan.hours * 3600
    logs = _Logs((plan.logs or Path.cwd() / "kennet-test-logs") / name)
    given = plan.test is not None
    out: dict[str, Any] = {"from": "tip", "runs": runs, "build_given": plan.build is not None, "test_given": given,
                           "stopped": "done", "commits": [], "breaks": []}
    at_once, jobs = (1, os.cpu_count() or 1) if given else workers(len(picks))
    if not given and found is None:
        out["commits"] = [{"commit": i, "broke": True} for i in picks]
        out["breaks"] = [{**b, "broke": True} for b in breaks or []]
        return out
    tip_cases: list[dict[str, bool] | None] = []
    try:
        if tipped:
            out["tip"], tip_cases = tipped["side"], tipped["cases"]
        else:
            with tempfile.TemporaryDirectory(prefix="kennet-test-", ignore_cleanup_errors=True) as scratch, \
                    plan.steps.step("tip"):
                out["tip"], tip_cases = _side(repo, _tree(repo, tip), Path(scratch) / "tip", plan,
                                              {**os.environ, "KENNET_TESTS": ""}, 1, logs, "000-tip", say, found, jobs)
        said = out["tip"]
        built = "build" not in said or said["build"]["exit"] == 0
        t = said["tests"][0] if said["tests"] else None
        tested = "" if not t else (f" \u00b7 {t['passed']} of {t['reported']} tests pass" if t.get("reported")
                                   else " \u00b7 tests pass" if t["exit"] == 0 else " \u00b7 tests fail")
        say(("\u2713 " if built and t and t["exit"] == 0 else "\u2717 ") + name
            + (" builds" if built else " does not build") + tested)
    except Exception as e:   # noqa: BLE001
        say(f"\u2717 {name} could not run ({type(e).__name__}: {e})")

    def label_of(n: int, i: int) -> str:
        return f"{n:03d}-{shas[i][:12]}" if i in shas else f"{n:03d}"

    def one(n: int, i: int, with_: tuple[Recipe, str | None] | None = found, again: str = "") -> dict | None:
        if time.monotonic() > deadline:
            return None
        label = label_of(n, i) + again
        try:
            c = by_id[i]
            row = {"commit": i, **_commit(repo, plan, runs, logs, label, shas[c["parents"][0]], shas[i], say,
                                          with_, jobs)}
            b = (row["post"].get("build") or {}).get("exit", 0)
            say(f"{name}: {label} build {'ok' if b == 0 else 'failed'} ({n}/{len(picks)})")
            return row
        except Exception as e:   # noqa: BLE001
            say(f"    {label}: could not run ({type(e).__name__}: {e})")
            return {"commit": i, "broke": True}
    rows: list[dict | None] = []
    if picks:
        with concurrent.futures.ThreadPoolExecutor(at_once) as pool, plan.steps.step("commits", at_once=at_once):
            rows = list(pool.map(one, range(1, len(picks) + 1), picks))
    if None in rows:
        out["stopped"] = "hours"
        rows = rows[:rows.index(None)]
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
            tail = "\n".join(line[6:] for line in _tail(logs.root / f"{label_of(k + 1, picks[k])}.post.build.log", 60))
            say(f"{name}: {len(ran)} builds failed; setting up again")
            try:
                with plan.steps.step("setup", era=era):
                    fixed = repair(era, shas[picks[k]], tail)
            except Exception as e:   # noqa: BLE001
                say(f"    setup failed ({type(e).__name__}: {e})")
                continue
            if not fixed:
                continue
            with concurrent.futures.ThreadPoolExecutor(at_once) as pool, plan.steps.step("commits", era=era):
                again = list(pool.map(one, [k + 1 for k in ran], [picks[k] for k in ran], [fixed] * len(ran),
                                      [f".era{era}"] * len(ran)))
            for k, row in zip(ran, again, strict=True):
                if row is not None and not _neither_built(row):
                    rows[k] = row
    out["commits"] = rows
    if breaks:
        tip_tree, paths = _tree(repo, tip), paths or {}

        def planted(n: int, b: dict) -> dict | None:
            if time.monotonic() > deadline:
                return None
            label = f"{n:03d}-tip"
            try:
                row = {**b, **_planted(repo, tip_tree, plan, runs, logs, label, paths[b["file"]], b["op"], b["nth"],
                                       (tip_cases or [None])[0], found, jobs)}
                built = (row["post"].get("build") or {}).get("exit", 0)
                say(f"{name}: {label} build {'ok' if built == 0 else 'failed'} ({n}/{len(breaks)})")
                return row
            except Exception as e:   # noqa: BLE001
                say(f"    {label}: could not run ({type(e).__name__}: {e})")
                return {**b, "broke": True}
        with concurrent.futures.ThreadPoolExecutor(at_once) as pool, plan.steps.step("breaks", at_once=at_once):
            done = list(pool.map(planted, range(len(picks) + 1, len(picks) + len(breaks) + 1), breaks))
        if None in done:
            out["stopped"] = "hours"
            done = done[:done.index(None)]
        out["breaks"] = done
    return out


__all__ = ["Plan", "run"]
