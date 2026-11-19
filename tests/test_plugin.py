import json
import os
import subprocess
from pathlib import Path

from metaphi import skill

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "codeval"


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_the_marketplaces_name_metaphi_and_point_at_the_codeval_plugin():
    claude = _json(ROOT / ".claude-plugin" / "marketplace.json")
    assert claude["name"] == "metaphi" and claude["owner"]["name"] == "Metaphi AI"
    [entry] = claude["plugins"]
    assert (entry["name"], entry["source"], entry["description"]) == ("codeval", "./plugins/codeval", skill.ABOUT)
    codex = _json(ROOT / ".agents" / "plugins" / "marketplace.json")
    [entry] = codex["plugins"]
    assert codex["name"] == "metaphi" and entry["name"] == "codeval"
    assert entry["source"] == {"source": "local", "path": "./plugins/codeval"}
    assert set(entry["policy"]) == {"installation", "authentication"} and entry["category"]


def test_the_plugin_says_what_codeval_is_in_the_founders_words_and_runs_one_server():
    claude = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    portable = _json(PLUGIN / "plugin.json")
    assert claude["name"] == portable["name"] == "codeval"
    assert claude["description"] == portable["description"] == skill.ABOUT
    allowed = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license"}
    assert set(portable) <= allowed | {"keywords", "extensions"}
    assert portable["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    dotted = _json(PLUGIN / ".mcp.json")["mcpServers"]
    plain = _json(PLUGIN / "mcp.json")
    assert plain["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert list(dotted) == list(plain["mcpServers"]) == ["metaphi"]
    assert plain["mcpServers"]["metaphi"] == {"type": "stdio", **dotted["metaphi"]}
    text = (PLUGIN / "skills" / "codeval" / "SKILL.md").read_text()
    assert text.startswith(f"---\nname: codeval\ndescription: {skill.ABOUT}.\n---\n")
    assert all(t in text for t in ("codeval_start", "codeval_status", "codeval_send"))


def _launch(tmp_path: Path, path: str) -> subprocess.CompletedProcess:
    server = _json(PLUGIN / ".mcp.json")["mcpServers"]["metaphi"]
    env = {"PATH": path, "HOME": str(tmp_path / "home")}
    return subprocess.run(
        [server["command"], *server["args"]], env=env, capture_output=True, text=True, timeout=30, check=False
    )


def test_the_launcher_runs_kennet_mcp_when_kennet_is_on_the_path(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "kennet").write_text('#!/bin/sh\necho "kennet $*"\n')
    (bin_ / "kennet").chmod(0o755)
    (bin_ / "curl").write_text("#!/bin/sh\necho curl >&2\nexit 1\n")
    (bin_ / "curl").chmod(0o755)
    got = _launch(tmp_path, f"{bin_}:/usr/bin:/bin")
    assert got.stdout == "kennet mcp\n" and "curl" not in got.stderr


def test_without_kennet_the_launcher_installs_it_with_install_sh_and_keeps_stdout_clean(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    home = tmp_path / "home"
    script = (
        "#!/bin/sh\n"
        f'echo "curl $*" >> {tmp_path}/curl.log\n'
        "cat <<'EOF'\n"
        'mkdir -p "$HOME/.local/bin"\n'
        'printf \'#!/bin/sh\\necho "installed kennet $*"\\n\' > "$HOME/.local/bin/kennet"\n'
        'chmod 755 "$HOME/.local/bin/kennet"; echo "Installed kennet; KENNET_SETUP_AGENTS=$KENNET_SETUP_AGENTS"\n'
        "EOF\n"
    )
    (bin_ / "curl").write_text(script)
    (bin_ / "curl").chmod(0o755)
    got = _launch(tmp_path, f"{bin_}:/usr/bin:/bin")
    assert got.stdout == "installed kennet mcp\n", got.stderr
    assert "Installed kennet; KENNET_SETUP_AGENTS=0" in got.stderr
    said = (tmp_path / "curl.log").read_text()
    assert "--proto =https" in said and said.strip().endswith("/releases/latest/download/install.sh")
    assert os.access(home / ".local" / "bin" / "kennet", os.X_OK)
