import stat

import pytest
import yaml

from metaphi import codebase_env, codebase_setup

WORKFLOW = b"""
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      db:
        image: postgres:16
        env: {POSTGRES_PASSWORD: pw, TOKEN: "${{ secrets.T }}"}
        ports: ["5432:5432"]
      cache: {image: "ghcr.io/acme/cache:1", ports: [6379]}
    steps: [{run: go test ./...}]
"""
COMPOSE = b"""
services:
  kafka: {image: "confluentinc/cp-kafka:latest", ports: ["9092:9092"]}
  postgres: {image: "postgres:latest", ports: ["5431:5432"]}
  app: {build: ., ports: ["8080:8080"]}
"""


def _read(files):
    return lambda p: files.get(p)


def test_public_names_only_official_images():
    assert codebase_env.public("postgres:16") == "postgres"
    assert codebase_env.public("docker.io/library/redis@sha256:ab") == "redis"
    assert codebase_env.public("confluentinc/cp-kafka:latest") is None
    assert codebase_env.public("registry.acme.io:5000/db") is None
    assert codebase_env.public("${{ matrix.db }}") is None


def test_ci_services_are_counted_and_started_first():
    files = {".github/workflows/ci.yml": WORKFLOW, "docker-compose-test-setup.yaml": COMPOSE, "Dockerfile": b""}
    sent, start = codebase_env.needs(set(files), _read(files))
    assert sent == {
        "ci": ["github"],
        "ci_files": 1,
        "ci_services": 2,
        "compose_files": 1,
        "compose_services": 3,
        "compose_built": 1,
        "service_images": ["postgres"],
        "other_images": 2,
        "dockerfiles": 1,
        "devcontainer": False,
    }
    assert start["from"] == "ci"
    db = start["services"]["s0"]
    assert db == {"image": "postgres:16", "environment": {"POSTGRES_PASSWORD": "pw"}, "ports": ["5432:5432"]}
    assert start["services"]["s1"]["ports"] == ["6379:6379"]


def test_no_ci_falls_back_to_the_compose_images():
    files = {"docker-compose-test-setup.yaml": COMPOSE, "deploy/docker-compose.yml": COMPOSE}
    sent, start = codebase_env.needs(set(files), _read(files))
    assert sent["ci"] == [] and sent["ci_files"] == 0
    assert start == {"from": "compose", "file": "docker-compose-test-setup.yaml", "names": ["kafka", "postgres"]}


def test_nothing_declared_starts_nothing():
    sent, start = codebase_env.needs({"main.go"}, _read({}))
    assert start is None and sent["compose_services"] == 0


def test_services_up_and_down_through_compose(tmp_path):
    calls = tmp_path / "calls"
    fake = tmp_path / "docker"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\ncat "$5" >> {calls} 2>/dev/null; exit 0\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    files = {".github/workflows/ci.yml": WORKFLOW}
    _sent, start = codebase_env.needs(set(files), _read(files))
    s = codebase_env.Services(str(fake), start, lambda d: None, tmp_path / "logs" / "services.log")
    got = s.up()
    assert got["up"] and got["from"] == "ci" and got["count"] == 2
    s.down()
    said = calls.read_text().splitlines()
    assert said[0].startswith(f"compose -p {s.project} -f ") and said[0].endswith("up -d --wait --wait-timeout 240")
    assert (
        yaml.safe_load(
            "\n".join(x for x in said[1 : said.index(next(y for y in said[1:] if y.startswith("compose")))])
        )["services"]["s0"]["image"]
        == "postgres:16"
    )
    assert any(x.startswith("compose") and x.endswith("down -v --remove-orphans -t 5") for x in said)
    assert not s.dir.exists()


def test_containers_are_hardened_and_join_the_runs_network(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        codebase_setup.subprocess,
        "run",
        lambda args, **k: (
            seen.append(args)
            or type("R", (), {"returncode": 0, "stderr": b"", "stdout": b'{"NCPU": 8, "MemTotal": 17179869184}'})()
        ),
    )
    monkeypatch.setattr(codebase_setup, "NETWORK", ["--network", "kennet-net"])
    codebase_setup.limits.cache_clear()
    codebase_setup.Shell("docker", "golang:1.22", tmp_path, tmp_path)
    run = seen[-1]
    assert run[run.index("--network") + 1] == "kennet-net" and "host" not in run
    for flag in (
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--init",
        f"--pids-limit={codebase_setup.PIDS}",
        "--cpus=7",
        f"--memory={17179869184 * 3 // 4}",
    ):
        assert flag in run, flag
    codebase_setup.limits.cache_clear()


def test_build_environment_is_allowlisted():
    got = codebase_setup.allowed(
        {
            "PATH": "/bin",
            "HOME": "/h",
            "LC_ALL": "C",
            "KENNET_TESTS": "a",
            "METAPHI_API_KEY": "mai_sk_x",
            "AWS_SECRET_ACCESS_KEY": "s",
            "GH_TOKEN": "t",
            "NPM_TOKEN": "t",
            "DATABASE_PASSWORD": "p",
            "RANDOM": "r",
        }
    )
    assert got == {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "KENNET_TESTS": "a"}


@pytest.mark.parametrize(
    ("service", "refused"),
    [
        ({"image": "postgres:16"}, []),
        ({"image": "postgres:16", "volumes": ["./data:/var/lib/postgresql/data", "pg:/x"]}, []),
        ({"image": "x", "privileged": True}, ["privileged"]),
        ({"image": "x", "network_mode": "host"}, ["network_mode"]),
        ({"image": "x", "pid": "host"}, ["host_namespace"]),
        ({"image": "x", "cap_add": ["SYS_ADMIN"]}, ["cap_add"]),
        ({"image": "x", "devices": ["/dev/kvm"]}, ["devices"]),
        ({"image": "x", "volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}, ["bind_mount"]),
        ({"image": "x", "volumes": ["../../etc:/x"]}, ["bind_mount"]),
        ({"image": "x", "volumes": [{"type": "bind", "source": "~/.ssh", "target": "/x"}]}, ["bind_mount"]),
    ],
)
def test_compose_services_asking_for_the_host_are_refused(tmp_path, service, refused):
    svcs = {"db": {"image": "postgres:16", "depends_on": ["dep"]}, "dep": service}
    assert codebase_env.refused(svcs, ["db"], tmp_path, tmp_path) == refused
