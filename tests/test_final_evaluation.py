import json
import os
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ml import final_evaluation as final
from ml.temperature_model import Sample, VERSION


class FinalEvaluationTests(unittest.TestCase):
    def fixture(self, frozen=final.DATA_START):
        artifact = {
            "schema_version": 1, "pipeline_version": VERSION,
            "machine_id": "synthetic-pc", "target": "cpu", "unit": "Cel",
            "input_seconds": 300, "horizon_seconds": 60, "signals": ["temperature"],
            "baseline": "persistence", "selected_model": "persistence",
            "candidate_model": {"coefficients": [0] * 5, "means": [0] * 5,
                                "scales": [1] * 5, "intercept": 50},
            "input_sha256": "old-input", "labels_sha256": "old-labels",
        }
        reviewed = final.DATA_START - timedelta(days=1)
        development = {
            **artifact, "generated_at": reviewed.isoformat(),
            "boundaries_utc": {name: {"end_exclusive": (reviewed - timedelta(days=offset)).isoformat()}
                               for name, offset in (("train", 3), ("validation", 2), ("test", 0))},
            "metrics": {"validation": {"persistence": {"mae": 1}, "moving_average": {"mae": 2}}},
            "data_period_utc": {"last_observed": (reviewed - final.SECOND).isoformat()},
        }
        data = json.dumps(artifact).encode()
        return data, final.make_freeze(data, json.dumps(development).encode(), reviewed, frozen)

    def test_schedule_reserves_separate_buffers_and_time_to_report(self):
        self.assertEqual(final.CORE_END - final.CORE_START, timedelta(hours=48))
        self.assertEqual(final.CORE_START - final.DATA_START, timedelta(minutes=5))
        self.assertEqual(final.DATA_END - final.CORE_END, timedelta(minutes=1))
        self.assertLess(final.DATA_END, final.REPORT_DEADLINE)

    def test_incomplete_or_reused_inputs_hold_baseline(self):
        model, freeze = self.fixture()
        result = final.evaluate(freeze, model, {}, [], "old-input", None, final.DATA_END, None)
        self.assertEqual(result["release_model"], "persistence")
        self.assertFalse(result["decision"]["adopted"])
        for reason in ("development_inputs_reused", "missing_holdout_labels", "missing_boundary_buffer",
                       "insufficient_valid_independent_transitions", "missing_independent_freeze_evidence"):
            self.assertIn(reason, result["decision"]["reasons"])

    def test_changed_model_or_evaluation_source_invalidates_freeze(self):
        model, freeze = self.fixture()
        with self.assertRaisesRegex(ValueError, "freeze mismatch"):
            final.evaluate(freeze, model + b" ", {}, [], None, None, final.DATA_END, "reviewed")
        with patch.object(final, "source_hashes", return_value={"changed": "hash"}):
            with self.assertRaisesRegex(ValueError, "freeze mismatch"):
                final.evaluate(freeze, model, {}, [], None, None, final.DATA_END, "reviewed")

    def test_late_freeze_or_report_and_premature_evaluation_are_blocked(self):
        model, freeze = self.fixture(final.DATA_START + final.SECOND)
        result = final.evaluate(freeze, model, {}, [], None, None, final.REPORT_DEADLINE + final.SECOND, "reviewed")
        self.assertIn("freeze_schedule_missed", result["decision"]["reasons"])
        self.assertIn("report_deadline_missed", result["decision"]["reasons"])
        model, freeze = self.fixture()
        sample = Sample((50, None, None, None, None), "idle", "boot", "config")
        partial = {final.CORE_START + i * final.SECOND: sample for i in range(600)}
        result = final.evaluate(freeze, model, partial, [], "new", None, final.CORE_START + timedelta(hours=1), "reviewed")
        self.assertIn("holdout_not_finished", result["decision"]["reasons"])
        self.assertIsNone(result["metrics"]["persistence"]["mae"])

    def test_full_synthetic_holdout_adopts_against_frozen_baseline(self):
        model, freeze = self.fixture()
        start, end = final.CORE_START, final.CORE_END
        samples = {}
        values = (40, 60)
        kinds = ("idle", "high_load", "transition")
        rows = {(value, kind): Sample((value, None, None, None, None), kind, "boot", "config")
                for value in values for kind in kinds}
        for second in range(-300, 48 * 3600 + 60):
            minute = second // 60
            samples[start + second * final.SECOND] = rows[values[minute % 2], kinds[minute % 3]]
        labels = []
        for day, count in ((0, 2), (1, 3)):
            for session in range(count):
                began = start + timedelta(days=day, hours=1 + session * 2)
                events = []
                for i, edge in enumerate((("idle", "cpu_load"), ("cpu_load", "cpu_cooldown")), 1):
                    events.append({"transition_id": f"{day}-{session}-{i}", "target": "cpu",
                                   "validation": "valid", "transition_status": "observed",
                                   "from_stage": edge[0], "to_stage": edge[1],
                                   "transition_at_utc": (began + timedelta(minutes=i * 10)).astimezone(timezone.utc).isoformat()})
                labels.append({"machine_id": "synthetic-pc", "session_id": f"{day}-{session}", "status": "completed",
                               "run_started_at_utc": began.isoformat(), "run_ended_at_utc": (began + timedelta(minutes=50)).isoformat(),
                               "boot_session_id": "boot", "sensor_config_id": "config", "transitions": events})
        result = final.evaluate(freeze, model, samples, labels, "new", "new-labels", final.DATA_END, "reviewed")
        self.assertTrue(result["decision"]["adopted"], result["decision"])
        self.assertEqual(result["release_model"], "ridge")
        self.assertEqual(result["valid_target_transitions"], 10)
        self.assertEqual(result["completed_sessions_by_day"], [2, 3])
        self.assertEqual(result["metrics"]["persistence"]["mae"], 20)
        self.assertEqual(result["metrics"]["ridge"]["mae"], 10)
        self.assertEqual(result["diagnostics"]["missing_rate"], 0)
        self.assertEqual(result["boundary_coverage"], {"input_5min": True, "horizon_60sec": True})
        self.assertEqual(len(samples), int((end - start).total_seconds()) + 360)


class PairedOutputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.report_path = self.root / "final-report.json"
        self.release_path = self.root / "release-model.json"
        decision = {"adopted": False, "reasons": ["missing_holdout_data"], "overall_improvement_fraction": None}
        self.report = {"version": final.VERSION, "release_model": "persistence", "decision": decision,
                       "freeze_manifest_sha256": "manifest"}
        self.release = {"schema_version": 1, "pipeline_version": VERSION, "selected_model": "persistence",
                        "candidate_model": None,
                        "s4_05": {"freeze_manifest_sha256": "manifest", "decision": decision}}

    def tearDown(self):
        self.directory.cleanup()

    def leftovers(self):
        return sorted(path.name for path in self.root.iterdir())

    def test_pair_is_published_together_and_release_links_report(self):
        final.publish_evaluation(self.report_path, self.report, self.release_path, self.release)
        self.assertEqual(self.leftovers(), ["final-report.json", "release-model.json"])
        report_bytes = self.report_path.read_bytes()
        release = json.loads(self.release_path.read_bytes())
        self.assertEqual(release["s4_05"]["evaluation_report_sha256"], final.digest(report_bytes))
        self.assertTrue(final.release_matches_report(self.release_path.read_bytes(), report_bytes))
        self.assertFalse(final.release_matches_report(self.release_path.read_bytes(), report_bytes + b" "))
        with self.assertRaisesRegex(ValueError, "unused output paths"):
            final.publish_evaluation(self.report_path, self.report, self.release_path, self.release)

    def test_release_write_failure_rolls_back_report_and_allows_retry(self):
        real_link = os.link
        calls = []

        def failing_link(source, destination):
            calls.append(destination)
            if len(calls) == 2:
                raise OSError("disk full while publishing release")
            real_link(source, destination)

        with patch.object(final.os, "link", side_effect=failing_link):
            with self.assertRaisesRegex(OSError, "disk full"):
                final.publish_evaluation(self.report_path, self.report, self.release_path, self.release)
        self.assertEqual(calls, [self.report_path, self.release_path])
        # Neither a half pair nor staged temporaries remain to be mistaken for valid output.
        self.assertEqual(self.leftovers(), [])
        final.publish_evaluation(self.report_path, self.report, self.release_path, self.release)
        self.assertTrue(final.release_matches_report(self.release_path.read_bytes(), self.report_path.read_bytes()))

    def test_release_staging_failure_writes_nothing(self):
        real_stage = final._stage

        def failing_stage(path, data):
            if path == self.release_path:
                raise OSError("cannot stage release")
            return real_stage(path, data)

        with patch.object(final, "_stage", side_effect=failing_stage):
            with self.assertRaisesRegex(OSError, "cannot stage release"):
                final.publish_evaluation(self.report_path, self.report, self.release_path, self.release)
        self.assertEqual(self.leftovers(), [])

    def test_unserializable_payload_writes_nothing(self):
        bad_release = {**self.release, "candidate_model": {"intercept": float("nan")}}
        with self.assertRaises(ValueError):
            final.publish_evaluation(self.report_path, self.report, self.release_path, bad_release)
        with self.assertRaises(ValueError):
            final.write_new(self.release_path, bad_release)
        self.assertEqual(self.leftovers(), [])

    def test_report_without_release_is_reported_as_incomplete_run(self):
        self.report_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incomplete previous run"):
            final.publish_evaluation(self.report_path, self.report, self.release_path, self.release)
        self.assertFalse(self.release_path.exists())
        with self.assertRaisesRegex(ValueError, "distinct"):
            final.publish_evaluation(self.report_path, self.report, self.report_path, self.release)

    def test_existing_destination_is_never_overwritten(self):
        self.release_path.write_text("original", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            final.write_new(self.release_path, self.release)
        self.assertEqual(self.release_path.read_text(encoding="utf-8"), "original")
        self.assertEqual(self.leftovers(), ["release-model.json"])


if __name__ == "__main__":
    unittest.main()
