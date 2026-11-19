"""The model pass makes no network call: onnxruntime's builds carry a telemetry client, and it must stay silent.

Both tests run the real model, so they run only where it is already cached (`kennet codeval` puts it in
~/.cache/kennet/deid on first use); they never download it. The first refuses every Python-level connect and name
lookup; the second runs the pass in a child process under a connect/sendto/getaddrinfo interposer compiled here,
which catches calls from native code too, such as a telemetry client inside onnxruntime.
"""

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from metaphi import deid, deid_model

LINE = "fixture owner Maria Gonzalez signed the release notes"


def _cached() -> bool:
    files = deid_model.cache() / deid_model.NAME
    model, tokenizer = files / "model_quint8.onnx", files / "tokenizer.json"
    if not (model.is_file() and model.stat().st_size == deid_model.MODEL[1]):
        return False
    if not (tokenizer.is_file() and tokenizer.stat().st_size == deid_model.TOKENIZER[1]):
        return False
    if importlib.util.find_spec("onnxruntime") and importlib.util.find_spec("tokenizers"):
        return True
    runtime = deid_model.cache() / f"runtime-cp312-{deid_model._target()}" / ".complete"
    return sys.version_info[:2] == (3, 12) and runtime.is_file()


needs_model = pytest.mark.skipif(not _cached(), reason="the model and its runtime are not cached on this machine")


@needs_model
def test_the_model_pass_runs_with_every_connect_refused(monkeypatch):
    monkeypatch.delenv("KENNET_DEID_MODEL")
    monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)
    monkeypatch.setattr(deid_model, "_loaded", None)
    tried: list[object] = []

    def refused(where: object) -> OSError:
        tried.append(where)
        return PermissionError(f"network refused in this test: {where}")

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        raise refused(address)

    def connect_ex(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect_ex(self, address)
        raise refused(address)

    def getaddrinfo(host, *a, **k):
        raise refused(host)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", lambda address, *a, **k: (_ for _ in ()).throw(refused(address)))

    silenced: list[bool] = []
    real_runtime = deid_model._runtime

    def runtime(say):
        ort, tokenizers, np = real_runtime(say)
        assert os.environ.get("ORT_DISABLE_TELEMETRY") == "1"
        real = ort.disable_telemetry_events
        monkeypatch.setattr(ort, "disable_telemetry_events", lambda: silenced.append(True) or real())
        return ort, tokenizers, np

    monkeypatch.setattr(deid_model, "_runtime", runtime)
    model, said = deid_model.load()
    assert model is not None, said
    scrub = deid.Scrubber(deid.Known(), model)
    assert scrub.text(LINE) == "fixture owner [name] signed the release notes"
    assert silenced == [True]
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
    assert tried == []


SHIM = r"""
#define _GNU_SOURCE
#include <errno.h>
#include <netdb.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#ifdef __APPLE__
#define REAL(name) name
#define WRAP(name) kennet_##name
#else
#include <dlfcn.h>
#define WRAP(name) name
#endif

static void said(const char *what, int family) {
  const char *path = getenv("KENNET_CONNECTS");
  FILE *f = path ? fopen(path, "a") : NULL;
  if (f) { fprintf(f, "%s %d\n", what, family); fclose(f); }
}

int WRAP(connect)(int s, const struct sockaddr *a, socklen_t l) {
  if (a && a->sa_family == AF_UNIX) {
#ifdef __APPLE__
    return connect(s, a, l);
#else
    int (*real)(int, const struct sockaddr *, socklen_t) = dlsym(RTLD_NEXT, "connect");
    return real(s, a, l);
#endif
  }
  said("connect", a ? a->sa_family : -1);
  errno = EACCES;
  return -1;
}

ssize_t WRAP(sendto)(int s, const void *b, size_t n, int fl, const struct sockaddr *a, socklen_t l) {
  if (!a || a->sa_family == AF_UNIX) {
#ifdef __APPLE__
    return sendto(s, b, n, fl, a, l);
#else
    ssize_t (*real)(int, const void *, size_t, int, const struct sockaddr *, socklen_t) = dlsym(RTLD_NEXT, "sendto");
    return real(s, b, n, fl, a, l);
#endif
  }
  said("sendto", a->sa_family);
  errno = EACCES;
  return -1;
}

int WRAP(getaddrinfo)(const char *h, const char *p, const struct addrinfo *hint, struct addrinfo **res) {
  said("getaddrinfo", 0);
  return EAI_FAIL;
}

#ifdef __APPLE__
__attribute__((used)) static struct { const void *n, *o; } interposers[] __attribute__((section("__DATA,__interpose")))
    = {{(const void *)kennet_connect, (const void *)connect},
       {(const void *)kennet_sendto, (const void *)sendto},
       {(const void *)kennet_getaddrinfo, (const void *)getaddrinfo}};
#endif
"""

CHILD = f"""
import socket
from metaphi import deid, deid_model
model, said = deid_model.load()
print(said)
print(deid.Scrubber(deid.Known(), model).text({LINE!r}) if model else "")
open(__import__("os").environ["KENNET_CONNECTS"], "a").write("pass done\\n")
for probe in (lambda: socket.getaddrinfo("example.com", 443), lambda: socket.socket().connect(("192.0.2.1", 9))):
    try:
        probe()
    except OSError:
        pass
"""


@needs_model
@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the interposer is for macOS and Linux")
@pytest.mark.skipif(not shutil.which("cc"), reason="no C compiler to build the interposer")
def test_no_native_code_reaches_the_network_during_the_model_pass(tmp_path):
    source, shim, log = tmp_path / "shim.c", tmp_path / "shim.so", tmp_path / "connects"
    source.write_text(SHIM)
    flags = ["-shared"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
    subprocess.run(["cc", *flags, "-o", str(shim), str(source), "-ldl"], check=True, timeout=60, capture_output=True)
    env = {k: v for k, v in os.environ.items() if k not in ("KENNET_DEID_MODEL", "ORT_DISABLE_TELEMETRY")}
    env.update(
        KENNET_CONNECTS=str(log),
        XDG_CACHE_HOME=str(deid_model.cache().parents[1]),
        PYTHONPATH=str(Path(deid_model.__file__).parents[1]),
        **({"DYLD_INSERT_LIBRARIES": str(shim)} if sys.platform == "darwin" else {"LD_PRELOAD": str(shim)}),
    )
    child = [sys.executable, "-c", CHILD]
    got = subprocess.run(child, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert got.returncode == 0, got.stderr
    said, scrubbed = got.stdout.splitlines()[:2]
    assert said.startswith("ran"), said
    assert "[name]" in scrubbed and "Maria" not in scrubbed
    calls = log.read_text().splitlines()
    # Nothing before the pass ended; after it, the two probes prove the interposer was live, and nothing else came.
    assert calls[0] == "pass done", calls
    assert [c.split()[0] for c in calls[1:]] == ["getaddrinfo", "connect"], calls
