"""`kennet codeval <paths>`, the one command, end to end against a stub service."""

import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
import threading

import httpx
import pytest
import respx
from test_codeval import _home, git

from metaphi import cli, codebase_checks, codebase_setup, codeval, codeval_runs, config
from metaphi.cli import _size

API = config.DEFAULT_API
REF = "cv_" + "5" * 16
SEED = "ab" * 32
AWAKE = codeval_runs.awake


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_" + "a" * 43)
    for var in ("METAPHI_ACCOUNTS", "METAPHI_API", "METAPHI_ALLOW_CUSTOM_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(codebase_setup, "engine", lambda: None)
    monkeypatch.setattr(codeval_runs, "awake", lambda pid: None)
    monkeypatch.setattr(cli, "_interactive", lambda: False)


@pytest.fixture
def made(tmp_path):
    r = tmp_path / "falcon_made"
    (r / "tests").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    (r / "Makefile").write_text(
        f"test:\n\tPYTHONPATH=. {sys.executable} -m pytest -q -p no:cacheprovider "
        "--junitxml=$$KENNET_JUNIT/r.xml tests\n"
    )
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "tests/test_calc.py").write_text("from calc import *\ndef test_add():\n    assert add(1, 2) == 3\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "add")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\ndef mul(a, b):\n    return a * b\n")
    (r / "tests/test_calc.py").write_text(
        "from calc import *\ndef test_add():\n    assert add(1, 2) == 3\ndef test_mul():\n    assert mul(2, 3) == 6\n"
    )
    git(r, "add", ".")
    git(r, "commit", "-qm", "mul")
    (r / "calc.py").write_text("def add(a, b):\n    return sum((a, b))\ndef mul(a, b):\n    return a * b\n")
    git(r, "commit", "-qam", "refactor")
    return r


def _server(picked=(2,), checks=200, logs=200):
    """The service: /picks names commits by their place in the history it was sent (1 is the oldest)."""

    def picks(request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "codeval_picks",
                "pick_id": "cvp_" + "1" * 16,
                "repos": [
                    {
                        "fingerprint": codeval.fingerprint(r["commits"][0]["id"]),
                        "seed": SEED,
                        "commits": [r["commits"][i - 1]["id"] for i in picked],
                    }
                    for r in body["repos"]
                ],
            },
        )

    return {
        "access": respx.get(f"{API}/codeval/access").mock(return_value=httpx.Response(204)),
        "picks": respx.post(f"{API}/codeval/picks").mock(side_effect=picks),
        "profile": respx.post(f"{API}/codeval").mock(return_value=httpx.Response(200, json={"id": REF})),
        "checks": respx.post(f"{API}/codeval/{REF}/checks").mock(return_value=httpx.Response(checks, json={"id": REF})),
        "logs": respx.post(f"{API}/codeval/{REF}/logs").mock(return_value=httpx.Response(logs, json={"id": REF})),
        "part": respx.post(url__regex=rf"{API}/codeval/{REF}/logs/parts/\d+$").mock(
            return_value=httpx.Response(200, json={"object": "codeval_logs_part"})
        ),
        "complete": respx.post(f"{API}/codeval/{REF}/logs/complete").mock(
            return_value=httpx.Response(200, json={"object": "codeval_logs"})
        ),
    }


def _left(got) -> list[str]:
    """Every request that carried anything off the machine."""
    return [c.request.url.path for c in respx.calls if c.request.method != "GET"]


@respx.mock
def test_an_agent_runs_it_and_only_commit_ids_and_times_leave_before_the_owner_sends(made, capsys, tmp_path):
    got = _server()
    assert cli.main(["codeval", str(made), "--json"]) == cli.NEEDS_PERSON
    out = json.loads(capsys.readouterr().out)
    assert _left(got) == ["/v1/kennet/codeval/picks"]
    sent = json.loads(got["picks"].calls[0].request.content)
    assert set(sent) == {"tool_version", "repos"} and len(sent["repos"][0]["commits"]) == 3
    assert all({"id", "at"} <= set(c) for c in sent["repos"][0]["commits"])
    for word in ("falcon", "calc", "Sam Seller", "seller@acme.test", "refactor"):
        assert word not in json.dumps(sent), word
    assert out["state"] == "measured" and out["next_step"] == (
        f"the owner runs `kennet codeval {made}` in a terminal to review and send"
    )
    payload = _home(tmp_path) / codeval.PAYLOAD
    assert sorted(p.name for p in payload.iterdir()) == [
        "checks.json.gz",
        "logs.tar.gz",
        "manifest.json",
        "profile.json.gz",
    ]
    checks = json.loads(gzip.decompress((payload / "checks.json.gz").read_bytes()))
    repo = checks["repos"][0]
    assert checks["schema"] == codeval.CHECKS_SCHEMA and repo["tip"]["tests"][0]["passed"] == 2
    assert [(c["commit"], len(c["id"])) for c in repo["commits"]] == [(2, 40)]
    assert repo["commands"]["test"] == "make test" and repo["toolchain"]
    assert set(repo["blobs"]) <= {
        str(f["file"])
        for f in json.loads(gzip.decompress((payload / "profile.json.gz").read_bytes()))["repos"][0]["files"]
    }
    profile = json.loads(gzip.decompress((payload / "profile.json.gz").read_bytes()))
    assert profile["pick_id"] == "cvp_" + "1" * 16 and profile["bundle_sha256"] == out["payload"]["bundle_sha256"]

    assert cli.main(["codeval", str(made), "--json"]) == cli.NEEDS_PERSON
    capsys.readouterr()
    assert _left(got) == ["/v1/kennet/codeval/picks"], "a measured run is not measured again"


@respx.mock
def test_at_a_terminal_the_owner_sees_exactly_what_is_sent_and_yes_sends_those_bytes(
    made, capsys, tmp_path, monkeypatch
):
    got = _server()
    assert cli.main(["codeval", str(made)]) == cli.NEEDS_PERSON
    capsys.readouterr()
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda q: asked.append(q) or "n")
    assert cli.main(["codeval", str(made)]) == 0
    out = capsys.readouterr().out
    assert asked == [cli.SEND_ASK_UNMODELLED] and "Nothing sent" in out and _left(got) == ["/v1/kennet/codeval/picks"]
    manifest = json.loads((_home(tmp_path) / codeval.PAYLOAD / "manifest.json").read_text())
    assert f"Bundle sha256: {manifest['bundle_sha256']}" in out and "Commands that ran:" in out
    assert "  falcon_made: test: make test\n" in out and "de-identified:" in out

    monkeypatch.setattr("builtins.input", lambda q: "y")
    assert cli.main(["codeval", str(made)]) == 0
    out = capsys.readouterr().out
    assert "the model pass that finds people's names did not run" in out and "Nothing sent" in out
    assert _left(got) == ["/v1/kennet/codeval/picks"], "with no model pass, y sends nothing"

    monkeypatch.setattr("builtins.input", lambda q: cli.SEND_UNMODELLED)
    assert cli.main(["codeval", str(made)]) == 0
    assert capsys.readouterr().out.rstrip().endswith(cli.SENT)
    payload = _home(tmp_path) / codeval.PAYLOAD
    for name, route in (("profile.json.gz", "profile"), ("checks.json.gz", "checks"), ("logs.tar.gz", "logs")):
        body = got[route].calls[-1].request.content
        assert body == (payload / name).read_bytes()
        assert hashlib.sha256(body).hexdigest()[:16] in out
    assert _left(got)[1:] == [
        "/v1/kennet/codeval",
        f"/v1/kennet/codeval/{REF}/checks",
        f"/v1/kennet/codeval/{REF}/logs",
    ]
    assert not got["part"].called, "logs within one part go as one body"
    with tarfile.open(fileobj=io.BytesIO(got["logs"].calls[-1].request.content), mode="r:gz") as tar:
        last = tar.getmembers()[-1]
        assert last.name == codeval.LOGS_DEID and set(json.load(tar.extractfile(last))) == {
            "logs",
            "checks",
            "model_pass",
            "unreadable",
        }
    profile = json.loads(gzip.decompress(got["profile"].calls[-1].request.content))
    assert [r["name"] for r in profile["repos"]] == ["[repo:0]"] and "falcon" not in json.dumps(profile)

    assert cli.main(["codeval", str(made)]) == 0
    assert "Already sent, on " in capsys.readouterr().out and got["profile"].call_count == 1


@respx.mock
def test_a_send_cut_short_goes_on_where_it_stopped(made, capsys, monkeypatch):
    got = _server(checks=503)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda q: cli.SEND_UNMODELLED)
    monkeypatch.setattr(codeval.time, "sleep", lambda s: None)
    assert cli.main(["codeval", str(made)]) != 0
    assert got["profile"].call_count == 1 and not got["logs"].called
    got["checks"].mock(return_value=httpx.Response(200, json={"id": REF}))
    monkeypatch.setattr("builtins.input", lambda q: pytest.fail("asked again for bytes already said yes to"))
    assert cli.main(["codeval", str(made)]) == 0
    assert "Going on with the send already said yes to" in capsys.readouterr().out
    assert got["profile"].call_count == 1 and got["logs"].call_count == 1 and got["picks"].call_count == 1


def _measured(made, tmp_path, monkeypatch, capsys, parts: int) -> bytes:
    """Measure, then make the part size small enough that the logs go in `parts` parts."""
    assert cli.main(["codeval", str(made)]) == cli.NEEDS_PERSON
    capsys.readouterr()
    logs = (_home(tmp_path) / codeval.PAYLOAD / codeval.LOGS).read_bytes()
    monkeypatch.setattr(codeval, "LOGS_PART", -(-len(logs) // parts))
    assert -(-len(logs) // codeval.LOGS_PART) == parts
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda q: cli.SEND_UNMODELLED)
    return logs


def _parts_sent(got) -> dict[int, bytes]:
    return {int(c.request.url.path.rsplit("/", 1)[1]): c.request.content for c in got["part"].calls}


@respx.mock
def test_logs_past_one_part_go_in_parts_then_are_joined(made, capsys, tmp_path, monkeypatch):
    got = _server()
    logs = _measured(made, tmp_path, monkeypatch, capsys, parts=3)
    assert cli.main(["codeval", str(made)]) == 0
    sent = _parts_sent(got)
    assert sorted(sent) == [0, 1, 2] and b"".join(sent[n] for n in range(3)) == logs
    assert all(len(sent[n]) == codeval.LOGS_PART for n in (0, 1)), "every part but the last is LOGS_PART bytes"
    assert json.loads(got["complete"].calls[-1].request.content) == {
        "parts": [hashlib.sha256(sent[n]).hexdigest() for n in range(3)]
    }
    assert not got["logs"].called, "one run's logs go one way, never both"
    assert _left(got)[-1] == f"/v1/kennet/codeval/{REF}/logs/complete"
    assert json.loads((_home(tmp_path) / "sent.json").read_text())["files"][-1] == codeval.LOGS


@respx.mock
def test_parts_go_several_at_once_and_the_terminal_shows_bytes_sent(made, capsys, tmp_path, monkeypatch):
    got = _server()
    _measured(made, tmp_path, monkeypatch, capsys, parts=3)
    together = threading.Barrier(3, timeout=10)

    def part(request):
        together.wait()
        return httpx.Response(200, json={"object": "codeval_logs_part"})

    got["part"].side_effect = part
    assert cli.main(["codeval", str(made)]) == 0, "the three parts were in flight at once"
    size = _size((_home(tmp_path) / codeval.PAYLOAD / codeval.LOGS).stat().st_size)
    assert f"Sending the logs: {size} of {size}" in capsys.readouterr().out


@respx.mock
def test_a_part_the_service_did_not_receive_is_sent_again_and_only_it(made, capsys, tmp_path, monkeypatch):
    got = _server()
    _measured(made, tmp_path, monkeypatch, capsys, parts=3)
    missing = {
        "error": {
            "type": "invalid_request_error",
            "code": "bad_parameter",
            "param": "parts",
            "message": "parts: parts 1 were not received: send them, then complete",
        }
    }
    got["complete"].side_effect = [
        httpx.Response(400, json=missing),
        httpx.Response(200, json={"object": "codeval_logs"}),
    ]
    assert cli.main(["codeval", str(made)]) == 0
    sent = [int(c.request.url.path.rsplit("/", 1)[1]) for c in got["part"].calls]
    assert sorted(sent[:3]) == [0, 1, 2] and sent[3:] == [1] and got["complete"].call_count == 2


@respx.mock
def test_a_parts_send_cut_short_goes_on_from_the_part_it_stopped_at(made, capsys, tmp_path, monkeypatch):
    got = _server()
    _measured(made, tmp_path, monkeypatch, capsys, parts=3)
    monkeypatch.setattr(codeval.time, "sleep", lambda s: None)
    got["part"].side_effect = lambda request: httpx.Response(
        503 if request.url.path.endswith("/2") else 200, json={"object": "codeval_logs_part"}
    )
    assert cli.main(["codeval", str(made)]) != 0
    capsys.readouterr()
    got["part"].side_effect = None
    monkeypatch.setattr("builtins.input", lambda q: pytest.fail("asked again for bytes already said yes to"))
    before = got["part"].call_count
    assert cli.main(["codeval", str(made)]) == 0
    assert [int(c.request.url.path.rsplit("/", 1)[1]) for c in got["part"].calls[before:]] == [2]
    assert got["profile"].call_count == 1 and got["complete"].call_count == 1


@respx.mock
def test_logs_past_two_gib_are_refused_here_and_nothing_is_sent(made, capsys, monkeypatch):
    got = _server()
    monkeypatch.setattr(codeval, "MAX_LOGS", 100)
    assert cli.main(["codeval", str(made), "--json"]) == cli.FAILED
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "logs_too_large" and "2 GiB" in err["message"] and "fewer repositories" in err["next_step"]
    assert _left(got) == ["/v1/kennet/codeval/picks"]


@respx.mock
def test_no_build_found_stops_with_the_command_to_give(tmp_path, capsys):
    r = tmp_path / "falcon_bare"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "notes.txt").write_text("x\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "x")
    got = _server(picked=())
    assert cli.main(["codeval", str(r), "--json"]) == cli.FAILED
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "test_command_missing" and f"kennet codeval {r} -- '" in err["next_step"]
    assert _left(got) == ["/v1/kennet/codeval/picks"]


@respx.mock
def test_a_command_after_double_dash_is_the_test_and_another_starts_afresh(made, capsys, tmp_path, monkeypatch):
    got = _server(picked=(2, 3))
    real, made_runs = codebase_checks._commit, []

    def commit(*a, **k):
        made_runs.append(a[6])
        if len(made_runs) == 2:
            raise KeyboardInterrupt
        return real(*a, **k)

    monkeypatch.setattr(codebase_checks, "_commit", commit)
    assert cli.main(["codeval", str(made), "--json", "--", "make", "test"]) == 130
    capsys.readouterr()
    journal = (_home(tmp_path) / codeval_runs.JOURNAL).read_text()
    assert journal.count('"kind":"commit"') == 1 and '"kind":"tip"' in journal

    made_runs.clear()
    monkeypatch.setattr(codebase_checks, "_commit", lambda *a, **k: made_runs.append(a[6]) or real(*a, **k))
    assert cli.main(["codeval", str(made), "--json", "--", "make", "test"]) == cli.NEEDS_PERSON
    out = json.loads(capsys.readouterr().out)
    assert len(made_runs) == 1 and got["picks"].call_count == 1
    assert out["next_step"] == f"the owner runs `kennet codeval {made} -- 'make test'` in a terminal to review and send"
    checks = json.loads(gzip.decompress((_home(tmp_path) / codeval.PAYLOAD / "checks.json.gz").read_bytes()))
    assert checks["repos"][0]["test_given"] and [c["commit"] for c in checks["repos"][0]["commits"]] == [2, 3]

    shutil.rmtree(_home(tmp_path) / codeval.PAYLOAD)
    made_runs.clear()
    assert cli.main(["codeval", str(made), "--json", "--", "true"]) == cli.NEEDS_PERSON
    capsys.readouterr()
    assert len(made_runs) == 2 and got["picks"].call_count == 2


@respx.mock
def test_not_signed_in_at_a_terminal_it_waits_on_the_printed_code_and_goes_on(made, capsys, monkeypatch):
    monkeypatch.delenv("METAPHI_API_KEY")
    _server()
    asked = []

    def login(say=print, device=None, **kw):
        asked.append(device)
        say("To sign in, open https://accounts.metaphi.ai/device and enter the code ABCD-EFGH")
        monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_" + "b" * 43)
        return {"email": "owner@example.com"}

    monkeypatch.setattr(cli.accounts, "login", login)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda q: "n")
    assert cli.main(["codeval", str(made)]) == 0
    out = capsys.readouterr().out
    assert asked == [True] and "enter the code ABCD-EFGH" in out and "Signed in as owner@example.com" in out


@pytest.mark.parametrize(
    "gone",
    [["--yes"], ["--dry-run"], ["--detach"], ["--deep"], ["--build", "make"], ["--test", "make"], ["--wait", "5"]],
)
def test_removed_flags_are_unknown(gone, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["codeval", ".", *gone])
    assert e.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("gone", ["send", "status"])
def test_removed_subcommands_are_unknown(gone, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["codeval", gone])
    assert e.value.code == 2 and f"`kennet codeval {gone}` is gone" in capsys.readouterr().err


def test_linux_keeps_awake_with_systemd_inhibit_when_there(monkeypatch):
    runs = codeval_runs
    calls: list = []
    monkeypatch.setattr(runs.subprocess, "Popen", lambda command, **kw: calls.append((command, kw)))
    monkeypatch.setattr(runs.sys, "platform", "linux")
    monkeypatch.setattr(runs.shutil, "which", lambda name: None)
    assert AWAKE(42) is None and not calls
    monkeypatch.setattr(runs.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert AWAKE(42) == "systemd-inhibit"
    command, kw = calls[0]
    assert command[0] == "/usr/bin/systemd-inhibit" and "--what=sleep:idle" in command and kw["start_new_session"]
    assert command[-1] == "while kill -0 42 2>/dev/null; do sleep 30; done"


@respx.mock
def test_a_rewritten_history_says_what_to_do(made, capsys):
    got = _server()
    message = "repository 1: its history is not the one its commits were picked from"
    got["picks"].mock(
        return_value=httpx.Response(
            409,
            json={"error": {"type": "invalid_request_error", "code": "history_rewritten", "message": message}},
        )
    )
    assert cli.main(["codeval", str(made), "--json"]) == cli.FAILED
    err = json.loads(capsys.readouterr().out)["error"]
    assert "force-push or rebase" in err["next_step"] and _left(got) == ["/v1/kennet/codeval/picks"]


@respx.mock
def test_not_signed_in_with_no_terminal_it_prints_the_code_at_once_and_the_rerun_picks_up_the_approval(
    made, capsys, monkeypatch
):
    monkeypatch.delenv("METAPHI_API_KEY")
    monkeypatch.setattr(cli.accounts, "_sleep", lambda s: None)
    got = _server()
    A = config.DEFAULT_ACCOUNTS
    start = respx.post(f"{A}/device/code").respond(
        json={
            "device_code": "dev",
            "user_code": "ABCD-EFGH",
            "verification_uri": f"{A}/device",
            "verification_uri_complete": f"{A}/device?user_code=ABCD-EFGH",
            "interval": 5,
            "expires_in": 600,
        }
    )
    token = respx.post(f"{A}/oauth/token").respond(400, json={"error": "authorization_pending"})
    assert cli.main(["codeval", str(made), "--json"]) == cli.NEEDS_PERSON
    said = json.loads(capsys.readouterr().out)
    assert said == {
        "state": "sign_in",
        "url": f"{A}/device?user_code=ABCD-EFGH",
        "code": "ABCD-EFGH",
        "next_step": "the owner opens the URL and enters the code, then run the same command again",
    }
    assert start.call_count == 1 and not token.called and not got["picks"].called

    assert cli.main(["codeval", str(made), "--json"]) == cli.NEEDS_PERSON
    assert json.loads(capsys.readouterr().out)["code"] == "ABCD-EFGH" and start.call_count == 1 and token.called

    token.respond(json={"access_token": "tok"})
    respx.post(f"{A}/keys/cli").respond(
        json={
            "email": "owner@example.com",
            "org": "acme",
            "api_key": "mai_sk_" + "c" * 43,
            "test_api_key": "mai_sk_test_" + "d" * 40,
            "scopes": [codeval.SCOPE],
        }
    )
    assert cli.main(["codeval", str(made), "--json"]) == cli.NEEDS_PERSON
    out = capsys.readouterr()
    assert json.loads(out.out)["state"] == "measured" and "Signed in as owner@example.com" in out.err
    assert start.call_count == 1 and got["picks"].called and "device_pending" not in config.load()


@respx.mock
@pytest.mark.parametrize(
    ("message", "said"),
    [
        ("ab12: it was picked for another profile of it; CodeVal measures a repository once", "is not kept"),
        ("ab12: this history was already picked as another repository", "a fork or a near copy"),
    ],
)
def test_a_repository_measured_before_says_what_to_do(made, capsys, message, said):
    got = _server()
    got["picks"].mock(
        return_value=httpx.Response(
            400, json={"error": {"type": "invalid_request_error", "code": "bad_parameter", "message": message}}
        )
    )
    assert cli.main(["codeval", str(made), "--json"]) == cli.FAILED
    assert said in json.loads(capsys.readouterr().out)["error"]["next_step"]


@respx.mock
def test_picks_commit_to_the_profile_filed_and_send_the_line_from_its_root(made, capsys, tmp_path):
    got = _server()
    assert cli.main(["codeval", str(made), "--json"]) == cli.NEEDS_PERSON
    capsys.readouterr()
    sent = json.loads(got["picks"].calls[0].request.content)
    (repo,) = sent["repos"]
    assert set(repo) == {"commits", "profile_sha256"}, "no fingerprint or roots: the service computes its own"
    assert [c["has_parent"] for c in repo["commits"]] == [False, True, True]
    assert set(repo["commits"][0]) == {"id", "at", "has_parent", "source_files", "source_lines", "test_code", "era"}
    profile = json.loads(gzip.decompress((_home(tmp_path) / codeval.PAYLOAD / codeval.PROFILE).read_bytes()))
    assert codeval.entry_sha256(profile["repos"][0]) == repo["profile_sha256"], "the entry filed is the one committed"
    assert profile["repos"][0]["tip"] == repo["commits"][-1]["id"]


@respx.mock
def test_each_measured_commit_carries_its_proof_in_the_logs(made, capsys, tmp_path):
    _server()
    assert cli.main(["codeval", str(made), "--json"]) == cli.NEEDS_PERSON
    capsys.readouterr()
    out = _home(tmp_path) / codeval.PAYLOAD
    checks = json.loads(gzip.decompress((out / codeval.CHECKS).read_bytes()))
    profile = json.loads(gzip.decompress((out / codeval.PROFILE).read_bytes()))
    with tarfile.open(out / codeval.LOGS, "r:gz") as tar:
        names = tar.getnames()
        for row in checks["repos"][0]["commits"]:
            head = tar.extractfile(f"0/proof/{row['id']}/head.txt").read()
            stat = tar.extractfile(f"0/proof/{row['id']}/numstat.txt").read().decode()
            assert head.decode().strip() == row["id"]
            lines = [x.split("\t") for x in stat.splitlines()]
            assert all(x[2] == "[file]" for x in lines)
            files = next(c for c in profile["repos"][0]["commits"] if c["commit"] == row["commit"])["files"]
            assert sorted((int(a), int(d)) for a, d, _ in lines) == sorted((f["added"], f["deleted"]) for f in files)
    assert names[-1] == codeval.LOGS_DEID


@respx.mock
def test_joined_logs_are_asked_after_while_the_service_checks_them_and_no_part_goes_again(
    made, capsys, tmp_path, monkeypatch
):
    got = _server()
    _measured(made, tmp_path, monkeypatch, capsys, parts=2)
    monkeypatch.setattr(codeval.time, "sleep", lambda s: None)
    answers = iter([{"state": "checking"}, {"state": "checking"}, {"state": "received"}])
    got["complete"].side_effect = lambda request: httpx.Response(
        200 if (a := next(answers))["state"] == "received" else 202, json={"object": "codeval_logs", **a}
    )
    assert cli.main(["codeval", str(made)]) == 0
    assert got["complete"].call_count == 3 and got["part"].call_count == 2
    assert "Checking the logs" in capsys.readouterr().out


@respx.mock
def test_a_send_cut_short_while_the_logs_are_checked_only_asks_again(made, capsys, tmp_path, monkeypatch):
    got = _server()
    _measured(made, tmp_path, monkeypatch, capsys, parts=2)
    monkeypatch.setattr(codeval.time, "sleep", lambda s: None)
    monkeypatch.setattr(codeval, "COMPLETE_FOR", -1.0)
    got["complete"].mock(return_value=httpx.Response(202, json={"object": "codeval_logs", "state": "checking"}))
    assert cli.main(["codeval", str(made)]) != 0
    assert got["part"].call_count == 2
    got["complete"].mock(return_value=httpx.Response(200, json={"object": "codeval_logs", "state": "received"}))
    assert cli.main(["codeval", str(made)]) == 0
    assert got["part"].call_count == 2, "after the check began, no part is sent again"


@respx.mock
def test_one_repository_files_the_entry_it_was_committed_to_wherever_and_however_it_runs_again(
    made, capsys, tmp_path, monkeypatch
):
    got = _server()
    real = codebase_checks._commit

    def killed(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(codebase_checks, "_commit", killed)
    assert cli.main(["codeval", str(made), "--json"]) == 130
    capsys.readouterr()
    committed = json.loads(got["picks"].calls[0].request.content)["repos"][0]["profile_sha256"]
    monkeypatch.setattr(codebase_checks, "_commit", real)
    monkeypatch.setattr(codeval, "_hunks", lambda *a: None)  # a later kennet that profiles otherwise

    def filed() -> str:
        profile = json.loads(gzip.decompress((_home(tmp_path) / codeval.PAYLOAD / codeval.PROFILE).read_bytes()))
        return codeval.entry_sha256(profile["repos"][0])

    assert cli.main(["codeval", str(made), "--json"]) == cli.NEEDS_PERSON, "resumed after the kill"
    assert filed() == committed
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cli.main(["codeval", str(made), "--json"]) == cli.NEEDS_PERSON
    assert filed() == committed, "another folder"
    assert cli.main(["codeval", str(made), "--json", "--", "make", "test"]) == cli.NEEDS_PERSON
    assert filed() == committed, "another command"
    assert {json.loads(c.request.content)["repos"][0]["profile_sha256"] for c in got["picks"].calls} == {committed}
    capsys.readouterr()


@respx.mock
def test_a_repository_committed_at_another_commit_says_so(made, capsys, tmp_path):
    _server()
    assert cli.main(["codeval", str(made), "--json"]) == cli.NEEDS_PERSON
    capsys.readouterr()
    (made / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    git(made, "commit", "-qam", "later")
    assert cli.main(["codeval", str(made), "--json"]) == cli.FAILED
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "committed_elsewhere" and "check out" in err["next_step"]
