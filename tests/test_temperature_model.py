import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ml.temperature_model import (
    VERSION, Example, Sample, adoption, build_examples, comparison_markdown, deployment_coverage,
    features, fit_ridge, infer, load_samples, predict_model, select_signals,
    train, valid_transitions, window,
)


class TemperatureModelTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def timeline(self, count=1000):
        return {self.start + timedelta(seconds=i): Sample(
            (40 + i / 100, 100., 50., 3000., 1200.),
            ('idle', 'high_load', 'transition')[i // 100 % 3], 'boot', 'config') for i in range(count)}

    def metric(self, overall, high=1., transition=1.):
        return {'mae': overall, 'by_workload': {'high_load': {'mae': high}, 'transition': {'mae': transition}}}

    def test_features_and_targets_stay_inside_split(self):
        samples = self.timeline()
        start = self.start + timedelta(seconds=100, microseconds=1)
        end = self.start + timedelta(seconds=900)
        examples, diag = build_examples(samples, start, end, ['temperature', 'power'])
        self.assertTrue(examples)
        self.assertEqual(len(examples), diag['possible_count'])
        self.assertGreaterEqual(examples[0].anchor - timedelta(seconds=299), start)
        self.assertLess(examples[-1].anchor + timedelta(seconds=60), end)
        self.assertAlmostEqual(examples[0].features[0], samples[examples[0].anchor].values[0])

    def test_missing_and_restart_windows_are_excluded_without_imputation(self):
        samples = self.timeline(500)
        anchor = self.start + timedelta(seconds=400)
        del samples[self.start + timedelta(seconds=200)]
        self.assertIsNone(window(samples, anchor, ['temperature']))
        samples = self.timeline(500)
        row = samples[anchor]
        samples[anchor] = Sample(row.values, row.workload, 'restarted', 'config')
        self.assertIsNone(window(samples, anchor, ['temperature']))

    def test_future_data_does_not_change_feature_selection_or_scaler(self):
        samples = self.timeline(1000)
        boundary = self.start + timedelta(seconds=600)
        chosen = select_signals(samples, self.start, boundary)
        examples, _ = build_examples(samples, self.start, boundary, chosen)
        fitted = fit_ridge(examples)
        for stamp in list(samples):
            if stamp >= boundary:
                samples[stamp] = Sample((1e9, None, None, None, None), 'transition', 'boot', 'config')
        self.assertEqual(select_signals(samples, self.start, boundary), chosen)
        later, _ = build_examples(samples, self.start, boundary, chosen)
        self.assertEqual(fit_ridge(later), fitted)

    def test_unsupported_auxiliary_signal_is_not_a_zero_feature(self):
        samples = {stamp: Sample((row.values[0], None, None, None, None), row.workload, row.boot, row.config)
                   for stamp, row in self.timeline().items()}
        chosen = select_signals(samples, self.start, self.start + timedelta(seconds=1000))
        self.assertEqual(chosen, ['temperature'])

    def test_fit_and_json_inference_are_reproducible(self):
        samples = self.timeline()
        examples, _ = build_examples(samples, self.start, self.start + timedelta(seconds=1000), ['temperature'])
        model = fit_ridge(examples, alpha=0.1)
        serialized = json.loads(json.dumps(model, allow_nan=False))
        self.assertEqual(predict_model(model, examples[-1].features), predict_model(serialized, examples[-1].features))
        artifact = dict(schema_version=1, pipeline_version=VERSION, selected_model='ridge', baseline='persistence',
                        signals=['temperature'], candidate_model=serialized)
        prediction = infer(artifact, samples)
        rows = window(samples, max(samples), ['temperature'])
        self.assertEqual(prediction['temperature_celsius'], predict_model(model, features(rows, ['temperature'])))
        self.assertFalse(prediction['fallback'])
        example = examples[-1]
        replay = infer(artifact, {stamp: row for stamp, row in samples.items() if stamp <= example.anchor})
        self.assertEqual(replay['temperature_celsius'], predict_model(model, example.features))

    def test_adoption_requires_both_improvement_and_subgroup_limits(self):
        baseline = self.metric(1)
        self.assertTrue(adoption(baseline, self.metric(.9, 1.05, 1.05), [])['adopted'])
        for candidate in (self.metric(.9001), self.metric(.8, 1.051), self.metric(.8, 1, 1.051)):
            self.assertFalse(adoption(baseline, candidate, [])['adopted'])
        self.assertFalse(adoption(baseline, self.metric(.8), ['insufficient_test_span'])['adopted'])

    def test_absolute_baseline_thresholds_and_zero_subgroup(self):
        for unit, threshold in (('Cel', .1), ('W', 1.)):
            decision = adoption(self.metric(threshold - .001), self.metric(0), [], unit)
            self.assertIn('baseline_below_absolute_threshold', decision['reasons'])
        self.assertTrue(adoption(self.metric(.1, 0, 0), self.metric(.08, 0, 0), [])['adopted'])
        self.assertFalse(adoption(self.metric(1, 0, 0), self.metric(.8, .001, 0), [])['adopted'])

    def test_missing_subgroup_score_prevents_adoption(self):
        self.assertFalse(adoption(self.metric(1), self.metric(.5, None, 1), [])['adopted'])

    def test_independent_transition_count_deduplicates_and_filters_quality(self):
        samples = self.timeline(2000)
        event = dict(transition_id='t1', target='cpu', validation='valid', transition_status='observed',
                     transition_at_utc=(self.start + timedelta(seconds=600)).isoformat(),
                     from_stage='idle', to_stage='cpu_load')
        session = dict(session_id='s1', machine_id='pc', boot_session_id='boot', sensor_config_id='config', transitions=[event, event])
        args = ('pc', 'cpu', samples, self.start, self.start + timedelta(seconds=2000), ['temperature'])
        self.assertEqual(valid_transitions([session, session], *args), 1)
        excluded = {**session, 'transitions': [{**event, 'validation': 'excluded'}]}
        self.assertEqual(valid_transitions([excluded], *args), 0)
        del samples[self.start + timedelta(seconds=500)]
        self.assertEqual(valid_transitions([session], *args), 0)

    def test_insufficient_data_still_emits_baseline_and_report(self):
        samples = self.timeline(500)
        artifact, report = train(samples, [], 'pc', 'cpu', 'input-hash', None)
        self.assertEqual(artifact['selected_model'], 'persistence')
        self.assertFalse(report['decision']['adopted'])
        self.assertIn('insufficient_train_span', report['decision']['reasons'])
        self.assertIn('insufficient_valid_independent_transitions', report['decision']['reasons'])
        self.assertEqual(infer(artifact, samples)['served_model'], 'persistence')
        self.assertIn('결측률', comparison_markdown(report))
        json.dumps(report, allow_nan=False)

    def test_missing_regression_inputs_degrade_to_baseline(self):
        samples = self.timeline(500)
        model = fit_ridge([Example(self.start, (i,), i + 60, i, i, 'idle') for i in range(10)])
        artifact = dict(schema_version=1, pipeline_version=VERSION, selected_model='ridge', baseline='moving_average',
                        signals=['temperature', 'fan'], candidate_model=model)
        stamp = max(samples)
        row = samples[stamp]
        samples[stamp] = Sample((*row.values[:-1], None), row.workload, row.boot, row.config)
        self.assertEqual(infer(artifact, samples)['served_model'], 'moving_average')
        del samples[self.start + timedelta(seconds=400)]
        self.assertEqual(infer(artifact, samples)['served_model'], 'persistence')
        samples[stamp] = Sample((None,) * 5, 'idle', 'boot', 'config')
        self.assertEqual(infer(artifact, samples)['status'], 'unavailable')

    def test_deployment_coverage_scores_candidate_with_baseline_fallback(self):
        samples = self.timeline(1000)
        start, end = self.start, self.start + timedelta(seconds=1000)
        signals = ['temperature', 'fan']
        gap = self.start + timedelta(seconds=500)
        row = samples[gap]
        samples[gap] = Sample((*row.values[:-1], None), row.workload, row.boot, row.config)
        candidates, _ = build_examples(samples, start, end, signals)
        baseline_only, _ = build_examples(samples, start, end, ['temperature'])
        model = fit_ridge(candidates, alpha=0.1)
        coverage = deployment_coverage(samples, start, end, candidates, 'moving_average', model)
        candidate_anchors = {example.anchor for example in candidates}
        fallback = [example for example in baseline_only if example.anchor not in candidate_anchors]
        self.assertEqual(coverage['baseline_predictable_count'], len(baseline_only))
        self.assertEqual(coverage['candidate_predictable_count'], len(candidates))
        self.assertTrue(fallback)
        self.assertLess(len(candidates), len(baseline_only))
        self.assertEqual(coverage['fallback_count'], len(fallback))
        self.assertEqual(coverage['baseline_only']['sample_count'], len(baseline_only))
        by_anchor = {example.anchor: example for example in candidates}
        expected = [abs(example.actual - (predict_model(model, by_anchor[example.anchor].features)
                                          if example.anchor in by_anchor else example.moving_average))
                    for example in baseline_only]
        self.assertAlmostEqual(coverage['candidate_with_fallback']['mae'], sum(expected) / len(expected))
        self.assertEqual(coverage['candidate_with_fallback']['sample_count'], len(baseline_only))
        # Each deployment prediction matches what infer() serves for the same history.
        artifact = dict(schema_version=1, pipeline_version=VERSION, selected_model='ridge', baseline='moving_average',
                        signals=signals, candidate_model=model)
        for example in (fallback[0], candidates[-1]):
            served = infer(artifact, {stamp: row for stamp, row in samples.items() if stamp <= example.anchor})
            expected_value = (predict_model(model, by_anchor[example.anchor].features)
                              if example.anchor in by_anchor else example.moving_average)
            self.assertAlmostEqual(served['temperature_celsius'], expected_value)
        self.assertIsNone(deployment_coverage(samples, start, end, candidates, 'persistence', None)['candidate_with_fallback'])

    def test_minimum_durations_are_not_overridable(self):
        with self.assertRaises(ValueError):
            train({}, [], 'pc', 'gpu', 'hash', None, test_days=1)

    def test_empty_target_emits_baseline_without_invented_data_period(self):
        artifact, report = train({}, [], 'pc', 'gpu', 'hash', None)
        self.assertEqual(artifact['selected_model'], 'persistence')
        self.assertEqual(report['boundaries_utc'], {})
        self.assertEqual(report['deployment_coverage'], {})
        self.assertIsNone(report['label_evaluation_period_utc'])
        self.assertIn('no_observations', report['decision']['reasons'])
        self.assertEqual(infer(artifact, {})['status'], 'unavailable')

    def test_full_eight_day_synthetic_training_keeps_low_error_baseline(self):
        # Reuse immutable samples to keep this full-duration integration check small.
        rows = [Sample((42., None, None, None, None), label, 'boot', 'config')
                for label in ('idle', 'high_load', 'transition')]
        samples = {self.start + timedelta(seconds=i): rows[(i // 600) % 3] for i in range(8 * 86400)}
        test_start = self.start + timedelta(days=6)
        labels = []
        for session in range(5):
            events = []
            for offset, edge in enumerate((('idle', 'cpu_load'), ('cpu_load', 'cpu_cooldown'))):
                events.append(dict(transition_id=f'{session}-{offset}', target='cpu', validation='valid',
                                   transition_status='observed', from_stage=edge[0], to_stage=edge[1],
                                   transition_at_utc=(test_start + timedelta(seconds=600 + session * 1200 + offset * 600)).isoformat()))
            labels.append(dict(session_id=f's{session}', machine_id='pc', boot_session_id='boot',
                               sensor_config_id='config', transitions=events))
        artifact, report = train(samples, labels, 'pc', 'cpu', 'synthetic-only', 'labels-hash')
        self.assertIsNotNone(artifact['candidate_model'])
        self.assertEqual(report['valid_test_transitions'], 10)
        self.assertEqual(report['decision']['reasons'], ['baseline_below_absolute_threshold'])
        self.assertEqual(artifact['selected_model'], 'persistence')
        for split in report['diagnostics'].values():
            self.assertEqual(split['missing_rate'], 0)
            self.assertEqual(split['temperature_coverage'], 1)
        test_coverage = report['deployment_coverage']['test']
        self.assertEqual(test_coverage['fallback_count'], 0)
        self.assertEqual(test_coverage['baseline_predictable_count'], test_coverage['candidate_predictable_count'])
        self.assertEqual(test_coverage['served_policy'], 'baseline_only')
        self.assertEqual(test_coverage['baseline_only']['mae'], report['metrics']['test'][artifact['baseline']]['mae'])
        self.assertIsNotNone(test_coverage['candidate_with_fallback'])
        self.assertIn('후보+대체 MAE', comparison_markdown(report))

    def test_csv_quality_and_clock_anomalies_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.csv'
            fields = ['machine_id', 'target', 'observed_at', 'temperature', 'temperature_quality',
                      'time_quality', 'workload', 'boot_session_id', 'sensor_config_id']
            with path.open('w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for i, (value, quality, time_quality) in enumerate((('50', 'ok', 'ok'), ('51', 'ok', 'suspect'), ('NaN', 'ok', 'ok'))):
                    writer.writerow(dict(zip(fields, ('pc', 'cpu', (self.start + timedelta(seconds=i)).isoformat(),
                        value, quality, time_quality, 'idle', 'boot', 'config'))))
            loaded = load_samples(path, 'pc', 'cpu')
            self.assertEqual([row.values[0] for row in loaded.values()], [50, None, None])

    def test_cli_writes_fallback_artifact_report_and_replays_prediction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'synthetic.csv'
            with data.open('w', newline='', encoding='utf-8') as handle:
                writer = csv.writer(handle)
                writer.writerow(['machine_id', 'target', 'observed_at', 'temperature', 'temperature_quality',
                                 'time_quality', 'workload', 'boot_session_id', 'sensor_config_id'])
                for i in range(400):
                    writer.writerow(['pc', 'gpu', (self.start + timedelta(seconds=i)).isoformat(), 42,
                                     'ok', 'ok', 'idle', 'boot', 'config'])
            output = root / 'output'
            subprocess.run([sys.executable, '-m', 'ml.temperature_model', 'train', '--input', str(data),
                            '--machine-id', 'pc', '--target', 'gpu', '--output-dir', str(output)],
                           check=True, capture_output=True, text=True)
            report = json.loads((output / 'evaluation.json').read_text(encoding='utf-8'))
            self.assertFalse(report['decision']['adopted'])
            self.assertTrue((output / 'comparison.md').exists())
            prediction = subprocess.run([sys.executable, '-m', 'ml.temperature_model', 'predict',
                '--input', str(data), '--artifact', str(output / 'model.json')],
                check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(prediction.stdout)['temperature_celsius'], 42)


if __name__ == '__main__':
    unittest.main()
