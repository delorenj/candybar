#!/usr/bin/env python3
"""One Candybar job's source-image adapter; never a clock or fleet launcher."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

CANDYBAR_ID = "01a1793b-6dcd-730f-8317-5975681f9792"
JOB_ID = "777c511a009c"
PROFILE = "33god-pm"
COMMIT = "3509618461f7831f43ac0c53f219f35492861647"
ARCHIVE_SHA256 = "1bf0e82725e0e2e5faed430d25713f6b3c6582ff1e0c9253c5aba1ca9799e3cb"
HOME = Path("/home/delorenj/.hermes/profiles/33god-pm")
RUNTIME = HOME / "runtime/candybar/crank-beat" / COMMIT
SOURCE = RUNTIME / "source"
PYTHON = Path("/home/delorenj/.hermes/hermes-agent/.venv/bin/python")
MODULES = (
    "cron.scheduler",
    "cron.monitor",
    "cron.economics",
    "run_agent",
    "gateway.platforms.base",
    "gateway.run_notifications",
    "gateway.run_turn",
    "gateway.run_voice",
    "hermes_cli.main",
)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temp.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def snapshot() -> dict:
    doc = json.loads((HOME / "cron/jobs.json").read_text())
    job = next(j for j in doc["jobs"] if j["id"] == JOB_ID)
    alias = HOME / "cron/output" / JOB_ID / "monitor_last_output.txt"
    state = job.get("monitor_state", {})
    obj = alias.parent / "monitor_snapshots" / f"{state.get('last_output_hash')}.txt"
    return {
        "monitor_state": state,
        "scheduler": job.get("scheduler"),
        "economics": job.get("economics"),
        "no_agent": job.get("no_agent"),
        "alias_sha256": hashlib.sha256(alias.read_bytes()).hexdigest()
        if alias.exists()
        else None,
        "object_sha256": hashlib.sha256(obj.read_bytes()).hexdigest()
        if obj.exists()
        else None,
    }


def verify_modules(source: Path, modules: dict, expected: dict) -> dict:
    result = {}
    for name, module in modules.items():
        path = Path(module.__file__).resolve()
        relative = str(path.relative_to(source))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected[relative]:
            raise RuntimeError(f"reviewed module hash mismatch: {name}")
        result[name] = {"path": str(path), "sha256": digest}
    return result


def classify(events: list[dict]) -> str:
    if any(e["event"] == "economics_refusal" for e in events):
        return "refused_before_inference"
    if any(e["event"] == "agent_constructed" for e in events):
        return "agent_executed_check_terminal_receipt"
    return "no_agent_execution_not_attendance"


def worker(receipt_path: Path) -> int:
    import importlib

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SOURCE))
    # Main pre-parses profile before importing profile-sensitive modules.
    sys.argv = [
        "hermes",
        "-p",
        PROFILE,
        "cron",
        "run",
        JOB_ID,
        "--wait",
        "--timeout",
        "1800",
    ]
    main = importlib.import_module("hermes_cli.main")
    modules = {name: importlib.import_module(name) for name in MODULES}
    expected = json.loads((RUNTIME / "module-hashes.json").read_text())
    provenance = verify_modules(SOURCE, modules, expected)
    from hermes_constants import get_hermes_home

    if Path(get_hermes_home()).resolve() != HOME:
        raise RuntimeError("wrong active profile home")
    before = snapshot()
    if before["scheduler"] != "external" or before["no_agent"]:
        raise RuntimeError("crank must remain external agent mode")
    receipt = {
        "commit": COMMIT,
        "archive_sha256": ARCHIVE_SHA256,
        "invocation_id": os.environ["CANDYBAR_INVOCATION_ID"],
        "trigger": os.environ["CANDYBAR_TRIGGER"],
        "tick_id": os.environ["CANDYBAR_TICK_ID"],
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "interpreter": sys.executable,
        "hermes_home": str(get_hermes_home()),
        "modules": provenance,
        "started_at": datetime.now(UTC).isoformat(),
        "before": before,
        "events": [],
    }
    atomic_json(receipt_path, receipt)

    def observe(frame, event, arg):
        if frame.f_globals.get("__file__") != str(SOURCE / "cron/scheduler.py"):
            return
        name = frame.f_code.co_name
        if name == "run_one_job" and event == "call":
            job = frame.f_locals["job"]
            receipt["events"].append(
                {
                    "event": "run_one_job",
                    "pid": os.getpid(),
                    "job_id": job.get("id"),
                    "execution_id": job.get("execution_id"),
                }
            )
            atomic_json(receipt_path, receipt)
        elif name == "_economics_skip_result" and event == "call":
            admission = frame.f_locals["admission"]
            receipt["events"].append(
                {
                    "event": "economics_refusal",
                    "reason": admission.reason,
                    "alert": admission.alert,
                }
            )
            atomic_json(receipt_path, receipt)
        elif name == "_construct_cron_agent" and event == "return" and arg is not None:
            receipt["events"].append({"event": "agent_constructed", "pid": os.getpid()})
            atomic_json(receipt_path, receipt)
        elif name == "run_one_job" and event == "return":
            receipt["after_scheduler"] = snapshot()
            receipt["events"].append(
                {"event": "run_one_job_return", "processed": bool(arg)}
            )
            atomic_json(receipt_path, receipt)

    sys.setprofile(observe)
    threading.setprofile(observe)
    try:
        rc = main.main()
        return rc if isinstance(rc, int) else 0
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
        receipt["classification"] = classify(receipt["events"])
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        receipt["after"] = snapshot()
        atomic_json(receipt_path, receipt)


def resolve_cli(stdout: str, exit_code: int):
    # Import the SAME parser that the native Hermes runner uses, not a copy.
    from candybar.executor.hermes_runner import parse_result_line

    parsed = parse_result_line(stdout)
    if parsed is None:
        return {
            "status": "fail",
            "message": "Hermes produced no valid terminal result",
            "metrics": {"token_spend": 0},
        }, 1
    result = parsed.model_dump(mode="json", exclude_none=True)
    return result, exit_code if exit_code != 0 else (
        0 if parsed.status == "success" else 1
    )


def parent() -> int:
    import uuid

    if os.environ.get("CANDYBAR_ID") != CANDYBAR_ID:
        raise RuntimeError("launcher is scoped to existing crank Candybar id")
    invocation = str(uuid.UUID(os.environ["CANDYBAR_INVOCATION_ID"]))
    result_path = Path(os.environ["CANDYBAR_RESULT_FILE"])
    receipts = RUNTIME.parent / "receipts" / invocation
    receipts.mkdir(parents=True, exist_ok=False)
    script = Path(__file__).resolve()
    env = dict(os.environ)
    env["HERMES_HOME"] = str(HOME)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # This is the existing canonical interpreter, not a second Hermes install.
    argv = [
        str(PYTHON),
        "-I",
        "-B",
        str(script),
        "--worker",
        str(receipts / "worker.json"),
    ]
    before = {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "invocation_id": invocation,
        "argv": argv,
        "launcher_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "parser": "candybar.executor.hermes_runner.parse_result_line",
        "before": snapshot(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    atomic_json(receipts / "parent-start.json", before)
    proc = subprocess.run(
        argv, env=env, cwd=HOME, capture_output=True, text=True, check=False
    )
    (receipts / "stdout.txt").write_text(proc.stdout)
    (receipts / "stderr.txt").write_text(proc.stderr)
    result, rc = resolve_cli(proc.stdout, proc.returncode)
    atomic_json(result_path, result)
    atomic_json(
        receipts / "parent-result.json",
        {
            **before,
            "child_exit_code": proc.returncode,
            "exit_code": rc,
            "result": result,
            "after": snapshot(),
            "finished_at": datetime.now(UTC).isoformat(),
        },
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return rc


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--worker":
            rc = worker(Path(sys.argv[2]))
        elif len(sys.argv) == 1:
            rc = parent()
        else:
            raise RuntimeError("unexpected launcher arguments")
    except Exception as exc:
        if len(sys.argv) == 1 and os.environ.get("CANDYBAR_RESULT_FILE"):
            atomic_json(
                Path(os.environ["CANDYBAR_RESULT_FILE"]),
                {
                    "status": "fail",
                    "message": f"scoped crank adapter: {exc}",
                    "metrics": {"token_spend": 0},
                },
            )
        raise
    raise SystemExit(rc)
