from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from typing import IO

from metaphi import palette

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR = 24


def clock_text(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


class Board:
    """One repository's commits as they run. On a terminal, a block redrawn in place: the stage, a bar, the units
    under way and the time left, with lines said printed above it. Anywhere else, each line as it comes."""

    def __init__(
        self,
        name: str,
        stream: IO[str] | None,
        say: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
        tick: float = 0.125,
    ):
        self.live = _terminal(stream)
        self.name, self.stream, self._say, self.clock = name, stream if self.live else None, say, clock
        self.title, self.total, self.built, self.failed = "", 0, 0, 0
        self.running: dict[str, float] = {}
        self.left, self.began, self.drawn, self.frame = "", clock(), 0, 0
        self.lock = threading.Lock()
        self.tick, self.stop = tick, threading.Event()
        self.thread: threading.Thread | None = None

    def _paint(self, text: str, tone: str | None, bold: bool = False) -> str:
        return palette.paint(text, tone, self.stream, bold) if self.stream else text

    def _lines(self) -> list[str]:
        if not self.title:
            return []
        done = self.built + self.failed
        fill = round(BAR * done / self.total) if self.total else 0
        bar = self._paint("━" * fill, "accent") + self._paint("─" * (BAR - fill), "dim")
        counts = f"{self.built} built" + (f", {self.failed} failed" if self.failed else "")
        head = (
            f"{self._paint(FRAMES[self.frame % len(FRAMES)], 'accent')} {self.name} · {self.title}  "
            f"{bar}  {done}/{self.total} · {counts}"
        )
        now = self.clock()
        units = [self._paint(f"    {k}  {clock_text(now - t)}", "dim") for k, t in sorted(self.running.items())]
        foot = self._paint(f"    {clock_text(now - self.began)} in" + (f" · {self.left}" if self.left else ""), "dim")
        return [head, *units, foot]

    def _draw(self) -> None:
        if not self.stream:
            return
        width = max(20, shutil.get_terminal_size().columns - 1)
        out = f"\x1b[{self.drawn}F\x1b[J" if self.drawn else ""
        lines = self._lines()
        out += "".join(_cut(line, width) + "\n" for line in lines)
        self.drawn = len(lines)
        self.stream.write(out)
        self.stream.flush()

    def _loop(self, tick: float) -> None:
        while not self.stop.wait(tick):
            with self.lock:
                self.frame += 1
                self._draw()

    def _clear(self) -> None:
        if self.live and self.drawn and self.stream:
            self.stream.write(f"\x1b[{self.drawn}F\x1b[J")
            self.stream.flush()
            self.drawn = 0

    def say(self, line: str) -> None:
        with self.lock:
            self._clear()
            self._say(line)
            if self.live:
                self._draw()

    def stage(self, title: str, total: int) -> None:
        with self.lock:
            self.title, self.total, self.built, self.failed, self.began = title, total, 0, 0, self.clock()
            self.running.clear()
            self.left = ""
        if self.live and not self.thread:
            self.stop.clear()
            self.thread = threading.Thread(target=self._loop, args=(self.tick,), daemon=True)
            self.thread.start()

    def begin(self, label: str) -> None:
        with self.lock:
            self.running[label] = self.clock()

    def end(self, label: str, built: bool, line: str, left: str = "") -> None:
        with self.lock:
            self.running.pop(label, None)
            self.built, self.failed = self.built + built, self.failed + (not built)
            self.left = left
            if not self.live:
                self._say(line + (f" · {left}" if left else ""))

    def close(self, line: str | None = None) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join()
            self.thread = None
        with self.lock:
            self._clear()
            self.title = ""
            if line:
                self._say(line)


def _terminal(stream: IO[str] | None) -> bool:
    try:
        return bool(stream and stream.isatty())
    except (ValueError, OSError):
        return False


def _cut(line: str, width: int) -> str:
    seen, out, i = 0, [], 0
    while i < len(line):
        if line[i] == "\x1b":
            j = line.find("m", i)
            out.append(line[i : j + 1])
            i = j + 1
            continue
        if seen >= width:
            i += 1
            continue
        out.append(line[i])
        seen += 1
        i += 1
    return "".join(out)


__all__ = ["Board", "clock_text"]
