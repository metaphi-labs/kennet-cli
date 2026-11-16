from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from metaphi import accounts, config, errors

RETRIES = 2
BASE_S = 0.5
BUDGET_S = 5.0
API_VERSION = "2026-10-11"
USER_AGENT = config.USER_AGENT
UPLOAD = httpx.Timeout(30.0, connect=5.0, read=600.0, write=600.0)


def error(r: httpx.Response) -> errors.MetaphiError:
    try:
        body = r.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return errors.from_object({"request_id": r.headers.get("request-id"), **error})
    return errors.APIError(
        f"the Metaphi AI API answered HTTP {r.status_code}",
        retryable=r.status_code >= 500,
        request_id=r.headers.get("request-id"),
    )


def retry_after(r: httpx.Response) -> float | None:
    held = r.headers.get("retry-after", "").strip()
    try:
        return max(0.0, float(held)) if held else None
    except ValueError:
        return None


class Tries:
    def __init__(
        self, sleep: Callable[[float], None], retries: int | None = None, budget_s: float | None = None
    ) -> None:
        self.sleep, self.made = sleep, 0
        self.retries = RETRIES if retries is None else retries
        self.until = time.monotonic() + (BUDGET_S if budget_s is None else budget_s)

    def again(self, retry_after: float | None = None) -> bool:
        left = self.until - time.monotonic()
        wait = random.uniform(0, BASE_S * 2**self.made) if retry_after is None else retry_after
        if self.made >= self.retries or left <= 0 or wait > left:
            return False
        self.made += 1
        self.sleep(wait)
        return True


def send(
    h: httpx.Client,
    method: str,
    path: str,
    sleep: Callable[[float], None],
    *,
    retries: int | None = None,
    budget_s: float | None = None,
    say: Callable[[str], None] | None = None,
    **kw: Any,
) -> httpx.Response:
    tries = Tries(sleep, retries, budget_s)

    def again(why: str, request_id: str | None = None, after: float | None = None) -> bool:
        began = time.monotonic()
        if not tries.again(after):
            return False
        if say is not None:
            say(
                f"{method} {path}: {why}"
                + (f" (request {request_id})" if request_id else "")
                + f"; sent again after {time.monotonic() - began:.0f}s ({tries.made}/{tries.retries})"
            )
        return True

    while True:
        try:
            r = h.request(method, path, **kw)
        except httpx.TransportError as e:
            if again(f"no answer ({type(e).__name__})"):
                continue
            raise errors.APIError(
                f"the Metaphi AI API did not answer ({type(e).__name__})", code="unreachable", retryable=True
            ) from None
        if r.status_code < 300:
            return r
        if r.status_code < 400:
            if again(f"cut off at HTTP {r.status_code}", r.headers.get("request-id")):
                continue
            raise errors.APIError(
                "the service took too long to answer",
                code="unavailable",
                retryable=True,
                request_id=r.headers.get("request-id"),
            )
        failed = error(r)
        failed.retry_after_s = retry_after(r)
        if (
            not failed.retryable
            or failed.type == "rate_limit_error"
            or not again(f"HTTP {r.status_code} {failed.code or ''}".rstrip(), failed.request_id, failed.retry_after_s)
        ):
            raise failed


def client(key: str) -> httpx.Client:
    from metaphi.update import floor

    return httpx.Client(
        base_url=config.api_url(),
        timeout=httpx.Timeout(30.0, connect=5.0),
        event_hooks={"response": [floor]},
        headers={"authorization": f"Bearer {key}", "metaphi-version": API_VERSION, "user-agent": USER_AGENT},
    )


def key(api_key: str | None, test: bool) -> str:
    key = config.api_key(api_key, test)
    if not key:
        cfg = config.load()
        if not cfg.get("token"):
            raise accounts.need(
                "no test key: run `kennet login`, or set METAPHI_API_KEY to a test key"
                if test
                else "no key: run `kennet login`, or set METAPHI_API_KEY",
                "not_signed_in",
                "kennet login",
            )
        raise accounts.no_key(cfg)
    if not key.startswith(config.KEY_PREFIX):
        raise accounts.Error(f"that is not an organization key ({config.KEY_PREFIX}...)")
    return key


__all__ = [
    "API_VERSION",
    "BASE_S",
    "BUDGET_S",
    "RETRIES",
    "UPLOAD",
    "USER_AGENT",
    "Tries",
    "client",
    "error",
    "key",
    "retry_after",
    "send",
]
