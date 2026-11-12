"""What a repository says its tests need around them, read from its own files at the tip, and those services started
in containers for the checks. Only counts and public image names leave the machine; the commands, settings and
values stay here."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

CI = (("github", re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")), ("gitlab", re.compile(r"^\.gitlab-ci\.ya?ml$")),
      ("circleci", re.compile(r"^\.circleci/config\.ya?ml$")), ("jenkins", re.compile(r"(^|/)Jenkinsfile$")),
      ("azure", re.compile(r"^(azure-pipelines|\.azure-pipelines/[^/]+)\.ya?ml$")),
      ("bitbucket", re.compile(r"^bitbucket-pipelines\.ya?ml$")), ("travis", re.compile(r"^\.travis\.ya?ml$")),
      ("buildkite", re.compile(r"^\.buildkite/[^/]+\.ya?ml$")), ("drone", re.compile(r"^\.drone\.ya?ml$")),
      ("woodpecker", re.compile(r"^\.woodpecker(\.ya?ml|/[^/]+\.ya?ml)$")), ("teamcity", re.compile(r"^\.teamcity/")),
      ("bamboo", re.compile(r"^bamboo-specs/")), ("cloudbuild", re.compile(r"^cloudbuild\.ya?ml$")))
COMPOSE = re.compile(r"(^|/)(docker-)?compose[\w.-]*\.ya?ml$", re.IGNORECASE)
DOCKERFILE = re.compile(r"(^|/)(Dockerfile|Containerfile)([.\w-]*)$|\.dockerfile$", re.IGNORECASE)
DEVCONTAINER = re.compile(r"^\.devcontainer(\.json$|/)")
VENDORED = re.compile(r"(^|/)(vendor|node_modules|third_party|external|deps|testdata)/", re.IGNORECASE)
WAIT_SECONDS = 240


def _yaml(body: bytes | None) -> Any:
    try:
        return yaml.safe_load(body.decode("utf-8", "replace")) if body else None
    except yaml.YAMLError:
        return None


def public(image: Any) -> str | None:
    """An official Docker Hub image's name (`postgres:16` -> postgres); None for any other image, whose name may be
    the seller's own."""
    if not isinstance(image, str) or "$" in image or not image.strip():
        return None
    name = image.strip().split("@")[0].rsplit(":", 1)[0] if ":" in image.split("/")[-1] else image.strip().split("@")[0]
    for prefix in ("docker.io/library/", "library/", "docker.io/"):
        name = name.removeprefix(prefix)
    return name.lower() if re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", name) else None


def _ci_services(paths: set[str], read: Callable[[str], bytes | None]) -> list[dict[str, Any]]:
    """GitHub jobs' and GitLab's service containers, as compose services: image, environment, published ports."""
    out: list[dict[str, Any]] = []
    for p in sorted(paths):
        if not CI[0][1].match(p) or not isinstance(doc := _yaml(read(p)), dict) or not isinstance(doc.get("jobs"), dict):
            continue
        for job in doc["jobs"].values():
            services = job.get("services") if isinstance(job, dict) else None
            for svc in services.values() if isinstance(services, dict) else []:
                svc = {"image": svc} if isinstance(svc, str) else svc
                if not isinstance(svc, dict) or not isinstance(svc.get("image"), str) or "${{" in svc["image"]:
                    continue
                env = svc.get("env") if isinstance(svc.get("env"), dict) else {}
                ports = [str(x) for x in svc.get("ports") or [] if isinstance(x, (str, int)) and "${{" not in str(x)]
                out.append({"image": svc["image"], "environment": {str(k): str(v) for k, v in env.items()
                                                                    if "${{" not in str(v)},
                            "ports": [x if ":" in x else f"{x}:{x}" for x in ports]})
    for p in (x for x in paths if CI[1][1].match(x)):
        doc = _yaml(read(p))
        jobs = [doc] + [v for v in doc.values() if isinstance(v, dict)] if isinstance(doc, dict) else []
        for job in jobs:
            for svc in job.get("services") or [] if isinstance(job.get("services"), list) else []:
                image = svc.get("name") if isinstance(svc, dict) else svc
                if isinstance(image, str) and "$" not in image:
                    out.append({"image": image, "environment": {}, "ports": []})
    seen, kept = set(), []
    for s in out:
        if (key := (s["image"], tuple(s["ports"]))) not in seen:
            seen.add(key)
            kept.append(s)
    return kept


def _compose(paths: set[str], read: Callable[[str], bytes | None]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for p in sorted(x for x in paths if COMPOSE.search(x) and not VENDORED.search(x)):
        doc = _yaml(read(p))
        if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
            out.append((p, {k: v for k, v in doc["services"].items() if isinstance(v, dict)}))
    return out


def needs(paths: set[str], read: Callable[[str], bytes | None]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """What the repository declares at the tip: the counts sent (first), and what to start here (second; None when
    nothing declares a service that runs from an image)."""
    ci = sorted({name for name, pattern in CI for p in paths if pattern.search(p)})
    ci_files = sum(1 for p in paths if any(pattern.search(p) for _n, pattern in CI))
    from_ci = _ci_services(paths, read)
    composed = _compose(paths, read)
    in_compose = [s for _p, svcs in composed for s in svcs.values()]
    images = [s["image"] for s in from_ci] + [s.get("image") for s in in_compose if not s.get("build")]
    names = sorted({n for i in images if (n := public(i))})
    sent = {"ci": ci, "ci_files": ci_files, "ci_services": len(from_ci), "compose_files": len(composed),
            "compose_services": len(in_compose), "compose_built": sum(bool(s.get("build")) for s in in_compose),
            "service_images": names, "other_images": sum(1 for i in images if not public(i)),
            "dockerfiles": sum(1 for p in paths if DOCKERFILE.search(p) and not VENDORED.search(p)),
            "devcontainer": any(DEVCONTAINER.search(p) for p in paths)}
    start: dict[str, Any] | None = None
    if from_ci:   # what CI starts the tests beside is the nearest to how the owner runs them
        start = {"from": "ci", "services": {f"s{n}": {k: v for k, v in s.items() if v} for n, s in enumerate(from_ci)}}
    else:
        ranked = sorted(composed, key=lambda c: (not re.search(r"test|ci|dev", c[0], re.IGNORECASE), c[0].count("/"), c[0]))
        for path, svcs in ranked:
            names_ = [k for k, s in svcs.items() if isinstance(s.get("image"), str) and not s.get("build")]
            if names_:
                start = {"from": "compose", "file": path, "names": names_}
                break
    return sent, start


class Services:
    """The services `needs` found, up in containers on this machine for one repository's checks, published on its
    ports as CI and compose publish them; down, with their volumes, when the checks end."""

    def __init__(self, engine: str, start: dict[str, Any], write: Callable[[Path], None], log: Path):
        self.engine, self.start, self.write, self.log = engine, start, write, log
        self.project = f"kennet-{uuid.uuid4().hex[:10]}"
        self.dir = Path(tempfile.mkdtemp(prefix="kennet-services-"))
        self.files: list[str] = []
        self.got: dict[str, Any] = {"from": start["from"], "count": len(start.get("services") or start.get("names") or []),
                                    "up": False, "seconds": 0}

    def _compose(self, *args: str, timeout: float) -> int | None:
        with self.log.open("ab") as f:
            try:
                return subprocess.run([self.engine, "compose", "-p", self.project, *self.files, *args], cwd=self.dir,
                                      stdout=f, stderr=subprocess.STDOUT, timeout=timeout, check=False).returncode
            except (OSError, subprocess.TimeoutExpired):
                return None

    def up(self) -> dict[str, Any]:
        started = time.monotonic()
        self.log.parent.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        if self.start["from"] == "ci":
            (self.dir / "compose.yaml").write_text(yaml.safe_dump({"services": self.start["services"]}))
            self.files = ["-f", str(self.dir / "compose.yaml")]
        else:
            self.write(self.dir)
            self.files, names = ["-f", str(self.dir / self.start["file"])], self.start["names"]
        code = self._compose("up", "-d", "--wait", "--wait-timeout", str(WAIT_SECONDS), *names, timeout=WAIT_SECONDS + 900)
        self.got.update(up=code == 0, seconds=round(time.monotonic() - started))
        return self.got

    def down(self) -> None:
        self._compose("down", "-v", "--remove-orphans", "-t", "5", timeout=600)
        shutil.rmtree(self.dir, ignore_errors=True)


__all__ = ["Services", "needs", "public"]
