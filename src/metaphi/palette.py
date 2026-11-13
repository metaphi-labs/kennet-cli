"""The CLI's colours: a few mid-value tones that read on a dark terminal and a light one, never a bright primary.
Truecolor where the terminal says it has it (COLORTERM), the nearest of the 256 otherwise; none under NO_COLOR or
when the stream is not a terminal.

    accent   periwinkle: what the reader acts on or keeps (a repository's name, a reference, the question)
    ok       sage: a check that holds, a send that landed
    no       dusty rose: a check that does not hold
    dim      what is there to look up, not to read (paths, steps, notes)
"""

from __future__ import annotations

import os
from typing import IO

TONES = {  # (truecolor rgb, xterm-256 index)
    "accent": ((126, 140, 224), 104),
    "ok": ((116, 168, 128), 108),
    "no": ((206, 124, 132), 174),
}


def on(stream: IO[str]) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")


def paint(text: str, tone: str | None, stream: IO[str], bold: bool = False) -> str:
    """`text` in `tone` (or "dim"), bold if asked, when `stream` shows colour; as it is otherwise."""
    if not text or not on(stream) or (tone is None and not bold):
        return text
    codes = ["1"] if bold else []
    if tone == "dim":
        codes.append("2")
    elif tone:
        rgb, index = TONES[tone]
        deep = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
        codes.append("38;2;{};{};{}".format(*rgb) if deep else f"38;5;{index}")
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"
