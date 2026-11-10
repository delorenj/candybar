import fcntl
import os

from candybar import scheduling_policy as policy


def test_lock_contention_remains_unknown_with_exact_diagnosis(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "CANONICAL_LEDGER", tmp_path / "ledger.yaml")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status, reason = policy.rollback_evidence({}, {}, None)
        assert status == "unknown" and "directory lock held" in reason
    finally:
        os.close(fd)
    status, reason = policy.rollback_evidence({}, {}, None)
    assert status == "unknown" and "registry unavailable" in reason
