"""Run all contract checks and preserve per-case evidence (stdlib only)."""
import hashlib
import json
import os
import platform
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from verify_contract import Checks, ROOT
from test_integration import Integration, StateTransitions


class Evidence(unittest.TextTestResult):
    def startTest(self, test):
        self.case_started = datetime.now(timezone.utc).isoformat()
        test.assertion_evidence = []
        for name in ('assertEqual', 'assertTrue', 'assertFalse', 'assertIsNone', 'assertIsNotNone', 'assertLessEqual'):
            original = getattr(test, name)
            def capture(*args, _original=original, _name=name, **kwargs):
                test.assertion_evidence.append(dict(assertion=_name,
                    actual=repr(args[0]) if args else '',
                    expected=repr(args[1]) if len(args) > 1 else
                        {'assertTrue':'True', 'assertFalse':'False', 'assertIsNone':'None',
                         'assertIsNotNone':'not None'}.get(_name, 'assertion succeeds')))
                return _original(*args, **kwargs)
            setattr(test, name, capture)
        super().startTest(test)

    def record(self, test, status, detail=''):
        self.cases.append(dict(case=test.id(), reproduction='python -m unittest '+test.id(),
            expected=test.shortDescription() or 'All assertions in this named test pass',
            actual=detail or 'All assertions passed', status=status,
            assertions=getattr(test, 'assertion_evidence', []),
            started_at_utc=self.case_started, finished_at_utc=datetime.now(timezone.utc).isoformat()))

    def addSuccess(self, test):
        self.record(test, 'PASS')
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.record(test, 'FAIL', self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addError(self, test, err):
        self.record(test, 'ERROR', self._exc_info_to_string(err, test))
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.record(test, 'SKIP', reason)
        super().addSkip(test, reason)


if __name__ == '__main__':
    Evidence.cases = []
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c)
                               for c in (Checks, Integration, StateTransitions))
    result = unittest.TextTestRunner(verbosity=2, resultclass=Evidence).run(suite)
    files = [p for folder in ('contracts', 'tests', '.github/workflows')
             for p in (ROOT/folder).rglob('*') if p.is_file()
             and 'results' not in p.parts and '__pycache__' not in p.parts
             and p.name != 'review-record.json']
    report = dict(contract_version='0.1.0-draft.2',
        base_revision=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        ci_run_url=('https://github.com/'+os.environ['GITHUB_REPOSITORY']+'/actions/runs/'+os.environ['GITHUB_RUN_ID'])
            if os.environ.get('GITHUB_RUN_ID') else None,
        ci_head_revision=os.environ.get('GITHUB_SHA'),
        source_hashes={p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)},
        executed_at_utc=datetime.now(timezone.utc).isoformat(),
        environment=dict(os=platform.platform(), pc=platform.node(), python=sys.version,
            node=subprocess.check_output([Integration.node, '--version'], text=True).strip()),
        reproduction='python tests/run_contracts.py (Node.js on PATH or NODE_BINARY set)',
        expected='Every contract case passes without skips',
        actual=dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped)),
        success=result.wasSuccessful() and not result.skipped,
        cases=Evidence.cases,
        scope='Independent JavaScript producer -> real loopback HTTP Python adapter -> SQLite transactions; synthetic clock state inputs',
        limitations=['Not the production .NET/FastAPI/PostgreSQL stack',
            'No physical sleep/resume, deployment clock accuracy or synchronization verification',
            'No human approvals; this report alone cannot mark S1-01 Done'])
    output = Path(os.environ.get('CONTRACT_REPORT', ROOT/'tests/results/s1-01-contracts.json'))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    sys.exit(not report['success'])
