from __future__ import annotations

import collections
import contextlib
import gzip
import hashlib
import io
import itertools
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any

import httpx

from metaphi import __version__, api, codebase_checks, codebase_env, codebase_sites, config, deid, deid_model, errors
from metaphi import codebase_reading as reading
from metaphi.o200k import tokens as _tokens
from metaphi.steps import Steps

SCHEMA = "kennet.repo_metadata.v16"
CHECKS_SCHEMA = "kennet.repo_checks.v4"
LOGS_SCHEMA = "kennet.repo_logs.v2"
LOG_FILE_MOST = 4 << 20
NOT_PACKED = re.compile(r"(^|/)(conversation\.jsonl|run-\d+\.log)$")
NOTICE = "Runs on this machine. Nothing is sent until the owner runs `kennet codeval send`."
MAX_BLOB = 2_000_000

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
    r"(^|/)(tests?|spec|specs|testdata|testutils?|__tests__|fixtures?|[\w.-]*-tests?(-[\w.-]*)?\.package)(/|$)"
    r"|Tests?\.class(/|\.st$)|Tests?\.(java|php|cs|kt|groovy)$|_test\.|\.test\.|\.spec\.|(^|/)test_|tests?\.[a-z]+$",
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
IDENT = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{2,}")
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
GIT_FLOOR = (2, 32)
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
TAG_FORMAT = (
    "%(objecttype)%00%(objectname)%00%(*objectname)%00%(taggerdate:unix)%00%(creatordate:unix)%00%(refname:lstrip=2)"
)


def _tags(repo: Path, order: dict[str, int]) -> list[dict[str, Any]]:
    out = []
    for line in _text(repo, "for-each-ref", f"--format={TAG_FORMAT}", "refs/tags").splitlines():
        kind, obj, target, tagged, created, name = line.split("\0")
        at = tagged or created
        out.append(
            {
                "commit": order.get(target if kind == "tag" else obj, 0),
                "at": int(at) if at.lstrip("-").isdigit() else None,
                "annotated": kind == "tag",
                "release": bool(RELEASE.match(name)),
            }
        )
    return sorted(out, key=lambda t: (t["commit"], t["at"] or 0))


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


def _time(said: str) -> int:
    return int(said) if said.lstrip("-").isdigit() and -62135596800 <= int(said) <= 253402300799 else 0


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


def _subject_key(subject: str, salt: bytes) -> str:
    said = HEX_RUN.sub("", TICKET.sub("", subject)).lower()
    said = " ".join(re.sub(r"\d", "0", said).split())
    return hashlib.sha256(salt + said.encode()).hexdigest()[:16]


def _path_words(path: str) -> set[str]:
    return {w.lower() for part in re.split(r"[/._-]+", path) for w in PATH_WORD.findall(part)}


def _commits(
    repo: Path,
    authors: _Ids,
    domains: _Ids,
    people: list[dict],
    order: dict[str, int],
    salt: bytes,
    words: dict[int, set[str]],
) -> list[dict]:
    fields = "%H%x00%P%x00%aE%x00%cE%x00%at%x00%ct%x00%ad%x00%B%x00%(trailers:only,unfold)%x00"
    raw = _out(repo, "log", "HEAD", "--topo-order", "--reverse", "--date=format:%z", f"--format={fields}").split(b"\0")

    def person(address: str) -> int:
        key = address.lower()
        if key not in authors:
            people.append(
                {"author": authors.of(key), "domain": domains.of(key.rpartition("@")[2]), "bot": bool(BOT.search(key))}
            )
        return authors[key]

    first = set(_text(repo, "rev-list", "--first-parent", "HEAD").split())
    rows = [[x.decode("utf-8", "replace") for x in raw[i : i + 9]] for i in range(0, len(raw) - 8, 9)]
    for row in rows:
        order[row[0].strip()] = len(order) + 1
    commits = []
    for row in rows:
        sha, parents, author, committer, at, ct, tz, msg, block = row
        sha = sha.strip()
        back = REVERTS.search(msg)
        subject, *body = msg.split("\n")
        words[order[sha]] = {w.lower() for w in WORD.findall(msg) if len(w) >= 4}
        commits.append(
            {
                "commit": order[sha],
                "parents": [order.get(p, 0) for p in parents.split()],
                "first_parent": sha in first,
                "author": person(author),
                "committer": person(committer),
                "authored_at": _time(at),
                "committed_at": _time(ct),
                "tz": tz if re.fullmatch(r"[+-]\d{4}", tz) else "+0000",
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
                "subject_key": _subject_key(subject, salt),
                "files": [],
            }
        )
    return commits


TRAILER_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _trailers(block: str) -> list[dict[str, str]]:
    out = []
    for line in block.split("\n"):
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if sep and TRAILER_KEY.fullmatch(key):
            m = EMAIL_DOMAIN.search(value)
            out.append({"key": key, "domain": m[1].lower().rstrip(".") if m else ""})
    return out


def _changes(repo: Path, by_sha: dict[str, dict], paths: _Ids, order: dict[str, int]) -> None:
    tokens = iter(
        _out(
            repo, "log", "HEAD", "-M", "--diff-merges=first-parent", "--raw", "--numstat", "-z", "--format=%x01%H"
        ).split(b"\0")
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
                "hunks": 0,
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


def _hunks(repo: Path, by_sha: dict[str, dict], paths: _Ids) -> None:
    p = subprocess.Popen(
        _git(
            repo,
            "log",
            "HEAD",
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
    counts: dict[str, int] = {}
    spans: dict[str, list[tuple[int, int]]] = {}
    words: dict[str, set[bytes]] = {"test+": set(), "source+": set(), "source-": set()}
    row: dict | None = None

    def close() -> None:
        if row is not None:
            by_id = {paths[path]: n for path, n in counts.items() if path in paths}
            gaps = {
                paths[path]: [max(0, b[0] - a[1]) for a, b in itertools.pairwise(s)]
                for path, s in spans.items()
                if path in paths
            }
            for change in row["files"]:
                change["hunks"] = by_id.get(change["file"], 0)
                change["gaps"] = gaps.get(change["file"], [])
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
            row, counts, spans, path = by_sha.get(line[1:].strip().decode()), {}, {}, None
            words = {"test+": set(), "source+": set(), "source-": set()}
        elif line.startswith(b"+++ "):
            if line[4:] != b"/dev/null\n":
                got = _unquote(line[4:])
                path = got[2:] if got[:2] == "b/" else got
        elif line.startswith(b"--- "):
            got = _unquote(line[4:])
            path = got[2:] if got[:2] == "a/" else None
        elif line.startswith(b"@@ ") and path is not None:
            counts[path] = counts.get(path, 0) + 1
            m = re.match(rb"@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
            if m:
                old, gone, came = int(m[1]), int(m[2] or 1), int(m[3] or 1)
                skip = gone + came
                spans.setdefault(path, []).append((old, old + gone))
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
    tails, leaves = set(), set()
    for path in tip:
        parts = os.path.splitext(path)[0].split("/")
        leaves.add(parts[-1].upper())
        tails.update("/".join(parts[i:]) for i in range(len(parts)))

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
        return any(
            "/".join(parts[i:]) in tails for i in range(len(parts)) if len(parts) - i >= (2 if len(parts) > 2 else 1)
        )

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


def compute(repo: Path, name: str, keep: dict | None = None) -> dict[str, Any]:
    authors, domains, paths, dirs = _Ids(), _Ids(), _Ids(), _Ids()
    salt = secrets.token_bytes(32)
    people: list[dict] = []
    order: dict[str, int] = {}
    words: dict[int, set[str]] = {}
    commits = _commits(repo, authors, domains, people, order, salt, words)
    by_sha = {sha: commits[i - 1] for sha, i in order.items()}
    _changes(repo, by_sha, paths, order)
    named = {i: _path_words(path) for path, i in paths.items()}
    for c in commits:
        changed = {i for x in c["files"] for i in (x["file"], x.get("old_file")) if i is not None}
        c["path_terms"] = len(words[c["commit"]] & set().union(*(named[i] for i in changed)))
    _hunks(repo, by_sha, paths)
    tree: dict[str, tuple[str, str, int]] = {}
    for entry in _out(repo, "ls-tree", "-r", "-l", "-z", "HEAD").split(b"\0"):
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
            "known_name": (b if (b := os.path.basename(path).lower()) in KNOWN else None),
            "at_tip": path in tree,
        }
        if path in tree:
            if tree[path][0] != "160000":
                row["blob"] = hashlib.sha256(salt + tree[path][1].encode()).hexdigest()[:16]
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
    tip = _text(repo, "rev-parse", "HEAD")
    env, start = codebase_env.needs(
        {p for p, (mode, _s, _z) in tree.items() if mode != "160000"},
        lambda p: next((b for _s, b in _blobs(repo, [tree[p][1]])), None) if p in tree else None,
    )
    pack = re.search(r"size-pack: (\d+)", _text(repo, "count-objects", "-v"))
    if keep is not None:
        keep.update(
            path=repo,
            tip=tip,
            commits=commits,
            order=order,
            start=start,
            paths={row["file"]: path for path, row in rows.items() if row["at_tip"]},
            authors=dict(authors),
            domains=dict(domains),
            people=_people(repo),
            remotes=sorted(set(_text(repo, "remote", "-v").split()[1::3])),
        )
    return {
        "name": name,
        "tip": tip,
        "roots": _text(repo, "rev-list", "--max-parents=0", "HEAD").split(),
        "git": {
            "branches": len(_text(repo, "for-each-ref", "--format=x", "refs/heads", "refs/remotes").split()),
            "tag_refs": _tags(repo, order),
            "submodules": sum(m == "160000" for m, _s, _z in tree.values()),
            "lfs": lfs,
            "pack_bytes": int(pack[1]) * 1024 if pack else 0,
        },
        "licenses": sorted(licenses),
        "env": env,
        "authors": people,
        "dirs": [{"dir": d, "parent": up} for d, up in tree_dirs.items()],
        "commits": commits,
        "files": sorted(rows.values(), key=lambda r: r["file"]),
    }


def _people(repo: Path) -> list[list[str]]:
    """Each author's and committer's name with the address it came with: kept on this machine, where
    de-identification replaces the names in logs with the profile's author ids."""
    said = _out(repo, "log", "HEAD", "--format=%aN%x00%aE%x00%cN%x00%cE%x00").decode("utf-8", "replace")
    fields = [f.strip() for f in said.split("\0")]
    return sorted({(n, e.lower()) for n, e in zip(fields[0::2], fields[1::2], strict=False) if n and e})


SKIPPED_FIX = {
    "shallow": "a shallow clone has only part of the history: run `git fetch --unshallow` in it, or give its URL",
    "partial_clone": "a partial clone lacks file contents: clone it in full, or give its URL",
    "not_a_git_repository": "give the repository's top folder (where .git is), or its URL",
    "no_commits": "it has no commits yet",
    "clone_failed": "Check you can `git clone` that URL in this terminal",
    "unreadable": "git could not read it",
}


_URL = re.compile(r"^(?:(?:https?|ssh|git|file)://\S+|[\w.-]+@[\w.-]+:\S+)$")


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


@contextlib.contextmanager
def profiled(paths: list[str], say: Callable[[str], None] = print) -> Iterator[tuple[dict[str, Any], list[dict]]]:
    _git_ready()
    repos, skipped, used, kept = [], [], set(), []
    with tempfile.TemporaryDirectory(prefix="kennet-clone-") as scratch:
        for n, given in enumerate(paths, 1):
            url = is_url(given)
            path = None if url else Path(given).expanduser().resolve()
            name = _name_of(given, n, used)
            if url:
                say(f"Cloning {name}\u2026")
                path = Path(scratch) / f"{n}.git"
                failed = _clone(given, path)
                if failed:
                    say(f"\u2717 Skipped {name}: clone failed ({failed}). {SKIPPED_FIX['clone_failed']}")
                    skipped.append({"name": name, "reason": "clone_failed"})
                    continue
            reason = why_not(path) if path.is_dir() else "not_a_git_repository"
            if reason:
                say(f"\u2717 Skipped {name}: {SKIPPED_FIX[reason]}")
                skipped.append({"name": name, "reason": reason})
                continue
            local: dict = {"name": name, "given": given}
            began = time.monotonic()
            try:
                repos.append(compute(path, name, local))
                local["seconds"] = time.monotonic() - began
                kept.append(local)
            except (Unreadable, ValueError, OSError):
                say(f"\u2717 Skipped {name}: {SKIPPED_FIX['unreadable']}")
                skipped.append({"name": name, "reason": "unreadable"})
        yield {"schema": SCHEMA, "tool_version": __version__, "repos": repos, "skipped": skipped}, kept


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


def _key(pairs: list[list[str]]) -> str:
    return "run_" + hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:16]


def run_id(doc: dict[str, Any]) -> str:
    return _key([[r["name"], r["tip"]] for r in doc["repos"]])


RUNS = 3
"""Test runs at each past commit, after the change: what tells a flaky test from a broken one."""


def checked(
    kept: list[dict],
    picks: list[list[int]],
    edits: list[list[dict]],
    plan: codebase_checks.Plan,
    say: Callable[[str], None] = print,
    steps: Steps | None = None,
) -> dict[str, Any]:
    """Build and test each repository's tip and its picked past commits. Raises `codebase_checks.Stuck` when a
    person must act; every unit finished before it is kept in the plan's ledger."""
    import dataclasses

    from metaphi import codebase_recipes, codebase_setup

    out, images = [], []
    steps = steps or Steps()
    box = codebase_setup.engine() if plan.test is None else None
    if plan.test is None and not box:
        say("No Docker or Podman: running on this machine")
    engine = box or (codebase_setup.engine() if any(k.get("start") for k in kept) else None)
    with (
        tempfile.TemporaryDirectory(prefix="kennet-cache-", ignore_cleanup_errors=True) as caches,
        contextlib.ExitStack() as done,
    ):
        done.callback(codebase_setup.forget, box, images)
        net = None
        if box:
            net = f"kennet-{secrets.token_hex(6)}"
            if (
                subprocess.run(
                    [box, "network", "create", net], capture_output=True, timeout=120, check=False
                ).returncode
                == 0
            ):
                codebase_setup.NETWORK[:] = ["--network", net]
                done.callback(
                    subprocess.run, [box, "network", "rm", net], capture_output=True, timeout=120, check=False
                )
            else:
                net = None
            done.callback(codebase_setup.NETWORK.clear)
        for n, (local, targets, planted) in enumerate(zip(kept, picks, edits, strict=True)):
            if plan.ledger and (finished := plan.ledger.get(local["name"], "repo")) is not None:
                say(f"{local['name']}: checked before; using that result")
                out.append(finished)
                continue
            repo, logs = local["path"], (plan.logs or Path.cwd() / "kennet-codeval") / local["name"]
            mine = dataclasses.replace(
                plan, cache=Path(caches) / str(n) if box else None, steps=Steps(logs / "steps.jsonl")
            )
            mine.steps.total["history"] = local.get("seconds", 0.0)
            if mine.cache:
                mine.cache.mkdir()
            found, setup, tip = None, [] if plan.test is None else None, {}
            services = None
            if engine and local.get("start"):
                tip_tree = codebase_checks._tree(repo, local["tip"])
                services = codebase_env.Services(
                    engine,
                    local["start"],
                    lambda d, t=tip_tree, r=repo: codebase_checks._write(r, t, d),
                    logs / "services.log",
                    net if box else None,
                )
                say(f"Starting {local['name']}'s services\u2026")
                with mine.steps.step("services"):
                    up = services.up()
                if not up["up"]:
                    say(f"\u2717 {local['name']}'s services did not start ({logs / 'services.log'})")
                elif services.shared:
                    codebase_setup.NETWORK[:] = ["--network", f"container:{services.shared}"]
            try:
                if plan.test is None:
                    say(f"Setting up {local['name']}\u2026")
                    tree = codebase_checks._tree(repo, local["tip"])
                    with mine.steps.step("setup"):
                        recipe, how = codebase_recipes.find(
                            lambda d, r=repo, t=tree: codebase_checks._write(r, t, d),
                            _reader(repo, tree),
                            set(tree),
                            "",
                            box,
                            logs / "setup",
                            say,
                            plan.run_minutes,
                            mine.cache,
                            mine.steps,
                            tip=tip,
                        )
                    setup.append(how)
                    if recipe is None:
                        raise _undetected(local, logs / "setup", box)
                    if recipe.image:
                        images.append(recipe.image)
                    found = (recipe, box)
                got = codebase_checks.run(
                    repo,
                    local["name"],
                    local["tip"],
                    local["commits"],
                    local["order"],
                    targets,
                    RUNS,
                    mine,
                    say,
                    found,
                    local.get("paths"),
                    tip or None,
                    stream,
                    planted,
                )
            except errors.MetaphiError:
                raise
            except Exception as e:  # noqa: BLE001
                say(f"\u2717 {local['name']}: tests stopped ({type(e).__name__}: {e})")
                got = {"from": "tip", "runs": RUNS, "test_given": plan.test is not None, "stopped": "error"}
                got.update(commits=[], edits=[])
            finally:
                if services:
                    services.down()
                    codebase_setup.NETWORK[:] = ["--network", net] if net else []
            got["commands"] = _commands(found, plan.test)
            got["toolchain"] = _toolchain(found, plan.test)
            if setup:
                got["setup"] = setup
            if services:
                got["services"] = services.got
            got["seconds"] = mine.steps.seconds()
            out.append(got)
            if plan.ledger and got.get("stopped") != "error":
                plan.ledger.put(local["name"], "repo", "", got)
            if box and mine.cache:
                try:
                    subprocess.run(
                        [
                            box,
                            "run",
                            "--rm",
                            "-v",
                            f"{mine.cache}:/c",
                            "--entrypoint",
                            "chmod",
                            found[0].image if found and found[0].image else CHMOD_IMAGE,
                            "-R",
                            "a+rwX",
                            "/c",
                        ],
                        capture_output=True,
                        timeout=600,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
    return {"schema": CHECKS_SCHEMA, "tool_version": __version__, "repos": out, "seconds": steps.seconds()}


def blobs(local: dict, ids: list[int]) -> dict[str, str]:
    """The raw git blob ids at the tip of the files `codeval_draws.reveal` names: whether the code is public."""
    tree, paths = codebase_checks._tree(local["path"], local["tip"]), local.get("paths") or {}
    return {
        str(i): tree[paths[i]][1]
        for i in ids
        if paths.get(i) in tree and re.fullmatch(r"[0-9a-f]{40}", tree[paths[i]][1])
    }


def _undetected(local: dict, logs: Path, box: str | None) -> codebase_checks.Stuck:
    """No build and test recipe worked: name a missing tool when the tries' logs show one."""
    tool = next((t for f in sorted(logs.glob("*.log")) if (t := reading.missing(f))), None)
    paths = shlex.join([local["given"]])
    if tool and not box:
        said, step = (
            f"could not build {local['name']}: {tool} is missing on this machine",
            f"install {tool} (or start Docker or Podman), then run the same command again",
        )
    else:
        said = f"could not find how to build and test {local['name']}"
        step = f"give the command that builds and tests it: kennet codeval {paths} -- '<build and test command>'"
    return codebase_checks.Stuck(said, code="test_command_missing", next_step=step)


def _commands(found: tuple | None, given: str | None) -> dict[str, str | None]:
    """The commands that ran, as they will be shown before sending."""
    if given is not None:
        return {"setup": None, "build": None, "test": given}
    recipe = found[0] if found else None
    return {k: getattr(recipe, k, None) for k in ("setup", "build", "test")}


TOOLS = ("java", "mvn", "gradle", "go", "node", "npm", "python3", "cargo", "rustc", "gcc", "cmake", "dotnet", "make")


def _toolchain(found: tuple | None, given: str | None) -> dict[str, str]:
    """The versions of the tools the commands name, as this machine reports them; in a container, its image."""
    recipe = found[0] if found else None
    if recipe is not None and recipe.image and found[1]:
        return {"image": recipe.image}
    words = set(re.findall(r"[\w.+-]+", " ".join(filter(None, _commands(found, given).values()))))
    out = {}
    for tool in TOOLS:
        if tool not in words or not shutil.which(tool):
            continue
        try:
            got = subprocess.run(
                [tool, "-version" if tool == "java" else "--version"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        line = next((x.strip() for x in (got.stdout + got.stderr).splitlines() if x.strip()), "")
        if line:
            out[tool] = line[:200]
    return out


SEND = httpx.Timeout(30.0, connect=10.0, read=90.0, write=600.0)
SEND_RETRIES = 4
SEND_BUDGET_S = 300.0


def _named(e: errors.MetaphiError) -> errors.MetaphiError:
    if e.code == "insufficient_scope":
        e.message += (
            "; it takes kennet:codeval:write. `kennet login` hands this machine keys that hold it "
            "once your organization does"
        )
        e.next_step = "kennet login"
    return e


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


def post(body: bytes, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _call(
        "POST",
        "/codeval",
        sleep,
        content=body,
        headers={"content-encoding": "gzip", "content-type": "application/json"},
    )


def fingerprint(roots: list[str]) -> str:
    """A repository's name for the service: the sha256 of its root commit ids, sorted, one per line."""
    return hashlib.sha256("\n".join(sorted(roots)).encode()).hexdigest()


def history(local: dict) -> list[dict[str, Any]]:
    """The first-parent history, oldest first: commit ids and author times, nothing else."""
    by_id = {c["commit"]: c for c in local["commits"]}
    return [
        {"id": sha, "at": by_id[i]["authored_at"]}
        for sha, i in sorted(local["order"].items(), key=lambda x: x[1])
        if by_id[i]["first_parent"]
    ]


def picks(doc: dict[str, Any], kept: list[dict], sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Ask which past commits to test. The one call before `kennet codeval send`: ids and times only."""
    body = {
        "tool_version": __version__,
        "repos": [
            {"fingerprint": fingerprint(r["roots"]), "commits": history(k)}
            for r, k in zip(doc["repos"], kept, strict=True)
        ],
    }
    got = _call("POST", "/codeval/picks", sleep, json=body)
    if not isinstance(got.get("pick_id"), str) or not isinstance(got.get("repos"), list):
        raise errors.APIError("the service sent no picks; run the same command again", code="unavailable")
    return got


def targets(doc: dict[str, Any], kept: list[dict], picked: dict[str, Any]) -> list[list[int]]:
    """Each repository's picked commits, as its commit numbers in the profile."""
    by_print = {r.get("fingerprint"): r.get("commits") or [] for r in picked["repos"] if isinstance(r, dict)}
    return [
        [k["order"][sha] for sha in by_print.get(fingerprint(r["roots"]), []) if sha in k["order"]]
        for r, k in zip(doc["repos"], kept, strict=True)
    ]


SCOPE = "kennet:codeval:write"


def key_ready(sleep: Callable[[float], None] = time.sleep) -> None:
    key, cfg = api.key(None, False), config.load()
    if key == cfg.get("api_key") and cfg.get("api_key_from") == "login" and isinstance(cfg.get("scopes"), list):
        if SCOPE in cfg["scopes"]:
            return
        raise _named(errors.AuthError("this API key does not hold access to CodeVal", code="insufficient_scope"))
    _call("GET", "/codeval/access", sleep, answered=False)


def post_logs(ref: str, body: bytes, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _call("POST", f"/codeval/{ref}/logs", sleep, content=body, headers={"content-type": "application/gzip"})


def post_checks(ref: str, body: bytes, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _call(
        "POST",
        f"/codeval/{ref}/checks",
        sleep,
        content=body,
        headers={"content-encoding": "gzip", "content-type": "application/json"},
    )


def packed(doc: dict[str, Any]) -> bytes:
    return gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0)


PAYLOAD = "payload"
PROFILE, CHECKS, LOGS = "profile.json.gz", "checks.json.gz", "logs.tar.gz"


def bundle_sha256(checks: bytes, logs: bytes) -> str:
    """The sha256 of the manifest of the checks and logs (`<sha256>  <file>` lines). The profile carries it, so the
    three files sent are bound to each other."""
    lines = "".join(f"{hashlib.sha256(b).hexdigest()}  {name}\n" for name, b in ((CHECKS, checks), (LOGS, logs)))
    return hashlib.sha256(lines.encode()).hexdigest()


def payload(home: Path, doc: dict[str, Any], report: dict[str, Any], kept: list[dict], pick_id: str) -> dict:
    """Write what `kennet codeval send` would send into <home>/payload/, de-identified here, with its manifest.
    Raises `deid.Unsafe`, and writes nothing, when a known identifier is left."""
    checks, checks_said = checks_packed(report, kept)
    logs, logs_said = logs_packed(home / "logs", pick_id, kept)
    bundle = bundle_sha256(checks, logs)
    profile = packed({**doc, "pick_id": pick_id, "bundle_sha256": bundle})
    out = home / PAYLOAD
    out.mkdir(exist_ok=True)
    files = []
    for name, body, said in ((PROFILE, profile, None), (CHECKS, checks, checks_said), (LOGS, logs, logs_said)):
        (out / name).write_bytes(body)
        files.append(
            {
                "file": name,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                **({"deidentified": said["replaced"]} if said else {}),
            }
        )
    manifest = {
        "pick_id": pick_id,
        "bundle_sha256": bundle,
        "files": files,
        "model_pass": logs_said["model"],
        "commands": [
            {"repo": k["name"], **(r.get("commands") or {})} for k, r in zip(kept, report["repos"], strict=True)
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def _cut(data: bytes) -> bytes:
    """A log over LOG_FILE_MOST keeps its head and tail, cut at line ends so no identifier is split."""
    if len(data) <= LOG_FILE_MOST:
        return data
    head = data[: 512 << 10]
    tail = data[-(LOG_FILE_MOST - (512 << 10)) :]
    head = head[: head.rfind(b"\n") + 1] or head
    tail = tail[tail.find(b"\n") + 1 :] if b"\n" in tail else tail
    return head + b"[... cut by kennet codeval ...]\n" + tail


def _scrubbers(kept: list[dict]) -> tuple[list[deid.Scrubber], str]:
    model, said = deid_model.load(say)
    return [deid.Scrubber(k, model) for k in deid.known(kept)], said


def logs_packed(root: Path, pick_id: str, kept: list[dict]) -> tuple[bytes, dict[str, Any]]:
    """The run's logs as a tar.gz, de-identified here, with what was replaced by kind. Raises `deid.Unsafe`, and
    nothing is sent, when a known identifier is still in the packed bytes."""
    scrubbers, said = _scrubbers(kept)
    buf, counts = io.BytesIO(), collections.Counter()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:

        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))

        add(
            "kennet-logs.json",
            json.dumps({"schema": LOGS_SCHEMA, "pick_id": pick_id, "tool_version": __version__}).encode(),
        )
        for n, (local, scrub) in enumerate(zip(kept, scrubbers, strict=True)):
            repo = local["name"]
            for f in sorted((root / repo).rglob("*")) if (root / repo).is_dir() else []:
                name = f"{n}/{f.relative_to(root / repo).as_posix()}"
                if f.is_symlink() or not f.is_file() or NOT_PACKED.search(name):
                    continue
                try:
                    data = f.read_bytes()
                except OSError:
                    continue
                add(scrub.text(name), scrub.bytes(_cut(data)))
    for scrub in scrubbers:
        counts.update(scrub.counts)
    body = buf.getvalue()
    _unsafe(gzip.decompress(body), scrubbers, "the logs")
    return body, deid.summary(counts, said)


def checks_packed(report: dict[str, Any], kept: list[dict]) -> tuple[bytes, dict[str, Any]]:
    """The checks report, its text de-identified here as the logs are; `deid.Unsafe` when an identifier is left."""
    scrubbers, said = _scrubbers(kept)
    repos = report.get("repos") or []
    scrubbed = {k: v for k, v in report.items() if k != "repos"}
    scrubbed["repos"] = [s.doc(r) for s, r in zip(scrubbers, repos, strict=False)] + repos[len(scrubbers) :]
    body = packed(scrubbed)
    _unsafe(gzip.decompress(body), scrubbers, "the checks report")
    counts: collections.Counter = collections.Counter()
    for scrub in scrubbers:
        counts.update(scrub.counts)
    return body, deid.summary(counts, said)


def _unsafe(data: bytes, scrubbers: list[deid.Scrubber], what: str) -> None:
    left: collections.Counter = collections.Counter()
    for scrub in scrubbers:
        left |= scrub.known.left(data)
    deid.check(left, what)


__all__ = [
    "CHECKS_SCHEMA",
    "LOGS_SCHEMA",
    "NOTICE",
    "SCHEMA",
    "checked",
    "checks_packed",
    "compute",
    "key_ready",
    "logs_packed",
    "packed",
    "payload",
    "picks",
    "post",
    "post_checks",
    "post_logs",
    "profiled",
    "why_not",
]
