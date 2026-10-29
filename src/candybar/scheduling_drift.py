"""Candybar-owned scheduling drift job; stdout contains only condition transitions."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from candybar import scheduling_policy as policy
from candybar.transition_exec import atomic


def transition(report: dict, previous: dict) -> tuple[dict, list[str]]:
    current = {row["id"] for row in report["violations"]}
    current.update("unknown:" + item for item in report["unknown"])
    old = previous.get("conditions", [])
    if not isinstance(old, list) or any(not isinstance(k, str) for k in old):
        raise ValueError("malformed drift transition state")
    old = set(old)
    # Unknown source must never declare its previous clocks recovered.
    held = set()
    if report["status"] == "unknown":
        unavailable = {item.split(":", 1)[0].removeprefix("unknown:") for item in report["unknown"]}
        if "inventory" in unavailable or "policy/inventory unavailable" in report["unknown"]:
            held = old - current
        else:
            prefixes = {"systemd-user": "systemd:user:", "systemd-system": "systemd:system:",
                        "crontab": "crontab:", "hermes": "hermes:", "n8n": "n8n:"}
            held = {key for key in old - current if any(key.startswith(prefixes.get(name, "unknown:" + name))
                                                        for name in unavailable)
                    or any(item.startswith(key + ":") for item in report["unknown"])}
    current |= held
    introduced, cleared = current - old, old - current
    messages = []
    if introduced or cleared:
        new_bypass = sum(not key.startswith("unknown:") for key in introduced)
        recovered_bypass = sum(not key.startswith("unknown:") for key in cleared)
        new_unknown = sum(key.startswith("unknown:") for key in introduced)
        resolved_unknown = sum(key.startswith("unknown:") for key in cleared)
        messages.append("SCHEDULING POLICY transition: "
                        f"{new_bypass} new bypass, {recovered_bypass} cleared; "
                        f"{new_unknown} new unknown, {resolved_unknown} resolved. "
                        "Inspect Candybar scheduling-policy-drift report and run candybar doctor --live. "
                        "Do not bulk-disable or migrate existing clocks.")
    return {"conditions": sorted(current)}, messages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--inventory", type=Path, help="offline fixture; no live calls")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        exceptions = policy.load_policy(args.policy)
        base = os.environ.get("CANDYBAR_API_URL", "http://127.0.0.1:8700")
        inventory = json.loads(args.inventory.read_text()) if args.inventory else policy.collect(base, Path.home() / ".hermes")
        report = policy.audit(inventory, exceptions, base=None if args.inventory else base)
    except (OSError, ValueError, TypeError):
        report = {"status": "unknown", "violations": [], "unknown": ["policy/inventory unavailable"]}
    previous = json.loads(args.state_file.read_text()) if args.state_file.exists() else {"conditions": []}
    updated, messages = transition(report, previous)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    atomic(args.report, json.dumps(report, indent=2).encode())
    atomic(args.state_file, json.dumps(updated).encode())
    for message in messages:
        print(message)
    # Invocation success means the read-only audit executed, not that all clocks
    # complied. Doctor's report/CLI retains fail/unknown; stdout alerts on change.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
