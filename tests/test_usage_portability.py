"""A source install can move without changing Candybar's advertised contract."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_usage_snapshot_is_install_and_cwd_independent(tmp_path):
    outputs = []
    for location in ("canonical", "worktree"):
        install = tmp_path / location
        shutil.copytree(ROOT / "src", install / "src", ignore=shutil.ignore_patterns("__pycache__"))
        env = {**os.environ, "PYTHONPATH": str(install / "src")}
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/export_usage.py"), "--stdout"],
            cwd=install,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    assert outputs[0] == (ROOT / "src/candybar/cli/candybar.usage.kdl").read_text()
    assert 'default="<candybar-install>/deploy/scheduling-policy.json"' in outputs[0]
    assert str(tmp_path) not in outputs[0]
    assert ".worktrees/" not in outputs[0]
    subprocess.run(
        ["mise", "exec", "--", "usage", "lint", "-q", "-"],
        input=outputs[0], text=True, check=True,
    )
    subprocess.run(
        ["mise", "exec", "--", "usage", "generate", "json", "-f", "-"],
        input=outputs[0], text=True, check=True, capture_output=True,
    )
