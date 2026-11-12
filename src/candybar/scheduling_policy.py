"""Read-only scheduling policy: inventory clocks, never disable or migrate them.

Exit 0 = audited scope clean, 1 = bypass clock(s), 2 = unknown inventory.
Exceptions are exact identities with an accountable owner and reason. Registered
systemd services are execution, not clocks; a timer is never blessed by its service.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from candybar.migration.ledger import LedgerError, load_ledger

# Never honor CANDYBAR_LEDGER, inventory metadata or a policy-supplied ledger path
# as rollback evidence: this checkout owns the canonical migration record.
CANONICAL_LEDGER = Path(__file__).resolve().parents[2] / "deploy/migration/ledger.yaml"
# A --policy override may remove an exemption (making it a violation), but must
# never convert this protected application clock into an unconditional exemption.
ROLLBACK_BINDINGS = {
    "systemd:user:newapi-allowances.timer": {
        "ledger_id": "systemd:newapi-allowances", "ledger_status": "rolled-back",
        "candybar_id": "01a1793a-d2a0-7070-b267-4f6d3e41a3b5",
    },
}

REQUIRED = {"registry", "systemd-user", "systemd-system", "crontab", "hermes"}
# macOS has no systemd: launchd is the native scheduler. `REQUIRED` stays the
# Linux set; an inventory is Linux unless it says `"platform": "darwin"`.
DARWIN_REQUIRED = {"registry", "launchd", "crontab", "hermes"}
REQUIRED_BY_PLATFORM = {"linux": REQUIRED, "darwin": DARWIN_REQUIRED}
# launchd plist keys that make the OS fire a job on a schedule. WatchPaths,
# QueueDirectories, StartOnMount and KeepAlive are events and supervision, not
# clocks, and are never flagged.
LAUNCHD_CLOCK_KEYS = ("StartInterval", "StartCalendarInterval")
REMEDY = "not a Candybar; register the recurring job in Candybar and retire its native clock"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def command(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)


def json_command(argv: list[str]) -> list:
    result = command(argv)
    if result.returncode:
        raise ValueError("inventory command failed")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid inventory rows")
    return rows


def request(base: str, route: str):
    with urllib.request.urlopen(base.rstrip("/") + route, timeout=15) as response:
        return json.loads(response.read(4_000_001))


def timers(scope: str) -> list[dict]:
    prefix = ["systemctl", *(["--user"] if scope == "user" else [])]
    units = json_command([*prefix, "list-units", "--type=timer", "--all", "--output=json", "--no-pager"])
    files = json_command([*prefix, "list-unit-files", "--type=timer", "--output=json", "--no-pager"])
    rows = {}
    for unit in units:
        name = unit["unit"]
        active = unit["active"]
        if not name.endswith(".timer") or not isinstance(active, str):
            raise ValueError("invalid timer")
        rows[name] = {"id": f"systemd:{scope}:{name}", "active": active not in {"inactive", "failed"}, "enabled": False}
    for unit in files:
        name, state = unit["unit_file"], unit["state"]
        if not name.endswith(".timer") or not isinstance(state, str):
            raise ValueError("invalid timer file")
        row = rows.setdefault(name, {"id": f"systemd:{scope}:{name}", "active": False, "enabled": False})
        row["enabled"] = state in {"enabled", "enabled-runtime"}
    return list(rows.values())


def cron_rows(text: str, source: str, system: bool = False) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or re.match(r"[A-Za-z_][\w]*\s*=", line):
            continue
        fields = line.split()
        minimum = (3 if system else 2) if line.startswith("@") else (7 if system else 6)
        if len(fields) < minimum:
            raise ValueError("malformed crontab")
        # Never retain command text; it may contain credentials.
        canonical = " ".join(fields)
        rows.append({"id": f"crontab:{source}:{digest(canonical)}", "active": True})
    return rows


def crontabs(etc: Path = Path("/etc"), *, tolerate_missing_cron_d: bool = False) -> list[dict]:
    result = command(["crontab", "-l"])
    if result.returncode and not (result.returncode == 1 and "no crontab for" in result.stderr.lower()):
        raise ValueError("cannot inspect user crontab")
    rows = cron_rows(result.stdout, "user")
    # macOS ships neither /etc/crontab nor /etc/cron.d; on Linux a missing
    # cron.d stays an unreadable inventory (fail closed).
    cron_d = [] if tolerate_missing_cron_d and not (etc / "cron.d").is_dir() else sorted((etc / "cron.d").iterdir())
    for path in [etc / "crontab", *cron_d]:
        if path.is_file() and (path.name == "crontab" or re.fullmatch(r"[\w-]+", path.name)):
            rows.extend(cron_rows(path.read_text(), str(path), system=True))
    return rows


def _plist_display(path: Path) -> str:
    """`~/Library/...` for the user's own files, the absolute path otherwise (names only, never contents)."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return path.as_posix()


def scan_launchd_plists(dirs: list[Path]) -> tuple[dict[str, dict], list[dict]]:
    """(by label, per file) from plists in auto-loading launchd directories.

    Only labels and clock key names are kept: `ProgramArguments` and
    `EnvironmentVariables` routinely hold credentials and are never read into
    the inventory. One bad file never aborts the scan of the others:

    * a plist with no usable `Label` (an empty `<dict/>`, a non-dict root) cannot
      be loaded by launchd, which requires one, so it is a per-file entry keyed on
      its path. It counts as a clock only when it carries a clock key (it cannot be
      proven that launchd will never load it), and an empty one is just recorded.
    * a plist that cannot be read or parsed is a per-file entry marked `unreadable`:
      the audit reports it as unknown (exit 2) and still audits every other job.
    """
    rows: dict[str, dict] = {}
    files: list[dict] = []
    for directory in dirs:
        if not directory.is_dir():  # /Library/LaunchAgents is often absent
            continue
        for path in sorted(directory.glob("*.plist")):
            shown = _plist_display(path)
            try:
                with path.open("rb") as handle:
                    data = plistlib.load(handle)
            except Exception:  # noqa: BLE001 - plistlib raises AttributeError/TypeError on a malformed <date> and more;
                # whatever the reason, this file is unreadable, and one such file must not abort the scan of the others.
                files.append({"file": shown, "clocks": [], "unreadable": True})
                continue
            if not isinstance(data, dict) or not isinstance(data.get("Label"), str) or not data["Label"]:
                clocks = sorted(k for k in LAUNCHD_CLOCK_KEYS if k in data) if isinstance(data, dict) else []
                files.append({"file": shown, "clocks": clocks, "unreadable": False})
                continue
            row = rows.setdefault(data["Label"], {"clocks": [], "disabled": True})
            # Duplicate labels across directories: union the clocks (a clock in
            # either copy is a clock) and count it enabled if either copy is.
            row["clocks"] = sorted(set(row["clocks"]) | {k for k in LAUNCHD_CLOCK_KEYS if k in data})
            row["disabled"] = row["disabled"] and data.get("Disabled") is True
    return rows, files


def launchd_plists(dirs: list[Path]) -> dict[str, dict]:
    """Label -> {clocks, disabled}: the labelled half of `scan_launchd_plists`."""
    return scan_launchd_plists(dirs)[0]


_SERVICE_ROW = re.compile(r"^\s*(?:\d+|-)\s+(?:-?\d+|-)\s+(\S+)\s*$")
_DISABLED_ROW = re.compile(r'^\s*"([^"]+)"\s*=>\s*(\w+)\s*$')


def parse_launchctl_services(text: str) -> set[str]:
    """Labels from the `services = { PID Status Label }` block of `launchctl print <domain>`."""
    labels: set[str] = set()
    inside = False
    for line in text.splitlines():
        if not inside:
            inside = line.strip() == "services = {"
            continue
        if line.strip() == "}":
            break
        match = _SERVICE_ROW.match(line)
        if not match:
            raise ValueError("unrecognised launchctl services row")
        labels.add(match.group(1))
    if not inside:
        raise ValueError("launchctl print has no services block")
    return labels


def parse_launchctl_overrides(text: str) -> tuple[set[str], set[str]]:
    """(disabled, enabled) labels from `launchctl print-disabled <domain>` (`"label" => disabled|enabled`).

    An `enabled` override beats a plist's own `Disabled` key, so both halves matter.
    The legacy spellings are `true` (disabled) and `false` (enabled).
    """
    disabled: set[str] = set()
    enabled: set[str] = set()
    for line in text.splitlines():
        match = _DISABLED_ROW.match(line)
        if not match:
            continue
        value = match.group(2).lower()
        if value in {"disabled", "true"}:
            disabled.add(match.group(1))
        elif value in {"enabled", "false"}:
            enabled.add(match.group(1))
    return disabled, enabled


def parse_launchctl_disabled(text: str) -> set[str]:
    """Labels marked disabled by `launchctl print-disabled <domain>` (`"label" => disabled`)."""
    return parse_launchctl_overrides(text)[0]


def launchctl_clocks(text: str) -> list[str]:
    """Clock keys visible in `launchctl print <domain>/<label>` for a job with no plist on disk."""
    clocks = []
    if re.search(r"^\s*run interval = \d+ seconds", text, re.MULTILINE):
        clocks.append("StartInterval")
    if "com.apple.launchd.calendarinterval" in text:
        clocks.append("StartCalendarInterval")
    return clocks


# Labels owned by the OS and by app login items, hundreds of which are loaded in every
# domain and are runtime-generated or Apple-owned. They are only probed for clocks when a
# plist in a scanned directory names them; a loaded one with no such plist is a stated
# coverage boundary (`not_audited`), not an approval: nothing here blesses a clock.
LAUNCHD_SKIP_PROBE = ("com.apple.", "application.", "0x", "[", "UIKitApplication:")
LAUNCHD_NOT_PROBED = ("loaded jobs with no plist in the scanned directories whose label starts with "
                      + ", ".join(f"'{prefix}'" for prefix in LAUNCHD_SKIP_PROBE) + " (OS-owned or runtime-generated)")
LAUNCHD_SERVICE_GONE = 113  # `launchctl print` exit for "Could not find service"


def probe_launchd_service(run, domain: str, label: str) -> list[str] | None:
    """Clock keys of the live definition of one loaded job, or None if it unloaded since the listing.

    The live definition is what fires, and it can differ from the plist on disk (an edited
    file that was never booted out and re-bootstrapped), so loaded jobs are always probed.
    Only "service not found" is the benign race; any other failure is an unreadable inventory.
    """
    probe = run(["launchctl", "print", f"{domain}/{label}"])
    if probe.returncode:
        if probe.returncode == LAUNCHD_SERVICE_GONE or "could not find service" in probe.stderr.lower():
            return None
        raise ValueError("launchctl service probe failed")
    return launchctl_clocks(probe.stdout)


def launchd_jobs(dirs: list[Path] | None = None, uid: int | None = None, run=command) -> list[dict]:
    """Every launchd job that loads for this user, with the clock keys it carries.

    Sources, combined per label: plists in ~/Library/LaunchAgents, /Library/LaunchAgents
    and /Library/LaunchDaemons (loads at login/boot unless disabled), the loaded
    services of the `gui/<uid>`, `user/<uid>` and `system` domains, and the enable/disable
    overrides of all three (an `enabled` override beats a plist's `Disabled`). Every loaded
    job is probed with `launchctl print <domain>/<label>` and its live clock keys are unioned
    with the plist's. Read-only: only `print` and `print-disabled` are ever run.
    """
    uid = os.getuid() if uid is None else uid
    dirs = dirs if dirs is not None else [Path.home() / "Library/LaunchAgents", Path("/Library/LaunchAgents"),
                                          Path("/Library/LaunchDaemons")]
    plists, unlabeled = scan_launchd_plists(dirs)
    loaded: dict[str, list[str]] = {}
    disabled: set[str] = set()
    enabled: set[str] = set()
    for domain in (f"gui/{uid}", f"user/{uid}", "system"):
        printed = run(["launchctl", "print", domain])
        if printed.returncode:
            if "could not find domain" in printed.stderr.lower():
                continue  # no GUI login session: its plists load at next login
            raise ValueError("launchctl print failed")
        for label in parse_launchctl_services(printed.stdout):
            loaded.setdefault(label, []).append(domain)
        overrides = run(["launchctl", "print-disabled", domain])
        if overrides.returncode:
            raise ValueError("launchctl print-disabled failed")
        domain_disabled, domain_enabled = parse_launchctl_overrides(overrides.stdout)
        disabled |= domain_disabled
        enabled |= domain_enabled
    rows = {}
    for label, plist in plists.items():
        if label in enabled:
            off = False
        elif label in disabled:
            off = True
        else:
            off = plist["disabled"]
        rows[label] = {"id": f"launchd:{label}", "label": label, "clocks": plist["clocks"],
                       "active": bool(loaded.get(label)) or not off, "domains": sorted(loaded.get(label, []))}
    for label, domains in sorted(loaded.items()):
        if label.startswith(LAUNCHD_SKIP_PROBE) and label not in rows:
            continue
        live: set[str] = set()
        seen = False
        for domain in domains:
            clocks = probe_launchd_service(run, domain, label)
            if clocks is not None:
                seen = True
                live |= set(clocks)
        if label in rows:
            rows[label]["clocks"] = sorted(set(rows[label]["clocks"]) | live)
        elif seen:  # loaded with no plist on disk (`launchctl submit`, a bootstrap from elsewhere)
            rows[label] = {"id": f"launchd:{label}", "label": label, "clocks": sorted(live), "active": True,
                           "domains": sorted(domains)}
    for entry in unlabeled:
        # No Label means no label to match against `launchctl` and nothing to load by
        # name: a per-file row, active only if it carries a clock key.
        rows[f"file:{entry['file']}"] = {"id": f"launchd:file:{entry['file']}", "label": None, "file": entry["file"],
                                         "clocks": entry["clocks"], "active": bool(entry["clocks"]),
                                         "unreadable": entry["unreadable"], "domains": []}
    return sorted(rows.values(), key=lambda row: row["id"])


def hermes_jobs(home: Path) -> list[dict]:
    rows = []
    # Iterate profile directories each audit, not a frozen profile list. Never
    # read .env, prompts, stdout, tokens or auth. No store = no native jobs.
    profiles = home / "profiles"
    stores = [("default", home / "cron/jobs.json")]
    stores += [(p.name, p / "cron/jobs.json") for p in sorted(profiles.iterdir()) if p.is_dir()]
    for profile, path in stores:
        if not path.exists():
            continue
        raw = json.loads(path.read_text())
        jobs = raw.get("jobs") if isinstance(raw, dict) else raw
        if not isinstance(jobs, list):
            raise TypeError("malformed Hermes store")
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get("id"), str) or type(job.get("enabled")) is not bool:
                raise ValueError("malformed Hermes job")
            schedule = job.get("schedule")
            if not isinstance(schedule, dict) or schedule.get("kind") not in {"cron", "interval", "once"}:
                raise ValueError("unknown Hermes schedule")
            scheduler = job.get("scheduler") or "internal"
            if scheduler not in {"internal", "external"}:
                raise ValueError("unknown Hermes scheduler")
            rows.append({"id": f"hermes:{profile}:{job['id']}", "profile": profile, "job_id": job["id"],
                         "enabled": job["enabled"] and not bool(job.get("paused_at")),
                         "recurring": schedule["kind"] != "once", "scheduler": scheduler})
    return rows


def registry(base: str) -> list[dict]:
    definitions = request(base, "/candybars")
    if not isinstance(definitions, list):
        raise TypeError("registry is not a list")
    def enrich(row):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or type(row.get("enabled")) is not bool:
            raise ValueError("malformed definition")
        runner = request(base, f"/candybars/{row['id']}/runner")
        if not isinstance(runner, dict) or not {"runner", "entrypoint"} <= runner.keys():
            raise ValueError("malformed runner")
        stored = runner["runner"]
        if stored is not None and not isinstance(stored, dict):
            raise TypeError("invalid runner")
        if row["enabled"] and stored is not None and stored.get("kind") not in {"exec", "systemd", "hermes", "n8n"}:
            raise ValueError("unrecognized runner kind")
        if stored and stored.get("kind") == "hermes" and any(not isinstance(stored.get(k), str) for k in ("profile", "job_id")):
            raise ValueError("invalid Hermes runner identity")
        # Policy needs ownership identities, never argv, environment or tokens.
        safe = {k: v for k, v in (stored or {}).items() if k in {"kind", "unit", "profile", "job_id"}}
        return {"id": row["id"], "name": row["name"], "enabled": row["enabled"], "runner": safe}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(enrich, definitions))


def n8n_rows(path: Path) -> list[dict]:
    """Optional read-only n8n CLI/API export; no credentials/endpoints guessed."""
    workflows = json.loads(path.read_text())
    if isinstance(workflows, dict):
        workflows = workflows.get("data")
    if not isinstance(workflows, list):
        raise TypeError("invalid n8n export")
    rows = []
    for workflow in workflows:
        if not isinstance(workflow, dict) or type(workflow.get("active")) is not bool or not isinstance(workflow.get("nodes"), list):
            raise ValueError("invalid n8n workflow")
        for node in workflow["nodes"]:
            if not isinstance(node, dict) or not isinstance(node.get("type"), str):
                raise TypeError("invalid n8n node")
            if node["type"] in {"n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.cron"}:
                rows.append({"id": f"n8n:{workflow['id']}:{node['id']}",
                             "active": workflow["active"] and not bool(node.get("disabled"))})
    return rows


def collect(base: str, home: Path, n8n_export: Path | None = None) -> dict:
    sources = {}
    darwin = sys.platform == "darwin"
    if darwin:
        probes = {"registry": lambda: registry(base), "launchd": launchd_jobs,
                  "crontab": lambda: crontabs(tolerate_missing_cron_d=True), "hermes": lambda: hermes_jobs(home)}
    else:
        probes = {"registry": lambda: registry(base), "systemd-user": lambda: timers("user"),
                  "systemd-system": lambda: timers("system"), "crontab": crontabs, "hermes": lambda: hermes_jobs(home)}
    if n8n_export:
        probes["n8n"] = lambda: n8n_rows(n8n_export)
    with ThreadPoolExecutor(max_workers=6) as pool:
        def probe(item):
            name, function = item
            try:
                return name, {"status": "ok", "rows": function()}
            except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
                return name, {"status": "unknown", "error": type(exc).__name__}
        sources.update(pool.map(probe, probes.items()))
    inventory = {"version": 1, "at": datetime.now(UTC).isoformat(), "sources": sources,
                 "scope": "current-user and system timers; current-user plus /etc crontabs; all local Hermes profiles; optional n8n export",
                 "not_audited": ["remote hosts", "other users' private crontabs", "n8n" if not n8n_export else "remote n8n drift since export"]}
    if darwin:
        # Absent `platform` means Linux, so Linux inventories keep their exact shape.
        inventory["platform"] = "darwin"
        inventory["scope"] = ("launchd plists in ~/Library/LaunchAgents, /Library/LaunchAgents and /Library/LaunchDaemons "
                              "plus the loaded jobs and enable/disable overrides of gui/<uid>, user/<uid> and system; "
                              "current-user crontab; all local Hermes profiles; optional n8n export")
        inventory["not_audited"] = ["remote hosts", "other users' private crontabs", LAUNCHD_NOT_PROBED,
                                    "jobs only loaded into other users' domains", "at(1) jobs",
                                    "n8n" if not n8n_export else "remote n8n drift since export"]
    return inventory


def load_policy(path: Path) -> dict[str, dict]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("exceptions"), list):
        raise ValueError("invalid scheduling policy")
    exceptions = {}
    for entry in raw["exceptions"]:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str) or not entry[k].strip() for k in ("id", "owner", "reason")):
            raise ValueError("exception requires exact id, owner and reason")
        if entry["id"] in exceptions or any(c in entry["id"] for c in "*?["):
            raise ValueError("duplicate or wildcard scheduling exception")
        if (any(key in entry for key in ("ledger_id", "ledger_status", "candybar_id"))
                and (not all(isinstance(entry.get(key), str) and entry[key].strip()
                             for key in ("ledger_id", "ledger_status", "candybar_id"))
                     or entry["ledger_status"] != "rolled-back"
                     or not entry["id"].startswith("systemd:user:")
                     or not entry["id"].endswith(".timer"))):
            raise ValueError("invalid rollback exception metadata")
        binding = ROLLBACK_BINDINGS.get(entry["id"], {})
        if any(entry.get(key) != value for key, value in binding.items()):
            raise ValueError("protected rollback exception requires exact canonical identities")
        exceptions[entry["id"]] = entry
    return exceptions


def rollback_evidence(exception: dict, registry_source: dict, base: str | None) -> tuple[str, str]:
    """Verify exact native/API ownership, serialized with migration mutations.

    Offline inventories supply registry observations, not ledger authority. Live
    audits refresh registry inside the ledger directory's shared lock so an
    in-progress migrate cannot be certified from mixed before/after snapshots.
    """
    try:
        fd = os.open(CANONICAL_LEDGER.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            source = {"status": "ok", "rows": registry(base)} if base is not None else registry_source
            if not isinstance(source, dict) or source.get("status") != "ok" or not isinstance(source.get("rows"), list):
                return "unknown", "registry unavailable; rerun doctor --live before accepting rollback"
            rows = source["rows"]
            if any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                   or type(row.get("enabled")) is not bool or not isinstance(row.get("runner"), dict) for row in rows):
                return "unknown", "registry malformed; rerun doctor --live before accepting rollback"
            owners = [row for row in rows if row["id"] == exception["candybar_id"]]
            if len(owners) != 1:
                return "unknown", "exact Candybar registration absent/ambiguous; inspect registry and retry"
            if owners[0]["enabled"]:
                return "fail", "duplicate native/Candybar clocks: disable exact Candybar registration via migrate rollback and verify"
            ledger = load_ledger(CANONICAL_LEDGER)
            entry = ledger.entry(exception["ledger_id"])
            native = entry.native
            if (entry.candybar_id != exception["candybar_id"] or native.kind != "systemd-timer"
                    or exception["id"] != f"systemd:user:{native.unit}"
                    or owners[0]["runner"] != entry.candybar.get("runner")):
                return "unknown", "rollback identity mismatch; inspect canonical ledger and exact API registration"
            if entry.disable_pending:
                return "unknown", "rollback disable_pending=true; restore API access, rerun migrate rollback, then doctor --live"
            if entry.status != "rolled-back":
                return "fail", "canonical ledger is not rolled-back; native clock must not be exempted"
            return "pass", "canonical rolled-back ledger and exact disabled registration verified"
        finally:
            os.close(fd)
    except BlockingIOError:
        return "unknown", "canonical migration directory lock held; inspect migrate/autosync owner and retry doctor --live"
    except (OSError, LedgerError, ValueError, TypeError, KeyError, urllib.error.URLError):
        return "unknown", "canonical rollback ledger/registry unavailable or malformed; inspect and retry doctor --live"


def audit(inventory: dict, exceptions: dict, *, base: str | None = None) -> dict:
    violations, approved, unknown = [], [], []
    counts = {}
    try:
        if not isinstance(inventory, dict) or inventory.get("version") != 1 or not isinstance(inventory.get("sources"), dict):
            raise ValueError("malformed inventory")
        sources = inventory["sources"]
        required = REQUIRED_BY_PLATFORM.get(inventory.get("platform", "linux"))
        if required is None:
            raise ValueError("unknown inventory platform")
        for name in sorted(required - sources.keys()):
            unknown.append(f"{name}: missing inventory")
        registry_rows = sources.get("registry", {}).get("rows", [])
        external = set()
        for row in registry_rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or type(row.get("enabled")) is not bool or not isinstance(row.get("runner"), dict):
                raise ValueError("malformed registry row")
            runner = row.get("runner") or {}
            if row["enabled"] and runner.get("kind") == "hermes":
                external.add((runner["profile"], runner["job_id"]))
        for name, source in sources.items():
            if name not in required | {"n8n"} or not isinstance(source, dict) or source.get("status") != "ok":
                unknown.append(f"{name}: unavailable inventory")
                continue
            rows = source.get("rows")
            if not isinstance(rows, list):
                raise TypeError("malformed inventory rows")
            counts[name] = len(rows)
            if name == "registry":
                continue
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    raise TypeError("malformed scheduler row")
                key = row["id"]
                if name == "hermes":
                    if any(type(row.get(k)) is not bool for k in ("enabled", "recurring")) or row.get("scheduler") not in {"internal", "external"}:
                        raise ValueError("malformed Hermes row")
                    active = row["enabled"] and row["recurring"]
                    if active and row["scheduler"] == "external":
                        if (row["profile"], row["job_id"]) not in external:
                            unknown.append(f"{key}: external execution has no enabled Candybar owner")
                        continue
                elif name.startswith("systemd-"):
                    if any(type(row.get(k)) is not bool for k in ("active", "enabled")):
                        raise ValueError("malformed timer row")
                    active = row["active"] or row["enabled"]
                elif name == "launchd":
                    clocks = row.get("clocks")
                    if type(row.get("active")) is not bool or not isinstance(clocks, list) or any(not isinstance(c, str) for c in clocks):
                        raise ValueError("malformed launchd row")
                    if row.get("unreadable") is True:
                        # One corrupt plist must not hide the rest of the inventory, but
                        # it also cannot be certified clock-free: unknown, never green.
                        unknown.append(f"{key}: plist unreadable; cannot certify it carries no clock")
                        continue
                    # A loaded or auto-loading job with no StartInterval/StartCalendarInterval
                    # is supervision or an event trigger, not a clock.
                    active = row["active"] and bool(clocks)
                else:
                    if type(row.get("active")) is not bool:
                        raise ValueError("malformed schedule row")
                    active = row["active"]
                if not active:
                    continue
                if key in exceptions:
                    exception = exceptions[key]
                    if key in ROLLBACK_BINDINGS or "ledger_id" in exception:
                        binding = ROLLBACK_BINDINGS.get(key, {})
                        if any(exception.get(k) != v for k, v in binding.items()):
                            unknown.append(f"{key}: protected rollback identity mismatch; inspect policy")
                            continue
                        status, detail = rollback_evidence(exception, sources.get("registry", {}), base)
                        if status == "unknown":
                            unknown.append(f"{key}: {detail}")
                        elif status == "fail":
                            violations.append({"id": key, "message": detail})
                        else:
                            approved.append({**exception, "evidence": detail})
                    else:
                        approved.append(exception)
                else:
                    violations.append({"id": key, "message": REMEDY})
    except (ValueError, TypeError, KeyError, AttributeError):
        unknown.append("inventory: malformed; cannot certify scheduling ownership")
    status = "unknown" if unknown else "fail" if violations else "pass"
    return {"status": status, "violations": violations, "unknown": unknown, "approved": approved,
            "counts": counts, "not_audited": inventory.get("not_audited", []) if isinstance(inventory, dict) else []}


def repo_lint(root: Path, exceptions: dict) -> dict:
    violations, unknown, approved = [], [], []
    try:
        files = command(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"])
        if files.returncode:
            raise ValueError("not a readable git repository")
        for relative in sorted(set(files.stdout.split("\0")) - {""}):
            if not relative.endswith(".timer"):
                continue
            path = root / relative
            if not path.is_file():
                continue  # tracked deletion is a retired timer
            content = path.read_text()
            if "[Timer]" not in content:
                unknown.append(f"{relative}: malformed timer")
                continue
            key = f"repo:{relative}"
            if key in exceptions:
                approved.append(exceptions[key])
            else:
                violations.append({"id": key, "message": REMEDY})
    except (OSError, ValueError, subprocess.SubprocessError):
        unknown.append("repo: inventory unavailable")
    return {"status": "unknown" if unknown else "fail" if violations else "pass", "violations": violations,
            "unknown": unknown, "approved": approved}


def exit_code(report: dict) -> int:
    return {"pass": 0, "fail": 1, "unknown": 2}[report["status"]]
