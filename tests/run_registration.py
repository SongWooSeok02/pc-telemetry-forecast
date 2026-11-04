"""Fail-closed registration evidence runner (requires TEST_DATABASE_URL)."""
import sys
from run_backend import main

if __name__ == '__main__':
    sys.exit(main('test_registration', 'REGISTRATION_REPORT', 'tests/results/s2-03-registration.json'))
