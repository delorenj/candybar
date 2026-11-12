"""W5-1: the macOS executor profile (deploy/mac) is exec-only, clock-free, secret-free.

Everything here runs on Linux: the plist is parsed with plistlib, the shell
wrappers run against a fake repo layout with fake resolver/launchctl binaries,
and the cgroup assertions prove that CANDYBAR_CGROUP_ROOT="" never reads
/proc/self/cgroup (macOS has no /proc).
"""
from __future__ import annotations

import asyncio
import builtins
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest

from candybar.executor.runner import run_invocation

REPO = Path(__file__).resolve().parents[1]
MAC = REPO / "deploy" / "mac"
TEMPLATE = MAC / "io.33god.candybar-executor.plist.template"
PLACEHOLDERS = {"__REPO__": "/Users/u/code/33GOD/candybar", "__PATH__": "/Users/u/code/33GOD/candybar/.venv/bin:/usr/bin:/bin",
                "__HOME__": "/Users/u", "__PG_PORT__": "5435", "__NATS_PORT__": "4222", "__SESSION_TYPE__": "Aqua"}
SH = shutil.which("sh") or "/bin/sh"


def rendered_template() -> dict:
    text = TEMPLATE.read_text()
    for token, value in PLACEHOLDERS.items():
        text = text.replace(token, value)
    assert not re.search(r"__[A-Z_]+__", text), "template has a placeholder the tests do not know"
    return plistlib.loads(text.encode())


# ---- the plist template -----------------------------------------------------------------

def test_template_has_no_interval_keys_and_is_kept_alive_not_scheduled():
    plist = rendered_template()
    assert plist["Label"] == "io.33god.candybar-executor"
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    for key in ("StartInterval", "StartCalendarInterval", "StartOnMount", "WatchPaths", "QueueDirectories"):
        assert key not in plist
    assert "Disabled" not in plist


def test_template_environment_is_explicit_loopback_and_cgroup_free():
    env = rendered_template()["EnvironmentVariables"]
    assert env["CANDYBAR_CGROUP_ROOT"] == ""  # present AND empty: unset would fall back to /sys/fs/cgroup/candybar
    assert env["CANDYBAR_EXECUTOR_METRICS_HOST"] == "127.0.0.1"
    assert env["CANDYBAR_EXECUTOR_METRICS_PORT"] == "8701"
    assert env["NATS_URL"].startswith("nats://127.0.0.1:")
    assert env["DATABASE_URL"] == "postgresql+psycopg://candybar@127.0.0.1:5435/candybar"
    assert env["PATH"].split(":")[0].endswith("/.venv/bin") and env["HOME"].startswith("/Users/")
    assert env["CANDYBAR_NVIDIA_SMI"] == ""  # no pmon probing on a Mac


def test_template_carries_no_secret_and_no_linux_path():
    text = re.sub(r"<!--.*?-->", "", TEMPLATE.read_text(), flags=re.DOTALL)  # the header comment may name PGPASSWORD
    assert "op://" not in text and "/home/delorenj" not in text
    assert "PGPASSWORD" not in rendered_template()["EnvironmentVariables"]
    assert not re.search(r"://[^/@\s:]+:[^/@\s]+@", text)  # no DSN userinfo password


def test_template_names_exactly_one_session_type_and_is_not_throttled():
    plist = rendered_template()
    # One type (the installer picks it from --domain): two would let launchd load a second copy into the other domain.
    assert plist["LimitLoadToSessionType"] == "Aqua" and isinstance(plist["LimitLoadToSessionType"], str)
    assert "ProcessType" not in plist  # Background would throttle every job the executor spawns
    assert plist["ExitTimeOut"] == 60  # parity with the Linux unit's TimeoutStopSec


def test_standard_launchd_path_shape_is_accepted_by_the_installer():
    # The convention file's PATH keeps ~/.local/bin and mise shims ahead of Homebrew and the system dirs.
    assert rendered_template()["EnvironmentVariables"]["PATH"].endswith(":/usr/bin:/bin")


# ---- install-executor.sh ------------------------------------------------------------------

def make_launchctl(tmp_path: Path, loaded: tuple[str, ...] = (), plutil_ok: bool = True) -> tuple[Path, Path]:
    """A launchctl shim that logs every call; `print <target>` succeeds only for targets in `loaded`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "launchctl.log"
    shim = bin_dir / "launchctl"
    cases = "".join(f"    {target}) exit 0 ;;\n" for target in loaded)
    shim.write_text(f'#!/bin/sh\necho "$@" >> {log}\n[ "$1" = print ] || exit 0\ncase "$2" in\n{cases}    *) exit 113 ;;\nesac\n')
    shim.chmod(0o755)
    plutil = bin_dir / "plutil"
    plutil.write_text(f'#!/bin/sh\necho "plutil $@" >> {log}\n' + ("exit 0\n" if plutil_ok else "echo 'bad plist' >&2\nexit 1\n"))
    plutil.chmod(0o755)
    return bin_dir, log


def install(tmp_path: Path, *args: str, agent_env: str | None = "PATH=/Users/u/.local/bin:/usr/bin:/bin\nHOME=/Users/u\n",
            loaded: tuple[str, ...] = (), plutil_ok: bool = True):
    bin_dir, log = make_launchctl(tmp_path, loaded, plutil_ok)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env_file = tmp_path / "launchd-agent.env"
    if agent_env is not None:
        env_file.write_text(agent_env)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(home), "CANDYBAR_LAUNCHD_AGENT_ENV": str(env_file),
           "CANDYBAR_BACKUP_DIR": str(tmp_path / "backups")}
    result = subprocess.run([SH, str(MAC / "install-executor.sh"), *args], env=env, capture_output=True, text=True, timeout=30, check=False)
    return result, log, home


def launchctl_mutations(log: Path) -> list[str]:
    return [line for line in log.read_text().splitlines() if line.split()[0] in {"bootstrap", "bootout"}] if log.exists() else []


def test_installer_dry_run_renders_a_valid_plist_from_the_convention_path(tmp_path):
    result, log, home = install(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(result.stdout.encode())
    env = plist["EnvironmentVariables"]
    assert env["PATH"] == f"{REPO}/.venv/bin:/Users/u/.local/bin:/usr/bin:/bin"  # venv prepended, base order kept
    assert plist["ProgramArguments"] == [f"{REPO}/deploy/mac/run-executor.sh"]
    assert plist["WorkingDirectory"] == str(REPO)
    assert env["HOME"] == str(home) and plist["StandardOutPath"].startswith(str(home))
    assert env["DATABASE_URL"].endswith(":5435/candybar") and env["NATS_URL"].endswith(":4222")
    assert not (home / "Library").exists() and not log.exists()  # dry run writes and loads nothing


def test_installer_writes_but_does_not_load_without_load_flag(tmp_path):
    out = tmp_path / "out" / "io.33god.candybar-executor.plist"
    result, log, _home = install(tmp_path, "--out", str(out), "--pg-port", "5999", "--nats-port", "4333")
    assert result.returncode == 0, result.stderr
    env = plistlib.loads(out.read_bytes())["EnvironmentVariables"]
    assert env["DATABASE_URL"].endswith(":5999/candybar") and env["NATS_URL"] == "nats://127.0.0.1:4333"
    assert [line for line in log.read_text().splitlines() if not line.startswith("plutil")] == []  # no launchctl call at all
    # Re-running backs the previous plist up outside the repo.
    again, _, _ = install(tmp_path, "--out", str(out))
    assert again.returncode == 0 and len(list((tmp_path / "backups").iterdir())) == 1


@pytest.mark.parametrize("domain,session", [("gui", "Aqua"), ("user", "Background")])
def test_installer_load_bootstraps_exactly_one_label_into_the_chosen_domain(tmp_path, domain, session):
    out = tmp_path / "x.plist"
    result, log, _ = install(tmp_path, "--out", str(out), "--load", "--domain", domain)
    assert result.returncode == 0, result.stderr
    assert launchctl_mutations(log) == [f"bootstrap {domain}/{os.getuid()} {out}"]
    assert plistlib.loads(out.read_bytes())["LimitLoadToSessionType"] == session  # one type per domain, never both


def test_installer_reload_boots_out_only_its_own_label_from_the_target_domain_first(tmp_path):
    uid, out = os.getuid(), tmp_path / "x.plist"
    result, log, _ = install(tmp_path, "--out", str(out), "--load", loaded=(f"gui/{uid}/io.33god.candybar-executor",))
    assert result.returncode == 0, result.stderr
    assert launchctl_mutations(log) == [f"bootout gui/{uid}/io.33god.candybar-executor", f"bootstrap gui/{uid} {out}"]


def test_installer_refuses_to_load_a_second_copy_into_the_other_domain_and_writes_nothing(tmp_path):
    uid, out = os.getuid(), tmp_path / "x.plist"
    result, log, _ = install(tmp_path, "--out", str(out), "--load", "--domain", "user", loaded=(f"gui/{uid}/io.33god.candybar-executor",))
    assert result.returncode == 73 and "already loaded in gui" in result.stderr
    assert launchctl_mutations(log) == [] and not out.exists()


def test_installer_never_replaces_a_working_plist_with_one_that_fails_lint(tmp_path):
    out = tmp_path / "x.plist"
    out.write_text("previous working plist")
    result, log, _ = install(tmp_path, "--out", str(out), "--load", plutil_ok=False)
    assert result.returncode == 65 and "failed plutil -lint" in result.stderr
    assert out.read_text() == "previous working plist" and launchctl_mutations(log) == []
    assert not list(tmp_path.glob("x.plist.new.*"))  # the temp file is cleaned up


@pytest.mark.parametrize("args,agent_env", [(("--dry-run", "--domain", "system"), "PATH=/usr/bin\n"), (("--dry-run", "--pg-port", "54;35"), "PATH=/usr/bin\n"),
                                            (("--dry-run",), None), (("--dry-run",), "HOME=/Users/u\n"), (("--dry-run", "--bogus"), "PATH=/usr/bin\n")])
def test_installer_refuses_bad_input_without_writing(tmp_path, args, agent_env):
    result, log, home = install(tmp_path, *args, agent_env=agent_env)
    assert result.returncode != 0
    assert not log.exists() and not (home / "Library").exists()


def test_installer_accepts_an_explicit_path_when_there_is_no_convention_file(tmp_path):
    result, _, _ = install(tmp_path, "--dry-run", "--path", "/opt/homebrew/bin:/usr/bin", agent_env=None)
    assert result.returncode == 0, result.stderr
    assert plistlib.loads(result.stdout.encode())["EnvironmentVariables"]["PATH"] == f"{REPO}/.venv/bin:/opt/homebrew/bin:/usr/bin"


# ---- run-executor.sh (macOS) --------------------------------------------------------------

def fake_checkout(tmp_path: Path, script: Path) -> Path:
    """A repo-shaped tree: <root>/deploy/<dir>/<script> + a fake .venv/bin/candybar."""
    root = tmp_path / "checkout"
    (root / "deploy" / script.parent.name).mkdir(parents=True)
    shutil.copy(script, root / "deploy" / script.parent.name / script.name)
    candybar = root / ".venv" / "bin" / "candybar"
    candybar.parent.mkdir(parents=True)
    candybar.write_text('#!/bin/sh\nif [ "${CANDYBAR_CGROUP_ROOT+set}" = set ]; then echo "cgroup-root=[$CANDYBAR_CGROUP_ROOT]"; else echo cgroup-root=UNSET; fi\n'
                        'echo "pgpassword=${PGPASSWORD:-none}"\necho "dyld=${DYLD_FALLBACK_LIBRARY_PATH:-none}"\necho "path0=${PATH%%:*}"\necho "args=$*"\n')
    candybar.chmod(0o755)
    return root


def fake_resolver(tmp_path: Path) -> Path:
    resolver = tmp_path / "bin" / "env-op-resolve"
    resolver.parent.mkdir(exist_ok=True)
    # env-op-resolve exec --env-file FILE -- CMD...: assert the shape, inject a value, exec.
    resolver.write_text('#!/bin/sh\n[ "$1" = exec ] && [ "$2" = --env-file ] && [ -r "$3" ] && [ "$4" = -- ] || { echo "bad resolver call: $*" >&2; exit 97; }\n'
                        'shift 4\nPGPASSWORD=injected-by-resolver exec "$@"\n')
    resolver.chmod(0o755)
    return resolver


def run_mac_wrapper(tmp_path, **extra_env):
    root = fake_checkout(tmp_path, MAC / "run-executor.sh")
    fake_resolver(tmp_path)
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path), **extra_env}
    return subprocess.run([SH, str(root / "deploy" / "mac" / "run-executor.sh")], env=env, capture_output=True, text=True, timeout=30, check=False)


def test_mac_wrapper_forces_an_empty_cgroup_root_and_injects_the_secret(tmp_path):
    shutil.copy(MAC / "executor.env.op", tmp_path / "executor.env.op")
    result = run_mac_wrapper(tmp_path, CANDYBAR_ENV_FILE=str(tmp_path / "executor.env.op"), CANDYBAR_CGROUP_ROOT="/sys/fs/cgroup/candybar")
    assert result.returncode == 0, result.stderr
    assert "cgroup-root=[]" in result.stdout  # even an inherited non-empty value is overridden: there is no cgroupfs
    assert "pgpassword=injected-by-resolver" in result.stdout
    assert "args=daemon --roles executor" in result.stdout  # exec role only, never heartbeat/dispatcher/api
    # No DYLD_* plumbing: SIP strips it across /usr/bin/env (the resolver's shebang) and /bin/sh, so it
    # could never reach candybar. libpq comes bundled in the darwin psycopg-binary wheel instead (below).
    assert "dyld=none" in result.stdout
    # The resolver's `#!/usr/bin/env python3` must find the venv python, never a mise shim (untrusted repo config).
    assert f"path0={tmp_path / 'checkout' / '.venv' / 'bin'}" in result.stdout


def test_mac_wrapper_sets_the_variable_when_the_environment_has_none(tmp_path):
    shutil.copy(MAC / "executor.env.op", tmp_path / "executor.env.op")
    result = run_mac_wrapper(tmp_path, CANDYBAR_ENV_FILE=str(tmp_path / "executor.env.op"))
    assert "cgroup-root=[]" in result.stdout and "UNSET" not in result.stdout


def test_mac_wrapper_does_not_depend_on_dyld_variables_that_sip_strips():
    text = (MAC / "run-executor.sh").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "DYLD_" not in code and "libpq" not in code


def locked_package(name):
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    return next((pkg for pkg in lock["package"] if pkg["name"] == name), None)


def test_darwin_installs_the_bundled_libpq_wheel_and_linux_does_not():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    assert "psycopg==3.3.6" in project  # every platform
    assert "psycopg-binary==3.3.6; sys_platform == 'darwin'" in project  # pinned to the same version, darwin only
    candybar = locked_package("candybar")
    edge = next(dep for dep in candybar["dependencies"] if dep["name"] == "psycopg-binary")
    assert edge["marker"] == "sys_platform == 'darwin'"  # the lock, not just pyproject, keeps Linux on the system libpq
    binary = locked_package("psycopg-binary")
    assert binary["version"] == locked_package("psycopg")["version"]
    wheels = [wheel["url"].rsplit("/", 1)[1] for wheel in binary["wheels"]]
    assert wheels and all("macosx" in name for name in wheels)  # nothing here can install on Linux
    for tag in ("cp313", "cp314"):  # requires-python >= 3.13: the Mac's uv and mise pythons are 3.14
        assert any(f"-{tag}-{tag}-macosx_11_0_arm64" in name for name in wheels), tag


def test_mac_wrapper_never_mentions_proc_or_cgroupfs():
    text = (MAC / "run-executor.sh").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "/proc" not in code and "/sys/fs/cgroup" not in code


def test_mac_wrapper_fails_with_config_error_when_the_checkout_or_resolver_is_missing(tmp_path):
    root = fake_checkout(tmp_path, MAC / "run-executor.sh")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    script = str(root / "deploy" / "mac" / "run-executor.sh")
    no_resolver = subprocess.run([SH, script], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert no_resolver.returncode == 78 and "env-op-resolve not found" in no_resolver.stderr
    (root / ".venv" / "bin" / "candybar").unlink()
    no_venv = subprocess.run([SH, script], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert no_venv.returncode == 78 and "uv sync --frozen" in no_venv.stderr


def test_wrappers_are_executable_and_posix_sh():
    for name in ("run-executor.sh", "install-executor.sh"):
        path = MAC / name
        assert path.stat().st_mode & stat.S_IXUSR
        assert path.read_text().startswith("#!/bin/sh\n")  # launchd's PATH gives bash 3.2; no bashisms


def test_secret_ref_files_hold_references_only():
    for name in ("executor.env.op", "compose.env.op"):
        lines = [ln for ln in (MAC / name).read_text().splitlines() if ln and not ln.startswith("#")]
        assert lines and all(re.fullmatch(r"[A-Z_]+=op://DeLoSecrets/[\w-]+/[\w-]+", ln) for ln in lines)
    refs = {ln.split("=", 1)[1] for name in ("executor.env.op", "compose.env.op")
            for ln in (MAC / name).read_text().splitlines() if ln and not ln.startswith("#")}
    assert len(refs) == 1  # container and host executor share ONE registry password


# ---- CANDYBAR_CGROUP_ROOT="" never reads /proc/self/cgroup ---------------------------------

def test_linux_wrapper_skips_the_proc_read_when_the_root_is_set_empty(tmp_path):
    marker = tmp_path / "sed-called"
    shim = tmp_path / "bin" / "sed"
    shim.parent.mkdir()
    shim.write_text(f"#!/bin/sh\necho called > {marker}\nexit 99\n")
    shim.chmod(0o755)
    root = fake_checkout(tmp_path, REPO / "deploy" / "host" / "run-executor.sh")
    # run-executor.sh execs "$root/.venv/bin/candybar"; the fake prints and exits.
    script = str(root / "deploy" / "host" / "run-executor.sh")
    base = {"PATH": f"{shim.parent}:/usr/bin:/bin", "HOME": str(tmp_path)}
    empty = subprocess.run([SH, script], env={**base, "CANDYBAR_CGROUP_ROOT": ""}, capture_output=True, text=True, timeout=30, check=False)
    assert empty.returncode == 0, empty.stderr
    assert not marker.exists() and "cgroup-root=[]" in empty.stdout
    # Control: unset, the wrapper does consult /proc/self/cgroup (through sed), so the assertion above has teeth.
    subprocess.run([SH, script], env=base, capture_output=True, text=True, timeout=30, check=False)
    assert marker.exists()


def test_only_the_systemd_runner_reads_proc_self_cgroup():
    readers = {p.relative_to(REPO).as_posix() for p in (REPO / "src").rglob("*.py") if "/proc/self/cgroup" in p.read_text()}
    assert readers == {"src/candybar/executor/systemd_runner.py"}  # a runner kind the Mac profile never uses


def _guarded_exec_run(monkeypatch, root_value: str, forbidden: tuple[str, ...]):
    touched: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def guard(path):
        if any(str(path).startswith(prefix) for prefix in forbidden):
            touched.append(str(path))
            raise AssertionError(f"read {path}")

    def open_guard(file, *args, **kwargs):
        guard(file)
        return real_open(file, *args, **kwargs)

    def os_open_guard(path, *args, **kwargs):
        guard(path)
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", root_value)  # read through the real cgroup_root()
    monkeypatch.setattr(builtins, "open", open_guard)
    monkeypatch.setattr(os, "open", os_open_guard)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (guard(self), real_open(self).read())[1])
    ids = {"invocation_id": uuid4(), "candybar_id": uuid4(), "trigger": "scheduled", "tick_id": "tick:1"}
    try:
        outcome = asyncio.run(run_invocation(argv=[sys.executable, "-c", "print('mac job')"], **ids))  # root=None: env decides
    except AssertionError:
        outcome = None
    return outcome, touched


def test_exec_invocation_with_an_empty_root_never_touches_proc_or_cgroupfs(monkeypatch):
    outcome, touched = _guarded_exec_run(monkeypatch, "", ("/proc/self/cgroup", "/sys/fs/cgroup"))  # the Mac plist value
    assert touched == []
    assert outcome is not None and outcome.exit_code == 0 and outcome.spawn_error is None
    assert outcome.scoped is False and outcome.memory_peak_bytes == 0 and outcome.cpu_seconds in (0, 0.0, None)


def test_guard_has_teeth_a_non_empty_root_does_touch_the_cgroup_tree(monkeypatch, tmp_path):
    root = tmp_path / "cg"
    _, touched = _guarded_exec_run(monkeypatch, str(root), (str(root),))
    assert touched, "the same guard must fire when a cgroup root is configured"


# ---- AD-22: the Mac ships no watchdog, and says so ----------------------------------------------

def test_no_watchdog_decision_is_recorded_where_an_operator_looks():
    mac = (MAC / "README.md").read_text()
    assert "AD-22: no watchdog on the Mac" in mac and "**none, by decision**" in mac
    watchdog = " ".join((REPO / "deploy" / "watchdog" / "README.md").read_text().split())
    assert "macOS: no watchdog" in watchdog and "recorded decision, not an exception" in watchdog


def test_mac_profile_adds_no_timer_no_launchd_clock_and_no_policy_exception():
    assert not list(MAC.rglob("*.timer"))
    for path in MAC.iterdir():
        if path.suffix in {".plist", ".template"}:
            assert "StartInterval" not in re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.DOTALL)
    policy_text = (REPO / "deploy" / "scheduling-policy.json").read_text()
    assert "launchd:" not in policy_text and "mac" not in policy_text.lower()  # decision, not an exception record


def test_doctor_repo_lint_is_clean_for_the_mac_profile():
    from candybar import scheduling_policy as policy

    exceptions = policy.load_policy(REPO / "deploy" / "scheduling-policy.json")
    report = policy.repo_lint(REPO, exceptions)
    assert not [v for v in report["violations"] if "mac" in v["id"]]
