#!/usr/bin/env python3
"""Parse the supported fleet.env assignment grammar without executing it."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import re
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Mapping, NamedTuple


HEADER = b"PJANGLER_FLEET_ENV_V1"
FOOTER = b"PJANGLER_FLEET_ENV_END"
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGNMENT = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=")
LEGACY_EXPANSION_NAME = "HERMES_FLEET_HOME"
LEGACY_EXPANSION = re.compile(
    r"\$(?:\{HERMES_FLEET_HOME\}|HERMES_FLEET_HOME(?![A-Za-z0-9_]))"
)
UNICODE_ERROR = "fleet environment parse error: input and values must be valid UTF-8 Unicode"
INITIAL_DOCUMENT = (
    "# Hermes fleet source of truth.\n"
    "# All generated wrappers and provisioning scripts read this file.\n"
)


class FleetEnvParseError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")


class FleetEnvRecoveryError(OSError):
    """A concurrent destination was preserved after rollback could not finish."""

    def __init__(self, recovery_path: Path) -> None:
        super().__init__(
            "fleet environment destination changed and recovery failed; "
            "concurrent data preserved"
        )
        self.recovery_path = recovery_path


class ParsedRecord(NamedTuple):
    key: str
    value: str
    start: int
    end: int


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def line_end(text: str, offset: int) -> int:
    end = text.find("\n", offset)
    return len(text) if end < 0 else end


def next_line(text: str, end: int) -> int:
    return end if end == len(text) else end + 1


def validate_suffix(text: str, offset: int, origin: int) -> int:
    end = line_end(text, offset)
    suffix = text[offset:end]
    if not re.fullmatch(r"[ \t]*(?:#.*)?", suffix):
        raise FleetEnvParseError(
            line_number(text, origin), "unexpected content after quoted value"
        )
    return next_line(text, end)


def parse_single_quoted(text: str, offset: int) -> tuple[str, int]:
    origin = offset
    closing = text.find("'", offset + 1)
    if closing < 0:
        raise FleetEnvParseError(line_number(text, origin), "unterminated single quote")
    return text[offset + 1 : closing], validate_suffix(text, closing + 1, origin)


def parse_double_quoted(text: str, offset: int) -> tuple[str, int]:
    origin = offset
    cursor = offset + 1
    value: list[str] = []
    while cursor < len(text):
        char = text[cursor]
        if char == '"':
            return "".join(value), validate_suffix(text, cursor + 1, origin)
        if char in {"$", "`"}:
            raise FleetEnvParseError(
                line_number(text, cursor),
                "dynamic expansion is not supported in fleet.env",
            )
        if char != "\\":
            value.append(char)
            cursor += 1
            continue
        if cursor + 1 >= len(text):
            raise FleetEnvParseError(line_number(text, cursor), "unterminated escape")
        escaped = text[cursor + 1]
        if escaped == "\n":
            cursor += 2
            continue
        if escaped in {'"', "\\", "$", "`"}:
            value.append(escaped)
        else:
            value.extend(("\\", escaped))
        cursor += 2
    raise FleetEnvParseError(line_number(text, origin), "unterminated double quote")


ANSI_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}


def parse_hex_escape(text: str, offset: int, maximum: int) -> tuple[str, int]:
    cursor = offset
    while (
        cursor < len(text)
        and cursor - offset < maximum
        and text[cursor] in "0123456789abcdefABCDEF"
    ):
        cursor += 1
    if cursor == offset:
        raise FleetEnvParseError(line_number(text, offset), "empty hexadecimal escape")
    return chr(int(text[offset:cursor], 16)), cursor


def parse_ansi_c_quoted(text: str, offset: int) -> tuple[str, int]:
    origin = offset
    cursor = offset + 2
    value: list[str] = []
    while cursor < len(text):
        char = text[cursor]
        if char == "'":
            parsed = "".join(value)
            if "\0" in parsed:
                raise FleetEnvParseError(
                    line_number(text, origin), "NUL is not allowed"
                )
            return parsed, validate_suffix(text, cursor + 1, origin)
        if char != "\\":
            value.append(char)
            cursor += 1
            continue
        if cursor + 1 >= len(text):
            raise FleetEnvParseError(line_number(text, cursor), "unterminated escape")
        escaped = text[cursor + 1]
        if escaped == "\n":
            cursor += 2
            continue
        if escaped in ANSI_ESCAPES:
            value.append(ANSI_ESCAPES[escaped])
            cursor += 2
            continue
        if escaped == "x":
            decoded, cursor = parse_hex_escape(text, cursor + 2, 2)
            value.append(decoded)
            continue
        if escaped in {"u", "U"}:
            width = 4 if escaped == "u" else 8
            start = cursor + 2
            digits = text[start : start + width]
            if len(digits) != width or any(
                c not in "0123456789abcdefABCDEF" for c in digits
            ):
                raise FleetEnvParseError(
                    line_number(text, cursor), "invalid Unicode escape"
                )
            try:
                value.append(chr(int(digits, 16)))
            except ValueError as error:
                raise FleetEnvParseError(
                    line_number(text, cursor), "invalid Unicode code point"
                ) from error
            cursor = start + width
            continue
        if escaped in "01234567":
            start = cursor + 1
            end = start
            while end < len(text) and end - start < 3 and text[end] in "01234567":
                end += 1
            value.append(chr(int(text[start:end], 8)))
            cursor = end
            continue
        value.extend(("\\", escaped))
        cursor += 2
    raise FleetEnvParseError(line_number(text, origin), "unterminated ANSI-C quote")


def parse_unquoted(
    text: str,
    offset: int,
    expansion_values: Mapping[str, str],
) -> tuple[str, int]:
    end = line_end(text, offset)
    raw = text[offset:end]
    comment = re.search(r"[ \t]+#", raw)
    if comment:
        raw = raw[: comment.start()]
    if not raw:
        return "", next_line(text, end)
    if re.search(r"[ \t;&|<>()`\\'\"]", raw):
        raise FleetEnvParseError(
            line_number(text, offset), "unquoted value contains shell syntax"
        )
    if "$" not in raw:
        return raw, next_line(text, end)

    # Compatibility is intentionally narrow: exactly one allowed token must be
    # the first byte, followed only by the already-validated literal suffix.
    # Prefixes and repeated/partial tokens would turn this into a general
    # expansion language and are therefore rejected.
    match = LEGACY_EXPANSION.match(raw)
    if match is None or match.start() != 0:
        raise FleetEnvParseError(
            line_number(text, offset),
            "dynamic expansion is not supported in fleet.env",
        )
    suffix = raw[match.end() :]
    if "$" in suffix:
        raise FleetEnvParseError(
            line_number(text, offset),
            "dynamic expansion is not supported in fleet.env",
        )
    value = expansion_values.get(LEGACY_EXPANSION_NAME)
    if value is None:
        raise FleetEnvParseError(
            line_number(text, offset),
            "legacy HERMES_FLEET_HOME expansion has no value",
        )
    return value + suffix, next_line(text, end)


def parse_value(
    text: str,
    offset: int,
    expansion_values: Mapping[str, str],
) -> tuple[str, int]:
    if offset >= len(text) or text[offset] == "\n":
        end = line_end(text, offset)
        return "", next_line(text, end)
    if text.startswith("$'", offset):
        return parse_ansi_c_quoted(text, offset)
    if text[offset] == "'":
        return parse_single_quoted(text, offset)
    if text[offset] == '"':
        return parse_double_quoted(text, offset)
    return parse_unquoted(text, offset, expansion_values)


def normalize_text(text: str) -> str:
    if "\0" in text:
        raise FleetEnvParseError(1, "NUL is not allowed")
    if "\r" in text:
        text = text.replace("\r\n", "\n")
        if "\r" in text:
            raise FleetEnvParseError(1, "bare carriage return is not supported")
    return text


def validate_unicode(value: str) -> None:
    for char in value:
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise UnicodeError("surrogate code point")
        if 0xFDD0 <= codepoint <= 0xFDEF or codepoint & 0xFFFE == 0xFFFE:
            raise UnicodeError("Unicode noncharacter")
    value.encode("utf-8", errors="strict")


def parse_document(
    text: str,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, list[ParsedRecord]]:
    text = normalize_text(text)
    inherited = environment if environment is not None else os.environ
    expansion_values: dict[str, str] = {}
    caller_has_fleet_home = LEGACY_EXPANSION_NAME in inherited
    if caller_has_fleet_home:
        expansion_values[LEGACY_EXPANSION_NAME] = inherited[LEGACY_EXPANSION_NAME]

    cursor = 0
    records: list[ParsedRecord] = []
    seen: set[str] = set()
    while cursor < len(text):
        origin = cursor
        end = line_end(text, cursor)
        physical = text[cursor:end]
        if not physical.strip() or physical.lstrip().startswith("#"):
            cursor = next_line(text, end)
            continue
        match = ASSIGNMENT.match(text, cursor)
        if not match or match.end() > end:
            raise FleetEnvParseError(
                line_number(text, cursor), "expected KEY=value or export KEY=value"
            )
        key = match.group(1)
        if not NAME.fullmatch(key):
            raise FleetEnvParseError(line_number(text, cursor), "invalid variable name")
        if key in seen:
            raise FleetEnvParseError(
                line_number(text, cursor), f"duplicate variable {key}"
            )
        value, cursor = parse_value(text, match.end(), expansion_values)
        validate_unicode(value)
        seen.add(key)
        records.append(ParsedRecord(key, value, origin, cursor))
        if key == LEGACY_EXPANSION_NAME and not caller_has_fleet_home:
            expansion_values[key] = value
    return text, records


def parse(
    text: str,
    environment: Mapping[str, str] | None = None,
) -> list[tuple[str, str]]:
    _, records = parse_document(text, environment)
    return [(record.key, record.value) for record in records]


def serialize_literal(value: str) -> str:
    validate_unicode(value)
    escaped: list[str] = []
    simple_escapes = {
        "\\": r"\\",
        "'": r"\'",
        "\a": r"\a",
        "\b": r"\b",
        "\x1b": r"\e",
        "\f": r"\f",
        "\n": r"\n",
        "\r": r"\r",
        "\t": r"\t",
        "\v": r"\v",
    }
    for char in value:
        if char in simple_escapes:
            escaped.append(simple_escapes[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            escaped.append(f"\\x{ord(char):02x}")
        else:
            escaped.append(char)
    return "$'" + "".join(escaped) + "'"


def serialize_systemd_value(value: str) -> str:
    """Return one lossless, injection-safe systemd.syntax scalar."""
    validate_unicode(value)
    if "\0" in value:
        raise FleetEnvParseError(1, "NUL is not allowed in a systemd value")
    if "\r" in value or "\n" in value:
        raise FleetEnvParseError(1, "newline control characters are not allowed in a systemd value")
    escaped: list[str] = []
    for char in value:
        if char == "\\":
            escaped.append(r"\\")
        elif char == '"':
            escaped.append(r'\"')
        elif char == "%":
            # systemd specifiers are expanded after syntax unquoting. Doubling
            # preserves a caller-provided percent byte as literal data.
            escaped.append("%%")
        elif char == "\t":
            escaped.append(r"\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            escaped.append(f"\\x{ord(char):02x}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def serialize_systemd_scalar(value: str) -> str:
    """Return a scalar directive value without introducing literal quotes."""
    validate_unicode(value)
    if "\0" in value:
        raise FleetEnvParseError(1, "NUL is not allowed in a systemd value")
    if "\r" in value or "\n" in value:
        raise FleetEnvParseError(1, "newline control characters are not allowed in a systemd value")
    escaped: list[str] = []
    for char in value:
        if char == "\\":
            escaped.append(r"\\")
        elif char == "%":
            escaped.append("%%")
        elif char == "\t":
            escaped.append(r"\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            escaped.append(f"\\x{ord(char):02x}")
        else:
            escaped.append(char)
    return "".join(escaped)


def serialize_systemd_environment(name: str, value: str) -> str:
    if not NAME.fullmatch(name):
        raise FleetEnvParseError(1, "invalid systemd environment variable name")
    return f"Environment={serialize_systemd_value(f'{name}={value}')}"


def serialize_systemd_exec_value(value: str) -> str:
    """Quote one ExecStart token while suppressing systemd $/%% expansion."""
    return serialize_systemd_value(value.replace("$", "$$"))


def read_regular_document(
    path: Path,
    *,
    allow_missing: bool = False,
) -> tuple[str, os.stat_result | None]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if allow_missing:
            return INITIAL_DOCUMENT, None
        raise FleetEnvParseError(1, "fleet environment path is unavailable")
    except OSError as error:
        raise FleetEnvParseError(1, "fleet environment path must be a regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FleetEnvParseError(1, "fleet environment path must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read()
        after = os.fstat(descriptor)
        if not _same_file_metadata(metadata, after):
            raise FleetEnvParseError(1, "fleet environment changed while it was read")
        return content.decode("utf-8"), after
    finally:
        os.close(descriptor)


def write_atomic_document(
    path: Path,
    content: str,
    original: os.stat_result | None,
    original_content: bytes | None = None,
) -> None:
    parent = path.parent
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=parent,
    )
    temporary = Path(temporary_name)
    preserve_temporary = False
    try:
        desired_mode = stat.S_IMODE(original.st_mode) if original else 0o600
        if original is not None:
            # chown(2) may clear set-ID mode bits. Establish ownership first,
            # then restore the exact original mode and attest both properties
            # on the prepared inode before it can participate in an exchange.
            # Permission failures are not an excuse to silently drift metadata.
            os.fchown(descriptor, original.st_uid, original.st_gid)
        encoded = content.encode("utf-8", errors="strict")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
        # A content write can also clear set-ID bits. This is therefore the
        # final metadata operation before attestation and atomic commit.
        os.fchmod(descriptor, desired_mode)
        prepared = os.fstat(descriptor)
        if original is not None and (
            prepared.st_uid != original.st_uid
            or prepared.st_gid != original.st_gid
        ):
            raise OSError("fleet environment ownership could not be preserved")
        if stat.S_IMODE(prepared.st_mode) != desired_mode:
            raise OSError("fleet environment mode could not be preserved")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1

        if original is None:
            # link(2) supplies portable no-clobber creation. The temporary and
            # destination share a directory/filesystem, so successful linking
            # commits the fully-written inode without a check/use gap.
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        else:
            # Linux renameat2(RENAME_EXCHANGE) swaps the prepared file and the
            # current destination in one syscall. Only after that atomic claim
            # do we inspect the displaced inode. A concurrent replacement is
            # immediately exchanged back and reported, never overwritten.
            # Platforms without atomic exchange fail closed before mutation;
            # there is no portable conditional-replace primitive for this CAS.
            _exchange_paths(temporary, path)
            displaced = os.lstat(temporary)
            if original_content is None or not _matches_file_snapshot(
                temporary,
                original,
                original_content,
            ):
                try:
                    _exchange_paths(temporary, path)
                except OSError as recovery_error:
                    # After the failed reverse exchange, `path` contains our
                    # prepared update and `temporary` contains the concurrent
                    # replacement. From this point onward the temporary name is
                    # data, not scratch: never let the generic finally cleanup
                    # unlink it. Give the displaced inode a deterministic,
                    # restrictive recovery name when the filesystem permits,
                    # otherwise retain the exact mkstemp name in exception
                    # metadata for operator recovery.
                    preserve_temporary = True
                    recovery_path = _preserve_displaced_recovery(
                        temporary,
                        path,
                        displaced,
                    )
                    raise FleetEnvRecoveryError(recovery_path) from recovery_error
                raise OSError("fleet environment destination changed during update")
            temporary.unlink()

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not preserve_temporary:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _same_file_metadata(expected: os.stat_result, actual: os.stat_result) -> bool:
    """Compare the stable identity, content-version, and security metadata."""
    return (
        stat.S_ISREG(actual.st_mode)
        and (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
        and actual.st_mode == expected.st_mode
        and actual.st_uid == expected.st_uid
        and actual.st_gid == expected.st_gid
        and actual.st_size == expected.st_size
        and actual.st_mtime_ns == expected.st_mtime_ns
    )


def _matches_file_snapshot(
    path: Path,
    expected: os.stat_result,
    expected_content: bytes,
) -> bool:
    """Attest one displaced inode without following a replacement symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    try:
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return (
        _same_file_metadata(expected, before)
        and _same_file_metadata(before, after)
        and b"".join(chunks) == expected_content
    )


def _remove_recovery_temporary(path: Path) -> bool:
    """Best-effort cleanup after a durable recovery link was established."""
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _preserve_displaced_recovery(
    temporary: Path,
    destination: Path,
    displaced: os.stat_result,
) -> Path:
    """Keep a displaced concurrent inode reachable without overwriting data."""
    if stat.S_ISREG(displaced.st_mode):
        descriptor = os.open(temporary, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fchmod(descriptor, 0o600)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.close(descriptor)
        fingerprint = digest.hexdigest()[:16]
    else:
        fingerprint = f"mode-{stat.S_IFMT(displaced.st_mode):x}"

    recovery = destination.parent / (
        f".{destination.name}.pjangler-recovery-"
        f"{displaced.st_dev:x}-{displaced.st_ino:x}-{fingerprint}"
    )
    try:
        os.link(temporary, recovery, follow_symlinks=False)
    except FileExistsError:
        existing = os.lstat(recovery)
        if (existing.st_dev, existing.st_ino) != (displaced.st_dev, displaced.st_ino):
            return temporary
    except OSError:
        return temporary
    _remove_recovery_temporary(temporary)
    return recovery


def _exchange_paths(left: Path, right: Path) -> None:
    """Atomically exchange two paths, or fail without a portability downgrade."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, "atomic path exchange is unavailable") from error
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_exchange = 2
    if renameat2(
        at_fdcwd,
        os.fsencode(left),
        at_fdcwd,
        os.fsencode(right),
        rename_exchange,
    ) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def render_upsert(
    text: str,
    key: str,
    value: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    if not NAME.fullmatch(key):
        raise FleetEnvParseError(1, "invalid variable name")
    normalized, records = parse_document(text, environment)
    matches = [record for record in records if record.key == key]
    if len(matches) > 1:
        raise FleetEnvParseError(1, f"duplicate variable {key}")

    replacement = f"{key}={serialize_literal(value)}\n"
    if matches:
        record = matches[0]
        updated = normalized[: record.start] + replacement + normalized[record.end :]
    else:
        separator = "" if not normalized or normalized.endswith("\n") else "\n"
        updated = normalized + separator + replacement

    # Validate the complete prospective document before opening a temporary
    # output. This prevents a malformed legacy record from being partially
    # repaired or hidden by an otherwise valid upsert.
    parse_document(updated, environment)
    return updated


def atomic_upsert(path: Path, key: str, value: str) -> None:
    text, original = read_regular_document(path, allow_missing=True)
    updated = render_upsert(text, key, value)
    original_content = text.encode("utf-8", errors="strict") if original is not None else None
    write_atomic_document(path, updated, original, original_content)


def emit_records(records: list[tuple[str, str]]) -> None:
    framed = [HEADER]
    framed.extend(f"{key}={value}".encode("utf-8") for key, value in records)
    framed.extend((FOOTER, b"", b""))
    sys.stdout.buffer.write(b"\0".join(framed))


def main() -> int:
    parse_mode = len(sys.argv) == 2
    upsert_mode = len(sys.argv) == 5 and sys.argv[1] == "--upsert"
    systemd_value_mode = len(sys.argv) == 3 and sys.argv[1] == "--systemd-value"
    systemd_scalar_mode = len(sys.argv) == 3 and sys.argv[1] == "--systemd-scalar"
    systemd_exec_value_mode = len(sys.argv) == 3 and sys.argv[1] == "--systemd-exec-value"
    systemd_environment_mode = len(sys.argv) == 4 and sys.argv[1] == "--systemd-environment"
    if not parse_mode and not upsert_mode and not systemd_value_mode and not systemd_scalar_mode and not systemd_exec_value_mode and not systemd_environment_mode:
        print(
            "usage: parse-fleet-env.py PATH | --upsert PATH KEY VALUE | "
            "--systemd-value VALUE | --systemd-scalar VALUE | "
            "--systemd-exec-value VALUE | --systemd-environment NAME VALUE",
            file=sys.stderr,
        )
        return 2
    try:
        if systemd_value_mode:
            print(serialize_systemd_value(sys.argv[2]), end="")
            return 0
        if systemd_scalar_mode:
            print(serialize_systemd_scalar(sys.argv[2]), end="")
            return 0
        if systemd_exec_value_mode:
            print(serialize_systemd_exec_value(sys.argv[2]), end="")
            return 0
        if systemd_environment_mode:
            print(serialize_systemd_environment(sys.argv[2], sys.argv[3]), end="")
            return 0
        if upsert_mode:
            atomic_upsert(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
            return 0
        path = Path(sys.argv[1])
        text, _ = read_regular_document(path)
        records = parse(text)
        emit_records(records)
    except UnicodeError:
        print(UNICODE_ERROR, file=sys.stderr)
        return 2
    except FleetEnvParseError as error:
        print(f"fleet environment parse error: {error}", file=sys.stderr)
        return 2
    except FleetEnvRecoveryError:
        print(
            "fleet environment write error: concurrent replacement preserved "
            "for operator recovery",
            file=sys.stderr,
        )
        return 2
    except OSError:
        print("fleet environment write error: update was not committed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
