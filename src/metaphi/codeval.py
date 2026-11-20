"""`kennet codeval`: measure a repository here and send Metaphi AI typed measurements (see `SENDS`).

The client holds no judgement of its own. It reads the repository's git metadata into the closed profile
(`codeval_schema`), sends it, and does what the server's answer says: build and test the commits it names, swap the
operators it names, send the blob ids of the files it names. What it sends back is typed results under the same
schema: exits, seconds, counts, enums, and keyed hashes (HMAC under a key that never leaves this machine). Logs stay
here; a run sends the sha256 of its log alone.
"""

from __future__ import annotations

import bisect
import collections
import contextlib
import gzip
import hashlib
import hmac
import itertools
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any

import httpx

from metaphi import __version__, api, codebase_checks, codebase_env, codebase_sites, codeval_runs, config, errors
from metaphi import codebase_reading as reading
from metaphi import codeval_extract as extract
from metaphi import codeval_schema as schema
from metaphi.o200k import tokens as _tokens

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None  # type: ignore[assignment]

PROFILE = schema.PROFILE
RESULTS = schema.RESULTS
TZ_MOST = 23 * 60 + 59  # format: the widest zone a clock can have, in minutes
MAX_BLOB = 2_000_000  # size: a file larger than this is not read for its counts
MAX_BLOBS = 64
REVEAL_FLOOR = 1024  # privacy floor: a blob id is revealed only for a source file of at least 1 KiB

SOURCE = frozenset(
    [
        "go",
        "py",
        "java",
        "kt",
        "kts",
        "scala",
        "groovy",
        "jelly",
        "js",
        "jsx",
        "mjs",
        "cjs",
        "ts",
        "tsx",
        "vue",
        "svelte",
        "c",
        "h",
        "cc",
        "cpp",
        "cxx",
        "hpp",
        "hh",
        "cs",
        "fs",
        "rb",
        "php",
        "rs",
        "dart",
        "swift",
        "cbl",
        "cob",
        "cpy",
        "jcl",
        "bms",
        "st",
        "gs",
        "pl",
        "pm",
        "f",
        "f90",
        "for",
        "sh",
        "bash",
        "ps1",
        "m",
        "mm",
        "ex",
        "exs",
        "erl",
        "hs",
        "ml",
        "clj",
        "lua",
        "vb",
        "pas",
        "sql",
        "r",
        "jl",
        "zig",
        "nim",
        "sol",
        "v",
        "sv",
        "vhd",
        "vhdl",
        "tf",
        "abap",
        "rpg",
        "pli",
    ]
)
DOC = frozenset(["md", "txt", "rst", "adoc", "pdf", "docx", "doc"])
CONFIG = frozenset(
    ["json", "yaml", "yml", "toml", "ini", "xml", "env", "cfg", "conf", "lock", "sum", "properties", "gradle"]
)
DATA = frozenset(
    [
        "csv",
        "tsv",
        "jsonl",
        "parquet",
        "dat",
        "bin",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "ico",
        "ttf",
        "woff",
        "woff2",
        "mp4",
        "zip",
        "gz",
        "tar",
        "jar",
    ]
)
KNOWN = frozenset(
    [
        "makefile",
        "cmakelists.txt",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "go.mod",
        "go.sum",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "cargo.toml",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "gemfile",
        "composer.json",
        "pubspec.yaml",
        "build.xml",
        "jenkinsfile",
        ".gitlab-ci.yml",
        ".travis.yml",
        "tox.ini",
        "pytest.ini",
        "license",
        "license.txt",
        "license.md",
        "licence",
        "licence.txt",
        "licence.md",
        "copying",
        "copying.txt",
        "unlicense",
        "readme.md",
        "contributing.md",
        "changelog.md",
        "codeowners",
        "agents.md",
        "claude.md",
        "gemini.md",
        "copilot-instructions.md",
        ".cursorrules",
        ".windsurfrules",
        ".aider.conf.yml",
        ".gitmodules",
        ".gitattributes",
    ]
)
TEST = re.compile(
    r"(^|/)(tests?|spec|specs|testdata|testutils?|__tests__|fixtures?|[\w.-]*-tests?(-[\w.-]*)?\.package)(/|\Z)"
    r"|Tests?\.class(/|\.st\Z)|Tests?\.(java|php|cs|kt|groovy)\Z|_test\.|\.test\.|\.spec\.|(^|/)test_|tests?\.[a-z]+\Z",
    re.IGNORECASE,
)
VENDORED = re.compile(r"(^|/)(vendor|node_modules|third_party|external|deps)/", re.IGNORECASE)
GENERATED = re.compile(
    r"(\.pb\.go|_gen\.go|\.gen\.|\.sql\.go|\.g\.dart|\.freezed\.dart|\.min\.js|/mocks?/|generated)", re.IGNORECASE
)
GENERATED_HEAD = re.compile(rb"code generated|do not edit|@generated|auto-?generated", re.IGNORECASE)
BOT = re.compile(r"(\[bot\]|dependabot|renovate|github-actions|bot@|noreply@.*bot)", re.IGNORECASE)
TICKET = re.compile(r"(?<![\w/])(#\d+|[A-Z][A-Z0-9]{1,9}-\d+)\b")
PR_NUMBER = re.compile(r"(Merge pull request #\d+|\(#\d+\)[ \t]*$)", re.MULTILINE)
PR = re.compile(r"(Merge pull request #\d+|\(#\d+\)[ \t]*$|Merged in |See merge request|Merge branch )", re.MULTILINE)
EMAIL_DOMAIN = re.compile(r"@([A-Za-z0-9.-]+)")
FIX = re.compile(r"(?i)\b(fix(es|ed)?|bug|defect|patch|hotfix|regression|crash)\b")
REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,64})")
PREFIX = re.compile(r"^(feat|fix|refactor|perf|test|docs|build|ci|chore|style|revert)(\([^)]*\))?!?:")
HEX_RUN = re.compile(r"\b[0-9a-fA-F]{7,}\b")
WORD = re.compile(r"[^\W_]+")
PATH_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
IDENT = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
TEST_FN = re.compile(
    r"(^[ \t]*func\s+(Test|Benchmark|test)\w*\(|^[ \t]*def\s+test_|@(Test|ParameterizedTest|RepeatedTest|TestFactory)\b"
    r"|function\s+test\w*\(|#\[(tokio::)?test\]|\[(Test|Fact|Theory|TestMethod|DataTestMethod|TestCase)\b"
    r"|^test\w*$|^[ \t]*(it|test|describe)\s*\(|^[ \t]*(it|test|specify|scenario)\s+['\"]|^[ \t]*TEST(_[FP])?\()",
    re.MULTILINE,
)
ENV_REF = re.compile(r"(os\.Getenv|os\.environ|process\.env|System\.getenv|ENV\[|getenv\(|API_?KEY|SECRET|TOKEN)")
URL_REF = re.compile(r"https?://(?!localhost|127\.0|example)")
MOCK_REF = re.compile(r"(?i)\b(mock|fake|stub|httptest|testcontainers|sqlmock|wiremock|nock)\b")
COMMENT = re.compile(r"^[ \t]*(//|#|--|/\*|\*|;|%|REM\b|!)|^.{6}\*")
_JAVA = re.compile(r"^[ \t]*import\s+(?:static\s+)?([\w.]+)", re.MULTILINE)
_JS = re.compile(r"(?:from\s+|require\()['\"]([^'\"]+)", re.MULTILINE)
_C = re.compile(r"^[ \t]*#include\s+\"([^\"]+)\"", re.MULTILINE)
_COBOL = re.compile(r"(?i)\b(?:COPY|CALL)\s+['\"]?([A-Z0-9#@$-]+)", re.MULTILINE)
IMPORTS: dict[str, re.Pattern[str]] = {
    "go": re.compile(r"^[ \t]*(?:import\s+)?(?:\w+\s+)?\"([\w./\-]+)\"[ \t]*$", re.MULTILINE),
    "py": re.compile(r"^[ \t]*(?:from|import)\s+([\w.]+)", re.MULTILINE),
    "php": re.compile(r"^[ \t]*use\s+([\w\\]+)", re.MULTILINE),
    "rs": re.compile(r"^[ \t]*(?:pub\s+)?use\s+(?:crate::)?([\w:]+)|^[ \t]*(?:pub\s+)?mod\s+(\w+);", re.MULTILINE),
    "dart": re.compile(r"^[ \t]*(?:import|export|part)\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
    **dict.fromkeys(("java", "kt", "scala", "groovy"), _JAVA),
    **dict.fromkeys(("js", "jsx", "mjs", "cjs", "ts", "tsx"), _JS),
    **dict.fromkeys(("c", "h", "cc", "cpp", "cxx", "hpp", "hh"), _C),
    **dict.fromkeys(("cbl", "cob", "cpy"), _COBOL),
}
LICENSES = (
    ("Apache-2.0", b"apache license"),
    ("AGPL-3.0", b"affero general public"),
    ("LGPL", b"lesser general public"),
    ("GPL", b"gnu general public"),
    ("MPL-2.0", b"mozilla public license"),
    ("BSD", b"redistribution and use in source"),
    ("MIT", b"permission is hereby granted, free of charge"),
    ("Unlicense", b"released into the public domain"),
)
KNOWN_EXT = SOURCE | DOC | CONFIG | DATA
GIT_FLOOR = (2, 32)  # build mechanics: the oldest git whose output this reads
_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "LC_ALL": "C",
}
_FIXED = (
    "core.quotePath=false",
    "core.fsmonitor=false",
    "log.showRoot=true",
    "log.mailmap=true",
    "log.showSignature=false",
    "log.follow=false",
    "diff.noprefix=false",
    "diff.mnemonicPrefix=false",
    "i18n.logOutputEncoding=UTF-8",
)


class Unreadable(Exception):
    pass


RELEASE = re.compile(r"^v?\d+(\.\d+)+")
TAG_FORMAT = "%(objecttype)%00%(objectname)%00%(*objectname)%00%(refname:lstrip=2)"


def _tags(repo: Path, order: dict[str, int]) -> list[dict[str, Any]]:
    """Each tag: the commit it names, whether it is annotated or reads as a release, and, for an annotated tag, the
    tagger's time and zone exactly as the tag object holds them. Its name is never sent."""
    out, objects = [], []
    for line in _text(repo, "for-each-ref", f"--format={TAG_FORMAT}", "refs/tags").splitlines():
        kind, obj, target, name = line.split("\0")
        out.append(
            {
                "commit": order.get(target if kind == "tag" else obj, 0),
                "annotated": kind == "tag",
                "release": bool(RELEASE.match(name)),
                **(dict.fromkeys(("tagger_time", "tagger_tz", "tagger_tz_neg0")) if kind != "tag" else {}),
            }
        )
        if kind == "tag":
            objects.append((out[-1], obj))
    said = _headers(repo, [obj for _t, obj in objects], "tagger")
    for tag, obj in objects:
        tag.update(_stamp(said.get(obj, {}).get("tagger"), "tagger"))
    return sorted(out, key=lambda t: (t["commit"], t["tagger_time"] is not None, t["tagger_time"] or 0))


def _zone(raw: str) -> tuple[int, bool]:
    """git's `+HHMM` as minutes east of UTC, and whether it is `-0000` (a zone git was not told). A zone git holds
    but no clock can have is sent as 0."""
    m = re.fullmatch(r"([+-])(\d\d)(\d\d)", raw)
    if not m:
        return 0, False
    minutes = int(m[2]) * 60 + int(m[3])
    return ((-minutes if m[1] == "-" else minutes) if minutes <= TZ_MOST else 0), raw == "-0000"


def _stamp(line: str | None, who: str) -> dict[str, Any]:
    """`<who>_time`, `<who>_tz` and `<who>_tz_neg0` from a raw `author`, `committer` or `tagger` header value."""
    m = re.search(r" (-?\d+) ([+-]\d{4}|\S+)\Z", line or "")
    if not m:
        return {f"{who}_time": 0, f"{who}_tz": 0, f"{who}_tz_neg0": False}
    tz, neg0 = _zone(m[2])
    return {f"{who}_time": min(max(int(m[1]), -(1 << 63)), (1 << 63) - 1), f"{who}_tz": tz, f"{who}_tz_neg0": neg0}


def _headers(repo: Path, objects: list[str], *names: str) -> dict[str, dict[str, str]]:
    """The raw header lines of each object, by name (the first of each), read straight from the object store: git's
    own formatting normalises `-0000` to `+0000`. `signed` is there when a gpgsig header is."""
    if not objects:
        return {}
    got = subprocess.run(
        _git(repo, "cat-file", "--batch"),
        input=("\n".join(objects) + "\n").encode(),
        capture_output=True,
        env=_ENV,
        check=False,
    )
    if got.returncode != 0:
        raise Unreadable("cat-file")
    out: dict[str, dict[str, str]] = {}
    data, at = got.stdout, 0
    for obj in objects:
        nl = data.index(b"\n", at)
        head = data[at:nl].split()
        if len(head) != 3:  # "<id> missing": a shallow or partial clone that does not hold the object
            at = nl + 1
            out[obj] = {}
            continue
        size = int(head[2])
        body = data[nl + 1 : nl + 1 + size]
        at = nl + 1 + size + 1
        fields: dict[str, str] = {}
        for line in body.split(b"\n\n", 1)[0].split(b"\n"):
            name, _, value = line.partition(b" ")
            word = name.decode("latin-1")
            if word in names and word not in fields:
                fields[word] = value.decode("utf-8", "replace")
            elif word in ("gpgsig", "gpgsig-sha256"):
                fields["signed"] = "1"
        out[obj] = fields
    return out


def _git(repo: Path, *args: str) -> list[str]:
    fixed = [word for setting in _FIXED for word in ("-c", setting)]
    return ["git", *fixed, "-C", str(repo), *args]


def _ask(repo: Path, *args: str) -> str:
    return (
        subprocess.run(_git(repo, *args), capture_output=True, env=_ENV, check=False)
        .stdout.decode("utf-8", "replace")
        .strip()
    )


def _out(repo: Path, *args: str) -> bytes:
    got = subprocess.run(_git(repo, *args), capture_output=True, env=_ENV, check=False)
    if got.returncode:
        raise Unreadable(args[0])
    return got.stdout


def _text(repo: Path, *args: str) -> str:
    return _out(repo, *args).decode("utf-8", "replace").strip()


class _Ids(dict):
    def of(self, key: Any) -> int:
        return self.setdefault(key, len(self) + 1)


def _kind(path: str) -> str:
    ext = _ext(path)
    if VENDORED.search(path):
        return "vendored"
    if TEST.search(path):
        return "test"
    if GENERATED.search(path):
        return "generated"
    for name, group in (("doc", DOC), ("config", CONFIG), ("data", DATA), ("source", SOURCE)):
        if ext in group:
            return name
    return "other"


def _ext(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return ext if ext in KNOWN_EXT else ""


def keyed(key: bytes, value: str) -> str:
    """HMAC-SHA256 of `value` under the repository's own key, first 16 hex: equal values give equal keys in one
    repository, and nothing else can be read from it. The key stays on this machine (`codeval_runs.repo_key`)."""
    return hmac.new(key, value.encode("utf-8", "surrogateescape"), hashlib.sha256).hexdigest()[:16]


KEY_LABEL = "kennet-codeval-key"


def key_id(key: bytes) -> str:
    """Which key this machine holds for the repository, without the key: the HMAC of a fixed label under it. The server
    holds a run to the machine that started it by this."""
    return keyed(key, KEY_LABEL)


def _subject_key(subject: str, key: bytes) -> str:
    said = HEX_RUN.sub("", TICKET.sub("", subject)).lower()
    return keyed(key, " ".join(re.sub(r"\d", "0", said).split()))


def _path_words(path: str) -> set[str]:
    return {w.lower() for part in re.split(r"[/._-]+", path) for w in PATH_WORD.findall(part)}


def _commits(
    repo: Path,
    authors: _Ids,
    domains: _Ids,
    people: list[dict],
    order: dict[str, int],
    words: dict[int, set[str]],
    key: bytes,
    at: str = "HEAD",
) -> list[dict]:
    fields = "%H%x00%P%x00%aE%x00%cE%x00%B%x00%(trailers:only,unfold)%x00"
    raw = _out(repo, "log", at, "--topo-order", "--reverse", f"--format={fields}").split(b"\0")

    def person(address: str) -> int:
        low = address.lower()
        if low not in authors:
            people.append(
                {"author": authors.of(low), "domain": domains.of(low.rpartition("@")[2]), "bot": bool(BOT.search(low))}
            )
        return authors[low]

    rows = [[x.decode("utf-8", "replace") for x in raw[i : i + 6]] for i in range(0, len(raw) - 5, 6)]
    # only the tip's own history (git log of `at`, no other branch), parents first and the tip last
    tip = _ask(repo, "rev-parse", f"{at}^{{commit}}")
    rows = [r for r in rows if r[0].strip() != tip] + [r for r in rows if r[0].strip() == tip]
    for row in rows:
        order[row[0].strip()] = len(order) + 1
    said = _headers(repo, list(order), "author", "committer")
    commits = []
    for row in rows:
        sha, parents, author, committer, msg, block = row
        sha = sha.strip()
        back = REVERTS.search(msg)
        subject, *body = msg.split("\n")
        words[order[sha]] = {w.lower() for w in WORD.findall(msg)}
        raw_head = said.get(sha, {})
        commits.append(
            {
                "commit": order[sha],
                "parents": [order.get(p, 0) for p in parents.split()],
                "author": person(author),
                "committer": person(committer),
                **_stamp(raw_head.get("author"), "author"),
                **_stamp(raw_head.get("committer"), "committer"),
                "signed": "signed" in raw_head,
                "msg_chars": len(msg),
                "msg_lines": msg.count("\n") + 1,
                "ticket_refs": len(TICKET.findall(PR_NUMBER.sub("", msg))),
                "pr_merge": bool(PR.search(msg)),
                "fix_word": bool(FIX.search(msg)),
                "revert": msg.lower().startswith("revert"),
                "reverts": next((order[s] for s in order if back and s.startswith(back.group(1))), None)
                if back
                else None,
                "trailers": _trailers(block),
                "subject_chars": len(subject),
                "body_lines": sum(bool(x.strip()) for x in body),
                "prefixed": bool(PREFIX.match(subject)),
                "path_terms": 0,
                "subject_key": _subject_key(subject, key),
                "files": [],
            }
        )
    return commits


TRAILER_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


TRAILER_KEYS = frozenset(
    [
        "signed-off-by",
        "co-authored-by",
        "reviewed-by",
        "acked-by",
        "tested-by",
        "reported-by",
        "suggested-by",
        "helped-by",
        "cc",
        "fixes",
        "closes",
        "refs",
        "link",
        "change-id",
        "assisted-by",
        "generated-by",
    ]
)
VENDORS = (
    ("anthropic", ("anthropic.com", "claude.ai")),
    ("openai", ("openai.com",)),
    ("cursor", ("cursor.com", "cursor.sh", "anysphere.co")),
    ("aider", ("aider.chat",)),
    ("cognition", ("cognition.ai", "cognition-labs.com", "devin.ai")),
    ("codeium", ("codeium.com",)),
    ("windsurf", ("windsurf.com",)),
    ("all_hands", ("all-hands.dev",)),
    ("factory", ("factory.ai",)),
    ("sweep", ("sweep.dev",)),
    ("coderabbit", ("coderabbit.ai",)),
    ("google", ("google.com",)),
)
"""Coding assistant vendors by their email domains (or a subdomain): a trailer names the vendor, never the domain."""


def _tool(domain: str) -> str | None:
    return next((t for t, ds in VENDORS for d in ds if domain == d or domain.endswith("." + d)), None)


"""Trailer keys sent as they are; any other (a project's own, `project-falcon`) is sent as `other`."""


def _trailers(block: str) -> list[dict[str, Any]]:
    out = []
    for line in block.split("\n"):
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if sep and TRAILER_KEY.fullmatch(key):
            m = EMAIL_DOMAIN.search(value)
            out.append(
                {"key": key if key in TRAILER_KEYS else "other", "tool": _tool(m[1].lower().rstrip(".")) if m else None}
            )
    return out


def _changes(repo: Path, by_sha: dict[str, dict], paths: _Ids, order: dict[str, int], at: str = "HEAD") -> None:
    tokens = iter(
        _out(repo, "log", at, "-M", "--diff-merges=first-parent", "--raw", "--numstat", "-z", "--format=%x01%H").split(
            b"\0"
        )
    )
    row: dict | None = None
    status: dict[str, str] = {}
    for token in tokens:
        text = token.decode("utf-8", "surrogateescape")
        head = text.lstrip("\n")
        if head.startswith("\x01"):
            row, status = by_sha.get(head[1:].strip()), {}
        elif head.startswith(":"):
            letter = head.split()[-1][:1]
            old = next(tokens).decode("utf-8", "surrogateescape")
            new = next(tokens).decode("utf-8", "surrogateescape") if letter in "RC" else old
            status[new] = letter
        elif (m := re.match(r"(\d+|-)\t(\d+|-)\t(.*)$", head, re.DOTALL)) and row is not None:
            old = None
            path = m[3]
            if not path:
                old = next(tokens).decode("utf-8", "surrogateescape")
                path = next(tokens).decode("utf-8", "surrogateescape")
            change = {
                "file": paths.of(path),
                "status": status.get(path, "M"),
                "added": int(m[1]) if m[1] != "-" else -1,
                "deleted": int(m[2]) if m[2] != "-" else -1,
                "hunks": [],
            }
            if old is not None:
                change["old_file"] = paths.of(old)
            row["files"].append(change)


def _unquote(raw: bytes) -> str:
    raw = raw.rstrip(b"\t\n")
    if raw[:1] == b'"':
        raw = re.sub(
            rb"\\([0-7]{3}|.)",
            lambda m: (
                bytes([int(m[1], 8)])
                if len(m[1]) == 3
                else {b"a": b"\a", b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r", b"t": b"\t", b"v": b"\v"}.get(
                    m[1], m[1]
                )
            ),
            raw[1:-1],
        )
    return raw.decode("utf-8", "surrogateescape")


def _hunks(repo: Path, by_sha: dict[str, dict], paths: _Ids, at: str = "HEAD") -> None:
    p = subprocess.Popen(
        _git(
            repo,
            "log",
            at,
            "-M",
            "--diff-merges=first-parent",
            "-p",
            "-U0",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--format=%x01%H",
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_ENV,
    )
    spans: dict[str, list[list[int]]] = {}
    words: dict[str, set[bytes]] = {"test+": set(), "source+": set(), "source-": set()}
    row: dict | None = None

    def close() -> None:
        if row is not None:
            by_id = {paths[path]: s for path, s in spans.items() if path in paths}
            for change in row["files"]:
                change["hunks"] = by_id.get(change["file"], [])
            row["shared_idents"] = len((words["test+"] & words["source+"]) - words["source-"])

    skip, path, side = 0, None, None
    assert p.stdout
    for line in p.stdout:
        if skip:
            if line[:1] != b"\\":
                skip -= 1
                if side and (key := side + line[:1].decode("latin-1")) in words:
                    words[key].update(IDENT.findall(line[1:]))
            continue
        if line[:1] == b"\x01":
            close()
            row, spans, path = by_sha.get(line[1:].strip().decode()), {}, None
            words = {"test+": set(), "source+": set(), "source-": set()}
        elif line.startswith(b"+++ "):
            if line[4:] != b"/dev/null\n":
                got = _unquote(line[4:])
                path = got[2:] if got[:2] == "b/" else got
        elif line.startswith(b"--- "):
            got = _unquote(line[4:])
            path = got[2:] if got[:2] == "a/" else None
        elif line.startswith(b"@@ ") and path is not None:
            m = re.match(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                old, gone, new, came = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
                skip = gone + came
                spans.setdefault(path, []).append([old, gone, new, came])
                kind = _kind(path)
                side = "test" if kind == "test" else "source" if kind == "source" else None
    close()
    if p.wait():
        raise Unreadable("log")


def _blobs(repo: Path, shas: list[str]) -> Iterator[tuple[str, bytes]]:
    p = subprocess.Popen(
        _git(repo, "cat-file", "--batch"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_ENV,
    )
    assert p.stdin and p.stdout
    for sha in shas:
        p.stdin.write(sha.encode() + b"\n")
        p.stdin.flush()
        head = p.stdout.readline().split()
        if len(head) == 3:
            yield sha, p.stdout.read(int(head[2]))
            p.stdout.read(1)
    p.stdin.close()
    p.wait()


def _content(body: bytes, ext: str, kind: str, resolves: Callable[[str, str], bool]) -> dict[str, Any]:
    if b"\0" in body[:8000]:
        return {"binary": True}
    text = body.decode("latin-1")
    lines = text.split("\n")
    indents = [len(x) - len(x.lstrip(" \t")) for x in lines if x.strip()]
    row: dict[str, Any] = {
        "binary": False,
        "generated_header": bool(GENERATED_HEAD.search(body[:1024])),
        "lines": len(lines),
        "blank": sum(not x.strip() for x in lines),
        "comment": sum(bool(COMMENT.match(x)) for x in lines if x.strip()),
        "indent_sum": sum(indents),
        "max_indent": max(indents or [0]),
        "max_line": max(map(len, lines)),
    }
    if kind in ("source", "test"):
        row["test_fns"] = len(TEST_FN.findall(text))
        pattern = IMPORTS.get(ext)
        row["imports_internal"] = row["imports_external"] = None
        if pattern:
            found = [x if isinstance(x, str) else next(y for y in x if y) for x in pattern.findall(text)]
            inside = sum(resolves(x, ext) for x in found)
            row.update(imports_internal=inside, imports_external=len(found) - inside)
    if kind == "source" and (ops := codebase_sites.counts(text, ext)) is not None:
        row["ops"] = ops
    if kind == "test":
        row.update(
            env_refs=len(ENV_REF.findall(text)),
            url_refs=len(URL_REF.findall(text)),
            mock_refs=len(MOCK_REF.findall(text)),
        )
    return row


def _resolver(tip: list[str]) -> Callable[[str, str], bool]:
    """Whether an import names something at the tip: the import's whole path is the end of a tip file or folder path
    (`com.acme.Ledger` and `src/com/acme/Ledger.java`), or a whole tip path is the end of the import's
    (`github.com/acme/core/billing` and `billing/`). A fact about the two paths, nothing weighed."""
    tails, wholes, leaves = set(), set(), set()
    for path in tip:
        parts = os.path.splitext(path)[0].split("/")
        leaves.add(parts[-1].upper())
        for end in range(1, len(parts) + 1):
            whole = parts[:end]
            wholes.add("/".join(whole))
            tails.update("/".join(whole[i:]) for i in range(len(whole)))

    def resolves(name: str, ext: str) -> bool:
        if ext in ("cbl", "cob", "cpy"):
            return name.upper() in leaves
        if name.startswith("."):
            return True
        q = re.sub(r"^package:", "", name)
        q = (
            re.sub(r"[.\\:]+", "/", q)
            if ext
            not in ("go", "dart", "js", "jsx", "mjs", "cjs", "ts", "tsx", "c", "h", "cc", "cpp", "cxx", "hpp", "hh")
            else os.path.splitext(q)[0]
        )
        parts = q.split("/")
        return q in tails or any("/".join(parts[i:]) in wholes for i in range(len(parts)))

    return resolves


def why_not(repo: Path) -> str | None:
    bare = _ask(repo, "rev-parse", "--is-bare-repository") == "true"
    top = _ask(repo, "rev-parse", "--show-toplevel")
    if not (bare and _ask(repo, "rev-parse", "--git-dir") == ".") and not (
        top and Path(top).resolve() == repo.resolve()
    ):
        return "not_a_git_repository"
    if _ask(repo, "rev-parse", "--is-shallow-repository") == "true":
        return "shallow"
    if _ask(repo, "config", "--get", "extensions.partialclone") or _ask(
        repo, "config", "--get-regexp", r"remote\..*\.promisor"
    ):
        return "partial_clone"
    if not _ask(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}"):
        return "no_commits"
    return None


def _git_ready() -> None:
    try:
        said = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False, env=_ENV).stdout
    except OSError:
        said = ""
    m = re.search(r"(\d+)\.(\d+)", said)
    if not m or (int(m[1]), int(m[2])) < GIT_FLOOR:
        raise errors.MetaphiError(
            f"kennet codeval needs git {GIT_FLOOR[0]}.{GIT_FLOOR[1]} or newer", code="git_too_old"
        )


SERVICE_IMAGES = frozenset(schema.defs()["Env"]["properties"]["service_images"]["items"]["enum"])
"""Official images a service may be named by; any other image is counted, never named."""


def _env(env: dict[str, Any]) -> dict[str, Any]:
    """What `codebase_env.needs` read, in the schema's terms: service images by official name or counted."""
    named = [n for n in env.get("service_images", []) if n in SERVICE_IMAGES]
    other = env.get("other_images", 0) + len(env.get("service_images", [])) - len(named)
    return {**env, "service_images": sorted(named), "other_images": other}


AGENT_FILES = (
    ("agents_md", re.compile(r"(^|/)AGENTS\.md\Z", re.IGNORECASE)),
    ("claude_md", re.compile(r"(^|/)CLAUDE(\.local)?\.md$|(^|/)\.claude/", re.IGNORECASE)),
    ("gemini_md", re.compile(r"(^|/)GEMINI\.md$|(^|/)\.gemini/", re.IGNORECASE)),
    ("copilot_instructions", re.compile(r"(^|/)copilot-instructions\.md$|(^|/)\.github/instructions/")),
    ("cursor_rules", re.compile(r"(^|/)\.cursorrules$|(^|/)\.cursor/rules/")),
    ("windsurf_rules", re.compile(r"(^|/)\.windsurfrules$|(^|/)\.windsurf/rules/")),
    ("aider_conf", re.compile(r"(^|/)\.aider\.conf\.ya?ml\Z")),
    ("mcp_config", re.compile(r"(^|/)\.mcp\.json\Z|(^|/)\.(cursor|vscode)/mcp\.json\Z")),
)


def _agents(paths: set[str]) -> dict[str, bool]:
    return {name: any(pattern.search(p) for p in paths) for name, pattern in AGENT_FILES}


PUBLIC_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}\Z")
NPM_REGISTRY = "https://registry.npmjs.org/"
CRATES = "registry+https://github.com/rust-lang/crates.io-index"
PYPI = ("https://pypi.org/simple", "https://pypi.org/simple/")
RUBYGEMS = ("https://rubygems.org/", "https://rubygems.org")


def _toml(body: bytes) -> dict[str, Any]:
    if tomllib is None:
        return {}
    try:
        return tomllib.loads(body.decode("utf-8", "replace"))
    except (tomllib.TOMLDecodeError, ValueError):
        return {}


def _npm(body: bytes) -> Iterator[tuple[str, bool]]:
    try:
        doc = json.loads(body)
    except ValueError:
        return
    for key, pkg in (doc.get("packages") or {}).items() if isinstance(doc, dict) else ():
        if "node_modules/" not in key or not isinstance(pkg, dict) or pkg.get("link"):
            continue
        name = key.rsplit("node_modules/", 1)[1]
        resolved = pkg.get("resolved")
        # A scoped package (@scope/name) is counted, never named: a scope is an organization's own.
        yield name, not name.startswith("@") and isinstance(resolved, str) and resolved.startswith(NPM_REGISTRY)


def _cargo(body: bytes) -> Iterator[tuple[str, bool]]:
    for pkg in _toml(body).get("package") or []:
        if isinstance(pkg, dict) and isinstance(pkg.get("name"), str) and "source" in pkg:
            yield pkg["name"], pkg["source"] == CRATES


def _poetry(body: bytes, own_index: bool = False) -> Iterator[tuple[str, bool]]:
    """poetry.lock: a package with no source came from the default index, which is PyPI unless one of the
    organization's own is configured (`own_index`); then it is counted, never named."""
    for pkg in _toml(body).get("package") or []:
        if isinstance(pkg, dict) and isinstance(pkg.get("name"), str):
            source = pkg.get("source")
            yield (
                pkg["name"],
                (not source and not own_index) or (isinstance(source, dict) and source.get("url") in PYPI),
            )


PIP_ENV = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL")
PIP_FILES = ("pip.conf", "pip.ini", ".pip/pip.conf", ".config/pip/pip.conf", "Library/Application Support/pip/pip.conf")
POETRY_FILES = (".config/pypoetry/config.toml", "Library/Application Support/pypoetry/config.toml")


def _own_index(tree: dict[str, tuple[str, str, int]], read: Callable[[str], bytes | None]) -> bool:
    """Whether Python packages here may come from an index other than PyPI: the repository's `[[tool.poetry.source]]`,
    `poetry.toml` repositories or pip config, the environment's pip index, or this user's pip or Poetry config."""
    if any(os.environ.get(k) for k in PIP_ENV) or any(k.startswith("POETRY_REPOSITORIES_") for k in os.environ):
        return True
    pyproject = read("pyproject.toml") if "pyproject.toml" in tree else None
    if pyproject and ((_toml(pyproject).get("tool") or {}).get("poetry") or {}).get("source"):
        return True
    if "poetry.toml" in tree and (body := read("poetry.toml")) and _toml(body).get("repositories"):
        return True
    if any(f in tree and b"index-url" in (read(f) or b"") for f in ("pip.conf", "pip.ini")):
        return True
    home = Path.home()
    mine = [Path(os.environ["PIP_CONFIG_FILE"])] if os.environ.get("PIP_CONFIG_FILE") else []
    mine += [home / f for f in PIP_FILES[2:]] + [Path("/etc/pip.conf")]
    poetry = [Path(os.environ["POETRY_CONFIG_DIR"]) / "config.toml"] if os.environ.get("POETRY_CONFIG_DIR") else []
    poetry += [home / f for f in POETRY_FILES]

    def said(path: Path, word: bytes) -> bool:
        try:
            return word in path.read_bytes()
        except OSError:
            return False

    return any(said(f, b"index-url") for f in mine) or any(said(f, b"repositories") for f in poetry)


def _uv(body: bytes) -> Iterator[tuple[str, bool]]:
    for pkg in _toml(body).get("package") or []:
        if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str):
            continue
        source = pkg.get("source") if isinstance(pkg.get("source"), dict) else {}
        if "editable" in source or "virtual" in source:
            continue  # the repository's own projects
        yield pkg["name"], source.get("registry") in PYPI


def _gems(body: bytes) -> Iterator[tuple[str, bool]]:
    public = False
    for line in body.decode("utf-8", "replace").splitlines():
        if line and not line[0].isspace():
            public = False
        if line.startswith("  remote: "):
            public = line.split(": ", 1)[1].strip() in RUBYGEMS
        elif m := re.match(r"^    ([A-Za-z0-9_.-]+) \(", line):
            yield m[1], public


LOCKFILES = (
    ("npm", re.compile(r"(^|/)package-lock\.json\Z"), _npm),
    ("cargo", re.compile(r"(^|/)Cargo\.lock\Z"), _cargo),
    ("pypi", re.compile(r"(^|/)poetry\.lock\Z"), _poetry),
    ("pypi", re.compile(r"(^|/)uv\.lock\Z"), _uv),
    ("rubygems", re.compile(r"(^|/)Gemfile\.lock\Z"), _gems),
)
"""Lockfiles whose entries say where each dependency resolved from. A name is sent only for one resolved from the
ecosystem's public registry; any other (a private registry, a path, a git URL) is counted, never named."""


def _dependencies(tree: dict[str, tuple[str, str, int]], read: Callable[[str], bytes | None]) -> tuple[list, int]:
    named: set[tuple[str, str]] = set()
    private: set[tuple[str, str]] = set()
    own_index = _own_index(tree, read)
    for path in sorted(tree):
        if VENDORED.search(path):
            continue
        for eco, pattern, parse in LOCKFILES:
            if pattern.search(path) and (body := read(path)) is not None:
                for name, public in parse(body, own_index) if parse is _poetry else parse(body):
                    low = name.lower()
                    if public and PUBLIC_NAME.fullmatch(low):
                        named.add((eco, low))
                    else:
                        private.add((eco, low))
    private -= named
    return [{"eco": e, "name": n} for e, n in sorted(named)][:20_000], len(private)


def compute(repo: Path, key: bytes, keep: dict | None = None, at: str = "HEAD") -> dict[str, Any]:
    """One repository's profile (`kennet.codeval.profile.v1`) at commit `at`: its git metadata and per-file counts, as
    ids, counts, enums and keyed hashes. `key` is the repository's own HMAC key, kept on this machine."""
    authors, domains, paths, dirs = _Ids(), _Ids(), _Ids(), _Ids()
    people: list[dict] = []
    order: dict[str, int] = {}
    words: dict[int, set[str]] = {}
    commits = _commits(repo, authors, domains, people, order, words, key, at)
    by_sha = {sha: commits[i - 1] for sha, i in order.items()}
    _changes(repo, by_sha, paths, order, at)
    named = {i: _path_words(path) for path, i in paths.items()}
    for c in commits:
        changed = {i for x in c["files"] for i in (x["file"], x.get("old_file")) if i is not None}
        c["path_terms"] = len(words[c["commit"]] & set().union(*(named[i] for i in changed)))
    _hunks(repo, by_sha, paths, at)
    tree: dict[str, tuple[str, str, int]] = {}
    for entry in _out(repo, "ls-tree", "-r", "-l", "-z", at).split(b"\0"):
        if b"\t" in entry:
            meta, _, path = entry.partition(b"\t")
            mode, _type, sha, size = meta.split()
            tree[path.decode("utf-8", "surrogateescape")] = (
                mode.decode(),
                sha.decode(),
                int(size) if size.isdigit() else 0,
            )
    resolves = _resolver(list(tree))
    rows: dict[str, dict] = {}
    tree_dirs: dict[int, int] = {}
    for path in sorted(set(paths) | set(tree)):
        parent = os.path.dirname(path)
        q = parent
        while q:
            up = os.path.dirname(q)
            tree_dirs[dirs.of(q)] = dirs.of(up) if up else 0
            q = up
        row = {
            "file": paths.of(path),
            "dir": dirs.of(parent) if parent else 0,
            "depth": path.count("/"),
            "ext": _ext(path),
            "kind": _kind(path),
            "known": (b if (b := os.path.basename(path).lower()) in KNOWN else None),
            "at_tip": path in tree,
            "ci": any(pattern.search(path) for _n, pattern in codebase_env.CI),
        }
        if path in tree:
            if tree[path][0] != "160000":
                row["blob_key"] = keyed(key, tree[path][1])
            row["bytes"] = tree[path][2]
        rows[path] = row
    wanted: dict[str, list[str]] = collections.defaultdict(list)
    for path, (mode, sha, size) in tree.items():
        if mode != "160000" and size <= MAX_BLOB:
            wanted[sha].append(path)
    licenses: set[str] = set()
    lfs = False
    for sha, body in _blobs(repo, list(wanted)):
        tokens = None if b"\0" in body[:8000] else _tokens(body)
        for path in wanted[sha]:
            rows[path].update(_content(body, rows[path]["ext"], rows[path]["kind"], resolves))
            if tokens is not None:
                rows[path]["tokens"] = tokens
            low = os.path.basename(path).lower()
            if low.startswith(("license", "licence", "copying", "unlicense")):
                folded = b" ".join(body[:20000].lower().split())
                rows[path]["license"] = next((spdx for spdx, mark in LICENSES if mark in folded), "unknown")
                licenses.add(rows[path]["license"])
            if low == ".gitattributes" and b"filter=lfs" in body:
                lfs = True

    def read(p: str) -> bytes | None:
        return next((b for _s, b in _blobs(repo, [tree[p][1]])), None) if p in tree else None

    tip = _text(repo, "rev-parse", f"{at}^{{commit}}")
    blobs = {p for p, (mode, _s, _z) in tree.items() if mode != "160000"}
    env, start = codebase_env.needs(blobs, read)
    deps, private = _dependencies(tree, read)
    pack = re.search(r"size-pack: (\d+)", _text(repo, "count-objects", "-v"))
    tags = _tags(repo, order)
    if keep is not None:
        keep.update(
            path=repo,
            tip=tip,
            commits=commits,
            order=order,
            start=start,
            paths={row["file"]: path for path, row in rows.items() if row["at_tip"]},
            source=sorted(row["file"] for row in rows.values() if row["at_tip"] and row["kind"] == "source"),
            revealable=sorted(
                row["file"]
                for row in rows.values()
                if row["at_tip"] and row["kind"] == "source" and row.get("bytes", 0) >= REVEAL_FLOOR
            ),
        )
    return {
        "schema": PROFILE,
        "cli": __version__,
        "key_id": key_id(key),
        "tip": tip,
        "roots": _text(repo, "rev-list", "--max-parents=0", at).split()[:64],
        "git": {
            "branches": len(_text(repo, "for-each-ref", "--format=x", "refs/heads", "refs/remotes").split()),
            "tags": tags,
            "submodules": sum(m == "160000" for m, _s, _z in tree.values()),
            "lfs": lfs,
            "pack_bytes": int(pack[1]) * 1024 if pack else 0,
        },
        "licenses": sorted(licenses),
        "env": _env(env),
        "agents": _agents(set(tree)),
        "deps": deps,
        "private_deps": private,
        "authors": people,
        "dirs": [{"dir": d, "parent": up} for d, up in tree_dirs.items()],
        "commits": commits,
        "files": sorted(rows.values(), key=lambda r: r["file"]),
    }


SKIPPED_FIX = {
    "shallow": "a shallow clone has only part of the history: run `git fetch --unshallow` in it, or give its URL",
    "partial_clone": "a partial clone lacks file contents: clone it in full, or give its URL",
    "not_a_git_repository": "give the repository's top folder (where .git is), or its URL",
    "no_commits": "it has no commits yet",
    "clone_failed": "Check you can `git clone` that URL in this terminal",
    "unreadable": "git could not read it",
}


_URL = re.compile(r"(?:(?:https?|ssh|git|file)://\S+|[\w.-]+@[\w.-]+:\S+)\Z")


def is_url(given: str) -> bool:
    return bool(_URL.match(given)) and not Path(given).expanduser().exists()


def _url_name(url: str) -> str:
    tail = re.split(r"[/:]", url.rstrip("/"))[-1]
    return tail.removesuffix(".git")


def _clone(url: str, into: Path) -> str | None:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    got = subprocess.run(
        ["git", "-c", "protocol.ext.allow=never", "clone", "--bare", "--quiet", "--", url, str(into)],
        capture_output=True,
        env=env,
        check=False,
    )
    if got.returncode == 0:
        return None
    lines = [ln.strip() for ln in got.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    said = next(
        (ln for ln in lines if ln.lower().startswith(("error:", "fatal:", "remote:"))), lines[0] if lines else ""
    )
    return re.sub(r"(?i)^(error|fatal|remote):\s*", "", said).rstrip(".") or "git could not clone it"


def repositories(paths: list[str]) -> list[str]:
    out: list[str] = []
    for given in paths:
        path = Path(given).expanduser()
        if is_url(given) or not path.is_dir() or why_not(path) != "not_a_git_repository":
            out.append(given)
            continue
        found = sorted(str(p.parent) for p in path.glob("*/.git")) or sorted(
            str(p.parent) for p in path.glob("*/*/.git")
        )
        out += found or [given]
    return out


CHMOD_IMAGE = "busybox:1.37"


def _reader(repo: Path, tree: dict[str, tuple[str, str]]) -> Callable[[str], bytes | None]:
    def read(path: str) -> bytes | None:
        got = tree.get(path)
        if not got:
            return None
        for _sha, body in _blobs(repo, [got[1]]):
            return body
        return None

    return read


def _name_of(given: str, n: int, used: set[str]) -> str:
    base = _url_name(given) if is_url(given) else Path(given).expanduser().resolve().name
    name = (re.sub(r"[^\w .@+-]", "-", base, flags=re.ASCII).strip() or f"repo-{n}")[:100]
    while name in used:
        name = f"{name[:120]}-{n}"
    used.add(name)
    return name


DOCS = "https://docs.metaphi.ai/kennet/data-licensing/codeval#data-collected"


SENDS = (
    "typed measurements: no source code, logs, file paths, author names or commit text. Public dependency names and "
    "standard build-file names are included."
)
"""What leaves the machine, said the same way everywhere the CLI says it."""


def disclosure(paths: list[str]) -> tuple[str, ...]:
    """What every run says first, before it reads or sends anything."""
    return (f"CodeVal measures {' '.join(paths)} here and sends Metaphi AI {SENDS} Exactly what: {DOCS}",)


class Local:
    """One repository as this machine holds it: what the measurements need and what is never sent (its paths, its
    commit ids, its key). `kept` is bound to the profile filed for the run: its tip, commit order and ids."""

    def __init__(self, name: str, given: str, path: Path, roots: list[str]):
        self.name, self.given, self.path, self.roots = name, given, path, roots
        self.home = codeval_runs.home(roots, given if is_url(given) else str(path))
        self.key = codeval_runs.repo_key(codeval_runs.repo_home(roots))
        self.kept: dict[str, Any] = {}

    @property
    def tip(self) -> str:
        return self.kept["tip"]


def opened(paths: list[str], scratch: Path, say: Callable[[str], None]) -> Iterator[Local]:
    """Each repository given that can be read, cloned first when given by URL; one that cannot is named and passed."""
    _git_ready()
    used: set[str] = set()
    for n, given in enumerate(paths, 1):
        name = _name_of(given, n, used)
        path = Path(given).expanduser().resolve()
        if is_url(given):
            say(f"Cloning {name}…")
            path = scratch / f"{n}.git"
            if failed := _clone(given, path):
                say(f"✗ Skipped {name}: clone failed ({failed}). {SKIPPED_FIX['clone_failed']}")
                continue
        reason = why_not(path) if path.is_dir() else "not_a_git_repository"
        if reason:
            say(f"✗ Skipped {name}: {SKIPPED_FIX[reason]}")
            continue
        roots = _text(path, "rev-list", "--max-parents=0", "HEAD").split()
        yield Local(name, given, path, roots)


def profile(local: Local, at: str = "HEAD") -> dict[str, Any]:
    """The repository's profile at `at`, checked against the schema; what it needs to run the server's instructions
    stays in `local.kept`."""
    try:
        got = compute(local.path, local.key, local.kept, at)
    except (Unreadable, ValueError, OSError) as e:
        raise errors.MetaphiError(
            f"git could not read {local.name}", code="unreadable", next_step=SKIPPED_FIX["unreadable"]
        ) from e
    schema.check("Profile", got)
    return got


def file(local: Local) -> None:
    """Keep what the filed profile bound the run to, so every later batch measures against it."""
    codeval_runs.keep_profile(local.home, {k: v for k, v in local.kept.items() if k != "path"})


def resumed(local: Local) -> bool:
    """Bind `local` to the profile its run was filed with, never a new one: the tip may have moved since, and the old
    commits are still in git. False when nothing was filed here. Stops (exit 6) when the filed tip is gone, as after a
    history rewrite."""
    kept = codeval_runs.kept_profile(local.home)
    if not kept:
        return False
    tip = kept["tip"]
    if _ask(local.path, "cat-file", "-t", tip) != "commit":
        raise errors.MetaphiError(
            f"{local.name}: the commit this run was filed at ({tip[:12]}) is no longer in the repository, so the run "
            "cannot go on",
            code="profile_gone",
            next_step=f"fetch the commit {tip[:12]} back into this clone, then run the same command again",
        )
    kept["order"] = {k: int(v) for k, v in kept["order"].items()}
    kept["paths"] = {int(k): v for k, v in kept["paths"].items()}
    local.kept = {**kept, "path": local.path}
    return True


def packed(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()


SEND = httpx.Timeout(30.0, connect=10.0, read=90.0, write=600.0)
SEND_RETRIES = 4
SEND_BUDGET_S = 300.0


def _named(e: errors.MetaphiError) -> errors.MetaphiError:
    if e.code == "restart_limit":
        e.message = (
            "this repository has been measured afresh as many times as a run takes and cannot be restarted again; "
            "the measurement it holds goes on"
        )
        e.next_step = "continue the live measurement: run `kennet codeval` without --restart where it runs"
    elif e.code == "insufficient_scope":
        e.message += (
            "; it takes kennet:codeval:write. `kennet login` hands this machine keys that hold it "
            "once your organization does"
        )
        e.next_step = "kennet login"
    return e


def filed_tip(e: errors.MetaphiError) -> str | None:
    """The tip a `profile_mismatch` names as data (`error.filed_tip`): the one the open run was filed at."""
    tip = e.context.get("filed_tip")
    return tip if isinstance(tip, str) and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", tip) else None


def _held(local: Local, tip: str | None) -> bool:
    return bool(tip) and _ask(local.path, "cat-file", "-t", tip) == "commit"


def send_filed(local: Local, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Profile and file the repository: at the tip a profile was filed at before on this machine, when one was and
    this clone still holds it, else at HEAD. When the server holds an open run for it at another tip
    (`profile_mismatch`), profile that tip instead, if this clone holds it, and file again: the run goes on where it
    stopped."""
    before = (codeval_runs.kept_profile(local.home) or {}).get("tip")
    doc = profile(local, before if _held(local, before) else "HEAD")
    try:
        got, _path = send_profile(local, doc, sleep)
    except errors.MetaphiError as e:
        tip = filed_tip(e) if e.code == "profile_mismatch" else None
        if tip is None or tip == local.tip or not _held(local, tip):
            raise
        local.kept = {}
        got, _path = send_profile(local, profile(local, tip), sleep)
    file(local)
    return got


RESTART_WHY = {
    "profile_gone": "the commit it was filed at is no longer in this clone",
    "asked": "--restart",
    "pending": "the restart sent before was cut off",
}


def restart(local: Local, why: str, sleep: Callable[[float], None] = time.sleep, at: str = "HEAD") -> dict[str, Any]:
    """Measure the repository afresh: a profile of the tip (`at`) sent with `restart`, which the server files in place
    of the open run's measure. It is staged first (`profile.pending.json`, the run marked pending), so a run killed
    before the answer sends it again; only once the server takes it does it replace the bound profile and set aside
    what this clone finished for the old measure. A refusal drops it, and the clone stays as it was."""
    if say:
        say(
            f"{local.name}: measuring the latest commit afresh ({RESTART_WHY.get(why, why)}); this replaces the "
            "earlier measurements of this repository"
        )
    bound, local.kept = local.kept, {}
    doc = {**profile(local, at), "restart": True}
    codeval_runs.stage_profile(local.home, {k: v for k, v in local.kept.items() if k != "path"})
    codeval_runs.keep_run(local.home, restart="pending")
    try:
        got, _path = send_profile(local, doc, sleep)
    except errors.MetaphiError as e:
        if not e.retryable:
            codeval_runs.settle(local.home, took=False)
            codeval_runs.keep_run(local.home, restart=None)
            local.kept = bound
        raise
    codeval_runs.settle(local.home, took=True)
    codeval_runs.keep_run(local.home, run=got["run"], tip=local.tip, restart=None, measure=got.get("measure"))
    return got


def pending_restart(local: Local) -> str | None:
    """The tip of a restart sent and not answered, when its staged profile is still here."""
    if codeval_runs.run_state(local.home).get("restart") != "pending":
        return None
    staged = codeval_runs.kept_profile(local.home, codeval_runs.PENDING)
    return staged["tip"] if staged and _held(local, staged["tip"]) else None


def too_slow(results: dict[str, Any]) -> bool:
    """Whether the tip sent ran out of the batch's minutes: its error, build or a test run ended in a timeout."""
    sent = results.get("tip") or {}
    classes = [sent.get("error"), *(r.get("class") for r in (sent.get("build"), *sent.get("tests", [])) if r)]
    return any(c in ("env_timeout", "test_timeout") for c in classes)


say: Callable[[str], None] | None = None
stream: IO[str] | None = None


def _call(method: str, path: str, sleep: Callable[[float], None], answered: bool = True, **kw: Any) -> dict[str, Any]:
    with api.client(api.key(None, False)) as h:
        tries = 0
        while True:
            try:
                r = api.send(
                    h, method, path, sleep, retries=SEND_RETRIES, budget_s=SEND_BUDGET_S, timeout=SEND, say=say, **kw
                )
            except errors.MetaphiError as e:
                raise _named(e)
            if not answered:
                return {}
            try:
                got = r.json()
            except ValueError:
                got = None
            if isinstance(got, dict):
                return got
            tries += 1
            if tries > 1:
                raise errors.APIError(
                    f"the service answered HTTP {r.status_code} with no result; run the same command again",
                    code="unavailable",
                    retryable=True,
                    request_id=r.headers.get("request-id"),
                )
            sleep(2.0)


def _sent(local: Local, name: str, kind: str, doc: dict[str, Any]) -> tuple[bytes, Path]:
    """The one way a body leaves: checked against the schema (any free text refused), kept on this machine exactly as
    sent, and gzipped."""
    schema.check(kind, doc)
    body = packed(doc)
    return gzip.compress(body, mtime=0), codeval_runs.sent(local.home, name, body)


GZIP = {"content-encoding": "gzip", "content-type": "application/json"}


def _answer(got: dict[str, Any]) -> dict[str, Any]:
    """The server's answer, its instructions checked against the schema before any is followed."""
    if isinstance(got.get("next"), dict):
        instructed(got["next"])
    return got


def instructed(nxt: Any) -> None:
    """Refuse instructions the schema does not hold, before anything is run: the Next schema itself (its lists, runs
    and run_minutes within their maxima), and no more commits, mutants, blobs or runs than a Results can answer."""
    try:
        schema.check("Next", nxt)
    except schema.Refused as e:
        raise schema.Refused(e.where, e.why, "follow") from None
    held = schema.defs()["Results"]["properties"]
    for k in ("commits", "mutants", "blobs"):
        if len(nxt[k]) > held[k]["maxItems"]:
            raise schema.Refused(f"Next.{k}", f"a batch answers at most {held[k]['maxItems']}", "follow")
    if nxt["runs"] > (most := schema.defs()["Side"]["properties"]["tests"]["maxItems"]):
        raise schema.Refused("Next.runs", f"a side answers at most {most} test runs", "follow")


def revealable(local: Local, ids: list[int]) -> None:
    """The privacy floor on blob ids, held here whatever the server asks: only a source file at the tip of at least
    1 KiB. Any other id is refused before anything is run."""
    allowed = set(local.kept.get("revealable") or [])
    if bad := [i for i in ids if i not in allowed]:
        raise schema.Refused("Next.blobs", f"file {bad[0]} is not a source file of at least 1 KiB", "follow")


def send_profile(
    local: Local, doc: dict[str, Any], sleep: Callable[[float], None] = time.sleep
) -> tuple[dict[str, Any], Path]:
    body, path = _sent(local, "profile", "Profile", doc)
    return _answer(_call("POST", "/codeval", sleep, content=body, headers=GZIP)), path


def send_results(
    local: Local, run: str, doc: dict[str, Any], sleep: Callable[[float], None] = time.sleep
) -> tuple[dict[str, Any], Path]:
    body, path = _sent(local, "results", "Results", doc)
    return _answer(_call("POST", f"/codeval/{run}/results", sleep, content=body, headers=GZIP)), path


def get_run(run: str, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _answer(_call("GET", f"/codeval/{run}", sleep))


SCOPE = "kennet:codeval:write"


def not_open(step: str) -> errors.MetaphiError:
    """The organization has not opened CodeVal, and only an owner or admin can: the step as accounts said it."""
    return errors.MetaphiError(step, code="codeval_not_open")


def key_ready(sleep: Callable[[float], None] = time.sleep) -> None:
    cfg = config.load()
    said = cfg.get("no_api_key")
    if cfg.get("no_api_key_code") == "codeval_not_open" and said and not config.api_key(None, False):
        raise not_open(said)
    key = api.key(None, False)
    if key == cfg.get("api_key") and cfg.get("api_key_from") == "login" and isinstance(cfg.get("scopes"), list):
        if SCOPE in cfg["scopes"]:
            return
        if isinstance(cfg.get("no_codeval"), str) and cfg["no_codeval"]:
            raise not_open(cfg["no_codeval"])
        org = cfg.get("org_name") or cfg.get("org") or "this organization"
        raise _named(
            errors.AuthError(
                f"signed in, but the keys for {org} do not hold CodeVal; nothing was sent",
                code="insufficient_scope",
            )
        )
    _call("GET", "/codeval/access", sleep, answered=False)


# -- measuring what the server names -------------------------------------------------------------------------------

TESTS_MOST = 200_000


def _log(raw: dict[str, Any]) -> str:
    """The sha256 of a run's log; the log stays here."""
    h = hashlib.sha256()
    with contextlib.suppress(OSError), open(raw.get("log") or os.devnull, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def took(seconds: float) -> int:
    """A duration as the schema's bucket: the index of the last bound at or below it, in milliseconds. The duration
    itself is never sent."""
    bounds = schema.defs()["Step"]["properties"]["took"]["x-bounds-ms"]
    return max(bisect.bisect_right(bounds, max(seconds, 0.0) * 1000) - 1, 0)


def step(raw: dict[str, Any], key: bytes, tested: bool = True) -> dict[str, Any]:
    """One build or test run as sent: which step it reached, how it ended and why, never what it printed. A test run
    (`tested`) adds its counts and its cases, named by HMAC alone; diagnostic codes only as the shipped tables name
    them."""
    code = raw.get("exit")
    out: dict[str, Any] = {
        "step": raw.get("step") or ("test" if tested else "compile"),
        "class": raw.get("class") or ("ok" if code == 0 else "env_timeout" if code is None else "unknown"),
        "exit": code,
        "took": took(float(raw.get("seconds") or 0)),
        "log": _log(raw),
    }
    if raw.get("compile_kind") and out["class"] in extract.COMPILED:
        out["compile_kind"] = raw["compile_kind"]
    if raw.get("codes"):
        out["codes"] = list(dict.fromkeys(raw["codes"]))[: extract.MAX_CODES]
    if not tested:
        return out
    for k in ("reported", "passed"):
        if raw.get(k) is not None:
            out[k] = min(raw[k], TESTS_MOST)
    if raw.get("report") is not None:
        out["report"] = raw["report"]
    if raw.get("cases"):
        out["outcomes"] = dict(
            itertools.islice(((keyed(key, "test\0" + c), o) for c, o in sorted(raw["cases"].items())), TESTS_MOST)
        )
    return out


def side(raw: dict[str, Any], key: bytes, part: str = "Side") -> dict[str, Any]:
    """One tree built and tested, as the schema's `part` holds it: `Pre` (one test run), `Side` (a commit's or a
    mutant's post side) or `Tip`, the only one measured beyond pass and fail."""
    most = schema.defs()[part]["properties"]["tests"]["maxItems"]
    out: dict[str, Any] = {"tests": [step(t, key) for t in raw.get("tests", [])][:most]}
    if raw.get("build"):
        out["build"] = step(raw["build"], key, tested=False)
    if part == "Tip":
        if raw.get("offline"):
            out["offline"] = step(raw["offline"], key)
        for k in ("installed_bytes", "memory_peak_bytes"):
            if raw.get(k) is not None:
                out[k] = raw[k]
        if raw.get("coverage") is not None:
            out["coverage"] = raw["coverage"][: schema.defs()["Tip"]["properties"]["coverage"]["anyOf"][0]["maxItems"]]
    return out


def tip(raw: dict[str, Any] | None, key: bytes, source: set[int] | None = None) -> dict[str, Any]:
    """The tip as sent, never nothing when it was asked for. When it did not get as far as a passing build and test
    runs that ended in the code, `error` alone says why: the class its build ended in, the environment class a test
    run ended in, or `unknown` when it could not be run here. Coverage is
    sent for the tip's source files (`source`) only."""
    out = side(raw, key, "Tip") if raw else {"tests": []}
    build = out.get("build")
    ended = next((t["class"] for t in out["tests"] if t["class"].startswith("env_")), None)
    why = build["class"] if build and build["class"] != "ok" else ended if out["tests"] else "unknown"
    if why:
        return {"tests": [], "error": why}
    if out.get("coverage") is not None:
        out["coverage"] = [c for c in out["coverage"] if source is None or c["file"] in source]
    return out


def _complete(post: dict[str, Any], runs: int) -> bool:
    """A post side runs its tests exactly `runs` times, unless its build failed or a run ended in the environment,
    was cancelled or timed out, which stops the rest."""
    stopped = (post.get("build") or {}).get("class", "ok") != "ok" or any(
        t["class"].startswith("env_") or t["class"] in ("cancelled", "test_timeout") for t in post["tests"]
    )
    return stopped or len(post["tests"]) == runs


def _unit(raw: dict[str, Any] | None, key: bytes, sides: tuple[str, ...], runs: int) -> dict[str, Any]:
    """A commit (both sides) or a mutant (its post side) as sent, or why it did not run: `cancelled` when the time
    Metaphi AI set ran out first, `unknown` when it could not be run here, or ran short of `runs` test runs."""
    if raw is None:
        return {"error": "cancelled"}
    if raw.get("broke") or any(not raw.get(k) for k in sides):
        return {"error": "unknown"}
    out = {k: side(raw[k], key, "Pre" if k == "pre" else "Side") for k in sides}
    return out if _complete(out["post"], runs) else {"error": "unknown"}


ENVIRONMENT = {
    "env_network": ("the network failed", "check this machine reaches the internet and the services the build needs"),
    "env_resource": ("this machine ran out of memory or disk", "free memory and disk, or use a larger machine"),
    "env_timeout": ("it ran out of time", "give it a machine that builds and runs the tests faster"),
    "env_permission": ("it was refused permission", "let this user read and write the clone and run the tools"),
}


def environment(local: Local, results: dict[str, Any]) -> errors.MetaphiError | None:
    """Why to stop once a batch whose tip ended in this machine's environment has been sent: the server would ask for
    the tip again, and this machine would end the same way. None when the tip did not. A rerun measures it again."""
    cls = (results.get("tip") or {}).get("error")
    if not (isinstance(cls, str) and cls.startswith("env_")):
        return None
    what, fix = ENVIRONMENT.get(cls, ("something on this machine failed", "fix this machine"))
    stop = errors.MetaphiError(
        f"{local.name}: the latest commit could not be measured here because {what} ({cls}); that was sent, and "
        "nothing more is run until it is fixed",
        code="environment",
        next_step=f"{fix}, then run the same command again; it measures the latest commit again",
    )
    stop.context["class"] = cls
    return stop


OS = {"linux": "linux", "darwin": "darwin", "windows": "windows"}
ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}


def _memory() -> int:
    with contextlib.suppress(ValueError, OSError, AttributeError):
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    with contextlib.suppress(OSError, ValueError, subprocess.TimeoutExpired):
        said = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=10, check=False)
        return int(said.stdout.strip())
    return 0


def _digest(engine: str | None, image: str | None) -> str | None:
    if not engine or not image:
        return None
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        said = subprocess.run(
            [engine, "image", "inspect", "-f", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout.strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", said):
            return said
    return None


def machine(engine: str | None, image: str | None) -> dict[str, Any]:
    out = {
        "os": OS.get(platform.system().lower(), "other"),
        "arch": ARCH.get(platform.machine().lower(), "other"),
        "cpus": max(1, min(os.cpu_count() or 1, 4096)),
        "memory_bytes": _memory(),
        "runtime": Path(engine).name if engine and Path(engine).name in ("docker", "podman") else "host",
    }
    if digest := _digest(engine, image):
        out["image"] = digest
    return out


TOOLS = {
    "go": ("go", "version"),
    "python": ("python3", "--version"),
    "node": ("node", "--version"),
    "deno": ("deno", "--version"),
    "bun": ("bun", "--version"),
    "java": ("java", "-version"),
    "kotlin": ("kotlin", "-version"),
    "maven": ("mvn", "--version"),
    "gradle": ("gradle", "--version"),
    "sbt": ("sbt", "--version"),
    "dotnet": ("dotnet", "--version"),
    "ruby": ("ruby", "--version"),
    "php": ("php", "--version"),
    "rust": ("rustc", "--version"),
    "cargo": ("cargo", "--version"),
    "gcc": ("gcc", "--version"),
    "clang": ("clang", "--version"),
    "cmake": ("cmake", "--version"),
    "make": ("make", "--version"),
    "bazel": ("bazel", "--version"),
    "dart": ("dart", "--version"),
    "swift": ("swift", "--version"),
    "elixir": ("elixir", "--version"),
    "perl": ("perl", "--version"),
    "zig": ("zig", "version"),
    "uv": ("uv", "--version"),
    "pip": ("pip", "--version"),
    "poetry": ("poetry", "--version"),
    "npm": ("npm", "--version"),
    "pnpm": ("pnpm", "--version"),
    "yarn": ("yarn", "--version"),
}
"""The schema's toolchains, by the command that runs each and how it says its version."""
WORDS = {"python": ("python", "python3", "pytest", "pip"), "maven": ("mvn", "mvnw"), "gradle": ("gradle", "gradlew")}
SEMVER = re.compile(r"(?<![\d.])(\d{1,6})\.(\d{1,6})(?:\.(\d{1,6}))?")


def _version(said: str) -> str | None:
    m = SEMVER.search(said)
    return ".".join(x for x in m.groups() if x is not None) if m else None


def toolchain(commands: list[str], engine: str | None, image: str | None, log: Path) -> list[dict[str, str]]:
    """The tools the commands name, by their numeric versions as this machine (or the set-up image) reports them;
    each answer whole in `log`, which stays here."""
    words = set(re.findall(r"[\w.+-]+", " ".join(commands)))
    named = [t for t, (run, _a) in TOOLS.items() if words & {t, run, *WORDS.get(t, ())}]
    out: list[dict[str, str]] = []
    log.parent.mkdir(parents=True, exist_ok=True)
    for tool in named:
        run, flag = TOOLS[tool]
        argv = [engine, "run", "--rm", "--entrypoint", run, image, flag] if engine and image else [run, flag]
        if not (engine and image) and not shutil.which(run):
            continue
        try:
            got = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        said = got.stdout + got.stderr
        with log.open("a") as f:
            f.write(f"$ {' '.join(argv)}\n{said}\n")
        if got.returncode == 0 and (version := _version(said)):
            out.append({"tool": tool, "version": version})
    if engine and image and (version := _version(_engine_version(engine))):
        out.append({"tool": Path(engine).name, "version": version})
    return out[:32]


def _engine_version(engine: str) -> str:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        return subprocess.run(
            [engine, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    return ""


def _undetected(local: Local, logs: Path, box: str | None) -> codebase_checks.Stuck:
    """No build and test recipe worked: name a missing tool when the tries' logs show one."""
    import shlex

    tool = next((t for f in sorted(logs.glob("*.log")) if (t := reading.missing(f))), None)
    if tool and not box:
        said = f"could not build {local.name}: {tool} is missing on this machine"
        step = f"install {tool} (or start Docker or Podman), then run the same command again"
    else:
        said = f"could not find how to build and test {local.name}"
        step = f"give the command that builds and tests it: kennet codeval {shlex.quote(local.given)} -- '<command>'"
    return codebase_checks.Stuck(said, code="test_command_missing", next_step=step)


class Bench:
    """What a repository's runs share for as long as this process measures it: the container engine, the set-up
    image (pulled and set up once), one dependency cache, the services, and the recipe found from CI config and
    build files. `close` puts it all away."""

    def __init__(self, local: Local, given: str | None, say: Callable[[str], None]):
        from metaphi import codebase_setup

        self.local, self.given, self.say = local, given, say
        self.box = codebase_setup.engine() if given is None else None
        self.found: tuple[codebase_setup.Recipe, str | None] | None = None
        self.by: str | None = "seller" if given is not None else None
        self.tip: dict[str, Any] = {}
        self.services: codebase_env.Services | None = None
        self.images: list[str] = []
        self.net: str | None = None
        self.done = contextlib.ExitStack()
        self.logs = local.home / "logs"
        self.cache = local.home / "cache" if self.box else None
        self.ready = False

    def open(self) -> None:
        """Set up once: the network, the services, and the build and test recipe."""
        if self.ready:
            return
        self.ready = True
        from metaphi import codebase_recipes, codebase_setup

        local, say, box = self.local, self.say, self.box
        if self.given is None and not box:
            say("No Docker or Podman: running on this machine")
        self.done.callback(codebase_setup.forget, box, self.images)
        if box:
            net = f"kennet-{secrets.token_hex(6)}"
            if (
                subprocess.run(
                    [box, "network", "create", net], capture_output=True, timeout=120, check=False
                ).returncode
                == 0
            ):
                self.net = net
                codebase_setup.NETWORK[:] = ["--network", net]
                self.done.callback(
                    subprocess.run, [box, "network", "rm", net], capture_output=True, timeout=120, check=False
                )
            self.done.callback(codebase_setup.NETWORK.clear)
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)
        engine = box or (codebase_setup.engine() if local.kept.get("start") else None)
        if engine and local.kept.get("start"):
            tree = codebase_checks._tree(local.path, local.tip)
            self.services = codebase_env.Services(
                engine,
                local.kept["start"],
                lambda d, t=tree, r=local.path: codebase_checks._write(r, t, d),
                self.logs / "services.log",
                self.net,
            )
            self.done.callback(self.services.down)
            say(f"Starting {local.name}'s services…")
            if not self.services.up()["up"]:
                say(f"✗ {local.name}'s services did not start ({self.logs / 'services.log'})")
            elif self.services.shared:
                codebase_setup.NETWORK[:] = ["--network", f"container:{self.services.shared}"]
        if self.given is not None:
            return
        say(f"Setting up {local.name}…")
        tree = codebase_checks._tree(local.path, local.tip)
        recipe, _how = codebase_recipes.find(
            lambda d, r=local.path, t=tree: codebase_checks._write(r, t, d),
            _reader(local.path, tree),
            set(tree),
            "",
            box,
            self.logs / "setup",
            say,
            codebase_checks.RUN_MINUTES,
            self.cache,
            tip=self.tip,
        )
        if recipe is None:
            raise _undetected(local, self.logs / "setup", box)
        if recipe.image:
            self.images.append(recipe.image)
        self.found = (recipe, box)
        self.by = "ci" if codebase_recipes.from_ci(recipe, set(tree), _reader(local.path, tree)) else "build_files"

    def commands(self) -> dict[str, Any] | None:
        key = self.local.key
        if self.given is not None:
            return {"by": "seller", "build": None, "test": keyed(key, self.given)}
        if not self.found:
            return None
        r = self.found[0]
        build = " && ".join(x for x in (r.setup, r.build) if x)
        return {"by": self.by, "build": keyed(key, build) if build else None, "test": keyed(key, r.test)}

    def toolchain(self) -> list[dict[str, str]]:
        r = self.found[0] if self.found else None
        said = [self.given] if self.given is not None else [r.setup, r.build, r.test] if r else []
        return toolchain([x for x in said if x], self.box, r.image if r else None, self.logs / "toolchain.log")

    def close(self) -> None:
        if self.box and self.cache and self.cache.exists():
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                image = self.found[0].image if self.found and self.found[0].image else CHMOD_IMAGE
                subprocess.run(
                    [
                        self.box,
                        "run",
                        "--rm",
                        "-v",
                        f"{self.cache}:/c",
                        "--entrypoint",
                        "chmod",
                        image,
                        "-R",
                        "a+rwX",
                        "/c",
                    ],
                    capture_output=True,
                    timeout=600,
                    check=False,
                )
        self.done.close()


def measured(local: Local, bench: Bench, nxt: dict[str, Any], say: Callable[[str], None] = print) -> dict[str, Any]:
    """Do exactly what one batch of instructions says, and answer it in the schema's terms. Each finished unit is
    kept in the run's journal first, so a run killed part way goes on where it stopped."""
    import dataclasses

    began = time.monotonic()
    bench.open()
    ledger = codeval_runs.Run(local.home)
    plan = codebase_checks.Plan(
        test=bench.given,
        run_minutes=float(nxt["run_minutes"]),
        hours=float(nxt["hours_left"]),
        logs=local.home / "logs",
        cache=bench.cache,
        ledger=ledger,
        alone=bench.services is not None or bench.given is not None,
    )
    plan = dataclasses.replace(plan, logs=local.home / "logs" / f"batch-{nxt['batch']}")
    kept = local.kept
    instructed(nxt)
    revealable(local, nxt["blobs"])
    mutants = [{"file": m["file"], "op": m["op"], "nth": m["nth"]} for m in nxt["mutants"]]
    key, source = local.key, set(kept.get("source") or [])

    def ran(targets: list[int], edits: list[dict]) -> dict[str, Any]:
        return codebase_checks.run(
            local.path,
            local.name,
            local.tip,
            kept["commits"],
            kept["order"],
            targets,
            int(nxt["runs"]),
            plan,
            say,
            bench.found,
            kept["paths"],
            bench.tip or None,
            stream,
            edits,
        )

    out: dict[str, Any] = {
        "schema": RESULTS,
        "cli": __version__,
        "key_id": key_id(local.key),
        "rules": extract.rules_hash(),
        "batch": nxt["batch"],
        "measure": nxt["measure"],
        "machine": machine(bench.box, bench.found[0].image if bench.found else None),
        "toolchain": bench.toolchain(),
        "commits": [],
        "mutants": [],
        "blobs": [],
    }
    # The tip first: a tip that fails answers the batch alone, and nothing else is run.
    raw = ran([], []) if nxt["tip"] and (nxt["commits"] or mutants) else None
    failed = raw is not None and "error" in tip(raw.get("tip"), key, source)
    if not failed:
        raw = ran(list(nxt["commits"]), mutants)
        runs = int(nxt["runs"])
        out["commits"] = [
            {"commit": i, **_unit(r, key, ("pre", "post"), runs)}
            for i, r in zip(nxt["commits"], raw["commits"], strict=False)
        ]
        out["mutants"] = [
            {**m, **_unit(r, key, ("post",), runs)} for m, r in zip(mutants, raw["mutants"], strict=False)
        ]
        answered = {c["commit"] for c in out["commits"]}
        out["commits"] += [{"commit": i, "error": "cancelled"} for i in nxt["commits"] if i not in answered]
        out["mutants"] += [{**m, "error": "cancelled"} for m in mutants[len(out["mutants"]) :]]
        out["blobs"] = blobs(local, nxt["blobs"])
    if (commands := bench.commands()) is not None:
        out["commands"] = commands
    if bench.services is not None:
        got = bench.services.got
        out["services"] = {
            **{k: v for k, v in got.items() if k in ("from", "count", "up", "refused")},
            "took": took(float(got.get("seconds") or 0)),
        }
    if nxt["tip"]:
        out["tip"] = tip(raw.get("tip") if raw else None, key, source)
        if "error" in out["tip"]:
            out.update(commits=[], mutants=[], blobs=[])
    out["took"] = took(time.monotonic() - began)
    return out


def blobs(local: Local, ids: list[int]) -> list[dict[str, Any]]:
    """The raw git blob ids at the tip of the files the server names."""
    tree, paths = codebase_checks._tree(local.path, local.tip), local.kept.get("paths") or {}
    allowed = set(local.kept.get("revealable") or [])
    return [
        {"file": i, "blob": tree[paths[i]][1]}
        for i in ids
        if i in allowed and paths.get(i) in tree and re.fullmatch(r"[0-9a-f]{40}", tree[paths[i]][1])
    ][:MAX_BLOBS]
