import gzip
import json
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from metaphi import cli, codebase_profile, config, errors

API = config.DEFAULT_API
SECRET = ["Falcon", "falcon_core", "hunter2", "seller@acme.test", "Grace Hopper", "git.acme.test", "ledger.py",
          "internal_payments", "billing"]


def git(repo, *args, **env):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(repo),
                        "GIT_AUTHOR_NAME": "Grace Hopper", "GIT_AUTHOR_EMAIL": "seller@acme.test",
                        "GIT_COMMITTER_NAME": "Grace Hopper", "GIT_COMMITTER_EMAIL": "seller@acme.test", **env})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "falcon_core"
    (r / "internal_payments" / "billing").mkdir(parents=True)
    (r / "tests").mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "remote", "add", "origin", "https://git.acme.test/falcon_core.git")
    (r / "internal_payments/billing/ledger.py").write_text("import os\nimport internal_payments.billing.ledger2\n# hunter2\nprint(1)\n")
    (r / "internal_payments/billing/other.py").write_text("x = 1\n")
    (r / "tests/test_ledger.py").write_text("import os\ndef test_a():\n    os.environ['TOKEN']\n")
    (r / "LICENSE").write_text("Permission is hereby granted, free of charge, to any person")
    git(r, "add", ".")
    git(r, "commit", "-qm", "Falcon: start hunter2\n\nSigned-off-by: Grace Hopper <seller@acme.test>\nProject-Falcon: x")
    (r / "internal_payments/billing/other.py").write_text("x = 2\n+++ b/fake\n@@ -1 +1 @@\ny = 2\n")
    (r / "tests/test_ledger.py").write_text("import os\ndef test_a():\n    pass\ndef test_b():\n    pass\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "fix FAL-12 in Falcon")
    git(r, "mv", "internal_payments/billing/other.py", "internal_payments/billing/ledger2.py")
    git(r, "commit", "-qm", "move")
    return r


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_" + "a" * 43)
    for var in ("METAPHI_ACCOUNTS", "METAPHI_API", "METAPHI_ALLOW_CUSTOM_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def test_document_has_no_content(repo):
    doc = codebase_profile.document([str(repo)], say=lambda s: None)
    sent = json.dumps(doc).replace('"falcon_core"', "")
    for word in SECRET:
        assert word not in sent, word
    r = doc["repos"][0]
    assert r["name"] == "falcon_core" and len(r["commits"]) == 3


def test_compute(repo):
    r = codebase_profile.document([str(repo)], say=lambda s: None)["repos"][0]
    c1, c2, c3 = r["commits"]
    assert c1["parents"] == [] and c2["parents"] == [1] and c1["trailers"] == {"signed-off-by": 1}
    assert c2["ticket_refs"] == 1 and c2["fix_word"] and not c1["fix_word"]
    assert {x["status"] for x in c1["files"]} == {"A"} and c3["files"][0]["status"] == "R" and "old_file" in c3["files"][0]
    assert sum(x["hunks"] for x in c2["files"]) == 2
    assert r["licenses"] == ["MIT"] and len(r["authors"]) == 1 and "blame" not in r
    files = {(f["kind"], f.get("test_fns")) for f in r["files"] if f["at_tip"]}
    assert ("test", 2) in files
    ledger = next(f for f in r["files"] if f.get("imports_internal"))
    assert ledger["imports_internal"] == 1 and ledger["imports_external"] == 1 and "lines_by_year" not in ledger


def test_skip_shallow_and_plain_dir(repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth=1", f"file://{repo}", str(shallow)], check=True, capture_output=True)
    doc = codebase_profile.document([str(plain), str(shallow)], say=lambda s: None)
    assert doc["repos"] == [] and [s["reason"] for s in doc["skipped"]] == ["not_a_git_repository", "shallow"]


def test_url_clone(repo, tmp_path, monkeypatch):
    origin = tmp_path / "srv" / "falcon_core.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)], check=True, capture_output=True)
    made = []
    real = codebase_profile.tempfile.TemporaryDirectory
    monkeypatch.setattr(codebase_profile.tempfile, "TemporaryDirectory", lambda **k: made.append(real(**k)) or made[-1])
    said = []
    doc = codebase_profile.document([f"file://{origin}"], say=said.append)
    assert [r["name"] for r in doc["repos"]] == ["falcon_core"] and doc["skipped"] == []
    assert said == ["falcon_core: cloning", "falcon_core: computing"]
    local = codebase_profile.document([str(repo)], say=lambda s: None)["repos"][0]
    assert doc["repos"][0]["commits"] == local["commits"]
    assert made and not any(Path(t.name).exists() for t in made)
    assert str(origin) not in json.dumps(doc) and "srv" not in json.dumps(doc)


def test_url_clone_fails(repo, tmp_path):
    said = []
    doc = codebase_profile.document([f"file://{tmp_path}/missing.git", str(repo)], say=said.append)
    assert doc["skipped"] == [{"name": "missing", "reason": "clone_failed"}]
    assert [r["name"] for r in doc["repos"]] == ["falcon_core"]
    assert any(s.startswith("missing: skipped, clone failed: ") and s.endswith("in this terminal") for s in said)


@pytest.mark.parametrize(("given", "url"), [
    ("https://github.com/acme/core.git", True), ("git@github.com:acme/core.git", True), ("ssh://git@h/acme/core", True),
    ("./core", False), ("core", False), ("/srv/core", False),
])
def test_is_url(given, url):
    assert codebase_profile.is_url(given) is url


def test_unknown_extension(repo):
    (repo / "plan.falcon").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "x")
    assert '"falcon"' not in json.dumps(codebase_profile.document([str(repo)], say=lambda s: None))


def test_subdirectory(repo):
    doc = codebase_profile.document([str(repo / "internal_payments")], say=lambda s: None)
    assert doc["repos"] == [] and doc["skipped"][0]["reason"] == "not_a_git_repository"


def test_blob_ids(repo):
    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD:LICENSE"], capture_output=True, text=True, check=True).stdout.strip()
    a = codebase_profile.compute(repo, "x")
    assert tip not in json.dumps(a)
    assert {f.get("blob") for f in a["files"]} != {f.get("blob") for f in codebase_profile.compute(repo, "x")["files"]}


def test_repo_config_inert(repo, tmp_path):
    mark = tmp_path / "ran"
    evil = tmp_path / "evil.sh"
    evil.write_text(f"#!/bin/sh\ntouch {mark}\ncat \"$1\"\n")
    evil.chmod(0o755)
    before = codebase_profile.compute(repo, "x")["commits"]
    (repo / ".git/info/attributes").write_text("* diff=evil filter=evil\n")
    for key, val in (("diff.evil.textconv", evil), ("diff.evil.command", evil), ("filter.evil.smudge", evil), ("filter.evil.clean", evil),
                     ("gpg.program", evil), ("log.showSignature", "true"), ("diff.noprefix", "true"), ("core.fsmonitor", evil)):
        git(repo, "config", key, str(val))
    after = codebase_profile.compute(repo, "x")
    assert not mark.exists() and after["commits"] == before


def test_bad_date(repo):
    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True, check=True).stdout.strip()
    raw = f"tree {tree}\nauthor a <a@b> notadate\ncommitter a <a@b> notadate\n\nm\n"
    sha = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin", "--literally"], input=raw, capture_output=True, text=True, check=True).stdout.strip()
    git(repo, "update-ref", "HEAD", sha)
    assert codebase_profile.document([str(repo)], say=lambda s: None)["repos"][0]["commits"][0]["authored_at"] == 0


def test_blank_lines():
    import time
    started = time.monotonic()
    codebase_profile._content(b"\n" * 500_000, "go", "source", lambda a, b: False)
    assert time.monotonic() - started < 5


@respx.mock
def test_key_checked_first(repo, capsys, monkeypatch):
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(403, json={"error": {
        "type": "authentication_error", "code": "insufficient_scope", "retryable": False, "request_id": "r",
        "message": "this API key does not hold access to codebase profiles"}}))
    monkeypatch.setattr(codebase_profile, "compute", lambda *a: pytest.fail("computed"))
    assert cli.main(["profile-codebase", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "kennet:codebase_profile:write" in err and "kennet login" in err


@respx.mock
def test_a_person_not_signed_in_signs_in_and_the_profile_goes_on(repo, capsys, monkeypatch):
    monkeypatch.delenv("METAPHI_API_KEY")
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    signed = []

    def login(say=print, **kw):
        signed.append(True)
        monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_" + "b" * 43)
        return {"email": "owner@example.com"}

    monkeypatch.setattr(cli.accounts, "login", login)
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    monkeypatch.setattr(codebase_profile, "profiled", lambda *a, **k: pytest.fail("profiled") if not signed else
                        (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        cli.main(["profile-codebase", str(repo)])
    assert signed and "Signed in as owner@example.com." in capsys.readouterr().out


@respx.mock
def test_signed_in_with_no_key_saved_picks_the_keys_up_and_goes_on(repo, monkeypatch):
    monkeypatch.delenv("METAPHI_API_KEY")
    config.save({"token": "tok", "email": "owner@example.com"})
    picked = respx.post(f"{config.DEFAULT_ACCOUNTS}/keys/cli").respond(json={
        "email": "owner@example.com", "org": "acme", "api_key": "mai_sk_" + "c" * 43,
        "test_api_key": "mai_sk_test_" + "d" * 40})
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    monkeypatch.setattr(codebase_profile, "profiled", lambda *a, **k: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        cli.main(["profile-codebase", str(repo), "--json"])
    assert picked.called


def test_away_from_a_terminal_not_signed_in_names_kennet_login(repo, capsys, monkeypatch):
    monkeypatch.delenv("METAPHI_API_KEY")
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli.accounts, "login", lambda **kw: pytest.fail("signed in"))
    assert cli.main(["profile-codebase", str(repo)]) == 3
    assert "kennet login" in capsys.readouterr().err


def test_tokens_offline(repo, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a: (_ for _ in ()).throw(OSError("no network")))
    from metaphi import o200k
    o200k._encoding.clear()
    r = codebase_profile.document([str(repo)], say=lambda s: None)["repos"][0]
    other = next(f for f in r["files"] if f["at_tip"] and f["kind"] == "other")
    assert other["tokens"] == 12
    assert all(("tokens" in f) == f["at_tip"] for f in r["files"])


@pytest.fixture
def tested(tmp_path):
    r = tmp_path / "falcon_calc"
    (r / "tests").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "tests/test_calc.py").write_text("from calc import *\ndef test_add():\n    assert add(1, 2) == 3\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "add")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\ndef mul(a, b):\n    return a * b\n")
    (r / "tests/test_calc.py").write_text("from calc import *\ndef test_add():\n    assert add(1, 2) == 3\n"
                                          "def test_mul_hunter2():\n    assert mul(2, 3) == 6\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "mul")
    (r / "calc.py").write_text("def add(a, b):\n    return sum((a, b))\ndef mul(a, b):\n    return a * b\n")
    (r / "tests/test_calc.py").write_text("from calc import *\ndef test_add():\n    assert add(1, 2) == 3\n"
                                          "def test_mul_hunter2():\n    assert mul(2, 3) == 6\n# again\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "refactor")
    return r


PYTEST = "PYTHONPATH=. {py} -m pytest -q -p no:cacheprovider --junitxml=$KENNET_JUNIT/r.xml tests"


def _checked(r, say=lambda s: None, picks=(2, 3), runs=2, **kw):
    import sys

    from metaphi import codebase_checks
    plan = codebase_checks.Plan(**{"test": PYTEST.format(py=sys.executable), **kw})
    with codebase_profile.profiled([str(r)], say=say) as (doc, kept):
        return doc, codebase_profile.checked(kept, {"runs": runs, "repos": [list(picks)]}, plan, say)


def test_runs_recorded(tested):
    doc, report = _checked(tested)
    got = report["repos"][0]
    assert report["schema"] == codebase_profile.CHECKS_SCHEMA and (got["stopped"], got["runs"]) == ("done", 2)
    assert got["tip"]["tests"][0] == {**got["tip"]["tests"][0], "exit": 0, "reported": 2, "passed": 2}
    by = {c["commit"]: c for c in got["commits"]}
    assert [t["exit"] for t in by[2]["pre"]["tests"]] == [1] and [t["exit"] for t in by[2]["post"]["tests"]] == [0, 0]
    assert (by[2]["fail_to_pass"], by[2]["pass_to_pass"], by[2]["flaky"]) == (1, 1, 0)
    assert (by[3]["fail_to_pass"], by[3]["pass_to_pass"]) == (0, 2)
    assert "verdict" not in json.dumps(got) and "checks" not in doc["repos"][0]
    sent = json.dumps(report) + json.dumps(doc)
    for word in ("hunter2", "test_mul", "calc.py"):
        assert word not in sent, word


def test_commit_order(tested):
    _doc, report = _checked(tested, picks=(3,), test="true")
    assert [c["commit"] for c in report["repos"][0]["commits"]] == [3]
    _doc, report = _checked(tested, picks=(3, 2), test="true")
    assert [c["commit"] for c in report["repos"][0]["commits"]] == [3, 2]


def test_junit_per_run(tested):
    _doc, report = _checked(tested, test=PYTEST.format(py=__import__("sys").executable).replace("r.xml", "TEST-$$.xml"))
    by = {c["commit"]: c for c in report["repos"][0]["commits"]}
    assert (by[2]["fail_to_pass"], by[2]["pass_to_pass"], by[2]["flaky"]) == (1, 1, 0)


def test_no_junit(tested):
    import sys
    _doc, report = _checked(tested, test=f"PYTHONPATH=. {sys.executable} -m pytest -q -p no:cacheprovider tests")
    by = {c["commit"]: c for c in report["repos"][0]["commits"]}
    assert by[2]["fail_to_pass"] is None and by[2]["post"]["tests"][0]["reported"] is None
    assert [t["exit"] for t in by[2]["pre"]["tests"]] == [1] and [t["exit"] for t in by[2]["post"]["tests"]] == [0, 0]


def test_build_fail_and_timeout(tested):
    old_only = 'test -z "$KENNET_TESTS"'
    got = _checked(tested, build=old_only)[1]["repos"][0]
    assert got["tip"]["build"]["exit"] == 0 and len(got["commits"]) == 2
    assert all(c["post"]["build"]["exit"] == 1 and c["post"]["tests"] == [] for c in got["commits"])
    slow = _checked(tested, test=f"{old_only} || sleep 30", run_minutes=0.02)[1]["repos"][0]
    assert all(c["post"]["tests"][0]["exit"] is None for c in slow["commits"])


def test_failed_run_output(tested, tmp_path, capfd):
    lines: list[str] = []
    doc, report = _checked(tested, say=lines.append, runs=1, test="./run-tests.sh", logs=tmp_path / "logs",
                           build='test -z "$KENNET_TESTS" || { echo "cc: FALCON-TOOLCHAIN missing"; exit 4; }')
    told = "\n".join(lines)
    assert told.count("cc: FALCON-TOOLCHAIN missing") == 4 and "exit 4" in told
    assert "exit 127" in told and "run-tests.sh: not found" in told.replace("No such file or directory", "not found")
    assert str(tmp_path / "logs") in told and "FALCON" not in json.dumps(report) + json.dumps(doc)


def test_repo_untouched(tested):
    status = ["git", "-C", str(tested), "status", "--porcelain", "--ignored"]
    before = subprocess.run(status, capture_output=True, text=True, check=True).stdout
    _checked(tested, picks=(2,))
    after = subprocess.run(status, capture_output=True, text=True, check=True).stdout
    assert before == after == "" and not (tested / ".git" / "worktrees").exists()


def test_run_error(tested, monkeypatch):
    from metaphi import codebase_checks
    real, calls = codebase_checks._commit, []

    def first_breaks(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("No space left on device")
        return real(*a, **k)
    monkeypatch.setattr(codebase_checks, "_commit", first_breaks)
    lines: list[str] = []
    got = _checked(tested, say=lines.append, picks=(2, 3, 99))[1]["repos"][0]
    assert [bool(c.get("broke")) for c in got["commits"]] == [True, False, True] and got["stopped"] == "done"
    assert "No space left on device" in "\n".join(lines)


def test_tests_cannot_start(tested, monkeypatch):
    from metaphi import codebase_checks
    monkeypatch.setattr(codebase_checks, "run", lambda *a, **k: 1 / 0)
    doc, report = _checked(tested)
    assert report["repos"][0]["stopped"] == "error" and doc["repos"][0]["commits"]


def _unpacked(body: bytes) -> tuple[list[str], dict]:
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        members = tar.getmembers()
        assert all(m.isfile() for m in members)
        said = {m.name: tar.extractfile(m).read() for m in members}
    said["kennet-logs.json"] = json.loads(said["kennet-logs.json"])
    return [m.name for m in members], said


def test_logs_packed(tmp_path):
    from metaphi import codebase_profile
    root = tmp_path / "logs"
    (root / "secret-name" / "setup").mkdir(parents=True)
    (root / "secret-name" / "setup" / "conversation.jsonl").write_text("cat src/main.c")
    (root / "secret-name" / "setup" / "run-03.log").write_text("int main() {}")
    (root / "secret-name" / "setup" / "turns.jsonl").write_text("{}")
    (root / "secret-name" / "steps.jsonl").write_text("{}")
    (root / "secret-name" / "big.log").write_bytes(b"A" * (1 << 20) + b"B" * (5 << 20) + b"Z")
    (root / "secret-name" / "away").symlink_to("/etc/hosts")
    (root / "other").mkdir()
    (root / "other" / "x.log").write_text("not this run's")
    names, said = _unpacked(codebase_profile.logs_packed(root, "cbp_" + "5" * 16, ["secret-name"]))
    assert sorted(names) == ["0/big.log", "0/setup/turns.jsonl", "0/steps.jsonl", "kennet-logs.json"]
    big = said["0/big.log"]
    assert len(big) < codebase_profile.LOG_FILE_MOST + 100 and big.startswith(b"A") and big.endswith(b"BZ")
    assert said["kennet-logs.json"]["schema"] == codebase_profile.LOGS_SCHEMA


def _said(*calls):
    return httpx.Response(200, json={"content": [{"type": "tool_use", "id": f"t{n}", "name": name, "input": args}
                                                 for n, (name, args) in enumerate(calls)],
                                     "usage": {"input_tokens": 100, "output_tokens": 10}})


def _no_commands(tested, model_said, first=1):
    import sys  # noqa: F401

    ref = "cbp_" + "2" * 16
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/codebase-profiles").mock(return_value=httpx.Response(200, json={"id": ref}))
    respx.post(f"{API}/codebase-profiles/{ref}/sample").mock(
        return_value=httpx.Response(200, json={"runs": 1, "repos": [[2, 3]], "first": first}))
    setup = respx.post(f"{API}/codebase-profiles/{ref}/setup").mock(return_value=httpx.Response(200, json={
        "base_url": "https://gw.test/anthropic", "token": "kgw1.t", "model": "claude-sonnet-5-5"}))
    model = respx.post("https://gw.test/anthropic/v1/messages").mock(side_effect=model_said)
    checks = respx.post(f"{API}/codebase-profiles/{ref}/checks").mock(return_value=httpx.Response(200, json={"id": ref}))
    logs = respx.post(f"{API}/codebase-profiles/{ref}/logs").mock(return_value=httpx.Response(200, json={"id": ref}))
    assert cli.main(["profile-codebase", str(tested), "--deep"]) in (0, None)
    names, said = _unpacked(logs.calls[-1].request.content)
    assert not any("conversation.jsonl" in n or "/run-" in n for n in names)
    for line in said.get("0/setup/turns.jsonl", b"").splitlines():
        for a in json.loads(line)["content"] if json.loads(line)["role"] == "user" else []:
            assert isinstance(a, dict) and "\n" not in a["content"]
    return setup, model, json.loads(gzip.decompress(checks.calls[-1].request.content))["repos"][0]


@respx.mock
def test_setup(tested, capsys, tmp_path, monkeypatch):
    import sys

    from metaphi import codebase_setup
    monkeypatch.setattr(codebase_setup, "engine", lambda: None)
    setup, model, sent = _no_commands(tested, [
        _said(("run", {"command": "ls tests"})),
        _said(("propose", {"setup": "", "build": "true", "test": PYTEST.format(py=sys.executable)}))])
    assert setup.call_count == 1 and model.call_count == 2
    first = json.loads(model.calls[0].request.content)
    assert model.calls[0].request.headers["x-api-key"] == "kgw1.t" and first["model"] == "claude-sonnet-5-5"
    assert "ls tests" in json.dumps(json.loads(model.calls[1].request.content)["messages"])
    assert (sent["test_given"], sent["build_given"]) == (False, False)
    assert [(s["by"], s["era"], s["worked"], s["steps"], s["container"], s["tokens"], s["why"]) for s in sent["setup"]] == [
        ("local", None, False, 0, False, 0, "none_found"), ("model", None, True, 2, False, 220, None)]
    assert sent["tip"]["tests"][0]["reported"] == 2 and sent["tip"]["tests"][0]["passed"] == 2
    assert [c["commit"] for c in sent["commits"]] == [2] and sent["commits"][0]["fail_to_pass"] == 1
    assert "pytest" not in json.dumps(sent) and "ls tests" not in json.dumps(sent)
    from metaphi.steps import NAMES
    assert {"profile", "setup", "setup_model", "setup_proposals", "tip", "commits", "build", "test"} <= set(
        sent["seconds"]) <= NAMES and all(isinstance(v, int) for v in sent["seconds"].values())
    logs = tmp_path / "kennet-profile" / ("cbp_" + "2" * 16) / "logs" / "falcon_calc"
    steps = [json.loads(x)["step"] for x in (logs / "steps.jsonl").open()]
    assert "setup_model" in steps and "build" in steps
    assert "pytest" in (logs / "setup" / "model" / "recipe.json").read_text()
    out = capsys.readouterr().out
    assert "running on this machine" in out and "000-tip build ok" in out and "0 of 2 failed" in out


@respx.mock
def test_setup_fails(tested, monkeypatch, tmp_path):
    from metaphi import codebase_setup
    monkeypatch.setattr(codebase_setup, "engine", lambda: None)
    _, _, sent = _no_commands(tested, [_said(("give_up", {"why": "license", "reason": "needs a licensed compiler"}))], first=2)
    assert [x["worked"] for x in sent["setup"]] == [False, False] and "tip" not in sent
    assert sent["setup"][1]["why"] == "gave_up_license"
    assert sent["commits"] == [{"commit": 2, "broke": True}, {"commit": 3, "broke": True}]
    assert "licensed" not in json.dumps(sent)
    logs = tmp_path / "kennet-profile" / ("cbp_" + "2" * 16) / "logs" / "falcon_calc"
    assert "licensed" in (logs / "setup" / "model" / "gave-up.txt").read_text()


def test_recipe_edits_files():
    from metaphi import codebase_setup
    d = _dirs()
    (d[0] / "configure.ac").write_text("AM_INIT\n")
    said, kept, why, _ = codebase_setup._tried(codebase_setup.Recipe("", "echo XX > configure.ac", "true"),
                                               None, d, "", d[0].parent, 1, 1, *_runners())
    assert kept is False and "configure.ac" in said and why == "tracked_file_changed"


def test_recipe_build_fails():
    from metaphi import codebase_setup
    record: list = []
    said, kept, why, _ = codebase_setup._tried(codebase_setup.Recipe("", "exit 3", "true"), None, _dirs(), "",
                                               _dirs()[0], 1, 1, *_runners(), None, record)
    assert kept is False and "exit: 3" in said and "build did not pass" in said and why == "build_failed"
    assert record[0]["build"]["exit"] == 3 and "test" not in record[0]


def test_recipe_records_build_beside_failing_tests():
    from metaphi import codebase_setup
    d, record = _dirs(), []
    _, kept, why, _ = codebase_setup._tried(codebase_setup.Recipe("", "true", _junit_of(0, 2) + "; exit 1"), None, d,
                                            "", d[0].parent, 1, 1, *_runners(), None, record)
    assert kept is False and why == "pass_fraction"
    assert record[0]["build"]["exit"] == 0 and isinstance(record[0]["build"]["seconds"], int)
    assert (record[0]["test"]["exit"], record[0]["test"]["reported"], record[0]["test"]["passed"]) == (1, 2, 0)


def _junit_of(passed: int, failed: int) -> str:
    cases = "".join(f'<testcase classname="a" name="p{n}"/>' for n in range(passed)) + "".join(
        f'<testcase classname="a" name="f{n}"><failure/></testcase>' for n in range(failed))
    return f"echo '<testsuite>{cases}</testsuite>' > $KENNET_JUNIT/r.xml"


@pytest.mark.parametrize(("test", "uncounted", "taken", "why", "report"), [
    ("exit 1", "", False, "tests_failed", None),
    ("true", "", False, "no_test_count", None),
    ("true", "a shell harness that prints no count", True, None, None),
    ("exit 1", "a shell harness that prints no count", False, "tests_failed", None),
    (_junit_of(0, 1) + "; exit 1", "", False, "pass_fraction", None),
    (_junit_of(1, 1) + "; exit 1", "", True, None, "junit"),
    ("printf '==== 3 passed, 1 failed in 0.20s ====\\n'; exit 1", "", True, None, "summary"),
])
def test_recipe_tests(test, uncounted, taken, why, report):
    from metaphi import codebase_setup
    d = _dirs()
    said, kept, got_why, got_report = codebase_setup._tried(codebase_setup.Recipe("", "true", test, None, uncounted),
                                                            None, d, "", d[0].parent, 1, 1, *_runners())
    assert (kept == "") is taken and got_why == why and got_report == report, said


def _dirs():
    import tempfile
    root = Path(tempfile.mkdtemp())
    (root / "src").mkdir()
    (root / "junit").mkdir()
    return root / "src", root / "junit"


def _runners():
    from metaphi.codebase_checks import _junit, _run
    return _run, _junit


def test_folder_of_clones(repo, tested, tmp_path):
    (tmp_path / "notes").mkdir()
    assert codebase_profile.repositories([str(tmp_path)]) == sorted([str(repo), str(tested)])
    assert codebase_profile.repositories([str(repo)]) == [str(repo)]


def test_prompt_cache():
    from metaphi import codebase_setup
    got = codebase_setup._cached([{"role": "user", "content": "a"}, {"role": "user", "content": [{"type": "text",
                                                                                                    "text": "b"}]}])
    assert got[0] == {"role": "user", "content": "a"} and got[1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_setup_again(monkeypatch, tmp_path):
    from metaphi import codebase_checks
    from metaphi.codebase_setup import Recipe
    tip, old = (Recipe("", "cmake", "ctest", "img:tip"), "docker"), (Recipe("", "./configure", "make check", "img:old"),
                                                                     "docker")
    side = lambda code: {"build": {"exit": code, "seconds": 1}, "tests": []}
    monkeypatch.setattr(codebase_checks, "_side", lambda *a, **k: (side(0), []))
    monkeypatch.setattr(codebase_checks, "_commit", lambda repo, plan, runs, logs, label, parent, sha, say, with_, jobs:
                        {"pre": side(0 if with_ is old or sha.startswith("new") else 1),
                         "post": side(0 if with_ is old or sha.startswith("new") else 1)})
    commits = [{"commit": i, "parents": [0]} for i in (1, 2, 3)]
    order = {"base": 0, "old1": 1, "old2": 2, "new3": 3}
    asked = []

    def repair(era, sha, tail):
        asked.append((era, sha))
        return old
    plan = codebase_checks.Plan(logs=tmp_path)
    got = codebase_checks.run(tmp_path, "r", "tip", commits, order, [1, 2, 3], 1, plan, lambda s: None, tip,
                              [1, 1, 2], repair)
    assert asked == [(1, "old1")]
    assert [r["post"]["build"]["exit"] for r in got["commits"]] == [0, 0, 0]
    asked.clear()
    got = codebase_checks.run(tmp_path, "r", "tip", commits, order, [1, 3, 2], 1, plan, lambda s: None, tip,
                              [1, 1, 1], repair)
    assert asked == [(1, "old1")] and [r["post"]["build"]["exit"] for r in got["commits"]] == [0, 0, 0]
    got = codebase_checks.run(tmp_path, "r", "tip", commits, order, [1, 2], 1, plan, lambda s: None, tip, [1, 1],
                              lambda era, sha, tail: tip)
    assert [r["post"]["build"]["exit"] for r in got["commits"]] == [1, 1]


def test_sites():
    from metaphi import codebase_sites as cs
    go = 'x := "a == b" // c < d\nif a == b && c < d { return a + b > 3 && true }\nch <- 1; i++; m[k]--\n/* || */\n'
    assert cs.counts(go, "go") == {"cmp": 3, "logic": 2, "arith": 1, "bool": 1}
    py = 'def f(a):\n    """a == b"""\n    # a or b\n    return a - 1 if a and not b else -a\n'
    assert cs.counts(py, "py") == {"cmp": 0, "logic": 1, "arith": 1, "bool": 0}
    assert cs.swapped(py, "py", "logic", 0).endswith("if a or not b else -a\n")
    assert cs.swapped(go, "go", "cmp", 2) == go.replace("+ b > 3", "+ b >= 3")
    assert cs.swapped(go, "go", "cmp", 3) is None and cs.counts("a == b", "md") is None
    rs = "fn f<'a>(x: &'a str) -> bool { let c = 'x'; x.len() > 1 && true }"
    assert cs.counts(rs, "rs") == {"cmp": 1, "logic": 1, "arith": 0, "bool": 1}


@pytest.fixture
def planted(tmp_path):
    r = tmp_path / "falcon_sums"
    (r / "tests").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    (r / "sums.py").write_text("def add(a, b):\n    return a + b\ndef pos(a):\n    return a > 0\n"
                               "def neg(a):\n    return a - 1  # hunter2 == x\n")
    (r / "tests/test_sums.py").write_text("from sums import *\ndef test_add():\n    assert add(1, 2) == 3\n"
                                          "def test_pos():\n    assert pos(1) and not pos(0)\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "sums")
    return r


def test_ops_counted(planted):
    doc = codebase_profile.document([str(planted)], say=lambda s: None)
    rows = [f for f in doc["repos"][0]["files"] if f["at_tip"]]
    assert [f["ops"] for f in rows if "ops" in f] == [{"cmp": 1, "logic": 0, "arith": 2, "bool": 0}]
    assert "hunter2" not in json.dumps(doc)


def test_breaks(planted):
    import sys

    from metaphi import codebase_checks
    plan = codebase_checks.Plan(test=PYTEST.format(py=sys.executable))
    with codebase_profile.profiled([str(planted)], say=lambda s: None) as (_doc, kept):
        f = next(i for i, p in kept[0]["paths"].items() if p == "sums.py")
        breaks = [{"file": f, "op": "arith", "nth": 0}, {"file": f, "op": "cmp", "nth": 0},
                  {"file": f, "op": "arith", "nth": 1}, {"file": f, "op": "logic", "nth": 0}]
        lines: list[str] = []
        got = codebase_profile.checked(kept, {"runs": 2, "repos": [[]], "breaks": [breaks]}, plan, lines.append)
    out = got["repos"][0]
    assert out["stopped"] == "done" and out["commits"] == []
    rows = out["breaks"]
    assert [(r["file"], r["op"], r["nth"]) for r in rows] == [(b["file"], b["op"], b["nth"]) for b in breaks]
    assert [r.get("caught") for r in rows] == [1, 1, 0, None] and rows[3]["broke"]
    assert rows[2]["kept"] == 2 and rows[0]["gone"] == 0 and len(rows[0]["post"]["tests"]) == 2
    assert rows[0]["post"]["tests"][0]["exit"] == 1 and rows[2]["post"]["tests"][0]["exit"] == 0
    assert (planted / "sums.py").read_text().count("a + b") == 1
    sent = json.dumps(got)
    for word in ("hunter2", "sums.py", "test_add", "assert"):
        assert word not in sent, word
    assert not any("assert" in line for line in lines) and sum("-tip build ok (" in x for x in lines) == 3


def _server(ref, picks=((2,),), runs=1, checks=200, logs=200):
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    return {
        "profile": respx.post(f"{API}/codebase-profiles").mock(return_value=httpx.Response(200, json={"id": ref})),
        "sample": respx.post(f"{API}/codebase-profiles/{ref}/sample").mock(return_value=httpx.Response(200, json={
            "runs": runs, "repos": [list(p) for p in picks], "run_minutes": 5, "hours": 1})),
        "setup": respx.post(f"{API}/codebase-profiles/{ref}/setup"),
        "checks": respx.post(f"{API}/codebase-profiles/{ref}/checks").mock(
            return_value=httpx.Response(checks, json={"id": ref})),
        "logs": respx.post(f"{API}/codebase-profiles/{ref}/logs").mock(
            return_value=httpx.Response(logs, json={"id": ref})),
    }


@respx.mock
def test_default_finds_nothing(repo, capsys, tmp_path, monkeypatch):
    from metaphi import codebase_setup
    monkeypatch.setattr(codebase_setup, "find", lambda *a, **k: pytest.fail("model"))
    ref = "cbp_" + "4" * 16
    got = _server(ref, picks=((2,),))
    assert cli.main(["profile-codebase", str(repo)]) in (0, None)
    assert not got["setup"].called and not got["sample"].called and got["checks"].called and got["logs"].called
    sent = json.loads(gzip.decompress(got["checks"].calls[-1].request.content))["repos"][0]
    assert [(s["by"], s["worked"], s["why"], s["tries"]) for s in sent["setup"]] == [("local", False, "none_found", [])]
    assert sent["commits"] == [] and sent["breaks"] == []
    out = capsys.readouterr().out
    assert f"No build or test found in {repo}" in out and "--build '<build>' --test '<test>'" in out
    assert "--deep" in out and "Done. 1 repository shared" in out
    assert json.loads((tmp_path / "kennet-profile" / ref / "profile.json").read_text())["repos"]


@pytest.fixture
def made(tmp_path):
    import sys
    r = tmp_path / "falcon_made"
    (r / "tests").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    (r / "Makefile").write_text(f"test:\n\tPYTHONPATH=. {sys.executable} -m pytest -q -p no:cacheprovider "
                                "--junitxml=$$KENNET_JUNIT/r.xml tests\n")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "tests/test_calc.py").write_text("from calc import *\ndef test_add():\n    assert add(1, 2) == 3\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "add")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\ndef mul(a, b):\n    return a * b\n")
    (r / "tests/test_calc.py").write_text("from calc import *\ndef test_add():\n    assert add(1, 2) == 3\n"
                                          "def test_mul():\n    assert mul(2, 3) == 6\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "mul")
    return r


@respx.mock
def test_default_finds_commands(made, capsys, tmp_path, monkeypatch):
    from metaphi import codebase_setup
    monkeypatch.setattr(codebase_setup, "engine", lambda: None)
    monkeypatch.setattr(codebase_setup, "find", lambda *a, **k: pytest.fail("model"))
    ref = "cbp_" + "6" * 16
    got = _server(ref, picks=((2,),))
    assert cli.main(["profile-codebase", str(made)]) in (0, None)
    sent = json.loads(gzip.decompress(got["checks"].calls[-1].request.content))["repos"][0]
    assert [(s["by"], s["worked"], s["steps"], s["report"]) for s in sent["setup"]] == [("local", True, 1, "junit")]
    assert [(t["build"]["exit"], t["test"]["reported"]) for t in sent["setup"][0]["tries"]] == [(0, 2)]
    assert sent["tip"]["tests"][0]["reported"] == 2 and sent["tip"]["tests"][0]["passed"] == 2
    assert sent["commits"] == [] and sent["runs"] == 0 and not got["sample"].called
    assert not got["setup"].called and "No build or test found" not in capsys.readouterr().out
    assert "make test" in (tmp_path / "kennet-profile" / ref / "logs" / "falcon_made" / "setup" / "recipe.json").read_text()
    # past commits, before and after each change, only when asked
    assert cli.main(["profile-codebase", str(made), "--commits"]) in (0, None)
    sent = json.loads(gzip.decompress(got["checks"].calls[-1].request.content))["repos"][0]
    assert got["sample"].call_count == 1 and sent["commits"][0]["fail_to_pass"] == 1


@respx.mock
def test_rerun_sends_what_was_kept(tested, capsys, tmp_path):
    ref = "cbp_" + "1" * 16
    got = _server(ref, picks=((2,),), checks=503)
    assert cli.main(["profile-codebase", str(tested), "--test", "echo SAID-BY-TESTS", "--commits", "--json"]) != 0
    assert "run the same command again" in json.loads(capsys.readouterr().out)["error"]["next_step"]
    sent = json.loads(gzip.decompress(got["checks"].calls[-1].request.content))
    assert [c["commit"] for c in sent["repos"][0]["commits"]] == [2] and sent["repos"][0]["test_given"]
    assert not got["logs"].called and got["sample"].call_count == 1
    got["checks"].mock(return_value=httpx.Response(200, json={"id": ref}))
    got["logs"].mock(return_value=httpx.Response(503))
    assert cli.main(["profile-codebase", str(tested), "--test", "echo SAID-BY-TESTS", "--commits"]) != 0
    assert got["sample"].call_count == 1 and json.loads(gzip.decompress(got["checks"].calls[-1].request.content)) == sent
    names, said = _unpacked(got["logs"].calls[-1].request.content)
    assert said["kennet-logs.json"]["profile"] == ref and b"SAID-BY-TESTS" in said["0/000-tip.test-1.log"]
    assert not any(tested.name in n for n in names)
    checks = got["checks"].call_count
    got["logs"].mock(return_value=httpx.Response(200, json={"id": ref}))
    assert cli.main(["profile-codebase", str(tested), "--test", "echo SAID-BY-TESTS", "--commits"]) in (0, None)
    assert got["checks"].call_count == checks and got["sample"].call_count == 1
    assert cli.main(["profile-codebase", str(tested), "--test", "true", "--commits"]) in (0, None)
    assert got["sample"].call_count == 2


@respx.mock
def test_needs_login(repo, monkeypatch, tmp_path):
    monkeypatch.delenv("METAPHI_API_KEY")
    access = respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    assert cli.main(["profile-codebase", str(repo), "--json"]) != 0
    assert not access.called and not (tmp_path / "kennet-profile").exists()


@respx.mock
def test_flag_errors(tested, capsys):
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    assert cli.main(["profile-codebase", str(tested), "--build", "make"]) != 0
    assert cli.main(["profile-codebase", str(tested), "--test", "make", "--deep"]) != 0
    said = capsys.readouterr().err
    assert "build_without_test" in said and "deep_with_test" in said
    for gone in ("--dry-run", "--send", "--hide-names", "--stream", "--test-logs", "--run-minutes", "--test-hours"):
        with pytest.raises(SystemExit):
            cli.main(["profile-codebase", str(tested), gone])


WORKFLOW = """
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ruff check .
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - run: sudo apt-get install -y libfoo-dev
      - run: |
          pip install -e .[test]
      - run: make build
      - run: pytest -x tests
      - run: codecov upload
  win:
    runs-on: windows-latest
    steps:
      - run: pytest
"""


def test_candidates():
    from metaphi import codebase_recipes as cr
    files = {".github/workflows/ci.yml": WORKFLOW.encode(), "pyproject.toml": b"[project]\nname='x'\n",
             "package.json": b'{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}',
             "go.mod": b"module x\n\ngo 1.22\n", "Makefile": b"all:\n\tcc x.c\ncheck:\n\t./t\n"}
    got = cr.candidates(set(files), files.get)
    ci = got[0]
    assert len([r for r in got if "codecov" in r.test or "ruff" in r.test]) == 0
    assert ci.image == "python:3.11" and "pip install -e .[test]" in ci.setup and "apt-get" in ci.setup
    assert "make build" in ci.build and "pytest -x tests" in ci.test and "codecov" not in ci.setup + ci.build
    images = [r.image for r in got[1:]]
    assert images == ["golang:1.22", "python:3.12", "gcc:14"] and got[-1].test == "make check"
    assert "sudo" not in cr.contained(ci).setup and cr.hosted(ci) is None
    py = cr.hosted(got[2])
    assert py.setup.startswith("python3 -m venv .kennet-venv") and ".kennet-venv/bin" in py.test and py.image is None
    assert cr.candidates(set(), {}.get) == []


def test_candidates_gitlab_travis():
    from metaphi import codebase_recipes as cr
    files = {".gitlab-ci.yml": b"image: maven:3-eclipse-temurin-17\nbuild:\n  script: [mvn -B compile]\n"
                               b"test:\n  script: [mvn -B test]\n.hidden:\n  script: [make test]\n"
                               b"deploy:\n  script: [echo $CI_JOB_TOKEN]\n",
             ".travis.yml": b"language: python\npython: ['3.9']\ninstall: pip install -r requirements.txt\n"
                            b"script: [flake8, python -m unittest]\n"}
    got = cr.candidates(set(files), files.get)
    assert [(r.image, r.test) for r in got] == [("maven:3-eclipse-temurin-17", "bash -ec 'mvn -B test'"),
                                               ("python:3.9", "bash -ec 'python -m unittest'")]
    assert "requirements.txt" in got[1].setup


@pytest.mark.parametrize("files,image,test", [
    ({"project.clj": b"(defproject x)"}, "clojure:lein", "lein test"),
    ({"mix.exs": b""}, "elixir:1.17", "mix test"),
    ({"x.sln": b"", "src/x.csproj": b""}, "mcr.microsoft.com/dotnet/sdk:8.0", "dotnet test --no-build x.sln"),
    ({"Package.swift": b""}, "swift:6.0", "swift test"),
    ({"Project.toml": b"", "test/runtests.jl": b""}, "julia:1.11", "julia --project=. -e 'using Pkg; Pkg.test()'"),
    ({"DESCRIPTION": b"", "tests/testthat/test-a.R": b""}, "rocker/r-ver:4.4",
     "Rscript -e 'testthat::test_local(reporter=\"check\")'"),
    ({"composer.json": b"{}", "phpunit.xml.dist": b""}, "composer:2", "vendor/bin/phpunit"),
    ({"build.sbt": b""}, "eclipse-temurin:21", "sbt -batch test"),
    ({"dune-project": b""}, "ocaml/opam:debian-12-ocaml-5.2", "opam exec -- dune runtest"),
    ({"pubspec.yaml": b"name: x\n"}, "dart:stable", "dart test"),
    ({"pubspec.yaml": b"dependencies:\n  flutter:\n    sdk: flutter\n"}, "ghcr.io/cirruslabs/flutter:stable", "flutter test"),
    ({"x-1.0-1.rockspec": b""}, "buildpack-deps:noble", "busted"),
])
def test_candidates_by_ecosystem(files, image, test):
    from metaphi import codebase_recipes as cr
    got = cr.candidates(set(files), files.get)
    assert (got[0].image, got[0].test) == (image, test)


def test_ci_test_workflows_first():
    from metaphi import codebase_recipes as cr
    flow = (b"on: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
            b"      - uses: erlef/setup-beam@v1\n        with: {elixir-version: '1.16'}\n      - run: mix test\n")
    files = {".github/workflows/a-release.yml": flow.replace(b"mix test", b"mix test --only slow"),
             ".github/workflows/tests.yml": flow}
    got = cr.candidates(set(files), files.get)
    assert got[0].image == "elixir:1.16" and got[0].test == "bash -ec 'mix test'"


def test_find_keeps_going_past_a_thin_suite(tmp_path, monkeypatch):
    from metaphi import codebase_recipes as cr
    thin, full = cr.Recipe("true", "true", "thin", None), cr.Recipe("true", "true", "full", None)
    monkeypatch.setattr(cr, "candidates", lambda paths, read: [thin, full])
    counts = {"thin": 1, "full": 40}
    monkeypatch.setattr(cr, "_tried", lambda r, *a: (
        f"ran\ntaken. {counts[r.test]} tests counted (junit), {counts[r.test]} passed", "img", None, "junit"))
    paths = {f"tests/test_{i}.py" for i in range(8)}
    got, how = cr.find(lambda d: d.mkdir(parents=True), lambda p: None, paths, "", "docker", tmp_path, lambda s: None, 1.0)
    assert got.test == "full" and how["steps"] == 2
    counts["thin"] = 9
    got, how = cr.find(lambda d: d.mkdir(parents=True), lambda p: None, paths, "", "docker", tmp_path, lambda s: None, 1.0)
    assert got.test == "thin" and how["steps"] == 1


def test_python_unittest_after_pytest():
    from metaphi import codebase_recipes as cr
    files = {"pyproject.toml": b"[project]\nname='x'\n", "tests/test_core.py": b""}
    got = cr.candidates(set(files), files.get)
    assert [r.test for r in got][1:] == ["python -m unittest discover -s tests -t ."] and "pytest" in got[0].test


def test_dotnet_elixir_and_node_shims():
    from metaphi import codebase_recipes as cr
    files = {"src/App/App.csproj": b"<TargetFramework>net8.0</TargetFramework>", "global.json": b'{"sdk": {"version": "10.0.100"}}',
             "tests/App.Tests/App.Tests.csproj": b"<TargetFramework>net10.0</TargetFramework>"}
    got = cr.candidates(set(files), files.get)[0]
    assert got.image == "mcr.microsoft.com/dotnet/sdk:10.0" and got.test == "dotnet test --no-build tests/App.Tests/App.Tests.csproj"
    files = {"mix.exs": b'def project, do: [app: :x, elixir: "~> 1.18"]'}
    assert cr.candidates(set(files), files.get)[0].image == "elixir:1.18"
    files = {"package.json": b'{"scripts": {"test": "vitest"}}', "pnpm-lock.yaml": b""}
    got = cr.candidates(set(files), files.get)[0]
    assert "corepack enable --install-directory .kennet-bin" in got.setup and got.test.endswith("CI=true pnpm test")
    assert ".kennet-bin" in cr.hosted(got).build


def test_work_dir_is_a_git_repo_with_its_submodules(tmp_path):
    import subprocess

    from metaphi import codebase_checks as cc
    def git(*a, cwd):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "protocol.file.allow=always", *a],
                       cwd=cwd, check=True, capture_output=True)
    lib, top = tmp_path / "lib", tmp_path / "top"
    for d in (lib, top):
        d.mkdir()
        git("init", "-q", cwd=d)
    (lib / "a.c").write_text("int a;\n")
    git("add", "-A", cwd=lib)
    git("commit", "-qm", "lib", cwd=lib)
    (top / "main.c").write_text("int main;\n")
    git("submodule", "add", "-q", str(lib), "vendor/lib", cwd=top)
    git("commit", "-qam", "top", cwd=top)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=top, capture_output=True, text=True, check=True).stdout.strip()
    tree = cc._tree(top, sha)
    assert "vendor/lib" in tree.links and "vendor/lib" not in tree
    into = tmp_path / "w"
    cc._write(top, tree, into)
    assert (into / "vendor/lib/a.c").read_text() == "int a;\n" and (into / ".git").is_dir()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=into, capture_output=True, text=True, check=True).stdout.strip() == ""


def test_the_closing_line_says_the_build_ran_when_only_the_tests_failed():
    tries = [{"build": {"exit": 0, "seconds": 5},
              "test": {"exit": 1, "seconds": 6, "report": "summary", "reported": 10, "passed": 0}}]
    said = cli._rerun("./core", tries)
    assert "the build ran and 0 of 10 tests passed" in said and "No build or test found" not in said
    assert "No build or test found in ./core" in cli._rerun("./core", [])


@respx.mock
def test_a_request_modal_cut_off_is_sent_again_and_said_never_a_crash(monkeypatch):
    """Modal ends a request at 150 s with a 303 and no body: the profile is sent again (its reference is its hash),
    the retry is said with the request id, and an answer that is never JSON ends as `unavailable`, not `internal`."""
    said: list[str] = []
    monkeypatch.setattr(codebase_profile, "say", said.append)
    route = respx.post(f"{API}/codebase-profiles").mock(side_effect=[
        httpx.Response(303, headers={"request-id": "req_cut", "location": "/x"}),
        httpx.Response(200, json={"id": "cbp_0123456789abcdef", "object": "codebase_profile"})])
    assert codebase_profile.post(b"x", sleep=lambda s: None)["id"] == "cbp_0123456789abcdef"
    assert route.call_count == 2 and "req_cut" in said[0] and "cut off at HTTP 303" in said[0]
    respx.post(f"{API}/codebase-profiles").mock(return_value=httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(errors.APIError) as e:
        codebase_profile.post(b"x", sleep=lambda s: None)
    assert e.value.code == "unavailable" and e.value.retryable


@respx.mock
def test_a_signed_in_key_is_checked_from_its_saved_scopes_with_no_call(monkeypatch):
    """The sign-in says what its keys hold; the check before a profile reads that, and asks the server only for a key
    from elsewhere (METAPHI_API_KEY), whose scopes this machine does not know."""
    access = respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    monkeypatch.delenv("METAPHI_API_KEY", raising=False)
    key = config.KEY_PREFIX + "login"
    cfg = {"token": "t", "api_key": key, "api_key_from": "login", "scopes": [codebase_profile.SCOPE]}
    monkeypatch.setattr(config, "load", lambda: cfg)
    codebase_profile.key_ready()
    assert access.call_count == 0
    cfg["scopes"] = ["kennet:twin"]
    with pytest.raises(errors.AuthError) as e:
        codebase_profile.key_ready()
    assert e.value.code == "insufficient_scope" and e.value.next_step == "kennet login" and access.call_count == 0
    monkeypatch.setenv("METAPHI_API_KEY", config.KEY_PREFIX + "elsewhere")
    codebase_profile.key_ready()
    assert access.call_count == 1


@pytest.mark.parametrize("text,n", [
    ("[TestFixture]\npublic class A {\n  [Test]\n  public void B() {}\n  [Fact]\n  public void C() {}\n}\n", 2),
    ("[TestClass]\nclass A {\n  [TestMethod]\n  void B() {}\n}\n", 1),
    ("@ParameterizedTest\nvoid a() {}\n@Test\nvoid b() {}\n", 2),
    ("func testLogin() {}\nfunc TestX(t *testing.T) {}\n", 2),
    ("describe Foo do\n  it 'works' do\n  end\n  it \"also\" do\n  end\nend\n", 2),
    ("TEST_P(Suite, Case) {}\nTEST(A, B) {}\n", 2),
])
def test_test_fns_by_language(text, n):
    assert len(codebase_profile.TEST_FN.findall(text)) == n


def test_gaps_and_interface(tmp_path):
    r = tmp_path / "shop"
    (r / "tests").mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    (r / "cart.py").write_text("".join(f"line{i} = {i}\n" for i in range(40)))
    (r / "tests/test_cart.py").write_text("import cart\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "start")
    body = (r / "cart.py").read_text().split("\n")
    body[2], body[30] = "def checkout_total(): pass", "line30 = 31"
    (r / "cart.py").write_text("\n".join(body))
    (r / "tests/test_cart.py").write_text("import cart\nassert cart.checkout_total() is None\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "total")
    c1, c2 = codebase_profile.document([str(r)], say=lambda s: None)["repos"][0]["commits"]
    src = next(x for x in c2["files"] if x["hunks"] == 2)
    assert src["gaps"] == [27] and c1["files"][0]["gaps"] == []
    assert c2["iface_idents"] == 1 and c1["iface_idents"] == 0
