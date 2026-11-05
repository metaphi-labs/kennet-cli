import gzip
import json
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from metaphi import cli, codebase_profile, config

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
    doc = codebase_profile.document([str(repo)], hide_names=True, say=lambda s: None)
    sent = json.dumps(doc)
    for word in SECRET:
        assert word not in sent, word
    r = doc["repos"][0]
    assert r["name"] == "repo-1" and len(r["commits"]) == 3


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


@respx.mock
def test_send(repo, capsys):
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    route = respx.post(f"{API}/codebase-profiles").mock(return_value=httpx.Response(200, json={"id": "cbp_8f2k", "object": "codebase_profile"}))
    assert cli.main(["profile-codebase", str(repo)]) in (0, None)
    out = capsys.readouterr().out
    assert codebase_profile.NOTICE in out and "Done. 1 repository shared. Reference: cbp_8f2k" in out
    sent = route.calls[0].request
    assert sent.headers["content-encoding"] == "gzip" and json.loads(gzip.decompress(sent.content))["schema"] == codebase_profile.SCHEMA


@respx.mock
def test_dry_run(repo, capsys, tmp_path):
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    route = respx.post(f"{API}/codebase-profiles")
    assert cli.main(["profile-codebase", str(repo), "--dry-run", "-o", "d.json"]) in (0, None)
    assert not route.called and json.loads((tmp_path / "d.json").read_text())["repos"]


@respx.mock
def test_send_fails_keeps_file(repo, capsys, tmp_path):
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/codebase-profiles").mock(side_effect=httpx.ConnectError("down"))
    code = cli.main(["profile-codebase", str(repo), "--json"])
    assert code == 4
    err = json.loads(capsys.readouterr().out)["error"]
    assert "--send" in err["next_step"] and list(tmp_path.glob("kennet-codebase-profile-*.json.gz"))


def test_unknown_extension(repo):
    (repo / "plan.falcon").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "x")
    assert "falcon" not in json.dumps(codebase_profile.document([str(repo)], hide_names=True, say=lambda s: None))


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
def test_send_file(repo, capsys, tmp_path):
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    route = respx.post(f"{API}/codebase-profiles").mock(return_value=httpx.Response(200, json={"id": "cbp_1", "object": "codebase_profile"}))
    cli.main(["profile-codebase", str(repo), "--dry-run", "-o", "d.json"])
    (tmp_path / "id_rsa").write_text("PRIVATE KEY")
    capsys.readouterr()
    assert cli.main(["profile-codebase", "--send", "id_rsa", "--json"]) == 1 and not route.called
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_a_document"
    assert cli.main(["profile-codebase", "--send", "d.json"]) in (0, None) and route.called
    assert codebase_profile.NOTICE in capsys.readouterr().out


@respx.mock
def test_refused_keeps_file(repo, capsys, tmp_path):
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/codebase-profiles").mock(return_value=httpx.Response(400, json={"error": {"type": "invalid_request_error", "code": "bad_parameter", "message": "no"}}))
    cli.main(["profile-codebase", str(repo), "--json"])
    assert "--send" in json.loads(capsys.readouterr().out)["error"]["next_step"]


@respx.mock
def test_key_checked_first(repo, capsys, monkeypatch):
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(403, json={"error": {
        "type": "authentication_error", "code": "insufficient_scope", "retryable": False, "request_id": "r",
        "message": "this API key does not hold access to codebase profiles"}}))
    monkeypatch.setattr(codebase_profile, "compute", lambda *a: pytest.fail("computed"))
    assert cli.main(["profile-codebase", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "kennet:codebase_profile:write" in err and "kennet login" in err


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
    with codebase_profile.profiled([str(r)], hide_names=True, say=say) as (doc, kept):
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
    for word in ("hunter2", "test_mul", "calc", "falcon"):
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
    _checked(tested, build="echo FALCON-LIVE; exit 3", stream=True, runs=1)
    assert "    | FALCON-LIVE" in capfd.readouterr().err


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


@respx.mock
def test_deep(tested, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    profile = respx.post(f"{API}/codebase-profiles").mock(return_value=httpx.Response(200, json={"id": "cbp_" + "1" * 16}))
    picked = respx.post(f"{API}/codebase-profiles/cbp_{'1' * 16}/sample").mock(
        return_value=httpx.Response(200, json={"runs": 1, "repos": [[2]]}))
    checks = respx.post(f"{API}/codebase-profiles/cbp_{'1' * 16}/checks").mock(return_value=httpx.Response(503))
    logs = respx.post(f"{API}/codebase-profiles/cbp_{'1' * 16}/logs").mock(return_value=httpx.Response(200, json={}))
    assert cli.main(["profile-codebase", str(tested), "--test", "true", "--deep"]) != 0
    assert not logs.called
    assert profile.called and picked.called and checks.called
    sent = json.loads(gzip.decompress(checks.calls[-1].request.content))
    assert [c["commit"] for c in sent["repos"][0]["commits"]] == [2] and sent["repos"][0]["runs"] == 1
    kept = next(tmp_path.glob("kennet-codebase-profile-*.json.gz"))
    assert f"--send {kept}" in capsys.readouterr().err
    checks.mock(return_value=httpx.Response(200, json={"id": "cbp_" + "1" * 16}))
    calls = profile.call_count
    assert cli.main(["profile-codebase", "--send", str(kept)]) in (0, None)
    assert profile.call_count == calls and json.loads(gzip.decompress(checks.calls[-1].request.content)) == sent


@respx.mock
def test_test_runs_history(tested, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ref = "cbp_" + "3" * 16
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    respx.post(f"{API}/codebase-profiles").mock(return_value=httpx.Response(200, json={"id": ref}))
    picked = respx.post(f"{API}/codebase-profiles/{ref}/sample").mock(
        return_value=httpx.Response(200, json={"runs": 1, "repos": [[2]]}))
    setup = respx.post(f"{API}/codebase-profiles/{ref}/setup")
    checks = respx.post(f"{API}/codebase-profiles/{ref}/checks").mock(return_value=httpx.Response(200, json={"id": ref}))
    logs = respx.post(f"{API}/codebase-profiles/{ref}/logs").mock(return_value=httpx.Response(503))
    assert cli.main(["profile-codebase", str(tested), "--test", "echo SAID-BY-TESTS"]) != 0
    assert picked.called and not setup.called and checks.called
    sent = json.loads(gzip.decompress(checks.calls[-1].request.content))["repos"][0]
    assert [c["commit"] for c in sent["commits"]] == [2] and sent["runs"] == 1
    assert sent["tip"]["tests"][0]["exit"] == 0 and sent["test_given"]
    names, said = _unpacked(logs.calls[-1].request.content)
    assert logs.calls[-1].request.headers["content-type"] == "application/gzip"
    assert said["kennet-logs.json"]["profile"] == ref and "0/000-tip.test-1.log" in names
    assert not any(tested.name in n for n in names)
    assert b"SAID-BY-TESTS" in said["0/000-tip.test-1.log"]
    kept = next(tmp_path.glob("kennet-codebase-logs-*.tar.gz"))
    assert (tmp_path / "kennet-test-logs" / ref / tested.name / "000-tip.test-1.log").exists()
    logs.mock(return_value=httpx.Response(200, json={"id": ref, "object": "codebase_profile_logs"}))
    assert cli.main(["profile-codebase", "--send", str(kept)]) in (0, None)
    assert logs.calls[-1].request.content == kept.read_bytes()


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
    assert codebase_profile.readable(codebase_profile.logs_packed(root, "cbp_" + "5" * 16, []))[2] == "logs"


@respx.mock
def test_default_runs_nothing(repo, monkeypatch):
    from metaphi import codebase_checks, codebase_setup
    monkeypatch.setattr(codebase_checks, "run", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(codebase_setup, "find", lambda *a, **k: 1 / 0)
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    route = respx.post(f"{API}/codebase-profiles").mock(return_value=httpx.Response(200, json={"id": "cbp_" + "4" * 16}))
    assert cli.main(["profile-codebase", str(repo)]) in (0, None) and route.call_count == 1


@respx.mock
def test_flag_errors(tested, capsys):
    respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    for argv in (["profile-codebase", str(tested), "--build", "make"],
                 ["profile-codebase", "--send", "x.json", "--test", "make"],
                 ["profile-codebase", str(tested), "--dry-run", "--test", "make"],
                 ["profile-codebase", str(tested), "--dry-run", "--deep"]):
        assert cli.main(argv) != 0
    said = capsys.readouterr().err
    assert "build_without_test" in said and "send_with_test" in said and "dry_run_with_test" in said


@respx.mock
def test_dry_run_needs_login(repo, capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("METAPHI_API_KEY")
    access = respx.get(f"{API}/codebase-profiles/access").mock(return_value=httpx.Response(204))
    assert cli.main(["profile-codebase", str(repo), "--dry-run", "--json"]) != 0
    assert not (tmp_path / "kennet-codebase-profile.json").exists() and not access.called
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_" + "a" * 43)
    assert cli.main(["profile-codebase", str(repo), "--dry-run"]) in (0, None) and access.called


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
def test_setup(tested, capsys, tmp_path,
                                                                                         monkeypatch):
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
    assert [(s["era"], s["worked"], s["steps"], s["container"], s["tokens"]) for s in sent["setup"]] == [
        (None, True, 2, False, 220)]
    assert sent["tip"]["tests"][0]["reported"] == 2 and sent["tip"]["tests"][0]["passed"] == 2
    assert [c["commit"] for c in sent["commits"]] == [2] and sent["commits"][0]["fail_to_pass"] == 1
    assert "pytest" not in json.dumps(sent) and "ls tests" not in json.dumps(sent)
    from metaphi.steps import NAMES
    assert {"profile", "setup", "setup_model", "setup_proposals", "tip", "commits", "build", "test"} <= set(
        sent["seconds"]) <= NAMES and all(isinstance(v, int) for v in sent["seconds"].values())
    steps = [json.loads(x)["step"] for x in (tmp_path / "kennet-test-logs" / ("cbp_" + "2" * 16) / "falcon_calc" / "steps.jsonl").open()]
    assert "setup_model" in steps and "build" in steps
    logs = tmp_path / "kennet-test-logs" / ("cbp_" + "2" * 16) / "falcon_calc"
    assert "pytest" in (logs / "setup" / "recipe.json").read_text()
    out = capsys.readouterr().out
    assert "running on this machine" in out and "000-tip build ok" in out and "0 of 2 failed" in out


@respx.mock
def test_setup_fails(tested, monkeypatch, tmp_path):
    from metaphi import codebase_setup
    monkeypatch.setattr(codebase_setup, "engine", lambda: None)
    _, _, sent = _no_commands(tested, [_said(("give_up", {"why": "license", "reason": "needs a licensed compiler"}))], first=2)
    assert sent["setup"][0]["worked"] is False and "tip" not in sent
    assert sent["setup"][0]["why"] == "gave_up_license"
    assert sent["commits"] == [{"commit": 2, "broke": True}, {"commit": 3, "broke": True}]
    assert "licensed" not in json.dumps(sent)
    assert "licensed" in (tmp_path / "kennet-test-logs" / ("cbp_" + "2" * 16) / "falcon_calc" / "setup" / "gave-up.txt").read_text()


def test_recipe_edits_files():
    from metaphi import codebase_setup
    d = _dirs()
    (d[0] / "configure.ac").write_text("AM_INIT\n")
    said, kept, why, _ = codebase_setup._tried(codebase_setup.Recipe("", "echo XX > configure.ac", "true"),
                                               None, d, "", d[0].parent, 1, 1, *_runners())
    assert kept is False and "configure.ac" in said and why == "tracked_file_changed"


def test_recipe_build_fails():
    from metaphi import codebase_setup
    said, kept, why, _ = codebase_setup._tried(codebase_setup.Recipe("", "exit 3", "true"), None, _dirs(), "",
                                               _dirs()[0], 1, 1, *_runners())
    assert kept is False and "exit: 3" in said and "build did not pass" in said and why == "build_failed"


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
    doc = codebase_profile.document([str(planted)], hide_names=True, say=lambda s: None)
    rows = [f for f in doc["repos"][0]["files"] if f["at_tip"]]
    assert [f["ops"] for f in rows if "ops" in f] == [{"cmp": 1, "logic": 0, "arith": 2, "bool": 0}]
    assert "hunter2" not in json.dumps(doc)


def test_breaks(planted):
    import sys

    from metaphi import codebase_checks
    plan = codebase_checks.Plan(test=PYTEST.format(py=sys.executable))
    with codebase_profile.profiled([str(planted)], hide_names=True, say=lambda s: None) as (_doc, kept):
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
    for word in ("hunter2", "sums", "test_add", "assert"):
        assert word not in sent, word
    assert not any("assert" in line for line in lines) and sum("-tip build ok (" in x for x in lines) == 3
