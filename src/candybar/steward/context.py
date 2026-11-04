"""The steward's context bundle and working directory (steward-contract.md section 4, steps 1-2).

Everything an agent needs to reason about one candybar, gathered on the
host: the definition and runner, its migration-ledger entry and manifest,
the unit text (`systemd`), the Hermes job record (`hermes`), the argv / unit
script files (`exec`, `systemd`), the n8n workflow export (`n8n`), the last
30 invocations and the stored profile. Then the directory the OpenCode
session runs in, the job's own home:

1. the ledger `source_of_truth.repo`'s local checkout: first the checkout
   whose git remote is that `owner/name` (a scan of ~/docker, ~/.agents,
   ~/.hermes/hermes-agent, ~/HeyMa and ~/code two levels deep, cached for
   the process; the named roots rank first, worktrees last), then by name
   (`~/code/<name>`, `~/code/*/<name>`) or the git root of a path the
   ledger entry, the unit's FragmentPath or its ExecStart names, verified
   to exist and, when it has an `origin`, to point at that repo. A source
   of truth that is this candybar repo only records the migration (the
   ledger lives here), so it counts as no repo and resolution falls
   through;
2. else the runner's own home: a hermes job's `workdir`; a systemd unit's
   FragmentPath or the git root of the script its ExecStart runs; an
   exec runner's `workdir` / argv path's git root (for a
   `candybar.transition_exec` wrapper, the wrapped script's git root, else
   its directory, never the wrapper's); an n8n workflow's export in the
   workflows repo. Every git root resolves symlinks first (the unit file or
   a `~/.local/bin` link may live in another repo, such as ~/docker). A
   root that is this candybar repo is taken only when nothing else is
   found;
3. else this candybar checkout.

Evidence is untrusted data: every pasted record, file and log goes into a
fence longer than any backtick run in its content (so nothing inside can
close it), secret-named files are skipped, secret-shaped values are
redacted, and logs are read from the tail.

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
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..migration.ledger import LedgerError, default_ledger_path, load_ledger

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
# This repo's own slug(s): a ledger source of truth naming it is no repo.
OWN_SLUGS = frozenset({"delorenj/candybar"})
CODE_ROOT_ENV = "CANDYBAR_CODE_ROOT"
HERMES_ROOT_ENV = "CANDYBAR_HERMES_HOME"
N8N_EXPORTS_ENV = "CANDYBAR_N8N_EXPORTS"
CHECKOUT_ROOTS_ENV = "CANDYBAR_CHECKOUT_ROOTS"
# The 33god-pm runtime keeps its own jobs file (the profile dir links to it).
PM_RUNTIME_JOBS = Path.home() / "code" / "33GOD" / "agents" / "hermes" / "pm" / "runtime" / "cron" / "jobs.json"

FILE_CAP = 12_000  # chars of one source file in the prompt
UNIT_CAP = 12_000
MAX_FILES = 6
MAX_FILE_BYTES = 512_000
LOG_TAIL_BYTES = 64_000  # a log larger than MAX_FILE_BYTES is still read, from its tail
INVOCATIONS = 30
FALLBACK_REASON = "this candybar checkout (no better home found)"

_SLUG_RE = re.compile(r"[:/]([^/:\s]+/[^/\s]+?)(?:\.git)?/?$")


def code_root() -> Path:
    return Path(os.environ.get(CODE_ROOT_ENV) or Path.home() / "code").expanduser()


def hermes_home() -> Path:
    return Path(os.environ.get(HERMES_ROOT_ENV) or Path.home() / ".hermes").expanduser()


def n8n_exports() -> Path:
    default = code_root() / "delonet-n8n-workflows" / "workflows"
    return Path(os.environ.get(N8N_EXPORTS_ENV) or default).expanduser()


def git_root(path: Path) -> Path | None:
    """The nearest ancestor (or `path`) holding a `.git` entry, symlinks resolved first, or None.

    `~/.local/bin/activity-report` that links into `~/code/skillex/all-skills`
    belongs to that checkout, not to whatever holds the link.
    """
    try:
        real = Path(os.path.realpath(path))
        current = real if real.is_dir() else real.parent
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


def remote_slugs(root: Path) -> list[tuple[str, str]]:
    """(remote name, `owner/name`) of every remote of the checkout at `root`, origin first."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "config", "--get-regexp", r"^remote\..*\.url$"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[tuple[str, str]] = []
    for line in out.stdout.splitlines() if out.returncode == 0 else []:
        key, _, url = line.partition(" ")
        remote = key[len("remote."):-len(".url")]
        match = _SLUG_RE.search(url.strip())
        if match:
            found.append((remote, match.group(1)))
    return sorted(found, key=lambda item: item[0] != "origin")


def checkout_roots() -> tuple[Path, ...]:
    """Checkouts outside the code root that hold jobs, each looked up by its remotes.

    `CANDYBAR_CHECKOUT_ROOTS` (os.pathsep-separated; empty for none) overrides.
    """
    raw = os.environ.get(CHECKOUT_ROOTS_ENV)
    if raw is not None:
        return tuple(Path(p).expanduser() for p in raw.split(os.pathsep) if p)
    home = Path.home()
    return (home / "docker", home / ".agents", hermes_home() / "hermes-agent", home / "HeyMa")


def _scan(code: Path, extra: tuple[Path, ...]) -> list[tuple[int, Path]]:
    """(tier, checkout) the remote index reads: `extra` (tier 0), then the code root one (1) and two (2) deep.

    The named roots are the live checkouts (the units run from ~/HeyMa, not
    a ~/code/HeyMa clone), so they rank first. Hidden directories under the
    code root (`.wt` worktrees, `.archive`) are skipped.
    """
    level1: list[Path] = []
    level2: list[Path] = []

    def subdirs(parent: Path) -> list[Path]:
        try:
            return sorted(p for p in parent.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return []

    for child in subdirs(code):
        if (child / ".git").exists():
            level1.append(child)
        level2 += [p for p in subdirs(child) if (p / ".git").exists()]
    return [
        *((0, p) for p in extra if (p / ".git").exists()),
        *((1, p) for p in level1),
        *((2, p) for p in level2),
    ]


@lru_cache(maxsize=8)
def _remote_index(code: str, extra: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    """`owner/name` (lowercase) -> local checkouts whose remotes name it, best first.

    Best: a full checkout before a worktree (`.git` is a file), a named root
    before the code root (one level before two), an `origin` remote before
    any other. Cached for the process (a long-lived steward sees a new clone
    after a restart).
    """
    ranked: dict[str, list[tuple[tuple[bool, int, bool], str]]] = {}
    for tier, repo in _scan(Path(code), tuple(Path(p) for p in extra)):
        resolved = str(Path(os.path.realpath(repo)))
        worktree = (repo / ".git").is_file()
        for remote, slug in remote_slugs(repo):
            entries = ranked.setdefault(slug.lower(), [])
            if resolved not in (path for _, path in entries):
                entries.append(((worktree, tier, remote != "origin"), resolved))
    return {
        slug: tuple(path for _, path in sorted(entries, key=lambda e: e[0]))
        for slug, entries in ranked.items()
    }


def remote_checkouts(slug: str) -> list[Path]:
    """Local checkouts (code root two deep, `checkout_roots()`) with a remote that is `slug`."""
    index = _remote_index(str(code_root()), tuple(str(p) for p in checkout_roots()))
    return [Path(p) for p in index.get(slug.lower(), ()) if (Path(p) / ".git").exists()]


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def is_own_repo(slug: str | None = None, path: Path | None = None) -> bool:
    """Whether `slug` / `path` is this candybar repo (the scheduler, not a job's home)."""
    if slug is not None and slug.lower() in OWN_SLUGS:
        return True
    return path is not None and _same(path, REPO_ROOT)


def _absolute_paths(texts: list[str]) -> list[Path]:
    home = str(Path.home())
    out: list[Path] = []
    for text in texts:
        text = text.replace("%h", home)
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        for token in tokens:
            token = token.split("=", 1)[1] if token.startswith("--") and "=" in token else token
            if token.startswith(("/", "~/")):
                out.append(Path(token).expanduser())
    return out


TRANSITION_EXEC = "candybar.transition_exec"
# transition_exec's options that take a value (src/candybar/transition_exec.py, main()).
_TRANSITION_VALUE_OPTIONS = frozenset({"--state-file", "--profile", "--target", "--timeout", "--notify-timeout"})


def unwrap_transition_exec(argv: list[str]) -> tuple[list[str], list[str]] | None:
    """(wrapper argv, wrapped command) when `argv` runs `candybar.transition_exec`, else None.

    `python -m candybar.transition_exec [options] [--] <command...>` (or a
    path to `transition_exec.py`): the wrapper only delivers the detector's
    output, the command is the job.
    """
    start = None
    for i, token in enumerate(argv):
        if (token == TRANSITION_EXEC and i > 0 and argv[i - 1] == "-m") or Path(token).name == "transition_exec.py":
            start = i + 1
            break
    if start is None:
        return None
    i = start
    while i < len(argv):
        token = argv[i]
        if token == "--":
            i += 1
            break
        if not token.startswith("--"):
            break
        i += 2 if token in _TRANSITION_VALUE_OPTIONS else 1
    return argv[:i], argv[i:]


def find_checkout(slug: str, hints: list[Path] = ()) -> Path | None:  # type: ignore[assignment]
    """The local checkout of `owner/name`.

    First by remote: a checkout in `checkout_roots()` (~/docker, ~/.agents,
    ~/.hermes/hermes-agent, ~/HeyMa) or under the code root (two levels
    deep, hidden directories skipped) with a remote whose URL is `slug`
    (case-insensitive). Among several, one a hint lives in wins, else the
    best ranked: a full checkout before a worktree, a named root before the
    code root, an `origin` remote before another. Then by name under the
    code root, or a hinted git root: a candidate with an `origin` must point
    at `slug`; one without a readable origin is accepted on its name alone.
    """
    hinted = {root for root in (git_root(h) for h in hints) if root is not None}
    by_remote = remote_checkouts(slug)
    if by_remote:
        return next((p for p in by_remote if p in hinted), by_remote[0])
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


def n8n_workflow(webhook: str) -> Path | None:
    """The exported workflow whose Webhook node serves `webhook`, or None.

    Exports are `<slug>--<id>.json` (plus a `.published.json` twin); the
    working copy wins over the published one.
    """
    path = webhook.split("/webhook/", 1)[-1].strip("/")
    if not path:
        return None
    needle = re.compile(r'"path"\s*:\s*"' + re.escape(path) + '"')
    folder = n8n_exports()
    try:
        files = sorted(folder.glob("*.json"), key=lambda p: (p.name.endswith(".published.json"), p.name))
    except OSError:
        return None
    for candidate in files:
        try:
            if candidate.stat().st_size > MAX_FILE_BYTES:
                continue
            if needle.search(candidate.read_text(encoding="utf-8", errors="replace")):
                return candidate
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Evidence hygiene: what is pasted, and how.

_LOG_SUFFIXES = frozenset({".log", ".out", ".err"})


def is_log(path: Path) -> bool:
    return path.suffix.lower() in _LOG_SUFFIXES or any(p.lower() in ("log", "logs") for p in path.parts[:-1])


def _read_capped(path: Path, cap: int = FILE_CAP, *, tail: bool | None = None) -> str | None:
    """A text file's content (capped), or None for a binary or unreadable one.

    Logs (`*.log`, `*.out`, `*.err`, anything under a `log(s)/` directory)
    keep their tail, where the latest run is; anything else keeps its head.
    A log too large to read whole is read from its last bytes.
    """
    from_tail = is_log(path) if tail is None else tail
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size > MAX_FILE_BYTES and not from_tail:
            return None
        with path.open("rb") as handle:
            if size > MAX_FILE_BYTES:
                handle.seek(size - LOG_TAIL_BYTES)
            raw = handle.read()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None  # a binary (an interpreter, a compiled tool): nothing to read
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= cap and size <= MAX_FILE_BYTES:
        return text
    if from_tail:
        return f"... [earlier output dropped; the last {cap} chars follow]\n" + text[-cap:]
    return text[:cap] + f"\n... [truncated at {cap} chars]"


# Files never pasted into a prompt: env files and anything named like a credential.
_SECRETISH = re.compile(
    r"(^\.env|\.env$|\.env\.|\.pass$|passw|secret|token|credential|^rclone\.conf$|\.key$|\.pem$"
    r"|\.p12$|\.pfx$|\.kdbx$|^id_(rsa|ecdsa|ed25519)|\.netrc$|\.pgpass$|htpasswd)",
    re.IGNORECASE,
)


def _secretish(path: Path) -> bool:
    return bool(_SECRETISH.search(path.name))


_SECRET_KEY = (
    r"[A-Za-z0-9_.-]*(?:passw(?:or)?d|passphrase|secret|token|api[_-]?key|apikey|credential"
    r"|private[_-]?key|access[_-]?key|authorization)[A-Za-z0-9_.-]*"
)
# KEY=value, KEY: value, "key": "value", --key value-less forms are left alone.
_ASSIGN = re.compile(
    r"(?P<key>[\"']?" + _SECRET_KEY + r"[\"']?[ \t]*[:=][ \t]*)(?P<q>[\"']?)(?P<val>[^\s\"',;}]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_CRED = re.compile(r"(://[^/\s:@]+:)([^/\s@]+)(@)")
_TOKEN_SHAPES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ops_[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)
REDACTED = "[redacted]"


def _keep_value(value: str) -> bool:
    """Values that name a secret rather than hold one: references, variables, paths."""
    return (
        value.startswith(("op://", "$", "%", "/", "~", "<", "[redacted]"))
        or value.lower() in ("true", "false", "null", "none", "required", "optional")
        or len(value) < 4
        or re.fullmatch(r"[\d.,_]+", value) is not None
    )


def redact(text: str) -> str:
    """`text` with secret-shaped values replaced (a value naming a reference or path is kept)."""

    def assign(match: re.Match[str]) -> str:
        value = match.group("val")
        if _keep_value(value):
            return match.group(0)
        return f"{match.group('key')}{match.group('q')}{REDACTED}"

    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _ASSIGN.sub(assign, text)
    text = _URL_CRED.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", text)
    return _TOKEN_SHAPES.sub(REDACTED, text)


def fence(body: str, lang: str = "") -> str:
    """`body` in a fence longer than any backtick run inside it (its content cannot close it)."""
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{body}\n{ticks}"


def evidence_block(label: str, body: str, lang: str = "text") -> str:
    """One delimited, redacted evidence item: data for the agent to read, never instructions."""
    return f"#### Evidence: {label}\n{fence(redact(body), lang)}"


# ---------------------------------------------------------------------------
# Sources.


@lru_cache(maxsize=1)
def _policy_names() -> tuple[frozenset[str], frozenset[str]]:
    """(candybar ids, names) that deploy/scheduling-policy.json names."""
    try:
        data = json.loads((REPO_ROOT / "deploy" / "scheduling-policy.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(), frozenset()
    ids: set[str] = set()
    names: set[str] = set()
    for entry in data.get("exceptions", []) if isinstance(data, dict) else []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("candybar_id"), str):
            ids.add(entry["candybar_id"])
        for key in ("id", "ledger_id"):
            value = entry.get(key)
            if isinstance(value, str):
                leaf = value.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
                names.add(re.sub(r"\.(timer|service|json|toml)$", "", leaf))
    return frozenset(ids), frozenset(names)


PROTECTED_TAGS = frozenset({"smoke", "canary", "watchdog", "policy", "ops"})
BACKUP_TAGS = frozenset({"backups", "backup"})
BACKUP_NAMES = ("backup", "restic")
# A name containing one of these is protected whatever its tags say.
PROTECTED_NAMES = ("watchdog", "backstop", "canary", "smoke", "policy")


def protection(definition: dict[str, Any]) -> str | None:
    """Why this candybar never gets an automatic archive or refactor, or None (contract section 4, evaluate).

    Backups, canaries, smoke tests, watchdogs, backstops, policy and ops
    jobs: by tag, by name, or named in deploy/scheduling-policy.json.
    """
    tags = {str(t).lower() for t in definition.get("tags") or []}
    name = str(definition.get("name") or "")
    lowered = name.lower()
    hit = sorted(tags & PROTECTED_TAGS)
    if hit:
        return f"tagged {hit[0]}"
    if tags & BACKUP_TAGS or any(word in lowered for word in BACKUP_NAMES):
        return "a backup job"
    named = next((word for word in PROTECTED_NAMES if word in lowered), None)
    if named is not None:
        return f"named *{named}*"
    ids, names = _policy_names()
    if str(definition.get("id")) in ids or name in names:
        return "named in deploy/scheduling-policy.json"
    return None


def _manifest_names(path: Path) -> set[str]:
    """Every candybar name a manifest file defines.

    `[candybar]` (AD-8, one name) and project files' `[candybars.<name>]`
    tables (many) both count; a bare top-level `name` is the legacy shape.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return set()
    names: set[str] = set()
    single = data.get("candybar")
    table = single if isinstance(single, dict) else data
    if isinstance(table.get("name"), str):
        names.add(table["name"])
    elif isinstance(single, dict):
        # A `.candybars/<stem>.candybar.toml` may leave `name` out: the stem is it.
        suffix = ".candybar.toml"
        if path.name.endswith(suffix) and path.name != suffix:
            names.add(path.name[: -len(suffix)])
    multi = data.get("candybars")
    if isinstance(multi, dict):
        names.update(key for key, body in multi.items() if isinstance(body, dict))
    return names


def _own_manifests(name: str) -> list[Path]:
    """This repo's manifests that define `name`, migration records excluded."""
    migration = REPO_ROOT / "deploy" / "migration"
    return [
        path for path in sorted((REPO_ROOT / "deploy").rglob("candybar.toml"))
        if migration not in path.parents and name in _manifest_names(path)
    ]


def _manifests(name: str, repo: Path | None) -> list[str]:
    """Committed candybar.toml files that register `name` (this checkout, then the job's repo)."""
    found: list[str] = []
    for path in sorted((REPO_ROOT / "deploy").rglob("candybar.toml")):
        if name in _manifest_names(path):
            found.append(str(path))
    if repo is not None and not is_own_repo(path=repo):
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "ls-files", "*candybar.toml"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            out = None
        for rel in (out.stdout.split() if out is not None and out.returncode == 0 else [])[:50]:
            path = repo / rel
            if name in _manifest_names(path) and str(path) not in found:
                found.append(str(path))
    return found


def _systemctl(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else (out.stdout or out.stderr)


def _unit_text(unit: str) -> str | None:
    text = (_systemctl("cat", unit) or "").strip()
    if not text:
        return None
    return text if len(text) <= UNIT_CAP else text[:UNIT_CAP] + "\n... [truncated]"


def fragment_path(unit: str) -> Path | None:
    """The unit's FragmentPath with symlinks resolved (it may live in another repo), or None."""
    value = (_systemctl("show", "-p", "FragmentPath", "--value", unit) or "").strip()
    if not value.startswith("/"):
        return None
    try:
        return Path(value).resolve()
    except OSError:
        return None


def _exec_lines(unit_text: str) -> list[str]:
    lines = []
    for line in unit_text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop"):
            lines.append(value.lstrip("-@:+!"))
    return lines


def _log_lines(unit_text: str) -> list[Path]:
    """Files the unit's StandardOutput / StandardError append to (its run logs)."""
    out: list[Path] = []
    for line in unit_text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("StandardOutput", "StandardError"):
            kind, _, target = value.strip().partition(":")
            target = target.replace("%h", str(Path.home()))
            if kind in ("append", "file", "truncate") and target.startswith("/"):
                out.append(Path(target))
    return out


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
    unit_file: str | None = None
    hermes_job: dict[str, Any] | None = None
    hermes_jobs_file: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    repo_slug: str | None = None
    # The ledger repo's local checkout (None when the ledger names this repo).
    local_repo: Path | None = None
    # The job's own checkout (git root of its home, or the home itself):
    # what the project classification matches. None when nothing was found.
    checkout: Path | None = None
    ledger_note: str | None = None
    workdir: Path = REPO_ROOT
    workdir_reason: str = FALLBACK_REASON

    @property
    def name(self) -> str:
        return str(self.definition["name"])

    @property
    def runner_kind(self) -> str:
        return str((self.runner or {}).get("kind") or "exec")

    def render(self, *, include_evaluation: bool = True) -> str:
        """The bundle as prompt text: one delimited, redacted evidence block per source.

        `include_evaluation=False` leaves the stored evaluation out of the
        profile block (fix and refactor quote the evaluation they work from
        on their own, which may not be the latest).
        """

        def dump(value: Any) -> str:
            return json.dumps(value, indent=2, default=str)

        runs = self.invocations
        ok = sum(1 for r in runs if r.get("result") == "success")
        failed = sum(1 for r in runs if r.get("result") == "fail")
        parts = [
            evidence_block("definition (registry)", dump(self.definition), "json"),
            evidence_block("runner (how the executor starts it)", dump({"runner": self.runner, "entrypoint": self.entrypoint}), "json"),
            f"#### Working directory\n`{self.workdir}`: {self.workdir_reason}.",
        ]
        if self.ledger_note:
            parts.append(f"#### Source of truth\n{self.ledger_note}")
        elif self.repo_slug:
            local = f"local checkout `{self.local_repo or self.checkout}`" if (self.local_repo or self.checkout) else "no local checkout found"
            parts.append(f"#### Source of truth\nRepo `{self.repo_slug}` ({local}).")
        if self.ledger:
            parts.append(evidence_block("migration ledger entry (deploy/migration/ledger.yaml)", dump(self.ledger), "json"))
        if self.manifests:
            parts.append("#### Manifest(s)\n" + "\n".join(f"- `{m}`" for m in self.manifests))
        if self.unit_text:
            where = f", unit file `{self.unit_file}`" if self.unit_file else ""
            parts.append(evidence_block(f"systemd unit (`systemctl --user cat`{where})", self.unit_text, "ini"))
        if self.hermes_job:
            parts.append(evidence_block(f"Hermes job record ({self.hermes_jobs_file})", dump(self.hermes_job), "json"))
        for path, text in self.files.items():
            kind = "log tail" if is_log(Path(path)) else "file"
            parts.append(evidence_block(f"{kind} `{path}`", text))
        parts.append(
            evidence_block(
                f"last {len(runs)} invocations (newest first): {ok} succeeded, {failed} failed",
                dump(runs) if runs else "No recorded invocations.",
                "json",
            )
        )
        profile = self.profile
        if profile and not include_evaluation:
            profile = {k: v for k, v in profile.items() if k != "evaluation"}
        parts.append(
            evidence_block(
                "stored profile (overview, project, last evaluation)" if include_evaluation
                else "stored profile (overview, project)",
                dump(profile) if profile else "No profile yet.",
                "json",
            )
        )
        return "\n\n".join(parts)


def _first_home(candidates: list[tuple[Path, str, bool]]) -> tuple[Path, Path, str] | None:
    """(workdir, checkout, reason) of the first candidate with a home, this repo last.

    A candidate is `(path, reason, as_dir)`: `as_dir` homes (a hermes job's
    workdir) are the directory itself, the rest their git root.
    """
    found: list[tuple[Path, Path, str]] = []
    for path, reason, as_dir in candidates:
        if as_dir:
            if path.is_dir():
                found.append((path, git_root(path) or path, reason))
            continue
        root = git_root(path)
        if root is not None:
            found.append((root, root, f"{reason} {path}"))
    for home in found:
        if not is_own_repo(path=home[1]):
            return home
    return found[0] if found else None


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
    ledger_slug: str | None = None
    if entry is not None:
        record = entry.model_dump(mode="json", exclude_none=True)
        record["history"] = record.get("history", [])[-5:]
        bundle.ledger = record
        ledger_slug = entry.source_of_truth.repo
        hints = _absolute_paths(
            [p for p in (entry.native.unit_file, entry.native.jobs_file) if p]
            + list(entry.rollback)
            + list(entry.disable)
        )

    kind = bundle.runner_kind
    exec_paths: list[Path] = []
    log_paths: list[Path] = []
    homes: list[tuple[Path, str, bool]] = []
    fragment: Path | None = None
    if kind == "systemd" and runner:
        unit = str(runner.get("unit"))
        bundle.unit_text = _unit_text(unit)
        fragment = fragment_path(unit)
        bundle.unit_file = str(fragment) if fragment else None
        if bundle.unit_text:
            exec_paths = _absolute_paths(_exec_lines(bundle.unit_text))
            log_paths = _log_lines(bundle.unit_text)
        if fragment is not None:
            homes.append((fragment, "the git root of the unit file", False))
        homes += [(p, "the git root of the unit's", False) for p in exec_paths]
    elif kind == "hermes" and runner:
        extra = [Path(entry.native.jobs_file)] if entry is not None and entry.native.jobs_file else []
        job, path = hermes_job(str(runner.get("profile")), str(runner.get("job_id")), extra)
        bundle.hermes_job = job
        bundle.hermes_jobs_file = str(path) if path else None
        workdir = (job or {}).get("workdir")
        if isinstance(workdir, str) and workdir:
            homes.append((Path(workdir).expanduser(), "the hermes job's workdir", True))
        if path is not None:
            homes.append((path.resolve(), "the git root of the hermes jobs file", False))
    elif kind == "exec":
        argv = [str(a) for a in (runner or {}).get("argv", [])] or ([entrypoint] if entrypoint else [])
        workdir = (runner or {}).get("workdir")
        base = Path(workdir).expanduser() if workdir else None

        def absolute(tokens: list[str]) -> list[Path]:
            out: list[Path] = []
            for token in tokens:
                path = Path(token).expanduser()
                if not path.is_absolute() and base is not None and "/" in token:
                    path = base / path
                if path.is_absolute():
                    out.append(path)
            return out

        wrapped = unwrap_transition_exec(argv)
        if wrapped is not None:
            # The wrapper (this repo's `candybar.transition_exec`, run from this
            # checkout) only delivers the detector's output: the job is the
            # wrapped command, and its home is that script's, not the wrapper's.
            wrapper, command = wrapped
            job_paths = absolute(command)
            exec_paths = [*job_paths, *absolute(wrapper[1:])]
            homes += [(p, "the git root of the transition_exec-wrapped script", False) for p in job_paths]
            homes += [
                (p.parent, "the directory of the transition_exec-wrapped script (it is in no git repo)", True)
                for p in job_paths[:1]
            ]
        else:
            exec_paths = absolute(argv)
            if base is not None:
                homes.append((base, "the git root of the exec runner's workdir", False))
            homes += [(p, "the git root of the exec runner's", False) for p in exec_paths]
    elif kind == "n8n" and runner:
        export = n8n_workflow(str(runner.get("webhook") or ""))
        if export is not None:
            exec_paths.append(export)
            homes.append((export, "the git root of the n8n workflow export", False))

    # A candybar defined in this repo (a smoke canary, the policy job, the
    # watchdog canary) has its home here; a migration manifest does not
    # count (it records the move, like the ledger).
    for manifest in _own_manifests(name):
        homes.append((manifest, "the git root of its manifest", False))

    for path in [*exec_paths, *log_paths]:
        if len(bundle.files) >= MAX_FILES:
            break
        if path.is_file() and not _secretish(path) and str(path) not in bundle.files:
            text = _read_capped(path)
            if text is not None:
                bundle.files[str(path)] = text

    # Working directory (contract step 2).
    if ledger_slug and is_own_repo(slug=ledger_slug):
        bundle.ledger_note = (
            f"The ledger entry names this candybar repo (`{ledger_slug}`) as its source of truth: that only "
            "records the migration (the ledger lives here), so it is not the job's home. The job's home is "
            "the working directory above."
        )
    elif ledger_slug:
        bundle.repo_slug = ledger_slug
        found = find_checkout(ledger_slug, [*hints, *([fragment] if fragment else []), *exec_paths])
        if found is not None and not is_own_repo(path=found):
            bundle.local_repo = found
            bundle.checkout = found
            bundle.workdir = found
            bundle.workdir_reason = f"the local checkout of the ledger's source of truth {ledger_slug}"
    if bundle.local_repo is None:
        home = _first_home(homes)
        if home is not None:
            bundle.workdir, bundle.checkout, bundle.workdir_reason = home
            if bundle.repo_slug is None:
                root = git_root(bundle.checkout)
                bundle.repo_slug = origin_slug(root) if root is not None else None
    bundle.manifests = _manifests(name, bundle.checkout)
    return bundle


async def build_context(**kwargs: Any) -> ContextBundle:
    return await asyncio.to_thread(gather, **kwargs)
