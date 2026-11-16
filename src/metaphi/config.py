from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from metaphi import __version__

DEFAULT_ACCOUNTS = "https://app.metaphi.ai"
DEFAULT_API = "https://api.metaphi.ai/v1/kennet"
ALLOWED_DOMAIN = "metaphi.ai"
KEY_PREFIX = "mai_sk_"
USER_AGENT = f"kennet/{__version__}"
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


class Error(Exception):
    pass


def _custom_hosts() -> bool:
    return os.environ.get("METAPHI_ALLOW_CUSTOM_HOST") == "1"


def check_url(url: str, what: str) -> str:
    if not isinstance(url, str):
        raise Error(f"{what} is not a URL: {url!r}")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        raise Error(f"{what} is not a URL: {url!r}") from None
    if not host or parts.username or parts.password:
        raise Error(f"{what} is not a usable URL: {url!r}")
    if _custom_hosts():
        if parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK):
            return url
        raise Error(f"{what} must be https (or http on localhost): {url!r}")
    if parts.scheme != "https":
        raise Error(f"{what} must be https: {url!r}")
    if host != ALLOWED_DOMAIN and not host.endswith("." + ALLOWED_DOMAIN):
        raise Error(f"{what} is not on {ALLOWED_DOMAIN}: {url!r} (set METAPHI_ALLOW_CUSTOM_HOST=1 to allow it)")
    return url


_warned: set[str] = set()


def _endpoint(var: str, default: str) -> str:
    url = os.environ.get(var) or default
    check_url(url, var)
    if url != default and var not in _warned:
        _warned.add(var)
        print(f"kennet: warning: {var} points at {url}, not {default}", file=sys.stderr)
    return url


def accounts_url() -> str:
    return _endpoint("METAPHI_ACCOUNTS", DEFAULT_ACCOUNTS).rstrip("/")


def api_url() -> str:
    return _endpoint("METAPHI_API", DEFAULT_API).rstrip("/")


def home() -> Path:
    return Path(os.environ.get("METAPHI_HOME") or Path.home() / ".config" / "metaphi")


def path() -> Path:
    return home() / "config.json"


def _read_file() -> dict[str, Any]:
    p = path()
    try:
        text = p.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise Error(f"cannot read {p}: {e.strerror or e}") from None
    try:
        cfg = json.loads(text)
    except ValueError:
        raise Error(f"{p} is not valid JSON; fix it or delete it and sign in again") from None
    if not isinstance(cfg, dict):
        raise Error(f"{p} does not hold a JSON object; fix it or delete it and sign in again")
    return cfg


def load() -> dict[str, Any]:
    return _read_file()


def _write_file(cfg: dict[str, Any]) -> None:
    d = home()
    if not d.is_dir():
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    elif os.name == "posix" and d.stat().st_mode & 0o077:
        print(f"kennet: warning: {d} is readable by others; run `chmod 700 {d}`", file=sys.stderr)
    tmp = d / f".config.json.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(cfg, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save(cfg: dict[str, Any]) -> None:
    _write_file(dict(cfg))


def api_key(flag: str | None = None, test: bool = False) -> str | None:
    return flag or os.environ.get("METAPHI_API_KEY") or load().get("test_api_key" if test else "api_key")
