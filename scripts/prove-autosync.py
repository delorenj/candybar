"""Opt-in live autosync proof against the API, never part of the test suite.

Creates uniquely named PAUSED canaries, edits both file formats atomically and
proves API convergence, lifecycle preservation, invalid-save refusal, and deletion
without pruning. Finally stops the temporary watcher and archives its canaries.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from candybar.migration.client import CandybarApi


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--editor-hook', type=Path, help='Optional LazyVim candybar.lua to prove real editor saves')
    args = parser.parse_args()
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

        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        for path, name in zip(files, names):
            save(path, text(name, "before"))
        logpath = root / "worker.log"
        try:
            with logpath.open("w") as log:
                worker = subprocess.Popen(
                    [sys.executable, "-m", "candybar.cli.main", "watch", str(root)],
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
                if args.editor_hook is not None:
                    worker.terminate()
                    worker.wait(timeout=30)
                    for path, name in zip(files, names):
                        save(path, text(name, 'real editor save'))
                    lua = root / 'editor-proof.lua'
                    lua.write_text(
                        'local original, started, finished = vim.system, 0, 0\n'
                        'vim.system = function(cmd, opts, callback)\n'
                        '  started = started + 1\n'
                        '  return original(cmd, opts, function(result)\n'
                        '    callback(result)\n'
                        '    vim.schedule(function() finished = finished + 1 end)\n'
                        '  end)\nend\n'
                        f'local hook = dofile({json.dumps(str(args.editor_hook.absolute()))})\n'
                        'hook.setup()\n'
                        f'for _, path in ipairs({{{",".join(json.dumps(str(p)) for p in files)}}}) do\n'
                        "  vim.cmd('edit ' .. vim.fn.fnameescape(path))\n"
                        "  vim.cmd('write')\nend\n"
                        'assert(vim.wait(30000, function() return started == 2 and finished == 2 end, 100))\n'
                    )
                    subprocess.run(['nvim', '--headless', '-u', 'NONE', '-l', str(lua)],
                                   check=True, timeout=40)
                    rows = wait_for(
                        lambda rs: all(rs[n]['description'] == 'real editor save' for n in names),
                        'editor saves without native watcher',
                    )
                    assert all(not rows[n]['enabled'] for n in names)
                    print('PASS real Neovim saves of both formats reached API with native watcher stopped', flush=True)
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
