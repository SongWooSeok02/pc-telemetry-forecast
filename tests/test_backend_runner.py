"""Failure paths for evidence reporting, independent of a live database."""
import io
import unittest

from run_backend import Evidence


class RunnerEvidence(unittest.TestCase):
    def run_case(self, case):
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=Evidence).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(case))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.records), 1)
        return result.records[0]

    def test_setup_failure_records_original_error(self):
        class BrokenSetup(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError('synthetic database setup failure')

            def test_never_runs(self):
                self.fail('unreachable')

        record = self.run_case(BrokenSetup)
        self.assertEqual(record['status'], 'error')
        self.assertIn('synthetic database setup failure', record['actual'])
        self.assertIn('setUpClass', record['case'])

    def test_failed_subtest_records_values(self):
        class BrokenVector(unittest.TestCase):
            def test_vectors(self):
                with self.subTest(vector='synthetic'):
                    self.assertEqual('actual', 'expected')

        record = self.run_case(BrokenVector)
        self.assertEqual(record['status'], 'fail')
        self.assertIn('synthetic', record['case'])
        self.assertIn('actual', record['actual'])
        self.assertIn('expected', record['actual'])
