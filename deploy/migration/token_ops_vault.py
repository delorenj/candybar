#!/usr/bin/env python3
"""Publish token checkpoints after externally scheduled Hermes execution; no clock."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path("/home/delorenj")
PROFILE = HOME / ".hermes/profiles/peter-griffin"
REPORTS = PROFILE / "reports/daily-token-ops"
CONFIG = HOME / ".config/delonet-daily-report/report.json"
HERMES = HOME / ".local/bin/hermes"
JOBS = {"report": "b240575fae0c", "action": "3684c5aa8eac"}
TZ = ZoneInfo("America/New_York")
OWNER = "<!-- token-ops-vault:v1 -->"


def read_json(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        tmp = Path(out.name)
        try:
            out.write(content.encode())
            out.flush()
            os.fsync(out.fileno())
            os.replace(tmp, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            tmp.unlink(missing_ok=True)


def vault_root(config=CONFIG):
    vault = read_json(config)["distribution"]["vault"]
    if not vault.get("enabled") or vault.get("layout") != "by-date":
        raise ValueError("daily-report vault must be enabled with by-date layout")
    root = Path(vault["path"]).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("configured vault destination is absent or not absolute")
    return root


def checked_hashes(root, hashes, required=()):
    if not isinstance(hashes, dict) or any(name not in hashes for name in required):
        raise ValueError(f"missing required integrity entries in {root}")
    for name, digest in hashes.items():
        path = root / name
        if (
            not path.resolve().is_relative_to(root.resolve())
            or not path.is_file()
            or sha(path) != digest
        ):
            raise ValueError(f"evidence integrity failure: {path}")
    return len(hashes)


def checkpoint(root, date, quarter):
    meta = read_json(root / "checkpoint.json")
    manifest = read_json(root / "manifest.json")
    if manifest.get("report_complete") is not True:
        raise ValueError(f"unfinished checkpoint: {root}")
    if meta["report_date"] != date or meta["quarter"] != quarter:
        raise ValueError(f"checkpoint identity mismatch: {root}")
    if manifest.get("report_date") != date or manifest.get("quarter") != quarter:
        raise ValueError(f"manifest identity mismatch: {root}")
    start = dt.datetime.combine(
        dt.date.fromisoformat(date), dt.time((quarter - 1) * 6), TZ
    )
    end = start + dt.timedelta(hours=6)
    bounds = meta["window"]
    if (bounds["start_epoch"], bounds["end_epoch"]) != (
        int(start.timestamp()),
        int(end.timestamp()),
    ):
        raise ValueError(f"non-calendar checkpoint bounds: {root}")
    if manifest["windows"]["window"] != bounds:
        raise ValueError(f"manifest bounds differ from checkpoint: {root}")
    checked_hashes(
        root, manifest.get("checksums_sha256"), ("report.md", "checkpoint.json")
    )
    return meta, manifest


def checked_action(path, date, quarter):
    receipt = read_json(path)
    status = str(receipt.get("status", "unknown"))
    complete = (
        receipt.get("action_complete") is True
        or status.startswith("completed")
        or status in ("blocked", "deferred", "verified", "no_change")
    )
    if not complete:
        raise ValueError(f"unfinished action receipt: {path}")
    if (
        receipt.get("report_date", date) != date
        or receipt.get("quarter", quarter) != quarter
    ):
        raise ValueError(f"action identity mismatch: {path}")
    checked_hashes(path.parent, receipt.get("checksums_sha256", {}))
    return receipt


def action_paths(root):
    paths = [root / "changes.json"] if (root / "changes.json").exists() else []
    return paths + sorted(root.glob("actions/*/changes.json"))


def render_day(date, reports=REPORTS):
    dt.date.fromisoformat(date)
    day = Path(reports) / date
    lines = [
        OWNER,
        f"# Token operations — {date}",
        "",
        "Six-hour checkpoints and action receipts, consolidated by covered New York date.",
        "Missing checkpoints are unavailable, never zero; daily totals are not sums of overlapping windows.",
        "The developer journal and its daily delivery remain separate and unchanged.",
        "",
    ]
    sources, quarters, actions = {}, [], []
    for quarter in range(1, 5):
        root = day / "checkpoints" / f"q{quarter}"
        lines += [f"## Q{quarter}", ""]
        if (root / "manifest.json").exists():
            meta, manifest = checkpoint(root, date, quarter)
            quarters.append(quarter)
            bounds = meta["window"]
            lines += [
                f"Window: {bounds['start_local']} to {bounds['end_local']} ({bounds['elapsed_hours']} elapsed hours).",
                f"Collected: {manifest['generated_at']}; {'degraded coverage' if manifest.get('degraded') else 'finalized report'}.",
                f"[Evidence and reproducible queries]({root.as_uri()}) · [Manifest]({(root / 'manifest.json').as_uri()})",
                "",
                (root / "report.md").read_text().strip(),
                "",
            ]
            sources[str(root / "manifest.json")] = sha(root / "manifest.json")
            sources.update(
                {
                    str(root / name): digest
                    for name, digest in manifest["checksums_sha256"].items()
                }
            )
        else:
            lines += ["Checkpoint report unavailable; no collection is implied.", ""]
        for path in action_paths(root):
            receipt = checked_action(path, date, quarter)
            status = str(receipt.get("status", "unknown"))
            sources[str(path)] = sha(path)
            sources.update(
                {
                    str(path.parent / name): digest
                    for name, digest in receipt.get("checksums_sha256", {}).items()
                }
            )
            actions.append(str(path))
            lines += [
                f"### Action — {status}",
                "",
                str(
                    receipt.get("summary")
                    or receipt.get("owner_dependency")
                    or "See the finalized action receipt."
                ),
                f"[Action evidence]({path.as_uri()})",
                "",
            ]
        if quarter == 4:
            for label, folder in [
                ("Daily trajectory", day / "trajectory"),
                ("Weekly wrap", Path(reports) / "weekly" / date),
            ]:
                if folder.is_dir():
                    lines += [f"[{label} evidence]({folder.as_uri()})", ""]
    if not quarters and not actions:
        raise ValueError(f"no finalized reports or action receipts for {date}")
    return "\n".join(lines).rstrip() + "\n", sources, quarters, actions


def publish(date, reports=REPORTS, vault=None, state=None):
    if dt.date.fromisoformat(date).isoformat() != date:
        raise ValueError("covered date must be canonical YYYY-MM-DD")
    root = Path(vault) if vault is not None else vault_root()
    state = Path(state) if state is not None else PROFILE / "reports/token-ops-delivery"
    state.mkdir(parents=True, exist_ok=True)
    with (state / f"{date}.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        markdown, sources, quarters, actions = render_day(date, reports)
        destination = root / date / "token-ops.md"
        if destination.exists() and not destination.read_text().startswith(OWNER):
            raise ValueError("refuse to overwrite an unowned vault note")
        changed = not destination.exists() or destination.read_text() != markdown
        before = sha(destination) if destination.exists() else None
        if changed:
            if before:
                history = state / "history" / date / f"{before}.md"
                if not history.exists():
                    atomic(history, destination.read_text())
            atomic(destination, markdown)
        digest = hashlib.sha256(markdown.encode()).hexdigest()
        if sha(destination) != digest:
            raise ValueError("vault readback hash differs")
        result = {
            "date": date,
            "destination": str(destination),
            "sha256": digest,
            "quarters": quarters,
            "actions": actions,
            "source_hashes": sources,
            "changed": changed,
            "source_artifacts_modified": False,
        }
        receipt = state / f"{date}.json"
        # A repeat is byte-for-byte idempotent, including its publication receipt.
        prior = read_json(receipt) if receipt.exists() else None
        identity = {k: v for k, v in result.items() if k != "changed"}
        if not prior or any(prior.get(k) != v for k, v in identity.items()):
            atomic(
                receipt,
                json.dumps(
                    {**identity, "published_at": dt.datetime.now(dt.UTC).isoformat()},
                    indent=2,
                )
                + "\n",
            )
        return result


def plan_now(now=None):
    spec = importlib.util.spec_from_file_location(
        "token_ops_checkpoint", PROFILE / "scripts/token_ops_checkpoint.py"
    )
    if spec is None or spec.loader is None:
        raise ValueError("checkpoint planner is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.plan(now or dt.datetime.now(dt.UTC))


def write_result(status, message, tokens):
    result = {"status": status, "message": message, "metrics": {"token_spend": tokens}}
    if os.environ.get("CANDYBAR_RESULT_FILE"):
        atomic(Path(os.environ["CANDYBAR_RESULT_FILE"]), json.dumps(result))
    print(json.dumps(result))


def run(stage):
    jobs = {j["id"]: j for j in read_json(PROFILE / "cron/jobs.json")["jobs"]}
    job = jobs[JOBS[stage]]
    if job.get("scheduler") != "external" or job.get("deliver") != "local":
        raise ValueError(
            "Hermes job must be external-pinned with local routine delivery"
        )
    plan = plan_now()
    env = os.environ.copy()
    # Only this wrapper writes the Candybar result, never a nested agent tool.
    env.pop("CANDYBAR_RESULT_FILE", None)
    with tempfile.TemporaryFile() as output:
        completed = subprocess.run(
            [
                str(HERMES),
                "-p",
                "peter-griffin",
                "cron",
                "run",
                JOBS[stage],
                "--wait",
                "--timeout",
                "3600",
            ],
            stdout=output,
            env=env,
            timeout=3660,
            check=False,
        )
        size = output.tell()
        output.seek(max(0, size - 65536))
        tail = output.read().decode(errors="replace")
    result = None
    for line in tail.splitlines():
        if line.startswith("cron-run-result: "):
            result = json.loads(line.removeprefix("cron-run-result: "))
    tokens = sum(
        max(0, int((result or {}).get(key) or 0))
        for key in ("input_tokens", "output_tokens")
    )
    if completed.returncode or not result or result.get("status") != "completed":
        write_result(
            "fail",
            "Hermes attempt did not complete; inspect the durable attempt",
            tokens,
        )
        return 1
    try:
        root = Path(plan["checkpoint_dir"])
        if stage == "report":
            _, manifest = checkpoint(root, plan["report_date"], plan["quarter"])
            if (
                dt.datetime.fromisoformat(manifest["generated_at"]).timestamp()
                < plan["window"]["end_epoch"]
            ):
                raise ValueError("report generation predates completed quarter")
        elif not action_paths(root):
            raise ValueError("matching checkpoint has no finalized action receipt")
        else:
            receipts = [
                checked_action(path, plan["report_date"], plan["quarter"])
                for path in action_paths(root)
            ]
            timestamps = [
                row.get("generated_at") or row.get("completed_at") for row in receipts
            ]
            if not any(
                stamp
                and dt.datetime.fromisoformat(stamp).timestamp()
                >= plan["window"]["end_epoch"]
                for stamp in timestamps
            ):
                raise ValueError("action receipt predates completed quarter")
        published = publish(plan["report_date"])
    except (OSError, ValueError, KeyError, TypeError):
        write_result(
            "fail",
            "Matching checkpoint publication failed; inspect local artifacts and vault receipt",
            tokens,
        )
        return 1
    write_result(
        "success",
        f"{stage}: verified vault publication {published['destination']}",
        tokens,
    )
    return 0


def notify_failure():
    invocation = os.environ.get("CANDYBAR_INVOCATION_ID", "manual")
    message = f"Token-ops execution or vault publication failed (Candybar invocation {invocation}). Routine reports remain local. Peter: inspect the attempt and reports/token-ops-delivery; no report completeness is implied."
    result = subprocess.run(
        [
            str(HERMES),
            "-p",
            "peter-griffin",
            "send",
            "--to",
            "telegram:7564050286",
            "--json",
            "--file",
            "-",
        ],
        input=message,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    try:
        response = json.loads(result.stdout)
        ok = (
            result.returncode == 0
            and response.get("success") is True
            and not response.get("skipped")
            and not response.get("error")
        )
    except (ValueError, AttributeError):
        ok = False
    atomic(
        PROFILE / "reports/token-ops-delivery/failures" / f"{invocation}.json",
        json.dumps(
            {
                "invocation_id": invocation,
                "delivery_confirmed": ok,
                "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
            }
        ),
    )
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("stage", choices=JOBS)
    pub = commands.add_parser("publish")
    pub.add_argument("--date", required=True)
    pub.add_argument("--reports", type=Path, default=REPORTS)
    pub.add_argument("--vault", type=Path)
    pub.add_argument("--state", type=Path)
    commands.add_parser("notify-failure")
    args = parser.parse_args()
    try:
        if args.command == "run":
            return run(args.stage)
        if args.command == "notify-failure":
            return notify_failure()
        print(
            json.dumps(
                publish(args.date, args.reports, args.vault, args.state), indent=2
            )
        )
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f"token-ops-vault: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
