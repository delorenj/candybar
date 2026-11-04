# ruff: noqa: F811  (the `reg` fixture is imported, then requested by name)
"""Steward round-2 review fixes (owner triage 2026-11-04), one regression test (or more) each.

Same in-memory registry and scripted OpenCode rig as tests/test_steward.py.

1. a refactor never runs a proving invoke (its next scheduled fire proves
   it), and a protected candybar's Suboptimal verdict is only flagged;
2. a failure to queue the enrich refresh never fails a fix or refactor;
3. the mutating-task lock is keyed on the git root of the job's checkout;
4. git roots resolve symlinks, a ledger repo is found by its git remote
   (~/code two deep and the extra checkout roots), transition_exec
   wrappers resolve to the wrapped script;
5. a cancelled follow-up settles its evaluation (the API side is in
   tests/test_api_steward.py);
6. protection by name and by the `ops` tag;
7. the task's own failure is never read as an operator cancel;
8. read-only sessions deny subagent delegation.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import candybar.steward.context as ctx
import candybar.steward.opencode as oc
from candybar.steward.context import (
    ContextBundle,
    find_checkout,
    gather,
    git_root,
    protection,
    unwrap_transition_exec,
)
from candybar.steward.model import CANCELLED, settled_evaluation
from candybar.steward.opencode import OpenCodeError, TurnResult
from test_steward import (
    Reg,
    ScriptedOpenCode,
    _prove_with,
    _verdict,
    _with_parent_evaluation,
    block,
    command_for,
    make,
    reg,  # noqa: F401  (fixture)
    run,
)


def _git(path: Path, remotes: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for name, url in (remotes or {}).items():
        subprocess.run(["git", "-C", str(path), "remote", "add", name, url], check=True)
    return path


@pytest.fixture()
def nogit() -> Iterator[Path]:
    """A scratch directory with no git repo above it (pytest's tmp_path may sit inside one)."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="candybar-nogit-") as raw:
        path = Path(raw).resolve()
        if git_root(path) is not None:
            pytest.skip(f"{path} is inside a git checkout")
        yield path


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    code = tmp_path / "code"
    code.mkdir()
    monkeypatch.setenv("CANDYBAR_CODE_ROOT", str(code))
    monkeypatch.setenv("CANDYBAR_HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("CANDYBAR_N8N_EXPORTS", str(tmp_path / "exports"))
    monkeypatch.setenv("CANDYBAR_CHECKOUT_ROOTS", "")
    monkeypatch.setattr(ctx, "_systemctl", lambda *args: None)
    monkeypatch.setattr(ctx, "PM_RUNTIME_JOBS", tmp_path / "none.json")
    ctx._remote_index.cache_clear()
    return tmp_path


def _definition(name: str, **kw: Any) -> dict[str, Any]:
    return {"id": str(uuid4()), "name": name, "tags": kw.pop("tags", []), **kw}


LEDGER = """
version: 1
jobs:
  - id: "systemd:{name}"
    wave: "7.20"
    candybar:
      name: {name}
      schedule: "*/5 * * * *"
      runner:
        kind: systemd
        unit: {name}.service
    native:
      kind: systemd-timer
      schedule: "OnCalendar=*:0/5"
      unit: {name}.timer
    source_of_truth:
      repo: {repo}
      path: systemd/user/{name}.timer
"""


# ---------------------------------------------------------------------------
# 1. Refactor: no proving run; protected Suboptimal is flagged.


def test_a_protected_suboptimal_is_flagged_not_refactored(reg: Reg) -> None:
    cb = reg.candybar("restic-vault", tags=["migrated"])
    task = reg.task(cb, "evaluate", model="automaticai/auto")
    steward = make(reg, ScriptedOpenCode(block(_verdict("Suboptimal"))))
    run(steward.handle_message(command_for(task)))
    assert task.status == "succeeded" and task.outcome == "Suboptimal", task.error
    assert [t.action for t in reg.tasks.values()] == ["evaluate"]  # no refactor queued
    assert not [e for e in reg.published if e.type == "bloodbank.cron.candybar.steward"]
    assert "flagged, not refactored" in task.summary and "protected (a backup job)" in task.summary
    stored = reg.profiles[cb.id].evaluation
    assert stored["followUp"] == "refactor" and stored["followUpStatus"] == "flagged"
    [call] = steward.report_calls  # type: ignore[attr-defined]
    assert "flagged for the operator, not refactored" in call["markdown"]


def test_a_protected_drifted_still_gets_its_fix(reg: Reg) -> None:
    cb = reg.candybar("watchdog-canary", tags=["watchdog"])
    task = reg.task(cb, "evaluate", model="automaticai/auto")
    run(make(reg, ScriptedOpenCode(block(_verdict("Drifted")))).handle_message(command_for(task)))
    [fix] = [t for t in reg.tasks.values() if t.action == "fix"]
    assert reg.profiles[cb.id].evaluation["followUpStatus"] == "queued" and fix.parent_task_id == task.id


def test_a_refactor_already_queued_for_a_protected_candybar_changes_nothing(reg: Reg) -> None:
    cb = reg.candybar("milkshake-mercury-backstop")
    _, task = _with_parent_evaluation(reg, cb)
    opencode = ScriptedOpenCode()
    run(make(reg, opencode).handle_message(command_for(task)))
    assert task.status == "succeeded" and task.outcome == "not_applied", task.error
    assert opencode.started == [] and "protected (named *backstop*)" in task.summary
    assert reg.profiles[cb.id].evaluation["followUpStatus"] == "flagged"


def test_a_refactor_with_side_effects_is_never_invoked(reg: Reg) -> None:
    cb = reg.candybar("dumply-daily-story", runner={"kind": "hermes", "profile": "dumply", "job_id": "abc"})
    _, task = _with_parent_evaluation(reg, cb, recommendation="Run at 21:00 instead.")
    steward = make(reg, ScriptedOpenCode(block({"applied": True, "summary": "Moved it.", "commits": ["candybar edit dumply-daily-story --schedule '0 21 * * *'"]})))
    _prove_with(steward, reg, "success", 0)  # would answer an invoke, if one were sent
    run(steward.handle_message(command_for(task)))
    assert task.outcome == "done" and task.invocation_id is None
    assert [e.type for e in reg.published if "invoke" in e.type] == []


# ---------------------------------------------------------------------------
# 2. A failed enrich-refresh queue never fails the change.


def _refresh_bus_blip(steward: Any, reg: Reg, *, proof: bool) -> None:
    real = steward._publish_raw

    async def publish(event: Any) -> None:
        if event.type == "bloodbank.cron.candybar.steward":
            raise OSError("bus blip")  # the enrich refresh's command
        await real(event)
        if proof and event.type == "bloodbank.cron.candybar.invoke":
            reg.invocations[event.data.invocation_id] = SimpleNamespace(result="success", exit_code=0)

    steward._publish_raw = publish


def test_a_refresh_that_cannot_be_queued_keeps_the_fix(reg: Reg) -> None:
    cb = reg.candybar()
    parent = reg.task(cb, "evaluate", status="succeeded", model="automaticai/auto")
    task = reg.task(cb, "fix", parent_task_id=parent.id)
    reg.profiles[cb.id] = SimpleNamespace(
        candybar_id=cb.id, overview=None, template_version=None, project=None, enriched_at=None,
        enrich_task_id=None, evaluation={"taskId": str(parent.id), "followUpStatus": "queued", "followUpTaskId": str(task.id)},
        updated_at=None,
    )
    steward = make(reg, ScriptedOpenCode(block({"repaired": True, "summary": "Moved it.", "commits": ["x@1 fix"]})))
    _refresh_bus_blip(steward, reg, proof=True)
    run(steward.handle_message(command_for(task)))
    assert task.status == "succeeded" and task.outcome == "fixed", task.error
    assert task.invocation_id is not None and "success (exit 0)" in task.summary
    assert "Overview refresh not queued (OSError: bus blip)" in task.summary
    assert "candybar steward enrich vault" in task.summary
    assert [t.action for t in reg.tasks.values()].count("enrich") == 0
    assert reg.profiles[cb.id].evaluation["followUpStatus"] == "done"


def test_a_refresh_that_cannot_be_queued_keeps_the_refactor(reg: Reg) -> None:
    cb = reg.candybar()
    _, task = _with_parent_evaluation(reg, cb)
    steward = make(reg, ScriptedOpenCode(block({"applied": True, "summary": "Trimmed.", "commits": ["x@1 trim"]})))
    _refresh_bus_blip(steward, reg, proof=False)
    run(steward.handle_message(command_for(task)))
    assert task.status == "succeeded" and task.outcome == "done", task.error
    assert "Overview refresh not queued" in task.summary
    assert reg.profiles[cb.id].evaluation["followUpStatus"] == "done"


# ---------------------------------------------------------------------------
# 3. One mutating task per repo (the git root of the checkout), not per workdir.


def test_two_homes_in_one_repo_share_the_lock(reg: Reg, tmp_path: Path) -> None:
    portal = _git(tmp_path / "client-portal")
    (portal / "gateway").mkdir()
    link = tmp_path / "portal-link"
    link.symlink_to(portal)
    a, b, c = reg.candybar("token-usage-report"), reg.candybar("newapi-db-pull"), reg.candybar("newapi-vm-health")
    # A hermes job whose workdir (and checkout) is a subdirectory; a unit
    # whose home is the repo; one reached through a symlink.
    homes = {
        a.id: (portal / "gateway", portal / "gateway"),
        b.id: (portal, portal),
        c.id: (link, link),
    }
    tasks = [reg.task(a, "fix"), reg.task(b, "fix"), reg.task(c, "fix")]
    inside = peak = 0
    gate = asyncio.Event()

    async def answer() -> TurnResult:
        nonlocal inside, peak
        inside += 1
        peak = max(peak, inside)
        await gate.wait()
        await asyncio.sleep(0)
        inside -= 1
        return TurnResult("ses_test", "succeeded", [block({"repaired": False, "summary": "Needs the operator."})])

    async def context(**kwargs: Any) -> ContextBundle:
        workdir, checkout = homes[next(k for k in homes if str(k) == kwargs["definition"]["id"])]
        return ContextBundle(
            definition=kwargs["definition"], runner=kwargs["runner"], entrypoint=kwargs["entrypoint"],
            invocations=[], profile=None, checkout=checkout, workdir=workdir,
        )

    steward = make(reg, ScriptedOpenCode(answer, answer, answer), context=context)

    async def scenario() -> None:
        running = [asyncio.create_task(steward.handle_message(command_for(t))) for t in tasks]
        for _ in range(200):
            await asyncio.sleep(0)
        assert inside == 1  # the others wait for the repo lock
        gate.set()
        await asyncio.gather(*running)

    run(scenario())
    assert peak == 1 and all(t.status == "succeeded" for t in tasks)
    assert list(steward._locks) == [str(portal.resolve())]


# ---------------------------------------------------------------------------
# 4. Symlinks, remote lookup, transition_exec.


def test_git_root_resolves_symlinks(nogit: Path) -> None:
    tmp_path = nogit
    skills = _git(tmp_path / "code" / "skillex" / "all-skills")
    script = skills / "activity-report" / "scripts" / "activity-report"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    bin_dir = tmp_path / "local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "activity-report").symlink_to(script)
    assert git_root(bin_dir / "activity-report") == skills.resolve()
    assert git_root(bin_dir) is None


def test_a_systemd_execstart_link_resolves_to_its_repo(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skills = _git(home / "code" / "skillex" / "all-skills", {"origin": "https://github.com/delorenj/skills.git"})
    _git(home / "code" / "skillex", {"origin": "git@github.com:delorenj/skillex.git"})
    script = skills / "activity-report" / "scripts" / "activity-report"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho report\n")
    link = home / "local" / "bin" / "activity-report"
    link.parent.mkdir(parents=True)
    link.symlink_to(script)
    unit = f"[Service]\nExecStart={link} run --client %i\n"
    monkeypatch.setattr(ctx, "_systemctl", lambda *args: unit if args[0] == "cat" else None)
    bundle = gather(
        definition=_definition("activity-report-james-brennan"),
        runner={"kind": "systemd", "unit": "activity-report@james-brennan.service"},
        entrypoint=None, invocations=[], profile=None, ledger_path=home / "no-ledger.yaml",
    )
    assert bundle.workdir == skills.resolve() and bundle.checkout == skills.resolve()
    assert bundle.repo_slug == "delorenj/skills"


def test_a_ledger_repo_is_found_by_its_remote(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ~/docker holds delorenj/DeLoContainers: no name match under ~/code.
    docker = _git(home / "docker", {"origin": "git@github.com:delorenj/DeLoContainers.git"})
    hermes_agent = _git(home / "hermes" / "hermes-agent", {"delorenj": "git@github.com:delorenj/hermes-agent.git"})
    nested = _git(home / "code" / "33GOD" / "flume", {"origin": "git@github.com:delorenj/flume-service"})
    _git(home / "code" / "flume", {"origin": "git@github.com:someone/flume"})
    monkeypatch.setenv("CANDYBAR_CHECKOUT_ROOTS", f"{docker}:{hermes_agent}:{home / 'nowhere'}")
    ctx._remote_index.cache_clear()
    assert find_checkout("delorenj/DeLoContainers") == docker.resolve()
    assert find_checkout("delorenj/deloContainers") == docker.resolve()  # case-insensitive
    assert find_checkout("delorenj/hermes-agent") == hermes_agent.resolve()  # a remote not named origin
    assert find_checkout("delorenj/flume-service") == nested.resolve()  # ~/code two levels deep, by remote
    assert find_checkout("delorenj/flume") is None  # a name match whose origin is someone else's
    # And through gather: ipm-metrics' unit lives outside every repo.
    ledger = home / "ledger.yaml"
    ledger.write_text(LEDGER.format(name="ipm-metrics", repo="delorenj/DeLoContainers"))
    bundle = gather(
        definition=_definition("ipm-metrics"), runner={"kind": "systemd", "unit": "ipm-metrics.service"},
        entrypoint=None, invocations=[], profile=None, ledger_path=ledger,
    )
    assert bundle.workdir == docker.resolve() and bundle.local_repo == docker.resolve()
    assert bundle.workdir_reason == "the local checkout of the ledger's source of truth delorenj/DeLoContainers"


def test_the_live_checkout_wins_over_clones_and_worktrees(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code = home / "code"

    def checkout(path: Path, *, worktree: bool = False) -> Path:
        path.mkdir(parents=True)
        if worktree:
            (path / ".git").write_text("gitdir: /elsewhere\n")
        else:
            (path / ".git").mkdir()
        return path

    live = checkout(home / "HeyMa")
    clone = checkout(code / "HeyMa")
    wt_first = checkout(code / "aaa-wt", worktree=True)
    hidden = checkout(code / ".wt" / "x")
    remotes = {
        str(live): [("origin", "delorenj/HeyMa")],
        str(clone): [("origin", "delorenj/HeyMa")],
        str(wt_first): [("origin", "delorenj/tool")],
        str(hidden): [("origin", "delorenj/hidden")],
    }
    tool = checkout(code / "group" / "tool")
    remotes[str(tool)] = [("upstream", "delorenj/tool")]
    monkeypatch.setattr(ctx, "remote_slugs", lambda root: remotes.get(str(root), []))
    monkeypatch.setenv("CANDYBAR_CHECKOUT_ROOTS", str(live))
    ctx._remote_index.cache_clear()
    assert find_checkout("delorenj/HeyMa") == live.resolve()  # the named root, not the ~/code clone
    assert find_checkout("delorenj/HeyMa", [clone / "bin" / "wax"]) == clone.resolve()  # a hint wins
    assert find_checkout("delorenj/tool") == tool.resolve()  # a full checkout before a worktree
    assert ctx.remote_checkouts("delorenj/hidden") == []  # .wt and other hidden dirs are skipped


def test_the_default_checkout_roots_are_the_job_repos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CANDYBAR_CHECKOUT_ROOTS")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CANDYBAR_HERMES_HOME", str(tmp_path / ".hermes"))
    assert ctx.checkout_roots() == (
        tmp_path / "docker", tmp_path / ".agents", tmp_path / ".hermes" / "hermes-agent", tmp_path / "HeyMa",
    )


def test_unwrap_transition_exec() -> None:
    argv = [
        "/c/.venv/bin/python", "-m", "candybar.transition_exec", "--state-file", "/s/watchdog.json",
        "--local-only", "--", "/h/scripts/crank-watchdog.sh", "--quiet",
    ]
    assert unwrap_transition_exec(argv) == (argv[:7], ["/h/scripts/crank-watchdog.sh", "--quiet"])
    no_dashes = ["python", "-m", "candybar.transition_exec", "--state-file=/s", "--timeout", "30", "/x.sh"]
    assert unwrap_transition_exec(no_dashes) == (no_dashes[:6], ["/x.sh"])
    assert unwrap_transition_exec(["/bin/sh", "-c", "true"]) is None


def test_a_transition_exec_wrapper_resolves_to_the_wrapped_script(home: Path, nogit: Path) -> None:
    scripts = nogit / "hermes" / "profiles" / "33god-pm" / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "crank-watchdog.sh"
    script.write_text("#!/bin/sh\necho ok\n")
    argv = [
        str(ctx.REPO_ROOT / ".venv" / "bin" / "python"), "-m", "candybar.transition_exec",
        "--state-file", str(home / "state" / "watchdog.json"), "--local-only", "--", str(script),
    ]
    runner = {"kind": "exec", "argv": argv, "workdir": str(ctx.REPO_ROOT)}
    bundle = gather(
        definition=_definition("crank-watchdog", tags=["ops"]), runner=runner,
        entrypoint=None, invocations=[], profile=None, ledger_path=home / "no-ledger.yaml",
    )
    # Not in a git repo: its directory, never the wrapper's workdir (this repo).
    assert bundle.workdir == scripts and bundle.checkout == scripts
    assert "transition_exec-wrapped script" in bundle.workdir_reason
    assert str(script) in bundle.files
    # In a git repo: that repo.
    repo = _git(home / "code" / "zcrank", {"origin": "git@github.com:delorenj/zcrank.git"})
    inside = repo / "bin" / "watchdog.sh"
    inside.parent.mkdir()
    inside.write_text("#!/bin/sh\n")
    runner["argv"] = [*argv[:-1], str(inside)]
    bundle = gather(
        definition=_definition("crank-watchdog"), runner=runner,
        entrypoint=None, invocations=[], profile=None, ledger_path=home / "no-ledger.yaml",
    )
    assert bundle.workdir == repo.resolve() and bundle.repo_slug == "delorenj/zcrank"


# ---------------------------------------------------------------------------
# 5. Cancel settles the follow-up (worker side; the API side is in test_api_steward).


def test_settled_evaluation_only_touches_the_evaluation_that_points_at_the_task() -> None:
    task = uuid4()
    mine = {"taskId": str(uuid4()), "followUpStatus": "queued", "followUpTaskId": str(task)}
    assert settled_evaluation(mine, task, "failed", CANCELLED) == {**mine, "followUpStatus": "failed", "followUpNote": CANCELLED}
    assert settled_evaluation({**mine, "followUpTaskId": str(uuid4())}, task, "failed") is None
    assert settled_evaluation(None, task, "failed") is None


def test_a_cancelled_running_follow_up_notes_the_cancel(reg: Reg) -> None:
    cb = reg.candybar()
    _, task = _with_parent_evaluation(reg, cb)

    async def hang() -> TurnResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    steward = make(reg, ScriptedOpenCode(hang))
    msg = command_for(task)

    async def scenario() -> None:
        live = asyncio.create_task(steward.handle_message(msg))
        for _ in range(50):
            await asyncio.sleep(0)
        task.status, task.error = "failed", CANCELLED  # what the cancel route writes
        await asyncio.wait_for(live, timeout=5)

    run(scenario())
    stored = reg.profiles[cb.id].evaluation
    assert stored["followUpStatus"] == "failed" and stored["followUpNote"] == CANCELLED


# ---------------------------------------------------------------------------
# 6. Protection by name and the ops tag.


@pytest.mark.parametrize(
    ("name", "tags", "reason"),
    [
        ("crank-watchdog", ["ops", "crank", "no-agent", "migrated"], "tagged ops"),
        ("milkshake-mercury-backstop", [], "named *backstop*"),
        ("sync-vault-to-drive", ["backup"], "a backup job"),
        ("hermes-watchdog-sweep", ["migrated"], "named *watchdog*"),
        ("deploy-smoke-exec", [], "named *smoke*"),
        ("db-canary", [], "named *canary*"),
        ("candybar-policy-drift", [], "named *policy*"),
        ("forever-ago-vault", ["backups"], "a backup job"),
    ],
)
def test_protection_by_name_and_tag(name: str, tags: list[str], reason: str) -> None:
    assert protection({"id": str(uuid4()), "name": name, "tags": tags}) == reason


def test_an_ordinary_job_is_still_not_protected() -> None:
    assert protection({"id": str(uuid4()), "name": "dumply-daily-story", "tags": ["migrated", "hermes"]}) is None


# ---------------------------------------------------------------------------
# 7. The task's own failure is never an operator cancel.


def test_the_keepalive_never_reads_the_tasks_own_failure_as_a_cancel(reg: Reg) -> None:
    cb = reg.candybar()
    _, task = _with_parent_evaluation(reg, cb)
    steward = make(reg, ScriptedOpenCode(OpenCodeError("OpenCode turn failed")))
    steward._keepalive_s = 0.01
    real_settle = steward._settle_follow_up

    async def slow_settle(*args: Any) -> None:
        await asyncio.sleep(0.2)  # several keepalive beats after the `failed` write
        await real_settle(*args)

    steward._settle_follow_up = slow_settle  # type: ignore[method-assign]
    msg = command_for(task)
    run(steward.handle_message(msg))  # returns: no CancelledError
    assert task.status == "failed" and task.error == "OpenCode turn failed"
    assert msg.acks[-1] == "ack" and task.id not in steward._cancelled
    assert reg.profiles[cb.id].evaluation["followUpStatus"] == "failed"
    assert "followUpNote" not in reg.profiles[cb.id].evaluation  # its own failure, not a cancel


# ---------------------------------------------------------------------------
# 8. Read-only sessions deny delegation.


def test_read_only_sessions_deny_subagents(monkeypatch: pytest.MonkeyPatch) -> None:
    actions = {rule["action"] for rule in oc.READ_ONLY_PERMISSIONS}
    assert {"edit", "write", "patch", "subagent", "task"} <= actions
    assert all(rule["effect"] == "deny" and rule["resource"] == "*" for rule in oc.READ_ONLY_PERMISSIONS)
    sent: list[Any] = []

    def call(endpoint: Any, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        sent.append(body)
        return 200, {"data": {"id": "ses_x"}}

    monkeypatch.setattr(oc, "_call", call)
    oc.OpenCodeClient(lambda: oc.Endpoint("http://o.test", None)).create_session(
        title="t", model="automaticai/auto/fast", directory="/w", permissions=list(oc.READ_ONLY_PERMISSIONS)
    )
    assert {"action": "subagent", "resource": "*", "effect": "deny"} in json.loads(json.dumps(sent[0]))["permissions"]
