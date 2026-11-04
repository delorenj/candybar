"""The steward's context bundle and working directory (steward-contract.md section 4, steps 1-2).

Everything an agent needs to reason about one candybar, gathered on the
host: the definition and runner, its migration-ledger entry and manifest,
the unit text (`systemd`), the Hermes job record (`hermes`), the argv / unit
script files (`exec`, `systemd`), the last 30 invocations and the stored
profile. Then the directory the OpenCode session runs in:

1. the ledger `source_of_truth.repo`'s local checkout (`~/code/<name>`,
   `~/code/*/<name>`, or the git root of a path the ledger entry names),
   verified to exist and, when it has an `origin`, to point at that repo;
2. else an exec runner's `workdir` / argv path's git root (or a bare
   entrypoint's);
3. else a hermes job's `workdir`;
4. else this candybar checkout.

Blocking file and git reads; `build_context` runs them off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..migration.ledger import LedgerError, default_ledger_path, load_ledger

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
CODE_ROOT_ENV = "CANDYBAR_CODE_ROOT"
HERMES_ROOT_ENV = "CANDYBAR_HERMES_HOME"
# The 33god-pm runtime keeps its own jobs file (the profile dir links to it).
PM_RUNTIME_JOBS = Path.home() / "code" / "33GOD" / "agents" / "hermes" / "pm" / "runtime" / "cron" / "jobs.json"

FILE_CAP = 12_000  # chars of one source file in the prompt
UNIT_CAP = 12_000
MAX_FILES = 6
MAX_FILE_BYTES = 512_000
INVOCATIONS = 30

_SLUG_RE = re.compile(r"[:/]([^/:\s]+/[^/\s]+?)(?:\.git)?/?$")


def code_root() -> Path:
    return Path(os.environ.get(CODE_ROOT_ENV) or Path.home() / "code").expanduser()


def hermes_home() -> Path:
    return Path(os.environ.get(HERMES_ROOT_ENV) or Path.home() / ".hermes").expanduser()


def git_root(path: Path) -> Path | None:
    """The nearest ancestor (or `path`) holding a `.git` entry, or None."""
    try:
        current = path if path.is_dir() else path.parent
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def origin_slug(root: Path) -> str | None:
    """`owner/name` of the checkout's `origin` remote, or None."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _SLUG_RE.search(out.stdout.strip()) if out.returncode == 0 else None
    return match.group(1) if match else None


def _absolute_paths(texts: list[str]) -> list[Path]:
    out: list[Path] = []
    for text in texts:
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        for token in tokens:
            token = token.split("=", 1)[1] if token.startswith("--") and "=" in token else token
            if token.startswith(("/", "~/")):
                out.append(Path(token).expanduser())
    return out


def find_checkout(slug: str, hints: list[Path] = ()) -> Path | None:  # type: ignore[assignment]
    """The local checkout of `owner/name`: by name under the code root, or a hinted git root.

    A candidate with an `origin` must point at `slug` (case-insensitive); one
    without a readable origin is accepted on its name alone.
    """
    name = slug.rsplit("/", 1)[-1]
    wanted = slug.lower()
    root = code_root()
    candidates: list[Path] = []
    for parent in (root, *sorted(p for p in root.glob("*") if p.is_dir())):
        try:
            for child in parent.iterdir():
                if child.is_dir() and child.name.lower() == name.lower():
                    candidates.append(child)
        except OSError:
            continue
    for hint in hints:
        found = git_root(hint)
        if found is not None:
            candidates.append(found)
    seen: set[Path] = set()
    fallback: Path | None = None
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not (resolved / ".git").exists():
            continue
        seen.add(resolved)
        remote = origin_slug(resolved)
        if remote is not None and remote.lower() == wanted:
            return resolved
        if remote is None and fallback is None and resolved.name.lower() == name.lower():
            fallback = resolved
    return fallback


def hermes_job(profile: str, job_id: str, extra: list[Path] = ()) -> tuple[dict[str, Any] | None, Path | None]:  # type: ignore[assignment]
    """The Hermes job record (`profile`'s cron/jobs.json, else the 33god-pm runtime's)."""
    home = hermes_home()
    files = [home / "profiles" / profile / "cron" / "jobs.json", *extra, PM_RUNTIME_JOBS]
    if profile == "default":
        files.insert(0, home / "cron" / "jobs.json")
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        jobs = data.get("jobs") if isinstance(data, dict) else data
        for job in jobs if isinstance(jobs, list) else []:
            if isinstance(job, dict) and job.get("id") == job_id:
                return job, path
    return None, None


def _read_capped(path: Path, cap: int = FILE_CAP) -> str | None:
    """A text file's content (capped), or None for a binary, oversized or unreadable one."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None  # a binary (an interpreter, a compiled tool): nothing to read
    text = raw.decode("utf-8", errors="replace")
    return text if len(text) <= cap else text[:cap] + f"\n... [truncated at {cap} chars]"


_SECRETISH = re.compile(r"(\.env$|secret|token|credential|\.key$|\.pem$|password)", re.IGNORECASE)


def _secretish(path: Path) -> bool:
    """Files never pasted into a prompt: env files and anything named like a credential."""
    return bool(_SECRETISH.search(path.name))


def _manifest_name(path: Path) -> str | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    table = data.get("candybar") if isinstance(data.get("candybar"), dict) else data
    name = table.get("name")
    return name if isinstance(name, str) else None


def _manifests(name: str, repo: Path | None) -> list[str]:
    """Committed candybar.toml files that register `name` (this checkout, then the job's repo)."""
    found: list[str] = []
    for path in sorted((REPO_ROOT / "deploy").rglob("candybar.toml")):
        if _manifest_name(path) == name:
            found.append(str(path))
    if repo is not None and repo.resolve() != REPO_ROOT.resolve():
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "ls-files", "*candybar.toml"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            out = None
        for rel in (out.stdout.split() if out is not None and out.returncode == 0 else [])[:50]:
            path = repo / rel
            if _manifest_name(path) == name and str(path) not in found:
                found.append(str(path))
    return found


def _unit_text(unit: str) -> str | None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "cat", unit], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr).strip()
    if not text:
        return None
    return text if len(text) <= UNIT_CAP else text[:UNIT_CAP] + "\n... [truncated]"


def _exec_lines(unit_text: str) -> list[str]:
    lines = []
    for line in unit_text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop"):
            lines.append(value.lstrip("-@:+!"))
    return lines


def _compact_invocation(row: dict[str, Any]) -> dict[str, Any]:
    out = {
        "startedAt": row.get("startedAt"),
        "trigger": row.get("trigger"),
        "result": row.get("result") or "running",
        "exitCode": row.get("exitCode"),
        "runtimeMs": row.get("runtimeMs"),
    }
    if row.get("result") == "fail":
        if row.get("errorMessage"):
            out["errorMessage"] = str(row["errorMessage"])[:300]
        if row.get("stderrTail"):
            out["stderrTail"] = str(row["stderrTail"])[-400:]
    if row.get("tokenSpend"):
        out["tokenSpend"] = row["tokenSpend"]
    if row.get("costUsd"):
        out["costUsd"] = row["costUsd"]
    return out


@dataclass
class ContextBundle:
    definition: dict[str, Any]
    runner: dict[str, Any] | None
    entrypoint: str | None
    invocations: list[dict[str, Any]]
    profile: dict[str, Any] | None
    ledger: dict[str, Any] | None = None
    manifests: list[str] = field(default_factory=list)
    unit_text: str | None = None
    hermes_job: dict[str, Any] | None = None
    hermes_jobs_file: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    repo_slug: str | None = None
    local_repo: Path | None = None
    workdir: Path = REPO_ROOT
    workdir_reason: str = "this candybar checkout (no better home found)"

    @property
    def name(self) -> str:
        return str(self.definition["name"])

    @property
    def runner_kind(self) -> str:
        return str((self.runner or {}).get("kind") or "exec")

    def render(self) -> str:
        """The bundle as prompt text: one section per source, JSON where structured."""

        def block(lang: str, body: str) -> str:
            return f"```{lang}\n{body}\n```"

        def dump(value: Any) -> str:
            return json.dumps(value, indent=2, default=str)

        runs = self.invocations
        ok = sum(1 for r in runs if r.get("result") == "success")
        failed = sum(1 for r in runs if r.get("result") == "fail")
        parts = [
            "### Definition (registry)",
            block("yaml", dump(self.definition)),
            "### Runner (how the executor starts it)",
            block("yaml", dump({"runner": self.runner, "entrypoint": self.entrypoint})),
            f"### Working directory\n`{self.workdir}`: {self.workdir_reason}.",
        ]
        if self.repo_slug:
            local = f"local checkout `{self.local_repo}`" if self.local_repo else "no local checkout found"
            parts.append(f"### Source of truth\nRepo `{self.repo_slug}` ({local}).")
        if self.ledger:
            parts += ["### Migration ledger entry (deploy/migration/ledger.yaml)", block("yaml", dump(self.ledger))]
        if self.manifests:
            parts.append("### Manifest(s)\n" + "\n".join(f"- `{m}`" for m in self.manifests))
        if self.unit_text:
            parts += ["### systemd unit (`systemctl --user cat`)", block("ini", self.unit_text)]
        if self.hermes_job:
            parts += [f"### Hermes job record ({self.hermes_jobs_file})", block("yaml", dump(self.hermes_job))]
        for path, text in self.files.items():
            parts += [f"### File `{path}`", block("text", text)]
        parts += [
            f"### Last {len(runs)} invocations (newest first): {ok} succeeded, {failed} failed",
            block("yaml", dump(runs)) if runs else "No recorded invocations.",
            "### Stored profile (overview, project, last evaluation)",
            block("yaml", dump(self.profile)) if self.profile else "No profile yet.",
        ]
        return "\n\n".join(parts)


def gather(
    *,
    definition: dict[str, Any],
    runner: dict[str, Any] | None,
    entrypoint: str | None,
    invocations: list[dict[str, Any]],
    profile: dict[str, Any] | None,
    ledger_path: Path | None = None,
) -> ContextBundle:
    """Build the bundle (blocking: file, git and systemctl reads)."""
    bundle = ContextBundle(
        definition=definition,
        runner=runner,
        entrypoint=entrypoint,
        invocations=[_compact_invocation(r) for r in invocations[:INVOCATIONS]],
        profile=profile,
    )
    name = bundle.name
    entry = None
    try:
        ledger = load_ledger(ledger_path or default_ledger_path())
        entry = next(
            (
                e for e in ledger.jobs
                if e.candybar_id == definition.get("id") or e.candybar.get("name") == name
            ),
            None,
        )
    except (LedgerError, OSError) as exc:
        log.warning("steward: ledger unreadable (%s); continuing without it", exc)
    hints: list[Path] = []
    if entry is not None:
        record = entry.model_dump(mode="json", exclude_none=True)
        record["history"] = record.get("history", [])[-5:]
        bundle.ledger = record
        bundle.repo_slug = entry.source_of_truth.repo
        hints = _absolute_paths(
            [p for p in (entry.native.unit_file, entry.native.jobs_file) if p]
            + list(entry.rollback)
            + list(entry.disable)
        )

    kind = bundle.runner_kind
    exec_paths: list[Path] = []
    if kind == "systemd" and runner:
        bundle.unit_text = _unit_text(str(runner.get("unit")))
        if bundle.unit_text:
            exec_paths = _absolute_paths(_exec_lines(bundle.unit_text))
    elif kind == "hermes" and runner:
        extra = [Path(entry.native.jobs_file)] if entry is not None and entry.native.jobs_file else []
        job, path = hermes_job(str(runner.get("profile")), str(runner.get("job_id")), extra)
        bundle.hermes_job = job
        bundle.hermes_jobs_file = str(path) if path else None
    elif kind == "exec":
        argv = [str(a) for a in (runner or {}).get("argv", [])] or ([entrypoint] if entrypoint else [])
        workdir = (runner or {}).get("workdir")
        base = Path(workdir).expanduser() if workdir else None
        for token in argv:
            path = Path(token).expanduser()
            if not path.is_absolute() and base is not None and "/" in token:
                path = base / path
            if path.is_absolute():
                exec_paths.append(path)
        if base is not None:
            exec_paths.insert(0, base)

    for path in exec_paths:
        if len(bundle.files) >= MAX_FILES:
            break
        if path.is_file() and not _secretish(path) and str(path) not in bundle.files:
            text = _read_capped(path)
            if text is not None:
                bundle.files[str(path)] = text

    # Working directory (contract step 2).
    if bundle.repo_slug:
        bundle.local_repo = find_checkout(bundle.repo_slug, hints)
        if bundle.local_repo is not None:
            bundle.workdir = bundle.local_repo
            bundle.workdir_reason = f"the local checkout of the ledger's source of truth {bundle.repo_slug}"
    if bundle.local_repo is None and kind == "exec":
        for path in exec_paths:
            root = git_root(path)
            if root is not None:
                bundle.workdir = root
                bundle.workdir_reason = f"the git root of the exec runner's {path}"
                break
    if bundle.workdir == REPO_ROOT and bundle.local_repo is None and kind == "hermes":
        workdir = (bundle.hermes_job or {}).get("workdir")
        if isinstance(workdir, str) and Path(workdir).is_dir():
            bundle.workdir = Path(workdir)
            bundle.workdir_reason = "the hermes job's workdir"
    if bundle.local_repo is None and kind == "systemd" and bundle.workdir == REPO_ROOT:
        # Not in the contract's list, but strictly better than the fallback:
        # the git root of the script the unit runs.
        for path in exec_paths:
            root = git_root(path)
            if root is not None:
                bundle.workdir = root
                bundle.workdir_reason = f"the git root of the unit's {path}"
                break
    bundle.manifests = _manifests(name, bundle.local_repo)
    return bundle


async def build_context(**kwargs: Any) -> ContextBundle:
    return await asyncio.to_thread(gather, **kwargs)
