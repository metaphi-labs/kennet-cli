"""A CodeVal run's checkpoints on disk, so a run that stops goes on where it stopped.

Each finished unit (the tip, a past commit, a whole repository) is one line of `checks.jsonl` in the run's
directory, written and synced before the next starts. Rerunning the same command reads them back and runs only the
rest. Nothing here is sent anywhere.

What a repository was committed to at `/picks` (its profile salt, and the exact profile entry) is kept per user, by
the repository's fingerprint, apart from any run: a run started again, from another folder or with another command,
files the same entry.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

JOURNAL = "checks.jsonl"
STATE_ENV = "KENNET_STATE_HOME"
SALT = "salt"
ENTRY = "entry.json"


def state_home() -> Path:
    """Where kennet keeps state that outlives a run: `$KENNET_STATE_HOME`, else the platform's per-user state
    folder: `%LOCALAPPDATA%\\kennet\\state` on Windows, `$XDG_STATE_HOME/kennet` or `~/.local/state/kennet`
    elsewhere."""
    if given := os.environ.get(STATE_ENV):
        return Path(given)
    if sys.platform == "win32" and (local := os.environ.get("LOCALAPPDATA")):
        return Path(local) / "kennet" / "state"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "kennet"


def committed_dir(fingerprint: str) -> Path:
    return state_home() / "codeval" / fingerprint


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


def salt(fingerprint: str, new: bytes) -> bytes:
    """The repository's profile salt: the one kept for it, else `new`, kept from now on."""
    path = committed_dir(fingerprint) / SALT
    try:
        kept = bytes.fromhex(path.read_text().strip())
        if len(kept) == len(new):
            return kept
    except (OSError, ValueError):
        pass
    _kept(path, new.hex().encode())
    return new


def committed(fingerprint: str) -> bytes | None:
    """The exact profile entry the repository was committed to at `/picks`, or None when none is kept here."""
    try:
        return (committed_dir(fingerprint) / ENTRY).read_bytes()
    except OSError:
        return None


def commit(fingerprint: str, entry: bytes) -> None:
    """Keep the entry committed to at `/picks`; one kept already stays."""
    if committed(fingerprint) is None:
        _kept(committed_dir(fingerprint) / ENTRY, entry)


class Run:
    """The finished units of one run. Worker threads call `get` and `put`."""

    def __init__(self, home: Path) -> None:
        self.home, self.lock = home, threading.Lock()
        self.units: dict[tuple[str, str, str], dict] = {}
        home.mkdir(parents=True, exist_ok=True)
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

    def forget(self) -> None:
        """Other checks were asked for: what the last run finished does not count. What a repository was committed to
        at `/picks` is kept elsewhere (`committed`) and never forgotten."""
        with self.lock:
            self.units.clear()
            (self.home / JOURNAL).unlink(missing_ok=True)

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


__all__ = ["JOURNAL", "Run", "awake", "commit", "committed", "salt", "state_home"]
