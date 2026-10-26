"""Round-trip and contract-shape tests for the bloodbank.cron.* vocabulary (Story 1.2)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from candybar.events import (
    ALL_TYPES,
    COMMAND_TYPES,
    EVENT_TYPES,
    STDERR_TAIL_MAX,
    build_candybar_completed,
    build_candybar_created,
    build_candybar_failed,
    build_candybar_invoke,
    build_candybar_invoked,
    build_candybar_skipped,
    build_candybar_updated,
    build_tick_fired,
    parse_envelope,
)
from candybar.events import vocabulary as voc
from candybar.events.envelope import InvocationMetrics

MINUTE = datetime(2026, 10, 25, 22, 30, tzinfo=UTC)
EPOCH_MINUTE = int(MINUTE.timestamp()) // 60
TICK_ID = f"tick:{EPOCH_MINUTE}"

CB_ID = uuid4()
CB_NAME = "nightly-rollup"
INVOCATION_ID = uuid4()


def _created():
    return build_candybar_created(
        candybar_id=CB_ID,
        name=CB_NAME,
        description="Rolls up the day",
        tags=["reporting"],
        schedule="0 3 * * *",
        noise=False,
    )


def _updated():
    return build_candybar_updated(
        candybar_id=CB_ID, name=CB_NAME, changes={"schedule": "0 4 * * *"}
    )


def _invoked():
    return build_candybar_invoked(
        candybar_id=CB_ID,
        name=CB_NAME,
        invocation_id=INVOCATION_ID,
        trigger="scheduled",
        tick_id=TICK_ID,
    )


def _completed():
    return build_candybar_completed(
        candybar_id=CB_ID,
        name=CB_NAME,
        invocation_id=INVOCATION_ID,
        trigger="scheduled",
        tick_id=TICK_ID,
        exit_code=0,
        runtime_ms=1234,
        metrics=InvocationMetrics(token_spend=4200, cost_usd=0.012),
    )


def _failed():
    return build_candybar_failed(
        candybar_id=CB_ID,
        name=CB_NAME,
        invocation_id=INVOCATION_ID,
        trigger="scheduled",
        tick_id=TICK_ID,
        exit_code=1,
        runtime_ms=87,
        message="exit 1",
        stderr="boom",
    )


def _skipped():
    return build_candybar_skipped(
        candybar_id=CB_ID, name=CB_NAME, tick_id=TICK_ID, reason="misfire_coalesced"
    )


def _invoke_scheduled():
    return build_candybar_invoke(
        candybar_id=CB_ID, name=CB_NAME, trigger="scheduled", tick_id=TICK_ID
    )


_BUILDERS = [
    ("tick.fired", lambda: build_tick_fired(MINUTE)),
    ("candybar.created", _created),
    ("candybar.updated", _updated),
    ("candybar.invoked", _invoked),
    ("candybar.completed", _completed),
    ("candybar.failed", _failed),
    ("candybar.skipped", _skipped),
    ("candybar.invoke", _invoke_scheduled),
]
ALL_BUILDERS = [pytest.param(build, id=name) for name, build in _BUILDERS]


class TestVocabulary:
    def test_exactly_the_locked_eight_types(self):
        assert EVENT_TYPES == {
            "bloodbank.cron.tick.fired",
            "bloodbank.cron.candybar.created",
            "bloodbank.cron.candybar.updated",
            "bloodbank.cron.candybar.invoked",
            "bloodbank.cron.candybar.completed",
            "bloodbank.cron.candybar.failed",
            "bloodbank.cron.candybar.skipped",
        }
        assert COMMAND_TYPES == {"bloodbank.cron.candybar.invoke"}
        assert len(ALL_TYPES) == 8

    def test_types_match_contract_regex(self):
        pattern = re.compile(r"^bloodbank\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
        for t in ALL_TYPES:
            assert pattern.match(t), t

    def test_subject_is_five_tokens_with_kind_marker(self):
        assert voc.subject_for(voc.TYPE_TICK_FIRED, "event") == "bloodbank.evt.cron.tick.fired"
        assert (
            voc.subject_for(voc.TYPE_CANDYBAR_INVOKE, "command")
            == "bloodbank.cmd.cron.candybar.invoke"
        )
        for t in ALL_TYPES:
            subject = voc.subject_for(t, "command" if t in COMMAND_TYPES else "event")
            assert len(subject.split(".")) == 5, subject

    def test_schema_refs_derive_from_type(self):
        assert voc.schemaref_for(voc.TYPE_TICK_FIRED) == "bloodbank.cron.tick.fired.v1"
        assert voc.dataschema_for(voc.TYPE_CANDYBAR_FAILED) == (
            "apicurio://holyfields/bloodbank.cron.candybar.failed/versions/1"
        )

    def test_tense_guard_rejects_mismatch(self):
        with pytest.raises(ValueError, match="imperative"):
            voc.validate_tense(voc.TYPE_CANDYBAR_INVOKE, "event")
        with pytest.raises(ValueError, match="past-tense"):
            voc.validate_tense(voc.TYPE_CANDYBAR_COMPLETED, "command")


class TestTickFired:
    def test_deterministic_identity(self):
        env = build_tick_fired(MINUTE)
        assert env.type == "bloodbank.cron.tick.fired"
        assert env.kind == "event"
        assert env.subject == "bloodbank.evt.cron.tick.fired"
        assert env.ordering_key == TICK_ID
        assert env.data.tick_id == TICK_ID
        assert env.data.minute == MINUTE
        # Root fact: correlates on itself, no cause.
        assert env.correlationid == env.id
        assert env.causationid is None
        assert env.actor.type == "scheduler"

    def test_rederived_identity_is_stable(self):
        a = build_tick_fired(MINUTE)
        b = build_tick_fired(MINUTE)
        assert a.data.tick_id == b.data.tick_id
        assert a.ordering_key == b.ordering_key

    def test_non_utc_rejected(self):
        cest = timezone(timedelta(hours=2))
        with pytest.raises(ValueError, match="UTC"):
            build_tick_fired(datetime(2026, 10, 25, 22, 30, tzinfo=cest))

    def test_naive_rejected(self):
        with pytest.raises(ValueError, match="UTC"):
            build_tick_fired(datetime(2026, 10, 25, 22, 30))

    def test_off_boundary_rejected(self):
        with pytest.raises(ValueError, match="minute boundary"):
            build_tick_fired(MINUTE.replace(second=1))


class TestInvokeCommand:
    def test_scheduled_shape(self):
        cmd = _invoke_scheduled()
        assert cmd.kind == "command"
        assert cmd.subject == "bloodbank.cmd.cron.candybar.invoke"
        assert cmd.delivery == "single_consumer"
        assert cmd.command_id is not None
        assert cmd.correlationid == cmd.command_id
        assert cmd.idempotency_key == f"candybar:{CB_ID}:tick:{TICK_ID}"
        assert cmd.data.trigger == "scheduled"
        assert cmd.data.tick_id == TICK_ID

    def test_manual_shape(self):
        cmd = build_candybar_invoke(candybar_id=CB_ID, name=CB_NAME, trigger="manual")
        assert cmd.idempotency_key == f"candybar:{CB_ID}:manual:{cmd.command_id}"
        assert cmd.data.tick_id is None

    def test_manual_retry_stable_key(self):
        cmd_id = uuid4()
        a = build_candybar_invoke(
            candybar_id=CB_ID, name=CB_NAME, trigger="manual", command_id=cmd_id
        )
        b = build_candybar_invoke(
            candybar_id=CB_ID, name=CB_NAME, trigger="manual", command_id=cmd_id
        )
        assert a.idempotency_key == b.idempotency_key

    def test_scheduled_without_tick_rejected(self):
        with pytest.raises(ValueError, match="requires a tick_id"):
            build_candybar_invoke(candybar_id=CB_ID, name=CB_NAME, trigger="scheduled")

    def test_misfire_requires_tick(self):
        with pytest.raises(ValueError, match="requires a tick_id"):
            build_candybar_invoke(candybar_id=CB_ID, name=CB_NAME, trigger="misfire")

    def test_unknown_trigger_rejected(self):
        with pytest.raises(ValueError, match="unknown trigger"):
            build_candybar_invoke(candybar_id=CB_ID, name=CB_NAME, trigger="cron")  # type: ignore[arg-type]

    def test_manual_with_tick_rejected(self):
        with pytest.raises(ValueError, match="no tick_id"):
            build_candybar_invoke(
                candybar_id=CB_ID, name=CB_NAME, trigger="manual", tick_id=TICK_ID
            )


class TestCandybarEvents:
    def test_ordering_key_bucket(self):
        for env in (_created(), _updated(), _invoked(), _completed(), _failed(), _skipped()):
            assert env.ordering_key == f"candybar:{CB_ID}"

    def test_failed_stderr_tail_truncated_and_marked(self):
        env = build_candybar_failed(
            candybar_id=CB_ID,
            name=CB_NAME,
            invocation_id=INVOCATION_ID,
            trigger="scheduled",
            tick_id=TICK_ID,
            exit_code=2,
            stderr="x" * (STDERR_TAIL_MAX + 500),
        )
        assert len(env.data.error.stderr_tail) == STDERR_TAIL_MAX
        assert env.data.error.stderr_tail == "x" * STDERR_TAIL_MAX
        assert env.data.error.redacted is True

    def test_completed_unmeasured_metrics_default_zero(self):
        env = build_candybar_completed(
            candybar_id=CB_ID,
            name=CB_NAME,
            invocation_id=INVOCATION_ID,
            trigger="manual",
            tick_id=None,
            exit_code=0,
            runtime_ms=5,
        )
        assert env.data.metrics.token_spend == 0
        assert env.data.metrics.cost_usd is None

    def test_invalid_name_rejected(self):
        with pytest.raises(ValidationError):
            build_candybar_created(
                candybar_id=CB_ID,
                name="Bad_Name",
                description="x",
                tags=[],
                schedule="* * * * *",
            )

    def test_invalid_trigger_on_event_builder_rejected(self):
        with pytest.raises(ValidationError):
            build_candybar_invoked(
                candybar_id=CB_ID,
                name=CB_NAME,
                invocation_id=INVOCATION_ID,
                trigger="bogus",  # type: ignore[arg-type]
                tick_id=TICK_ID,
            )

    def test_empty_changes_rejected(self):
        with pytest.raises(ValidationError):
            build_candybar_updated(candybar_id=CB_ID, name=CB_NAME, changes={})

    def test_oversized_failure_message_rejected_loudly(self):
        with pytest.raises(ValidationError):
            build_candybar_failed(
                candybar_id=CB_ID,
                name=CB_NAME,
                invocation_id=INVOCATION_ID,
                trigger="scheduled",
                tick_id=TICK_ID,
                exit_code=1,
                message="m" * 501,
            )

    def test_naive_time_rejected_by_model(self):
        raw = _created().model_dump(mode="json")
        raw["time"] = "2026-10-25T22:30:00"  # no offset — violates UTC convention
        with pytest.raises(ValidationError):
            parse_envelope(raw)

    def test_naive_minute_rejected_by_model(self):
        raw = build_tick_fired(MINUTE).model_dump(mode="json")
        raw["data"]["minute"] = "2026-10-25T22:30:00"
        with pytest.raises(ValidationError):
            parse_envelope(raw)


class TestRoundTrip:
    @pytest.mark.parametrize("build", ALL_BUILDERS)
    def test_build_parse_equal(self, build):
        env = build()
        raw = env.model_dump(mode="json")
        json.dumps(raw)  # wire-safe
        back = parse_envelope(raw)
        assert back == env
        assert type(back) is type(env)

    @pytest.mark.parametrize("build", ALL_BUILDERS)
    def test_subject_matches_kind_marker(self, build):
        env = build()
        assert env.subject == voc.subject_for(env.type, env.kind)

    def test_unknown_type_rejected(self):
        raw = _created().model_dump(mode="json")
        raw["type"] = "bloodbank.cron.candybar.exploded"
        with pytest.raises(ValueError, match="not in the locked cron vocabulary"):
            parse_envelope(raw)

    def test_missing_type_rejected(self):
        with pytest.raises(TypeError, match="no string 'type'"):
            parse_envelope({})

    def test_subject_tamper_rejected(self):
        raw = _created().model_dump(mode="json")
        raw["subject"] = "bloodbank.cmd.cron.candybar.created"
        with pytest.raises(ValidationError):
            parse_envelope(raw)

    def test_malformed_payload_rejected(self):
        raw = _completed().model_dump(mode="json")
        raw["data"]["runtime_ms"] = -1
        with pytest.raises(ValidationError):
            parse_envelope(raw)

    def test_kind_flip_on_typed_model_rejected(self):
        raw = _invoke_scheduled().model_dump(mode="json")
        raw["kind"] = "event"
        with pytest.raises(ValidationError):
            parse_envelope(raw)


class TestDraftSchemas:
    DRAFT_DIR = Path(__file__).resolve().parent.parent / "schemas" / "cron"

    def test_all_eight_drafts_exist(self):
        names = {p.name for p in self.DRAFT_DIR.glob("*.json")}
        assert names == {
            "tick.fired.json",
            "candybar.created.json",
            "candybar.updated.json",
            "candybar.invoked.json",
            "candybar.completed.json",
            "candybar.failed.json",
            "candybar.skipped.json",
            "candybar.invoke.json",
        }

    def test_drafts_align_with_vocabulary(self):
        for path in self.DRAFT_DIR.glob("*.json"):
            doc = json.loads(path.read_text())
            type_ = doc["properties"]["type"]["const"]
            assert type_ in ALL_TYPES, path.name
            tokens = type_.split(".")
            assert path.name == f"{tokens[2]}.{tokens[3]}.json"
            assert doc["properties"]["domain"]["const"] == "cron"
            assert doc["$id"].endswith(f"/bloodbank/cron/{path.name}")
            kind = doc["properties"]["kind"]["const"]
            expected_kind = "command" if type_ in COMMAND_TYPES else "event"
            assert kind == expected_kind

    def test_built_envelopes_satisfy_draft_consts(self):
        """Every builder output matches its draft schema's const envelope fields."""
        by_type = {}
        for path in self.DRAFT_DIR.glob("*.json"):
            doc = json.loads(path.read_text())
            by_type[doc["properties"]["type"]["const"]] = doc
        for _name, build in _BUILDERS:
            env = build()
            raw = env.model_dump(mode="json")
            doc = by_type[env.type]
            props = doc["properties"]
            for field in ("type", "kind", "domain", "subject", "dataschema", "schemaref"):
                assert raw[field] == props[field]["const"], (env.type, field)
            if env.kind == "command":
                assert raw["delivery"] == props["delivery"]["const"]
            for required in doc["required"]:
                assert required in raw, (env.type, required)
                if required != "causationid":
                    assert raw[required] is not None, (env.type, required)

    def test_failed_draft_stderr_limit_matches_builder_constant(self):
        """The draft schema's stderr_tail cap and STDERR_TAIL_MAX must not drift."""
        doc = json.loads((self.DRAFT_DIR / "candybar.failed.json").read_text())
        error_props = doc["properties"]["data"]["properties"]["error"]["properties"]
        assert error_props["stderr_tail"]["maxLength"] == STDERR_TAIL_MAX
        assert error_props["redacted"]["const"] is True


def test_tick_event_id_is_a_pure_function_of_tick_id():
    """Story 2.1: the tick's CloudEvent id — and so its Nats-Msg-Id — derives
    from tick_id alone, so the broker dedupes a re-published minute."""
    a = build_tick_fired(MINUTE)
    b = build_tick_fired(MINUTE)
    assert a.id == b.id == voc.tick_event_id(a.data.tick_id)
    assert a.correlationid == a.id  # root lineage still self-correlates
    later = build_tick_fired(MINUTE + timedelta(minutes=1))
    assert later.id != a.id
    explicit = uuid4()
    assert build_tick_fired(MINUTE, event_id=explicit).id == explicit
    with pytest.raises(ValueError, match="tick_id"):
        voc.tick_event_id("29883066")
