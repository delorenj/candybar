"""Project classification for an enriched candybar (steward-contract.md section 4, enrich).

1. `method: "ledger"`, confidence 1: the job's own checkout (the ledger
   repo's local checkout, or the git root of the runner's home when the
   ledger names no repo or names this candybar repo, which only records the
   migration) is (or sits inside) a pjangler project's `repo_path` (the
   deepest one wins). A job only falls back to this repo's checkout when
   nothing else was found, and that fallback is never classified here.
2. Otherwise `method: "jev"`: TypeSafe's Jev through the fleet's Jev ingress
   (`https://jev.delo.sh/typesafe/v1/systemone`, model `jev-1.13.0`, the
   steward's own LiteLLM virtual key in `CANDYBAR_JEV_KEY`), one `noul`
   question per pjangler project over the candybar's name, description,
   tags, overview and paths. Accept P >= 0.6; the highest wins.
3. No match leaves the project null.

The request shape is Wax's and pilot's (`HeyMa/components/wax/src/wax/jev.py`,
`33GOD/pilot/src/harvest/jev.js`): `{model, state, questions}` answered by
`{answers: {<name>: {type: "noul", noul: P}}}`. Every answer is validated:
a missing one is an error, never a silent 0.0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REGISTRY_URL_ENV = "CANDYBAR_PJANGLER_REGISTRY_URL"
DEFAULT_REGISTRY_URL = "http://127.0.0.1:8764/v1/registry"
JEV_URL_ENV = "CANDYBAR_JEV_URL"
DEFAULT_JEV_URL = "https://jev.delo.sh/typesafe/v1/systemone"
JEV_KEY_ENV = "CANDYBAR_JEV_KEY"
JEV_MODEL = "jev-1.13.0"
ACCEPT_P = 0.6
CHUNK = 150  # questions per request (pilot's chunking; ~32K-token budget)
TIMEOUT_S = 20.0
ATTEMPTS = 2
STATE_CAP = 6000


class JevError(RuntimeError):
    """Jev or the pjangler registry failed, or answered malformed."""


@dataclass(frozen=True)
class ProjectInfo:
    project_id: str
    name: str
    description: str
    repo_path: str | None


@dataclass(frozen=True)
class Classification:
    project: dict[str, Any] | None  # the contract Project, or None
    note: str  # one line: how it was decided
    probabilities: dict[str, float] | None = None
    request_ids: tuple[str, ...] = ()


def _get_json(url: str, timeout: float = 10.0) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise JevError(f"pjangler registry unreadable at {url}: {exc}") from exc


def load_projects(url: str | None = None) -> list[ProjectInfo]:
    data = _get_json(url or os.environ.get(REGISTRY_URL_ENV, DEFAULT_REGISTRY_URL))
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        raise JevError("pjangler registry has no projects object")
    out = []
    for key, project in projects.items():
        if not isinstance(project, dict):
            continue
        out.append(
            ProjectInfo(
                project_id=str(project.get("project_id") or key),
                name=str(project.get("name") or key),
                description=str(project.get("description") or ""),
                repo_path=project.get("repo_path") if isinstance(project.get("repo_path"), str) else None,
            )
        )
    return out


def ledger_project(projects: list[ProjectInfo], checkout: Path | None) -> dict[str, Any] | None:
    """The project whose repo_path is (or contains) the checkout; the deepest match wins."""
    if checkout is None:
        return None
    target = checkout.resolve()
    best: tuple[int, ProjectInfo] | None = None
    for project in projects:
        if not project.repo_path:
            continue
        try:
            root = Path(project.repo_path).expanduser().resolve()
        except OSError:
            continue
        if target == root or root in target.parents:
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, project)
    if best is None:
        return None
    project = best[1]
    return {"id": project.project_id, "name": project.name, "confidence": 1.0, "method": "ledger"}


def question(project: ProjectInfo) -> dict[str, Any]:
    about = f" Description: {project.description}" if project.description else ""
    where = f" Its repository lives at {project.repo_path}." if project.repo_path else ""
    return {
        "type": "noul",
        "instructions": (
            f"Is the scheduled job described in `job` work for, or explicitly part of, the project "
            f'"{project.name}" (project id: {project.project_id})?{about}{where} A job belongs to a '
            "project when it runs that project's code, maintains its data or infrastructure, or "
            "reports on it; merely mentioning a shared tool does not count."
        ),
        "criteria": {
            "true": f"The job is work for {project.name} ({project.project_id}).",
            "false": f"The job is not part of {project.name} ({project.project_id}).",
        },
    }


def job_state(definition: dict[str, Any], overview: dict[str, Any] | None, paths: list[str]) -> dict[str, Any]:
    job: dict[str, Any] = {
        "name": definition.get("name"),
        "description": definition.get("description"),
        "tags": definition.get("tags", []),
        "paths": paths[:20],
    }
    if overview:
        job["overview"] = {
            k: overview.get(k)
            for k in ("summary", "purpose", "behavior", "outputs", "dependencies", "source")
            if overview.get(k) is not None
        }
    text = json.dumps(job, default=str)
    if len(text) > STATE_CAP and "overview" in job:
        job["overview"] = {k: job["overview"].get(k) for k in ("summary", "purpose", "source")}
    return {"job": job}


def _probability(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


class Jev:
    def __init__(self, key: str, *, url: str | None = None, model: str = JEV_MODEL, timeout: float = TIMEOUT_S) -> None:
        self.key = key
        self.url = url or os.environ.get(JEV_URL_ENV, DEFAULT_JEV_URL)
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> Jev | None:
        key = os.environ.get(JEV_KEY_ENV, "").strip()
        return cls(key) if key else None

    def decide(self, state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> tuple[dict[str, float], str]:
        body = json.dumps({"model": self.model, "state": state, "questions": questions}).encode()
        last: Exception | None = None
        for attempt in range(1, ATTEMPTS + 1):
            request = urllib.request.Request(
                self.url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                    "User-Agent": "candybar-steward",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode())
                    request_id = response.headers.get("x-typesafe-request-id") or ""
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:200]
                last = JevError(f"Jev refused the request: HTTP {exc.code} {detail}")
                if exc.code < 500 and exc.code != 429:
                    raise last from exc
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = JevError(f"Jev unreachable at {self.url}: {exc}")
            if attempt < ATTEMPTS:
                threading.Event().wait(1.0 * attempt)
        else:
            raise last or JevError("Jev failed")
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            raise JevError("Jev answer is malformed: no answers object")
        out: dict[str, float] = {}
        for name in questions:
            answer = answers.get(name)
            if not isinstance(answer, dict) or answer.get("type") != "noul" or not _probability(answer.get("noul")):
                raise JevError(f"Jev answer is malformed: no noul probability for {name!r}")
            out[name] = float(answer["noul"])
        return out, request_id

    def classify(self, state: dict[str, Any], projects: list[ProjectInfo]) -> Classification:
        probs: dict[str, float] = {}
        request_ids: list[str] = []
        for start in range(0, len(projects), CHUNK):
            chunk = projects[start : start + CHUNK]
            answers, request_id = self.decide(state, {p.project_id: question(p) for p in chunk})
            probs.update(answers)
            if request_id:
                request_ids.append(request_id)
        ranked = sorted(probs.items(), key=lambda kv: (-kv[1], kv[0]))
        by_id = {p.project_id: p for p in projects}
        top = {pid: round(p, 4) for pid, p in ranked[:5]}
        if ranked and ranked[0][1] >= ACCEPT_P:
            pid, p = ranked[0]
            project = {"id": pid, "name": by_id[pid].name, "confidence": round(p, 4), "method": "jev"}
            return Classification(project, f"Jev matched {pid} at P={p:.2f}", top, tuple(request_ids))
        best = f"{ranked[0][0]} at P={ranked[0][1]:.2f}" if ranked else "no projects"
        return Classification(None, f"Jev found no project at P>={ACCEPT_P} (best: {best})", top, tuple(request_ids))


def classify_project(
    *,
    definition: dict[str, Any],
    overview: dict[str, Any] | None,
    paths: list[str],
    checkout: Path | None,
    jev: Jev | None,
    registry_url: str | None = None,
) -> Classification:
    """The contract's enrich project rule (blocking; see `classify_project_async`)."""
    projects = load_projects(registry_url)
    found = ledger_project(projects, checkout)
    if found is not None:
        return Classification(found, f"the job's checkout {checkout} is pjangler project {found['id']}")
    if jev is None:
        return Classification(None, f"no ledger project and no Jev key ({JEV_KEY_ENV} unset)")
    return jev.classify(job_state(definition, overview, paths), projects)


async def classify_project_async(**kwargs: Any) -> Classification:
    return await asyncio.to_thread(classify_project, **kwargs)
