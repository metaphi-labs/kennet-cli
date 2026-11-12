import os
import stat

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
    assert sent == {"ci": ["github"], "ci_files": 1, "ci_services": 2, "compose_files": 1, "compose_services": 3,
                    "compose_built": 1, "service_images": ["postgres"], "other_images": 2, "dockerfiles": 1,
                    "devcontainer": False}
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
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\ncat \"$5\" >> {calls} 2>/dev/null; exit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    files = {".github/workflows/ci.yml": WORKFLOW}
    _sent, start = codebase_env.needs(set(files), _read(files))
    s = codebase_env.Services(str(fake), start, lambda d: None, tmp_path / "logs" / "services.log")
    got = s.up()
    assert got["up"] and got["from"] == "ci" and got["count"] == 2
    s.down()
    said = calls.read_text().splitlines()
    assert said[0].startswith(f"compose -p {s.project} -f ") and said[0].endswith("up -d --wait --wait-timeout 240")
    assert yaml.safe_load("\n".join(x for x in said[1:said.index(next(y for y in said[1:] if y.startswith("compose")))]))["services"]["s0"]["image"] \
        == "postgres:16"
    assert any(x.startswith("compose") and x.endswith("down -v --remove-orphans -t 5") for x in said)
    assert not s.dir.exists()


def test_containers_join_the_host_network_while_services_are_up(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(codebase_setup.subprocess, "run",
                        lambda args, **k: seen.append(args) or type("R", (), {"returncode": 0, "stderr": b""})())
    monkeypatch.setattr(codebase_setup, "NETWORK", ["--network", "host"])
    codebase_setup.Shell("docker", "golang:1.22", tmp_path, tmp_path)
    assert "--network" in seen[0] and seen[0][seen[0].index("--network") + 1] == "host"
    assert os.environ.get("KENNET_TESTS") is None


def test_the_readiness_table_draws_the_answer_and_counts_only_yes_and_no(capsys):
    from metaphi import cli
    got = {"repos": [{"name": "core", "checks": [
        {"key": "ci", "label": "CI config included", "state": "no", "fix": "No CI config found."},
        {"key": "services", "label": "Test services start here", "state": "not_needed"},
        {"key": "build", "label": "Builds on this machine", "state": "yes"},
        {"key": "tests_run", "label": "Tests run on this machine", "state": "not_collected"}]}], "note": "Each no."}
    cli._readiness(got)
    out = capsys.readouterr().out
    assert "\x1b[" not in out                                   # not a terminal: no colour
    assert "│ Yes" in out and "1 of 2" in out and "· not collected" in out and "– not needed" in out
    assert "core · CI config included\n    No CI config found." in out and out.rstrip().endswith("Each no.")
    cli._readiness({"repos": []})
    assert capsys.readouterr().out == ""
