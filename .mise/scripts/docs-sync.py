#!/usr/bin/env python3
"""Copy recent project Markdown into a dated Obsidian snapshot.

The vault folder is Projects/<project_id>/<date>, where project_id comes from
the repository's .project.json manifest (falling back to the repository
directory name for bare repos). The index heading uses project_name (or the
directory name) so the display name may differ from the vault folder.
"""

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import quote, urlencode


# Sources are root files or under docs/ and _bmad-output/, so this cannot collide.
INDEX = "_docs-sync/Index.md"


def positive_hours(value):
    hours = float(value)
    if not math.isfinite(hours) or hours <= 0:
        raise argparse.ArgumentTypeError("hours must be a finite number greater than zero")
    return hours


def vault_component(value, source):
    """Reject anything that is not a safe single path component."""
    if (
        not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise RuntimeError(f"Unsafe {source} for the vault path: {value!r}")
    return value


def project_identity(root):
    """Return (vault folder, display name) for the repository.

    Reads .project.json when present: project_id names the vault folder and
    project_name titles the index. A bare repository without a manifest (or
    with blank identity fields) falls back to the repository directory name
    for both. A manifest that exists but is not valid JSON is an error rather
    than a silent fallback, because it indicates a broken project.
    """
    identifier, name = None, None
    manifest = root / ".project.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Cannot parse {manifest}: {error}")
        if isinstance(data, dict):
            identifier = data.get("project_id")
            name = data.get("project_name")
    if not isinstance(identifier, str) or not identifier.strip():
        identifier = root.name
    if not isinstance(name, str) or not name.strip():
        name = root.name
    # The display name goes into a Markdown heading; keep it one line.
    display = " ".join(name.strip().splitlines())
    return vault_component(identifier.strip(), "project_id"), display


def recent_docs(root, hours):
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others",
         "--exclude-standard", "-z"],
        check=True, capture_output=True,
    )
    cutoff = time.time() - hours * 3600
    for name in sorted(set(os.fsdecode(result.stdout).split("\0"))):
        relative = Path(name)
        if not name or relative.suffix.lower() != ".md":
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        if len(relative.parts) > 1 and relative.parts[0] not in ("docs", "_bmad-output"):
            continue
        source = root / relative
        if any(path.is_symlink() for path in (source, *source.parents)):
            continue
        if source.is_file() and source.stat().st_mtime >= cutoff:
            yield relative


def open_note(note):
    uri = "obsidian://open?" + urlencode({"path": str(note)}, quote_via=quote)
    if sys.platform == "win32":
        os.startfile(uri)
        return
    launcher = "open" if sys.platform == "darwin" else "xdg-open"
    executable = shutil.which(launcher)
    if not executable:
        raise RuntimeError(f"Cannot find {launcher}; use --no-open for a headless sync")
    subprocess.run([executable, uri], check=True)


def sync(root, vault, hours, no_open):
    if not vault.is_dir():
        raise RuntimeError(f"Obsidian vault does not exist: {vault}; set VAULT or pass --vault")
    if vault == root or root in vault.parents:
        raise RuntimeError("The vault must be outside the repository")
    project_id, display = project_identity(root)
    files = list(recent_docs(root, hours))
    if not files:
        print(f"No Markdown files modified in the last {hours:g} hours; nothing copied or opened.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    destination = vault / "Projects" / project_id / today
    destination.mkdir(parents=True, exist_ok=True)
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)

    # Include earlier copies from today too; narrower reruns never delete notes.
    links = []
    for note in sorted(destination.rglob("*")):
        if note.is_file() and note.suffix.lower() == ".md" and note != destination / INDEX:
            relative = note.relative_to(destination).as_posix()
            label = relative.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")
            links.append(f"- [{label}](<../{quote(relative, safe='/')}>)")
    index = destination / INDEX
    index.parent.mkdir(exist_ok=True)
    index.write_text(
        f"# {display} docs — {today}\n\n"
        f"Copied from `{root}`. Relative repository paths are preserved.\n\n"
        + "\n".join(links) + "\n",
        encoding="utf-8",
    )
    print(f"Synced {len(files)} Markdown files to {destination}", flush=True)
    print(f"Index: {index}", flush=True)
    if not no_open:
        open_note(index)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2],
                        help=argparse.SUPPRESS)
    parser.add_argument("--vault", type=Path,
                        default=os.environ.get("VAULT") or os.environ.get("OBSIDIAN_VAULT")
                        or str(Path.home() / "code/DeLoDocs"),
                        help="existing Obsidian vault (default: VAULT, OBSIDIAN_VAULT, ~/code/DeLoDocs)")
    parser.add_argument("--hours", type=positive_hours, default=24,
                        help="copy Markdown modified within this many hours (default: 24)")
    parser.add_argument("--no-open", action="store_true", help="copy without opening Obsidian")
    args = parser.parse_args()
    try:
        sync(args.root.expanduser().resolve(), args.vault.expanduser().resolve(),
             args.hours, args.no_open)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            detail = ": " + os.fsdecode(error.stderr).strip()
        print(f"docs:sync: {error}{detail}", file=sys.stderr)
        print("If copying succeeded but Obsidian could not open, retry with --no-open.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
