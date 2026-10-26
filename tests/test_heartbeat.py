"""Heartbeat unit tests: boundary math, tick identity, retry policy, clock ownership.

Everything here runs without NATS: the Heartbeat loop is driven by an
injected virtual clock and sleep, and the sink is in-memory. Live-bus
behavior lives in test_heartbeat_integration.py.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner

from candybar.cli.main import app
from candybar.daemon import available_roles, resolve_roles
from candybar.events import TYPE_TICK_FIRED, build_tick_fired, parse_envelope
from candybar.events.envelope import TickFiredEvent
from candybar.heartbeat import (
    Heartbeat,
    NatsTickSink,
    next_minute_boundary,
    seconds_until,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "candybar"

# ---------------------------------------------------------------------------
# Clock math
# ---------------------------------------------------------------------------


def test_next_minute_boundary_mid_minute() -> None:
    now = datetime(2026, 10, 25, 12, 34, 56, 789000, tzinfo=UTC)
    assert next_minute_boundary(now) == datetime(2026, 10, 25, 12, 35, tzinfo=UTC)


def test_next_minute_boundary_exact_boundary_goes_to_following() -> None:
    now = datetime(2026, 10, 25, 12, 34, tzinfo=UTC)
    assert next_minute_boundary(now) == datetime(2026, 10, 25, 12, 35, tzinfo=UTC)


def test_next_minute_boundary_naive_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_minute_boundary(datetime(2026, 10, 25, 12, 34, 56))


def test_next_minute_boundary_non_utc_normalized() -> None:
    est = timezone(timedelta(hours=-4))
    now = datetime(2026, 10, 25, 8, 34, 59, tzinfo=est)  # 12:34:59 UTC
    assert next_minute_boundary(now) == datetime(2026, 10, 25, 12, 35, tzinfo=UTC)


def test_seconds_until_clamps_negative_for_passed_boundary() -> None:
    now = datetime(2026, 10, 25, 12, 35, 1, tzinfo=UTC)
    boundary = datetime(2026, 10, 25, 12, 35, tzinfo=UTC)
    assert seconds_until(boundary, now) == 0.0


# ---------------------------------------------------------------------------
# Virtual clock + in-memory sink
# ---------------------------------------------------------------------------


class VirtualClock:
    """A clock that only advances when the heartbeat sleeps on it."""

    def __init__(self, start: datetime, skew_per_sleep: timedelta = timedelta(0)) -> None:
        self.t = start
        self.skew_per_sleep = skew_per_sleep
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds) + self.skew_per_sleep
        await asyncio.sleep(0)  # yield so the stop watcher can run


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[TickFiredEvent] = []

    async def publish_tick(self, event: TickFiredEvent) -> None:
        self.events.append(event)


class FlakySink:
    """Fails `failures` times, then collects."""

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.attempts: list[UUID] = []
        self.events: list[TickFiredEvent] = []

    async def publish_tick(self, event: TickFiredEvent) -> None:
        self.attempts.append(event.id)
        if self.remaining > 0:
            self.remaining -= 1
            raise ConnectionError("bus unavailable")
        self.events.append(event)


def _epoch_minute(dt: datetime) -> int:
    return int(dt.timestamp()) // 60


async def _run_ticks(hb: Heartbeat, stop: asyncio.Event, count: int, sink: CollectingSink) -> None:
    async def watch() -> None:
        while len(sink.events) < count and not stop.is_set():
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(hb.run(stop), watch())


def test_one_tick_per_minute_over_virtual_window() -> None:
    start = datetime(2026, 10, 25, 12, 34, 56, 789000, tzinfo=UTC)
    clock = VirtualClock(start)
    sink = CollectingSink()
    hb = Heartbeat(sink, now=clock.now, sleep=clock.sleep)
    stop = asyncio.Event()
    asyncio.run(_run_ticks(hb, stop, 5, sink))

    assert len(sink.events) == 5
    first = datetime(2026, 10, 25, 12, 35, tzinfo=UTC)  # mid-minute start fires the NEXT boundary
    expected_minutes = [_epoch_minute(first) + i for i in range(5)]
    actual_minutes = [_epoch_minute(e.data.minute) for e in sink.events]
    assert actual_minutes == expected_minutes  # one tick per minute, consecutive
    for event in sink.events:
        assert event.type == TYPE_TICK_FIRED
        assert event.subject == "bloodbank.evt.cron.tick.fired"
        assert event.ordering_key == event.data.tick_id == f"tick:{_epoch_minute(event.data.minute)}"
    # The loop slept once per boundary: first ~3.2s to the boundary, then ~60s each.
    assert clock.sleeps[0] == pytest.approx(3.211, abs=0.001)
    assert all(s == pytest.approx(60.0) for s in clock.sleeps[1:])


def test_tick_identity_pinned_to_scheduled_minute_under_skew() -> None:
    # The clock jumps 90s forward DURING the sleep (suspend/resume, NTP step):
    # the tick must still carry the boundary that was captured before sleeping.
    start = datetime(2026, 10, 25, 12, 34, 30, tzinfo=UTC)
    clock = VirtualClock(start, skew_per_sleep=timedelta(seconds=90))
    sink = CollectingSink()
    hb = Heartbeat(sink, now=clock.now, sleep=clock.sleep)
    stop = asyncio.Event()
    asyncio.run(_run_ticks(hb, stop, 1, sink))

    assert sink.events[0].data.tick_id == f"tick:{_epoch_minute(datetime(2026, 10, 25, 12, 35, tzinfo=UTC))}"
    assert sink.events[0].data.minute == datetime(2026, 10, 25, 12, 35, tzinfo=UTC)


def test_stop_during_sleep_publishes_nothing() -> None:
    sink = CollectingSink()
    stop = asyncio.Event()

    async def blocking_sleep(_seconds: float) -> None:
        await stop.wait()  # hang until stopped, like a real long sleep

    hb = Heartbeat(sink, sleep=blocking_sleep)

    async def drive() -> None:
        async def stopper() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.gather(hb.run(stop), stopper())

    asyncio.run(drive())
    assert sink.events == []


def test_publish_retry_uses_same_event_then_succeeds() -> None:
    start = datetime(2026, 10, 25, 12, 34, 30, tzinfo=UTC)
    clock = VirtualClock(start)
    sink = FlakySink(failures=2)
    hb = Heartbeat(sink, now=clock.now, sleep=clock.sleep)
    stop = asyncio.Event()
    asyncio.run(_run_ticks(hb, stop, 1, sink))

    assert len(sink.events) == 1
    assert len(sink.attempts) == 3
    assert len(set(sink.attempts)) == 1  # same event id → Nats-Msg-Id dedupes server-side


def test_publish_drops_tick_after_max_attempts_and_loops_on() -> None:
    start = datetime(2026, 10, 25, 12, 34, 30, tzinfo=UTC)
    clock = VirtualClock(start)
    # The first tick's 3 attempts all fail (tick dropped); the next tick succeeds.
    sink = FlakySink(failures=3)
    hb = Heartbeat(sink, now=clock.now, sleep=clock.sleep, publish_backoff=0.0)
    stop = asyncio.Event()
    asyncio.run(_run_ticks(hb, stop, 1, sink))

    assert len(sink.events) == 1
    assert len(sink.attempts) == 4  # 3 dropped attempts on tick 1 + 1 success on tick 2
    assert len(set(sink.attempts[:3])) == 1  # the dropped tick was retried as the SAME event
    assert sink.events[0].id != sink.attempts[0]  # the dropped tick never made it
    assert sink.events[0].data.minute == datetime(2026, 10, 25, 12, 36, tzinfo=UTC)


# ---------------------------------------------------------------------------
# NatsTickSink wire shape (fake JetStream, no server)
# ---------------------------------------------------------------------------


class FakeJetStream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    async def publish(self, subject: str, payload: bytes, headers: dict[str, str]) -> object:
        self.calls.append((subject, payload, headers))

        class Ack:
            stream = "BLOODBANK_EVENTS"
            seq = 1
            duplicate = False

        return Ack()


def test_nats_sink_publishes_subject_payload_and_msg_id_header() -> None:
    js = FakeJetStream()
    sink = NatsTickSink(js)  # type: ignore[arg-type]
    event = build_tick_fired(datetime(2026, 10, 25, 12, 35, tzinfo=UTC))
    asyncio.run(sink.publish_tick(event))

    assert len(js.calls) == 1
    subject, payload, headers = js.calls[0]
    assert subject == "bloodbank.evt.cron.tick.fired"
    assert headers == {"Nats-Msg-Id": str(event.id)}
    assert parse_envelope(json.loads(payload)) == event


# ---------------------------------------------------------------------------
# Clock ownership guard (AD-1): only heartbeat may sleep-until-a-time
# ---------------------------------------------------------------------------

CLOCK_PATTERNS = [
    r"asyncio\.sleep\(",
    r"(?<![\w.])time\.sleep\(",
    r"\.call_later\(",
    r"\.call_at\(",
    r"sched\.scheduler",
    r"APScheduler",
    r"AsyncIOScheduler",
]
CLOCK_RE = re.compile("|".join(CLOCK_PATTERNS))


def _offenders(root: Path, *, allow_heartbeat: bool) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        in_heartbeat = "heartbeat" in path.relative_to(root).parts
        if in_heartbeat == allow_heartbeat:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if CLOCK_RE.search(line):
                hits.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    return hits


def test_only_heartbeat_owns_a_clock() -> None:
    assert _offenders(SRC, allow_heartbeat=True) == []


def test_clock_guard_sanity_heartbeat_does_own_one() -> None:
    # The guard above is meaningless if nothing can ever match: the heartbeat
    # module must contain a sleep primitive for the scan to be real.
    assert _offenders(SRC, allow_heartbeat=False) != []


# ---------------------------------------------------------------------------
# Role resolution + CLI
# ---------------------------------------------------------------------------


def test_available_roles_contains_heartbeat() -> None:
    assert available_roles() == ["api", "dispatcher", "executor", "heartbeat"]  # executor joined in 1.6


def test_resolve_roles_all_defaults_to_everything() -> None:
    assert resolve_roles("all") == available_roles()


def test_resolve_roles_dedupes_and_strips() -> None:
    assert resolve_roles(" heartbeat ,heartbeat,") == ["heartbeat"]


def test_resolve_roles_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="bogus"):
        resolve_roles("heartbeat,bogus")


def test_resolve_roles_rejects_empty_selection() -> None:
    # `--roles ""` (or ", ,") must not silently start zero roles and exit 0.
    with pytest.raises(ValueError, match="no roles"):
        resolve_roles(" , ,")


def test_run_daemon_starts_roles_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Composition smoke: run_daemon actually launches each selected role."""
    import candybar.daemon as daemon_mod

    started: list[str] = []

    async def fake_role(stop: asyncio.Event) -> None:
        started.append("fake")
        await stop.wait()

    monkeypatch.setitem(daemon_mod.ROLES, "fake", fake_role)

    async def drive() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(daemon_mod.run_daemon(["fake"], stop))
        await asyncio.sleep(0.05)
        assert started == ["fake"]
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())


def test_cli_daemon_rejects_unknown_role_exit_2() -> None:
    result = CliRunner().invoke(app, ["daemon", "--roles", "heartbeat,bogus"])
    assert result.exit_code == 2
    assert "bogus" in result.output


def test_cli_daemon_help_exit_0() -> None:
    result = CliRunner().invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0
    # Rich forces color under GITHUB_ACTIONS and splits "--roles" with ANSI codes.
    assert "--roles" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def test_cli_daemon_happy_path_runs_and_exits_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon command wires roles -> asyncio.run -> run_daemon and exits 0 on stop."""
    import candybar.daemon as daemon_mod

    ran_with: list[list[str]] = []

    async def fake_run_daemon(roles: list[str], stop: asyncio.Event) -> None:
        ran_with.append(roles)
        stop.set()  # pretend a signal arrived

    monkeypatch.setattr(daemon_mod, "run_daemon", fake_run_daemon)

    result = CliRunner().invoke(app, ["daemon", "--roles", "heartbeat"])
    assert result.exit_code == 0, result.output
    assert ran_with == [["heartbeat"]]
    assert "starting roles: heartbeat" in result.output


# ---------------------------------------------------------------------------
# Role wiring: connect-retry posture (no live server needed)
# ---------------------------------------------------------------------------


def test_role_retries_connect_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """NATS down at startup: retry forever (no crash exit), stop cleanly."""
    import candybar.heartbeat.role as role_mod

    attempts: list[dict] = []

    async def fake_connect(url: str, **kwargs: object) -> object:
        attempts.append({"url": url, **kwargs})
        raise ConnectionError("refused")

    monkeypatch.setattr(role_mod.nats, "connect", fake_connect)
    monkeypatch.setattr(role_mod, "CONNECT_RETRY_SECONDS", 0.05)
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:1")

    async def drive() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(role_mod.run_heartbeat(stop))
        await asyncio.sleep(0.2)  # a few failed connect attempts
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    assert len(attempts) >= 2  # retried, not crash-exited
    assert attempts[0]["url"] == "nats://127.0.0.1:1"
    assert attempts[0]["max_reconnect_attempts"] == -1  # reconnect forever mid-run
    assert attempts[0]["name"] == "candybar-heartbeat"
