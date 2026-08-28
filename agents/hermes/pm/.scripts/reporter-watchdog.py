#!/usr/bin/env python3
"""Gateway-independent health watchdog for a Hermes reporter profile.

Defaults target the DeLoNET company reporter; override the REPORTER_* environment
variables below to point it at another reporter deployment.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

# Deployment-specific constants. The defaults describe the DeLoNET company
# reporter this watchdog was written for; every one is overridable so a second
# reporter does not need a forked copy of this file.
PROFILE_NAME = os.environ.get("REPORTER_PROFILE_NAME", "delonet-company-reporter")
REPORT_SLUG = os.environ.get("REPORTER_REPORT_SLUG", "delonet-daily-report")
JOB_PREFIX = os.environ.get("REPORTER_JOB_PREFIX", "ddr")
EXPECTED_JOBS = {
    job.strip()
    for job in os.environ.get(
        "REPORTER_EXPECTED_JOBS",
        f"{JOB_PREFIX}:daily,"
        f"{JOB_PREFIX}:journal:hermes-fleet-health,"
        f"{JOB_PREFIX}:journal:nightly-pr-maintenance,"
        f"{JOB_PREFIX}:journal:report-delivery-health",
    ).split(",")
    if job.strip()
}
REPORT_TIMEZONE = os.environ.get("REPORTER_TIMEZONE", "America/New_York")
NTFY_URL = os.environ.get("REPORTER_NTFY_URL", "https://ntfy.delo.sh/bloodbank")
NTFY_TOKEN_REF = os.environ.get(
    "REPORTER_NTFY_TOKEN_REF", "op://DeLoSecrets/ntfy/add more/accessToken"
)
HOME = Path.home()
PROFILE = HOME / ".hermes" / "profiles" / PROFILE_NAME
CONFIG = HOME / ".config" / REPORT_SLUG / "report.json"
STATE_DIR = HOME / ".local" / "state" / REPORT_SLUG / "watchdog"
STATUS_PATH = STATE_DIR / "status.json"
ALERT_PATH = STATE_DIR / "last-alert.json"
TICK_MAX_AGE_SECONDS = 180


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def gateway_homes() -> dict[str, list[str]]:
    homes: dict[str, list[str]] = {}
    for unit in (HOME / ".config" / "systemd" / "user").glob("hermes*gateway.service"):
        text = unit.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^Environment=HERMES_HOME=(.+)$", text, re.MULTILINE)
        if not match:
            continue
        raw = match.group(1).strip().strip('"')
        resolved = str(Path(os.path.expandvars(raw)).expanduser().resolve(strict=False))
        homes.setdefault(resolved, []).append(unit.name)
    return homes


def alert_ntfy(codes: list[str]) -> str | None:
    try:
        token = subprocess.run(
            ["op", "read", NTFY_TOKEN_REF],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        if not token:
            return "ntfy access token unavailable"
        body = (
            f"Critical {PROFILE_NAME} health failure: "
            + ", ".join(sorted(codes))
            + ". Inspect the profile-scoped watchdog status."
        ).encode()
        request = urllib.request.Request(
            NTFY_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Title": f"{PROFILE_NAME} critical",
                "Priority": "urgent",
                "Tags": "rotating_light",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            if not 200 <= response.status < 300:
                return f"ntfy returned HTTP {response.status}"
    except Exception as exc:  # fail open; status captures the alert failure
        return f"ntfy alert failed: {type(exc).__name__}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-alert", action="store_true")
    parser.add_argument("--tick-max-age", type=int, default=TICK_MAX_AGE_SECONDS)
    args = parser.parse_args()

    now = dt.datetime.now(dt.UTC)
    eastern = now.astimezone(ZoneInfo(REPORT_TIMEZONE))
    critical: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    target = PROFILE.resolve(strict=False)
    if not PROFILE.is_symlink():
        critical.append({"code": "profile_not_symlinked", "detail": str(PROFILE)})

    jobs_path = PROFILE / "cron" / "jobs.json"
    try:
        raw_jobs = load_json(jobs_path)
        jobs = raw_jobs.get("jobs", []) if isinstance(raw_jobs, dict) else []
    except (OSError, json.JSONDecodeError):
        jobs = []
        critical.append({"code": "jobs_store_unreadable", "detail": str(jobs_path)})

    names = [job.get("name") for job in jobs if isinstance(job, dict)]
    duplicates = sorted({name for name in names if name and names.count(name) > 1})
    missing = sorted(EXPECTED_JOBS - set(names))
    unexpected = sorted({name for name in names if name and name.startswith(f"{JOB_PREFIX}:")} - EXPECTED_JOBS)
    if duplicates:
        critical.append({"code": "duplicate_managed_jobs", "detail": ",".join(duplicates)})
    if missing:
        critical.append({"code": "missing_managed_jobs", "detail": ",".join(missing)})
    if unexpected:
        warnings.append({"code": "unexpected_managed_jobs", "detail": ",".join(unexpected)})
    paused = sorted(
        job.get("name")
        for job in jobs
        if isinstance(job, dict)
        and job.get("name") in EXPECTED_JOBS
        and not job.get("enabled", False)
    )
    if paused:
        warnings.append({"code": "managed_jobs_paused", "detail": ",".join(paused)})

    tick_lock = PROFILE / "cron" / ".tick.lock"
    tick_age = None
    if tick_lock.exists():
        tick_age = max(0, int(now.timestamp() - tick_lock.stat().st_mtime))
        if tick_age > args.tick_max_age:
            critical.append({"code": "stale_cron_tick", "detail": f"age_seconds={tick_age}"})
    else:
        critical.append({"code": "missing_cron_tick", "detail": str(tick_lock)})

    daily = next((job for job in jobs if isinstance(job, dict) and job.get("name") == f"{JOB_PREFIX}:daily"), {})
    state = str(daily.get("last_status") or daily.get("status") or "").lower()
    if state in {"failed", "error"}:
        critical.append({"code": "daily_job_failed", "detail": state})

    try:
        config = load_json(CONFIG)
    except (OSError, json.JSONDecodeError):
        config = {}
        critical.append({"code": "config_unreadable", "detail": str(CONFIG)})
    daily_config = (
        config.get("daily", {}) if isinstance(config, dict) and isinstance(config.get("daily"), dict) else {}
    )
    deliver = (
        daily_config.get("deliver")
    )
    if deliver != "telegram":
        warnings.append({"code": "telegram_delivery_not_activated", "detail": str(deliver)})

    archive_dir = Path(config.get("archive_dir", "")) if isinstance(config, dict) else Path()
    current = archive_dir / eastern.strftime("%Y/%m/%Y-%m-%d/current.json")
    if daily_config.get("enabled") and (eastern.hour, eastern.minute) >= (8, 15) and not current.is_file():
        critical.append({"code": "daily_report_missed", "detail": str(current)})

    homes = gateway_homes()
    profile_units = sorted(homes.get(str(target), []))
    if len(profile_units) > 1:
        critical.append({"code": "reporter_gateway_duplicated", "detail": ",".join(profile_units)})
    for home, units in sorted(homes.items()):
        if len(units) > 1 and home != str(target):
            warnings.append(
                {"code": "fleet_gateway_home_duplicated", "detail": f"{home}: {','.join(sorted(units))}"}
            )

    codes = sorted(item["code"] for item in critical)
    fingerprint = hashlib.sha256("\n".join(codes).encode()).hexdigest() if codes else ""
    previous = {}
    try:
        previous = load_json(ALERT_PATH)
    except (OSError, json.JSONDecodeError):
        pass
    alert_error = None
    if codes and fingerprint != previous.get("fingerprint") and not args.no_alert:
        alert_error = alert_ntfy(codes)
        if alert_error is None:
            atomic_json(ALERT_PATH, {"fingerprint": fingerprint, "sent_at": now.isoformat()})
    elif not codes and previous.get("fingerprint"):
        atomic_json(ALERT_PATH, {"fingerprint": "", "cleared_at": now.isoformat()})

    status = {
        "checked_at": now.isoformat(),
        "profile": PROFILE_NAME,
        "profile_home": str(PROFILE),
        "profile_target": str(target),
        "healthy": not critical,
        "critical": critical,
        "warnings": warnings,
        "tick_age_seconds": tick_age,
        "managed_jobs": sorted(name for name in names if name and name.startswith(f"{JOB_PREFIX}:")),
        "delivery": deliver,
        "alert_error": alert_error,
    }
    atomic_json(STATUS_PATH, status)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 1 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
