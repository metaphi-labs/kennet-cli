from __future__ import annotations

import os
from typing import IO

TONES = {
    "accent": ((126, 140, 224), 104),
    "ok": ((116, 168, 128), 108),
    "no": ((206, 124, 132), 174),
}


def on(stream: IO[str]) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")


def paint(text: str, tone: str | None, stream: IO[str], bold: bool = False) -> str:
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
