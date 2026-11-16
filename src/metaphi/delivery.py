from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from metaphi import api, errors

LOCK, SUMS, TASKS = "delivery.lock.json", "SHA256SUMS", "tasks"
AT_ONCE = 8
ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
TREE = re.compile(r"sha256:[0-9a-f]{64}")
KINDS = (1, 2)
GRADING = "tests/"


def _need(message: str, code: str, next_step: str | None = None, retryable: bool = False) -> errors.MetaphiError:
    return errors.InvalidRequestError(message, code=code, next_step=next_step, retryable=retryable)


def _named(e: errors.MetaphiError) -> errors.MetaphiError:
    if e.code == "resource_missing":
        e.next_step = "kennet delivery list"
    elif e.code == "insufficient_scope":
        e.message += "; `kennet login` hands this machine keys that do"
        e.next_step = "kennet login"
    return e


def _get(path: str, sleep: Callable[[float], None] = time.sleep) -> httpx.Response:
    with api.client(api.key(None, False)) as h:
        try:
            return api.send(h, "GET", path, sleep)
        except errors.MetaphiError as e:
            raise _named(e) from None


def held(sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    return _get("/deliveries", sleep).json()["data"]


def latest(name: str, sleep: Callable[[float], None] = time.sleep) -> str:
    for d in held(sleep):
        if d["name"] == name and not d.get("newer"):
            return d["id"]
    raise _need(f"your organization holds no delivery named {name!r}", "resource_missing", "kennet delivery list")


def show(delivery: str, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _get(f"/deliveries/{delivery}", sleep).json()


CLIENTS = ("docker", "podman", "nerdctl")


def registry_token(sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    with api.client(api.key(None, False)) as h:
        try:
            return api.send(h, "POST", "/registry/token", sleep).json()
        except errors.MetaphiError as e:
            raise _named(e) from None


def registry_login(client: str | None = None, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    found = shutil.which(client) if client else next((w for w in map(shutil.which, CLIENTS) if w), None)
    if not found:
        raise _need(
            f"no {client or ' / '.join(CLIENTS)} on this machine to sign in; `kennet registry token` "
            "prints the token for whatever pulls your images",
            "no_container_client",
            "kennet registry token",
        )
    got = registry_token(sleep)
    try:
        ran = subprocess.run(
            [found, "login", got["server"], "--username", got["username"], "--password-stdin"],
            input=got["token"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise _need(
            f"{Path(found).name} login did not finish ({type(e).__name__})",
            "registry_login_failed",
            "kennet registry token",
        ) from None
    if ran.returncode:
        said = (ran.stderr or ran.stdout).strip().splitlines()[-1:] or ["no reason given"]
        raise _need(
            f"{Path(found).name} login to {got['server']} failed: {said[0]}",
            "registry_login_failed",
            "kennet registry token",
        )
    return {
        "server": got["server"],
        "repository": got["repository"],
        "client": Path(found).name,
        "expires_at": got["expires_at"],
        "signed_in": True,
    }


def files(tree: Path) -> list[tuple[str, str, str]]:
    out = []
    for home, dirs, names in os.walk(tree):
        for name in names + [d for d in dirs if (Path(home) / d).is_symlink()]:
            path = Path(home) / name
            if path.is_symlink():
                body, mode = os.readlink(path).encode(), "120000"
            else:
                body = path.read_bytes()
                mode = "100755" if path.stat().st_mode & 0o100 else "100644"
            out.append((path.relative_to(tree).as_posix(), hashlib.sha256(body).hexdigest(), mode))
    return sorted(out)


def sums(tree: Path) -> list[str]:
    return [f"{digest}  {rel}" for rel, digest, _ in files(tree)]


def tree_hash(tree: Path, kind: int = 2) -> str:
    lines = [f"{d}  {m}  {p}" for p, d, m in files(tree)] if kind >= 2 else sums(tree)
    return "sha256:" + hashlib.sha256("".join(line + "\n" for line in lines).encode()).hexdigest()


def _read_lock(raw: bytes) -> dict[str, Any] | None:
    try:
        lock = json.loads(raw)
    except ValueError:
        return None
    tasks = lock.get("tasks") if isinstance(lock, dict) else None
    if not isinstance(tasks, list) or not ID.fullmatch(str(lock.get("delivery", ""))):
        return None
    for t in tasks:
        if not (
            isinstance(t, dict)
            and isinstance(t.get("name"), str)
            and TASK.fullmatch(t["name"])
            and isinstance(t.get("tree"), str)
            and TREE.fullmatch(t["tree"])
            and isinstance(t.get("images", []), list)
        ):
            return None
    return lock if len({t["name"] for t in tasks}) == len(tasks) else None


def _kind(lock: dict[str, Any]) -> int:
    kind = lock.get("lock", 1)
    if kind not in KINDS:
        raise _need(
            f"{lock.get('delivery')} has a lock of kind {kind!r}, newer than this kennet reads; upgrade "
            "kennet (pip install -U kennet, or brew upgrade kennet)",
            "lock_too_new",
        )
    return kind


def _holds(bundle: Path, tree: str, kind: int) -> bool:
    try:
        return bundle.is_dir() and not bundle.is_symlink() and tree_hash(bundle, kind) == tree
    except OSError:
        return False


def _inside(at: Path, root: str) -> Path:
    parent = os.path.realpath(at.parent)
    if parent != root and not parent.startswith(root + os.sep):
        raise ValueError(f"{at.name!r} would be written outside the task")
    return at


def _unpack(tar_path: Path, into: Path) -> None:
    with tarfile.open(tar_path, "r:") as tar:
        members = tar.getmembers()
        for m in members:
            name = PurePosixPath(m.name)
            if name.is_absolute() or ".." in name.parts or not (m.isreg() or m.isdir() or m.issym()):
                raise ValueError(f"{m.name!r} is not a file of a task")
            if m.issym():
                to = PurePosixPath(os.path.normpath(str(name.parent / m.linkname)))
                if PurePosixPath(m.linkname).is_absolute() or ".." in to.parts:
                    raise ValueError(f"{m.name!r} links outside the task")
        into.mkdir(parents=True)
        root = os.path.realpath(into)
        for m in members:
            at = into / m.name
            for part in reversed([at, *at.parents][: len(PurePosixPath(m.name).parts)][1:]):
                if not part.is_dir():
                    _inside(part, root).mkdir()
            if m.isdir():
                if not at.is_dir():
                    _inside(at, root).mkdir()
            elif m.issym():
                os.symlink(m.linkname, _inside(at, root))
            else:
                if at.is_symlink():
                    raise ValueError(f"{m.name!r} is both a link and a file")
                with tar.extractfile(m) as src, open(_inside(at, root), "wb") as dst:  # type: ignore[union-attr]
                    shutil.copyfileobj(src, dst)
                at.chmod(stat.S_IMODE(m.mode) & 0o755 | 0o600)
        for m in members:
            if m.isdir() and not (into / m.name).is_symlink():
                _inside(into / m.name, root)
                (into / m.name).chmod(stat.S_IMODE(m.mode) & 0o755 | 0o700)


def _fetch(
    h: httpx.Client, delivery: str, task: dict[str, Any], home: Path, kind: int, sleep: Callable[[float], None]
) -> int:
    name, mark = task["name"], uuid.uuid4().hex[:8]
    tar_path, fresh = home / f".{name}.{mark}.tar", home / f".{name}.{mark}.part"
    tries = api.Tries(sleep)
    try:
        while True:
            try:
                with h.stream("GET", f"/deliveries/{delivery}/tasks/{name}/content", timeout=api.UPLOAD) as r:
                    if r.status_code >= 400:
                        r.read()
                        failed = api.error(r)
                        failed.retry_after_s = api.retry_after(r)
                        if not failed.retryable or not tries.again(failed.retry_after_s):
                            raise _named(failed)
                        continue
                    with open(tar_path, "wb") as f:
                        f.writelines(r.iter_bytes())
                break
            except httpx.TransportError as e:
                if not tries.again():
                    raise errors.APIError(
                        f"the service did not answer ({type(e).__name__})",
                        code="unreachable",
                        retryable=True,
                        next_step=f"kennet delivery pull {delivery}",
                    ) from None
        size = tar_path.stat().st_size
        try:
            _unpack(tar_path, fresh)
            good = tree_hash(fresh, kind) == task["tree"]
        except (OSError, ValueError, tarfile.TarError):
            good = False
        if not good:
            raise errors.APIError(
                f"{name}: what was downloaded is not the tree the lock names; nothing was put in place",
                code="tree_mismatch",
                retryable=True,
                next_step=f"kennet delivery pull {delivery}",
            )
        bundle, old = home / name, home / f".{name}.{mark}.old"
        if bundle.exists() or bundle.is_symlink():
            bundle.rename(old)
        try:
            fresh.rename(bundle)
        except OSError:
            if old.exists():
                old.rename(bundle)
            raise
        return size
    finally:
        tar_path.unlink(missing_ok=True)
        for left in (fresh, home / f".{name}.{mark}.old"):
            if left.exists():
                shutil.rmtree(left, ignore_errors=True)


def default_dir(delivery: str) -> Path:
    return Path(delivery.replace("@", "-"))


def pull(
    delivery: str,
    to: Path | None = None,
    dry_run: bool = False,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    pinned = ID.fullmatch(delivery)
    if not pinned and not NAME.fullmatch(delivery):
        raise _need(
            f"{delivery!r} is not a delivery: one is <name> or <name>@<version>, as `kennet delivery list` says them",
            "invalid_delivery",
            "kennet delivery list",
        )
    if not pinned:
        delivery = latest(delivery, sleep)
    to = to or default_dir(delivery)
    with api.client(api.key(None, False)) as h:
        try:
            lock_raw = api.send(h, "GET", f"/deliveries/{delivery}/lock", sleep).content
            sums_raw = api.send(h, "GET", f"/deliveries/{delivery}/sums", sleep).content
        except errors.MetaphiError as e:
            raise _named(e) from None
        lock = _read_lock(lock_raw)
        if lock is None or lock["delivery"] != delivery:
            raise errors.APIError(
                f"the lock the service sent for {delivery} is not one this reads; nothing was written",
                code="lock_invalid",
            )
        try:
            newer = api.send(h, "GET", f"/deliveries/{delivery}", sleep).json().get("newer") if pinned else None
        except errors.MetaphiError as e:
            raise _named(e) from None
        there = to / LOCK
        if there.is_file() and there.read_bytes() != lock_raw:
            other = (_read_lock(there.read_bytes()) or {}).get("delivery") or "another lock"
            raise _need(
                f"{to} holds {other}, not {delivery}; a delivery has its own directory",
                "directory_in_use",
                f"kennet delivery pull {delivery} --to {default_dir(delivery)}",
            )
        home, kind = to / TASKS, _kind(lock)
        if home.is_symlink():
            raise _need(
                f"{home} is a link; a delivery's tasks are a directory of its own",
                "directory_in_use",
                f"kennet delivery pull {delivery} --to {default_dir(delivery)}",
            )
        want = [t for t in lock["tasks"] if not _holds(home / t["name"], t["tree"], kind)]
        result: dict[str, Any] = {
            "id": delivery,
            "newer": newer,
            "dir": str(to),
            "lock": str(there),
            "dry_run": dry_run,
            "tasks": len(lock["tasks"]),
            "unchanged": len(lock["tasks"]) - len(want),
            "fetched": [t["name"] for t in want],
            "bytes": 0,
        }
        if dry_run:
            return result
        try:
            home.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise _need(
                f"{to} cannot hold a delivery ({e.strerror or e}); name a directory this can write",
                "directory_in_use",
                f"kennet delivery pull {delivery} --to {default_dir(delivery)}",
            ) from None
        done = [0]

        def one(task: dict[str, Any]) -> int:
            size = _fetch(h, delivery, task, home, kind, sleep)
            done[0] += 1
            say(f"  {done[0]}/{len(want)}  {task['name']}")
            return size

        if want:
            say(f"Pulling {delivery}: {len(want)} of {len(lock['tasks'])} tasks to fetch")
            with ThreadPoolExecutor(AT_ONCE) as pool:
                result["bytes"] = sum(pool.map(one, want))
        (to / SUMS).write_bytes(sums_raw)
        there.write_bytes(lock_raw)
    return result


def verify(to: Path) -> dict[str, Any]:
    try:
        lock = _read_lock((to / LOCK).read_bytes())
    except OSError:
        lock = None
    if lock is None or (to / TASKS).is_symlink():
        raise _need(
            f"{to} holds no {LOCK} this reads, or its {TASKS}/ is a link: it is not a pulled delivery",
            "not_a_delivery",
            "kennet delivery list",
        )
    home, delivery, kind = to / TASKS, lock["delivery"], _kind(lock)
    want: dict[str, str] = {}
    try:
        listed = (to / SUMS).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        listed = None
    for line in listed or []:
        digest, _, rel = line.partition("  ")
        want[rel] = digest
    tasks, differ = [], []
    for t in lock["tasks"]:
        bundle = home / t["name"]
        if not bundle.is_dir() or bundle.is_symlink():
            state, differing = "missing", []
        else:
            have = dict(reversed(line.split("  ", 1)) for line in sums(bundle))
            state = "as_delivered" if tree_hash(bundle, kind) == t["tree"] else "changed"
            mine = {rel[len(t["name"]) + 1 :]: d for rel, d in want.items() if rel.startswith(t["name"] + "/")}
            differing = (
                []
                if state == "as_delivered" or not mine
                else [
                    {
                        "path": f,
                        "state": "missing" if f not in have else "added" if f not in mine else "changed",
                        "delivered": mine.get(f),
                        "here": have.get(f),
                    }
                    for f in sorted(mine.keys() | have.keys())
                    if mine.get(f) != have.get(f)
                ]
            )
        tasks.append({"name": t["name"], "tree": t["tree"], "state": state})
        if state != "as_delivered":
            differ.append(
                {
                    "name": t["name"],
                    "state": state,
                    "files": differing,
                    "modes_only": state == "changed" and bool(mine) and not differing,
                }
            )
    named = {t["name"] for t in lock["tasks"]}
    added = sorted(p.name for p in home.iterdir() if p.name not in named) if home.is_dir() else []
    off = {d["name"] for d in differ}
    held = {
        f"{digest}  {t['name']}/{path}"
        for t in lock["tasks"]
        if t["name"] not in off
        for path, digest, _ in files(home / t["name"])
    }
    said = (
        "missing"
        if listed is None
        else "as_delivered"
        if held <= set(listed) and (off or len(listed) == len(held))
        else "changed"
    )
    ok = not differ and not added and said == "as_delivered"
    return {
        "id": delivery,
        "dir": str(to),
        "ok": ok,
        "tasks": len(tasks),
        "as_delivered": len(tasks) - len(differ),
        "differ": differ,
        "added": added,
        "sums": said,
        "next_step": None if ok else f"kennet delivery pull {delivery} --to {to}",
    }


def _sums(text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        digest, _, rel = line.partition("  ")
        task, _, path = rel.partition("/")
        out.setdefault(task, {})[path] = digest
    return out


def _side(x: str, sleep: Callable[[float], None]) -> tuple[str, dict[str, dict[str, Any]], Callable[[], dict]]:
    at = Path(x)
    if (at / LOCK).is_file():
        lock = _read_lock((at / LOCK).read_bytes())
        if lock is None:
            raise _need(
                f"{at} holds no {LOCK} this reads: it is not a pulled delivery",
                "not_a_delivery",
                "kennet delivery list",
            )
        tasks = {
            t["name"]: {"tree": t["tree"], "images": t.get("images", []), "grader_images": t.get("grader")}
            for t in lock["tasks"]
        }

        def local() -> dict[str, dict[str, str]]:
            try:
                return _sums((at / SUMS).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}

        return lock["delivery"], tasks, local
    if not ID.fullmatch(x):
        raise _need(
            f"{x!r} is neither a pulled delivery's directory nor a delivery (<name>@<version>)",
            "invalid_delivery",
            "kennet delivery list",
        )
    got = show(x, sleep)
    return got["id"], {t["name"]: t for t in got["tasks"]}, lambda: _sums(_get(f"/deliveries/{x}/sums", sleep).text)


def diff(a: str, b: str, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    (a, old, read_a), (b, new, read_b) = _side(a, sleep), _side(b, sleep)
    changed_names = [
        n for n in old if n in new and (old[n]["tree"] != new[n]["tree"] or old[n]["images"] != new[n]["images"])
    ]
    files_a, files_b = (read_a(), read_b()) if changed_names else ({}, {})
    changed = []
    for n in changed_names:
        fa, fb = files_a.get(n, {}), files_b.get(n, {})
        paths = sorted(p for p in fa.keys() | fb.keys() if fa.get(p) != fb.get(p))
        graders = (old[n].get("grader_images"), new[n].get("grader_images"))
        images = old[n]["images"] != new[n]["images"]
        grading = any(p.startswith(GRADING) for p in paths) or (
            graders[0] != graders[1] if None not in graders else images
        )
        changed.append({"name": n, "grading_changed": grading, "images_changed": images, "files": paths})
    return {
        "from": a,
        "to": b,
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": changed,
        "unchanged": len(set(old) & set(new)) - len(changed),
        "grading_changed": [c["name"] for c in changed if c["grading_changed"]],
    }
