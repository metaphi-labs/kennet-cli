"""How long each step of `kennet profile-codebase` took: every one written to a JSON-lines file on this machine as it
ends, and their sums, in whole seconds by step name, sent beside the counts. Names come from `NAMES` alone."""
from __future__ import annotations

import contextlib
import json
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

NAMES = frozenset(["profile", "send", "sample", "token", "setup", "setup_model", "setup_commands", "setup_proposals",
                   "tip", "commits", "build", "test", "post_checks"])


class Steps:
    def __init__(self, path: Path | None = None):
        self.path, self.total, self.lock = path, defaultdict(float), threading.Lock()

    @contextlib.contextmanager
    def step(self, name: str, **seen: object) -> Iterator[None]:
        assert name in NAMES, name
        began = time.monotonic()
        try:
            yield
        finally:
            took = time.monotonic() - began
            with self.lock:
                self.total[name] += took
                if self.path:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a") as f:
                        f.write(json.dumps({"step": name, "seconds": round(took, 3), "at": round(time.time(), 3),
                                            **seen}) + "\n")

    def add(self, other: Steps) -> None:
        with self.lock:
            for k, v in other.total.items():
                self.total[k] += v

    def seconds(self) -> dict[str, int]:
        with self.lock:
            return {k: round(v) for k, v in sorted(self.total.items())}

    def said(self) -> str:
        return ", ".join(f"{k} {v // 60}m{v % 60:02d}s" for k, v in self.seconds().items())


NONE = Steps()   # where nothing is kept: a step measured for no one
