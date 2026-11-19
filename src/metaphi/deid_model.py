"""The model pass of de-identification: a small local model that finds names and other personal data in free text.

The model is knowledgator/gliner-pii-edge-v1.0 (Apache-2.0; GLiNER on a 32M-parameter encoder, uint8 ONNX, 46 MB).
Nothing of it ships in kennet: on first use the model, its tokenizer and, where this Python lacks them, the wheels
of its runtime (onnxruntime, tokenizers, numpy) are downloaded from pinned URLs, checked against pinned sha256 sums
and kept in the cache. Where any step fails the pass does not run, and the preview says so; the other passes and the
check before sending run either way. KENNET_DEID_MODEL=off turns it off.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import secrets
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from metaphi import config

REVISION = "9b7f39b0a2da971a5beea78d35f1539d4009c891"
HF = f"https://huggingface.co/knowledgator/gliner-pii-edge-v1.0/resolve/{REVISION}/"
MODEL = (HF + "onnx/model_quint8.onnx", 45_820_894, "988acb03456b26e2d9f2521016d820310c2ed64deb4a846297d3289f0c2eb7e4")
TOKENIZER = (HF + "tokenizer.json", 3_583_593, "84b3a9b18f04a0ccd03b72d9f871b7e0bec40fd7021ef50bc30a7c3693c11205")
NAME = "gliner-pii-edge-v1.0"

_PYPI = "https://files.pythonhosted.org/packages/"
RUNTIME: dict[str, list[tuple[str, int, str]]] = {
    "darwin-arm64": [
        (
            "6f/a4/480cd367bc135dd9951c89d461cc6af6be9302cd2ce91e231f6cb0e7ee1b/onnxruntime-1.32.0-cp312-cp312-macosx_14_0_arm64.whl",
            22_519_308,
            "d52510bbbcc880c13b826f7d75cdf33ac5cf0bb864d960000fc29ce73f0cb83e",
        ),
        (
            "8a/92/6de34324a266b73c04ba977db5864b9016500076b26f09078a565f527d34/numpy-2.5.5-cp312-cp312-macosx_14_0_arm64.whl",
            5_513_680,
            "a3856a97e48ed8371c680fb04120b99fbcc29f2076ebe6dc3deb34f77947c5ca",
        ),
        (
            "b2/72/01e49f032bb346e5aaf06c10c74fe8aeec847173adbadd66eb7c53054bf2/tokenizers-0.23.3-cp310-abi3-macosx_11_0_arm64.whl",
            3_101_381,
            "68649e97d5b43c44c031d8d848874a6eecae8f8fe40ea989aa777a5a83aca716",
        ),
    ],
    "linux-x86_64": [
        (
            "a3/81/55845186c20cba59f614991f4065dea148e48eeb9749893768c85ce64327/onnxruntime-1.32.0-cp312-cp312-manylinux_2_28_x86_64.whl",
            27_780_211,
            "99e1074c4b2197accd5073052a465361be17b89c093b8ffb6e6bd8f986c52df8",
        ),
        (
            "99/61/522b973ba38987b22a4a71beaed2ea6ac9fd4f687baaa2bc9a45100625e0/numpy-2.5.5-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
            15_720_599,
            "704d819265e5d1064d932968e9ee85fff2103ccc78c05b878c06943ba27b8507",
        ),
        (
            "3a/40/6706b82693715581457c6d5423eaa7faae576bb0526c5738a57085eb4449/tokenizers-0.23.3-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
            3_396_193,
            "376851d22bcf9d650a5c3090bb83e6cf9e895fbf0595369fa4cd43c1f69b5f87",
        ),
    ],
    "linux-arm64": [
        (
            "4a/8f/1e43fb994513c7c7a9c6965272ef89d963c1bd4080993a6267bf913701d1/onnxruntime-1.32.0-cp312-cp312-manylinux_2_28_aarch64.whl",
            23_196_724,
            "e36c187006e7a7c8b8d6d57f1a209dd63f52430dca6c282b177718ad6b841054",
        ),
        (
            "c2/22/82184e9a5b50a4a298e9f8fe0bd8cfc483672dbfea8a3b57c78e2376b139/numpy-2.5.5-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl",
            15_284_117,
            "a0750f2156d53446caca85663d0b65d67214e1664b9050454a731ea1418d176a",
        ),
        (
            "15/fc/ae987741829b1cd547668c4c94be732ae3eefd1d74344e64c3d2ca714acd/tokenizers-0.23.3-cp310-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
            3_519_944,
            "ec82e80e65a862275b97c3d90b7a523df8d9519ee48aeb4e9625b2cc909274e0",
        ),
    ],
}
"""Wheels of the model's runtime for the Python the kennet binary carries (3.12), by platform: their paths on PyPI's
file host, sizes and sha256 sums. onnxruntime publishes no wheel for Intel Macs."""

# onnxruntime's macOS and Linux builds carry Microsoft's 1DS telemetry client; ORT_DISABLE_TELEMETRY, set before
# it is imported, keeps it off, and disable_telemetry_events() turns off what remains.
DOWNLOAD_HOSTS = re.compile(r"^(?:huggingface\.co|[a-z0-9.-]+\.hf\.co|files\.pythonhosted\.org|pypi\.org)$")
LABELS = ("person",)
THRESHOLD = 0.4
MAX_WORDS = 384
BATCH = 16
WORD = re.compile(r"\w+(?:[-_]\w+)*|\S")


def cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "kennet" / "deid"


class Refused(Exception):
    pass


def _hosts(request: httpx.Request) -> None:
    if request.url.scheme != "https" or not DOWNLOAD_HOSTS.match(request.url.host):
        raise Refused(f"a download went to {request.url.host}")


def fetch(url: str, size: int, sha256: str, into: Path, client: httpx.Client | None = None) -> Path:
    """The file at `url`, from the cache when it is there and whole, else downloaded and checked. Raises Refused."""
    into.parent.mkdir(parents=True, exist_ok=True)
    if into.is_file() and into.stat().st_size == size and _sha256(into) == sha256:
        return into
    tmp = into.with_name(f".{into.name}.{secrets.token_hex(6)}")
    h = client or httpx.Client(
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=True,
        event_hooks={"request": [_hosts]},
        headers={"user-agent": config.USER_AGENT},
    )
    try:
        digest, got = hashlib.sha256(), 0
        with h.stream("GET", url) as r, tmp.open("wb") as out:
            if r.status_code != 200:
                raise Refused(f"HTTP {r.status_code}")
            for chunk in r.iter_bytes(1 << 20):
                got += len(chunk)
                if got > size:
                    raise Refused("larger than pinned")
                digest.update(chunk)
                out.write(chunk)
        if got != size or digest.hexdigest() != sha256:
            raise Refused("does not match its pinned sha256")
        tmp.replace(into)
        return into
    except (httpx.HTTPError, OSError) as e:
        raise Refused(type(e).__name__) from None
    finally:
        tmp.unlink(missing_ok=True)
        if client is None:
            h.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _target() -> str:
    from metaphi import update

    return update.target()


def _runtime(say: Callable[[str], None] | None) -> tuple[Any, Any, Any]:
    """onnxruntime, tokenizers and numpy: this Python's own, else the pinned wheels unpacked into the cache."""
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    try:
        return tuple(importlib.import_module(m) for m in ("onnxruntime", "tokenizers", "numpy"))  # type: ignore[return-value]
    except ImportError:
        pass
    wheels = RUNTIME.get(_target())
    if sys.version_info[:2] != (3, 12) or not wheels:
        raise Refused(f"no pinned runtime for Python {sys.version_info[0]}.{sys.version_info[1]} on {_target()}")
    where = cache() / f"runtime-cp312-{_target()}"
    marker = where / ".complete"
    if not marker.is_file():
        if say:
            say(f"Downloading the de-identification model's runtime ({sum(s for _n, s, _h in wheels) >> 20} MB, once)")
        for path, size, sha in wheels:
            wheel = fetch(_PYPI + path, size, sha, cache() / "wheels" / path.rsplit("/", 1)[1])
            with zipfile.ZipFile(wheel) as z:
                for member in z.namelist():
                    if member.startswith("/") or ".." in member.split("/"):
                        raise Refused("a wheel holds a path outside itself")
                z.extractall(where)
        marker.touch()
    sys.path.insert(0, str(where))
    try:
        return tuple(importlib.import_module(m) for m in ("onnxruntime", "tokenizers", "numpy"))  # type: ignore[return-value]
    except ImportError as e:
        raise Refused(f"its runtime does not load here ({e.name})") from None


class Gliner:
    """GLiNER token-level inference with onnxruntime and tokenizers alone: no torch, no gliner package."""

    def __init__(self, model: Path, tokenizer: Path, runtime: tuple[Any, Any, Any]) -> None:
        ort, tokenizers, self.np = runtime
        if hasattr(ort, "disable_telemetry_events"):
            ort.disable_telemetry_events()
        ort.set_default_logger_severity(3)
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(4, os.cpu_count() or 1)
        options.log_severity_level = 3
        self.session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        self.tokenizer = tokenizers.Tokenizer.from_file(str(tokenizer))
        self.prompt = [w for label in LABELS for w in ("<<ENT>>", label)] + ["<<SEP>>"]

    def __call__(self, lines: list[str]) -> list[list[tuple[int, int, str]]]:
        out: list[list[tuple[int, int, str]]] = []
        for i in range(0, len(lines), BATCH):
            out += self._batch(lines[i : i + BATCH])
        return out

    def _batch(self, lines: list[str]) -> list[list[tuple[int, int, str]]]:
        np = self.np
        words = [[(m.start(), m.end()) for m in WORD.finditer(line)][:MAX_WORDS] for line in lines]
        encs = [
            self.tokenizer.encode(self.prompt + [line[a:b] for a, b in ws], is_pretokenized=True)
            for line, ws in zip(lines, words, strict=True)
        ]
        width = max(len(e.ids) for e in encs)
        ids = np.zeros((len(encs), width), dtype=np.int64)
        mask, wmask = np.zeros_like(ids), np.zeros_like(ids)
        skip = len(self.prompt)
        for r, e in enumerate(encs):
            ids[r, : len(e.ids)], mask[r, : len(e.ids)] = e.ids, e.attention_mask
            prev = None
            for t, w in enumerate(e.word_ids):
                if w is not None and w >= skip and w != prev:
                    wmask[r, t] = w - skip + 1
                prev = w
        lengths = np.array([[len(ws)] for ws in words], dtype=np.int64)
        (logits,) = self.session.run(
            None, {"input_ids": ids, "attention_mask": mask, "words_mask": wmask, "text_lengths": lengths}
        )
        probs = 1.0 / (1.0 + np.exp(-logits))
        return [self._spans(probs[r, : len(ws)], ws) for r, ws in enumerate(words)]

    def _spans(self, p: Any, words: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
        """Start, end and inside scores per word and label; a span runs from a start to an end of the same label
        with every word inside above the threshold. Overlaps keep the higher-scoring span."""
        found = []
        for c, label in enumerate(LABELS):
            starts = [i for i in range(len(words)) if p[i, c, 0] > THRESHOLD]
            ends = [i for i in range(len(words)) if p[i, c, 1] > THRESHOLD]
            for s in starts:
                for e in ends:
                    if e >= s and all(p[k, c, 2] >= THRESHOLD for k in range(s, e + 1)):
                        score = min(p[s, c, 0], p[e, c, 1], *(p[k, c, 2] for k in range(s, e + 1)))
                        found.append((float(score), s, e, label))
        kept: list[tuple[int, int, str]] = []
        taken: set[int] = set()
        for _score, s, e, label in sorted(found, reverse=True):
            if taken.isdisjoint(range(s, e + 1)):
                taken.update(range(s, e + 1))
                kept.append((words[s][0], words[e][1], "name" if label == "person" else label.replace(" ", "_")))
        return sorted(kept)


def load(say: Callable[[str], None] | None = None) -> tuple[Gliner | None, str]:
    """The model, or None with why it does not run. Never raises: the model pass is one layer of several."""
    if os.environ.get("KENNET_DEID_MODEL", "").lower() in ("0", "off", "no", "false"):
        return None, "not run (KENNET_DEID_MODEL=off)"
    try:
        runtime = _runtime(say)
        files = cache() / NAME
        if say and not (files / "model_quint8.onnx").is_file():
            say(f"Downloading the de-identification model {NAME} ({MODEL[1] >> 20} MB, once)")
        model = fetch(*MODEL, files / "model_quint8.onnx")
        tokenizer = fetch(*TOKENIZER, files / "tokenizer.json")
        return Gliner(model, tokenizer, runtime), f"ran ({NAME})"
    except Refused as e:
        return None, f"not run: {e}"
    except Exception as e:  # noqa: BLE001 - a model that fails to load leaves the other passes to run
        return None, f"not run: the model did not load ({type(e).__name__})"


__all__ = ["LABELS", "MODEL", "NAME", "RUNTIME", "TOKENIZER", "Gliner", "Refused", "fetch", "load"]
