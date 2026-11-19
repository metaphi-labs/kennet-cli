"""What CodeVal runs besides the picked commits, drawn here from the profile and each repository's seed from `/picks`,
and the facts of each commit `/picks` picks from.

The service draws the same from the profile it receives and the seed it keeps, and refuses checks that differ, so
these functions must stay identical to its own. A shared test vector (tests/test_codeval_draws.py) holds them so.

- `plant`: where history holds few commits that can become a task, operators at the tip to swap one at a time and
  run the tip's tests over (`edits`): how much of the code the tests pin down.
- `reveal`: tip source files whose raw git blob ids the checks carry (`blobs`): whether the code is already public.
"""

from __future__ import annotations

import collections
import random

SAMPLE = 30
MAX_SOURCE_FILES = 15
WIDE = (3, 20)
SNAPSHOT_BELOW = 10
OPS = ("cmp", "logic", "arith", "bool")
REVEAL = 64
REVEAL_BYTES = 1024
BUILD_FILES = frozenset(
    [
        *("makefile", "cmakelists.txt", "go.mod", "package.json", "pom.xml", "build.gradle", "build.gradle.kts"),
        *("cargo.toml", "pyproject.toml", "setup.py", "requirements.txt", "gemfile", "composer.json", "pubspec.yaml"),
        *("build.xml", "dockerfile"),
    ]
)
NOT_CODE = frozenset(
    [
        *("md", "txt", "rst", "adoc", "pdf", "docx", "doc", "json", "yaml", "yml", "toml", "ini", "xml", "env"),
        *("cfg", "conf", "lock", "sum", "properties", "gradle", "csv", "tsv", "jsonl", "parquet", "dat", "bin"),
        *("png", "jpg", "jpeg", "gif", "svg", "ico", "ttf", "woff", "woff2", "mp4", "zip", "gz", "tar", "jar", ""),
    ]
)


def draw(seed: str, what: bytes) -> random.Random:
    """The draw of `what` (b"plant" or b"reveal") from one repository's seed."""
    return random.Random(bytes.fromhex(seed) + what)


def candidates(repo: dict) -> tuple[list[int], list[int]]:
    """Commits that can become a task: on the first-parent line, 1 to 15 source files and a test in code changed;
    (wide, narrow) by `WIDE`."""
    files = {f["file"]: f for f in repo["files"]}
    wide, narrow = [], []
    for c in repo["commits"]:
        if not c["first_parent"] or not c["parents"] or not c["parents"][0]:
            continue
        src = [x for x in c["files"] if files[x["file"]]["kind"] == "source"]
        test = any(files[x["file"]]["kind"] == "test" and files[x["file"]]["ext"] not in NOT_CODE for x in c["files"])
        if not test or not 1 <= len(src) <= MAX_SOURCE_FILES:
            continue
        lines = sum(max(x["added"], 0) + max(x["deleted"], 0) for x in src)
        (wide if len(src) >= WIDE[0] and lines > WIDE[1] else narrow).append(c["commit"])
    return wide, narrow


def eras(repo: dict) -> dict[int, int]:
    """Each first-parent commit's build era, from the root build files its history added and removed."""
    files = {f["file"]: f for f in repo["files"]}
    root = lambda i: files[i]["depth"] == 0 and files[i]["known_name"] in BUILD_FILES
    have: set[str] = set()
    seen: list[frozenset] = []
    of = {}
    for c in repo["commits"]:
        if not c["first_parent"]:
            continue
        for x in c["files"]:
            if x.get("old_file") and root(x["old_file"]):
                have.discard(files[x["old_file"]]["known_name"])
            if root(x["file"]):
                (have.discard if x["status"] == "D" else have.add)(files[x["file"]]["known_name"])
        if not seen or seen[-1] != frozenset(have):
            seen.append(frozenset(have))
        of[c["commit"]] = len(seen)
    return of


def facts(repo: dict) -> dict[int, dict]:
    """What `/picks` needs of each first-parent commit to pick as `candidates` and `eras` do: numbers and booleans,
    never names. The service recomputes them from the profile it is sent and refuses a difference."""
    files, era = {f["file"]: f for f in repo["files"]}, eras(repo)
    out = {}
    for c in repo["commits"]:
        if not c["first_parent"]:
            continue
        src = [x for x in c["files"] if files[x["file"]]["kind"] == "source"]
        out[c["commit"]] = {
            "has_parent": bool(c["parents"] and c["parents"][0]),
            "source_files": len(src),
            "source_lines": sum(max(x["added"], 0) + max(x["deleted"], 0) for x in src),
            "test_code": any(
                files[x["file"]]["kind"] == "test" and files[x["file"]]["ext"] not in NOT_CODE for x in c["files"]
            ),
            "era": era[c["commit"]],
        }
    return out


def _spread(ids: list[int], n: int, era_of: dict[int, int], rng: random.Random) -> list[int]:
    by: dict[int | None, list[int]] = collections.defaultdict(list)
    for i in ids:
        by[era_of.get(i)].append(i)
    share = {e: n * len(v) / len(ids) for e, v in by.items()} if ids else {}
    quota = {e: int(s) for e, s in share.items()}
    for e in sorted(share, key=lambda e: share[e] - quota[e], reverse=True)[: n - sum(quota.values())]:
        quota[e] += 1
    return [i for e, v in by.items() for i in rng.sample(v, quota[e])]


def sites(repo: dict) -> list[tuple[int, str, int]]:
    """Every operator counted in the tip's source, as (file, class, nth within the file and class)."""
    return [
        (f["file"], op, k)
        for f in repo["files"]
        if f["at_tip"] and f["kind"] == "source" and f.get("ops")
        for op in OPS
        for k in range(f["ops"].get(op, 0))
    ]


def plant(repo: dict, rng: random.Random, n: int = SAMPLE) -> list[dict]:
    """Operators to swap, spread over files in proportion to the operators each holds, in a random order; empty where
    history holds `SNAPSHOT_BELOW` candidates or more."""
    wide, narrow = candidates(repo)
    if len(wide) + len(narrow) >= SNAPSHOT_BELOW:
        return []
    every = sites(repo)
    got = _spread(list(range(len(every))), min(n, len(every)), {i: s[0] for i, s in enumerate(every)}, rng)
    rng.shuffle(got)
    return [{"file": every[i][0], "op": every[i][1], "nth": every[i][2]} for i in got]


def reveal(repo: dict, rng: random.Random) -> list[int]:
    """Up to `REVEAL` tip source files of `REVEAL_BYTES` or more, drawn at random; in id order."""
    ids = sorted(
        f["file"]
        for f in repo["files"]
        if f["at_tip"] and f["kind"] == "source" and not f.get("binary") and (f.get("bytes") or 0) >= REVEAL_BYTES
    )
    return sorted(rng.sample(ids, min(REVEAL, len(ids))))


def drawn(doc: dict, seeds: list[str]) -> tuple[list[list[dict]], list[list[int]]]:
    """Each repository's edits and revealed files, in profile order, from its own seed."""
    repos = doc["repos"]
    return (
        [plant(r, draw(seed, b"plant")) for r, seed in zip(repos, seeds, strict=True)],
        [reveal(r, draw(seed, b"reveal")) for r, seed in zip(repos, seeds, strict=True)],
    )


__all__ = ["draw", "drawn", "eras", "facts", "plant", "reveal"]
