"""Story 7.3 (AD-18): the exec runner — argv, workdir, env literals, timeout, on_failure.

Real subprocesses, unscoped (`root=""`): the cgroup path has its own suites.
`run_target` dispatch and the failure hook are driven with real children too.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from candybar.executor.runner import ProcessRegistry, run_invocation
from candybar.executor.targets import FAILURE_MESSAGE_ENV, run_on_failure, run_target
from candybar.executor.worker import resolve_outcome

PY = sys.executable
IDS = {"invocation_id": uuid4(), "candybar_id": uuid4(), "trigger": "scheduled", "tick_id": "tick:1"}


def run(coro):
    return asyncio.run(coro)


def _dump_env_script(tmp_path: Path) -> list[str]:
    """argv for a python child that writes cwd + env to <tmp>/seen.json."""
    out = tmp_path / "seen.json"
    code = (
        "import json,os;"
        f"json.dump({{'cwd': os.getcwd(), 'env': dict(os.environ)}}, open({str(out)!r}, 'w'))"
    )
    return [PY, "-c", code]


def _seen(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "seen.json").read_text())


def test_argv_runs_in_the_given_workdir_with_literals_and_the_protocol_env(tmp_path: Path) -> None:
    workdir = tmp_path / "wd"
    workdir.mkdir()
    outcome = run(
        run_invocation(
            argv=_dump_env_script(tmp_path),
            workdir=str(workdir),
            env={"JOB_LITERAL": "kept", "CANDYBAR_TRIGGER": "spoofed"},
            root="",
            **IDS,
        )
    )
    assert outcome.exit_code == 0 and outcome.spawn_error is None
    seen = _seen(tmp_path)
    assert seen["cwd"] == str(workdir)
    assert seen["env"]["JOB_LITERAL"] == "kept"
    # The protocol always wins over a literal (the model also refuses the key).
    assert seen["env"]["CANDYBAR_TRIGGER"] == "scheduled"
    assert seen["env"]["CANDYBAR_INVOCATION_ID"] == str(IDS["invocation_id"])


def test_workdir_defaults_to_the_users_home(tmp_path: Path) -> None:
    outcome = run(run_invocation(argv=_dump_env_script(tmp_path), root="", **IDS))
    assert outcome.exit_code == 0
    assert _seen(tmp_path)["cwd"] == str(Path.home())


def test_a_bare_command_name_is_looked_up_on_path() -> None:
    outcome = run(run_invocation(argv=["true"], root="", **IDS))
    assert outcome.exit_code == 0 and outcome.spawn_error is None


def test_a_missing_workdir_or_program_is_a_spawn_failure(tmp_path: Path) -> None:
    gone = run(run_invocation(argv=["true"], workdir=str(tmp_path / "nope"), root="", **IDS))
    assert gone.spawn_error and gone.exit_code is None
    nobin = run(run_invocation(argv=["/no/such/binary"], root="", **IDS))
    assert nobin.spawn_error and "FileNotFoundError" in nobin.spawn_error
    assert resolve_outcome(nobin).success is False


def test_entrypoint_and_argv_are_exclusive() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        run(run_invocation(entrypoint="/bin/true", argv=["true"], root="", **IDS))
    with pytest.raises(ValueError, match="exactly one"):
        run(run_invocation(root="", **IDS))


def test_the_legacy_entrypoint_still_runs_from_its_own_directory(tmp_path: Path) -> None:
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\npwd > cwd.txt\n")
    script.chmod(0o755)
    outcome = run(run_invocation(entrypoint=str(script), root="", **IDS))
    assert outcome.exit_code == 0
    assert (tmp_path / "cwd.txt").read_text().strip() == str(tmp_path)


def test_timeout_kills_the_job_and_names_the_timeout(tmp_path: Path) -> None:
    started = time.monotonic()
    outcome = run(run_invocation(argv=["sleep", "30"], timeout_s=0.4, root="", **IDS))
    assert time.monotonic() - started < 10
    assert outcome.exit_code not in (0, None)
    assert outcome.detail == "timed out after 0.4s and was killed"
    resolved = resolve_outcome(outcome)
    assert resolved.success is False and resolved.message == outcome.detail


def test_timeout_escalates_to_sigkill_for_a_job_that_ignores_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from candybar.executor import runner

    monkeypatch.setattr(runner, "TIMEOUT_KILL_GRACE_SECONDS", 0.3)
    stubborn = "import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);time.sleep(30)"
    started = time.monotonic()
    outcome = run(run_invocation(argv=[PY, "-c", stubborn], timeout_s=0.3, root="", **IDS))
    assert time.monotonic() - started < 10
    assert outcome.exit_code == -9 and "timed out" in (outcome.detail or "")


def test_a_job_that_finishes_inside_its_timeout_is_untouched() -> None:
    outcome = run(run_invocation(argv=["true"], timeout_s=30, root="", **IDS))
    assert outcome.exit_code == 0 and outcome.detail is None


def test_a_jobs_own_failure_message_beats_the_runners_diagnosis() -> None:
    from candybar.executor.runner import ResultFile, RunOutcome

    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    base = {
        "runtime_ms": 1, "stderr_tail": "", "result_error": None, "spawn_error": None,
        "scoped": False, "started_at": now, "finished_at": now,
    }
    wrote = RunOutcome(
        exit_code=1, result=ResultFile(status="fail", message="disk full"), detail="timed out", **base
    )
    assert resolve_outcome(wrote).message == "disk full"
    plain = RunOutcome(exit_code=1, result=None, detail="timed out", **base)
    assert resolve_outcome(plain).message == "timed out"
    bare = RunOutcome(exit_code=7, result=None, **base)
    assert resolve_outcome(bare).message == "exit code 7"


# ---------------------------------------------------------------------------
# run_target dispatch.


def test_run_target_runs_an_exec_runner_dict(tmp_path: Path) -> None:
    runner = {
        "kind": "exec",
        "argv": _dump_env_script(tmp_path),
        "env": {"VIA": "runner"},
        "workdir": str(tmp_path),
    }
    outcome = run(run_target(runner=runner, root="", **IDS))
    assert outcome.exit_code == 0
    assert _seen(tmp_path)["env"]["VIA"] == "runner"


def test_run_target_runs_a_bare_entrypoint(tmp_path: Path) -> None:
    outcome = run(run_target(entrypoint="/bin/true", root="", **IDS))
    assert outcome.exit_code == 0


def test_run_target_turns_a_corrupt_stored_runner_into_a_failed_run() -> None:
    outcome = run(run_target(runner={"kind": "exec", "argv": []}, root="", **IDS))
    assert outcome.exit_code is None and "invalid runner" in (outcome.spawn_error or "")
    assert resolve_outcome(outcome).success is False


def test_run_target_needs_something_to_run() -> None:
    with pytest.raises(ValueError, match="entrypoint or a runner"):
        run(run_target(root="", **IDS))


def test_the_exec_runner_is_registered_for_shutdown_like_any_child() -> None:
    registry = ProcessRegistry()
    runner = {"kind": "exec", "argv": ["sleep", "30"]}

    async def go() -> int | None:
        task = asyncio.create_task(run_target(runner=runner, registry=registry, root="", **IDS))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if registry.procs:
                break
        assert registry.procs
        await registry.terminate_all()
        return (await task).exit_code

    assert run(go()) == -15


# ---------------------------------------------------------------------------
# on_failure.


def _hook_runner(tmp_path: Path, **extra: object) -> dict:
    code = (
        "import json,os,sys;"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd()}}, "
        f"open({str(tmp_path / 'hook.json')!r}, 'w'))"
    )
    return {"kind": "exec", "argv": ["true"], "on_failure": [PY, "-c", code, "arg1"], **extra}


def _hook(runner: dict, message: str | None = "it broke", **kw: object):
    return run_on_failure(runner, message=message, **{**IDS, **kw})


def test_on_failure_runs_with_the_protocol_env_and_the_failure_message(tmp_path: Path) -> None:
    run(_hook(_hook_runner(tmp_path, workdir=str(tmp_path), env={"PAGE_TOPIC": "alerts"})))
    seen = json.loads((tmp_path / "hook.json").read_text())
    assert seen["argv"] == ["arg1"] and seen["cwd"] == str(tmp_path)
    env = seen["env"]
    assert env[FAILURE_MESSAGE_ENV] == "it broke"
    assert env["CANDYBAR_INVOCATION_ID"] == str(IDS["invocation_id"])
    assert env["CANDYBAR_ID"] == str(IDS["candybar_id"])
    assert env["CANDYBAR_TRIGGER"] == "scheduled" and env["CANDYBAR_TICK_ID"] == "tick:1"
    assert env["PAGE_TOPIC"] == "alerts"


def test_on_failure_is_best_effort_and_never_raises(tmp_path: Path) -> None:
    for broken in (
        {"kind": "exec", "argv": ["true"], "on_failure": ["/no/such/hook"]},
        {"kind": "exec", "argv": ["true"], "on_failure": ["false"]},
        {"kind": "exec", "argv": ["true"], "on_failure": ["true"], "workdir": str(tmp_path / "gone")},
    ):
        run(_hook(broken))  # must simply return


def test_on_failure_without_a_hook_does_nothing() -> None:
    run(_hook({"kind": "exec", "argv": ["true"]}))


def test_on_failure_that_hangs_is_killed_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from candybar.executor import targets

    monkeypatch.setattr(targets, "HOOK_TIMEOUT_SECONDS", 0.3)
    started = time.monotonic()
    run(_hook({"kind": "exec", "argv": ["true"], "on_failure": ["sleep", "30"]}))
    assert time.monotonic() - started < 10


def test_on_failure_scrubs_nul_from_the_failure_message(tmp_path: Path) -> None:
    run(_hook(_hook_runner(tmp_path), message="bad\x00byte"))
    seen = json.loads((tmp_path / "hook.json").read_text())
    assert seen["env"][FAILURE_MESSAGE_ENV] == "bad�byte"


def test_environment_is_not_polluted_by_the_hook(tmp_path: Path) -> None:
    run(_hook(_hook_runner(tmp_path)))
    assert FAILURE_MESSAGE_ENV not in os.environ
