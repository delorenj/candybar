"""Suite-wide test defaults.

GPU metering (Story 3.2) samples the host's real `nvidia-smi pmon` by default.
The unit suite must be deterministic on GPU and GPU-less hosts alike, so the
process-default sampler is off unless the environment says otherwise.
tests/test_gpu.py drives its own samplers, using a fake pmon or the real one
behind CANDYBAR_TEST_GPU=1.
"""

from __future__ import annotations

import os

os.environ.setdefault("CANDYBAR_NVIDIA_SMI", "")
# Story 7.2 (AD-17): the host executor serves /metrics on a real port and
# refuses to consume on an alembic-head mismatch. Neither belongs in the unit
# suite by default: no port contention, no database to compare against.
# tests/test_executor_host.py drives both explicitly.
os.environ.setdefault("CANDYBAR_EXECUTOR_METRICS_PORT", "")
os.environ.setdefault("CANDYBAR_SCHEMA_GUARD", "off")
