import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ml.evaluate_baselines import Observation, build_examples, chronological_boundaries, evaluate, read_input


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

    def test_default_boundaries_have_exact_durations_and_include_latest_tick(self):
        latest = self.start + timedelta(days=8) - timedelta(seconds=1)
        boundaries = chronological_boundaries(latest)
        for name, days in (("train", 5), ("validation", 1), ("test", 2)):
            start, end = boundaries[name]
            self.assertEqual(end - start, timedelta(days=days))
        self.assertEqual(boundaries["test"], (self.start + timedelta(days=6), self.start + timedelta(days=8)))
        self.assertEqual(boundaries["validation"][1], boundaries["test"][0])
        self.assertEqual(boundaries["train"][1], boundaries["validation"][0])
        self.assertLess(latest, boundaries["test"][1])

    def test_exclusive_test_end_keeps_exact_candidate_count_and_latest_target(self):
        timeline = self.timeline(2000)
        latest = max(timeline)
        boundaries = chronological_boundaries(latest, test_days=600 / 86400)
        start, end = boundaries["test"]
        self.assertEqual(end - start, timedelta(seconds=600))
        examples, possible = build_examples(timeline, start, end)
        self.assertEqual(possible, 600 - 299 - 60)
        self.assertEqual(len(examples), possible)
        self.assertEqual(examples[0].anchor - timedelta(seconds=299), start)
        self.assertEqual(examples[-1].target_time, latest)

    def test_missing_or_bad_quality_value_excludes_windows_without_imputation(self):
        timeline = self.timeline(500)
        bad_time = self.start + timedelta(seconds=200)
        timeline[bad_time] = Observation(None, "idle")
        examples, possible = build_examples(timeline, self.start, self.start + timedelta(seconds=500))
        self.assertEqual(possible, 141)
        self.assertEqual(examples, [])

    def test_fractional_split_boundary_aligns_anchor_to_second_grid(self):
        timeline = self.timeline(1_000)
        split_start = self.start + timedelta(seconds=300, microseconds=500_000)
        split_end = self.start + timedelta(seconds=900, microseconds=500_000)

        examples, possible = build_examples(timeline, split_start, split_end)

        self.assertTrue(examples)
        self.assertGreater(possible, 0)
        self.assertEqual(examples[0].anchor, self.start + timedelta(seconds=600))
        self.assertTrue(all(example.anchor.microsecond == 0 for example in examples))
        self.assertEqual(examples[-1].target_time, self.start + timedelta(seconds=900))
        self.assertGreaterEqual(examples[0].anchor - timedelta(seconds=299), split_start)
        self.assertLess(examples[-1].target_time, split_end)

    def test_fractional_day_splits_are_supported_and_invalid_durations_rejected(self):
        boundaries = chronological_boundaries(
            self.start + timedelta(seconds=2_000),
            train_days=0.005005,
            validation_days=0.005005,
            test_days=0.005005,
        )
        self.assertTrue(any(start.microsecond or end.microsecond for start, end in boundaries.values()))
        for invalid in (0, -1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                chronological_boundaries(self.start, train_days=invalid)

    def read_rows(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.csv"
            lines = ["machine_id,sensor_id,observed_at,value,quality,workload"]
            for second, (value, quality) in enumerate(rows):
                lines.append(f"pc-a,cpu/temp,2026-01-01T00:00:{second:02d}Z,{value},{quality},idle")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return read_input(path)

    def test_invalid_quality_rows_count_each_unusable_row_once(self):
        cases = {
            "ok_empty_value": [("", "ok")],
            "ok_non_finite_value": [("nan", "ok")],
            "ok_infinite_value": [("inf", "ok")],
            "ok_unparsable_value": [("hot", "ok")],
            "bad_quality_with_value": [("40.0", "stale")],
            "bad_quality_empty_value": [("", "missing")],
        }
        for name, rows in cases.items():
            with self.subTest(name):
                groups, _, invalid_rows = self.read_rows(rows)
                self.assertEqual(invalid_rows, 1)
                observation = groups[("pc-a", "cpu/temp")][self.start]
                self.assertIsNone(observation.value)

    def test_invalid_quality_rows_ignore_valid_rows_and_sum_mixed_rows(self):
        _, _, invalid_rows = self.read_rows([("40.0", "ok"), ("", "ok"), ("nan", "ok"), ("41.0", "stale"), ("42", "OK")])
        self.assertEqual(invalid_rows, 3)

    def test_invalid_split_bounds_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "earlier"):
            build_examples(self.timeline(400), self.start + timedelta(seconds=100), self.start)

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
