"""Deploy-hygiene gate (Story 1.9): no LAN IPs, no plaintext secrets on deploy surfaces.

The CI workflow runs scripts/check-deploy-hygiene.sh directly; this suite runs
the same script against the repo (so the gate is enforced by `pytest` too) and
pins its detection with planted-violation fixtures in throwaway roots.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-deploy-hygiene.sh"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _plant(root: Path, line: str) -> None:
    (root / "deploy").mkdir(parents=True, exist_ok=True)
    (root / "deploy" / "compose.yml").write_text(line)


# Assembled at runtime so this repo's own secret scanner (git pre-commit
# guard) doesn't see a literal PEM marker in the fixture — the gate still
# sees the assembled string when the planted file is scanned.
PEM_MARKER = "-" * 5 + "BEGIN OPENSSH PRIVATE KEY" + "-" * 5


def test_repo_deploy_surfaces_are_clean() -> None:
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_gate_fails_loudly_without_ripgrep(tmp_path: Path) -> None:
    # The Ubuntu 26 runner image dropped ripgrep; without this guard every
    # scan silently returned empty and the gate printed OK over unchecked
    # files (CI run 40275446031). Exit 2 = tooling error, never clean.
    _plant(tmp_path, "NATS_URL=nats://192.168.1.50:4222\n")
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), str(tmp_path)],  # absolute: PATH is stripped below
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": str(tmp_path)},
    )
    assert result.returncode == 2
    assert "ripgrep" in result.stderr


@pytest.mark.parametrize(
    ("line", "label"),
    [
        ("NATS_URL=nats://192.168.1.50:4222\n", "lan-192"),
        ("NATS_URL=nats://10.0.0.4:4222\n", "lan-10"),
        ("NATS_URL=nats://172.20.0.4:4222\n", "lan-172"),
        ("DATABASE_URL=postgresql://candybar:hunter2hunter2@db:5432/db\n", "dsn-password"),
        ('api_key: "abc123def456"\n', "keyword-secret"),
        ('KEY="' + PEM_MARKER + '"\n', "private-key"),
        # A sanctioned ref on the same line must not whitewash a co-located
        # plaintext secret (span-strip, not line-whitelist).
        ("A=op://DeLoSecrets/x/y password=hunter2hunter2\n", "mixed-line-secret"),
    ],
)
def test_gate_rejects_planted_violations(tmp_path: Path, line: str, label: str) -> None:
    _plant(tmp_path, line)
    result = _run(tmp_path)
    assert result.returncode == 1, f"{label}: gate missed {line!r}"
    assert "FAIL" in result.stderr


@pytest.mark.parametrize(
    ("line", "label"),
    [
        # op:// vault references are THE sanctioned secret shape.
        (
            "DATABASE_URL=postgresql+psycopg://candybar:op://DeLoSecrets/abc/password@host.docker.internal:5432/db\n",
            "op-ref-dsn",
        ),
        # Shell interpolation is resolved at deploy time, never a literal.
        ("NATS_URL=nats://${NATS_HOST}:4222\n", "shell-interpolation"),
        # Docker aliases and loopback are names/loopback, not LAN IPs.
        ("NATS_URL=nats://nats:4222\n", "docker-alias"),
        ("API_URL=http://127.0.0.1:8700\n", "loopback"),
    ],
)
def test_gate_allows_sanctioned_shapes(tmp_path: Path, line: str, label: str) -> None:
    _plant(tmp_path, line)
    result = _run(tmp_path)
    assert result.returncode == 0, f"{label}: false positive on {line!r}: {result.stderr}"
