"""The steward notifier: finished tasks reach the operator (steward/notify.py)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from candybar.events import build_steward_task_fact
from candybar.events.builders import STEWARD_FACT_FOR_STATUS
from candybar.events.envelope import StewardTaskData
from candybar.steward.notify import (
    DEFAULT_PROFILE,
    DEFAULT_TARGET,
    NOTIFY_MAX_DELIVERIES,
    TARGET_ENV,
    NotifyConfig,
    StewardNotifier,
    render,
)

CB_ID = UUID("01a1794c-0000-7000-8000-000000000001")
CONFIG = NotifyConfig(
    target="telegram:1", profile="p", holocene="https://h.example/candybar", reports_dir=Path("/r")
)


def data(status: str = "succeeded", **kw: Any) -> StewardTaskData:
    base: dict[str, Any] = {
        "task_id": uuid4(), "candybar_id": CB_ID, "name": "skillex-hermes-resync", "action": "evaluate", "status": status,
        "model": "automaticai/auto", "created_at": datetime(2026, 11, 4, 22, 2, tzinfo=UTC),
    }
    base.update(kw)
    return StewardTaskData(**base)


class Msg:
    def __init__(self, raw: bytes, deliveries: int = 1) -> None:
        self.data = raw
        self.metadata = SimpleNamespace(num_delivered=deliveries)
        self.acks: list[str] = []

    async def ack(self) -> None:
        self.acks.append("ack")

    async def nak(self, delay: float | None = None) -> None:
        self.acks.append(f"nak:{delay}")

    async def term(self) -> None:
        self.acks.append("term")


def msg_for(task: StewardTaskData, deliveries: int = 1) -> Msg:
    fact = build_steward_task_fact(STEWARD_FACT_FOR_STATUS[task.status], task)
    return Msg(fact.model_dump_json().encode(), deliveries)


class Sender:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail

    def __call__(self, profile: str, target: str, message: str, timeout: float) -> None:
        if self.fail:
            raise RuntimeError("notification was not confirmed")
        self.sent.append((profile, target, message))


def handle(msg: Msg, sender: Sender) -> None:
    asyncio.run(StewardNotifier(CONFIG, sender).handle_message(msg))


def test_evaluate_with_attention_outcome_names_report_and_link() -> None:
    task = data(outcome="Drifted", summary="Blocked, not obsolete.", report_key="2026-11-04/candybar-evaluate-x.md")
    text = render(task, CONFIG)
    assert text is not None
    assert text.startswith("⚠️ Candybar evaluate · skillex-hermes-resync → Drifted")
    assert "Blocked, not obsolete." in text
    assert "Report: /r/2026-11-04/candybar-evaluate-x.md" in text
    assert f"Open: https://h.example/candybar?tab=definitions&open={CB_ID}" in text
    assert str(task.task_id) in text


@pytest.mark.parametrize(("action", "outcome"), [("evaluate", "Nominal"), ("fix", "fixed"), ("enrich", "done")])
def test_good_outcomes_get_a_check(action: str, outcome: str) -> None:
    text = render(data(action=action, outcome=outcome, summary="ok"), CONFIG)
    assert text is not None and text.startswith("✅")


def test_follow_up_names_its_evaluate_and_failure_shows_the_error() -> None:
    parent = uuid4()
    text = render(data("failed", action="fix", parent_task_id=parent, error="opencode timed out"), CONFIG)
    assert text is not None
    assert text.startswith("❌ Candybar fix · skillex-hermes-resync → failed")
    assert f"follow-up of evaluate {str(parent)[:8]}" in text
    assert "opencode timed out" in text


def test_operator_cancel_is_not_announced() -> None:
    assert render(data("failed", error="cancelled by operator"), CONFIG) is None


def test_completed_fact_is_sent_then_acked() -> None:
    sender = Sender()
    msg = msg_for(data(outcome="Drifted", summary="s"))
    handle(msg, sender)
    assert msg.acks == ["ack"]
    assert [(p, t) for p, t, _ in sender.sent] == [("p", "telegram:1")]


def test_queued_and_started_facts_are_acked_silently() -> None:
    for status in ("queued", "running"):
        sender, msg = Sender(), msg_for(data(status))
        handle(msg, sender)
        assert msg.acks == ["ack"] and sender.sent == []


def test_send_failure_retries_with_backoff_then_gives_up() -> None:
    msg = msg_for(data(outcome="Nominal"), deliveries=2)
    handle(msg, Sender(fail=True))
    assert msg.acks == ["nak:60"]
    last = msg_for(data(outcome="Nominal"), deliveries=NOTIFY_MAX_DELIVERIES)
    handle(last, Sender(fail=True))
    assert last.acks == ["ack"]


def test_unparseable_fact_is_terminated() -> None:
    msg = Msg(b"{not json")
    handle(msg, Sender())
    assert msg.acks == ["term"]


def test_config_defaults_and_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TARGET_ENV, raising=False)
    config = NotifyConfig.from_env()
    assert config is not None and (config.target, config.profile) == (DEFAULT_TARGET, DEFAULT_PROFILE)
    monkeypatch.setenv(TARGET_ENV, "off")
    assert NotifyConfig.from_env() is None
