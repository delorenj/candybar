"""Exercise watcher installer with fake CLI/systemctl; never touch host units."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_watch_installer_enrollment_is_explicit_and_existing_config_preserved(tmp_path):
    repo = tmp_path / "repo"
    host = repo / "deploy/host"
    host.mkdir(parents=True)
    for name in ("install-watch.sh", "watch.toml.example"):
        shutil.copyfile(ROOT / "deploy/host" / name, host / name)
    binary = repo / ".venv/bin"
    binary.mkdir(parents=True)
    cli = binary / "candybar"
    cli.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    cli.chmod(0o755)
    systemctl = binary / "systemctl"
    systemctl.write_text('#!/bin/sh\nprintf "UNEXPECTED SYSTEMCTL\\n" >> "$CALLS"\nexit 99\n')
    systemctl.chmod(0o755)
    config_root = tmp_path / "xdg"
    env = {
        **os.environ, "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config_root), "CALLS": str(tmp_path / "calls"),
        "PATH": str(binary) + ":" + os.environ["PATH"],
    }
    args = ["bash", str(host / "install-watch.sh")]
    first = subprocess.run(args, env=env, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    config = config_root / "candybar/watch.toml"
    assert config.read_text() == (host / "watch.toml.example").read_text()
    assert not (tmp_path / "calls").exists()  # no reconcile or activation on first run
    config.write_text('projects=["~/code/authoritative"]\n')
    preview = subprocess.run([*args, "--dry-run"], env=env, capture_output=True, text=True, check=False)
    assert preview.returncode == 0, preview.stderr
    assert config.read_text() == 'projects=["~/code/authoritative"]\n'
    assert (tmp_path / "calls").read_text() == f"watch --config {config} --dry-run\n"
