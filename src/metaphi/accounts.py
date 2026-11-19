from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import secrets
import sys
import time
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from metaphi import config
from metaphi.errors import MetaphiError

CLIENT_ID = "metaphi-cli"
CALLBACK = "/callback"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SIGN_IN_S = 600.0
STEP_UP_MAX_AGE_S = 300


CHECKOUT = "https://checkout.stripe.com/"
MIN_INTERVAL = 5.0
ROLES = ("owner", "admin", "billing", "member")
MANAGERS = ("owner", "admin")


class Error(MetaphiError):
    pass


def need(message: str, code: str, next_step: str | None) -> Error:
    return Error(message, code=code, type="auth_error", next_step=next_step)


UNAVAILABLE = (502, 503, 504)


def _unanswered(r: httpx.Response) -> Error:
    again = r.status_code in UNAVAILABLE or r.status_code < 400
    e = Error(
        f"the service answered HTTP {r.status_code}; try again in a minute",
        code="service_unavailable" if again else None,
        type="api_error",
        retryable=again,
    )
    e.status = r.status_code
    held = r.headers.get("retry-after", "").strip()
    with contextlib.suppress(ValueError):
        e.retry_after_s = max(0.0, float(held)) if held else None
    return e


def _refused(r: httpx.Response) -> Error:
    try:
        r.json()
    except ValueError:
        return _unanswered(r)
    e = Error(_said(r), code=_code(r))
    e.status = r.status_code
    return e


def _said(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return _unanswered(r).message
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            return detail["message"]
        return str(detail or body.get("error_description") or body.get("error") or body)
    return str(body)


def _code(r: httpx.Response) -> str | None:
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    code = detail.get("code") if isinstance(detail, dict) else body.get("code")
    return code if isinstance(code, str) and code else None


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        body = None
    if body is None:
        raise _unanswered(r)
    if not isinstance(body, dict):
        raise Error(_said(r))
    return body


def _http() -> httpx.Client:
    from metaphi.update import floor

    return httpx.Client(
        base_url=config.accounts_url(),
        timeout=30.0,
        event_hooks={"response": [floor]},
        headers={"user-agent": config.USER_AGENT},
    )


class _Return:
    def __init__(self, state: str, done_url: str) -> None:
        import http.server
        from urllib.parse import parse_qsl, urlsplit

        got: dict[str, str] = {}
        self.got = got

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                where = urlsplit(self.path)
                asked = dict(parse_qsl(where.query))
                if (
                    got
                    or where.path != CALLBACK
                    or asked.get("state") != state
                    or not ("code" in asked or "error" in asked)
                ):
                    self.send_response(404)
                    self.end_headers()
                    return
                got.update(asked)
                self.send_response(302)
                self.send_header("Location", done_url + ("?denied=1" if "error" in asked else "?signed_in=1"))
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 0.5
        self.uri = f"http://127.0.0.1:{self.server.server_port}{CALLBACK}"

    def wait(self, until: float) -> dict[str, str]:
        while not self.got and time.time() < until:
            self.server.handle_request()
        return self.got

    def close(self) -> None:
        self.server.server_close()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _keep(body: dict[str, Any], token: str) -> None:
    cfg = config.load()
    for name in (
        "test_api_key",
        "org",
        "scopes",
        "no_api_key",
        "no_api_key_code",
        "no_api_key_org",
        "invitation",
        "step_up",
    ):
        cfg.pop(name, None)
    cfg.update({"token": token, "email": body.get("email")})
    if all(str(body.get(k) or "").startswith(config.KEY_PREFIX) for k in ("api_key", "test_api_key")):
        cfg.update({"api_key": body["api_key"], "test_api_key": body["test_api_key"], "api_key_from": "login"})
        if isinstance(body.get("org"), str) and body["org"]:
            cfg["org"] = body["org"]
        if isinstance(body.get("scopes"), list):
            cfg["scopes"] = [x for x in body["scopes"] if isinstance(x, str)]
    else:
        if cfg.get("api_key_from") == "login":
            cfg.pop("api_key", None)
            cfg.pop("api_key_from", None)
        if isinstance(body.get("org"), str) and body["org"]:
            cfg["no_api_key_org"] = body["org"]
    cfg.update({k: body[k] for k in ("no_api_key", "no_api_key_code") if isinstance(body.get(k), str) and body[k]})
    if (invitation := invited(body)) is not None:
        cfg["invitation"] = invitation
    config.save(cfg)


def invited(body: dict[str, Any]) -> dict[str, Any] | None:
    held = body.get("invitation")
    if not (isinstance(held, dict) and isinstance(held.get("org"), str) and held["org"]):
        return None
    return {k: held.get(k) for k in ("id", "org", "org_name", "role", "expires") if held.get(k) is not None}


def pickup() -> dict[str, Any] | None:
    token = config.load().get("token")
    if not token:
        return None
    return _keys(token, fresh=False)


def _keys(token: str, fresh: bool = True) -> Any:
    with _http() as h:
        r = h.post("/keys/cli", headers={"authorization": f"Bearer {token}"})
    if r.status_code in (401, 403):
        if not fresh:
            return None
        raise need(
            "the sign-in server did not take the token it handed; run `kennet login` again",
            "login_refused",
            "kennet login",
        )
    if r.status_code != 200:
        raise _refused(r)
    body = _json(r)
    _keep(body, token)
    return body


def headless() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


_sleep = time.sleep


def device_start() -> dict[str, Any]:
    """Start a device sign-in: the code the person enters, where, and when it expires."""
    with _http() as h:
        r = h.post("/device/code", data={"client_id": CLIENT_ID})
    if r.status_code != 200:
        raise _refused(r)
    started = _json(r)
    code = started.get("device_code")
    if not isinstance(code, str) or not isinstance(started.get("user_code"), str):
        raise need("the sign-in server sent no device code", "login_refused", "kennet login")
    return {
        "device_code": code,
        "user_code": started["user_code"],
        "url": started.get("verification_uri"),
        "page": started.get("verification_uri_complete") or started.get("verification_uri"),
        "interval": max(float(started.get("interval") or 5), 2.0),
        "expires_at": time.time() + float(started.get("expires_in") or SIGN_IN_S),
    }


def device_poll(started: dict[str, Any], wait_s: float | None = None) -> dict[str, Any] | None:
    """The keys once the person has approved the sign-in; None if `wait_s` ran out first. Raises when it was
    declined or has expired."""
    interval, until = started["interval"], started["expires_at"]
    if wait_s is not None:
        until = min(until, time.time() + wait_s)
    while time.time() < until:
        _sleep(interval)
        with _http() as h:
            r = h.post(
                "/oauth/token",
                data={"grant_type": DEVICE_GRANT, "device_code": started["device_code"], "client_id": CLIENT_ID},
            )
        got = _json(r)
        if r.status_code == 200 and isinstance(got.get("access_token"), str):
            return _keys(got["access_token"])
        error = got.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise need("the sign-in was declined", "login_refused", "kennet login")
        if error == "expired_token":
            break
        raise need(_said(r), "login_refused", "kennet login")
    if time.time() < started["expires_at"] and wait_s is not None:
        return None
    raise need("the sign-in was not finished in time; run `kennet login` again", "login_expired", "kennet login")


def device_login(say: Callable[[str], None] = print, wait_s: float | None = None) -> dict[str, Any]:
    started = device_start()
    say(f"To sign in, open {started['url']} and enter the code {started['user_code']}")
    if started["page"] != started["url"]:
        say(f"Or open: {started['page']}")
    got = device_poll(started, wait_s)
    if got is None:
        raise need("the sign-in was not finished in time; run `kennet login` again", "login_expired", "kennet login")
    return got


PENDING = "device_pending"


def pending_login(wait_s: float = 10.0) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Sign in with no one waiting at a terminal: finish the device sign-in started on an earlier run, or start one
    and keep it. Answers (keys, the sign-in) once approved, or (None, the sign-in to show) while it is pending."""
    started = config.load().get(PENDING)
    if isinstance(started, dict) and float(started.get("expires_at") or 0) > time.time():
        try:
            got = device_poll(started, wait_s)
        except Error as e:
            _forget_pending()
            if e.code != "login_expired":
                raise
        else:
            if got is None:
                return None, started
            _forget_pending()
            return got, started
    started = device_start()
    config.save({**config.load(), PENDING: started})
    return None, started


def _forget_pending() -> None:
    cfg = config.load()
    if cfg.pop(PENDING, None) is not None:
        config.save(cfg)


def login(
    say: Callable[[str], None] = print, wait_s: float | None = None, device: bool | None = None
) -> dict[str, Any]:
    if device or (device is None and headless()):
        return device_login(say, wait_s)
    base = config.accounts_url()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    back = _Return(state, f"{base}/authorize")
    try:
        asked = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": back.uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if config.load().get("step_up"):
            asked["max_age"] = str(STEP_UP_MAX_AGE_S)
        page = f"{base}/oauth/authorize?{urlencode(asked)}"
        webbrowser.open(page)
        say("Opening your browser to sign in\u2026")
        say(f"If it did not open, go to: {page}")
        got = back.wait(time.time() + (SIGN_IN_S if wait_s is None else min(SIGN_IN_S, wait_s)))
    finally:
        back.close()
    if "error" in got:
        raise need(got.get("error_description") or "the sign-in was declined", "login_refused", "kennet login")
    if not got:
        raise need("the sign-in was not finished in time; run `kennet login` again", "login_expired", "kennet login")
    with _http() as h:
        r = h.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": got["code"],
                "redirect_uri": back.uri,
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            },
        )
    body = _json(r)
    if r.status_code != 200 or not isinstance(body.get("access_token"), str):
        raise need(
            _said(r) if r.status_code != 200 else "the sign-in server sent no access token",
            "login_refused",
            "kennet login",
        )
    return _keys(body["access_token"])


def logout() -> None:
    cfg = config.load()
    token = cfg.pop("token", None)
    for name in (
        "email",
        "api_key",
        "api_key_from",
        "test_api_key",
        "org",
        "no_api_key",
        "no_api_key_code",
        "no_api_key_org",
        "invitation",
        "step_up",
    ):
        cfg.pop(name, None)
    config.save(cfg)
    if token:
        with _http() as h:
            h.post("/logout", headers={"authorization": f"Bearer {token}"})


def _person() -> dict[str, str]:
    token = config.load().get("token")
    if not token:
        raise need("not signed in; run `kennet login`", "not_signed_in", "kennet login")
    return {"authorization": f"Bearer {token}"}


def call(method: str, path: str, **kw: Any) -> Any:
    with _http() as h:
        r = h.request(method, path, headers=_person(), **kw)
    if r.status_code == 401 and _code(r) == "step_up_required":
        cfg = config.load()
        cfg["step_up"] = True
        config.save(cfg)
        raise need(_said(r), "step_up_required", "kennet login")
    if r.status_code == 401:
        try:
            r.json()
        except ValueError:
            raise need("your sign-in has ended; run `kennet login`", "not_signed_in", "kennet login") from None
        e = need(_said(r), _code(r), "kennet login")
        e.status = 401
        raise e
    if r.status_code >= 400:
        raise _refused(r)
    try:
        return r.json()
    except ValueError:
        raise _unanswered(r) from None


def orgs() -> list[dict[str, Any]]:
    return call("GET", "/keys/orgs")


def mnda(sign: bool = False) -> dict[str, Any]:
    every = orgs()
    if not every:
        raise need(
            "you are in no organization; make one, then ask again", "no_organization", "kennet org create <name>"
        )
    org = every[0]["id"]
    got = call("GET", f"/keys/orgs/{org}/mnda")
    if sign and got.get("status") == "none":
        got = call("POST", f"/keys/orgs/{org}/mnda")
    return {"org": org, **{k: got.get(k) for k in ("status", "signed_at", "url", "version")}}


def create_org(name: str) -> dict[str, Any]:
    made = call("POST", "/keys/orgs", json={"name": name})
    if not (isinstance(made, dict) and isinstance(made.get("id"), str)):
        raise Error("the server sent no organization")
    cfg = config.load()
    cfg["org"] = made["id"]
    if isinstance(made.get("message"), str):
        cfg["no_api_key"] = made["message"].rstrip(".")
    cfg["no_api_key_code"] = "no_keys_yet" if made.get("access") == "active" else "awaiting_approval"
    config.save(cfg)
    return made


def expired(invitation: dict[str, Any], now: float | None = None) -> bool:
    at = invitation.get("expires")
    return isinstance(at, (int, float)) and at <= (time.time() if now is None else now)


def invitations() -> list[dict[str, Any]]:
    got = call("GET", "/keys/invitations")
    held = [invited({"invitation": {"org_name": i.get("name"), **i}}) for i in got if isinstance(i, dict)]
    return [i for i in held if i is not None and not expired(i)]


def _invitation_org(target: str) -> str:
    if not target.startswith("inv_"):
        return target
    open_ = invitations()
    for i in open_:
        if i.get("id") == target:
            return str(i["org"])
    if len(open_) == 1 and not open_[0].get("id"):
        return str(open_[0]["org"])
    raise Error(f"you have no open invitation {target}; `kennet whoami` shows the ones you have", code="no_invitation")


def join(target: str) -> dict[str, Any]:
    target = target.strip()
    org_id = _invitation_org(target)
    try:
        got = call(
            "POST",
            f"/keys/invitations/{quote(org_id, safe='')}",
            **({"json": {"id": target}} if target.startswith("inv_") else {}),
        )
    except Error as e:
        if e.code == "invitation_for_another_address":
            e.next_step = "kennet logout"
            e.advice = "run `kennet logout`, then `kennet login` as the address the invitation was sent to"
            raise
        if getattr(e, "status", None) != 404:
            raise
        e = Error(f"{e.message}; it may have expired or been taken back", code="no_invitation")
        e.advice = f"ask an owner of {org_id} to invite you again"
        raise e from None
    if not isinstance(got, dict):
        raise Error("the server sent no membership")
    return {"org": str(got.get("org") or org_id), "role": got.get("role")}


def managed(name: str | None, what: str = "members") -> dict[str, Any]:
    every = orgs()
    mine = [o for o in every if o.get("role") in MANAGERS]
    unmanaged = not mine or name and name not in [o["id"] for o in mine]
    if unmanaged and (theirs := next((o for o in every if not name or o["id"] == name), None)) is not None:
        raise _not_manager(theirs, what)
    if name:
        for o in mine:
            if o["id"] == name:
                return o
        raise Error(f"you manage no organization {name!r}; yours: {', '.join(o['id'] for o in mine) or 'none'}")
    if len(mine) == 1:
        return mine[0]
    if not mine:
        raise Error("you are an owner or admin of no organization")
    raise Error(f"you manage {len(mine)} organizations; name one with --org ({', '.join(o['id'] for o in mine)})")


def _not_manager(o: dict[str, Any], what: str = "members") -> Error:
    role = o.get("role") or "member"
    return Error(
        f"only owners and admins of {o['id']} see or change its {what}; you're "
        f"{'an' if role[0] in 'aeiou' else 'a'} {'billing member' if role == 'billing' else role}",
        code="not_manager",
    )


def ask_access(org_id: str, scopes: list[str], reason: str | None = None) -> dict[str, Any]:
    return call(
        "POST",
        f"/keys/orgs/{quote(org_id, safe='')}/requests",
        json={"scopes": scopes, **({"reason": reason} if reason else {})},
    )


def domains(org_id: str) -> list[dict[str, Any]]:
    return call("GET", f"/keys/orgs/{quote(org_id, safe='')}/domains")


def add_domain(org_id: str, domain: str) -> dict[str, Any]:
    return call("POST", f"/keys/orgs/{quote(org_id, safe='')}/domains", json={"domain": domain})


def verify_domain(org_id: str, domain: str) -> dict[str, Any]:
    return call("POST", f"/keys/orgs/{quote(org_id, safe='')}/domains/{quote(domain, safe='')}/verify")


def access_requests(org_id: str) -> list[dict[str, Any]]:
    return call("GET", f"/keys/orgs/{quote(org_id, safe='')}/requests")


def _member(org_id: str, email: str) -> str:
    return f"/keys/orgs/{quote(org_id, safe='')}/members/{quote(email.strip().lower(), safe='@')}"


def set_member(org_id: str, email: str, role: str) -> dict[str, Any]:
    return call("PUT", _member(org_id, email), json={"role": role})


def remove_member(org_id: str, email: str) -> dict[str, Any]:
    return call("DELETE", _member(org_id, email))


def create_key(org_id: str, name: str, scopes: list[str], test: bool = False) -> dict[str, Any]:
    made = call("POST", f"/keys/orgs/{quote(org_id, safe='')}", json={"name": name, "scopes": scopes, "test": test})
    if not (isinstance(made, dict) and str(made.get("key") or "").startswith(config.KEY_PREFIX)):
        raise Error("the server sent no key")
    return made


def revoke_key(org_id: str, key_id: str) -> dict[str, Any]:
    return call("DELETE", f"/keys/orgs/{quote(org_id, safe='')}/{quote(key_id, safe='')}")


def org_credits() -> list[dict[str, Any]]:
    return call("GET", "/credits/orgs")


def available(org: str) -> dict[str, Any]:
    return call("GET", "/credits/me", params={"org": org})


def my_spend(month: str | None = None, org: str | None = None) -> dict[str, Any]:
    return call("GET", "/credits/me/spend", params={k: v for k, v in (("month", month), ("org", org)) if v})


def credit_of(name: str | None) -> dict[str, Any]:
    mine = org_credits()
    if name:
        for o in mine:
            if o["id"] == name:
                return o
        raise Error(
            f"you see the balance of no organization {name!r}; yours: {', '.join(o['id'] for o in mine) or 'none'}"
        )
    if len(mine) == 1:
        return mine[0]
    if not mine:
        raise Error("you are an owner, admin or billing member of no organization")
    raise Error(f"you see {len(mine)} organizations; name one with --org ({', '.join(o['id'] for o in mine)})")


def buy(
    org_id: str,
    usd: float,
    say: Callable[[str], None] = print,
    open_browser: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    wait_s: float = 1800.0,
) -> dict[str, Any]:
    made = call("POST", f"/credits/orgs/{org_id}/checkout", json={"usd": usd})
    if not (isinstance(made, dict) and isinstance(made.get("url"), str) and isinstance(made.get("id"), str)):
        raise Error("the server sent no checkout link")
    url = made["url"]
    if not url.startswith(CHECKOUT):
        raise Error(f"the checkout link is not Stripe's: {url!r}")
    say(f"Pay in the browser:\n  {url}")
    if open_browser:
        webbrowser.open(url)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        sleep(MIN_INTERVAL)
        got = call("GET", f"/credits/orgs/{org_id}/checkout/{made['id']}")
        if got.get("paid"):
            return got
    raise Error("the checkout was not paid in time; run `kennet credits` to see the balance")


def no_key(cfg: dict[str, Any], again: bool = True, fresh: dict[str, Any] | None = None) -> Error:
    why = str(cfg.get("no_api_key") or "your organization has no key for this machine yet").rstrip(".")
    reason = cfg.get("no_api_key_code")
    cached = invited(cfg)
    invitation = fresh if fresh is not None else (cached if cached and not expired(cached) else None)
    if invitation is not None and reason in (None, "no_organization", "invitation_pending"):
        org = invitation["org"]
        if reason != "invitation_pending" or fresh is not None and cached and cached.get("org") != org:
            why = f"{invitation.get('org_name') or org} has invited you to join it"
        step = f"kennet org join {org}" if fresh is not None else ("kennet login" if again else None)
        e = need(f"no key: {why}", "no_api_key", step)
        e.context = {"reason": "invitation_pending", "invitation": invitation}
        return e
    if reason == "removed":
        org = cfg.get("no_api_key_org") or cfg.get("org") or "your organization"
        e = need(f"no key: {why}", "no_api_key", None)
        e.advice = f"ask an owner of {org} to add you back"
        e.context = {"reason": reason}
        return e
    step = {"no_organization": "kennet org create <name>"}.get(reason, "kennet login" if again else None)
    e = need(
        f"no key: {why}. Signed in as {cfg.get('email')}; `kennet login` saves the keys once "
        "that changes, or set METAPHI_API_KEY",
        "no_api_key",
        step,
    )
    e.context = {"reason": reason}
    return e
