"""API role: FastAPI REST + SSE implementing the CAP-8 contract (AD-11).

Story 1.4 lands the register endpoint (`POST /candybars`) and the daemon
role wiring; Story 1.7 adds `GET /metrics`; Story 1.8 adds manual invoke
(`POST /candybars/{id}/invoke`); Epic 5 builds the full read surface
(definitions, schedule window, noise, skips, SSE).
"""

from .app import create_app, problem
from .role import run_api

__all__ = ["create_app", "problem", "run_api"]
