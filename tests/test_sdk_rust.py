"""Cross-language contract for the Rust SDK (story 4.3).

The crate is compiled with cargo, and its binaries are run through the
daemon's REAL executor runner (`run_invocation`). The result is then read by
the daemon's own `ResultFile` model and `resolve_outcome`. A drift in key
names, exit codes, metrics bounds or message capping fails here, not in
production.

Skips when cargo is absent unless CANDYBAR_REQUIRE_RUST=1. CI sets the flag,
so a missing toolchain can never make the suite pass vacuously.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest

from candybar.executor.runner import ResultFile, run_invocation
from candybar.executor.worker import resolve_outcome
from candybar.registry import load_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent
SDK_DIR = REPO_ROOT / "sdks" / "rust"
HELLO_DIR = SDK_DIR / "examples" / "hello"
CARGO = shutil.which("cargo")
REQUIRE_RUST = os.environ.get("CANDYBAR_REQUIRE_RUST") == "1"
TOKEN_SPEND_MAX = 2**63 - 1
TICK = "tick:29883066"

pytestmark = pytest.mark.skipif(
    CARGO is None and not REQUIRE_RUST,
    reason="cargo not on PATH (set CANDYBAR_REQUIRE_RUST=1 to make this a failure)",
)


def _cargo(*args: str, cwd: Path = SDK_DIR) -> subprocess.CompletedProcess[str]:
    if CARGO is None:
        pytest.fail("cargo is not on PATH (CANDYBAR_REQUIRE_RUST=1 or a live Rust SDK test needs it)")
    proc = subprocess.run(
        [CARGO, *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=600
    )
    if proc.returncode != 0:
        pytest.fail(f"cargo {' '.join(args)} failed:\n{proc.stderr[-4000:]}")
    return proc


def _executable(proc: subprocess.CompletedProcess[str], target: str) -> Path:
    """The built binary's path, from cargo's own artifact messages."""
    for line in proc.stdout.splitlines():
        msg = json.loads(line)
        if (
            msg.get("reason") == "compiler-artifact"
            and msg["target"]["name"] == target
            and msg.get("executable")
        ):
            return Path(msg["executable"])
    pytest.fail(f"cargo produced no executable for {target}")


def build_outcomes_probe() -> Path:
    """Build `examples/outcomes.rs`, the probe covering every outcome mapping."""
    proc = _cargo("build", "--locked", "--example", "outcomes", "--message-format=json-render-diagnostics")
    return _executable(proc, "outcomes")


def build_hello() -> Path:
    """Build the author-style example crate where its candybar.toml expects it."""
    proc = _cargo(
        "build", "--locked", "--release",
        "--target-dir", str(HELLO_DIR / "target"),
        "--message-format=json-render-diagnostics",
        cwd=HELLO_DIR,
    )
    return _executable(proc, "candybar-hello")


@pytest.fixture(scope="module")
def probe() -> Path:
    return build_outcomes_probe()


@pytest.fixture(scope="module")
def hello() -> Path:
    return build_hello()


@pytest.fixture(autouse=True)
def _unscoped_work_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")


def _run(entrypoint: Path, *, trigger: str = "scheduled", tick_id: str | None = TICK):
    invocation_id, candybar_id = uuid4(), uuid4()
    outcome = asyncio.run(
        run_invocation(
            entrypoint=str(entrypoint),
            invocation_id=invocation_id,
            candybar_id=candybar_id,
            trigger=trigger,
            tick_id=tick_id,
            root="",
        )
    )
    return outcome, invocation_id, candybar_id


# mode -> (exit code, success, resolved message, token_spend, cost_usd)
MODES = {
    "success": (0, True, None, 0, None),
    "metrics": (0, True, None, 123, 0.25),
    "fail": (1, False, "quota", 7, None),
    "err": (1, False, "boom", 11, None),
    "panic": (1, False, "panic: kaboom", 13, None),
    "overflow": (0, True, None, TOKEN_SPEND_MAX, None),
    "threads": (0, True, None, 1000, 0.0042),
}


@pytest.mark.parametrize("mode", sorted(MODES))
def test_every_outcome_reads_back_through_the_real_executor(
    probe: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CANDYBAR_EXAMPLE_MODE", mode)  # inherited by the child
    exit_code, success, message, tokens, cost = MODES[mode]
    outcome, _, _ = _run(probe)
    assert outcome.spawn_error is None
    assert outcome.result_error is None, outcome.result_error  # file valid for the daemon
    assert outcome.result is not None
    assert outcome.exit_code == exit_code
    resolved = resolve_outcome(outcome)
    assert resolved.success is success
    assert resolved.message == message
    assert resolved.metrics.token_spend == tokens
    assert resolved.metrics.cost_usd == cost


def test_success_message_is_carried_in_the_result_file(probe: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANDYBAR_EXAMPLE_MODE", "metrics")
    outcome, _, _ = _run(probe)
    assert outcome.result.status == "success"
    assert outcome.result.message == "done"


def test_err_and_panic_leave_failure_detail_on_stderr(probe: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANDYBAR_EXAMPLE_MODE", "err")
    outcome, _, _ = _run(probe)
    assert "candybar-sdk: invocation failed: boom" in outcome.stderr_tail

    monkeypatch.setenv("CANDYBAR_EXAMPLE_MODE", "panic")
    outcome, _, _ = _run(probe)
    assert "panicked" in outcome.stderr_tail and "kaboom" in outcome.stderr_tail
    assert "candybar-sdk: invocation failed: panic: kaboom" in outcome.stderr_tail


def test_nul_never_reaches_what_the_executor_stores(probe: Path, monkeypatch) -> None:
    # Postgres text rejects NUL: one in error_message or stderr_tail would make
    # the terminal write fail and the command re-run on every redelivery.
    monkeypatch.setenv("CANDYBAR_EXAMPLE_MODE", "nul")
    outcome, _, _ = _run(probe)
    resolved = resolve_outcome(outcome)
    assert resolved.success is False
    assert resolved.message == "bad�byte"
    assert resolved.metrics.token_spend == 17
    assert "\x00" not in outcome.stderr_tail
    assert "invocation failed: bad�byte" in outcome.stderr_tail


def test_bad_cost_fails_the_invocation_and_keeps_the_file_valid(probe: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANDYBAR_EXAMPLE_MODE", "bad-cost")
    outcome, _, _ = _run(probe)
    assert outcome.result_error is None
    resolved = resolve_outcome(outcome)
    assert resolved.success is False
    assert resolved.message.startswith("cost_usd must be a finite number >= 0")
    assert resolved.metrics.token_spend == 5 and resolved.metrics.cost_usd is None


def test_exotic_long_message_is_valid_json_capped_at_500_chars(probe: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANDYBAR_EXAMPLE_MODE", "exotic")
    outcome, _, _ = _run(probe)
    assert outcome.result_error is None
    message = outcome.result.message
    assert len(message) == 500
    assert message.startswith('exotic 🍬"\\\n\t\x1b')


def test_context_reads_all_five_env_vars(probe: Path, monkeypatch) -> None:
    monkeypatch.setenv("CANDYBAR_EXAMPLE_MODE", "context")
    outcome, invocation_id, candybar_id = _run(probe)
    assert outcome.result.message == f"{invocation_id}|{candybar_id}|scheduled|{TICK}"

    outcome, invocation_id, candybar_id = _run(probe, trigger="manual", tick_id=None)
    assert outcome.result.message == f"{invocation_id}|{candybar_id}|manual|"

    outcome, _, _ = _run(probe, trigger="misfire")
    assert outcome.result.message.endswith(f"|misfire|{TICK}")


def _bare_env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "CANDYBAR_INVOCATION_ID": str(uuid4()),
        "CANDYBAR_ID": str(uuid4()),
        "CANDYBAR_TRIGGER": "scheduled",
        "CANDYBAR_TICK_ID": TICK,
        "CANDYBAR_RESULT_FILE": str(tmp_path / "result.json"),
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def test_outside_the_executor_exits_two_without_running(probe: Path, tmp_path: Path) -> None:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CANDYBAR_EXAMPLE_MODE": "success"}
    proc = subprocess.run([str(probe)], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    for name in ("CANDYBAR_INVOCATION_ID", "CANDYBAR_ID", "CANDYBAR_TRIGGER", "CANDYBAR_RESULT_FILE"):
        assert name in proc.stderr
    assert not list(tmp_path.iterdir())


def test_partial_env_writes_a_fail_result_the_daemon_accepts(probe: Path, tmp_path: Path) -> None:
    env = _bare_env(tmp_path, CANDYBAR_ID=None)
    proc = subprocess.run([str(probe)], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 2
    result = ResultFile.model_validate(json.loads((tmp_path / "result.json").read_text()))
    assert result.status == "fail"
    assert result.message == "not running under the candybar executor: missing CANDYBAR_ID"
    assert result.metrics.token_spend == 0


def test_unwritable_result_file_exits_one(probe: Path, tmp_path: Path) -> None:
    env = _bare_env(tmp_path, CANDYBAR_RESULT_FILE=str(tmp_path / "missing" / "result.json"))
    proc = subprocess.run([str(probe)], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 1
    assert "cannot write result file" in proc.stderr


def test_shipped_hello_manifest_registers_and_runs(hello: Path) -> None:
    manifest = load_manifest(HELLO_DIR)
    assert manifest.name == "hello-rust"
    assert Path(manifest.entrypoint) == hello.resolve()
    outcome, _, _ = _run(Path(manifest.entrypoint), trigger="manual", tick_id=None)
    resolved = resolve_outcome(outcome)
    assert outcome.exit_code == 0 and resolved.success is True
    assert outcome.result.message == "hello from rust [manual, no tick (manual)]"
    assert resolved.metrics.token_spend == 1545 and resolved.metrics.cost_usd == 0.0042


def test_crate_has_no_nats_or_any_other_dependency() -> None:
    meta = json.loads(_cargo("metadata", "--format-version", "1", "--locked").stdout)
    assert [p["name"] for p in meta["packages"]] == ["candybar-sdk"]
    (node,) = meta["resolve"]["nodes"]
    assert node["deps"] == [] and node["dependencies"] == []

    hello_meta = json.loads(_cargo("metadata", "--format-version", "1", "--locked", cwd=HELLO_DIR).stdout)
    names = sorted(p["name"] for p in hello_meta["packages"])
    assert names == ["candybar-hello", "candybar-sdk"]
    assert not any("nats" in name for name in names)


def test_manifest_declares_no_dependency_tables() -> None:
    manifest = tomllib.loads((SDK_DIR / "Cargo.toml").read_text())
    assert manifest.get("dependencies", {}) == {}
    for table in ("dev-dependencies", "build-dependencies", "target"):
        assert table not in manifest, f"{table} would let a bus client in"
    assert manifest["package"]["name"] == "candybar-sdk"
    assert manifest["package"]["rust-version"] == "1.99.1"


def test_sdk_source_has_no_network_or_unsafe_surface() -> None:
    source = "\n".join(p.read_text() for p in sorted((SDK_DIR / "src").rglob("*.rs")))
    for forbidden in (r"\bstd::net\b", r"\bTcpStream\b", r"\bUdpSocket\b", r"\bextern\s+crate\b", r"\bunsafe\s*\{"):
        assert not re.search(forbidden, source), forbidden
    assert "#![forbid(unsafe_code)]" in source
