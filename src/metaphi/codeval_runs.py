"""A CodeVal run as a record on disk: what it has finished, where it stands, and whether a person must act.

Each run keeps `state.json` (where it stands) and `checks.jsonl` (each finished commit, edit and repository, one
line each) in its directory. A per-user index under the config directory finds a run from anywhere. Progress goes
to the service as counts and codes only, best effort: a report that fails never stops the run.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from metaphi import api, config

PHASES = ("profile", "plan", "setup", "checks", "logs", "done")
NEEDS = ("test_command_missing", "container_required")
STATE, JOURNAL, LOG = "state.json", "checks.jsonl", "run.log"
UNITS = ("commit", "edit")
REPORT = httpx.Timeout(5.0, connect=3.0)
WAIT_POLL_S = 2.0
RUN_NAME = re.compile(r"run_[0-9a-f]{16}")


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read(path: Path) -> dict[str, Any]:
    try:
        got = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write_lines(path: Path, lines: list[str]) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp.write_text("".join(lines))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def index() -> Path:
    return config.home() / "codeval"


def register(run: str, home: Path) -> None:
    d = index()
    if not d.is_dir():
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(d / f"{run}.json", {"run": run, "results": str(home)})


def located(run: str | None = None) -> tuple[str, Path] | None:
    """The run named, or the one updated last; None when the index holds no such run."""
    d = index()
    if run is not None:
        if not RUN_NAME.fullmatch(run):
            return None
        got = _read(d / f"{run}.json").get("results")
        return (run, Path(got)) if isinstance(got, str) else None
    found: list[tuple[float, str, Path]] = []
    for f in d.glob("run_*.json") if d.is_dir() else ():
        got = _read(f).get("results")
        if not isinstance(got, str) or not RUN_NAME.fullmatch(f.stem):
            continue
        home = Path(got)
        try:
            at = (home / STATE).stat().st_mtime
        except OSError:
            at = 0.0
        found.append((at, f.stem, home))
    if not found:
        return None
    _at, name, home = max(found)
    return name, home


def alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class Run:
    """One run's record. Worker threads call `get` and `put`; every write is whole, so a reader never sees half."""

    def __init__(self, home: Path, run: str, resume: str | None = None) -> None:
        self.home, self.run, self.lock = home, run, threading.RLock()
        self.ref: str | None = None
        self.totals: dict[str, int] = {}
        self.units: dict[tuple[str, str, str], dict] = {}
        home.mkdir(parents=True, exist_ok=True)
        self._load()
        self.data = {
            **_read(home / STATE),
            "run": run,
            "state": "running",
            "phase": "profile",
            "pid": os.getpid(),
            "needs": None,
            "error": None,
            "next_step": None,
        }
        self.data.setdefault("started", round(time.time()))
        if resume:
            self.data["resume"] = resume
        self._save()

    def _load(self) -> None:
        try:
            lines = (self.home / JOURNAL).read_text().splitlines()
        except OSError:
            return
        for line in lines:
            try:
                row = json.loads(line)
                self.units[(row["repo"], row["kind"], row["key"])] = row["row"]
            except (ValueError, KeyError, TypeError):
                continue

    def _save(self) -> None:
        self.data["updated"] = round(time.time())
        _write(self.home / STATE, self.data)

    def write(self, **fields: Any) -> None:
        with self.lock:
            self.data.update(fields)
            self._save()

    def forget(self, keep: tuple[str, ...] = ()) -> None:
        """A run asked for other checks: what the last one finished does not count, but for the kinds kept."""
        with self.lock:
            self.units = {k: row for k, row in self.units.items() if k[1] in keep}
            lines = [
                json.dumps({"repo": r, "kind": kind, "key": key, "row": row}, separators=(",", ":")) + "\n"
                for (r, kind, key), row in self.units.items()
            ]
            if lines:
                _write_lines(self.home / JOURNAL, lines)
            else:
                (self.home / JOURNAL).unlink(missing_ok=True)

    def counts(self) -> tuple[int, int]:
        with self.lock:
            done = 0
            for repo, total in self.totals.items():
                if (repo, "repo", "") in self.units:
                    done += total
                else:
                    done += min(total, sum(1 for r, k, _ in self.units if r == repo and k in UNITS))
            return done, sum(self.totals.values())

    def plan(self, totals: dict[str, int]) -> None:
        self.totals = dict(totals)
        done, total = self.counts()
        self.write(done=done, total=total)

    def get(self, repo: str, kind: str, key: object = "") -> dict | None:
        with self.lock:
            return self.units.get((repo, kind, json.dumps(key) if key != "" else ""))

    def put(self, repo: str, kind: str, key: object, row: dict) -> None:
        k = json.dumps(key) if key != "" else ""
        line = json.dumps({"repo": repo, "kind": kind, "key": k, "row": row}, separators=(",", ":")) + "\n"
        with self.lock:
            with (self.home / JOURNAL).open("a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self.units[(repo, kind, k)] = row
            done, total = self.counts()
            self.write(done=done, total=total)
        self.report()

    def phase(self, name: str) -> None:
        assert name in PHASES, name
        if self.data.get("phase") == name:
            return
        if name == "checks":
            self.write(phase=name, checks_at=time.time(), checks_from=self.counts()[0])
        else:
            self.write(phase=name)
        self.report()

    def finished(self) -> None:
        done, total = self.counts()
        self.write(state="done", phase="done", done=max(done, total), total=total, pid=None)
        self.report()

    def needs(self, block: dict[str, Any], next_step: str | None) -> None:
        self.write(state="needs_person", needs=block, next_step=next_step, pid=None)
        self.report(block["code"])

    def failed(self, error: dict[str, Any], stopped: bool = False) -> None:
        self.write(state="stopped" if stopped else "failed", error=error, pid=None)

    def report(self, needs: str | None = None) -> None:
        """Tell the service where the run stands: counts and codes, nothing else. Never raises."""
        if not self.ref:
            return
        done, total = self.counts()
        if self.data.get("state") == "done":
            done = max(done, total)
        body = {"phase": self.data.get("phase") or "profile", "done": min(done, total), "total": total, "needs": needs}
        with contextlib.suppress(Exception), api.client(api.key(None, False)) as h:
            h.post(f"/codeval/{self.ref}/progress", json=body, timeout=REPORT)


def started(home: Path, run: str, pid: int, resume: str, **more: Any) -> None:
    data = {
        **_read(home / STATE),
        **more,
        "run": run,
        "state": "running",
        "pid": pid,
        "needs": None,
        "error": None,
        "next_step": None,
        "resume": resume,
        "updated": round(time.time()),
    }
    data.setdefault("phase", "profile")
    data.setdefault("started", round(time.time()))
    _write(home / STATE, data)


def view(run: str, home: Path) -> dict[str, Any]:
    s = _read(home / STATE)
    state = s.get("state") or "stopped"
    if state == "running" and not alive(s.get("pid")):
        state = "stopped"
    error = s.get("error") if state in ("failed", "stopped") else None
    if state == "running":
        step = f"kennet codeval status {run} --json --wait 600"
    elif state == "needs_person":
        step = s.get("next_step")
    elif state in ("failed", "stopped"):
        said = (error or {}).get("next_step")
        step = said if isinstance(said, str) and said.startswith("kennet ") else s.get("resume")
    else:
        step = None
    needs = s.get("needs") if state == "needs_person" else None
    if _elsewhere(home):
        go = f"cd {shlex.quote(str(home.parent.parent))} && "

        def there(command: str | None) -> str | None:
            runs = command and command.startswith("kennet codeval ") and not command.startswith("kennet codeval status")
            return go + command if runs else command

        step = there(step)
        if needs:
            needs = {**needs, "options": [{**o, "run": there(o.get("run"))} for o in needs.get("options") or []]}
    return {
        "run": run,
        "state": state,
        "phase": s.get("phase"),
        "done": s.get("done") or 0,
        "total": s.get("total") or 0,
        "reference": s.get("reference"),
        "results": str(home),
        "pid": s.get("pid") if state == "running" else None,
        "started": s.get("started"),
        "updated": s.get("updated"),
        "needs": needs,
        "error": error,
        "next_step": step,
        "left_s": left(s, state),
        "local": bool(s.get("local")),
        "asked": s.get("asked"),
    }


def left(s: dict[str, Any], state: str, now: float | None = None) -> int | None:
    """Seconds of checks left, from the pace of the units this process has finished; None until one has."""
    at, start, done, total = s.get("checks_at"), s.get("checks_from"), s.get("done") or 0, s.get("total") or 0
    if state != "running" or s.get("phase") != "checks" or not isinstance(at, (int, float)):
        return None
    if not isinstance(start, int) or done <= start or done >= total:
        return None
    took = (now or time.time()) - at
    return round(took / (done - start) * (total - done))


def _elsewhere(home: Path) -> bool:
    """A run goes on from the directory it started in, where ./kennet-codeval/<run> is."""
    try:
        return Path.cwd().resolve() != home.parent.parent.resolve()
    except OSError:
        return True


def _moved(v: dict[str, Any]) -> tuple:
    return v["state"], v["phase"], v["done"], v["total"], v["needs"] is not None


def wait(
    run: str,
    home: Path,
    seconds: float,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Where the run stands; with seconds, wait up to that long for it to move, end, or need a person."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    first = now = view(run, home)
    until = clock() + max(0.0, seconds)
    while now["state"] == "running" and _moved(now) == _moved(first) and clock() < until:
        sleep(min(WAIT_POLL_S, max(0.0, until - clock())))
        now = view(run, home)
    return now


def _start(command: list[str], **kw: Any) -> subprocess.Popen:
    return subprocess.Popen(command, **kw)


def _self() -> list[str]:
    return [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "metaphi"]


def awake(pid: int) -> str | None:
    """Keep the machine from sleeping while pid runs: caffeinate on macOS, systemd-inhibit on Linux when there."""
    if sys.platform == "darwin" and (tool := shutil.which("caffeinate")):
        command = [tool, "-i", "-w", str(pid)]
    elif sys.platform.startswith("linux") and (tool := shutil.which("systemd-inhibit")):
        command = [
            tool,
            "--what=sleep:idle",
            "--who=kennet",
            "--why=kennet codeval is building and testing",
            "--mode=block",
            "sh",
            "-c",
            f"while kill -0 {pid} 2>/dev/null; do sleep 30; done",
        ]
    else:
        return None
    try:
        _start(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return None
    return Path(tool).name


def detach(home: Path, argv: list[str]) -> tuple[int, str | None]:
    """Start `kennet <argv>` in a session of its own, writing to the run's log; the pid, and what keeps it awake."""
    with (home / LOG).open("ab") as log:
        log.write(f"\n--- {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} kennet {' '.join(argv)}\n".encode())
        log.flush()
        child = _start(
            [*_self(), *argv],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=home.parent.parent,
        )
    return child.pid, awake(child.pid)


__all__ = ["NEEDS", "PHASES", "Run", "alive", "awake", "detach", "located", "register", "started", "view", "wait"]
