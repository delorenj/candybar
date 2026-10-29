#!/usr/bin/env python3
"""Promote reviewed source bytes and PATCH only the existing crank runner."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tarfile
import time
import tomllib
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("scoped_crank", HERE / "launch.py")
assert spec is not None and spec.loader is not None
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
API = "http://127.0.0.1:8700"
REVIEW = launcher.HOME / "cache/scratch/monitor-ack-repair"
TRANSACTION = launcher.RUNTIME.parent / "activation.json"


def request(method: str, suffix: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        API + suffix,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def promote() -> dict:
    manifest = json.loads((REVIEW / "image-manifest.json").read_text())
    archive = Path(manifest["archive"])
    if (
        manifest["commit"] != launcher.COMMIT
        or hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
        != launcher.ARCHIVE_SHA256
    ):
        raise RuntimeError("candidate archive mismatch")
    if launcher.RUNTIME.exists():
        raise RuntimeError(
            "versioned runtime already exists; inspect rather than overwrite"
        )
    staging = launcher.RUNTIME.with_name(launcher.COMMIT + f".staging-{os.getpid()}")
    staging.mkdir(parents=True)
    source = staging / "source"
    source.mkdir()
    try:
        shutil.copyfile(archive, staging / "reviewed-source.tar")
        with tarfile.open(archive) as tar:
            tar.extractall(source, filter="data")
        expected = {f["path"]: f["sha256"] for f in manifest["files"]}
        actual = {
            str(p.relative_to(source))
            for p in source.rglob("*")
            if p.is_file() or p.is_symlink()
        }
        if actual != set(expected):
            raise RuntimeError("promoted source inventory mismatch")
        for relative, digest in expected.items():
            if hashlib.sha256((source / relative).read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"promoted source hash mismatch: {relative}")
        launcher.atomic_json(staging / "module-hashes.json", expected)
        shutil.copyfile(HERE / "launch.py", staging / "launch.py")
        shutil.copyfile(
            REVIEW / "independent-review.md", staging / "independent-review.md"
        )
        # Sync immutable bytes, directories, then publish this complete generation.
        for path in staging.rglob("*"):
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
                path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
        for path in sorted(
            (p for p in staging.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(fd)
            os.close(fd)
            path.chmod(0o555)
        os.replace(staging, launcher.RUNTIME)
        launcher.RUNTIME.chmod(0o555)
        fd = os.open(launcher.RUNTIME.parent, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(fd)
        os.close(fd)
    except BaseException:  # noqa: TRY203 - retain staging for forensic inspection
        raise
    return {
        "runtime": str(launcher.RUNTIME),
        "verified_files": len(expected),
        "archive_sha256": launcher.ARCHIVE_SHA256,
        "launcher_sha256": hashlib.sha256(
            (launcher.RUNTIME / "launch.py").read_bytes()
        ).hexdigest(),
    }


def desired_runner() -> dict:
    manifest = HERE.parents[1] / "migration/manifests/crank-beat/candybar.toml"
    return tomllib.loads(manifest.read_text())["candybar"]["runner"]


def cutover() -> dict:
    suffix = f"/candybars/{launcher.CANDYBAR_ID}"
    before = request("GET", suffix)
    original = request("GET", suffix + "/runner")
    if original != {
        "runner": {
            "kind": "hermes",
            "job_id": launcher.JOB_ID,
            "profile": launcher.PROFILE,
            "timeout_s": 1800,
        },
        "entrypoint": None,
    }:
        raise RuntimeError(
            "unexpected original runner; do not overwrite another owner's update"
        )
    tx = {
        "before": before,
        "original": original,
        "desired": desired_runner(),
        "status": "prepared",
    }
    launcher.atomic_json(TRANSACTION, tx)
    try:
        request("POST", suffix + "/enabled", {"enabled": False})
        # Bounded transaction drain only, not a recurring worker or clock.
        for _ in range(90):
            jobs = json.loads((launcher.HOME / "cron/jobs.json").read_text())["jobs"]
            if not next(j for j in jobs if j["id"] == launcher.JOB_ID).get(
                "fire_claim"
            ):
                break
            time.sleep(1)
        else:
            raise RuntimeError("existing crank attempt did not drain in 90s")
        request("PATCH", suffix, {"runner": tx["desired"]})
        readback = request("GET", suffix + "/runner")
        # The API canonicalizes env defaults; compare the actual runner models.
        from candybar.runners import canonical_runner

        if readback["runner"] != canonical_runner(tx["desired"]):
            raise RuntimeError("runner readback mismatch")
        request("POST", suffix + "/enabled", {"enabled": before["enabled"]})
        after = request("GET", suffix)
        for key in ("id", "name", "schedule", "timezone", "noise", "tags", "enabled"):
            if before[key] != after[key]:
                raise RuntimeError(f"unchanged definition field drifted: {key}")
        tx.update(status="activated", after=after, runner_readback=readback)
    except BaseException as exc:
        try:
            request("PATCH", suffix, {"runner": original["runner"]})
            request("POST", suffix + "/enabled", {"enabled": before["enabled"]})
            tx.update(status="rolled_back", rollback=request("GET", suffix + "/runner"))
        except Exception as rollback_error:  # noqa: BLE001 - persist rollback failure, preserve original exception
            tx.update(status="rollback_failed", rollback_error=str(rollback_error))
        tx["error"] = str(exc)
        launcher.atomic_json(TRANSACTION, tx)
        raise
    launcher.atomic_json(TRANSACTION, tx)
    return tx


if __name__ == "__main__":
    if sys.argv[1:] == ["promote"]:
        print(json.dumps(promote(), indent=2))
    elif sys.argv[1:] == ["cutover"]:
        print(json.dumps(cutover(), indent=2))
    else:
        raise SystemExit("usage: activate.py promote|cutover")
