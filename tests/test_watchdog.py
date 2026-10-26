"""Tests for deploy/watchdog/candybar-watchdog.sh (Story 7.6, AD-22).

The script is bash; these tests drive it with stub `curl`, `jq`, `systemctl`
and `ntfy-alert` on PATH so every I/O-matrix row is pinned without touching
the live API, systemd or ntfy. Each test runs the real script with
CANDYBAR_WATCHDOG_* env pointing at a tmp state dir.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "watchdog" / "candybar-watchdog.sh"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="watchdog script missing")


class Rig:
    """A sandboxed run of the watchdog script with stubbed externals."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.state = tmp / "state"
        self.pages: Path = tmp / "pages.log"
        # Stub behavior knobs live under tmp/knobs/.
        self.knobs = tmp / "knobs"
        self.knobs.mkdir()
        self._mk_curl()
        self._mk_systemctl()
        self._mk_ntfy()

    def _mk(self, name: str, body: str) -> None:
        p = self.bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _mk_curl(self) -> None:
        # Stub curl driven by files in $KNOBS:
        #   healthz_code    — HTTP code for */healthz (default 200)
        #   candybars.json  — body for */candybars (default: one canary row)
        #   invocations.json — body for */invocations* (default: fresh success)
        #   api_fail        — if present, /candybars + invocations exit 22
        self._mk(
            "curl",
            """#!/usr/bin/env bash
set -uo pipefail
url="${@: -1}"
case "$url" in
  */healthz)
    code="$(cat "$KNOBS/healthz_code" 2>/dev/null || echo 200)"
    if [[ "$code" != "200" ]]; then
      # mimic -f: fail on 4xx/5xx; still print the code like -w would on error path
      printf '%s' "$code"
      exit 22
    fi
    printf '%s' "$code"
    ;;
  */candybars)
    [[ -f "$KNOBS/api_fail" ]] && exit 22
    cat "$KNOBS/candybars.json"
    ;;
  */invocations*)
    [[ -f "$KNOBS/api_fail" ]] && exit 22
    cat "$KNOBS/invocations.json"
    ;;
  *)
    echo "stub-curl: unexpected url $url" >&2
    exit 2
    ;;
esac
""",
        )

    def _mk_systemctl(self) -> None:
        # $KNOBS/unit_active holds "active" or anything else.
        self._mk(
            "systemctl",
            """#!/usr/bin/env bash
set -uo pipefail
# called as: systemctl --user is-active --quiet <unit>
[[ "$(cat "$KNOBS/unit_active" 2>/dev/null || echo active)" == "active" ]] && exit 0
exit 3
""",
        )

    def _mk_ntfy(self) -> None:
        self._mk(
            "ntfy-alert",
            """#!/usr/bin/env bash
echo "$*" >> "$PAGES"
exit 0
""",
        )

    # --- knobs ---------------------------------------------------------------
    def set_healthz(self, code: int) -> None:
        (self.knobs / "healthz_code").write_text(str(code))

    def set_candybars(self, rows: list[dict]) -> None:
        (self.knobs / "candybars.json").write_text(json.dumps(rows))

    def set_invocations(self, inv: list[dict]) -> None:
        (self.knobs / "invocations.json").write_text(json.dumps({"invocations": inv}))

    def set_unit(self, active: bool) -> None:
        (self.knobs / "unit_active").write_text("active" if active else "inactive")

    def set_api_fail(self) -> None:
        (self.knobs / "api_fail").write_text("")

    # --- run ------------------------------------------------------------------
    def run(self) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "KNOBS": str(self.knobs),
                "PAGES": str(self.pages),
                "CANDYBAR_WATCHDOG_STATE_DIR": str(self.state),
                "CANDYBAR_WATCHDOG_CURL": str(self.bin / "curl"),
                "CANDYBAR_WATCHDOG_JQ": "jq",
                "CANDYBAR_WATCHDOG_SYSTEMCTL": f"{self.bin}/systemctl --user",
                "CANDYBAR_WATCHDOG_NTFY_ALERT": str(self.bin / "ntfy-alert"),
            }
        )
        return subprocess.run(
            ["bash", str(SCRIPT)], check=False, env=env, capture_output=True, text=True, timeout=30
        )

    def page_log(self) -> str:
        return self.pages.read_text() if self.pages.exists() else ""

    def stamped(self) -> bool:
        return (self.state / "ok").exists()


CANARY = {"id": "aaaaaaaa-0000-7000-8000-000000000001", "name": "watchdog-canary"}


def fresh_inv(result: str = "success", age_s: int = 30) -> dict:
    return {
        "id": "inv-1",
        "result": result,
        "finishedAt": (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat(),
    }


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    r = Rig(tmp_path)
    r.set_healthz(200)
    r.set_candybars([CANARY])
    r.set_invocations([fresh_inv()])
    r.set_unit(active=True)
    return r


class TestAllGreen:
    def test_stamps_and_stays_silent(self, rig: Rig) -> None:
        cp = rig.run()
        assert cp.returncode == 0, cp.stderr
        assert rig.stamped()
        assert rig.page_log() == ""

    def test_recovery_page_after_outage(self, rig: Rig) -> None:
        rig.set_unit(active=False)
        rig.run()
        assert "candybar DOWN" in rig.page_log()
        rig.set_unit(active=True)
        rig.run()
        log = rig.page_log()
        assert log.count("candybar DOWN") == 1
        assert log.count("candybar RECOVERED") == 1
        assert rig.stamped()


class TestFailures:
    def test_healthz_down_pages_once_and_no_stamp(self, rig: Rig) -> None:
        rig.set_healthz(503)
        for _ in range(3):  # outage persists: no re-page
            cp = rig.run()
            assert cp.returncode == 0
        log = rig.page_log()
        assert log.count("candybar DOWN") == 1
        assert "healthz" in log
        assert not rig.stamped()

    def test_stale_canary_pages(self, rig: Rig) -> None:
        rig.set_invocations([fresh_inv(age_s=600)])
        rig.run()
        log = rig.page_log()
        assert log.count("candybar DOWN") == 1
        assert "canary" in log
        assert not rig.stamped()

    def test_canary_failure_result_pages(self, rig: Rig) -> None:
        rig.set_invocations([fresh_inv(result="fail")])
        rig.run()
        assert "candybar DOWN" in rig.page_log()
        assert not rig.stamped()

    def test_canary_never_ran(self, rig: Rig) -> None:
        rig.set_invocations([])
        rig.run()
        assert "candybar DOWN" in rig.page_log()

    def test_canary_not_registered(self, rig: Rig) -> None:
        rig.set_candybars([])
        rig.run()
        assert "candybar DOWN" in rig.page_log()

    def test_invocations_api_5xx_is_fail(self, rig: Rig) -> None:
        rig.set_api_fail()
        rig.run()
        assert "candybar DOWN" in rig.page_log()
        assert not rig.stamped()

    def test_executor_inactive_pages(self, rig: Rig) -> None:
        rig.set_unit(active=False)
        rig.run()
        log = rig.page_log()
        assert "executor" in log
        assert not rig.stamped()

    def test_running_row_is_skipped_for_terminal(self, rig: Rig) -> None:
        running = {"id": "inv-0", "result": None, "finishedAt": None}
        rig.set_invocations([running, fresh_inv()])
        cp = rig.run()
        assert cp.returncode == 0
        assert rig.stamped()
        assert rig.page_log() == ""

    def test_connection_refused_healthz(self, rig: Rig) -> None:
        # curl exits nonzero without printing a code: script maps to 000.
        rig.set_healthz(503)  # stub prints code + exit 22; covers -f failure path
        rig.run()
        assert "healthz" in rig.page_log()
