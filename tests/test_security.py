import io
import json
import os
import stat
from pathlib import Path

import httpx
import pytest
import respx

from metaphi import accounts, cli, config, twin

A = config.DEFAULT_ACCOUNTS


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    for var in ("METAPHI_API_KEY", "METAPHI_ACCOUNTS", "METAPHI_API", "METAPHI_ALLOW_CUSTOM_HOST"):
        monkeypatch.delenv(var, raising=False)
    config._warned.clear()
    return tmp_path


@pytest.mark.parametrize(
    "url", ["https://app.metaphi.ai", "https://metaphi.ai/x", "https://a.b.metaphi.ai/mcp/", "https://APP.METAPHI.AI."]
)
def test_metaphi_hosts_over_https_pass(url):
    assert config.check_url(url, "u") == url


@pytest.mark.parametrize(
    "url",
    [
        "http://app.metaphi.ai",
        "https://evil.test",
        "https://metaphi.ai.evil.test",
        "https://evilmetaphi.ai",
        "https://app.metaphi.ai@evil.test/",
        "https://user:pw@app.metaphi.ai/",
        "ftp://app.metaphi.ai",
        "app.metaphi.ai",
        "",
        None,
        7,
    ],
)
def test_other_hosts_and_schemes_are_refused(url):
    with pytest.raises(config.Error):
        config.check_url(url, "u")


def test_custom_host_needs_the_opt_in_and_still_needs_https(monkeypatch, capsys):
    monkeypatch.setenv("METAPHI_ACCOUNTS", "https://accounts.example.test")
    with pytest.raises(config.Error, match="METAPHI_ALLOW_CUSTOM_HOST"):
        config.accounts_url()
    monkeypatch.setenv("METAPHI_ALLOW_CUSTOM_HOST", "1")
    assert config.accounts_url() == "https://accounts.example.test"
    assert "warning: METAPHI_ACCOUNTS" in capsys.readouterr().err
    assert config.check_url("http://localhost:8000/", "u")
    with pytest.raises(config.Error):
        config.check_url("http://accounts.example.test", "u")


def test_a_bad_endpoint_is_an_error_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("METAPHI_ACCOUNTS", "http://app.metaphi.ai")
    config.save({"token": "tok"})
    assert cli.main(["credits"]) == 1
    assert "must be https" in capsys.readouterr().err


def test_default_hosts_say_nothing(capsys):
    config.accounts_url(), config.api_url()
    assert capsys.readouterr().err == ""


def test_an_api_off_metaphi_is_refused_before_any_bytes_leave(home, monkeypatch):
    trace = home / "trace.json"
    trace.write_text("{}")
    monkeypatch.setenv("METAPHI_API", "https://collector.example.test/v1")
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    with respx.mock(assert_all_called=False) as m:
        put = m.post("https://collector.example.test/v1/twins").respond(202)
        with pytest.raises(config.Error, match="METAPHI_ALLOW_CUSTOM_HOST"):
            twin.twin(trace, say=lambda s: None)
    assert not put.called


def test_a_key_that_is_not_an_organization_key_never_leaves(home, monkeypatch):
    trace = home / "trace.json"
    trace.write_text("{}")
    monkeypatch.setenv("METAPHI_API_KEY", "sk_live_other")
    with respx.mock(assert_all_called=False) as m:
        made = m.post(f"{config.DEFAULT_API}/twins").respond(202)
        with pytest.raises(config.Error, match="not an organization key"):
            twin.twin(trace, say=lambda s: None)
    assert not made.called


@respx.mock
def test_a_non_json_token_answer_is_an_error(browser):
    respx.post(f"{A}/oauth/token").mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(accounts.Error) as e:
        accounts.login(say=lambda s: None)
    assert (e.value.message, e.value.code, e.value.retryable) == (
        "the service answered HTTP 502; try again in a minute",
        "service_unavailable",
        True,
    )


@respx.mock
def test_the_sign_in_page_is_always_the_accounts_host_and_the_listener_this_machine(browser):
    respx.post(f"{A}/oauth/token").respond(json={"access_token": "tok", "token_type": "Bearer"})
    respx.post(f"{A}/keys/cli").respond(json={"email": "alice@example.test"})
    accounts.login(say=lambda s: None)
    assert browser.opened[0].startswith(f"{A}/oauth/authorize?")
    assert browser.asked["redirect_uri"].startswith("http://127.0.0.1:")


@respx.mock
def test_a_token_the_server_will_not_take_is_no_sign_in(browser):
    respx.post(f"{A}/oauth/token").respond(json={"access_token": "tok", "token_type": "Bearer"})
    respx.post(f"{A}/keys/cli").respond(401, json={"detail": "unknown token"})
    with pytest.raises(accounts.Error) as e:
        accounts.login(say=lambda s: None)
    assert e.value.code == "login_refused" and "token" not in config.load()


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_the_file_is_600_in_a_700_directory_and_written_whole(home):
    config.save({"api_key": "mai_sk_a", "email": "alice@example.test"})
    assert _mode(config.path()) == 0o600 and _mode(config.home()) == 0o700
    assert config.load() == {"api_key": "mai_sk_a", "email": "alice@example.test"}
    assert [p.name for p in config.home().iterdir()] == ["config.json"]


def test_save_replaces_a_symlink_instead_of_writing_through_it(home):
    config.home().mkdir(mode=0o700, parents=True)
    target = home / "elsewhere"
    target.write_text("keep")
    config.path().symlink_to(target)
    config.save({"token": "t"})
    assert target.read_text() == "keep" and not config.path().is_symlink()


@pytest.mark.parametrize("text", ["{not json", "[1, 2]"])
def test_a_corrupt_file_is_an_error_not_an_empty_sign_in(home, text, capsys):
    config.home().mkdir(parents=True)
    config.path().write_text(text)
    with pytest.raises(config.Error, match="config.json"):
        config.load()
    assert cli.main(["whoami"]) == 1 and "config.json" in capsys.readouterr().err


def test_everything_is_in_the_config_file_and_no_keychain_is_asked(home):
    config.save({"token": "tok", "api_key": "mai_sk_k", "test_api_key": "mai_sk_test_k", "email": "alice@example.test"})
    assert json.loads(config.path().read_text()) == {
        "token": "tok",
        "api_key": "mai_sk_k",
        "test_api_key": "mai_sk_test_k",
        "email": "alice@example.test",
    }
    assert config.path().stat().st_mode & 0o777 == 0o600 and config.load()["token"] == "tok"
    assert "keyring" not in Path(config.__file__).read_text()


def test_login_with_api_key_reads_stdin_not_argv(home, monkeypatch, capsys):
    config.save({"api_key": "mai_sk_old", "api_key_from": "login"})
    monkeypatch.setattr("sys.stdin", io.StringIO("mai_sk_piped\n"))
    assert cli.main(["login", "--with-api-key"]) == 0
    assert config.load() == {"api_key": "mai_sk_piped"}
    assert "warning" not in capsys.readouterr().err


def test_login_with_api_key_prompts_without_echo_on_a_terminal(home, monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty())
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "mai_sk_typed")
    assert cli.main(["login", "--with-api-key"]) == 0
    assert config.load()["api_key"] == "mai_sk_typed"


def test_login_refuses_what_is_not_a_key(home, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("hunter2\n"))
    assert cli.main(["login", "--with-api-key"]) == 1
    assert "api_key" not in config.load()


def test_a_key_on_argv_is_refused(home, capsys):
    with pytest.raises(SystemExit):
        cli.main(["login", "--api-key", "mai_sk_argv"])
    assert "api_key" not in config.load()
