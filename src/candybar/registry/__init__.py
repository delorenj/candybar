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
from .models import (
    SKIP_KINDS,
    SKIP_OCCURRENCE,
    Base,
    Candybar,
    DispatchCursor,
    Invocation,
    Skip,
)
from .store import (
    INVOCATION_LIST_MAX,
    DuplicateInvocationError,
    DuplicateNameError,
    SkipEntry,
    TerminalInvocation,
    UnknownCandybarError,
    add_occurrence_skip,
    advance_dispatch_cursor,
    create_registry_engine,
    database_url,
    get_candybar,
    get_dispatch_cursor,
    get_entrypoint,
    get_invocation,
    insert_candybar,
    invocation_by_key,
    list_invocations,
    list_skips,
    name_taken,
    normalize_dsn,
    occurrence_skip_id,
    record_terminal_invocation,
    session_scope,
)

__all__ = [
    "INVOCATION_LIST_MAX",
    "MANIFEST_FILENAME",
    "SKIP_KINDS",
    "SKIP_OCCURRENCE",
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
    "SkipEntry",
    "TerminalInvocation",
    "UnknownCandybarError",
    "add_occurrence_skip",
    "advance_dispatch_cursor",
    "create_registry_engine",
    "database_url",
    "get_candybar",
    "get_dispatch_cursor",
    "get_entrypoint",
    "get_invocation",
    "insert_candybar",
    "invocation_by_key",
    "list_invocations",
    "list_skips",
    "load_manifest",
    "name_taken",
    "normalize_dsn",
    "occurrence_skip_id",
    "record_terminal_invocation",
    "session_scope",
    "uuid7",
]
