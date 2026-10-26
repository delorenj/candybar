"""In-container metering smoke (Story 3.1): run as the daemon's uid inside the image.

    docker cp scripts/container-metering-smoke.py <ctr>:/tmp/smoke.py
    docker exec -u candybar <ctr> python /tmp/smoke.py

Spawns a 32 MiB canary through the real runner against the entrypoint's
delegated scope tree (/sys/fs/cgroup/candybar) as the unprivileged user. It
fails if the child could not join its scope (e.g. the entrypoint stopped
handing cgroup.procs to uid 10001) or memory.peak did not reach the canary.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

from candybar.executor.runner import run_invocation

CANARY_MIB = 32
ROOT = os.environ.get("CANDYBAR_CGROUP_ROOT") or "/sys/fs/cgroup/candybar"


def main() -> int:
    work = Path(tempfile.mkdtemp())
    canary = work / "canary.py"
    canary.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(0.2)\n"
        f"blob = b'\\x01' * ({CANARY_MIB} * 1024 * 1024)\ntime.sleep(0.1)\n"
    )
    canary.chmod(0o755)
    outcome = asyncio.run(
        run_invocation(
            entrypoint=str(canary),
            invocation_id=uuid.uuid4(),
            candybar_id=uuid.uuid4(),
            trigger="manual",
            tick_id=None,
            root=ROOT,
        )
    )
    peak_mib = outcome.memory_peak_bytes / 2**20
    print(f"uid={os.getuid()} scoped={outcome.scoped} peak_mib={peak_mib:.1f} cpu={outcome.cpu_load}")
    ok = outcome.exit_code == 0 and outcome.scoped and peak_mib >= CANARY_MIB
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
