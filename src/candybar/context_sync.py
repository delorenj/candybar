"""Resolve a local editing context to the existing fail-closed manifest autosync.

Unlike the host watcher, this entrypoint never enrolls a global inventory. A
project manifest selects its entire project (including siblings); a legacy file
selects only itself. Directory resolution stays inside the current checkout.
"""

from __future__ import annotations

from pathlib import Path
from threading import Event

from .autosync import (
    EXCLUDED,
    ReconcileReport,
    WatchConfig,
    _safe,
    _walk,
    reconcile,
    run_watch,
)
from .project import CANDYBARS_DIR, FILE_SUFFIX, PROJECT_FILE, Project, find_project


def _project_files(directory: Path) -> bool:
    return bool(Project(directory).files())


def _excluded_context(path: Path, boundary: Path | None = None) -> bool:
    """Reject excluded directories within a checkout, not e.g. a tmp test root."""
    parts = path.relative_to(boundary).parts if boundary else path.parts
    return any(part in EXCLUDED for part in parts)


def context_config(path: Path | None = None) -> WatchConfig:
    """Enroll only manifests belonging to a saved file or current directory.

    A directory under a manifest-bearing project selects the nearest project.
    Otherwise its git checkout is a bounded discovery root for project and
    legacy manifests. Nothing searches a parent of the checkout or ~/code.
    """
    target = Path.cwd() if path is None else Path(path).expanduser()
    target = target.absolute()
    if not _safe(target):
        raise ValueError(f'unsafe candybar context: {target}')

    checkout = find_project(target.parent if target.is_file() else target)
    git_root = checkout.root if checkout is not None and (checkout.root / '.git').exists() else None
    if git_root is not None and _excluded_context(target, git_root):
        raise ValueError(f'excluded candybar context: {target}')
    if git_root is None and any(part in EXCLUDED for part in target.parts):
        raise ValueError(f'excluded candybar context: {target}')

    if target.is_file():
        if target.name == PROJECT_FILE:
            return WatchConfig((), projects=(target.parent,))
        if target.name.endswith(FILE_SUFFIX) and target.parent.name == CANDYBARS_DIR:
            return WatchConfig((), projects=(target.parent.parent,))
        if target.name == 'candybar.toml':
            return WatchConfig((), legacy=(target,))
        raise ValueError(f'not a candybar manifest: {target}')
    if not target.is_dir():
        raise ValueError(f'no candybar manifest or directory at {target}')

    # Do this before using find_project's git root: nested projects (such as
    # bloodbank/services/target-watch) have their own identity and siblings.
    for directory in (target, *target.parents):
        if git_root is not None and not directory.is_relative_to(git_root):
            break
        if directory.name in EXCLUDED or not _safe(directory):
            raise ValueError(f'excluded or unsafe candybar context: {directory}')
        if _project_files(directory):
            # Directory entry activates the checkout's complete inventory,
            # including nested legacy manifests alongside root project files.
            # A saved project file above still selects only its own siblings.
            if directory == git_root:
                return WatchConfig((git_root,))
            legacy = directory / 'candybar.toml'
            return WatchConfig((), projects=(directory,),
                               legacy=(legacy,) if legacy.is_file() else ())
        legacy = directory / 'candybar.toml'
        if legacy.is_file():
            if directory == git_root:
                return WatchConfig((git_root,))
            return WatchConfig((), legacy=(legacy,))
        if git_root is not None and directory == git_root:
            break

    if git_root is not None:
        # _walk prunes fixtures, worktrees, symlinks and generated trees. Check
        # for real manifests *before* API access, so an empty checkout is inert.
        for directory in _walk(git_root):
            if _project_files(directory) or (directory / 'candybar.toml').is_file():
                return WatchConfig((git_root,))
    raise ValueError(f'no candybar manifests in context: {target}')


def sync_context(path: Path | None = None, *, dry_run: bool = False,
                 api=None) -> ReconcileReport:
    """Reconcile the local context through autosync's validation and API path."""
    return reconcile(context_config(path), api=api, dry_run=dry_run)


def watch_context(path: Path | None = None, *, stop_event: Event | None = None) -> None:
    """Watch the local context with the existing event-driven native watcher."""
    run_watch(context_config(path), stop_event=stop_event)
