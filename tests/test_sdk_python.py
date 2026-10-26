"""Story 4.1: the python SDK speaks the AD-8 process protocol.

Every matrix row runs through `Candybar.invoke(environ=...)`, and every file
the SDK writes is validated by the daemon's OWN reader (`ResultFile`) — the
contract test against the executor, not a copy of it.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import math
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import candybar_sdk
import pytest
from candybar_sdk import (
    Context,
    ProtocolError,
    Result,
    candybar,
    result_document,
    write_result,
)

from candybar.executor.runner import ResultFile, _read_result_file
from candybar.registry import load_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent
SDK_ROOT = REPO_ROOT / "sdks" / "python"
EXAMPLE_DIR = SDK_ROOT / "examples" / "hello"


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "CANDYBAR_INVOCATION_ID": "0192f0a4-0000-7000-8000-000000000001",
        "CANDYBAR_ID": "0192f0a4-0000-7000-8000-0000000000aa",
        "CANDYBAR_TRIGGER": "scheduled",
        "CANDYBAR_TICK_ID": "tick:29883066",
        "CANDYBAR_RESULT_FILE": str(tmp_path / "result.json"),
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def _run(handler, tmp_path: Path, **env_overrides: str) -> tuple[int, dict | None, str]:
    err = io.StringIO()
    env = _env(tmp_path, **env_overrides)
    code = candybar(handler).invoke(env, stderr=err)
    path = Path(env.get("CANDYBAR_RESULT_FILE") or tmp_path / "result.json")
    doc = None
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
        # The executor's own reader must accept every file the SDK writes.
        parsed, error = _read_result_file(path)
        assert error is None, error
        assert parsed == ResultFile.model_validate(doc)
    return code, doc, err.getvalue()


# --- I/O matrix -------------------------------------------------------------


def test_bare_success(tmp_path):
    code, doc, _ = _run(lambda ctx: None, tmp_path)
    assert code == 0
    assert doc == {"status": "success", "metrics": {"token_spend": 0}}


def test_success_with_message(tmp_path):
    code, doc, _ = _run(lambda ctx: "done 12", tmp_path)
    assert code == 0
    assert doc == {"status": "success", "message": "done 12", "metrics": {"token_spend": 0}}


def test_explicit_fail_result(tmp_path):
    code, doc, _ = _run(lambda ctx: Result.fail("quota"), tmp_path)
    assert code == 1
    assert doc == {"status": "fail", "message": "quota", "metrics": {"token_spend": 0}}


def test_explicit_success_result(tmp_path):
    code, doc, _ = _run(lambda ctx: Result.success(), tmp_path)
    assert code == 0 and doc["status"] == "success" and "message" not in doc


def test_metrics_accumulate(tmp_path):
    def handler(ctx: Context):
        ctx.add_tokens(100)
        ctx.add_tokens(23)
        ctx.add_cost(0.25)
        assert ctx.token_spend == 123 and ctx.cost_usd == 0.25

    code, doc, _ = _run(handler, tmp_path)
    assert code == 0
    assert doc["metrics"] == {"token_spend": 123, "cost_usd": 0.25}


def test_exception_maps_to_fail_and_keeps_metrics(tmp_path):
    def handler(ctx: Context):
        ctx.add_tokens(7)
        raise RuntimeError("boom")

    code, doc, err = _run(handler, tmp_path)
    assert code == 1
    assert doc == {"status": "fail", "message": "RuntimeError: boom", "metrics": {"token_spend": 7}}
    assert "Traceback" in err and "RuntimeError: boom" in err


def test_exception_without_text_uses_type_name(tmp_path):
    def handler(ctx):
        raise ValueError

    _, doc, _ = _run(handler, tmp_path)
    assert doc["message"] == "ValueError"


def test_async_handler(tmp_path):
    async def handler(ctx: Context):
        await asyncio.sleep(0)
        ctx.add_tokens(5)
        return "async ok"

    code, doc, _ = _run(handler, tmp_path)
    assert code == 0
    assert doc == {"status": "success", "message": "async ok", "metrics": {"token_spend": 5}}


def test_async_handler_raising(tmp_path):
    async def handler(ctx):
        raise KeyError("missing")

    code, doc, _ = _run(handler, tmp_path)
    assert code == 1 and doc["message"] == "KeyError: 'missing'"


@pytest.mark.parametrize(
    ("exit_code", "status", "message", "rc"),
    [(0, "success", None, 0), (None, "success", None, 0), (3, "fail", "exit 3", 1), ("bad", "fail", "bad", 1)],
)
def test_sys_exit_in_handler(tmp_path, exit_code, status, message, rc):
    def handler(ctx):
        sys.exit(exit_code)

    code, doc, _ = _run(handler, tmp_path)
    assert code == rc
    assert doc["status"] == status
    assert doc.get("message") == message


def test_keyboard_interrupt_is_fail(tmp_path):
    def handler(ctx):
        raise KeyboardInterrupt

    code, doc, _ = _run(handler, tmp_path)
    assert code == 1 and doc["message"] == "interrupted"


def test_unsupported_return_type_is_fail(tmp_path):
    code, doc, _ = _run(lambda ctx: 42, tmp_path)
    assert code == 1
    assert doc["status"] == "fail" and "int" in doc["message"]


@pytest.mark.parametrize(
    ("call", "exc"),
    [
        (lambda ctx: ctx.add_tokens(-1), ValueError),
        (lambda ctx: ctx.add_tokens(True), TypeError),
        (lambda ctx: ctx.add_tokens(1.5), TypeError),
        (lambda ctx: ctx.add_cost(math.nan), ValueError),
        (lambda ctx: ctx.add_cost(math.inf), ValueError),
        (lambda ctx: ctx.add_cost(-0.01), ValueError),
        (lambda ctx: ctx.add_cost("1"), TypeError),
        (lambda ctx: ctx.add_tokens(2**63), ValueError),
    ],
)
def test_bad_metric_takes_fail_path(tmp_path, call, exc):
    code, doc, _ = _run(call, tmp_path)
    assert code == 1
    assert doc["status"] == "fail"
    assert doc["message"].startswith(exc.__name__)


def test_manual_trigger_has_no_tick(tmp_path):
    seen = {}

    def handler(ctx: Context):
        seen.update(trigger=ctx.trigger, tick_id=ctx.tick_id, inv=ctx.invocation_id, cid=ctx.candybar_id)

    code, _, _ = _run(handler, tmp_path, CANDYBAR_TRIGGER="manual", CANDYBAR_TICK_ID="")
    assert code == 0
    assert seen == {
        "trigger": "manual",
        "tick_id": None,
        "inv": "0192f0a4-0000-7000-8000-000000000001",
        "cid": "0192f0a4-0000-7000-8000-0000000000aa",
    }


def test_scheduled_trigger_carries_tick(tmp_path):
    seen = {}
    _run(lambda ctx: seen.update(tick=ctx.tick_id, trig=ctx.trigger), tmp_path)
    assert seen == {"tick": "tick:29883066", "trig": "scheduled"}


def test_unknown_trigger_passes_through(tmp_path):
    seen = {}
    code, _, _ = _run(lambda ctx: seen.update(trig=ctx.trigger), tmp_path, CANDYBAR_TRIGGER="future")
    assert code == 0 and seen == {"trig": "future"}


def test_not_under_executor(tmp_path):
    called = []
    err = io.StringIO()
    code = candybar(lambda ctx: called.append(1)).invoke({}, stderr=err)
    assert code == 2
    assert not called
    assert "not running under the candybar executor" in err.getvalue()
    assert "CANDYBAR_RESULT_FILE" in err.getvalue()
    assert list(tmp_path.iterdir()) == []


def test_partial_env_writes_fail_naming_missing(tmp_path):
    called = []
    code, doc, err = _run(lambda ctx: called.append(1), tmp_path, CANDYBAR_ID="")
    assert code == 2
    assert not called
    assert doc["status"] == "fail" and "CANDYBAR_ID" in doc["message"]
    assert "CANDYBAR_ID" in err
    assert "CANDYBAR_INVOCATION_ID" not in doc["message"]


def test_long_message_truncated(tmp_path):
    def handler(ctx):
        raise RuntimeError("x" * 2000)

    _, doc, _ = _run(handler, tmp_path)
    assert len(doc["message"]) == candybar_sdk.MESSAGE_MAX == 500


def test_long_success_message_truncated(tmp_path):
    _, doc, _ = _run(lambda ctx: "y" * 900, tmp_path)
    assert doc["message"] == "y" * 500


# --- protocol plumbing ------------------------------------------------------


def test_unwritable_result_file_is_protocol_error(tmp_path):
    err = io.StringIO()
    env = _env(tmp_path, CANDYBAR_RESULT_FILE=str(tmp_path / "missing-dir" / "result.json"))
    assert candybar(lambda ctx: None).invoke(env, stderr=err) == 2
    assert "cannot write result file" in err.getvalue()


def test_write_is_atomic_no_temp_left(tmp_path):
    target = tmp_path / "result.json"
    write_result(target, result_document("success", "a"))
    write_result(target, result_document("fail", "b"))
    assert json.loads(target.read_text())["message"] == "b"
    assert [p.name for p in tmp_path.iterdir()] == ["result.json"]


def test_non_ascii_message_round_trips_utf8(tmp_path):
    _, doc, _ = _run(lambda ctx: "café ✓", tmp_path)
    assert doc["message"] == "café ✓"


def test_unstorable_characters_are_replaced(tmp_path):
    """A lone surrogate (non-UTF-8 filename in an OSError) would make the file
    unwritable; NUL would break the executor's Postgres write. Both -> U+FFFD."""

    def handler(ctx):
        raise FileNotFoundError(f"no such file: {os.fsdecode(b'bad-\xff')}\x00tail")

    code, doc, _ = _run(handler, tmp_path)
    assert code == 1
    assert doc["message"] == "FileNotFoundError: no such file: bad-��tail"


def test_result_rejects_bad_status_and_message():
    with pytest.raises(ValueError):
        Result("ok")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Result("fail", 5)  # type: ignore[arg-type]


def test_context_from_env_raises_protocol_error():
    with pytest.raises(ProtocolError):
        Context.from_env({"CANDYBAR_ID": "x"})


def test_context_identity_is_read_only(tmp_path):
    ctx = Context.from_env(_env(tmp_path))
    with pytest.raises(AttributeError):
        ctx.trigger = "manual"  # type: ignore[misc]


def test_decorated_object_calls_handler_directly(tmp_path):
    @candybar
    def main(ctx):
        """docstring kept"""
        return f"hi {ctx.trigger}"

    assert main(Context.from_env(_env(tmp_path))) == "hi scheduled"
    assert main.__name__ == "main" and main.__doc__ == "docstring kept"


def test_run_exits_with_invoke_code(tmp_path, monkeypatch):
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit) as exc:
        candybar(lambda ctx: Result.fail("no")).run()
    assert exc.value.code == 1


def test_document_shape_keys_are_exactly_ad8():
    doc = result_document("success", "m", token_spend=3, cost_usd=0.5)
    assert list(doc) == ["status", "message", "metrics"]
    assert list(doc["metrics"]) == ["token_spend", "cost_usd"]


# --- no NATS (AD-8 / NFR11) -------------------------------------------------


def test_sdk_declares_no_dependencies():
    meta = tomllib.loads((SDK_ROOT / "pyproject.toml").read_text())
    assert meta["project"]["name"] == "candybar-sdk"
    assert meta["project"]["dependencies"] == []
    assert meta["project"]["requires-python"] == ">=3.13"


def test_sdk_import_loads_no_nats_client():
    """Fresh interpreter: nats-py IS installed here, yet importing and running
    the SDK must not load it."""
    probe = (
        "import sys, candybar_sdk\n"
        "from candybar_sdk import _protocol\n"
        "bad = sorted(m for m in sys.modules if m == 'nats' or m.startswith('nats.'))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(SDK_ROOT / "src")},
    )
    assert out.stdout.strip() == ""
    # Counter-probe: the same detector does see nats once it is imported.
    seen = subprocess.run(
        [sys.executable, "-c", "import nats\n" + probe], capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(SDK_ROOT / "src")},
    )
    assert "nats" in seen.stdout.split(",")[0]


def test_sdk_source_imports_only_stdlib():
    allowed = set(sys.stdlib_module_names) | {"candybar_sdk", "__future__"}
    for path in (SDK_ROOT / "src" / "candybar_sdk").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [node.module.split(".")[0]]
            else:
                continue
            for root in roots:
                assert root in allowed, f"{path.name} imports non-stdlib {root!r}"


# --- example ----------------------------------------------------------------


def test_example_manifest_registers_and_is_executable():
    manifest = load_manifest(EXAMPLE_DIR)
    assert manifest.name == "python-sdk-hello"
    entry = Path(manifest.entrypoint)
    assert entry == (EXAMPLE_DIR / "hello.py").resolve()
    assert os.access(entry, os.X_OK)
    assert entry.read_text().startswith("#!/usr/bin/env python3\n")


def test_example_runs_as_a_process(tmp_path):
    env = {**os.environ, **_env(tmp_path, CANDYBAR_TRIGGER="manual", CANDYBAR_TICK_ID="")}
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    proc = subprocess.run(
        [str(EXAMPLE_DIR / "hello.py")], env=env, cwd=EXAMPLE_DIR, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads((tmp_path / "result.json").read_text())
    assert doc["status"] == "success"
    assert doc["metrics"] == {"token_spend": 142, "cost_usd": 0.0007}
    assert "[manual, no tick (manual)]" in doc["message"]
