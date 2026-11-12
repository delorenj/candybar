"""Offline fixtures only; live acceptance uses persisted checkpoint evidence separately."""

import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

SOURCE = Path(__file__).parents[1] / "deploy/migration/token_ops_vault.py"
spec = importlib.util.spec_from_file_location("token_ops_vault", SOURCE)
assert spec is not None and spec.loader is not None
vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault)


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.reports = self.base / "reports"
        self.destination = self.base / "vault"
        self.destination.mkdir()
        self.state = self.base / "state"

    def fixture(self, date="2026-11-10", quarter=4):
        root = self.reports / date / "checkpoints" / f"q{quarter}"
        root.mkdir(parents=True, exist_ok=True)
        start = dt.datetime.combine(
            dt.date.fromisoformat(date),
            dt.time((quarter - 1) * 6),
            ZoneInfo("America/New_York"),
        )
        end = start + dt.timedelta(hours=6)
        bounds = {
            "start_local": start.isoformat(),
            "end_local": end.isoformat(),
            "start_epoch": int(start.timestamp()),
            "end_epoch": int(end.timestamp()),
            "elapsed_hours": (end.timestamp() - start.timestamp()) / 3600,
        }
        checkpoint = {"report_date": date, "quarter": quarter, "window": bounds}
        (root / "checkpoint.json").write_text(json.dumps(checkpoint))
        (root / "report.md").write_text(f"Fixture report {date} Q{quarter}\n")
        manifest = {
            "report_date": date,
            "quarter": quarter,
            "report_complete": True,
            "generated_at": end.isoformat(),
            "degraded": True,
            "windows": {"window": bounds},
            "checksums_sha256": {
                n: vault.sha(root / n) for n in ("checkpoint.json", "report.md")
            },
        }
        (root / "manifest.json").write_text(json.dumps(manifest))
        return root

    def publish(self, date="2026-11-10"):
        return vault.publish(date, self.reports, self.destination, self.state)

    def test_idempotent_and_preserves_source_and_journal(self):
        root = self.fixture()
        originals = {p: p.read_bytes() for p in root.iterdir()}
        day = self.destination / "2026-11-10"
        day.mkdir()
        journal = day / "dev-journal.md"
        journal.write_text("Leave this alone\n")
        first = self.publish()
        dest = Path(first["destination"])
        receipt = self.state / "2026-11-10.json"
        previous = (
            dest.read_bytes(),
            dest.stat().st_mtime_ns,
            receipt.read_bytes(),
            receipt.stat().st_mtime_ns,
        )
        second = self.publish()
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(
            previous,
            (
                dest.read_bytes(),
                dest.stat().st_mtime_ns,
                receipt.read_bytes(),
                receipt.stat().st_mtime_ns,
            ),
        )
        self.assertEqual(journal.read_text(), "Leave this alone\n")
        self.assertEqual(originals, {p: p.read_bytes() for p in root.iterdir()})
        self.assertEqual(second["quarters"], [4])
        self.assertEqual(dest.read_text().count("## Q4"), 1)
        self.assertIn("Checkpoint report unavailable", dest.read_text())

    def test_quarters_integrate_and_preserve_old_generation(self):
        self.fixture(quarter=1)
        first = self.publish()
        self.fixture(quarter=2)
        second = self.publish()
        self.assertEqual(second["quarters"], [1, 2])
        self.assertTrue(
            (self.state / "history/2026-11-10" / f"{first['sha256']}.md").exists()
        )
        self.assertEqual(Path(second["destination"]).read_text().count("## Q1"), 1)

    def test_midnight_uses_covered_day(self):
        self.fixture()
        result = self.publish()
        self.assertIn("/2026-11-10/", result["destination"])
        self.assertFalse((self.destination / "2026-11-11").exists())

    def test_dst_calendar_quarters(self):
        for date, elapsed in [("2026-03-08", 5), ("2026-11-01", 7)]:
            root = self.fixture(date, 1)
            meta, _ = vault.checkpoint(root, date, 1)
            self.assertEqual(meta["window"]["elapsed_hours"], elapsed)

    def test_hash_corruption_refused_before_vault_write(self):
        root = self.fixture()
        (root / "report.md").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "integrity failure"):
            self.publish()
        self.assertFalse((self.destination / "2026-11-10").exists())

    def test_unfinished_report_refused(self):
        root = self.fixture()
        path = root / "manifest.json"
        data = json.loads(path.read_text())
        data["report_complete"] = False
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "unfinished checkpoint"):
            self.publish()

    def test_wrong_bounds_refused(self):
        root = self.fixture()
        path = root / "checkpoint.json"
        data = json.loads(path.read_text())
        data["window"]["end_epoch"] -= 60
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "calendar checkpoint bounds"):
            self.publish()

    def test_hash_path_escape_refused(self):
        root = self.fixture()
        outside = root.parent / "outside"
        outside.write_text("outside")
        path = root / "manifest.json"
        data = json.loads(path.read_text())
        data["checksums_sha256"]["../outside"] = vault.sha(outside)
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "integrity failure"):
            self.publish()

    def test_action_without_report_does_not_invent_report(self):
        root = self.reports / "2026-11-10/checkpoints/q2/actions/diagnosis"
        root.mkdir(parents=True)
        (root / "changes.json").write_text(
            json.dumps({"status": "blocked", "summary": "Owner dependency"})
        )
        result = self.publish()
        self.assertEqual(result["quarters"], [])
        self.assertEqual(len(result["actions"]), 1)
        self.assertIn("Owner dependency", Path(result["destination"]).read_text())

    def test_unfinished_action_refused(self):
        root = self.fixture()
        (root / "changes.json").write_text(json.dumps({"status": "running"}))
        with self.assertRaisesRegex(ValueError, "unfinished action"):
            self.publish()

    def test_unowned_note_not_overwritten(self):
        self.fixture()
        note = self.destination / "2026-11-10/token-ops.md"
        note.parent.mkdir()
        note.write_text("Human-owned note")
        with self.assertRaisesRegex(ValueError, "unowned"):
            self.publish()
        self.assertEqual(note.read_text(), "Human-owned note")

    def test_missing_day_does_not_publish(self):
        with self.assertRaisesRegex(ValueError, "no finalized"):
            self.publish()

    def test_config_resolves_actual_layout(self):
        config = self.base / "report.json"
        config.write_text(
            json.dumps(
                {
                    "distribution": {
                        "vault": {
                            "enabled": True,
                            "layout": "by-date",
                            "path": str(self.destination),
                        }
                    }
                }
            )
        )
        self.assertEqual(vault.vault_root(config), self.destination)
        config.write_text(
            json.dumps(
                {
                    "distribution": {
                        "vault": {
                            "enabled": False,
                            "layout": "by-date",
                            "path": str(self.destination),
                        }
                    }
                }
            )
        )
        with self.assertRaises(ValueError):
            vault.vault_root(config)

    def test_wrapper_external_local_guard(self):
        profile = self.base / "profile"
        (profile / "cron").mkdir(parents=True)
        job = {"id": vault.JOBS["report"], "scheduler": "internal", "deliver": "local"}
        (profile / "cron/jobs.json").write_text(json.dumps({"jobs": [job]}))
        with (
            patch.object(vault, "PROFILE", profile),
            patch.object(vault.subprocess, "run") as runner,
        ):
            with self.assertRaisesRegex(ValueError, "external-pinned"):
                vault.run("report")
            runner.assert_not_called()

    def test_wrapper_success_requires_matching_artifact_and_meters_tokens(self):
        root = self.fixture(quarter=1)
        meta = json.loads((root / "checkpoint.json").read_text())
        meta["checkpoint_dir"] = str(root)
        profile = self.base / "profile"
        (profile / "cron").mkdir(parents=True)
        job = {"id": vault.JOBS["report"], "scheduler": "external", "deliver": "local"}
        (profile / "cron/jobs.json").write_text(json.dumps({"jobs": [job]}))

        def execute(argv, **kwargs):
            self.assertEqual(
                argv[1:6], ["-p", "peter-griffin", "cron", "run", job["id"]]
            )
            self.assertNotIn("CANDYBAR_RESULT_FILE", kwargs["env"])
            kwargs["stdout"].write(
                b'cron-run-result: {"status":"completed","input_tokens":100,"output_tokens":23}\n'
            )
            return vault.subprocess.CompletedProcess(argv, 0)

        with (
            patch.object(vault, "PROFILE", profile),
            patch.object(vault, "plan_now", return_value=meta),
            patch.object(vault.subprocess, "run", side_effect=execute),
            patch.object(
                vault, "publish", return_value={"destination": "fixture-vault"}
            ) as publisher,
            patch.object(vault, "write_result") as result,
        ):
            self.assertEqual(vault.run("report"), 0)
            publisher.assert_called_once_with("2026-11-10")
            self.assertEqual(result.call_args.args[0], "success")
            self.assertEqual(result.call_args.args[2], 123)
            (root / "report.md").write_text("corrupt")
            publisher.reset_mock()
            self.assertEqual(vault.run("report"), 1)
            publisher.assert_not_called()
            self.assertEqual(result.call_args.args[0], "fail")
            self.assertEqual(result.call_args.args[2], 123)

    def test_failure_alert_requires_confirmed_delivery(self):
        with (
            patch.object(vault, "PROFILE", self.base),
            patch.dict("os.environ", {"CANDYBAR_INVOCATION_ID": "fixture"}),
            patch.object(vault.subprocess, "run") as sender,
        ):
            sender.return_value = vault.subprocess.CompletedProcess(
                [], 0, '{"success":true}', ""
            )
            self.assertEqual(vault.notify_failure(), 0)
            self.assertIn("telegram:7564050286", sender.call_args.args[0])
            sender.return_value = vault.subprocess.CompletedProcess(
                [], 0, '{"success":true,"skipped":true}', ""
            )
            self.assertEqual(vault.notify_failure(), 1)
            receipt = self.base / "reports/token-ops-delivery/failures/fixture.json"
            self.assertFalse(json.loads(receipt.read_text())["delivery_confirmed"])

    def test_action_wrong_date_refused(self):
        root = self.fixture()
        (root / "changes.json").write_text(
            json.dumps({"status": "blocked", "report_date": "2026-11-09"})
        )
        with self.assertRaisesRegex(ValueError, "action identity mismatch"):
            self.publish()

    def test_noncanonical_date_refused_before_state_write(self):
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.publish("20261110")
        self.assertFalse(self.state.exists())

    def test_token_accounting_written_in_metrics(self):
        result_path = self.base / "result.json"
        with patch.dict("os.environ", {"CANDYBAR_RESULT_FILE": str(result_path)}):
            vault.write_result("fail", "publication failed", 123)
        self.assertEqual(
            json.loads(result_path.read_text()),
            {
                "status": "fail",
                "message": "publication failed",
                "metrics": {"token_spend": 123},
            },
        )


if __name__ == "__main__":
    unittest.main()
