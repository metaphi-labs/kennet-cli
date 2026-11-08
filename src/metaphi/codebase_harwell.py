"""A mainframe estate's build on Harwell, for `kennet profile-codebase --deep`.

A repository holding harwell.toml is a codebase Harwell runs (github.com/harwell-project/harwell). With `--deep`, the
tree under the shallowest harwell.toml at the tip is sent to Harwell's `build` (https://harwell.metaphi.ai/mcp/),
which compiles every program member and assembles every BMS mapset there, and runs nothing. Nothing compiles on this
machine. What is kept is Harwell's counts: never a member's or a library's name, nor what the compiler printed.

It is called with the organization's key (`mai_sk_...`, the one `kennet login` saves or METAPHI_API_KEY), which
Harwell admits when the key holds `harwell:runs:write`. The call is MCP's tools/call over streamable HTTP at protocol
2026-07-28; a build outlives the 150 s the endpoint holds a request, so it runs as a task (the Tasks extension,
`io.modelcontextprotocol/tasks`) and tasks/get is polled to its end.
"""
from __future__ import annotations

import base64
import io
import itertools
import json
import os
import time
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

from metaphi.steps import NONE, Steps

URL = "https://harwell.metaphi.ai/mcp/"
PROTOCOL = "2026-07-28"
TASKS = "io.modelcontextprotocol/tasks"
ARGS_MOST = 8 << 20   # Harwell keeps a task's arguments up to this (harwell/mcp/server.py TASK_ARGS_MAX)
WAIT_S = 2 * 3600     # Harwell stops a run at an hour; a run its container lost is run again from the start
AGAIN = 5             # a poll that went unanswered, asked again this many times in a row
COUNTS = ("members", "clean", "failed", "mapsets_assembled", "mapsets_failed", "seconds")
SAID = {"too_large": "its files are past what one Harwell call takes",
        "no_access": "the organization's key does not reach Harwell's build: it takes harwell:runs:write "
                     "(`kennet org request harwell:runs:write`)",
        "refused": "Harwell did not build it",
        "unreachable": "Harwell did not answer"}


class _Why(Exception):
    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


def estate(paths: Iterable[str]) -> tuple[str, int] | None:
    """The directory of the shallowest harwell.toml among `paths` ("" for the root), and how many there are."""
    found = sorted((p for p in paths if p == "harwell.toml" or p.endswith("/harwell.toml")),
                   key=lambda p: (p.count("/"), p))
    return (os.path.dirname(found[0]), len(found)) if found else None


def zipped(repo: Path, tree: dict[str, tuple[str, str]], root: str) -> bytes:
    """The files under `root` at the tree, named from `root`, as one zip: the same tree zips to the same bytes.
    Links, and anything that is not a file, stay out."""
    from metaphi.codebase_profile import _blobs
    under = {p[len(root) + 1:] if root else p: blob for p, (mode, blob) in tree.items()
             if (not root or p.startswith(root + "/")) and mode in ("100644", "100755")}
    by_blob: dict[str, list[str]] = {}
    for name, blob in under.items():
        by_blob.setdefault(blob, []).append(name)
    bodies = {name: body for blob, body in _blobs(repo, list(by_blob)) for name in by_blob[blob]}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(bodies):
            z.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), bodies[name], zipfile.ZIP_DEFLATED)
    return out.getvalue()


class _Mcp:
    """MCP over streamable HTTP, one request a call: each carries its protocol version, who asks and what it can do in
    its `_meta`, and the method and the name it bears in headers, as the 2026-07-28 protocol has a stateless server
    read them."""

    def __init__(self, key: str, client: httpx.Client | None = None):
        from metaphi import __version__
        self.http = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0, read=180.0))
        self.headers = {"authorization": f"Bearer {key}", "accept": "application/json, text/event-stream",
                        "mcp-protocol-version": PROTOCOL, "user-agent": f"metaphi-cli/{__version__}"}
        self.meta: dict[str, Any] = {"io.modelcontextprotocol/protocolVersion": PROTOCOL,
                                     "io.modelcontextprotocol/clientInfo": {"name": "kennet", "version": __version__},
                                     "io.modelcontextprotocol/clientCapabilities": {}}
        self.ids = itertools.count(1)

    def ask(self, method: str, params: dict[str, Any], name: str | None = None) -> dict[str, Any]:
        n = next(self.ids)
        headers = {**self.headers, "mcp-method": method, **({"mcp-name": name} if name else {})}
        try:
            r = self.http.post(URL, headers=headers,
                               json={"jsonrpc": "2.0", "id": n, "method": method, "params": {**params, "_meta": self.meta}})
        except httpx.HTTPError:
            raise _Why("unreachable") from None
        if r.status_code in (401, 403):
            raise _Why("no_access")
        if r.status_code == 413:
            raise _Why("too_large")
        if r.status_code >= 400:
            raise _Why("unreachable")
        try:
            said = [json.loads(x[5:]) for x in r.text.splitlines() if x.startswith("data:")] \
                if r.headers.get("content-type", "").startswith("text/event-stream") else [r.json()]
        except ValueError:
            raise _Why("unreachable") from None
        got = next((x for x in said if isinstance(x, dict) and x.get("id") == n), None)
        if got is None:
            raise _Why("unreachable")
        if not isinstance(got.get("result"), dict):
            raise _Why("refused")
        return got["result"]


def _counts(result: dict[str, Any]) -> dict[str, Any]:
    """Harwell's counts from a build's result, or why there are none."""
    b = (result.get("structuredContent") or {}).get("build")
    if isinstance(b, dict) and all(isinstance(b.get(k), (int, float)) and not isinstance(b.get(k), bool)
                                   and b[k] >= 0 for k in COUNTS):
        return {k: round(b[k]) for k in COUNTS}
    text = " ".join(str(c.get("text") or "") for c in result.get("content") or [] if isinstance(c, dict))
    if "needs harwell:" in text:
        return {"why": "no_access"}
    if "task-args-too-large" in text or "zip-size" in text or "zip-members" in text:
        return {"why": "too_large"}
    return {"why": "refused"}


def build(body: bytes, key: str, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Harwell's `build` of the zipped tree `body`: its counts, or `why` there are none."""
    b64 = base64.b64encode(body).decode("ascii")
    if len(b64) + 4096 > ARGS_MOST:
        return {"why": "too_large"}
    m = _Mcp(key, client)
    try:
        found = m.ask("server/discover", {})
        if TASKS in ((found.get("capabilities") or {}).get("extensions") or {}):
            m.meta["io.modelcontextprotocol/clientCapabilities"] = {"extensions": {TASKS: {}}}
        got = m.ask("tools/call", {"name": "build", "arguments": {"codebase_zip_b64": b64}}, "build")
        if got.get("resultType") == "task":
            task, until, missed = str(got.get("taskId") or ""), clock() + WAIT_S, 0
            while got.get("status") == "working":
                if clock() > until:
                    return {"why": "unreachable"}
                sleep(max(1.0, min(60.0, float(got.get("pollIntervalMs") or 5000) / 1000)))
                try:
                    got, missed = m.ask("tasks/get", {"taskId": task}, task), 0
                except _Why as e:
                    if e.why != "unreachable" or (missed := missed + 1) > AGAIN:
                        raise
            if got.get("status") != "completed" or not isinstance(got.get("result"), dict):
                return {"why": "refused"}
            got = got["result"]
        return _counts(got)
    except _Why as e:
        return {"why": e.why}
    finally:
        if client is None:
            m.http.close()


def checked(repo: Path, sha: str, name: str, key: str, say: Callable[[str], None] = print,
            steps: Steps = NONE, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """The estate at `sha` built on Harwell, as the checks send it; None where the tree holds no harwell.toml."""
    from metaphi.codebase_checks import _tree
    tree = _tree(repo, sha)
    found = estate(tree)
    if found is None:
        return None
    root, codebases = found
    say(f"{name}: building {root or 'the root'} on Harwell (sends its files to Metaphi AI)")
    with steps.step("harwell"):
        got = build(zipped(repo, tree, root), key, client)
    if "why" in got:
        say(f"{name}: no Harwell build: {SAID[got['why']]}")
    else:
        say(f"{name}: Harwell build: {got['members']} members, {got['clean']} clean, {got['failed']} failed; "
            f"{got['mapsets_assembled']} mapsets assembled, {got['mapsets_failed']} failed; {got['seconds']}s")
    return {"codebases": codebases, **got}


__all__ = ["build", "checked", "estate", "zipped"]
