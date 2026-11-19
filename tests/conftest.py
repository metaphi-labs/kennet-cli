import pytest


@pytest.fixture(autouse=True)
def _no_release_look(monkeypatch):
    monkeypatch.setenv("KENNET_UPDATED", "test")


@pytest.fixture(autouse=True)
def _no_model_download(monkeypatch):
    monkeypatch.setenv("KENNET_DEID_MODEL", "off")


@pytest.fixture(autouse=True)
def _with_display(monkeypatch):
    from metaphi import accounts

    monkeypatch.setattr(accounts, "headless", lambda: False)


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: True)


class Browser:
    def __init__(self) -> None:
        self.answer: dict | None = {"code": "c0de"}
        self.opened: list[str] = []
        self.asked: dict[str, str] = {}
        self.sent_on: list[str] = []
        self.going: list = []

    def settle(self) -> None:
        for t in self.going:
            t.join(5)

    def open(self, url, *a, **k):
        import threading
        import urllib.error
        import urllib.request
        from urllib.parse import parse_qsl, urlencode, urlsplit

        self.opened.append(url)
        self.asked = dict(parse_qsl(urlsplit(url).query))
        if self.answer is None or "redirect_uri" not in self.asked:
            return True
        to = f"{self.asked['redirect_uri']}?{urlencode({**self.answer, 'state': self.asked.get('state', '')})}"

        class Stay(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        def go():
            try:
                urllib.request.build_opener(Stay).open(to, timeout=5)
            except urllib.error.HTTPError as e:
                self.sent_on.append(e.headers.get("Location"))

        t = threading.Thread(target=go, daemon=True)
        self.going.append(t)
        t.start()
        return True


@pytest.fixture
def browser(monkeypatch):
    import webbrowser

    b = Browser()
    monkeypatch.setattr(webbrowser, "open", b.open)
    return b


FAKE_ENGINE = r"""
import json, os, shutil, subprocess, sys
from pathlib import Path

state = Path(STATE)
args = sys.argv[1:]


def held():
    f = state / "containers.json"
    return json.loads(f.read_text()) if f.is_file() else {}


def keep(got):
    (state / "containers.json").write_text(json.dumps(got))


with (state / "calls.jsonl").open("a") as f:
    f.write(json.dumps(args) + "\n")
verb = args[0] if args else ""
if verb == "info":
    print("{}")
elif verb == "run" and "-d" in args:
    name = args[args.index("--name") + 1]
    mounts = {}
    for i, a in enumerate(args):
        if a == "-v":
            host, _, inside = args[i + 1].rpartition(":")
            mounts[inside] = host
    got = held()
    got[name] = mounts
    keep(got)
    print(name)
elif verb == "exec":
    rest, env = args[1:], {}
    while rest and rest[0].startswith("-"):
        if rest[0] == "-e":
            k, _, v = rest[1].partition("=")
            env[k] = v
        rest = rest[2:]
    name, command = rest[0], rest[1:]
    mounts = held().get(name)
    if mounts is None:
        sys.exit(1)
    line = " ".join(command)
    if "kill -9 -1" in line or command[:1] == ["chmod"]:
        sys.exit(0)
    junit = mounts.get("/kennet/junit", "")
    if "rm -rf /kennet/junit" in line:
        for x in Path(junit).iterdir():
            shutil.rmtree(x) if x.is_dir() else x.unlink()
        sys.exit(0)
    env = {
        k: v.replace("/kennet/junit", junit)
        for k, v in env.items()
        if not k.endswith("_DIR") and "/kennet/cache" not in v
    }
    sys.exit(subprocess.run(command, cwd=mounts["/work"], env={**os.environ, **env}).returncode)
elif verb == "rm":
    got = held()
    for a in args[1:]:
        got.pop(a, None)
    keep(got)
"""


@pytest.fixture
def engine(tmp_path, monkeypatch):
    import sys

    from metaphi import codebase_setup

    state = tmp_path / "engine"
    state.mkdir()
    path = state / "fake-engine"
    path.write_text(f"#!{sys.executable}\nSTATE = {str(state)!r}\n" + FAKE_ENGINE)
    path.chmod(0o755)
    monkeypatch.setattr(codebase_setup, "engine", lambda: str(path))
    codebase_setup.limits.cache_clear()
    return path
