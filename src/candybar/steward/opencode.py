"""OpenCode v2 HTTP client for the steward (steward-contract.md section 4, step 3).

The steward drives the user's background OpenCode service, the one the
`opencode` client itself starts and records in
`$XDG_STATE_HOME/opencode/service.json` (`{url, password, pid, version}`;
default `~/.local/state/opencode/service.json`). The URL moves when the
service restarts, so it is read on every session. Basic auth, user
`opencode`. `CANDYBAR_OPENCODE_URL` / `CANDYBAR_OPENCODE_SERVICE_FILE`
override; `~/.config/opencode/service.json` is a password-only fallback.

Routes (verified against the live server's `/openapi.json`, OpenCode 2.1.4):

- `POST /api/session` `{title, model: {providerID, id}, location: {directory}}`
- `POST /api/session/{id}/prompt` `{text}` (returns at once; the turn runs)
- `POST /api/experimental/session/{id}/wait` (204 once idle; we bound each
  call and re-check, so a dropped long poll never loses the outcome)
- `GET /api/session/{id}/message?order=desc&limit=N`: the `idle` message
  carries the turn's `outcome` (`succeeded|failed|interrupted`), assistant
  messages carry `content[].text`
- `POST /api/session/{id}/interrupt` on timeout

Blocking stdlib HTTP, run off the event loop by the async wrappers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

URL_ENV = "CANDYBAR_OPENCODE_URL"
SERVICE_FILE_ENV = "CANDYBAR_OPENCODE_SERVICE_FILE"
BASIC_USER = "opencode"
PROVIDER = "automaticai"
# One long-poll slice: the wait route is re-armed after this many seconds
# (short, so a shutdown never waits long on a blocked HTTP call).
WAIT_SLICE_S = 30.0
REQUEST_TIMEOUT_S = 30.0
MESSAGE_PAGE = 50


class OpenCodeError(RuntimeError):
    """OpenCode refused a request, is unreachable, or a turn did not succeed."""


class OpenCodeTimeout(OpenCodeError):
    """A turn ran past its deadline (the session was interrupted)."""


def _state_file() -> Path:
    override = os.environ.get(SERVICE_FILE_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "opencode" / "service.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class Endpoint:
    url: str
    credential: str | None  # the Basic-auth secret, never logged

    @classmethod
    def discover(cls) -> Endpoint:
        state = _read_json(_state_file())
        legacy = _read_json(Path.home() / ".config" / "opencode" / "service.json")
        url = os.environ.get(URL_ENV) or state.get("url")
        if not isinstance(url, str) or not url:
            raise OpenCodeError(
                f"no OpenCode service URL: {_state_file()} has none (is the background "
                f"service running? `opencode` starts it) and {URL_ENV} is unset"
            )
        basic = state.get("password") or legacy.get("password")
        return cls(url=url.rstrip("/"), credential=basic if isinstance(basic, str) else None)

    def headers(self, *, json_body: bool) -> dict[str, str]:
        out = {"Accept": "application/json"}
        if json_body:
            out["Content-Type"] = "application/json"
        if self.credential:
            token = base64.b64encode(f"{BASIC_USER}:{self.credential}".encode()).decode()
            out["Authorization"] = f"Basic {token}"
        return out


def _call(
    endpoint: Endpoint, method: str, path: str, body: Any = None, *, timeout: float = REQUEST_TIMEOUT_S
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        endpoint.url + path, data=data, method=method, headers=endpoint.headers(json_body=data is not None)
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise OpenCodeError(f"OpenCode {method} {path}: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OpenCodeError(f"OpenCode unreachable at {endpoint.url} ({method} {path}): {exc}") from exc


def _texts(message: dict[str, Any]) -> str:
    parts = message.get("content") or []
    return "\n".join(
        part.get("text", "") for part in parts if isinstance(part, dict) and part.get("type") == "text"
    ).strip()


@dataclass(frozen=True)
class TurnResult:
    session_id: str
    outcome: str
    # Assistant texts written during the turn, newest first (empty ones dropped).
    answers: list[str]
    cost: float | None = None


class OpenCodeClient:
    """One steward's view of the OpenCode service. Every method blocks: use the async ones."""

    def __init__(
        self,
        endpoint_factory: Callable[[], Endpoint] = Endpoint.discover,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait_slice_s: float = WAIT_SLICE_S,
    ) -> None:
        self._endpoint_factory = endpoint_factory
        self._clock = clock
        self._wait_slice_s = wait_slice_s

    # --- blocking primitives -------------------------------------------------

    def create_session(self, *, title: str, model: str, directory: str) -> tuple[Endpoint, str]:
        endpoint = self._endpoint_factory()
        provider, _, model_id = model.partition("/")
        if not model_id:
            raise OpenCodeError(f"model {model!r} is not <provider>/<id>")
        _, body = _call(
            endpoint,
            "POST",
            "/api/session",
            {
                "title": title,
                "model": {"providerID": provider, "id": model_id},
                "location": {"directory": directory},
            },
        )
        session_id = (body or {}).get("data", {}).get("id")
        if not isinstance(session_id, str) or not session_id.startswith("ses"):
            raise OpenCodeError(f"OpenCode created no session: {str(body)[:200]}")
        return endpoint, session_id

    def prompt(self, endpoint: Endpoint, session_id: str, text: str) -> float:
        """Send one prompt; returns its creation time (epoch ms) as the turn's floor."""
        _, body = _call(endpoint, "POST", f"/api/session/{session_id}/prompt", {"text": text})
        created = ((body or {}).get("data") or {}).get("time", {}).get("created")
        return float(created) if isinstance(created, (int, float)) else time.time() * 1000

    def messages(self, endpoint: Endpoint, session_id: str, limit: int = MESSAGE_PAGE) -> list[dict[str, Any]]:
        _, body = _call(endpoint, "GET", f"/api/session/{session_id}/message?order=desc&limit={limit}")
        data = (body or {}).get("data")
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []

    def interrupt(self, endpoint: Endpoint, session_id: str) -> None:
        try:
            _call(endpoint, "POST", f"/api/session/{session_id}/interrupt", {})
        except OpenCodeError as exc:
            log.warning("could not interrupt %s: %s", session_id, exc)

    def session_cost(self, endpoint: Endpoint, session_id: str) -> float | None:
        try:
            _, body = _call(endpoint, "GET", f"/api/session/{session_id}")
        except OpenCodeError:
            return None
        cost = ((body or {}).get("data") or {}).get("cost")
        return float(cost) if isinstance(cost, (int, float)) else None

    def _idle_since(self, endpoint: Endpoint, session_id: str, floor_ms: float) -> dict[str, Any] | None:
        for message in self.messages(endpoint, session_id, limit=5):
            created = (message.get("time") or {}).get("created") or 0
            if message.get("type") == "idle" and created >= floor_ms:
                return message
        return None

    def wait_turn(
        self, endpoint: Endpoint, session_id: str, floor_ms: float, timeout_s: float
    ) -> TurnResult:
        """Block until the turn that started at `floor_ms` is idle, or interrupt at the deadline."""
        deadline = self._clock() + timeout_s
        while True:
            idle = self._idle_since(endpoint, session_id, floor_ms)
            if idle is not None:
                break
            remaining = deadline - self._clock()
            if remaining <= 0:
                self.interrupt(endpoint, session_id)
                raise OpenCodeTimeout(
                    f"OpenCode session {session_id} did not finish within {timeout_s:.0f}s; interrupted"
                )
            slice_s = max(1.0, min(self._wait_slice_s, remaining))
            began = self._clock()
            try:
                _call(
                    endpoint,
                    "POST",
                    f"/api/experimental/session/{session_id}/wait",
                    timeout=slice_s + 5,
                )
            except OpenCodeError as exc:
                # A long poll cut short (proxy, restart, our own slice) says nothing
                # about the turn: the idle check above decides. A hard failure
                # repeats until the deadline interrupts.
                log.debug("wait on %s returned early: %s", session_id, exc)
            if self._clock() - began < 1.0:
                # The wait came back at once without the turn being over (or the
                # service is down): back off so the loop never spins hot.
                self._pause(min(2.0, max(0.0, deadline - self._clock())))
        outcome = str(idle.get("outcome") or "unknown")
        if outcome != "succeeded":
            error = idle.get("error")
            raise OpenCodeError(f"OpenCode turn {outcome}: {json.dumps(error)[:300] if error else 'no detail'}")
        answers = []
        for message in self.messages(endpoint, session_id):
            created = (message.get("time") or {}).get("created") or 0
            if created < floor_ms:
                break
            if message.get("type") == "assistant":
                text = _texts(message)
                if text:
                    answers.append(text)
        return TurnResult(
            session_id=session_id,
            outcome=outcome,
            answers=answers,
            cost=self.session_cost(endpoint, session_id),
        )

    def _pause(self, seconds: float) -> None:
        # A bounded backoff inside one blocking call, not a schedule.
        if seconds > 0:
            threading.Event().wait(seconds)

    # --- async wrappers ------------------------------------------------------

    async def start(self, *, title: str, model: str, directory: str) -> tuple[Endpoint, str]:
        return await asyncio.to_thread(self.create_session, title=title, model=model, directory=directory)

    async def turn(self, endpoint: Endpoint, session_id: str, text: str, timeout_s: float) -> TurnResult:
        floor = await asyncio.to_thread(self.prompt, endpoint, session_id, text)
        # The prompt's own creation stamp is the floor; allow the idle marker a
        # few ms of clock skew below it.
        return await asyncio.to_thread(self.wait_turn, endpoint, session_id, floor - 5, timeout_s)

    async def stop(self, endpoint: Endpoint, session_id: str) -> None:
        await asyncio.to_thread(self.interrupt, endpoint, session_id)


SessionCallback = Callable[[str], Awaitable[None]]
