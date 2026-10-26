"""Registry: Postgres access layer — the sole writer class for the candybar database (AD-10).

Bootstrapped by Story 1.4: SQLAlchemy models (AD-10 ERD), stdlib-only UUIDv7
ids, `candybar.toml` manifest parsing/validation (AD-8), and the async store
the api/dispatcher/executor roles call. No other module touches the database.
"""

from .ids import uuid7
from .manifest import (
    MANIFEST_FILENAME,
    SLUG_RE,
    CandybarManifest,
    ManifestError,
    load_manifest,
)
from .models import Base, Candybar, Invocation, Skip
from .store import (
    DuplicateNameError,
    create_registry_engine,
    database_url,
    insert_candybar,
    name_taken,
    normalize_dsn,
    session_scope,
)

__all__ = [
    "MANIFEST_FILENAME",
    "SLUG_RE",
    "Base",
    "Candybar",
    "CandybarManifest",
    "DuplicateNameError",
    "Invocation",
    "ManifestError",
    "Skip",
    "create_registry_engine",
    "database_url",
    "insert_candybar",
    "load_manifest",
    "name_taken",
    "normalize_dsn",
    "session_scope",
    "uuid7",
]
