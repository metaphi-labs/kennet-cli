"""A CodeVal run's checkpoints on disk, so a run that stops goes on where it stopped.

Each finished unit (the tip, a past commit, a whole repository) is one line of `checks.jsonl` in the run's
directory, written and synced before the next starts. Rerunning the same command reads them back and runs only the
rest. Nothing here is sent anywhere.
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
        """Other checks were asked for: what the last run finished does not count."""
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


__all__ = ["JOURNAL", "Run", "awake"]
