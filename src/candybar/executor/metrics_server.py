"""The executor's own `/metrics` endpoint (AD-17, Story 7.2).

The executor role runs as a host process without the api role, so its
per-candybar series (AD-9) need a scrape endpoint of their own: port 8701,
read by the existing Prometheus through `host.docker.internal`. The series live
in the process-default Prometheus registry and are encoded exactly like the
api's `/metrics` (OpenMetrics when the scraper asks for it, so exemplars
survive).

A deliberately tiny asyncio HTTP server rather than a second uvicorn: uvicorn
swaps the process signal handlers while it serves, which must not interfere
with the daemon's stop handling, and `GET /metrics` + `GET /healthz` need no
framework. Observability never gates execution: a bind failure is logged
loudly and the executor runs on.
"""

from __future__ import annotations

import asyncio
import logging
import os

from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_client.exposition import choose_encoder

log = logging.getLogger(__name__)

METRICS_HOST_ENV = "CANDYBAR_EXECUTOR_METRICS_HOST"
METRICS_PORT_ENV = "CANDYBAR_EXECUTOR_METRICS_PORT"
DEFAULT_METRICS_HOST = "127.0.0.1"
DEFAULT_METRICS_PORT = 8701
# A scraper that opens a connection and says nothing must not pin a task.
REQUEST_TIMEOUT_SECONDS = 5
MAX_REQUEST_BYTES = 16384


def metrics_bind() -> tuple[str, int] | None:
    """(host, port) from the environment; None when the endpoint is disabled.

    `CANDYBAR_EXECUTOR_METRICS_PORT` set to an empty string turns it off
    (the unit suite does, so tests never contend for a real port).
    """
    raw = os.environ.get(METRICS_PORT_ENV)
    if raw is not None and raw.strip() == "":
        return None
    port = DEFAULT_METRICS_PORT if raw is None else int(raw)
    return os.environ.get(METRICS_HOST_ENV, DEFAULT_METRICS_HOST), port


def _response(status: str, body: bytes, content_type: str) -> bytes:
    head = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("latin-1") + body


def render(method: str, target: str, accept: str, registry: CollectorRegistry) -> bytes:
    """The full HTTP response for one request line (pure; the tests drive it)."""
    path = target.split("?", 1)[0]
    if method != "GET":
        return _response("405 Method Not Allowed", b"method not allowed\n", "text/plain")
    if path == "/metrics":
        encoder, content_type = choose_encoder(accept)
        return _response("200 OK", encoder(registry), content_type)
    if path == "/healthz":
        return _response("200 OK", b'{"status":"ok"}', "application/json")
    return _response("404 Not Found", b"not found\n", "text/plain")


def _accept_header(lines: list[str]) -> str:
    for line in lines:
        name, _, value = line.partition(":")
        if name.strip().lower() == "accept":
            return value.strip()
    return ""


class MetricsServer:
    """Start/stop wrapper around `asyncio.start_server`."""

    def __init__(self, host: str, port: int, registry: CollectorRegistry = REGISTRY) -> None:
        self.host = host
        self.port = port
        self._registry = registry
        self._server: asyncio.Server | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=REQUEST_TIMEOUT_SECONDS
            )
            lines = raw[:MAX_REQUEST_BYTES].decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) < 2:
                writer.write(_response("400 Bad Request", b"bad request\n", "text/plain"))
            else:
                writer.write(render(parts[0], parts[1], _accept_header(lines[1:]), self._registry))
            await writer.drain()
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            pass  # a client that vanished or never finished its request
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self._server.sockets or []
        if sockets:
            self.port = sockets[0].getsockname()[1]  # resolves port 0 for tests

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


async def start_metrics_server(registry: CollectorRegistry = REGISTRY) -> MetricsServer | None:
    """Bind the endpoint from the environment; None when disabled or unbindable."""
    bind = metrics_bind()
    if bind is None:
        log.info("executor metrics endpoint disabled (%s is empty)", METRICS_PORT_ENV)
        return None
    server = MetricsServer(*bind, registry=registry)
    try:
        await server.start()
    except OSError as exc:
        log.error(
            "executor /metrics could not bind %s:%d (%s); the executor runs WITHOUT a "
            "scrape endpoint",
            bind[0],
            bind[1],
            exc,
        )
        return None
    log.info("executor /metrics on %s:%d", server.host, server.port)
    return server
