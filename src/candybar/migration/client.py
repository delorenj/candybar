"""REST client of the candybar api role for `candybar migrate` (Story 7.5, AD-21).

`candybar migrate` never writes the registry itself (AD-10): every
registration, definition re-sync and enable/disable goes through the API, so
each change publishes its `bloodbank.cron.candybar.*` fact like any other.
stdlib `urllib` only, like the rest of the CLI.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_URL_ENV = "CANDYBAR_API_URL"
DEFAULT_API_URL = "http://127.0.0.1:8700"
INVOCATIONS_PAGE = 500


class ApiError(Exception):
    """The API answered with an error (status set) or could not be reached (status None)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def unreachable(self) -> bool:
        return self.status is None


def api_url() -> str:
    return os.environ.get(API_URL_ENV, DEFAULT_API_URL).rstrip("/")


class CandybarApi:
    def __init__(self, base: str | None = None, *, timeout: float = 15) -> None:
        self.base = (base or api_url()).rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                problem = json.loads(detail)
                detail = problem.get("detail") or problem.get("title") or detail
            except (json.JSONDecodeError, AttributeError):
                pass
            raise ApiError(
                f"{method} {path}: {exc.code} {detail}", status=exc.code
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ApiError(f"candybar API unreachable at {self.base}: {exc}") from exc
        return json.loads(raw) if raw else None

    @staticmethod
    def _id(candybar_id: str) -> str:
        return urllib.parse.quote(candybar_id, safe="")

    def list_candybars(self) -> list[dict[str, Any]]:
        return self._call("GET", "/candybars")

    def get_candybar(self, candybar_id: str) -> dict[str, Any] | None:
        try:
            return self._call("GET", f"/candybars/{self._id(candybar_id)}")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def runner(self, candybar_id: str) -> dict[str, Any]:
        return self._call("GET", f"/candybars/{self._id(candybar_id)}/runner")

    def register(self, definition: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/candybars", definition)

    def update(self, candybar_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self._call("PATCH", f"/candybars/{self._id(candybar_id)}", changes)

    def set_enabled(self, candybar_id: str, enabled: bool) -> dict[str, Any]:
        return self._call(
            "POST", f"/candybars/{self._id(candybar_id)}/enabled", {"enabled": enabled}
        )

    def invocations(
        self, candybar_id: str, limit: int = INVOCATIONS_PAGE
    ) -> list[dict[str, Any]]:
        body = self._call(
            "GET", f"/candybars/{self._id(candybar_id)}/invocations?limit={limit}"
        )
        return list(body.get("invocations", []))

    # The operator control surface (`candybar ls|show|next|schedule|pause|
    # resume|skip|edit|history|tail`) reads and writes through the same client.

    def get_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        """The stored row, or None while the invocation is still in flight."""
        try:
            return self._call("GET", f"/invocations/{self._id(invocation_id)}")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def invoke(self, candybar_id: str) -> dict[str, Any]:
        return self._call("POST", f"/candybars/{self._id(candybar_id)}/invoke")

    def schedule(self, start: str, end: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"from": start, "to": end})
        return self._call("GET", f"/schedule?{query}")

    def schedule_next(self) -> dict[str, Any] | None:
        try:
            return self._call("GET", "/schedule/next")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def skip(self, candybar_id: str, fire_time: str) -> dict[str, Any]:
        return self._call(
            "POST", f"/candybars/{self._id(candybar_id)}/skip", {"fireTime": fire_time}
        )

    def skip_day(self, candybar_id: str, day: str) -> dict[str, Any]:
        return self._call(
            "POST", f"/candybars/{self._id(candybar_id)}/skip-rest-of-day", {"date": day}
        )

    def set_noise(self, candybar_id: str, noise: bool) -> dict[str, Any]:
        return self._call("PUT" if noise else "DELETE", f"/candybars/{self._id(candybar_id)}/noise")


__all__ = ["API_URL_ENV", "ApiError", "CandybarApi", "api_url"]
