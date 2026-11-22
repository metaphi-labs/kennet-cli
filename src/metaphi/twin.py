from __future__ import annotations

import os
import shlex
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from metaphi import accounts, api, errors
from metaphi.api import UPLOAD, Tries, client, send

ENDED = ("completed", "failed", "cancelled", "expired")
FIRST_WAIT_S = 1.0
MAX_WAIT_S = 15.0
CONSISTENCY_KEY = "KENNET_CONSISTENCY_KEY"
DEADLINE_S = 2 * 3600.0


def _download(h: httpx.Client, twin_id: str, out: Path, sleep: Callable[[float], None]) -> None:
    tmp = out.with_name(f".{out.name}.{uuid.uuid4().hex[:8]}.part")
    tries = Tries(sleep)
    while True:
        try:
            with h.stream("GET", f"/twins/{twin_id}/content", timeout=UPLOAD) as r:
                if r.status_code >= 400:
                    r.read()
                    failed = api.error(r)
                    failed.retry_after_s = api.retry_after(r)
                    if not failed.retryable or not tries.again(failed.retry_after_s):
                        raise failed
                    continue
                with open(tmp, "wb") as f:
                    f.writelines(r.iter_bytes())
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, out)
                _sync_dir(out.parent)
                return
        except httpx.TransportError as e:
            tmp.unlink(missing_ok=True)
            if not tries.again():
                raise errors.APIError(
                    f"the Metaphi AI API did not answer ({type(e).__name__})", code="unreachable", retryable=True
                ) from None
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def _sync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def deadline() -> float:
    said = os.environ.get("METAPHI_TWIN_TIMEOUT", "").strip()
    try:
        got = float(said) if said else DEADLINE_S
    except ValueError:
        raise accounts.Error(f"METAPHI_TWIN_TIMEOUT is {said!r}; it is a number of seconds") from None
    if got <= 0:
        raise accounts.Error("METAPHI_TWIN_TIMEOUT is a number of seconds above zero")
    return got


def check(key: str, path: str = "/twins", sleep: Callable[[float], None] = time.sleep) -> errors.MetaphiError | None:
    try:
        with client(key) as h:
            send(h, "GET", path, sleep, params={"limit": 1})
    except errors.MetaphiError as e:
        return e
    return None


def consistency_key(flag: str | None = None, file: Path | None = None) -> str:
    if flag:
        return flag
    if file is not None:
        try:
            return file.read_text().strip()
        except OSError as e:
            raise accounts.Error(f"cannot read the key file {file}: {e.strerror or e}") from None
    return os.environ.get(CONSISTENCY_KEY, "").strip()


def again(name: str, test: bool = False) -> str:
    return f"kennet twin {shlex.quote(name)}" + (" --test" if test else "")


def get(
    twin_id: str, api_key: str | None = None, test: bool = False, sleep: Callable[[float], None] = time.sleep
) -> dict[str, Any]:
    with client(api.key(api_key, test)) as h:
        return send(h, "GET", f"/twins/{twin_id}", sleep).json()


def recent(
    limit: int = 10, api_key: str | None = None, test: bool = False, sleep: Callable[[float], None] = time.sleep
) -> list[dict[str, Any]]:
    with client(api.key(api_key, test)) as h:
        return send(h, "GET", "/twins", sleep, params={"limit": limit}).json()["data"]


def back(twin_id: str, test: bool = False) -> str:
    return f"kennet twins wait {twin_id}" + (" --test" if test else "")


def _finish(
    h: httpx.Client,
    made: dict[str, Any],
    out: Path,
    say: Callable[[str], None],
    sleep: Callable[[float], None],
    wait_s: float,
    clock: Callable[[], float],
    back: str,
    again: str | None = None,
) -> None:
    twin_id, status, wait = made["id"], made["status"], FIRST_WAIT_S
    last, until = status, clock() + wait_s
    try:
        while status not in ENDED:
            if clock() + wait > until:
                raise errors.APIError(
                    f"{twin_id} did not end within {wait_s:.0f}s; it goes on: `{back}` for it",
                    code="timed_out",
                    retryable=True,
                    run_id=twin_id,
                    next_step=back,
                )
            sleep(wait)
            wait = min(wait * 1.5, MAX_WAIT_S)
            made = send(h, "GET", f"/twins/{twin_id}", sleep).json()
            status = made["status"]
            if status != last:
                say(f"  {status}")
                last = status
    except KeyboardInterrupt:
        say(f"stopped waiting; {twin_id} goes on. Come back for it: {back}")
        raise
    if status != "completed":
        error = made.get("error") if isinstance(made.get("error"), dict) else {}
        failed = errors.from_object(
            {"type": "run_error", "code": None, "message": f"twinning {status}", "run_id": twin_id, **error}
        )
        failed.next_step = again if failed.retryable else None
        raise failed
    _download(h, twin_id, out, sleep)
    say(f"wrote {out}")


def _into(out: Path | None, out_dir: Path | None, name: Path) -> Path:
    if out is not None:
        return out
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{name.stem}.twin{name.suffix}"
    return name.with_name(f"{name.stem}.twin{name.suffix}")


def wait(
    twin_id: str,
    out: Path | None = None,
    api_key: str | None = None,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    deadline_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    test: bool = False,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    with client(api.key(api_key, test)) as h:
        made = send(h, "GET", f"/twins/{twin_id}", sleep).json()
        name = Path(Path(str(made.get("filename") or "")).name or "trace.txt")
        out = _into(out, out_dir, name)
        say(f"twinning ({twin_id}): {made['status']}")
        _finish(
            h,
            made,
            out,
            say,
            sleep,
            deadline() if deadline_s is None else deadline_s,
            clock,
            back(twin_id, test),
            again(name.name, test),
        )
    return {"output": str(out), "id": twin_id}


def twin(
    trace: Path,
    out: Path | None = None,
    api_key: str | None = None,
    key_hex: str = "",
    confidential: list[str] | None = None,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    deadline_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    test: bool = False,
    idempotency_key: str | None = None,
    detach: bool = False,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    key = api.key(api_key, test)
    if not trace.is_file():
        raise accounts.Error(f"no file {trace}")
    out = _into(out, out_dir, trace)
    started = time.time()
    wait_s = deadline() if deadline_s is None else deadline_s
    form: dict[str, Any] = {"key": key_hex} if key_hex else {}
    if confidential:
        form["confidential"] = list(confidential)
    with client(key) as h:
        body = trace.read_bytes()
        try:
            made = send(
                h,
                "POST",
                "/twins",
                sleep,
                files={"file": (trace.name, body)},
                data=form,
                timeout=UPLOAD,
                headers={"idempotency-key": idempotency_key or uuid.uuid4().hex},
            ).json()
        except errors.MetaphiError as e:
            if e.code in errors.QUOTA:
                e.message = f"its free test run is used; run it live: {again(str(trace))}"
                e.retryable, e.retry_after_s, e.next_step = False, None, again(str(trace))
            raise
        say(f"uploaded {trace.name} ({len(body):,} bytes)")
        twin_id = made["id"]
        say(f"twinning ({twin_id})")
        if detach:
            say(f"Come back for it: {back(twin_id, test)}")
            return {"id": twin_id, "status": made["status"], "next_step": back(twin_id, test)}
        _finish(h, made, out, say, sleep, wait_s, clock, back(twin_id, test), again(str(trace), test))
    say(f"done in {time.time() - started:.0f}s")
    return {"output": str(out), "id": twin_id}
