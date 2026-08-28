#!/usr/bin/env python3
"""Fail closed when a runtime tree contains likely credential material."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

KEY_VALUE = re.compile(
    r"(?im)^\s*[\"']?(?P<key>api[_-]?key|token|secret|password|authorization|cookie|client[_-]?secret|private[_-]?key)[\"']?\s*[:=]\s*(?P<value>[^\r\n#]*)"
)
TOKEN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|tvly-[A-Za-z0-9_-]{10,}|fc-[A-Za-z0-9_-]{10,}|[0-9]{6,}:[A-Za-z0-9_-]{20,})"
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
SAFE_VALUES = {
    "openrouter",
    "openai",
    "anthropic",
    "auto",
    "none",
    "null",
    "false",
    "true",
}
SAFE_PREFIXES = ("${", "$", "op://", "env:", "[REDACTED]", "{{", "{%")
COMPUTED_VALUE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*\s*(?:\(|\[)")
INTERPOLATED_VALUE = re.compile(
    r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_.]*\})"
)
ENV_ASSIGNMENT = re.compile(
    r"(?m)^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>[^\r\n#]*)"
)
SENSITIVE_ENV_KEY = re.compile(
    r"(?i)(?:^|_)(?:api_key|token|secret|password|authorization|cookie|client_secret|private_key)(?:_|$)"
)
ALLOWED_ENV_FILES = {".env.example", ".env.op"}
SKIP_PARTS = {
    ".cache",
    ".direnv",
    ".eggs",
    ".git",
    ".mypy_cache",
    ".nox",
    ".npm",
    ".pnpm-store",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".turbo",
    ".uv-cache",
    ".venv",
    ".yarn",
    "__pycache__",
    "dist-packages",
    "node_modules",
    "site-packages",
    "venv",
}


def safe_assignment(raw_value: str) -> bool:
    """Return true only for an explicit reference, sentinel, or computation."""

    value = raw_value.strip().rstrip(",").strip().strip("\"'")
    if not value or value.lower() in SAFE_VALUES:
        return True
    if value.startswith(SAFE_PREFIXES) or value.endswith("_ENV"):
        return True
    if INTERPOLATED_VALUE.search(value):
        return True
    # Source code commonly binds credentials from a runtime provider. A bare
    # literal does not have a call/index boundary and remains fail-closed.
    return COMPUTED_VALUE.match(value) is not None


def safe_env_reference(raw_value: str) -> bool:
    """Keep a tracked .env.op declarative: sensitive values stay references."""

    value = raw_value.strip().rstrip(",").strip().strip("\"'")
    return not value or value.startswith(SAFE_PREFIXES)


def findings(root: Path) -> list[str]:
    result: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not path.is_file() or any(part in SKIP_PARTS for part in relative.parts):
            continue
        if path.name == "secret-scan.py":
            continue
        if path.name.startswith(".env") and path.name not in ALLOWED_ENV_FILES:
            result.append(f"forbidden secret file: {relative}")
            continue
        if path.name in {"auth.json", "auth.lock"} or path.suffix in {".pem", ".key"}:
            result.append(f"forbidden credential file: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if TOKEN.search(text) or PRIVATE_KEY.search(text):
            result.append(f"credential token pattern: {relative}")
        if path.name == ".env.op":
            for match in ENV_ASSIGNMENT.finditer(text):
                if SENSITIVE_ENV_KEY.search(
                    match.group("key")
                ) and not safe_env_reference(match.group("value")):
                    result.append(f"literal credential assignment: {relative}")
                    break
        for match in KEY_VALUE.finditer(text):
            if safe_assignment(match.group("value")):
                continue
            result.append(f"literal credential assignment: {relative}")
            break
    return sorted(set(result))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    issues = findings(args.root.resolve())
    if issues:
        for issue in issues:
            print(issue)
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
