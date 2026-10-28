"""Evidence runner: setup failures and failed subtests must also leave a report."""
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))


class Evidence(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def startTest(self, test):
        test.evidence_started = time.monotonic()
        test.evidence_at = datetime.now(timezone.utc).isoformat()
        test.assertion_evidence = []
        for name in ('assertEqual', 'assertTrue'):
            original = getattr(test, name)
            def capture(*args, _original=original, _name=name, **kwargs):
                test.assertion_evidence.append(dict(assertion=_name, actual=repr(args[0]),
                    expected=repr(args[1]) if len(args)>1 else 'True'))
                return _original(*args, **kwargs)
            setattr(test, name, capture)
        super().startTest(test)

    def record(self, test, status, actual):
        # setUpClass/tearDownClass failures use unittest._ErrorHolder; no
        # startTest callback or _testMethodName is available for those errors.
        now = time.monotonic()
        method = getattr(test, '_testMethodName', None)
        reproduction = 'python -m unittest discover -s tests -p test_backend.py'
        if method:
            reproduction += ' -k ' + method
        self.records.append(dict(case=test.id(), reproduction=reproduction,
            expected=test.shortDescription() or 'Test suite setup/teardown succeeds',
            actual=actual, assertions=getattr(test, 'assertion_evidence', []), status=status,
            executed_at_utc=getattr(test, 'evidence_at', datetime.now(timezone.utc).isoformat()),
            duration_seconds=round(now-getattr(test, 'evidence_started', now), 4)))

    def addSuccess(self, test):
        self.record(test, 'pass', 'All stated assertions matched')
        super().addSuccess(test)

    def addSkip(self, test, reason):
        self.record(test, 'not_executed', reason)
        super().addSkip(test, reason)

    def addFailure(self, test, err):
        self.record(test, 'fail', self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addError(self, test, err):
        self.record(test, 'error', self._exc_info_to_string(err, test))
        super().addError(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.record(subtest, 'fail' if issubclass(err[0], test.failureException) else 'error',
                        self._exc_info_to_string(err, test))
        super().addSubTest(test, subtest, err)


def main():
    import psycopg
    import test_backend
    result = unittest.TextTestRunner(verbosity=2, resultclass=Evidence).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_backend))
    files = sorted(p for folder in ('backend', 'database', 'tests/fixtures')
        for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    files += [ROOT/p for p in ('tests/test_backend.py', 'tests/run_backend.py',
        'tests/test_backend_runner.py', '.github/workflows/backend.yml')]
    version, version_error = None, None
    if os.environ.get('TEST_DATABASE_URL'):
        try:
            with psycopg.connect(os.environ['TEST_DATABASE_URL'], connect_timeout=5) as db:
                version = db.execute('SELECT version()').fetchone()[0]
        except psycopg.Error as exc:
            version_error = type(exc).__name__
    report = dict(executed_at_utc=datetime.now(timezone.utc).isoformat(),
        base_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        pc=platform.node(), os=platform.platform(), python=sys.version,
        packages={p:importlib.metadata.version(p) for p in ('fastapi', 'psycopg', 'starlette', 'httpx')},
        postgres_version=version, postgres_version_error=version_error,
        source_sha256={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted(set(files))},
        postgres_executed=bool(version), success=result.wasSuccessful() and not result.skipped and bool(version),
        cases=result.records)
    path = ROOT/os.environ.get('BACKEND_REPORT', 'tests/results/s1-03-backend.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(f'Evidence: {path}; PostgreSQL executed: {bool(version)}')
    return 0 if report['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
