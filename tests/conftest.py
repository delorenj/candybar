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
