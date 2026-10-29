"""Run a stdout-transition detector without losing its state on delivery failure.

The detector must accept --state-file and emit only new/recovered conditions.
State is staged under the real detector's lock; only successful delivery commits
it. No inference, scheduler, credentials or platform transport lives here.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

MAX_OUTPUT = 65536


def bounded(argv: list[str], timeout: float, body: str | None = None) -> tuple[int, str]:
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE if body is not None else subprocess.DEVNULL,
                                 stdout=out, stderr=err, start_new_session=True)
        try:
            child.communicate(None if body is None else body.encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise RuntimeError("command timed out") from None
        out.seek(0)
        raw = out.read(MAX_OUTPUT + 1)
        if len(raw) > MAX_OUTPUT:
            raise RuntimeError("command exceeded output limit")
        return child.returncode, raw.decode("utf-8", errors="strict")


def atomic(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        tmp = Path(out.name)
        try:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def notify(profile: str, target: str, message: str, timeout: float) -> None:
    rc, output = bounded([str(Path.home() / ".local/bin/hermes"), "-p", profile,
                          "send", "--to", target, "--json", "--file", "-"], timeout, message)
    try:
        result = json.loads(output)
        ok = rc == 0 and result.get("success") is True and not result.get("skipped") and not result.get("error")
    except (ValueError, AttributeError):
        ok = False
    if not ok:
        # Do not persist transport output: it can contain credentials or message bodies.
        raise RuntimeError("notification was not confirmed")


def execute(command: list[str], state: Path, profile: str, target: str,
            timeout: float = 90, notify_timeout: float = 45, sender=notify) -> dict:
    state = state.expanduser()
    state.parent.mkdir(parents=True, exist_ok=True)
    operational = state.with_name(state.name + ".delivery-health")
    with open(str(state) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix=".transition-", dir=state.parent) as directory:
            staged = Path(directory) / "state.json"
            if state.exists():
                staged.write_bytes(state.read_bytes())
            try:
                rc, output = bounded([*command, "--state-file", str(staged)], timeout)
                if rc:
                    raise RuntimeError(f"detector exited {rc}")
                candidate = json.loads(staged.read_text())
                if not isinstance(candidate, dict):
                    raise TypeError("detector state must be an object")
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                if not operational.exists():
                    sender(profile, target, "CANDYBAR TRANSITION TOOL FAILURE: detector execution unavailable; inspect Candybar invocation. No inference attempted.", notify_timeout)
                    atomic(operational, b"failed\n")
                raise RuntimeError("detector execution failed; transition state preserved") from exc
            message = output.strip()
            if operational.exists():
                message = "\n".join(filter(None, [message, "CANDYBAR TRANSITION RECOVERED: detector execution available."]))
            if message:
                sender(profile, target, message, notify_timeout)
            atomic(state, staged.read_bytes())
            operational.unlink(missing_ok=True)
            return {"status": "success", "message": "transitions delivered" if message else "silent: no transitions",
                    "token_spend": 0, "delivery": "confirmed" if message else "silent"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--notify-timeout", type=float, default=45)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or "--state-file" in command or args.timeout <= 0 or args.notify_timeout <= 0:
        parser.error("detector command required; wrapper owns --state-file; timeouts must be positive")
    try:
        result = execute(command, args.state_file, args.profile, args.target, args.timeout, args.notify_timeout)
        code = 0
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        result = {"status": "fail", "message": str(exc) if isinstance(exc, RuntimeError) else "transition execution failed", "token_spend": 0}
        code = 1
    invocation_id = os.environ.get("CANDYBAR_INVOCATION_ID")
    if invocation_id:
        receipt = {**result, "invocation_id": invocation_id,
                   "trigger": os.environ.get("CANDYBAR_TRIGGER")}
        atomic(args.state_file.with_name(args.state_file.name + ".last-run.json"),
               json.dumps(receipt).encode())
    result_file = os.environ.get("CANDYBAR_RESULT_FILE")
    if result_file:
        Path(result_file).write_text(json.dumps({k: v for k, v in result.items() if k != "delivery"}))
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
