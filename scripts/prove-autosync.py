"""Opt-in live autosync proof against the API, never part of the test suite.

Creates uniquely named PAUSED canaries, edits both file formats atomically and
proves API convergence, lifecycle preservation, invalid-save refusal, and deletion
without pruning. Finally stops the temporary watcher and archives its canaries.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from candybar.migration.client import CandybarApi


def main() -> None:
    api = CandybarApi()
    prefix = f"autosync-proof-{uuid4().hex[:8]}"
    created: list[str] = []
    worker = None
    with tempfile.TemporaryDirectory(prefix="candybar-autosync-proof-") as raw:
        root = Path(raw)
        project = root / f"{prefix}-project"
        project.mkdir()
        legacy = root / "legacy" / "candybar.toml"
        legacy.parent.mkdir()
        names = [f"{prefix}-project", f"{prefix}-legacy"]
        files = [project / ".candybar.toml", legacy]

        def text(name: str, description: str) -> str:
            return (
                f'[candybar]\nname = "{name}"\ndescription = "{description}"\n'
                'schedule = "0 0 1 1 *"\nenabled = true\n'
                '[candybar.runner]\nkind = "exec"\nargv = ["/usr/bin/true"]\n'
            )

        def save(path: Path, content: str) -> None:
            tmp = path.with_name(path.name + ".saving")
            tmp.write_text(content)
            tmp.replace(path)

        def wait_for(check, label):
            until = time.monotonic() + 45
            while time.monotonic() < until:
                rows = {r["name"]: r for r in api.list_candybars()}
                if check(rows):
                    return rows
                time.sleep(0.2)  # bounded verification of this one invocation, not a clock
            raise RuntimeError(f"live proof timed out: {label}")

        config = root / "watch.toml"
        config.write_text(
            f'projects = [{json.dumps(str(project))}]\n'
            f'legacy = [{json.dumps(str(legacy))}]\ndebounce_ms = 100\nretries = 1\n'
        )
        for path, name in zip(files, names):
            save(path, text(name, "before"))
        logpath = root / "worker.log"
        try:
            with logpath.open("w") as log:
                worker = subprocess.Popen(
                    [sys.executable, "-m", "candybar.cli.main", "watch", "--config", str(config)],
                    stdout=log, stderr=subprocess.STDOUT,
                )
                rows = wait_for(lambda rs: all(n in rs for n in names), "both formats created")
                created = [rows[n]["id"] for n in names]
                assert all(not rows[n]["enabled"] and not rows[n]["archived"] for n in names)
                print("PASS both formats created PAUSED", flush=True)
                for job_id in created:
                    invocation = api.invoke(job_id)
                    def finished(_rows, invocation_id=invocation["invocationId"]):
                        result = api.get_invocation(invocation_id)
                        return result is not None and result["result"] == "success"
                    wait_for(finished, "side-effect-free canary terminal invocation")
                print("PASS both canaries executed /usr/bin/true to terminal success", flush=True)
                for path, name in zip(files, names):
                    save(path, text(name, "after atomic save"))
                rows = wait_for(
                    lambda rs: all(rs[n]["description"] == "after atomic save" for n in names),
                    "both atomic saves synced",
                )
                assert all(not rows[n]["enabled"] for n in names)
                print("PASS both atomic saves reached API; paused state preserved", flush=True)
                save(files[0], "invalid [ toml")
                time.sleep(1)
                assert api.get_candybar(created[0])["description"] == "after atomic save"
                print("PASS invalid save left live definition intact", flush=True)
                save(files[0], text(names[0], "recovered"))
                wait_for(lambda rs: rs[names[0]]["description"] == "recovered", "invalid-save recovery")
                files[1].unlink()
                time.sleep(1)
                assert api.get_candybar(created[1]) is not None
                assert api.get_candybar(created[1])["archived"] is False
                print("PASS correction recovered; removal did not prune", flush=True)
        finally:
            if worker is not None:
                worker.terminate()
                worker.wait(timeout=30)
            # Resolve even partial/uncertain creation before cleanup.
            for row in api.list_candybars():
                if row["name"] in names:
                    api.set_archived(row["id"], True)
            print("Cleanup: temporary watcher stopped; proof canaries archived", flush=True)
            if worker is not None and worker.returncode not in {0, -15}:
                print(logpath.read_text(), file=sys.stderr)


if __name__ == "__main__":
    main()
