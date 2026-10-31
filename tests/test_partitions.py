"""Partition worker logs must diagnose failure without disclosing connection data."""
import os
import unittest
from unittest.mock import patch

from backend import partitions


class PartitionWorker(unittest.TestCase):
    def test_connection_failure_is_redacted_and_nonzero(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'synthetic-connection'}), \
                patch.object(partitions.psycopg, 'connect',
                             side_effect=RuntimeError('synthetic-private-host-and-password')), \
                self.assertLogs(level='ERROR') as logs, \
                self.assertRaises(SystemExit) as result:
            partitions.main()
        self.assertEqual(result.exception.code, 1)
        self.assertIn('partition_maintenance_failed type=RuntimeError', logs.output[0])
        self.assertNotIn('synthetic-private', ''.join(logs.output))

    def test_success_commits_and_reports(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'synthetic-connection'}), \
                patch.object(partitions.psycopg, 'connect') as connect, \
                self.assertLogs(level='INFO') as logs:
            partitions.main()
        statements = [call.args[0] for call in
                      connect.return_value.__enter__.return_value.execute.call_args_list]
        self.assertEqual(statements[-1], 'SELECT ensure_measurement_partitions()')
        connect.return_value.__exit__.assert_called_once_with(None, None, None)
        self.assertIn('partition_maintenance_success', ''.join(logs.output))
