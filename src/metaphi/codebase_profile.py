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
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx

from metaphi import __version__, codebase_checks, codebase_env, codebase_sites, config, errors
from metaphi.o200k import tokens as _tokens
from metaphi.steps import Steps
from metaphi.twin import _client, _key, _send

SCHEMA = "kennet.repo_metadata.v13"
CHECKS_SCHEMA = "kennet.repo_checks.v1"
LOGS_SCHEMA = "kennet.repo_logs.v1"
LOG_FILE_MOST = 4 << 20
NOT_PACKED = re.compile(r"(^|/)(conversation\.jsonl|run-\d+\.log)$")
NOTICE = "Runs locally. Results are sent to Metaphi AI; your code stays here."
LOCAL_NOTICE = "Runs locally."
MAX_BLOB = 2_000_000

SOURCE = frozenset(["go", "py", "java", "kt", "kts", "scala", "groovy", "jelly", "js", "jsx", "mjs", "cjs", "ts", "tsx", "vue", "svelte", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "cs", "fs", "rb", "php", "rs", "dart", "swift", "cbl", "cob", "cpy", "jcl", "bms", "st", "gs", "pl", "pm", "f", "f90", "for", "sh", "bash", "ps1", "m", "mm", "ex", "exs", "erl", "hs", "ml", "clj", "lua", "vb", "pas", "sql", "r", "jl", "zig", "nim", "sol", "v", "sv", "vhd", "vhdl", "tf", "abap", "rpg", "pli"])
DOC = frozenset(["md", "txt", "rst", "adoc", "pdf", "docx", "doc"])
CONFIG = frozenset(["json", "yaml", "yml", "toml", "ini", "xml", "env", "cfg", "conf", "lock", "sum", "properties", "gradle"])
DATA = frozenset(["csv", "tsv", "jsonl", "parquet", "dat", "bin", "png", "jpg", "jpeg", "gif", "svg", "ico", "ttf", "woff", "woff2", "mp4", "zip", "gz", "tar", "jar"])
KNOWN = frozenset(["makefile", "cmakelists.txt", "dockerfile", "docker-compose.yml", "docker-compose.yaml", "go.mod", "go.sum", "package.json", "package-lock.json", "yarn.lock", "pom.xml", "build.gradle", "build.gradle.kts", "cargo.toml", "pyproject.toml", "requirements.txt", "setup.py", "gemfile", "composer.json", "pubspec.yaml", "build.xml", "jenkinsfile", ".gitlab-ci.yml", ".travis.yml", "tox.ini", "pytest.ini", "license", "license.txt", "license.md", "licence", "licence.txt", "licence.md", "copying", "copying.txt", "unlicense", "readme.md", "contributing.md", "changelog.md", "codeowners", ".gitmodules", ".gitattributes"])
TRAILERS = frozenset(["co-authored-by", "signed-off-by", "reviewed-by", "acked-by", "tested-by", "reported-by", "suggested-by", "helped-by", "change-id", "git-svn-id", "fixes", "closes", "refs", "see-also", "cc", "bug", "jira", "generated-by"])
TEST = re.compile(r"(^|/)(tests?|spec|specs|testdata|testutils?|__tests__|fixtures?|[\w.-]*-tests?(-[\w.-]*)?\.package)(/|$)"
                  r"|Tests?\.class(/|\.st$)|Tests?\.(java|php|cs|kt|groovy)$|_test\.|\.test\.|\.spec\.|(^|/)test_|tests?\.[a-z]+$", re.IGNORECASE)
VENDORED = re.compile(r"(^|/)(vendor|node_modules|third_party|external|deps)/", re.IGNORECASE)
GENERATED = re.compile(r"(\.pb\.go|_gen\.go|\.gen\.|\.sql\.go|\.g\.dart|\.freezed\.dart|\.min\.js|/mocks?/|generated)", re.IGNORECASE)
GENERATED_HEAD = re.compile(rb"code generated|do not edit|@generated|auto-?generated", re.IGNORECASE)
BOT = re.compile(r"(\[bot\]|dependabot|renovate|github-actions|bot@|noreply@.*bot)", re.IGNORECASE)
TOOL = re.compile(r"(claude|anthropic|copilot|cursor|devin|codex|aider|windsurf|gemini|jules)", re.IGNORECASE)
TOOL_LINE = re.compile(r"(?i)^(co-authored-by|generated (with|by))")
TICKET = re.compile(r"(?<![\w/])(#\d+|[A-Z][A-Z0-9]{1,9}-\d+)\b")
PR_NUMBER = re.compile(r"(Merge pull request #\d+|\(#\d+\)[ \t]*$)", re.MULTILINE)
PR = re.compile(r"(Merge pull request #\d+|\(#\d+\)[ \t]*$|Merged in |See merge request|Merge branch )", re.MULTILINE)
TRAILER = re.compile(r"^([A-Za-z][A-Za-z-]+): .+$", re.MULTILINE)
FIX = re.compile(r"(?i)\b(fix(es|ed)?|bug|defect|patch|hotfix|regression|crash)\b")
REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,64})")
IDENT = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{2,}")
TEST_FN = re.compile(r"(^[ \t]*func\s+(Test|Benchmark|test)\w*\(|^[ \t]*def\s+test_|@(Test|ParameterizedTest|RepeatedTest|TestFactory)\b"
                     r"|function\s+test\w*\(|#\[(tokio::)?test\]|\[(Test|Fact|Theory|TestMethod|DataTestMethod|TestCase)\b"
                     r"|^test\w*$|^[ \t]*(it|test|describe)\s*\(|^[ \t]*(it|test|specify|scenario)\s+['\"]|^[ \t]*TEST(_[FP])?\()",
                     re.MULTILINE)
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
    **dict.fromkeys(("java", "kt", "scala", "groovy"), _JAVA), **dict.fromkeys(("js", "jsx", "mjs", "cjs", "ts", "tsx"), _JS),
    **dict.fromkeys(("c", "h", "cc", "cpp", "cxx", "hpp", "hh"), _C), **dict.fromkeys(("cbl", "cob", "cpy"), _COBOL),
}
LICENSES = (("Apache-2.0", b"apache license"), ("AGPL-3.0", b"affero general public"), ("LGPL", b"lesser general public"),
            ("GPL", b"gnu general public"), ("MPL-2.0", b"mozilla public license"), ("BSD", b"redistribution and use in source"),
            ("MIT", b"permission is hereby granted, free of charge"),
            ("Unlicense", b"released into the public domain"))
KNOWN_EXT = SOURCE | DOC | CONFIG | DATA
GIT_FLOOR = (2, 32)
_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}
_FIXED = ("core.quotePath=false", "core.fsmonitor=false", "log.showRoot=true", "log.mailmap=true", "log.showSignature=false",
          "log.follow=false", "diff.noprefix=false", "diff.mnemonicPrefix=false", "i18n.logOutputEncoding=UTF-8")


class Unreadable(Exception):
    pass


def _git(repo: Path, *args: str) -> list[str]:
    fixed = [word for setting in _FIXED for word in ("-c", setting)]
    return ["git", *fixed, "-C", str(repo), *args]


def _ask(repo: Path, *args: str) -> str:
    return subprocess.run(_git(repo, *args), capture_output=True, env=_ENV, check=False).stdout.decode("utf-8", "replace").strip()


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


def _commits(repo: Path, authors: _Ids, domains: _Ids, people: list[dict], order: dict[str, int]) -> list[dict]:
    fields = "%H%x00%P%x00%aE%x00%cE%x00%at%x00%ct%x00%ad%x00%B%x00"
    raw = _out(repo, "log", "HEAD", "--topo-order", "--reverse", "--date=format:%z", f"--format={fields}").split(b"\0")

    def person(address: str) -> int:
        key = address.lower()
        if key not in authors:
            people.append({"author": authors.of(key), "domain": domains.of(key.rpartition("@")[2]), "bot": bool(BOT.search(key))})
        return authors[key]
    first = set(_text(repo, "rev-list", "--first-parent", "HEAD").split())
    rows = [[x.decode("utf-8", "replace") for x in raw[i:i + 8]] for i in range(0, len(raw) - 7, 8)]
    for row in rows:
        order[row[0].strip()] = len(order) + 1
    commits = []
    for row in rows:
        sha, parents, author, committer, at, ct, tz, msg = row
        sha = sha.strip()
        names = collections.Counter(k.lower() for k in TRAILER.findall(msg) if k.lower() in TRAILERS)
        back = REVERTS.search(msg)
        commits.append({
            "commit": order[sha], "parents": [order.get(p, 0) for p in parents.split()], "first_parent": sha in first,
            "author": person(author), "committer": person(committer), "authored_at": _time(at), "committed_at": _time(ct),
            "tz": tz if re.fullmatch(r"[+-]\d{4}", tz) else "+0000", "msg_chars": len(msg), "msg_lines": msg.count("\n") + 1,
            "ticket_refs": len(TICKET.findall(PR_NUMBER.sub("", msg))), "pr_merge": bool(PR.search(msg)),
            "fix_word": bool(FIX.search(msg)), "revert": msg.lower().startswith("revert"),
            "reverts": next((order[s] for s in order if back and s.startswith(back.group(1))), None) if back else None,
            "trailers": dict(names), "tool_trailers": sum(bool(TOOL.search(x)) for x in msg.split("\n") if TOOL_LINE.match(x)),
            "files": []})
    return commits


def _changes(repo: Path, by_sha: dict[str, dict], paths: _Ids, order: dict[str, int]) -> None:
    tokens = iter(_out(repo, "log", "HEAD", "-M", "--diff-merges=first-parent", "--raw", "--numstat", "-z",
                       "--format=%x01%H").split(b"\0"))
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
            change = {"file": paths.of(path), "status": status.get(path, "M"), "added": int(m[1]) if m[1] != "-" else -1,
                      "deleted": int(m[2]) if m[2] != "-" else -1, "hunks": 0}
            if old is not None:
                change["old_file"] = paths.of(old)
            row["files"].append(change)


def _unquote(raw: bytes) -> str:
    raw = raw.rstrip(b"\t\n")
    if raw[:1] == b'"':
        raw = re.sub(rb"\\([0-7]{3}|.)", lambda m: bytes([int(m[1], 8)]) if len(m[1]) == 3 else
                     {b"a": b"\a", b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r", b"t": b"\t", b"v": b"\v"}.get(m[1], m[1]), raw[1:-1])
    return raw.decode("utf-8", "surrogateescape")


def _hunks(repo: Path, by_sha: dict[str, dict], paths: _Ids) -> None:
    p = subprocess.Popen(_git(repo, "log", "HEAD", "-M", "--diff-merges=first-parent", "-p", "-U0", "--no-ext-diff",
                              "--no-textconv", "--no-color", "--format=%x01%H"), stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=_ENV)
    counts: dict[str, int] = {}
    spans: dict[str, list[tuple[int, int]]] = {}
    words: dict[str, set[bytes]] = {"test+": set(), "source+": set(), "source-": set()}
    row: dict | None = None

    def close() -> None:
        if row is not None:
            by_id = {paths[path]: n for path, n in counts.items() if path in paths}
            gaps = {paths[path]: [max(0, b[0] - a[1]) for a, b in itertools.pairwise(s)] for path, s in spans.items() if path in paths}
            for change in row["files"]:
                change["hunks"] = by_id.get(change["file"], 0)
                change["gaps"] = gaps.get(change["file"], [])
            row["iface_idents"] = len((words["test+"] & words["source+"]) - words["source-"])
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
    p = subprocess.Popen(_git(repo, "cat-file", "--batch"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=_ENV)
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
    row: dict[str, Any] = {"binary": False, "generated_header": bool(GENERATED_HEAD.search(body[:1024])), "lines": len(lines),
                           "blank": sum(not x.strip() for x in lines), "comment": sum(bool(COMMENT.match(x)) for x in lines if x.strip()),
                           "indent_sum": sum(indents), "max_indent": max(indents or [0]), "max_line": max(map(len, lines))}
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
        row.update(env_refs=len(ENV_REF.findall(text)), url_refs=len(URL_REF.findall(text)), mock_refs=len(MOCK_REF.findall(text)))
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
        q = re.sub(r"[.\\:]+", "/", q) if ext not in ("go", "dart", "js", "jsx", "mjs", "cjs", "ts", "tsx", "c", "h", "cc", "cpp", "cxx", "hpp", "hh") else os.path.splitext(q)[0]
        parts = q.split("/")
        return any("/".join(parts[i:]) in tails for i in range(len(parts)) if len(parts) - i >= (2 if len(parts) > 2 else 1))
    return resolves


def why_not(repo: Path) -> str | None:
    bare = _ask(repo, "rev-parse", "--is-bare-repository") == "true"
    top = _ask(repo, "rev-parse", "--show-toplevel")
    if not (bare and _ask(repo, "rev-parse", "--git-dir") == ".") and not (top and Path(top).resolve() == repo.resolve()):
        return "not_a_git_repository"
    if _ask(repo, "rev-parse", "--is-shallow-repository") == "true":
        return "shallow"
    if _ask(repo, "config", "--get", "extensions.partialclone") or _ask(repo, "config", "--get-regexp", r"remote\..*\.promisor"):
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
        raise errors.MetaphiError(f"kennet profile-codebase needs git {GIT_FLOOR[0]}.{GIT_FLOOR[1]} or newer", code="git_too_old")


def compute(repo: Path, name: str, keep: dict | None = None) -> dict[str, Any]:
    authors, domains, paths, dirs = _Ids(), _Ids(), _Ids(), _Ids()
    salt = secrets.token_bytes(32)
    people: list[dict] = []
    order: dict[str, int] = {}
    commits = _commits(repo, authors, domains, people, order)
    by_sha = {sha: commits[i - 1] for sha, i in order.items()}
    _changes(repo, by_sha, paths, order)
    _hunks(repo, by_sha, paths)
    tree: dict[str, tuple[str, str, int]] = {}
    for entry in _out(repo, "ls-tree", "-r", "-l", "-z", "HEAD").split(b"\0"):
        if b"\t" in entry:
            meta, _, path = entry.partition(b"\t")
            mode, _type, sha, size = meta.split()
            tree[path.decode("utf-8", "surrogateescape")] = (mode.decode(), sha.decode(), int(size) if size.isdigit() else 0)
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
        row = {"file": paths.of(path), "dir": dirs.of(parent) if parent else 0, "depth": path.count("/"), "ext": _ext(path),
               "kind": _kind(path), "known_name": (b if (b := os.path.basename(path).lower()) in KNOWN else None), "at_tip": path in tree}
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
    env, start = codebase_env.needs({p for p, (mode, _s, _z) in tree.items() if mode != "160000"},
                                    lambda p: next((b for _s, b in _blobs(repo, [tree[p][1]])), None) if p in tree else None)
    pack = re.search(r"size-pack: (\d+)", _text(repo, "count-objects", "-v"))
    if keep is not None:
        keep.update(path=repo, tip=tip, commits=commits, order=order, start=start,
                    paths={row["file"]: path for path, row in rows.items() if row["at_tip"]})
    return {"name": name, "tip": tip, "roots": _text(repo, "rev-list", "--max-parents=0", "HEAD").split(),
            "git": {"branches": len(_text(repo, "for-each-ref", "--format=x", "refs/heads", "refs/remotes").split()),
                    "tags": len(_text(repo, "for-each-ref", "--format=x", "refs/tags").split()),
                    "submodules": sum(m == "160000" for m, _s, _z in tree.values()), "lfs": lfs,
                    "pack_bytes": int(pack[1]) * 1024 if pack else 0},
            "licenses": sorted(licenses), "env": env, "authors": people,
            "dirs": [{"dir": d, "parent": up} for d, up in tree_dirs.items()], "commits": commits,
            "files": sorted(rows.values(), key=lambda r: r["file"])}


SKIPPED_FIX = {"shallow": "a shallow clone has only part of the history: run `git fetch --unshallow` in it, or give its URL",
       "partial_clone": "a partial clone lacks file contents: clone it in full, or give its URL",
       "not_a_git_repository": "give the repository's top folder (where .git is), or its URL",
       "no_commits": "it has no commits yet",
       "clone_failed": "Check you can `git clone` that URL in this terminal",
       "unreadable": "git could not read it"}


_URL = re.compile(r"^(?:(?:https?|ssh|git|file)://\S+|[\w.-]+@[\w.-]+:\S+)$")


def is_url(given: str) -> bool:
    return bool(_URL.match(given)) and not Path(given).expanduser().exists()


def _url_name(url: str) -> str:
    tail = re.split(r"[/:]", url.rstrip("/"))[-1]
    return tail.removesuffix(".git")


def _clone(url: str, into: Path) -> str | None:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    got = subprocess.run(["git", "-c", "protocol.ext.allow=never", "clone", "--bare", "--quiet", "--", url, str(into)],
                         capture_output=True, env=env, check=False)
    if got.returncode == 0:
        return None
    lines = [ln.strip() for ln in got.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    said = next((ln for ln in lines if ln.lower().startswith(("error:", "fatal:", "remote:"))), lines[0] if lines else "")
    return re.sub(r"(?i)^(error|fatal|remote):\s*", "", said).rstrip(".") or "git could not clone it"


def repositories(paths: list[str]) -> list[str]:
    out: list[str] = []
    for given in paths:
        path = Path(given).expanduser()
        if is_url(given) or not path.is_dir() or why_not(path) != "not_a_git_repository":
            out.append(given)
            continue
        found = sorted(str(p.parent) for p in path.glob("*/.git")) or sorted(str(p.parent) for p in path.glob("*/*/.git"))
        out += found or [given]
    return out


@contextlib.contextmanager
def profiled(paths: list[str], say: Callable[[str], None] = print
             ) -> Iterator[tuple[dict[str, Any], list[dict]]]:
    _git_ready()
    repos, skipped, used, kept = [], [], set(), []
    with tempfile.TemporaryDirectory(prefix="kennet-clone-") as scratch:
        for n, given in enumerate(paths, 1):
            url = is_url(given)
            path = None if url else Path(given).expanduser().resolve()
            base = _url_name(given) if url else path.name
            name = (re.sub(r"[^\w .@+-]", "-", base, flags=re.ASCII).strip() or f"repo-{n}")[:100]
            while name in used:
                name = f"{name[:120]}-{n}"
            used.add(name)
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


def document(paths: list[str], say: Callable[[str], None] = print) -> dict[str, Any]:
    with profiled(paths, say) as (doc, _kept):
        return doc


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


def run_id(doc: dict[str, Any]) -> str:
    """This run's folder under ./kennet-profile: the repositories as measured, the same for the same commits, so a run
    again finds what it kept."""
    said = json.dumps([[r["name"], r["tip"]] for r in doc["repos"]]).encode()
    return "run_" + hashlib.sha256(said).hexdigest()[:16]


READY_NOTE = "Fix what you can and run the same command again."
WHY_FIX = {
    "service": "The tests could not reach a service (a database, a queue). Start it, or install Docker.",
    "config": "The tests need configuration (an environment variable or config file). Set it and run again.",
    "timeout": "The tests ran too long. Run again with a faster suite: --test '<command>'.",
}


def _tip_test(got: dict) -> dict:
    """The tip's own test run, else the found try that counted the most tests."""
    tests = (got.get("tip") or {}).get("tests") or []
    if tests:
        return tests[0]
    tried = [t["test"] for x in got.get("setup") or [] for t in x.get("tries") or [] if t.get("test")]
    return max(tried, key=lambda t: t.get("reported") or 0, default={})


def readiness(doc: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """The five checks the seller is shown before anything is sent: per repository, each yes, no, not collected or
    not needed, and for a no, what would turn it to yes. Facts this run saw on this machine, never how Metaphi AI
    weighs them. Only what the seller can change by running again is asked: never the history, rights or licences."""
    out = []
    for repo, got in zip(doc["repos"], report["repos"], strict=True):
        got, env = got or {}, repo.get("env")
        tip = got.get("tip") or {}
        builds = [tip["build"]] if tip.get("build") else [t["build"] for x in got.get("setup") or []
                                                           for t in x.get("tries") or [] if t.get("build")]
        test = _tip_test(got)
        # counted but none passed, for want of a service or configuration, or crashing: the tests never got to run
        stopped = not test.get("passed") and test.get("why") in ("service", "config", "crash")
        ran = (bool(test.get("reported")) or (test.get("exit") == 0 and bool(test))) and not stopped
        whole = ran and test.get("exit") == 0 and test.get("reported") == test.get("passed")
        rows: list[dict] = []

        def check(key: str, label: str, ok: bool | None, fix: str, state: str | None = None, rows: list = rows) -> None:
            state = state or ("not_collected" if ok is None else "yes" if ok else "no")
            rows.append({"key": key, "label": label, "state": state, **({"fix": fix} if state == "no" else {})})
        check("ci", "CI config", None if env is None else env["ci_files"] > 0,
              "No CI config found. Add the one your team runs its tests with.")
        needed = env and env["ci_services"] + env["compose_services"] - env["compose_built"]
        check("services", "Services start",
              None if env is None else (got.get("services") or {}).get("up", False),
              "The tests need services (databases, queues). Install Docker and run again." if not got.get("services")
              else "The services did not start; see services.log.",
              None if env is None or needed else "not_needed")
        check("build", "Builds", None if not got else any(b.get("exit") == 0 for b in builds) or (ran and not builds),
              "The build failed. Run again with yours: --build '<command>' --test '<command>'.")
        check("tests_run", "Tests run", None if not got else ran,
              WHY_FIX.get(test.get("why") or "", "No tests ran. Run again with yours: --test '<command>'."))
        failed = (test.get("reported") or 0) - (test.get("passed") or 0)
        check("tests_pass", "Tests pass", None if not got else whole,
              " ".join(x for x in (f"{failed} of {test['reported']} tests fail."
                                   if test.get("reported") else "The tests did not pass.",
                                   WHY_FIX.get(test.get("why") or "")) if x))
        out.append({"name": repo["name"], "checks": rows})
    return {"repos": out, "note": READY_NOTE}


def checked(kept: list[dict], picked: dict[str, Any] | None, plan: codebase_checks.Plan,
            say: Callable[[str], None] = print, ref: str | None = None, steps: Steps | None = None) -> dict[str, Any]:
    import dataclasses

    from metaphi import codebase_harwell, codebase_recipes, codebase_setup
    out, images, box, models = [], [], None, []
    steps = steps or Steps()
    if plan.test is None:
        box = codebase_setup.engine()
        if not box:
            say("No Docker or Podman: running on this machine")
    # services run in containers even where the seller's own commands run here: their ports are published here
    engine = box or (codebase_setup.engine() if any(k.get("start") for k in kept) else None)

    def model() -> codebase_setup.Model | None:
        if not plan.deep or not ref:
            return None
        if not models or time.time() > models[-1][1] - 3600:
            with steps.step("token"):
                reach = setup_reach(ref)
            models.append((codebase_setup.Model(reach), reach.get("expires_at") or time.time() + 8 * 3600))
        return models[-1][0]
    picked = picked or {"repos": [[] for _ in kept], "runs": 0}
    first = picked.get("first")
    with tempfile.TemporaryDirectory(prefix="kennet-cache-", ignore_cleanup_errors=True) as caches, \
            contextlib.ExitStack() as done:
        done.callback(codebase_setup.forget, box, images)
        for n, (local, picks) in enumerate(zip(kept, picked["repos"], strict=True)):
            picks = picks[:first] if first is not None else picks
            repo, logs = local["path"], (plan.logs or Path.cwd() / "kennet-profile") / local["name"]
            mine = dataclasses.replace(plan, cache=Path(caches) / str(n) if box else None,
                                       steps=Steps(logs / "steps.jsonl"))
            mine.steps.total["profile"] = local.get("seconds", 0.0)
            if mine.cache:
                mine.cache.mkdir()
            found, setup, tip = None, [] if plan.test is None else None, {}

            def setup_at(sha: str, where: Path, era: int | None, later: tuple[codebase_setup.Recipe, str] | None,
                         repo=repo, mine=mine, setup=setup, tip=tip) -> codebase_setup.Recipe | None:
                tree = codebase_checks._tree(repo, sha)

                def write(d: Path) -> None:
                    codebase_checks._write(repo, tree, d)
                with mine.steps.step("setup", era=era):
                    recipe, how = codebase_recipes.find(write, _reader(repo, tree), set(tree), "", box, where, say,
                                                        plan.run_minutes, mine.cache, mine.steps,
                                                        skip=later[0] if later else None,
                                                        tip=tip if era is None else None)
                    setup.append({"era": era, **how})
                    if recipe is None and (m := model()) is not None:
                        recipe, how = codebase_setup.find(write, "", m, box, where / "model", say, plan.run_minutes,
                                                          mine.cache, mine.steps, later=later)
                        setup.append({"era": era, **how})
                if recipe and recipe.image:
                    images.append(recipe.image)
                return recipe
            # the services the repository declares (CI's, else compose's), up for its checks where containers run
            services = None
            if engine and local.get("start"):
                tip_tree = codebase_checks._tree(repo, local["tip"])
                services = codebase_env.Services(engine, local["start"],
                                                 lambda d, t=tip_tree, r=repo: codebase_checks._write(r, t, d),
                                                 logs / "services.log")
                say(f"Starting {local['name']}'s services\u2026")
                with mine.steps.step("services"):
                    up = services.up()
                if not up["up"]:
                    say(f"\u2717 {local['name']}'s services did not start ({logs / 'services.log'})")
                codebase_setup.NETWORK[:] = ["--network", "host"] if up["up"] else []
            # a mainframe estate (harwell.toml) is built on Harwell, never here: `--deep` alone sends its files
            built = codebase_harwell.checked(repo, local["tip"], local["name"], _key(None, False), say,
                                             mine.steps) if plan.deep and ref else None
            try:
                if plan.test is None:
                    say(f"Setting up {local['name']}\u2026")
                    recipe = setup_at(local["tip"], logs / "setup", None, None)
                    if not recipe:
                        say(f"\u2717 Could not set up {local['name']} ({logs / 'setup'})")
                    if recipe:
                        found = (recipe, box)
                era_of = (picked.get("eras") or [[]] * len(kept))[n][:len(picks)] or None

                def repair(era: int, sha: str, tail: str, logs=logs, found=found, setup_at=setup_at):
                    recipe = setup_at(sha, logs / f"setup-era{era}", era, (found[0], tail))
                    return (recipe, box) if recipe else None
                got = codebase_checks.run(repo, local["name"], local["tip"], local["commits"], local["order"], picks,
                                          picked["runs"], mine, say, found, era_of,
                                          repair if found and setup is not None else None,
                                          (picked.get("breaks") or [[]] * len(kept))[n], local.get("paths"),
                                          tip or None)
            except Exception as e:   # noqa: BLE001
                say(f"\u2717 {local['name']}: tests stopped ({type(e).__name__}: {e})")
                got = {"from": "tip", "runs": picked["runs"], "build_given": plan.build is not None,
                       "test_given": plan.test is not None, "stopped": "error", "commits": [], "breaks": []}
            finally:
                if services:
                    services.down()
                    codebase_setup.NETWORK.clear()
            if setup:
                got["setup"] = setup
            if services:
                got["services"] = services.got
            if built:
                got["build_remote"] = built
            got["seconds"] = mine.steps.seconds()
            out.append(got)
            if box and mine.cache:
                try:
                    subprocess.run([box, "run", "--rm", "-v", f"{mine.cache}:/c", "--entrypoint", "chmod",
                                    found[0].image if found and found[0].image else CHMOD_IMAGE, "-R", "a+rwX", "/c"],
                                   capture_output=True, timeout=600, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
    return {"schema": CHECKS_SCHEMA, "tool_version": __version__, "repos": out, "seconds": steps.seconds()}


def packed(doc: dict[str, Any]) -> bytes:
    return gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0)


# Every call here is idempotent (the reference is the profile's hash; checks and logs sent again are taken), so each
# is sent again through a slow or busy moment, not just a dropped packet. The server answers within a minute
# (`unavailable`, 503, when the volume is slow); Modal ends a request at 150 s; so a call waits 90 s for its answer,
# a body as long as it takes to send, and the whole call at most SEND_BUDGET_S.
SEND = httpx.Timeout(30.0, connect=10.0, read=90.0, write=600.0)
SEND_RETRIES = 4
SEND_BUDGET_S = 300.0


def _named(e: errors.MetaphiError) -> errors.MetaphiError:
    if e.code == "insufficient_scope":
        e.message += ("; it takes kennet:codebase_profile:write. `kennet login` hands this machine keys that hold it "
                      "once your organization does")
        e.next_step = "kennet login"
    return e


# where a call says it is sending again: the command's progress line (stderr under --json); set by the command
say: Callable[[str], None] | None = None


def _call(method: str, path: str, sleep: Callable[[float], None], answered: bool = True, **kw: Any) -> dict[str, Any]:
    """One call of the profile API on the saved key, its JSON answer (`answered`; else none is read); an answer that
    is not JSON (a proxy's page, a cut-off request) is sent again, then said as `unavailable`, never a crash."""
    with _client(_key(None, False)) as h:
        tries = 0
        while True:
            try:
                r = _send(h, method, path, sleep, retries=SEND_RETRIES, budget_s=SEND_BUDGET_S,
                          timeout=SEND, say=say, **kw)
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
                raise errors.APIError(f"the service answered HTTP {r.status_code} with no result; run the same "
                                      "command again", code="unavailable", retryable=True,
                                      request_id=r.headers.get("request-id"))
            sleep(2.0)


def post(body: bytes, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _call("POST", "/codebase-profiles", sleep, content=body,
                 headers={"content-encoding": "gzip", "content-type": "application/json"})


SCOPE = "kennet:codebase_profile:write"


def key_ready(sleep: Callable[[float], None] = time.sleep) -> None:
    """Whether the key may send a profile, before any local work: from the scopes the sign-in saved with it, with
    no call. A key from elsewhere (METAPHI_API_KEY, `kennet login --with-api-key`) has no scopes saved here, so the
    server is asked once."""
    key, cfg = _key(None, False), config.load()
    if key == cfg.get("api_key") and cfg.get("api_key_from") == "login" and isinstance(cfg.get("scopes"), list):
        if SCOPE in cfg["scopes"]:
            return
        raise _named(errors.AuthError("this API key does not hold access to codebase profiles",
                                      code="insufficient_scope"))
    _call("GET", "/codebase-profiles/access", sleep, answered=False)


def sample(ref: str, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _call("POST", f"/codebase-profiles/{ref}/sample", sleep)


def setup_reach(ref: str, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _call("POST", f"/codebase-profiles/{ref}/setup", sleep)


def post_logs(ref: str, body: bytes, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _call("POST", f"/codebase-profiles/{ref}/logs", sleep, content=body,
                 headers={"content-type": "application/gzip"})


def post_checks(ref: str, body: bytes, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _call("POST", f"/codebase-profiles/{ref}/checks", sleep, content=body,
                 headers={"content-encoding": "gzip", "content-type": "application/json"})


def logs_packed(root: Path, ref: str, names: list[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
        add("kennet-logs.json", json.dumps({"schema": LOGS_SCHEMA, "profile": ref, "tool_version": __version__}).encode())
        for n, repo in enumerate(names):
            for f in sorted((root / repo).rglob("*")) if (root / repo).is_dir() else []:
                name = f"{n}/{f.relative_to(root / repo).as_posix()}"
                if f.is_symlink() or not f.is_file() or NOT_PACKED.search(name):
                    continue
                try:
                    data = f.read_bytes()
                except OSError:
                    continue
                if len(data) > LOG_FILE_MOST:
                    data = data[:512 << 10] + b"\n[... cut by kennet profile-codebase ...]\n" + data[-(LOG_FILE_MOST - (512 << 10)):]
                add(name, data)
    return buf.getvalue()


__all__ = ["CHECKS_SCHEMA", "LOGS_SCHEMA", "NOTICE", "SCHEMA", "checked", "compute", "document", "key_ready",
           "logs_packed", "packed", "post", "post_checks", "post_logs", "profiled", "sample", "setup_reach", "why_not"]
