"""Story 7.4 (AD-19): the hermes runner.

`hermes -p <profile> cron run <job_id> --wait` is triggered through the same spawn path as an exec
runner. A stub `hermes` shell script (CANDYBAR_HERMES) pins the argv, the exit-code mapping, the
timeout and the `cron-run-result:` usage parse with real subprocesses, unscoped (`root=""`).
A gated live suite (CANDYBAR_TEST_HERMES=1) drives the real hermes CLI against a throwaway
HERMES_HOME, including the double-trigger join.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from candybar.executor import hermes_runner
from candybar.executor.hermes_runner import (
    HERMES_BIN_ENV,
    RESULT_PREFIX,
    hermes_argv,
    parse_result_line,
)
from candybar.executor.targets import run_target
from candybar.executor.worker import resolve_outcome
from candybar.registry import CandybarManifest
from candybar.runners import (
    HermesRunner,
    RunnerError,
    canonical_runner,
    parse_runner,
    runner_label,
)

HERMES = {"kind": "hermes", "profile": "33god-pm", "job_id": "a1b2c3d4e5f6"}
IDS = {"invocation_id": uuid4(), "candybar_id": uuid4(), "trigger": "scheduled", "tick_id": "tick:1"}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The model.


def test_hermes_runner_canonical_form_and_label() -> None:
    once = canonical_runner(HERMES)
    assert once == {**HERMES, "timeout_s": 3600}
    assert canonical_runner(once) == once
    assert runner_label(once) == "hermes:33god-pm/a1b2c3d4e5f6"


def test_hermes_runner_takes_a_timeout_and_a_failure_hook() -> None:
    raw = {**HERMES, "timeout_s": 900, "on_failure": ["ntfy-publish", "--topic", "alerts"]}
    assert canonical_runner(raw) == raw


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"kind": "hermes", "profile": "p"}, "job_id"),
        ({"kind": "hermes", "job_id": "j"}, "profile"),
        ({"kind": "hermes", "profile": "-p", "job_id": "j"}, "no leading dash"),
        ({"kind": "hermes", "profile": "p", "job_id": "--wait"}, "no leading dash"),
        ({"kind": "hermes", "profile": "p q", "job_id": "j"}, "plain name or id"),
        ({"kind": "hermes", "profile": "../x", "job_id": "j"}, "plain name or id"),
        ({"kind": "hermes", "profile": "p", "job_id": "j", "timeout_s": 0}, "greater than or equal to 1"),
        ({"kind": "hermes", "profile": "p", "job_id": "j", "argv": ["x"]}, "argv"),
        ({"kind": "hermes", "profile": "p", "job_id": "j", "on_failure": []}, "must not be empty"),
    ],
)
def test_invalid_hermes_runner_is_rejected(raw: dict, fragment: str) -> None:
    with pytest.raises(RunnerError, match=fragment):
        parse_runner(raw)


def test_manifest_accepts_a_hermes_runner() -> None:
    manifest = CandybarManifest.model_validate({"name": "daily-brief", "schedule": "0 9 * * *", "runner": HERMES})
    assert manifest.entrypoint is None
    assert manifest.runner == {**HERMES, "timeout_s": 3600}
    assert manifest.entrypoint_label == "hermes:33god-pm/a1b2c3d4e5f6"


def test_hermes_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = HermesRunner.model_validate({**HERMES, "timeout_s": 120})
    monkeypatch.delenv(HERMES_BIN_ENV, raising=False)
    assert hermes_argv(runner) == [
        "hermes", "-p", "33god-pm", "cron", "run", "a1b2c3d4e5f6", "--wait", "--timeout", "120",
    ]
    monkeypatch.setenv(HERMES_BIN_ENV, "/opt/hermes")
    assert hermes_argv(runner)[0] == "/opt/hermes"


# ---------------------------------------------------------------------------
# The usage parse.


def _line(**fields: object) -> str:
    return RESULT_PREFIX + json.dumps(fields)


def test_parse_success_sums_input_and_output_tokens() -> None:
    result = parse_result_line(
        "banner\n" + _line(status="completed", error=None, input_tokens=1200, output_tokens=345, total_tokens=9)
    )
    assert result is not None and result.status == "success" and result.message is None
    assert result.metrics.token_spend == 1545  # input + output, never the reported total


def test_parse_failure_carries_the_attempt_error_and_its_tokens() -> None:
    result = parse_result_line(_line(status="failed", error="Script exited with code 3", input_tokens=5, output_tokens=1))
    assert result is not None and result.status == "fail"
    assert result.message == "Script exited with code 3" and result.metrics.token_spend == 6


def test_parse_unknown_status_is_a_failure_with_a_message() -> None:
    result = parse_result_line(_line(status="unknown", error=None))
    assert result is not None and result.status == "fail" and "unknown" in (result.message or "")


def test_parse_missing_usage_is_zero() -> None:
    result = parse_result_line(_line(status="completed", error=None))
    assert result is not None and result.metrics.token_spend == 0


@pytest.mark.parametrize("bad", [-5, "12", None, True, float("nan"), float("inf"), [1]])
def test_parse_unusable_token_counts_read_zero(bad: object) -> None:
    result = parse_result_line(_line(status="completed", input_tokens=bad, output_tokens=bad))
    assert result is not None and result.metrics.token_spend == 0


def test_parse_takes_the_last_result_line_and_ignores_noise() -> None:
    out = "\n".join(
        [_line(status="failed", error="old"), "noise", _line(status="completed", input_tokens=2, output_tokens=2), "tail"]
    )
    result = parse_result_line(out)
    assert result is not None and result.status == "success" and result.metrics.token_spend == 4


@pytest.mark.parametrize("stdout", ["", "no result here\n", RESULT_PREFIX + "{not json", RESULT_PREFIX + "[1]"])
def test_parse_without_a_usable_line_is_none(stdout: str) -> None:
    assert parse_result_line(stdout) is None


# ---------------------------------------------------------------------------
# run_target with a stub hermes.


def _stub(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "hermes-stub"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run_hermes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, **runner_extra: object):
    monkeypatch.setenv(HERMES_BIN_ENV, str(_stub(tmp_path, body)))
    return run(run_target(runner={**HERMES, **runner_extra}, root="", **IDS))


def test_success_maps_to_success_with_the_attempts_token_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv_log = tmp_path / "argv"
    body = (
        f'printf "%s\\n" "$@" > {argv_log}\n'
        "echo 'Triggered job: x'\n"
        f"echo '{_line(status='completed', error=None, input_tokens=1200, output_tokens=345)}'\n"
    )
    outcome = _run_hermes(tmp_path, monkeypatch, body)
    assert outcome.exit_code == 0 and outcome.spawn_error is None
    resolved = resolve_outcome(outcome)
    assert resolved.success and resolved.message is None
    assert resolved.metrics.token_spend == 1545
    assert argv_log.read_text().split() == [
        "-p", "33god-pm", "cron", "run", "a1b2c3d4e5f6", "--wait", "--timeout", "3600",
    ]


def test_failed_attempt_maps_to_failed_with_hermes_own_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = f"echo '{_line(status='failed', error='provider quota exhausted', input_tokens=40, output_tokens=0)}'\nexit 1\n"
    outcome = _run_hermes(tmp_path, monkeypatch, body)
    resolved = resolve_outcome(outcome)
    assert outcome.exit_code == 1
    assert not resolved.success and resolved.message == "provider quota exhausted"
    assert resolved.metrics.token_spend == 40  # a failed attempt still spent them


def test_a_crash_without_a_result_line_is_an_exit_code_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = _run_hermes(tmp_path, monkeypatch, "echo 'Traceback...' >&2\nexit 2\n")
    resolved = resolve_outcome(outcome)
    assert not resolved.success and resolved.message == "exit code 2"
    assert "Traceback" in outcome.stderr_tail
    assert resolved.metrics.token_spend == 0


def test_success_without_a_usage_line_reads_zero_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = resolve_outcome(_run_hermes(tmp_path, monkeypatch, "echo done\n"))
    assert resolved.success and resolved.metrics.token_spend == 0


def test_exit_zero_with_a_failed_result_line_is_still_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = f"echo '{_line(status='failed', error='lied about exit', input_tokens=1, output_tokens=1)}'\n"
    resolved = resolve_outcome(_run_hermes(tmp_path, monkeypatch, body))
    assert not resolved.success and resolved.message == "lied about exit"


def test_timeout_kills_the_cli_and_names_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hermes_runner, "TIMEOUT_GRACE_SECONDS", 0)
    outcome = _run_hermes(tmp_path, monkeypatch, "sleep 30\n", timeout_s=1)
    resolved = resolve_outcome(outcome)
    assert outcome.exit_code != 0 and outcome.runtime_ms < 15_000
    assert not resolved.success and "timed out after 1s" in (resolved.message or "")


def test_a_missing_hermes_binary_is_a_spawn_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HERMES_BIN_ENV, str(tmp_path / "no-such-hermes"))
    outcome = run(run_target(runner=HERMES, root="", **IDS))
    resolved = resolve_outcome(outcome)
    assert outcome.spawn_error is not None
    assert not resolved.success and "spawn failed" in (resolved.message or "")


def test_the_protocol_env_reaches_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = tmp_path / "env"
    outcome = _run_hermes(tmp_path, monkeypatch, f'echo "$CANDYBAR_INVOCATION_ID $CANDYBAR_TRIGGER" > {seen}\n')
    assert outcome.exit_code == 0
    assert seen.read_text().split() == [str(IDS["invocation_id"]), "scheduled"]


# ---------------------------------------------------------------------------
# Live: the real hermes CLI, a throwaway HERMES_HOME (CANDYBAR_TEST_HERMES=1).

# The user-level launcher, exactly what the executor unit's PATH resolves. Never a bare
# `hermes`: a project checkout can put a profile-pinned PM launcher first on PATH.
LIVE_HERMES = Path.home() / ".local" / "bin" / "hermes"
LIVE = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_HERMES") != "1" or not LIVE_HERMES.exists(),
    reason="set CANDYBAR_TEST_HERMES=1 with ~/.local/bin/hermes installed",
)


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes-home"
    (home / "scripts").mkdir(parents=True)
    (home / "config.yaml").write_text("model: test\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(HERMES_BIN_ENV, str(LIVE_HERMES))
    return home


def _create_external_job(home: Path, script: str, body: str, name: str) -> str:
    path = home / "scripts" / script
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    # No `-p`: HERMES_HOME (the throwaway) selects the store. `-p default` would resolve the
    # scripts directory under the real ~/.hermes.
    proc = subprocess.run(
        [str(LIVE_HERMES), "cron", "create", "every 1h", "--no-agent", "--script", script,
         "--name", name, "--deliver", "local", "--scheduler", "external"],
        check=False, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for line in proc.stdout.splitlines():
        if line.startswith("Created job:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(proc.stdout)


def _executions(home: Path, job_id: str) -> int:
    import sqlite3

    conn = sqlite3.connect(home / "cron" / "executions.db")
    try:
        return conn.execute("SELECT COUNT(*) FROM executions WHERE job_id=?", (job_id,)).fetchone()[0]
    finally:
        conn.close()


@LIVE
def test_live_success_and_failure_map_through_the_real_cli(hermes_home: Path) -> None:
    ok = _create_external_job(hermes_home, "ok.sh", "echo fine\n", "live-ok")
    bad = _create_external_job(hermes_home, "bad.sh", "echo oops >&2\nexit 3\n", "live-bad")
    good = resolve_outcome(run(run_target(runner={"kind": "hermes", "profile": "default", "job_id": ok}, root="", **IDS)))
    assert good.success and good.metrics.token_spend == 0
    failed = resolve_outcome(run(run_target(runner={"kind": "hermes", "profile": "default", "job_id": bad}, root="", **IDS)))
    assert not failed.success and "exited with code 3" in (failed.message or "")


@LIVE
def test_live_double_trigger_is_joined_never_two_attempts(hermes_home: Path) -> None:
    job = _create_external_job(hermes_home, "slow.sh", "sleep 6\necho slow done\n", "live-slow")

    async def both() -> list:
        spec = {"kind": "hermes", "profile": "default", "job_id": job}
        first = asyncio.create_task(run_target(runner=spec, root="", **IDS))
        await asyncio.sleep(3)  # the first attempt is running inside its CLI
        second = asyncio.create_task(run_target(runner=spec, root="", **IDS))
        return await asyncio.gather(first, second)

    outcomes = run(both())
    assert [resolve_outcome(o).success for o in outcomes] == [True, True]
    assert _executions(hermes_home, job) == 1
