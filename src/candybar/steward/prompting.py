"""Answer extraction and prompt templates (steward-contract.md section 4, steps 3-4).

Templates are versioned files in `deploy/steward/templates/<action>.md`; the
YAML-ish front matter carries `template_version: N` (changing a template
bumps it, and an enrich stores the version that produced its overview). The
body uses `{{placeholder}}` slots the steward fills. Every template ends by
asking for exactly one fenced ```json block; `last_json_block` reads the last
such block of the final answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATES_DIR_ENV = "CANDYBAR_STEWARD_TEMPLATES"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TEMPLATES_DIR = REPO_ROOT / "deploy" / "steward" / "templates"

_FENCE = re.compile(r"```[ \t]*json[^\n]*\n(.*?)\n?```", re.DOTALL | re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


class AnswerError(ValueError):
    """The agent's final answer has no usable JSON block."""


def last_json_block(answers: list[str]) -> dict[str, Any]:
    """The last fenced ```json object of the newest answer that has one.

    `answers` are the turn's assistant texts, newest first: the final answer
    normally carries the block; an agent that adds a closing line after it
    is still read correctly from the message before.
    """
    for text in answers:
        blocks = _FENCE.findall(text)
        for raw in reversed(blocks):
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
        if blocks:
            raise AnswerError("the final ```json block is not a JSON object")
    raise AnswerError("the final answer has no fenced ```json block")


@dataclass(frozen=True)
class Template:
    action: str
    version: int
    body: str

    def render(self, values: dict[str, str]) -> str:
        missing = sorted({m for m in _PLACEHOLDER.findall(self.body) if m not in values})
        if missing:
            raise KeyError(f"template {self.action} needs {missing}")
        return _PLACEHOLDER.sub(lambda m: values[m.group(1)], self.body)


def templates_dir() -> Path:
    import os

    override = os.environ.get(TEMPLATES_DIR_ENV)
    return Path(override).expanduser() if override else DEFAULT_TEMPLATES_DIR


def load_template(action: str, directory: Path | None = None) -> Template:
    path = (directory or templates_dir()) / f"{action}.md"
    text = path.read_text(encoding="utf-8")
    front = _FRONT.match(text)
    if front is None:
        raise ValueError(f"{path} has no front matter")
    version = None
    for line in front.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "template_version":
            version = int(value.strip())
    if version is None or version < 1:
        raise ValueError(f"{path} has no template_version")
    return Template(action=action, version=version, body=text[front.end() :].lstrip("\n"))
