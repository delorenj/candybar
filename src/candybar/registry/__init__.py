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
from .models import Base, Candybar, DispatchCursor, Invocation, Skip
from .store import (
    DuplicateInvocationError,
    DuplicateNameError,
    TerminalInvocation,
    advance_dispatch_cursor,
    create_registry_engine,
    database_url,
    get_candybar,
    get_dispatch_cursor,
    get_entrypoint,
    insert_candybar,
    invocation_by_key,
    name_taken,
    normalize_dsn,
    record_terminal_invocation,
    session_scope,
)

__all__ = [
    "MANIFEST_FILENAME",
    "SLUG_RE",
    "Base",
    "Candybar",
    "CandybarManifest",
    "DispatchCursor",
    "DuplicateInvocationError",
    "DuplicateNameError",
    "Invocation",
    "ManifestError",
    "Skip",
    "TerminalInvocation",
    "advance_dispatch_cursor",
    "create_registry_engine",
    "database_url",
    "get_candybar",
    "get_dispatch_cursor",
    "get_entrypoint",
    "insert_candybar",
    "invocation_by_key",
    "load_manifest",
    "name_taken",
    "normalize_dsn",
    "record_terminal_invocation",
    "session_scope",
    "uuid7",
]
