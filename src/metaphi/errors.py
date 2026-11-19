from __future__ import annotations

from typing import Any

from metaphi import config


class MetaphiError(config.Error):
    type: str | None = None

    def __init__(
        self,
        message: str,
        code: str | None = None,
        type: str | None = None,
        retryable: bool = False,
        request_id: str | None = None,
        run_id: str | None = None,
        action_url: str | None = None,
        next_step: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.type = type or self.type
        self.retryable = retryable
        self.request_id = request_id
        self.run_id = run_id
        self.action_url = action_url
        self.next_step = next_step
        self.retry_after_s = retry_after_s
        self.context: dict[str, Any] = {}
        self.advice: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.type,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "next_step": self.next_step,
        }
        out.update(
            {
                k: v
                for k, v in (("request_id", self.request_id), ("run_id", self.run_id), ("action_url", self.action_url))
                if v
            }
        )
        if self.retry_after_s is not None:
            out["retry_after_s"] = self.retry_after_s
        if self.advice:
            out["advice"] = self.advice
        return out

    def __str__(self) -> str:
        said = [f"code: {self.code}"] if self.code else []
        said += [f"request: {self.request_id}"] if self.request_id else []
        return f"{self.message} ({', '.join(said)})" if said else self.message


class BillingError(MetaphiError):
    type = "billing_error"


class AuthError(MetaphiError):
    type = "auth_error"


class RateLimitError(MetaphiError):
    type = "rate_limit_error"


class InvalidRequestError(MetaphiError):
    type = "invalid_request_error"


class IdempotencyError(MetaphiError):
    type = "idempotency_error"


class RunError(MetaphiError):
    type = "run_error"


class APIError(MetaphiError):
    type = "api_error"


BY_TYPE: dict[str, type[MetaphiError]] = {
    c.type: c
    for c in (BillingError, AuthError, RateLimitError, InvalidRequestError, IdempotencyError, RunError, APIError)
}


def from_object(error: dict[str, Any]) -> MetaphiError:
    kind = error.get("type")
    cls = BY_TYPE.get(kind, MetaphiError) if isinstance(kind, str) else MetaphiError

    def text(key: str) -> str | None:
        value = error.get(key)
        return value if isinstance(value, str) and value else None

    got = cls(
        text("message") or text("code") or "the call failed",
        code=text("code"),
        type=kind if isinstance(kind, str) else None,
        retryable=error.get("retryable") is True,
        request_id=text("request_id"),
        run_id=text("run_id"),
        action_url=text("action_url"),
    )
    if tip := text("filed_tip"):  # profile_mismatch: the tip the CodeVal run measures, as data
        got.context["filed_tip"] = tip
    if isinstance(error.get("measure"), int):  # superseded: the CodeVal run's live measure, as data
        got.context["measure"] = error["measure"]
    return got
