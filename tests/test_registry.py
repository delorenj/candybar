"""Unit tests for the registry layer: uuid7, manifest validation, sole-writer guard.

No database or bus needed — validation is pure by design.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from candybar.registry import (
    SLUG_RE,
    CandybarManifest,
    ManifestError,
    load_manifest,
    normalize_dsn,
    uuid7,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "candybar"


# ---------------------------------------------------------------------------
# uuid7
# ---------------------------------------------------------------------------


def test_uuid7_version_and_variant() -> None:
    u = uuid7()
    assert u.version == 7
    assert u.variant == "specified in RFC 4122"


def test_uuid7_embeds_epoch_milliseconds() -> None:
    ms = 1_761_400_000_000  # arbitrary fixed instant
    u = uuid7(ms=ms, rand=0)
    # Top 48 bits of the UUID are the epoch milliseconds.
    assert u.int >> 80 == ms


def test_uuid7_orders_by_milliseconds() -> None:
    earlier = uuid7(ms=1_000, rand=(1 << 74) - 1)
    later = uuid7(ms=1_001, rand=0)
    assert earlier < later  # time-ordered even when rand runs backwards


def test_uuid7_rejects_out_of_range_fields() -> None:
    with pytest.raises(ValueError):
        uuid7(ms=1 << 48)
    with pytest.raises(ValueError):
        uuid7(rand=1 << 74)


# ---------------------------------------------------------------------------
# Slug + cron validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["a", "my-job", "j0b-1", "0k"])
def test_slug_accepts_valid_names(name: str) -> None:
    assert SLUG_RE.match(name)


@pytest.mark.parametrize(
    "name", ["A", "-lead", "trail-", "double--dash-ok".replace("--", "_"), "bad name", ""]
)
def test_slug_rejects_invalid_names(name: str) -> None:
    assert not SLUG_RE.match(name)


def _manifest(**overrides: object) -> CandybarManifest:
    base: dict[str, object] = {
        "name": "demo",
        "schedule": "*/5 * * * *",
        "entrypoint": "/bin/true",
    }
    base.update(overrides)
    return CandybarManifest.model_validate(base)


def test_manifest_defaults() -> None:
    m = _manifest()
    assert m.description == "" and m.tags == [] and m.noise is False


def test_manifest_rejects_invalid_name() -> None:
    with pytest.raises(ValueError, match="invalid candybar name"):
        _manifest(name="Bad_Name")


def test_manifest_rejects_garbage_cron() -> None:
    with pytest.raises(ValueError, match="invalid cron schedule"):
        _manifest(schedule="not a cron at all")


def test_manifest_rejects_six_field_cron() -> None:
    # croniter happily parses 6-field (seconds) expressions; we must not.
    with pytest.raises(ValueError, match="5-field"):
        _manifest(schedule="0 * * * * *")


@pytest.mark.parametrize(
    "schedule",
    ["R * * * *", "0 r * * *", "R(0-30) * * * *", "0 R/2 * * *", "0 0 * * r"],
)
def test_manifest_rejects_random_cron_fields(schedule: str) -> None:
    # croniter accepts its non-standard `R` token and re-rolls it on every
    # parse; the dispatcher parses per tick, so the job would fire at random.
    import croniter

    croniter.croniter(schedule)  # precondition: croniter itself accepts it
    with pytest.raises(ValueError, match="hashed/random"):
        _manifest(schedule=schedule)


@pytest.mark.parametrize("schedule", ["0 0 * mar fri", "0 0 * * 5#3", "0 0 L * *"])
def test_manifest_keeps_named_and_extended_fields(schedule: str) -> None:
    # Month/day names contain `r` (mar, apr, fri): only whole R/H tokens are refused.
    assert _manifest(schedule=schedule).schedule == schedule


def test_manifest_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        _manifest(schedul="* * * * *")  # typo must not silently pass


# ---------------------------------------------------------------------------
# Manifest file loading
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, body: str, *, with_entrypoint: bool = True) -> Path:
    if with_entrypoint:
        (tmp_path / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tmp_path / "candybar.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_load_manifest_happy_path_resolves_entrypoint(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        """
[candybar]
name = "demo"
description = "a demo"
tags = ["a", "b"]
schedule = "*/5 * * * *"
entrypoint = "./run.sh"
""",
    )
    m = load_manifest(tmp_path)
    assert m.name == "demo"
    assert m.tags == ["a", "b"]
    assert m.entrypoint == str((tmp_path / "run.sh").resolve())


def test_load_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="no candybar.toml"):
        load_manifest(tmp_path)


def test_load_manifest_bad_toml(tmp_path: Path) -> None:
    (tmp_path / "candybar.toml").write_text("[candybar\nbroken", encoding="utf-8")
    with pytest.raises(ManifestError, match="invalid TOML"):
        load_manifest(tmp_path)


def test_load_manifest_missing_candybar_table(tmp_path: Path) -> None:
    (tmp_path / "candybar.toml").write_text("[other]\nname = 'x'\n", encoding="utf-8")
    with pytest.raises(ManifestError, match=r"\[candybar\]"):
        load_manifest(tmp_path)


def test_load_manifest_missing_required_key(tmp_path: Path) -> None:
    _write_manifest(tmp_path, '[candybar]\nname = "demo"\nentrypoint = "./run.sh"\n')
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_load_manifest_missing_entrypoint_file(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        '[candybar]\nname = "demo"\nschedule = "* * * * *"\nentrypoint = "./nope.sh"\n',
        with_entrypoint=False,
    )
    with pytest.raises(ManifestError, match="not an existing file"):
        load_manifest(tmp_path)


def test_load_manifest_rejects_directory_entrypoint(tmp_path: Path) -> None:
    # A directory "exists" — but the executor needs an executable FILE.
    (tmp_path / "subdir").mkdir()
    _write_manifest(
        tmp_path,
        '[candybar]\nname = "demo"\nschedule = "* * * * *"\nentrypoint = "./subdir"\n',
    )
    with pytest.raises(ManifestError, match="not an existing file"):
        load_manifest(tmp_path)


def test_manifest_rejects_empty_entrypoint() -> None:
    with pytest.raises(ValueError, match="entrypoint must not be empty"):
        _manifest(entrypoint="")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", "nul\x00here"),
        ("description", "lone \ud800 surrogate"),
        ("entrypoint", "/bin/tr\x00ue"),
        ("tags", ["ok", "bad\x00tag"]),
    ],
)
def test_manifest_rejects_text_postgres_cannot_store(field: str, value: object) -> None:
    """NUL (and lone surrogates) would 500 out of the insert/event: refuse as 422."""
    with pytest.raises(ValueError, match="NUL or lone surrogate"):
        _manifest(**{field: value})


# ---------------------------------------------------------------------------
# DSN normalization
# ---------------------------------------------------------------------------


def test_normalize_dsn_forces_psycopg3() -> None:
    assert normalize_dsn("postgresql://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalize_dsn("postgres://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalize_dsn("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"


# ---------------------------------------------------------------------------
# Sole-writer guard (AD-10): only registry/ may touch sqlalchemy/psycopg
# ---------------------------------------------------------------------------

DB_PATTERNS = [
    r"^\s*(from|import)\s+sqlalchemy",
    r"^\s*(from|import)\s+psycopg",
    r"^\s*(from|import)\s+alembic",
]
DB_RE = re.compile("|".join(DB_PATTERNS), re.MULTILINE)


def _offenders(root: Path, *, allow_registry: bool) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        in_registry = "registry" in path.relative_to(root).parts
        if in_registry == allow_registry:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if DB_RE.search(line):
                hits.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    return hits


def test_only_registry_touches_the_database() -> None:
    assert _offenders(SRC, allow_registry=True) == []


def test_sole_writer_guard_sanity_registry_does_touch_it() -> None:
    # The guard above is meaningless if nothing can ever match: the registry
    # module must contain DB imports for the scan to be real.
    assert _offenders(SRC, allow_registry=False) != []


def test_update_candybar_refuses_registry_owned_fields_before_touching_the_db() -> None:
    """Story 5.1: ids, timestamps and counters are never caller-updatable."""
    import asyncio
    from types import SimpleNamespace

    from candybar.registry import UPDATABLE_FIELDS, update_candybar

    assert set(UPDATABLE_FIELDS) == {
        "name", "description", "tags", "schedule", "timezone", "entrypoint", "runner", "noise",
        "enabled",
    }
    row = SimpleNamespace(name="x", total_invoked=0)
    for field in ("id", "total_invoked", "created_at"):
        with pytest.raises(ValueError, match="not updatable"):
            asyncio.run(update_candybar(None, row, {field: 1}))  # type: ignore[arg-type]
    assert row.total_invoked == 0


def test_manifest_rejects_blank_tags() -> None:
    """A blank tag would break the created/updated event payload (NonBlank tags)."""
    for tags in ([""], ["ok", "   "]):
        with pytest.raises(ValueError, match="non-blank"):
            CandybarManifest(name="t", schedule="* * * * *", entrypoint="/bin/true", tags=tags)
    assert CandybarManifest(name="t", schedule="* * * * *", entrypoint="/bin/true",
                            tags=["a"]).tags == ["a"]
