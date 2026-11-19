import gzip
import io
import json
import os
import subprocess
import sys
import threading
import time

import httpx
import pytest
import respx

from metaphi import accounts, cli, codeval, codeval_mcp, codeval_runs, config

API = config.DEFAULT_API
PYTEST = f"PYTHONPATH=. {sys.executable} -m pytest -q -p no:cacheprovider --junitxml=$KENNET_JUNIT/r.xml tests"


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "Sam Seller",
            "GIT_AUTHOR_EMAIL": "seller@acme.test",
            "GIT_COMMITTER_NAME": "Sam Seller",
            "GIT_COMMITTER_EMAIL": "seller@acme.test",
        },
    )


@pytest.fixture
def tested(tmp_path):
    r = tmp_path / "falcon_calc"
    (r / "tests").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    for n, body in enumerate(("return a + b", "return sum((a, b))", "return b + a"), 1):
        (r / "calc.py").write_text(f"def add(a, b):\n    {body}\n")
        (r / "tests/test_calc.py").write_text(
            f"from calc import *\ndef test_add():\n    assert add(1, {n}) == {n + 1}\n"
        )
        git(r, "add", ".")
        git(r, "commit", "-qm", f"change {n}")
    return r


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_" + "a" * 43)
    for var in ("METAPHI_ACCOUNTS", "METAPHI_API", "METAPHI_ALLOW_CUSTOM_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


class Workers:
    """Stands in for the detached process: records each start, and runs it here, under respx, on request."""

    def __init__(self, monkeypatch):
        self.started: list[list[str]] = []
        monkeypatch.setattr(codeval_runs, "_start", self)
        monkeypatch.setattr(codeval_runs, "awake", lambda pid: None)

    def __call__(self, command, **kw):
        if kw.get("start_new_session"):
            self.started.append([*command[len(codeval_runs._self()) :]])
            self.cwd = kw["cwd"]
        self.pid = os.getpid()
        return self

    def run(self, monkeypatch):
        with monkeypatch.context() as m:
            m.chdir(self.cwd)
            return cli.main(self.started[-1])


def _server(ref, commits=(2,)):
    respx.get(f"{API}/codeval/access").mock(return_value=httpx.Response(204))
    return {
        "profile": respx.post(f"{API}/codeval").mock(return_value=httpx.Response(200, json={"id": ref})),
        "plan": respx.post(f"{API}/codeval/{ref}/plan").mock(
            return_value=httpx.Response(
                200, json={"runs": 1, "repos": [list(commits)], "run_minutes": 5, "hours": 1, "blobs": [[]]}
            )
        ),
        "checks": respx.post(f"{API}/codeval/{ref}/checks").mock(return_value=httpx.Response(200, json={"id": ref})),
        "logs": respx.post(f"{API}/codeval/{ref}/logs").mock(return_value=httpx.Response(200, json={"id": ref})),
        "progress": respx.post(f"{API}/codeval/{ref}/progress").mock(return_value=httpx.Response(204)),
        "read": respx.get(f"{API}/codeval/{ref}").mock(return_value=httpx.Response(200, json={"accepted_at": None})),
    }


def _server_for(client="claude-code", capabilities=None):
    s = codeval_mcp.Server(io.StringIO())
    s.dispatch("initialize", {"clientInfo": {"name": client}, "capabilities": capabilities or {}})
    return s


def _call(s, name, **args):
    got = s.dispatch("tools/call", {"name": name, "arguments": args})
    return got["structuredContent"], got["isError"]


def test_three_tools_with_schemas_and_send_marked_for_the_persons_yes():
    s = codeval_mcp.Server(io.StringIO())
    init = s.dispatch("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {}})
    assert init["protocolVersion"] == "2025-11-25" and set(init["capabilities"]) >= {"tools", "prompts"}
    assert len(init["instructions"]) < 4096 and init["instructions"].startswith(codeval_mcp.ABOUT)
    assert s.dispatch("initialize", {"protocolVersion": "1999-01-01"})["protocolVersion"] == "2025-11-25"
    tools = {t["name"]: t for t in s.dispatch("tools/list", {})["tools"]}
    assert sorted(tools) == ["codeval_send", "codeval_start", "codeval_status"]
    assert tools["codeval_start"]["inputSchema"]["required"] == ["paths"]
    assert set(tools["codeval_start"]["inputSchema"]["properties"]) == {"paths", "build", "test"}
    assert set(tools["codeval_status"]["inputSchema"]["properties"]) == {"run", "wait_s"}
    assert tools["codeval_status"]["annotations"]["readOnlyHint"] is True
    assert tools["codeval_send"]["inputSchema"]["required"] == ["run"]
    assert tools["codeval_send"]["_meta"] == {"anthropic/requiresUserInteraction": True}
    assert tools["codeval_send"]["annotations"]["destructiveHint"] is True
    assert all("_meta" not in tools[n] for n in ("codeval_start", "codeval_status"))
    assert "deep" not in json.dumps(tools)
    prompts = s.dispatch("prompts/list", {})["prompts"]
    assert [p["name"] for p in prompts] == ["codeval"]
    said = s.dispatch("prompts/get", {"name": "codeval"})["messages"][0]
    assert said["role"] == "user" and "Metaphi AI" in said["content"]["text"]


def test_the_stdio_loop_answers_each_request_and_writes_nothing_else_to_stdout():
    out = io.StringIO()
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "codeval_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
    ]
    codeval_mcp.serve(io.StringIO("\n".join(json.dumps(x) for x in lines) + "\nnot json\n"), out)
    said = {m.get("id"): m for m in map(json.loads, out.getvalue().splitlines())}
    assert said[2]["result"]["structuredContent"] == {"runs": []}
    assert said[3]["error"]["code"] == -32601 and said[None]["error"]["code"] == -32700
    assert len(said) == 4


@respx.mock
def test_start_status_preview_send_sends_exactly_what_was_shown(tested, tmp_path, monkeypatch):
    workers, post = Workers(monkeypatch), codeval.post
    monkeypatch.setattr(codeval, "post", lambda *a: pytest.fail("sent before the yes"))
    s = _server_for()
    started, failed = _call(s, "codeval_start", paths=[str(tested)], test=PYTEST)
    assert not failed and started["state"] == "measuring"
    run = started["run"]
    assert workers.started[-1] == ["codeval", str(tested), "--test", PYTEST, "--dry-run", "--worker", run, "--json"]
    assert workers.cwd == tmp_path / "cfg"
    assert _call(s, "codeval_status", run=run)[0]["state"] == "measuring"
    refused, failed = _call(s, "codeval_send", run=run)
    assert failed and refused["error"]["code"] == "not_ready"

    assert workers.run(monkeypatch) in (0, None)
    said, _ = _call(s, "codeval_status", run=run, wait_s=1)
    assert said["state"] == "ready" and "needs" not in said
    assert [f["file"] for f in said["preview"]["files"]] == ["codeval.json.gz", "checks.json.gz", "logs.tar.gz"]
    assert said["preview"]["folder"] == str(tmp_path / "cfg" / "kennet-codeval" / run / "sent")
    assert said["latest_commit"] == ["falcon_calc: no build step \u00b7 1 of 1 tests passed, exit 0"]
    shown = (tmp_path / "cfg" / "kennet-codeval" / run / "sent" / "codeval.json.gz").read_bytes()
    listed = [{"run": run, "state": "ready", "phase": "done", "done": 0, "total": 0}]
    assert _call(s, "codeval_status")[0]["runs"] == listed

    monkeypatch.setattr(codeval, "post", post)
    ref = "cv_" + "5" * 16
    got = _server(ref)
    sent, failed = _call(s, "codeval_send", run=run)
    assert not failed and sent["state"] == "sending" and not got["profile"].called
    assert workers.started[-1] == ["codeval", str(tested), "--test", PYTEST, "--yes", "--worker", run, "--json"]
    assert workers.run(monkeypatch) in (0, None)
    assert got["profile"].calls[-1].request.content == shown
    assert got["plan"].call_count == 1 and got["logs"].called
    checks = json.loads(gzip.decompress(got["checks"].calls[-1].request.content))["repos"][0]
    assert [c["commit"] for c in checks["commits"]] == [2] and checks["tip"]["tests"]
    said, _ = _call(s, "codeval_status", run=run)
    assert said["state"] == "sent" and said["reference"] == ref and "reach out" in said["next"]
    got["read"].mock(return_value=httpx.Response(200, json={"accepted_at": "2026-11-19T12:00:00Z"}))
    said, _ = _call(s, "codeval_status", run=run)
    assert (said["state"], said["accepted_at"]) == ("accepted", "2026-11-19T12:00:00Z")


@respx.mock
def test_the_latest_commit_measured_before_the_yes_is_not_run_again(tested, tmp_path, monkeypatch, capsys):
    workers = Workers(monkeypatch)
    s = _server_for()
    run = _call(s, "codeval_start", paths=[str(tested)], test="true")[0]["run"]
    workers.run(monkeypatch)
    _server("cv_" + "6" * 16)
    _call(s, "codeval_send", run=run)
    capsys.readouterr()
    workers.run(monkeypatch)
    assert "the latest commit was run before" in capsys.readouterr().err


def test_a_client_that_cannot_ask_the_person_never_sends(tested, tmp_path, monkeypatch):
    workers = Workers(monkeypatch)
    s = _server_for("some-agent")
    run = _call(s, "codeval_start", paths=[str(tested)], test="true")[0]["run"]
    workers.run(monkeypatch)
    monkeypatch.setattr(codeval, "key_ready", lambda: pytest.fail("signed in"))
    said, failed = _call(s, "codeval_send", run=run)
    assert failed and said["error"]["code"] == "consent_unavailable" and len(workers.started) == 1
    assert said["error"]["advice"].endswith(f"kennet codeval {tested} --test true --yes --detach")
    s = _server_for("url-only", {"elicitation": {"url": {}}})
    assert _call(s, "codeval_send", run=run)[0]["error"]["code"] == "consent_unavailable"


@pytest.mark.parametrize(
    ("answer", "sends"),
    [
        ({"action": "accept", "content": {"send": True}}, True),
        ({"action": "accept", "content": {"send": False}}, False),
        ({"action": "decline"}, False),
        ({"action": "cancel"}, False),
        (None, False),
    ],
)
def test_elsewhere_send_asks_in_a_form_first(tested, monkeypatch, answer, sends):
    workers = Workers(monkeypatch)
    s = _server_for("other-agent", {"elicitation": {}})
    run = _call(s, "codeval_start", paths=[str(tested)], test="true")[0]["run"]
    workers.run(monkeypatch)
    asked = []
    monkeypatch.setattr(s, "ask", lambda method, params: asked.append((method, params)) or answer)
    monkeypatch.setattr(codeval, "key_ready", lambda: None)
    said, failed = _call(s, "codeval_send", run=run)
    [(method, params)] = asked
    assert method == "elicitation/create" and params["mode"] == "form" and params["message"] == codeval_mcp.ASK
    assert params["requestedSchema"]["properties"]["send"]["type"] == "boolean"
    assert not failed and (said["state"] == "sending") is sends and (len(workers.started) == 2) is sends


def test_an_elicitation_goes_to_the_client_and_its_answer_comes_back(monkeypatch):
    class Pipe(io.StringIO):
        def __init__(self, server_box):
            super().__init__()
            self.box = server_box

        def write(self, line):
            m = json.loads(line)
            if m.get("method") == "elicitation/create":
                answer = {"jsonrpc": "2.0", "id": m["id"], "result": {"action": "accept", "content": {"send": True}}}
                threading.Timer(0.05, self.box[0].handle, (answer,)).start()
            return len(line)

    box: list = []
    s = codeval_mcp.Server(Pipe(box))
    box.append(s)
    began = time.monotonic()
    assert s.ask("elicitation/create", {"message": "?"}, timeout=5) == {"action": "accept", "content": {"send": True}}
    assert time.monotonic() - began < 4 and not s.waiting


def test_send_signs_in_in_the_browser_when_not_signed_in(tested, monkeypatch):
    workers = Workers(monkeypatch)
    s = _server_for()
    run = _call(s, "codeval_start", paths=[str(tested)], test="true")[0]["run"]
    workers.run(monkeypatch)
    monkeypatch.delenv("METAPHI_API_KEY")
    signed: list = []

    def login(say, **kw):
        signed.append(say)
        monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_" + "b" * 43)
        return {"email": "seller@acme.test"}

    monkeypatch.setattr(accounts, "login", login)
    monkeypatch.setattr(codeval, "key_ready", lambda: None if signed else codeval.api.key(None, False))
    said, failed = _call(s, "codeval_send", run=run)
    assert not failed and said["state"] == "sending" and len(signed) == 1 and len(workers.started) == 2


def test_no_build_or_test_found_asks_the_person_before_anything_is_sent(tmp_path, monkeypatch):
    from metaphi import codebase_setup

    r = tmp_path / "bare"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "notes.txt").write_text("x\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "one")
    monkeypatch.setattr(codebase_setup, "engine", lambda: None)
    monkeypatch.setattr(codebase_setup, "find", lambda *a, **k: pytest.fail("model"))
    monkeypatch.setattr(codeval, "post", lambda *a: pytest.fail("sent"))
    workers = Workers(monkeypatch)
    s = _server_for()
    run = _call(s, "codeval_start", paths=[str(r)])[0]["run"]
    assert workers.run(monkeypatch) == cli.NEEDS_PERSON
    said, _ = _call(s, "codeval_status", run=run)
    assert said["state"] == "needs_person" and said["preview"]["files"]
    assert said["needs"]["question"].startswith(f"CodeVal could not build and test {r}.")
    assert [c["label"] for c in said["needs"]["choices"]] == [
        "Give the build and test commands",
        "Send without build and test results",
    ]


def test_send_refuses_a_run_whose_repositories_moved_on(tested, monkeypatch):
    workers = Workers(monkeypatch)
    s = _server_for()
    run = _call(s, "codeval_start", paths=[str(tested)], test="true")[0]["run"]
    workers.run(monkeypatch)
    (tested / "calc.py").write_text("x = 1\n")
    git(tested, "commit", "-qam", "later")
    monkeypatch.setattr(codeval, "key_ready", lambda: None)
    said, failed = _call(s, "codeval_send", run=run)
    assert failed and said["error"]["code"] == "run_changed" and len(workers.started) == 1


def test_status_waits_no_longer_than_the_client_allows(tmp_path, monkeypatch):
    home = tmp_path / "cfg" / "kennet-codeval" / "run_0123456789abcdef"
    codeval_runs.Run(home, home.name)
    codeval_runs.register(home.name, home)
    waited = []
    monkeypatch.setattr(
        codeval_runs, "wait", lambda run, h, seconds: waited.append(seconds) or codeval_runs.view(run, h)
    )
    _call(_server_for("other"), "codeval_status", run=home.name, wait_s=240)
    _call(_server_for("claude-code"), "codeval_status", run=home.name, wait_s=240)
    assert waited == [codeval_mcp.WAIT_MAX_DEFAULT_S, 240]


def test_seconds_left_come_from_the_pace_of_finished_commits():
    s = {"phase": "checks", "checks_at": 1000.0, "checks_from": 2, "done": 4, "total": 10}
    assert codeval_runs.left(s, "running", now=1100.0) == 300
    assert codeval_runs.left({**s, "done": 2}, "running", now=1100.0) is None
    assert codeval_runs.left(s, "done", now=1100.0) is None
