"""W5-1: deploy/compose.mac.yml really removes the Linux/homelab wiring from compose.yml.

Compose MERGES lists and maps across -f files, so an overlay that merely sets a
key keeps the base values too. The overlay uses explicit !reset/!override tags
(Compose >= 2.24.4); these tests prove the merged result, not the intent:

  - a static check that every Linux-only key is tagged (runs everywhere), and
  - a real `docker compose config` render of base + overlay (runs wherever Docker
    Compose >= 2.24.4 exists, including the CI runner), asserting no `proxy`
    network, no Traefik labels, no /home/delorenj mounts, loopback-only ports.

The render uses a dummy password and a tmp copy of the two files with NO `.env`
beside them, so a leftover deploy/.env on a developer box cannot mask a failed
`env_file: !reset`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "deploy" / "compose.yml"
OVERLAY = REPO / "deploy" / "compose.mac.yml"
MIN_COMPOSE = (2, 24, 4)


class _Tagged:
    def __init__(self, tag: str, value):
        self.tag, self.value = tag, value


def _load_overlay() -> dict:
    class Loader(yaml.SafeLoader):
        pass

    for tag in ("!reset", "!override"):
        def construct(loader, node, tag=tag):
            if isinstance(node, yaml.MappingNode):
                return _Tagged(tag, loader.construct_mapping(node, deep=True))
            if isinstance(node, yaml.SequenceNode):
                return _Tagged(tag, loader.construct_sequence(node, deep=True))
            return _Tagged(tag, loader.construct_scalar(node))
        Loader.add_constructor(tag, construct)
    return yaml.load(OVERLAY.read_text(), Loader=Loader)


def _compose_version() -> tuple[int, ...] | None:
    docker = shutil.which("docker")
    if not docker:
        return None
    result = subprocess.run([docker, "compose", "version", "--short"], capture_output=True, text=True, timeout=30, check=False)
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", result.stdout.strip()) if result.returncode == 0 else None
    return tuple(int(part) for part in match.groups()) if match else None


needs_compose = pytest.mark.skipif(
    (_compose_version() or (0, 0, 0)) < MIN_COMPOSE,
    reason="needs `docker compose` >= 2.24.4 for !reset/!override (render proof)",
)


# ---- static: every Linux-only key is explicitly removed ---------------------------------------

def test_overlay_tags_every_linux_only_key_on_the_candybar_service():
    service = _load_overlay()["services"]["candybar"]
    for key in ("env_file", "extra_hosts", "volumes", "labels"):
        assert isinstance(service[key], _Tagged) and service[key].tag == "!reset" and service[key].value == [], key
    assert service["networks"].tag == "!override" and set(service["networks"].value) == {"candybar-internal", "bloodbank-network"}
    assert service["ports"].tag == "!override"
    assert all(port["host_ip"] == "127.0.0.1" for port in service["ports"].value)


def test_overlay_replaces_the_top_level_network_map_so_proxy_cannot_survive_the_merge():
    networks = _load_overlay()["networks"]
    assert networks.tag == "!override"
    assert set(networks.value) == {"candybar-internal", "bloodbank-network"}
    assert networks.value["bloodbank-network"] == {"external": True, "name": "bloodbank-network"}  # the join to the Mac's bus
    assert "proxy" in yaml.safe_load(BASE.read_text())["networks"]  # control: the base does carry it


def test_overlay_password_is_required_and_never_literal():
    text = OVERLAY.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert code.count("${CANDYBAR_DB_PASSWORD:?") == 2  # candybar (PGPASSWORD) and postgres (POSTGRES_PASSWORD)
    assert "/home/delorenj" not in code and "traefik" not in code.lower()
    assert not re.search(r"://[^/@\s:]+:[^/@\s]+@", code)  # DSN carries no userinfo password


# ---- rendered: the merge result ---------------------------------------------------------------

def _render(tmp_path: Path, *files: str, password: bool = True, env_file: bool = False) -> subprocess.CompletedProcess[str]:
    deploy = tmp_path / "deploy"
    deploy.mkdir(exist_ok=True)
    for name in files:
        shutil.copy(REPO / "deploy" / name, deploy / name)
    if env_file:
        (deploy / ".env").write_text("DATABASE_URL=postgresql://x@host.docker.internal:5432/candybar\n")
    # Inherit the real environment (HOME included): `docker compose` is a CLI plugin found under
    # ~/.docker/cli-plugins on a Mac, so a fake HOME makes it vanish ("unknown shorthand flag").
    # Only what could mask the proof is dropped: any CANDYBAR_* value (a password in the
    # developer's shell would hide the "refuses without it" case) and DATABASE_URL.
    env = {key: value for key, value in os.environ.items() if not key.startswith("CANDYBAR_") and key != "DATABASE_URL"}
    if password:
        env["CANDYBAR_DB_PASSWORD"] = "dummy-render-only"
    argv = ["docker", "compose", *(arg for name in files for arg in ("-f", str(deploy / name))), "config", "--format", "json"]
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60, check=False, cwd=tmp_path)


def _scan_text(rendered: dict) -> str:
    """The render as text, minus build.context (the tmp dir the test renders in, not config under test)."""
    scrubbed = json.loads(json.dumps(rendered))
    for service in scrubbed["services"].values():
        service.pop("build", None)
    return json.dumps(scrubbed)


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


@needs_compose
def test_control_the_base_file_alone_has_everything_the_overlay_must_remove(tmp_path):
    result = _render(tmp_path, "compose.yml", env_file=True)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    service = rendered["services"]["candybar"]
    assert "proxy" in rendered["networks"] and "proxy" in service["networks"]
    assert any(k.startswith("traefik.") for k in service["labels"])
    assert any("/home/delorenj" in v["source"] for v in service["volumes"])
    # compose inlines env_file into `environment`: the op-injected DSN and the host gateway are both present.
    assert "host.docker.internal" in service["environment"]["DATABASE_URL"] and service["extra_hosts"]


@needs_compose
def test_merged_render_has_no_proxy_network_no_traefik_labels_no_home_delorenj_mounts(tmp_path):
    result = _render(tmp_path, "compose.yml", "compose.mac.yml")
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    services = rendered["services"]
    # 1. no external proxy network, anywhere
    assert "proxy" not in rendered["networks"]
    assert all("proxy" not in service.get("networks", {}) for service in services.values())
    # 2. no Traefik labels, on any service
    assert not any(str(key).startswith("traefik") for service in services.values() for key in service.get("labels", {}))
    assert "traefik" not in _scan_text(rendered).lower()
    # 3. no /home/delorenj mounts (and no host bind mounts at all: the profile is exec-only)
    assert "/home/delorenj" not in _scan_text(rendered)
    assert not [v for service in services.values() for v in service.get("volumes", []) if v["type"] == "bind"]


@needs_compose
def test_merged_render_keeps_only_the_bloodbank_join_as_external_and_resets_the_rest(tmp_path):
    rendered = json.loads(_render(tmp_path, "compose.yml", "compose.mac.yml").stdout)
    externals = {name for name, net in rendered["networks"].items() if net.get("external")}
    assert externals == {"bloodbank-network"}
    candybar = rendered["services"]["candybar"]
    assert set(candybar["networks"]) == {"candybar-internal", "bloodbank-network"}
    assert candybar["environment"]["NATS_URL"] == "nats://nats:4222"  # the alias the Mac's 33god-bus gives NATS
    for gone in ("env_file", "extra_hosts", "labels", "volumes"):
        assert not candybar.get(gone), gone
    assert candybar["command"] == ["--roles", "heartbeat,dispatcher,api"]  # control plane only; the executor is a launchd job
    assert candybar["environment"]["CANDYBAR_CGROUP_ROOT"] == ""


@needs_compose
def test_merged_render_publishes_only_on_loopback(tmp_path):
    rendered = json.loads(_render(tmp_path, "compose.yml", "compose.mac.yml").stdout)
    published = {(name, int(p["published"]), p["host_ip"]) for name, s in rendered["services"].items() for p in s.get("ports", [])}
    assert published == {("candybar", 8700, "127.0.0.1"), ("candybar-postgres", 5435, "127.0.0.1")}


@needs_compose
def test_merged_render_wires_one_password_into_container_database_and_dsn_stays_passwordless(tmp_path):
    rendered = json.loads(_render(tmp_path, "compose.yml", "compose.mac.yml").stdout)
    candybar, postgres = rendered["services"]["candybar"], rendered["services"]["candybar-postgres"]
    assert candybar["environment"]["PGPASSWORD"] == postgres["environment"]["POSTGRES_PASSWORD"] == "dummy-render-only"
    assert candybar["environment"]["DATABASE_URL"] == "postgresql+psycopg://candybar@candybar-postgres:5432/candybar"
    assert candybar["depends_on"]["candybar-postgres"]["condition"] == "service_healthy"
    assert "@sha256:" in postgres["image"]  # pinned by digest
    # No other string in the render may contain the dummy password except those two env values.
    assert sum(node == "dummy-render-only" for node in _walk(rendered)) == 2


@needs_compose
def test_render_refuses_without_the_password(tmp_path):
    result = _render(tmp_path, "compose.yml", "compose.mac.yml", password=False)
    assert result.returncode != 0 and "CANDYBAR_DB_PASSWORD must come from env-op-resolve" in result.stderr
