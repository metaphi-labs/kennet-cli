import base64
import gzip
import io
import json
import os
import shutil
import zipfile

import httpx
import pytest
import respx
from test_codebase_profile import API, _server, git, home  # noqa: F401  (home: the same key and settings)

from metaphi import cli, codebase_checks, codebase_harwell
from metaphi.steps import Steps

URL = codebase_harwell.URL
BUILT = {"members": 3, "clean": 2, "failed": 1, "mapsets_assembled": 1, "mapsets_failed": 0, "seconds": 12.6,
         "libraries": {"PAYROLL.PROGS": 3}}
COUNTED = {"members": 3, "clean": 2, "failed": 1, "mapsets_assembled": 1, "mapsets_failed": 0, "seconds": 13}
SENT = {"parts": [{"kind": "member", "total": 3, "clean": 2, "failed": 1},
                  {"kind": "mapset", "total": 1, "clean": 1, "failed": 0}], "seconds": 13}
PRINTED = "BUILD failed · 3 members, 2 clean, 1 failed\nPAYCALC: IGYPS2121-S \"WS-RATE\" was not defined"


@pytest.fixture(autouse=True)
def no_cobc(monkeypatch):
    """Nothing compiles here: no directory on PATH holds cobc."""
    path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                           if d and not os.path.exists(os.path.join(d, "cobc")))
    monkeypatch.setenv("PATH", path)
    assert shutil.which("cobc") is None


@pytest.fixture
def estate(tmp_path):
    r = tmp_path / "payroll"
    (r / "mainframe" / "cbl").mkdir(parents=True)
    (r / "mainframe" / "bms").mkdir()
    (r / "docs").mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "mainframe" / "harwell.toml").write_text('[codebase]\nname = "PAYROLL"\n')
    (r / "mainframe" / "cbl" / "PAYCALC.cbl").write_bytes(b"       IDENTIFICATION DIVISION.\r\n       PROGRAM-ID. PAYCALC.\r\n")
    (r / "mainframe" / "bms" / "PAYMAP.bms").write_text("PAYMAP DFHMSD TYPE=MAP\n")
    (r / "docs" / "README").write_text("not built\n")
    (r / "mainframe" / "link").symlink_to("cbl/PAYCALC.cbl")
    git(r, "add", ".")
    git(r, "commit", "-qm", "PAYROLL estate")
    return r


def _rpc(request):
    return json.loads(request.content)


def _harwell(*, tasks=True, call=None, polls=()):
    """Harwell's MCP: discover, then a `build` call that is a task (polled through `polls`) or answered at once."""
    polls = list(polls)

    def answer(request):
        body = _rpc(request)
        n, method = body["id"], body["method"]
        if method == "server/discover":
            ext = {"io.modelcontextprotocol/tasks": {}} if tasks else {}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": n, "result": {"capabilities": {"extensions": ext}}})
        if method == "tools/call":
            if call is not None:
                return call(n) if callable(call) else call
            if tasks:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": n, "result": {
                    "resultType": "task", "taskId": "tsk_1", "status": "working", "pollIntervalMs": 5000}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": n, "result": _result()})
        if method == "tasks/get":
            got = polls.pop(0) if polls else {"status": "completed", "result": _result()}
            if isinstance(got, httpx.Response):
                return got
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": n, "result": {"taskId": "tsk_1", **got}})
        raise AssertionError(method)
    return respx.post(URL).mock(side_effect=answer)


def _result(build=BUILT, text=PRINTED, error=True):
    return {"content": [{"type": "text", "text": text}], "isError": error,
            "structuredContent": {"status": "failed", "build": build} if build else None}


def _build(body=b"PK", **k):
    return codebase_harwell.build(body, "mai_sk_" + "a" * 43, sleep=lambda s: None, **k)


def test_estate():
    assert codebase_harwell.estate(["a.py", "x/harwell.toml", "harwell.toml", "y/z/harwell.toml"]) == ("", 3)
    assert codebase_harwell.estate(["b/harwell.toml", "a/harwell.toml"]) == ("a", 2)
    assert codebase_harwell.estate(["harwell.toml.bak", "src/myharwell.toml"]) is None


def test_zipped_is_the_estate_alone(estate):
    tree = codebase_checks._tree(estate, "HEAD")
    body = codebase_harwell.zipped(estate, tree, "mainframe")
    z = zipfile.ZipFile(io.BytesIO(body))
    assert sorted(z.namelist()) == ["bms/PAYMAP.bms", "cbl/PAYCALC.cbl", "harwell.toml"]
    assert z.read("cbl/PAYCALC.cbl").endswith(b"PAYCALC.\r\n")
    assert codebase_harwell.zipped(estate, tree, "mainframe") == body
    assert "docs/README" in zipfile.ZipFile(io.BytesIO(codebase_harwell.zipped(estate, tree, ""))).namelist()


@respx.mock
def test_build_as_a_task():
    route = _harwell(polls=[{"status": "working", "pollIntervalMs": 1000}])
    assert _build() == COUNTED
    sent = [_rpc(c.request) for c in route.calls]
    assert [s["method"] for s in sent] == ["server/discover", "tools/call", "tasks/get", "tasks/get"]
    assert sent[1]["params"]["arguments"] == {"codebase_zip_b64": base64.b64encode(b"PK").decode()}
    assert sent[1]["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] == {
        "extensions": {"io.modelcontextprotocol/tasks": {}}}
    assert sent[2]["params"]["taskId"] == "tsk_1"
    heads = [c.request.headers for c in route.calls]
    assert all(h["authorization"] == "Bearer mai_sk_" + "a" * 43 and h["mcp-protocol-version"] == "2026-07-28"
               for h in heads)
    assert [h.get("mcp-name") for h in heads] == [None, "build", "tsk_1", "tsk_1"]


@respx.mock
def test_build_at_once():
    route = _harwell(tasks=False)
    assert _build() == COUNTED
    assert _rpc(route.calls[1].request)["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] == {}


@respx.mock
@pytest.mark.parametrize("call, why", [
    (httpx.Response(401), "no_access"),
    (httpx.Response(403), "no_access"),
    (lambda n: httpx.Response(200, json={"jsonrpc": "2.0", "id": n, "result": _result(
        None, "build needs harwell:runs:write; this credential holds harwell:runs:read")}), "no_access"),
    (httpx.Response(413), "too_large"),
    (lambda n: httpx.Response(200, json={"jsonrpc": "2.0", "id": n, "result": _result(
        None, "task-args-too-large: 9000000 bytes")}), "too_large"),
    (lambda n: httpx.Response(200, json={"jsonrpc": "2.0", "id": n, "error": {"code": -32602, "message": "bad"}}),
     "refused"),
    (lambda n: httpx.Response(200, json={"jsonrpc": "2.0", "id": n, "result": _result(
        {**BUILT, "failed": -1})}), "refused"),
    (httpx.Response(502), "unreachable"),
    (httpx.Response(200, text="<html>"), "unreachable"),
])
def test_build_none(call, why):
    _harwell(call=call)
    assert _build() == {"why": why}


@respx.mock
def test_build_too_large_never_sent():
    route = _harwell()
    assert _build(b"x" * (codebase_harwell.ARGS_MOST * 3 // 4)) == {"why": "too_large"}
    assert not route.called


@respx.mock
def test_build_task_ends_without_counts():
    _harwell(polls=[{"status": "failed"}])
    assert _build() == {"why": "refused"}


@respx.mock
def test_build_polls_through_a_lost_answer():
    _harwell(polls=[httpx.Response(502)] * codebase_harwell.AGAIN)
    assert _build() == COUNTED


@respx.mock
def test_build_stops_on_lost_answers():
    route = _harwell(polls=[httpx.Response(502)] * (codebase_harwell.AGAIN + 1))
    assert _build() == {"why": "unreachable"}
    assert len(route.calls) == 2 + codebase_harwell.AGAIN + 1


@respx.mock
def test_build_waits_no_longer_than_its_hours():
    _harwell(polls=[{"status": "working"}] * 10)
    t = iter(range(0, 10 ** 6, 3600))
    assert _build(clock=lambda: next(t)) == {"why": "unreachable"}


@respx.mock
def test_build_sse_answer():
    def call(n):
        msg = json.dumps({"jsonrpc": "2.0", "id": n, "result": _result()})
        return httpx.Response(200, text=f"event: message\ndata: {msg}\n\n",
                              headers={"content-type": "text/event-stream"})
    _harwell(call=call)
    assert _build() == COUNTED


@respx.mock
def test_checked(estate):
    _harwell()
    said, steps = [], Steps()
    got = codebase_harwell.checked(estate, "HEAD", "payroll", "mai_sk_" + "a" * 43, said.append, steps)
    assert got == {"codebases": 1, **SENT}
    assert "build_remote" in steps.total
    assert said[-1] == "payroll: Harwell build: 3 members, 2 clean, 1 failed; 1 mapsets assembled, 0 failed; 13s"


def test_checked_not_an_estate(estate, monkeypatch):
    (estate / "mainframe" / "harwell.toml").unlink()
    git(estate, "commit", "-qam", "not an estate")
    monkeypatch.setattr(codebase_harwell, "build", lambda *a, **k: pytest.fail("sent"))
    assert codebase_harwell.checked(estate, "HEAD", "payroll", "k", lambda s: None) is None


@pytest.fixture
def no_model(monkeypatch):
    """Setup finds no build here, and its model is never asked: what is left is Harwell's part."""
    from metaphi import codebase_setup
    monkeypatch.setattr(codebase_setup, "engine", lambda: None)
    monkeypatch.setattr(codebase_setup, "find", lambda *a, **k: (None, {
        "by": "model", "worked": False, "steps": 0, "seconds": 0, "tokens": 0, "container": False,
        "why": "gave_up_toolchain", "report": None, "tries": []}))


def _profile(estate, *flags):
    ref = "cbp_" + "6" * 16
    got = _server(ref, picks=((),))
    got["setup"].mock(return_value=httpx.Response(200, json={
        "base_url": "https://gw.test/anthropic", "token": "kgw1.t", "model": "claude-sonnet-5-5"}))
    assert cli.main(["profile-codebase", str(estate), *flags]) in (0, None)
    return json.loads(gzip.decompress(got["checks"].calls[-1].request.content))["repos"][0]


@respx.mock
def test_profile_deep_sends_the_counts(estate, capsys, no_model):
    route = _harwell()
    sent = _profile(estate, "--deep")
    assert route.called
    assert sent["build_remote"] == {"codebases": 1, **SENT}
    assert "build_remote" in sent["seconds"]
    text = json.dumps(sent)
    for word in ("PAYROLL", "PAYCALC", "IGYPS2121", "WS-RATE", "BUILD failed", "libraries"):
        assert word not in text, word
    assert "Harwell build: 3 members" in capsys.readouterr().out


@respx.mock
def test_profile_no_access_is_sent_as_why(estate, capsys, no_model):
    _harwell(call=httpx.Response(401))
    sent = _profile(estate, "--deep")
    assert sent["build_remote"] == {"codebases": 1, "why": "no_access"}
    assert "harwell:runs:write" in capsys.readouterr().out


@respx.mock
def test_profile_default_stays_here(estate, no_model):
    route = _harwell()
    sent = _profile(estate)
    assert not route.called and "build_remote" not in sent
    assert all(not str(c.request.url).startswith(URL) for c in respx.calls)
    assert any(str(c.request.url).startswith(API) for c in respx.calls)
