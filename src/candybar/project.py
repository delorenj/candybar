"""Project-scoped candybars: find, load and write the candybar files a repo owns.

A candybar belongs to a project when its definition is committed in that
project's repo, in one of two places (mise-style: one file or many):

- ``<root>/.candybars/<anything>.candybar.toml``: one file per candybar (a
  file may still hold several);
- ``<root>/.candybar.toml``: one file holding many candybars.

Both are read together. Inside any candybar file a definition is either the
single ``[candybar]`` table (the original AD-8 shape; in ``.candybars/`` its
``name`` defaults to the file's stem) or one ``[candybars.<name>]`` table per
candybar, the way mise keys ``[tasks.<name>]``:

.. code-block:: toml

    [candybars.nightly-backup]
    schedule = "0 3 * * *"
    timezone = "America/New_York"

    [candybars.nightly-backup.runner]
    kind = "exec"
    argv = ["./scripts/backup.sh"]      # relative to the project root

Relative paths in a project file resolve against the project root, not the
file: ``entrypoint``, an ``argv[0]`` or ``on_failure[0]`` that contains a
slash, and an exec ``workdir``. An exec runner with no ``workdir`` starts in
the project root. An ``entrypoint`` registers as the equivalent exec runner
(the script, started in its own directory) because the API, in its
container, refuses a host path it cannot see. Every project candybar carries the ``project:<name>`` tag,
so the registry knows which project owns it (``candybar ls`` in the repo
shows exactly those, and the ones that exist only in the registry are
reported as orphans).

The project is the git toplevel of the working directory (its basename is the
project name, the same rule the hindsight banks use); outside git, the
nearest ancestor holding ``.candybars/`` or ``.candybar.toml``.

Pure apart from reading and writing the candybar files themselves: no API,
no bus, no database. The CLI (`register`, `ls`, `create`) and the create TUI
share it, so a file the TUI writes is exactly a file `register` accepts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .registry.manifest import CandybarManifest, ManifestError
from .runners import canonical_runner

CANDYBARS_DIR = ".candybars"
PROJECT_FILE = ".candybar.toml"
FILE_SUFFIX = ".candybar.toml"
PROJECT_TAG_PREFIX = "project:"

SINGLE_TABLE = "candybar"
MULTI_TABLE = "candybars"
_TOP_LEVEL = frozenset({SINGLE_TABLE, MULTI_TABLE})

# The writable definition fields, in the order a written file lists them.
FIELD_ORDER = ("name", "description", "tags", "schedule", "timezone", "entrypoint", "noise", "enabled")
RUNNER_ORDER = (
    "kind", "argv", "workdir", "env", "timeout_s", "unit", "profile", "job_id",
    "webhook", "url", "data", "on_failure",
)
# The fields `register --update` keeps in step with the registry (PATCH-able).
# `enabled` is deliberately absent: it is the state a candybar is *created*
# in; pause/resume own it afterwards.
SYNCED_FIELDS = ("description", "tags", "schedule", "timezone", "entrypoint", "runner", "noise")

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# --- the project ---------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    """A directory that can own candybars."""

    root: Path
    # The project's identity when it is not the root's basename: a linked git
    # worktree is still its main repo's project.
    label: str | None = None

    @property
    def name(self) -> str:
        return self.label or self.root.name

    @property
    def tag(self) -> str:
        return project_tag(self.name)

    @property
    def candybars_dir(self) -> Path:
        return self.root / CANDYBARS_DIR

    @property
    def project_file(self) -> Path:
        return self.root / PROJECT_FILE

    def files(self) -> list[Path]:
        """Every candybar file this project holds: `.candybar.toml` first, then `.candybars/*`."""
        found = [self.project_file] if self.project_file.is_file() else []
        if self.candybars_dir.is_dir():
            found.extend(
                path for path in sorted(self.candybars_dir.glob(f"*{FILE_SUFFIX}")) if path.is_file()
            )
        return found

    def owns(self, path: Path) -> bool:
        """True when `path` is one of this project's candybar files (by location, not existence)."""
        for candidate, project_file, candybars_dir in (
            (_absolute(path), _absolute(self.project_file), _absolute(self.candybars_dir)),
            (path.resolve(), self.project_file.resolve(), self.candybars_dir.resolve()),
        ):
            if candidate == project_file:
                return True
            if candidate.parent == candybars_dir and candidate.name.endswith(FILE_SUFFIX):
                return True
        return False

    def default_file_for(self, name: str) -> Path:
        """Where `candybar create` puts a new one-candybar file."""
        return self.candybars_dir / f"{name}{FILE_SUFFIX}"


def project_tag(name: str) -> str:
    return f"{PROJECT_TAG_PREFIX}{name}"


def project_of_tags(tags: Iterable[str]) -> str | None:
    """The project a registry row says owns it (its first `project:` tag), or None."""
    for tag in tags:
        if tag.startswith(PROJECT_TAG_PREFIX) and len(tag) > len(PROJECT_TAG_PREFIX):
            return tag[len(PROJECT_TAG_PREFIX):]
    return None


def _absolute(path: Path) -> Path:
    """`path` made absolute and normalised WITHOUT following symlinks.

    A project's `.venv/bin/python` must stay that path: resolving it would
    register the interpreter the venv links to and silently drop the venv.
    """
    return Path(os.path.normpath(os.path.abspath(path)))


def _git(start: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(start), *args],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


def _git_toplevel(start: Path) -> Path | None:
    top = _git(start, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def _git_project_name(start: Path, top: Path) -> str | None:
    """The repo's name for a linked worktree (its main checkout's basename), else None.

    `--git-common-dir` is `<main>/.git` for a worktree of `<main>`, and the
    worktree's own `.git` (or `.git/modules/<name>` for a submodule) otherwise.
    """
    common = _git(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        return None
    common_path = Path(common)
    if common_path.name == ".git":
        name = common_path.parent.name
    elif common_path.parent.name == "modules":
        return None  # a submodule: its toplevel basename is already its name
    else:
        name = common_path.name.removesuffix(".git")  # a bare repo's worktree
    return name if name and name != top.name else None


def find_project(start: Path | str | None = None) -> Project | None:
    """The project `start` (default: the working directory) is inside, or None."""
    here = Path(start if start is not None else Path.cwd()).resolve()
    if here.is_file():
        here = here.parent
    top = _git_toplevel(here)
    if top is not None:
        return Project(top.resolve(), _git_project_name(here, top))
    for candidate in (here, *here.parents):
        if (candidate / CANDYBARS_DIR).is_dir() or (candidate / PROJECT_FILE).is_file():
            return Project(candidate)
    return None


def project_at(directory: Path | str) -> Project:
    """`directory` as a project root (keeping its git identity when it is a toplevel)."""
    root = Path(directory).resolve()
    found = find_project(root)
    return found if found is not None and found.root == root else Project(root)


# --- loading -----------------------------------------------------------------


@dataclass(frozen=True)
class CandybarDef:
    """One validated candybar and the file (and table) it came from."""

    manifest: CandybarManifest
    source: Path
    table: str  # "[candybar]" or "[candybars.<name>]"

    @property
    def name(self) -> str:
        return self.manifest.name


@dataclass
class LoadResult:
    defs: list[CandybarDef] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_name(self) -> dict[str, CandybarDef]:
        return {d.name: d for d in self.defs}


def _abs_against(value: str, base: Path) -> str:
    return value if value.startswith("/") else str(_absolute(base / value))


def _resolve_project_paths(table: dict[str, Any], project: Project) -> dict[str, Any]:
    """Project-file path rules: relative paths resolve against the project root."""
    table = dict(table)
    runner = table.get("runner")
    if isinstance(runner, dict):
        runner = dict(runner)
        keys = ("argv", "on_failure") if runner.get("kind") == "exec" else ("on_failure",)
        for key in keys:
            argv = runner.get(key)
            if isinstance(argv, list) and argv and isinstance(argv[0], str) and "/" in argv[0]:
                runner[key] = [_abs_against(argv[0], project.root), *argv[1:]]
        if runner.get("kind") == "exec":
            workdir = runner.get("workdir")
            if workdir is None:
                runner["workdir"] = str(project.root)
            elif isinstance(workdir, str) and workdir.strip():
                runner["workdir"] = _abs_against(workdir, project.root)
        table["runner"] = runner
    tags = table.get("tags", [])
    if isinstance(tags, list):
        foreign = [t for t in tags if isinstance(t, str) and t.startswith(PROJECT_TAG_PREFIX) and t != project.tag]
        if foreign:
            raise ManifestError(
                f"tag(s) {', '.join(map(repr, foreign))} name another project; "
                f"{project.tag!r} is added automatically, drop the `project:` tags"
            )
        if project.tag not in tags:
            table["tags"] = [*tags, project.tag]
    return table


def _tables(
    raw: dict[str, Any], path: Path, errors: list[str], *, strict: bool
) -> list[tuple[str, dict[str, Any]]]:
    """The candybar tables in one parsed file, each with its `[header]` label.

    `strict` (project files) refuses unknown top-level keys; a legacy
    `candybar.toml` keeps AD-8's tolerance of other tables.
    """
    found: list[tuple[str, dict[str, Any]]] = []
    unknown = sorted(set(raw) - _TOP_LEVEL) if strict else []
    if unknown:
        errors.append(
            f"{path}: unknown top-level key(s) {', '.join(map(repr, unknown))}; "
            f"expected [{SINGLE_TABLE}] or [{MULTI_TABLE}.<name>] tables"
        )
    single = raw.get(SINGLE_TABLE)
    if single is not None:
        if isinstance(single, dict):
            table = dict(single)
            if "name" not in table and path.name.endswith(FILE_SUFFIX) and path.name != PROJECT_FILE:
                table["name"] = path.name[: -len(FILE_SUFFIX)]
            found.append((f"[{SINGLE_TABLE}]", table))
        else:
            errors.append(f"{path}: `{SINGLE_TABLE}` must be a table")
    multi = raw.get(MULTI_TABLE)
    if multi is not None:
        if not isinstance(multi, dict):
            errors.append(f"{path}: `{MULTI_TABLE}` must be a table of [{MULTI_TABLE}.<name>] tables")
        else:
            for key, body in multi.items():
                label = f"[{MULTI_TABLE}.{key}]"
                if not isinstance(body, dict):
                    errors.append(f"{path}: {label} must be a table")
                    continue
                table = dict(body)
                if table.setdefault("name", key) != key:
                    errors.append(
                        f"{path}: {label} sets name = {table['name']!r}; "
                        "the table key is the name, drop the `name` line or make them match"
                    )
                    continue
                found.append((label, table))
    if not found and not unknown and not errors:
        errors.append(f"{path}: no candybars: expected [{SINGLE_TABLE}] or [{MULTI_TABLE}.<name>] tables")
    return found


def parse_text(
    text: str, path: Path, *, project: Project | None, base: Path | None = None
) -> LoadResult:
    """Parse and validate one candybar file's text (`path` labels errors and names stems).

    With a `project`, project-file rules apply (paths against the root, the
    project tag). Without one, a relative `entrypoint` resolves against
    `base` (default: the file's directory), the original AD-8 rule.
    """
    result = LoadResult()
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        result.errors.append(f"{path}: invalid TOML: {exc}")
        return result
    base = project.root if project is not None else (base or path.parent)
    for label, table in _tables(raw, path, result.errors, strict=project is not None):
        if project is not None:
            try:
                table = _resolve_project_paths(table, project)
            except ManifestError as exc:
                result.errors.append(f"{path} {label}: {exc}")
                continue
        try:
            manifest = CandybarManifest.model_validate(table)
        except ValueError as exc:
            result.errors.append(f"{path} {label}: {_short(exc)}")
            continue
        if manifest.entrypoint is not None:
            # Legacy files keep AD-8's resolve(); project files keep symlinks.
            entrypoint = (
                Path(_abs_against(manifest.entrypoint, base))
                if project is not None
                else (base / manifest.entrypoint).resolve()
            )
            if not entrypoint.is_file():
                result.errors.append(
                    f"{path} {label}: entrypoint is not an existing file: {manifest.entrypoint!r}"
                )
                continue
            if project is not None:
                # A project file's `entrypoint` registers as the equivalent exec
                # runner (argv = the script, started in its own directory, as a
                # bare entrypoint is): the API runs in a container and refuses
                # an `entrypoint` host path it cannot see, a runner it does not check.
                runner = canonical_runner(
                    {"kind": "exec", "argv": [str(entrypoint)], "workdir": str(entrypoint.parent)}
                )
                manifest = manifest.model_copy(update={"entrypoint": None, "runner": runner})
            else:
                manifest = manifest.model_copy(update={"entrypoint": str(entrypoint)})
        result.defs.append(CandybarDef(manifest=manifest, source=path, table=label))
    return result


def _short(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    parts = []
    for err in errors():
        where = ".".join(str(p) for p in err.get("loc", ()))
        msg = str(err.get("msg", "invalid")).removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts)


def load_file(path: Path, *, project: Project | None) -> LoadResult:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return LoadResult(errors=[f"{path}: cannot read: {exc}"])
    return parse_text(text, path, project=project)


def _check_duplicates(result: LoadResult) -> LoadResult:
    seen: dict[str, CandybarDef] = {}
    unique: list[CandybarDef] = []
    for d in result.defs:
        first = seen.get(d.name)
        if first is not None:
            result.errors.append(
                f"duplicate candybar name {d.name!r}: {first.source} {first.table} and {d.source} {d.table}"
            )
            continue
        seen[d.name] = d
        unique.append(d)
    result.defs = unique
    return result


def load_project(project: Project) -> LoadResult:
    """Every candybar the project defines, plus every problem found on the way."""
    result = LoadResult()
    for path in project.files():
        one = load_file(path, project=project)
        result.defs.extend(one.defs)
        result.errors.extend(one.errors)
    return _check_duplicates(result)


def load_path(path: Path) -> LoadResult:
    """The candybars in one explicitly named file (or legacy `candybar.toml` directory).

    A project's own candybar file gets project rules; any other file the
    original AD-8 rules (paths against the file's directory, no project tag).
    """
    path = Path(path)
    if path.is_dir():
        legacy = path / "candybar.toml"
        if legacy.is_file():
            path = legacy
        else:
            project = project_at(path)
            if project.files():
                return load_project(project)
            raise ManifestError(
                f"no candybar.toml, {PROJECT_FILE} or {CANDYBARS_DIR}/*{FILE_SUFFIX} at {path}"
            )
    if not path.is_file():
        raise ManifestError(f"no candybar file at {path}")
    project = find_project(path.parent)
    owner = project if project is not None and project.owns(path) else None
    return _check_duplicates(load_file(path, project=owner))


# --- comparing with the registry --------------------------------------------


def definition(manifest: CandybarManifest) -> dict[str, Any]:
    """The manifest's writable fields, in registry (PATCH) terms."""
    return manifest.model_dump()


def drift(manifest: CandybarManifest, registered: Mapping[str, Any]) -> dict[str, Any]:
    """{field: wanted} for every synced field where the registry differs from the file.

    `registered` is the API's row merged with its `/runner` body (the list row
    carries no runner). A field the row does not carry is not compared.
    """
    wanted = definition(manifest)
    changes: dict[str, Any] = {}
    for key in SYNCED_FIELDS:
        if key not in registered:
            continue
        have = registered.get(key)
        want = wanted.get(key)
        if key == "tags":
            have, want = sorted(have or []), sorted(want or [])
        if have != want:
            changes[key] = wanted.get(key)
    # Exactly one of entrypoint/runner is set; send only the one that defines it.
    if "entrypoint" in changes and wanted.get("entrypoint") is None:
        changes.pop("entrypoint")
    if "runner" in changes and wanted.get("runner") is None:
        changes.pop("runner")
    return changes


# --- writing -----------------------------------------------------------------


def toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.match(key) else toml_value(key)


def toml_value(value: Any) -> str:
    """One TOML value. Strings are JSON-escaped (valid TOML basic strings) plus DEL."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{toml_key(str(k))} = {toml_value(v)}" for k, v in value.items()) + " }"
    if value is None:
        raise ValueError("TOML has no null")
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


def _ordered(table: Mapping[str, Any], order: tuple[str, ...]) -> list[tuple[str, Any]]:
    keys = [k for k in order if k in table] + sorted(k for k in table if k not in order)
    return [(k, table[k]) for k in keys]


def render(
    spec: Mapping[str, Any], *, style: str = "single", comment: str | None = None
) -> str:
    """A candybar definition as TOML text.

    `spec` uses manifest field names (`runner` a dict). Empty optionals and
    defaults that say nothing (`description = ""`, `tags = []`, `noise =
    false`, `enabled = true`, `timezone = "UTC"`) are left out. `style` is
    "single" (`[candybar]`, with `name`) or "multi" (`[candybars.<name>]`).
    """
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("a candybar needs a name")
    skip_default = {"description": "", "tags": [], "noise": False, "enabled": True, "timezone": "UTC"}
    body = {
        k: v
        for k, v in spec.items()
        if k != "runner" and v is not None and not (k in skip_default and v == skip_default[k])
    }
    if style == "multi":
        body.pop("name", None)
        header = f"{MULTI_TABLE}.{toml_key(name)}"
    elif style == "single":
        header = SINGLE_TABLE
    else:
        raise ValueError(f"unknown style {style!r}")
    lines = [f"# {line}".rstrip() for line in comment.splitlines()] if comment else []
    lines.append(f"[{header}]")
    lines.extend(f"{toml_key(k)} = {toml_value(v)}" for k, v in _ordered(body, FIELD_ORDER))
    runner = spec.get("runner")
    if runner:
        lines.append("")
        lines.append(f"[{header}.runner]")
        lines.extend(
            f"{toml_key(k)} = {toml_value(v)}"
            for k, v in _ordered({k: v for k, v in runner.items() if v is not None}, RUNNER_ORDER)
        )
    return "\n".join(lines) + "\n"


class WriteError(ValueError):
    """The file was not written: invalid result, existing file, or a taken name."""


@dataclass(frozen=True)
class Planned:
    """A write `plan_write` checked: the file's whole new text and what it defines."""

    path: Path
    text: str
    appended: bool
    result: LoadResult


def plan_write(
    spec: Mapping[str, Any],
    dest: Path,
    *,
    project: Project | None,
    comment: str | None = None,
    overwrite: bool = False,
) -> Planned:
    """Render `spec` for `dest` and validate the file exactly as `register` would read it.

    `dest` named `.candybar.toml` is appended to (multi style); any other path
    gets a new single-style file. Raises WriteError, never touches the disk.
    """
    dest = Path(dest)
    name = spec.get("name")
    if dest.is_dir():
        raise WriteError(f"{dest} is a directory; name a file")
    ancestor = dest.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        raise WriteError(f"cannot create {dest}: {ancestor} is not a writable directory")
    append = dest.name == PROJECT_FILE
    block = render(spec, style="multi" if append else "single", comment=comment)
    if append and dest.is_file():
        existing = dest.read_text(encoding="utf-8")
        try:
            present = tomllib.loads(existing)
        except tomllib.TOMLDecodeError as exc:
            raise WriteError(f"{dest}: invalid TOML, fix it before appending: {exc}") from exc
        table = present.get(MULTI_TABLE)
        single = present.get(SINGLE_TABLE)
        if (isinstance(table, dict) and name in table) or (
            isinstance(single, dict) and single.get("name") == name
        ):
            raise WriteError(f"{dest} already defines {name!r}")
        sep = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        text = existing + sep + block
    else:
        if dest.exists() and not overwrite:
            raise WriteError(f"{dest} already exists")
        text = block
    owner = project if project is not None and project.owns(dest) else None
    result = parse_text(text, dest, project=owner)
    if not result.ok:
        raise WriteError("; ".join(result.errors))
    names = [d.name for d in result.defs]
    if names.count(name) > 1:  # pragma: no cover - TOML refuses a duplicate table first
        raise WriteError(f"{dest} already defines {name!r}")
    if owner is not None:
        # The name must also be free across the project's other files.
        others = [d for d in load_project(owner).defs if d.source.resolve() != dest.resolve()]
        clash = next((d for d in others if d.name == name), None)
        if clash is not None:
            raise WriteError(f"{name!r} is already defined in {clash.source} {clash.table}")
    return Planned(path=dest, text=text, appended=append and dest.is_file(), result=result)


def write(planned: Planned) -> Path:
    planned.path.parent.mkdir(parents=True, exist_ok=True)
    planned.path.write_text(planned.text, encoding="utf-8")
    return planned.path


__all__ = [
    "CANDYBARS_DIR",
    "FILE_SUFFIX",
    "PROJECT_FILE",
    "PROJECT_TAG_PREFIX",
    "SYNCED_FIELDS",
    "CandybarDef",
    "LoadResult",
    "Planned",
    "Project",
    "WriteError",
    "definition",
    "drift",
    "find_project",
    "load_file",
    "load_path",
    "load_project",
    "parse_text",
    "plan_write",
    "project_at",
    "project_of_tags",
    "project_tag",
    "render",
    "toml_value",
    "write",
]
