"""A CodeVal run's state on this machine, so a run that stops goes on where it stopped. Nothing here is sent.

Each repository has one folder, `<state>/codeval/<fingerprint>`, named as the server names its run: the sha256 of
its root commits. It holds `key`, the repository's HMAC key (random, mode 0600): what keys test ids, commands, subjects
and blobs; one per repository on this machine. Each clone of it has its own run record, `clones/<clone>/` (the clone's
absolute path, or its URL, hashed), so two clones never share a run:

    run.json       the server's run id and the tip it was measured at
    profile.json   what the filed profile bound the run to: its tip, commit order and ids (never re-read from git)
    checks.jsonl   each finished unit (the tip, a commit, a mutant), written and synced before the next starts
    logs/          every build and test log, whole; only each log's sha256 is sent
    replaced/      the finished units of a measure a restart replaced, set aside and never read again
    sent/          every body sent, exactly as sent (before gzip): profile-N.json, results-N.json, never overwritten
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

JOURNAL = "checks.jsonl"
STATE_ENV = "KENNET_STATE_HOME"
KEY = "key"
RUN = "run.json"
PROFILE = "profile.json"


def state_home() -> Path:
    """Where kennet keeps state that outlives a run: `$KENNET_STATE_HOME`, else the platform's per-user state
    folder: `%LOCALAPPDATA%\\kennet\\state` on Windows, `$XDG_STATE_HOME/kennet` or `~/.local/state/kennet`
    elsewhere."""
    if given := os.environ.get(STATE_ENV):
        return Path(given)
    if sys.platform == "win32" and (local := os.environ.get("LOCALAPPDATA")):
        return Path(local) / "kennet" / "state"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "kennet"


def fingerprint(roots: list[str]) -> str:
    """A repository's name for its run, as the server keys it: the sha256 of its sorted root commits."""
    return hashlib.sha256("\n".join(sorted(roots)).encode()).hexdigest()


def repo_home(roots: list[str]) -> Path:
    """The repository's folder, one per root fingerprint: it holds the key: one key per repository per machine."""
    return state_home() / "codeval" / fingerprint(roots)[:32]


def home(roots: list[str], where: str) -> Path:
    """One clone's run record: the repository's folder and the clone (its absolute path, or the URL it was cloned
    from), so two clones of one repository never share a run."""
    return repo_home(roots) / "clones" / hashlib.sha256(where.encode()).hexdigest()[:16]


def _kept(path: Path, body: bytes) -> None:
    """Write `body` to `path` whole or not at all, synced, readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def repo_key(at: Path) -> bytes:
    """The repository's HMAC key: the one kept for it, else a new random one, kept from now on. It never leaves this
    machine, so what it keys can be matched within one repository and read by no one."""
    path = at / KEY
    try:
        kept = bytes.fromhex(path.read_text().strip())
        if len(kept) == 32:
            return kept
    except (OSError, ValueError):
        pass
    new = secrets.token_bytes(32)
    _kept(path, new.hex().encode())
    return new


def run_state(at: Path) -> dict[str, Any]:
    try:
        got = json.loads((at / RUN).read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def keep_run(at: Path, **state: Any) -> None:
    _kept(at / RUN, json.dumps({**run_state(at), **state}, indent=1).encode())


def keep_profile(at: Path, kept: dict[str, Any]) -> None:
    _kept(at / PROFILE, json.dumps(kept, separators=(",", ":")).encode())


def kept_profile(at: Path) -> dict[str, Any] | None:
    try:
        got = json.loads((at / PROFILE).read_text())
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) and isinstance(got.get("tip"), str) else None


def replaced(at: Path) -> None:
    """A restart: the finished units were measured against the old profile's ids, so they are set aside, kept as they
    were (`replaced/checks-N.jsonl`), and never read again."""
    journal = at / JOURNAL
    if not journal.exists():
        return
    folder = at / "replaced"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    n = 1 + sum(1 for _ in folder.glob("checks-*.jsonl"))
    while (folder / f"checks-{n}.jsonl").exists():
        n += 1
    os.replace(journal, folder / f"checks-{n}.jsonl")


def sent(at: Path, kind: str, body: bytes) -> Path:
    """Keep a body exactly as it is sent, under the next free name of its kind (`profile-1.json`, `profile-2.json`,
    `results-1.json`, ...), never over an earlier one; answers where."""
    folder = at / "sent"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    n = 1 + sum(1 for _ in folder.glob(f"{kind}-*.json"))
    while True:
        path = folder / f"{kind}-{n}.json"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            n += 1
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        return path


class Run:
    """The finished units of one run. Worker threads call `get` and `put`."""

    def __init__(self, home: Path) -> None:
        self.home, self.lock = home, threading.Lock()
        self.units: dict[tuple[str, str, str], dict] = {}
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            lines = (home / JOURNAL).read_text().splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                row = json.loads(line)
                self.units[(row["repo"], row["kind"], row["key"])] = row["row"]
            except (ValueError, KeyError, TypeError):
                continue  # a line cut short by a kill: that unit runs again

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
        subprocess.Popen(
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


__all__ = [
    "JOURNAL",
    "Run",
    "awake",
    "fingerprint",
    "home",
    "keep_profile",
    "keep_run",
    "kept_profile",
    "replaced",
    "repo_key",
    "run_state",
    "sent",
    "state_home",
]
