import unittest
from datetime import datetime, timedelta, timezone

from ml.evaluate_baselines import Observation, build_examples, evaluate


class BaselinePipelineTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def timeline(self, count):
        return {
            self.start + timedelta(seconds=second): Observation(float(second), "idle")
            for second in range(count)
        }

    def test_examples_keep_input_and_prediction_inside_split(self):
        timeline = self.timeline(900)
        split_start = self.start + timedelta(seconds=300)
        split_end = self.start + timedelta(seconds=800)
        examples, possible = build_examples(timeline, split_start, split_end)
        self.assertEqual(len(examples), possible)
        self.assertTrue(examples)
        self.assertGreaterEqual(examples[0].anchor - timedelta(seconds=299), split_start)
        self.assertLess(examples[-1].target_time, split_end)
        self.assertAlmostEqual(examples[0].moving_average, sum(range(300, 600)) / 300)

    def test_missing_or_bad_quality_value_excludes_windows_without_imputation(self):
        timeline = self.timeline(500)
        bad_time = self.start + timedelta(seconds=200)
        timeline[bad_time] = Observation(None, "idle")
        examples, possible = build_examples(timeline, self.start, self.start + timedelta(seconds=500))
        self.assertEqual(possible, 141)
        self.assertEqual(examples, [])

    def test_validation_selects_baseline_and_report_records_test_metrics(self):
        timeline = {}
        # A deterministic trend provides a stable validation-only selection case.
        for second in range(2_000):
            value = float(second // 10)
            timeline[self.start + timedelta(seconds=second)] = Observation(value, "high_load")
        groups = {("pc-a", "cpu/temp"): timeline}
        report = evaluate(groups, "abc123", 0, train_days=0.005, validation_days=0.005, test_days=0.005)
        target = report["targets"]["pc-a/cpu/temp"]
        self.assertEqual(target["status"], "evaluated")
        scores = target["validation_selection_mae"]
        self.assertEqual(target["selected_baseline"], min(scores, key=scores.get))
        self.assertEqual(target["splits"]["validation"]["selected_baseline"], target["selected_baseline"])
        self.assertIn("missing_rate", target["splits"]["test"]["models"]["moving_average"])
        self.assertFalse(report["config"]["test_used_for_selection_or_tuning"])


if __name__ == "__main__":
    unittest.main()
