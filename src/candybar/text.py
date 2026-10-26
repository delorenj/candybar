"""Free text made safe for Postgres `text` columns and UTF-8 bus payloads.

Postgres rejects U+0000 in `text`, and a lone UTF-16 surrogate (U+D800 to
U+DFFF, e.g. from a JSON `"\\ud800"` escape) cannot be encoded as UTF-8 for
either the database or the bus. A child controls its stderr and its result
file message, so either one could poison the executor's terminal write: the
write fails, the invoke command is nak'd, and on the next delivery the job
runs again. Every child-supplied string goes through `scrub_text` before it
is stored or published. The replacement character matches the SDKs' own
scrub (Story 4.1) so both paths render the same.
"""

from __future__ import annotations

REPLACEMENT = "�"


def _unsafe(c: str) -> bool:
    return c == "\x00" or "\ud800" <= c <= "\udfff"


def scrub_text(value: str) -> str:
    """Replace NUL and lone surrogates with U+FFFD; everything else is kept."""
    if not any(_unsafe(c) for c in value):
        return value
    return "".join(REPLACEMENT if _unsafe(c) else c for c in value)


def scrub_optional(value: str | None) -> str | None:
    return None if value is None else scrub_text(value)


def has_unsafe_text(value: str) -> bool:
    """True when `value` holds a character Postgres or UTF-8 would reject."""
    return any(_unsafe(c) for c in value)


__all__ = ["REPLACEMENT", "has_unsafe_text", "scrub_optional", "scrub_text"]
