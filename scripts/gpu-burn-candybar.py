#!/usr/bin/env python3
"""A dependency-free GPU load candybar (Story 3.2 proof job).

It keeps the first NVIDIA GPU's SMs busy for GPU_BURN_SECONDS (default 3)
through the CUDA driver API (libcuda via ctypes). A tiny PTX busy-loop kernel
is JIT-compiled by the driver, so no CUDA toolkit, torch or other package is
needed. That makes it runnable on the host and in the candybar container
(where the NVIDIA runtime injects libcuda under `gpus: all`).

It speaks the AD-8 protocol like any bare executable: exit 0 = completed. It
writes no result file, since GPU load is executor-measured, never
self-reported. Without libcuda or a GPU it exits 3 with a reason on stderr.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

PTX = rb"""
.version 6.0
.target sm_50
.address_size 64

.visible .entry burn(.param .u64 out, .param .u32 iters)
{
    .reg .pred p;
    .reg .f32 f<3>;
    .reg .b32 r<3>;
    .reg .b64 rd<3>;

    ld.param.u64 rd1, [out];
    ld.param.u32 r1, [iters];
    mov.f32 f1, 0f3F800000;
    mov.f32 f2, 0f3F000000;
    mov.u32 r2, 0;
LOOP:
    fma.rn.f32 f1, f1, f2, f2;
    add.u32 r2, r2, 1;
    setp.lt.u32 p, r2, r1;
    @p bra LOOP;
    cvta.to.global.u64 rd2, rd1;
    st.global.f32 [rd2], f1;
    ret;
}
"""


def _check(rc: int, what: str) -> None:
    if rc != 0:
        print(f"gpu-burn: {what} failed (CUresult {rc})", file=sys.stderr)
        sys.exit(3)


def main() -> int:
    seconds = float(os.environ.get("GPU_BURN_SECONDS", "3"))
    try:
        cuda = ctypes.CDLL("libcuda.so.1")
    except OSError as exc:
        print(f"gpu-burn: no CUDA driver ({exc})", file=sys.stderr)
        return 3
    _check(cuda.cuInit(0), "cuInit")
    dev = ctypes.c_int()
    _check(cuda.cuDeviceGet(ctypes.byref(dev), 0), "cuDeviceGet")
    ctx = ctypes.c_void_p()
    _check(cuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev), "cuCtxCreate")
    mod = ctypes.c_void_p()
    _check(cuda.cuModuleLoadData(ctypes.byref(mod), ctypes.c_char_p(PTX)), "cuModuleLoadData")
    fn = ctypes.c_void_p()
    _check(cuda.cuModuleGetFunction(ctypes.byref(fn), mod, b"burn"), "cuModuleGetFunction")
    out = ctypes.c_uint64()
    _check(cuda.cuMemAlloc_v2(ctypes.byref(out), ctypes.c_size_t(4)), "cuMemAlloc")
    iters = ctypes.c_uint32(200_000)
    params = (ctypes.c_void_p * 2)(
        ctypes.cast(ctypes.byref(out), ctypes.c_void_p),
        ctypes.cast(ctypes.byref(iters), ctypes.c_void_p),
    )
    deadline = time.monotonic() + seconds
    launches = 0
    while time.monotonic() < deadline:
        _check(
            cuda.cuLaunchKernel(fn, 4096, 1, 1, 256, 1, 1, 0, None, params, None),
            "cuLaunchKernel",
        )
        _check(cuda.cuCtxSynchronize(), "cuCtxSynchronize")
        launches += 1
    cuda.cuMemFree_v2(out)
    cuda.cuCtxDestroy_v2(ctx)
    print(f"gpu-burn: {launches} kernel launches in {seconds:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
