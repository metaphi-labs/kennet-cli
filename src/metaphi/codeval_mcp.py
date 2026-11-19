"""`kennet mcp`: CodeVal as a local MCP server over stdio, in three tools.

codeval_start measures the repositories here and sends nothing; codeval_status reads where a run stands and, once
measured, exactly what would leave the machine; codeval_send, the one consent point, signs in when needed and sends.
The engine is `kennet codeval`'s own: start is `--dry-run --detach`, send is `--yes --detach`, each a run of the
same id under one directory, so a send goes on from the measurement the person saw.

JSON-RPC 2.0, one message per line (MCP stdio transport, revisions 2024-11-05 to 2025-11-25). Tool calls run on
threads of their own so an elicitation's answer can be read while a call waits for it.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import shlex
import sys
import threading
from pathlib import Path
from typing import IO, Any

import httpx

from metaphi import __version__, accounts, api, config, errors

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
NAME = "metaphi"
# Clients known to ask the person before every call of a tool marked requiresUserInteraction.
HONOURS_CONSENT = ("claude-code",)
# Claude Code waits for a tool for hours; other clients stop a call after about 60 s (Codex's default).
WAIT_MAX_S = {"claude-code": 240}
WAIT_MAX_DEFAULT_S = 50
END_S = 10.0
ASK = "Send this no-code assessment to Metaphi AI for commercial diligence?"
ABOUT = (
    "CodeVal is a tool that helps data owners to monetize their private code with a completely private, and local "
    "only assessment. It collects data and de identified logs that help Metaphi AI evaluate your data for "
    "commercial licensing for AI training"
)

INSTRUCTIONS = f"""{ABOUT}.

Nothing leaves the machine until the person approves codeval_send. The procedure:
1. Read each repository yourself first: README, CI configuration, build files. Work out the commands that build it
   and run its tests.
2. codeval_start with the repositories' absolute paths (or git URLs) and the build and test commands you found.
   It returns a run id at once; measuring runs locally in the background.
3. codeval_status with the run id and wait_s, again until the state changes. Tell the person how far it is.
4. When status has `needs`, put `needs.question` to the person word for word, with `needs.choices`, and do what
   they choose.
5. When status has `preview`, show the person all of it: what would leave the machine, the sizes, and the folder
   where the exact files are.
6. Only after the person has seen the preview, call codeval_send. It asks the person to approve, and signs them in
   in the browser if needed. Never call it to find out whether they agree; ask them first.
7. codeval_status again until the state is `sent` or `accepted`. It goes on in the background for hours; the
   person can close the session and ask again later.
8. Tell the person that Metaphi AI will reach out if there is a commercial fit. There is never a score or a price
   to show.
Never ask the person to paste a key, token or code into the chat.
"""

PROMPT = (
    "Run CodeVal on my code repositories: measure them here, show me exactly what would be sent, and send it to "
    "Metaphi AI only if I say yes."
)

STATES = {
    "measuring": "Measuring on this machine; nothing has been sent. Call codeval_status with wait_s.",
    "needs_person": "Put needs.question to the person word for word, with needs.choices.",
    "ready": "Show the person the preview in full. Call codeval_send only after they have seen it.",
    "sending": "Sent the profile; building and testing past commits here, then sending the results. Call "
    "codeval_status with wait_s; it may take hours, and the person may come back later.",
    "sent": "Everything is sent. Tell the person that Metaphi AI will reach out if there is a commercial fit.",
    "accepted": "Metaphi AI has accepted the assessment. Tell the person that Metaphi AI will reach out if there "
    "is a commercial fit.",
    "stopped": "The run ended early (the machine slept or the process stopped). Call the same tool again: it goes "
    "on from the last finished commit and never sends anything twice.",
    "failed": "Tell the person error.message; call the same tool again once it is cleared.",
}

TOOLS = [
    {
        "name": "codeval_start",
        "title": "Measure repositories",
        "description": "Measure git repositories on this machine for CodeVal and return a run id at once; it goes "
        "on in the background. It reads each repository's history and builds and tests its latest commit. "
        "Nothing is sent. Read each repository yourself first (README, CI configuration, build files) and pass "
        "the build and test commands you find; your reading stays on this machine. Calling it again for the "
        "same repositories goes on with the same run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Absolute paths to full git clones, or git URLs. A folder of clones counts "
                    "each one.",
                },
                "build": {"type": "string", "description": "The command that builds the code, run before test."},
                "test": {
                    "type": "string",
                    "description": "The command that runs the tests. It may write JUnit XML to $KENNET_JUNIT.",
                },
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "codeval_status",
        "title": "Where a run stands",
        "description": "Where a CodeVal run stands: state, phase, done of total and seconds left when known; "
        "`needs`, a question for the person; `preview`, exactly what would leave the machine, once measured; "
        "after sending, the remote steps and `accepted_at`. Without run, it lists the runs. With wait_s, it "
        "returns as soon as the run moves on. Reads only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string", "description": "The run id from codeval_start."},
                "wait_s": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": max(WAIT_MAX_S.values()),
                    "description": "Wait up to this many seconds for the run to move on (capped below this "
                    "client's tool timeout).",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "codeval_send",
        "title": "Send to Metaphi AI",
        "description": "Send a measured run's no-code assessment to Metaphi AI, then build and test the past "
        "commits Metaphi AI names here and send those results, in the background. The person approves every "
        "call. Call it only after the person has seen the preview from codeval_status. Signs in through the "
        "browser when needed. Returns at once.",
        "inputSchema": {
            "type": "object",
            "properties": {"run": {"type": "string", "description": "The run id from codeval_start."}},
            "required": ["run"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
        "_meta": {"anthropic/requiresUserInteraction": True},
    },
]


def base() -> Path:
    """Where runs started here live: <config>/kennet-codeval/<run>, whatever directory the client started us in."""
    return config.home()


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n}"


def _what(row: dict[str, Any]) -> str:
    if row["file"] == "codeval.json.gz":
        return (
            "The history profile: counts and ids for files, folders, commits and authors, and the shape of commit "
            "messages. No file names, author names, commit message text or source code. This exact file is sent."
        )
    if row["file"] == "checks.json.gz":
        return (
            "Build and test results for the latest commit. After you send, results for the past commits Metaphi AI "
            "picks, and the git ids of files it names, are added, so the file sent is larger."
        )
    hidden = sum((row.get("redacted") or {}).values())
    return (
        "Build and test output, with common secret and token patterns replaced"
        + (f" ({hidden} replaced)" if hidden else "")
        + ". After you send, the output from past commits is added."
    )


def preview(home: Path) -> dict[str, Any] | None:
    try:
        rows = json.loads((home / "sent" / "sent.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(rows, list) or not rows or any(r.get("sent") for r in rows):
        return None
    order = {"codeval.json.gz": 0, "checks.json.gz": 1, "logs.tar.gz": 2}
    rows = sorted((r for r in rows if r.get("file") in order), key=lambda r: order[r["file"]])
    return {
        "summary": "Nothing has been sent. If you send, these leave this machine, and nothing else: no file "
        "names, author names, commit messages or source code.",
        "files": [
            {"file": r["file"], "size": _size(int(r.get("bytes") or 0)), "bytes": r.get("bytes"), "what": _what(r)}
            for r in rows
        ],
        "while_running": "How far it is: a phase, counts, and which question it waits on.",
        "folder": str(home / "sent"),
    }


def results(home: Path) -> list[str] | None:
    from metaphi.cli import _result_line

    try:
        report = json.loads((home / "checks.json").read_text())
        doc = json.loads((home / "codeval.json").read_text())
        return [
            _result_line(r["name"], got).strip()
            for r, got in zip(doc["repos"], report["repos"], strict=True)
            if isinstance(r, dict)
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _accepted(ref: str) -> str | None:
    with contextlib.suppress(Exception), api.client(api.key(None, False)) as h:
        r = h.get(f"/codeval/{ref}", timeout=httpx.Timeout(5.0, connect=3.0))
        if r.status_code == 200:
            got = r.json().get("accepted_at")
            return got if isinstance(got, str) else None
    return None


def _needs(v: dict[str, Any]) -> dict[str, Any] | None:
    held = v.get("needs") or {}
    if held.get("code") != "test_command_missing":
        return None
    names = ", ".join(held.get("repos") or []) or "a repository"
    return {
        "code": "test_command_missing",
        "question": f"CodeVal could not build and test {names}. What commands build and test it? Or should the "
        "assessment go without build and test results?",
        "choices": [
            {"label": "Give the build and test commands", "then": "codeval_start with the same paths, build and test"},
            {
                "label": "Send without build and test results",
                "then": "show the preview, then codeval_send on the person's yes",
            },
        ],
    }


def status(run: str, home: Path, v: dict[str, Any] | None = None) -> dict[str, Any]:
    from metaphi import codeval_runs

    v = v or codeval_runs.view(run, home)
    local, state = v["local"], v["state"]
    needs = _needs(v) if local and state == "needs_person" else None
    if state == "running":
        said = "measuring" if local else "sending"
    elif state == "needs_person":
        said = "needs_person" if needs else ("ready" if local else "sent")
    elif state == "done":
        said = "ready" if local else "sent"
    else:
        said = state if state in ("stopped", "failed") else "failed"
    out: dict[str, Any] = {
        "run": run,
        "state": said,
        "phase": v["phase"],
        "done": v["done"],
        "total": v["total"],
        "seconds_left": v["left_s"],
        "results_on_this_machine": v["results"],
    }
    if said in ("ready", "needs_person"):
        out["preview"] = preview(home)
        out["latest_commit"] = results(home)
    if needs:
        out["needs"] = needs
    if v["reference"]:
        out["reference"] = v["reference"]
    if said == "sent" and v["reference"] and (at := _accepted(v["reference"])):
        out["state"], out["accepted_at"] = "accepted", at
    if v["error"]:
        out["error"] = {k: v["error"].get(k) for k in ("code", "message", "request_id") if v["error"].get(k)}
    out["next"] = STATES[out["state"]]
    return out


def _namespace(paths: list[str], build: str | None, test: str | None, send: bool) -> argparse.Namespace:
    flags = {"deep": False, "yes": send, "dry_run": not send, "detach": True, "json": True, "worker": None}
    return argparse.Namespace(paths=paths, build=build, test=test, **flags)


def _paths(given: list[str]) -> list[str]:
    from metaphi import codeval

    return [p if codeval.is_url(p) else str(Path(p).expanduser().resolve()) for p in given]


class Refused(errors.MetaphiError):
    pass


class Server:
    def __init__(self, out: IO[str]) -> None:
        self.out, self.lock = out, threading.Lock()
        self.client: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.ids = itertools.count(1)
        self.waiting: dict[Any, tuple[threading.Event, list]] = {}
        self.calls: list[threading.Thread] = []

    # transport

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":"))
        with self.lock:
            self.out.write(line + "\n")
            self.out.flush()

    def ask(self, method: str, params: dict[str, Any], timeout: float = 600.0) -> dict[str, Any] | None:
        """A request to the client; its result, or None when it errs or does not answer in time."""
        key = f"s{next(self.ids)}"
        done, got = threading.Event(), []
        self.waiting[key] = (done, got)
        self.send({"jsonrpc": "2.0", "id": key, "method": method, "params": params})
        done.wait(timeout)
        self.waiting.pop(key, None)
        return got[0] if got and isinstance(got[0], dict) else None

    def log(self, line: str) -> None:
        print(line, file=sys.stderr)
        self.send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": line}})

    def handle(self, message: Any) -> None:
        if not isinstance(message, dict):
            return self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
        if "method" not in message:
            held = self.waiting.get(message.get("id"))
            if held:
                held[1].append(message.get("result"))
                held[0].set()
            return None
        method, key, params = message["method"], message.get("id"), message.get("params") or {}
        if key is None:
            return None  # notifications: initialized, cancelled; nothing to answer
        if method == "tools/call":
            call = threading.Thread(target=self._answer, args=(key, method, params), daemon=True)
            self.calls = [t for t in self.calls if t.is_alive()] + [call]
            call.start()
        else:
            self._answer(key, method, params)
        return None

    def _answer(self, key: Any, method: str, params: dict[str, Any]) -> None:
        try:
            result = self.dispatch(method, params)
        except LookupError as e:
            return self.send({"jsonrpc": "2.0", "id": key, "error": {"code": -32601, "message": str(e)}})
        except Exception as e:  # noqa: BLE001
            return self.send({"jsonrpc": "2.0", "id": key, "error": {"code": -32603, "message": type(e).__name__}})
        self.send({"jsonrpc": "2.0", "id": key, "result": result})

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            self.client = params.get("clientInfo") or {}
            self.capabilities = params.get("capabilities") or {}
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                "capabilities": {"tools": {}, "prompts": {}, "logging": {}},
                "serverInfo": {"name": NAME, "title": "Metaphi AI CodeVal", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping" or method == "logging/setLevel":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "prompts/list":
            return {"prompts": [{"name": "codeval", "title": "CodeVal", "description": ABOUT}]}
        if method == "prompts/get":
            if params.get("name") != "codeval":
                raise LookupError(f"no prompt {params.get('name')}")
            return {"description": ABOUT, "messages": [{"role": "user", "content": {"type": "text", "text": PROMPT}}]}
        if method == "tools/call":
            return self.call(params.get("name"), params.get("arguments") or {})
        raise LookupError(f"method not found: {method}")

    # tools

    def call(self, name: Any, args: dict[str, Any]) -> dict[str, Any]:
        tools = {"codeval_start": self.start, "codeval_status": self.status, "codeval_send": self.send_run}
        if name not in tools:
            return _result({"error": {"code": "unknown_tool", "message": f"no tool {name}"}}, True)
        try:
            return _result(tools[name](args))
        except config.Error as e:
            error = e if isinstance(e, errors.MetaphiError) else errors.MetaphiError(str(e))
            said = {k: v for k, v in error.to_dict().items() if v is not None and k != "type"}
            return _result({"error": said, **error.context}, True)
        except httpx.TransportError as e:
            return _result({"error": {"code": "network", "message": f"no connection ({type(e).__name__})"}}, True)

    def start(self, args: dict[str, Any]) -> dict[str, Any]:
        from metaphi import cli

        given = args.get("paths")
        if not isinstance(given, list) or not given or not all(isinstance(p, str) and p for p in given):
            raise errors.MetaphiError("paths: give one or more paths or git URLs", code="bad_arguments")
        build, test = args.get("build") or None, args.get("test") or None
        if build and not test:
            raise errors.MetaphiError("build runs only with test: give test too", code="build_without_test")
        got = cli.codeval_detached(_namespace(_paths(given), build, test, send=False), base())
        return {
            "run": got["run"],
            "state": "measuring",
            "already_running": not got["started"],
            "next": "Call codeval_status with this run and wait_s until it is ready. Nothing has been sent.",
        }

    def _wait_max(self) -> int:
        return WAIT_MAX_S.get(self.client.get("name") or "", WAIT_MAX_DEFAULT_S)

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        from metaphi import codeval_runs

        run = args.get("run")
        if not run:
            return {"runs": runs()}
        found = codeval_runs.located(run)
        if found is None:
            raise errors.MetaphiError(f"no CodeVal run {run} on this machine", code="run_not_found")
        wait = max(0, min(int(args.get("wait_s") or 0), self._wait_max()))
        return status(run, found[1], codeval_runs.wait(run, found[1], wait))

    def consented(self) -> bool:
        """The person's yes, from the client. Claude Code asks before every call of a tool marked
        requiresUserInteraction; elsewhere we ask in an elicitation form; with neither, nobody can say yes here."""
        if (self.client.get("name") or "") in HONOURS_CONSENT:
            return True
        held = self.capabilities.get("elicitation")
        if not isinstance(held, dict) or (held and "form" not in held):
            raise Refused(
                "this agent cannot ask you to approve a send; nothing was sent",
                code="consent_unavailable",
            )
        got = self.ask(
            "elicitation/create",
            {
                "mode": "form",
                "message": ASK,
                "requestedSchema": {
                    "type": "object",
                    "properties": {"send": {"type": "boolean", "title": "Send to Metaphi AI", "default": False}},
                    "required": ["send"],
                },
            },
        )
        return bool(got and got.get("action") == "accept" and (got.get("content") or {}).get("send") is True)

    def signed_in(self) -> None:
        from metaphi import codeval

        try:
            codeval.key_ready()
        except accounts.Error as e:
            if e.code == "no_api_key" and accounts.pickup() is not None:
                return codeval.key_ready()
            if e.code != "not_signed_in":
                raise
            got = accounts.login(say=self.log)
            self.log(f"Signed in as {got.get('email')}")
            codeval.key_ready()
        return None

    def send_run(self, args: dict[str, Any]) -> dict[str, Any]:
        from metaphi import codeval_runs

        run = args.get("run")
        found = codeval_runs.located(run) if isinstance(run, str) else None
        if found is None:
            raise errors.MetaphiError(f"no CodeVal run {run} on this machine", code="run_not_found")
        home = found[1]
        v = codeval_runs.view(run, home)
        if not v["local"]:
            if v["state"] in ("stopped", "failed"):
                return self._sent(run, home, v["asked"])  # the person said yes to this run already
            return status(run, home, v)
        if v["state"] == "running":
            raise errors.MetaphiError(
                "still measuring: show the person the preview first", code="not_ready", next_step="codeval_status"
            )
        if preview(home) is None:
            raise errors.MetaphiError("there is no preview to send: measure again with codeval_start", code="not_ready")
        try:
            agreed = self.consented()
        except Refused as e:
            asked = v["asked"] or {}
            words = ["kennet", "codeval", *(asked.get("paths") or [])]
            for flag in ("build", "test"):
                if asked.get(flag):
                    words += [f"--{flag}", asked[flag]]
            e.advice = f"to send, run this in your own terminal: cd {shlex.quote(str(base()))} && " + shlex.join(
                [*words, "--yes", "--detach"]
            )
            raise
        if not agreed:
            return {"run": run, "state": "ready", "sent": False, "next": "The person said no. Nothing was sent."}
        return self._sent(run, home, v["asked"])

    def _sent(self, run: str, home: Path, asked: dict[str, Any] | None) -> dict[str, Any]:
        from metaphi import cli

        if not asked or not asked.get("paths"):
            raise errors.MetaphiError("this run was not started here: measure with codeval_start", code="not_ready")
        self.signed_in()
        cli.codeval_detached(_namespace(asked["paths"], asked.get("build"), asked.get("test"), send=True), base(), run)
        return {"run": run, "state": "sending", "next": STATES["sending"]}


def runs() -> list[dict[str, Any]]:
    from metaphi import codeval_runs

    d = codeval_runs.index()
    out = []
    for f in sorted(d.glob("run_*.json")) if d.is_dir() else ():
        found = codeval_runs.located(f.stem)
        if found:
            said = status(*found)
            out.append({k: said.get(k) for k in ("run", "state", "phase", "done", "total") if k in said})
    return out


def _result(said: dict[str, Any], error: bool = False) -> dict[str, Any]:
    text = json.dumps(said, indent=1)
    return {"content": [{"type": "text", "text": text}], "structuredContent": said, "isError": error}


def serve(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """Read requests until stdin closes. Nothing but protocol goes to stdout: everything else said goes to stderr."""
    out = stdout or sys.stdout
    server = Server(out)
    with contextlib.redirect_stdout(sys.stderr):
        for line in stdin or sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                server.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            server.handle(message)
        for call in server.calls:  # stdin closed: answer what was asked before, briefly
            call.join(END_S)
    return 0


__all__ = ["ASK", "INSTRUCTIONS", "TOOLS", "Server", "preview", "serve", "status"]
