"""Node SDK <-> executor contract (Story 4.2, AD-8).

Runs node candybars through the REAL executor spawn path (`run_invocation`)
and resolves them with the REAL verdict logic (`resolve_outcome`), so the
SDK's output is proven against the daemon's own reader — not a copy of it.
No bus, no database.

Skips when `node` is absent, unless CANDYBAR_REQUIRE_NODE=1 (CI), where a
missing node is a hard failure so this suite can never pass vacuously.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from candybar.executor import run_invocation
from candybar.executor.runner import ResultFile
from candybar.executor.worker import resolve_outcome
from candybar.registry import load_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent
SDK_ROOT = REPO_ROOT / "sdks" / "node"
SDK_ENTRY = SDK_ROOT / "src" / "index.js"
EXAMPLE_DIR = SDK_ROOT / "examples" / "hello-node"
NODE = shutil.which("node")

if NODE is None and os.environ.get("CANDYBAR_REQUIRE_NODE") == "1":
    raise RuntimeError("CANDYBAR_REQUIRE_NODE=1 but no `node` on PATH")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(autouse=True)
def _isolated_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")
    for var in ("HELLO_NODE_FAIL", "HELLO_NODE_TOKENS"):
        monkeypatch.delenv(var, raising=False)


def _job(tmp_path: Path, body: str) -> str:
    """An executable node candybar importing the SDK by absolute file URL."""
    script = tmp_path / f"job-{uuid4().hex[:8]}.mjs"
    script.write_text(
        "#!/usr/bin/env node\n" + body.replace("__SDK__", SDK_ENTRY.as_uri())
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _run(entrypoint: str, *, trigger: str = "scheduled", tick_id: str | None = "tick:29883100"):
    outcome = asyncio.run(
        run_invocation(
            entrypoint=entrypoint,
            invocation_id=uuid4(),
            candybar_id=uuid4(),
            trigger=trigger,
            tick_id=tick_id,
        )
    )
    return outcome, resolve_outcome(outcome)


def test_node_is_the_spine_floor() -> None:
    version = subprocess.run([NODE, "--version"], capture_output=True, text=True, check=True)
    major = int(version.stdout.strip().lstrip("v").split(".")[0])
    assert major >= 24, f"Node SDK floor is 24 LTS, found {version.stdout.strip()}"


def test_example_manifest_registers_and_points_at_an_executable() -> None:
    manifest = load_manifest(EXAMPLE_DIR)
    assert manifest.name == "hello-node"
    entry = Path(manifest.entrypoint)
    assert entry == (EXAMPLE_DIR / "candybar.mjs").resolve()
    assert os.access(entry, os.X_OK), "the executor execs the entrypoint directly"
    assert entry.read_text().startswith("#!/usr/bin/env node\n")


def test_example_success_lands_as_success_with_token_spend() -> None:
    outcome, verdict = _run(load_manifest(EXAMPLE_DIR).entrypoint)
    assert outcome.exit_code == 0, outcome.stderr_tail
    assert outcome.result_error is None
    assert outcome.result is not None and outcome.result.status == "success"
    assert verdict.success is True
    assert verdict.metrics.token_spend == 42
    assert verdict.metrics.cost_usd is None


def test_rejected_promise_lands_as_fail_with_message_and_spend(monkeypatch) -> None:
    monkeypatch.setenv("HELLO_NODE_FAIL", "1")
    monkeypatch.setenv("HELLO_NODE_TOKENS", "11")
    outcome, verdict = _run(load_manifest(EXAMPLE_DIR).entrypoint)
    assert outcome.exit_code == 1
    assert verdict.success is False
    assert verdict.message == "hello-node asked to fail (scheduled)"
    assert verdict.metrics.token_spend == 11  # failed LLM calls still spent tokens
    assert "handler failed" in outcome.stderr_tail


def test_manual_trigger_sees_null_tick(tmp_path: Path) -> None:
    job = _job(
        tmp_path,
        'import { candybar } from "__SDK__";\n'
        "await candybar(async (ctx) => JSON.stringify({ t: ctx.trigger, k: ctx.tickId }));\n",
    )
    outcome, verdict = _run(job, trigger="manual", tick_id=None)
    assert verdict.success is True
    assert outcome.result is not None
    assert json.loads(outcome.result.message) == {"t": "manual", "k": None}


def test_cost_and_tokens_round_trip_into_invocation_metrics(tmp_path: Path) -> None:
    job = _job(
        tmp_path,
        'import { candybar } from "__SDK__";\n'
        "await candybar(async (ctx) => { ctx.addTokens(1000); ctx.addCost(0.002);"
        " return { tokenSpend: 234, costUsd: 0.001 }; });\n",
    )
    _, verdict = _run(job)
    assert verdict.success is True
    assert verdict.metrics.token_spend == 1234
    assert verdict.metrics.cost_usd == pytest.approx(0.003)


def test_explicit_fail_status_is_honored_with_metrics(tmp_path: Path) -> None:
    job = _job(
        tmp_path,
        'import { candybar } from "__SDK__";\n'
        'await candybar(async (ctx) => { ctx.addTokens(5); return { status: "fail", message: "quota" }; });\n',
    )
    outcome, verdict = _run(job)
    assert outcome.exit_code == 1
    assert (verdict.success, verdict.message, verdict.metrics.token_spend) == (False, "quota", 5)


def test_invalid_metrics_never_produce_a_file_the_executor_rejects(tmp_path: Path) -> None:
    """A bad tokenSpend is caught by the SDK (fail + message), never written raw —
    the executor's metrics model is extra=forbid / int-only."""
    job = _job(
        tmp_path,
        'import { candybar } from "__SDK__";\n'
        "await candybar(async () => ({ tokenSpend: 1.5 }));\n",
    )
    outcome, verdict = _run(job)
    assert outcome.result_error is None  # the file parsed under ResultFile
    assert verdict.success is False
    assert verdict.message is not None and verdict.message.startswith("invalid result: ")


def test_every_sdk_shape_parses_under_the_daemon_result_model() -> None:
    """The SDK's toResult() output for a spread of inputs validates under the
    executor's ResultFile exactly (no extra keys anywhere)."""
    script = (
        f'import {{ toResult }} from "{SDK_ENTRY.as_uri()}";\n'
        "const cases = [\n"
        '  { status: "success" },\n'
        '  { status: "success", message: "m", tokenSpend: 3 },\n'
        '  { status: "fail", message: "x".repeat(900), tokenSpend: 0, costUsd: 0 },\n'
        '  { status: "fail", costUsd: 1.25 },\n'
        "];\n"
        "console.log(JSON.stringify(cases.map(toResult)));\n"
    )
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    shapes = json.loads(out.stdout)
    assert len(shapes) == 4
    for raw in shapes:
        parsed = ResultFile.model_validate(raw)
        assert set(raw) <= {"status", "message", "metrics"}
        assert set(raw["metrics"]) <= {"token_spend", "cost_usd"}
        assert parsed.metrics.token_spend == raw["metrics"]["token_spend"]
    assert len(shapes[2]["message"]) == 500
    assert "cost_usd" not in shapes[0]["metrics"]
