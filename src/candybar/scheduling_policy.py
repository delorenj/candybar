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
import re
import subprocess
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


def crontabs(etc: Path = Path("/etc")) -> list[dict]:
    result = command(["crontab", "-l"])
    if result.returncode and not (result.returncode == 1 and "no crontab for" in result.stderr.lower()):
        raise ValueError("cannot inspect user crontab")
    rows = cron_rows(result.stdout, "user")
    for path in [etc / "crontab", *sorted((etc / "cron.d").iterdir())]:
        if path.is_file() and (path.name == "crontab" or re.fullmatch(r"[\w-]+", path.name)):
            rows.extend(cron_rows(path.read_text(), str(path), system=True))
    return rows


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
    return {"version": 1, "at": datetime.now(UTC).isoformat(), "sources": sources,
            "scope": "current-user and system timers; current-user plus /etc crontabs; all local Hermes profiles; optional n8n export",
            "not_audited": ["remote hosts", "other users' private crontabs", "n8n" if not n8n_export else "remote n8n drift since export"]}


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
    except (OSError, LedgerError, ValueError, TypeError, KeyError, urllib.error.URLError):
        return "unknown", "canonical rollback ledger/registry unavailable, malformed or migrating; inspect and retry doctor --live"


def audit(inventory: dict, exceptions: dict, *, base: str | None = None) -> dict:
    violations, approved, unknown = [], [], []
    counts = {}
    try:
        if not isinstance(inventory, dict) or inventory.get("version") != 1 or not isinstance(inventory.get("sources"), dict):
            raise ValueError("malformed inventory")
        sources = inventory["sources"]
        for name in sorted(REQUIRED - sources.keys()):
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
            if name not in REQUIRED | {"n8n"} or not isinstance(source, dict) or source.get("status") != "ok":
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
