"""Evidence runner: skips remain explicit and cannot count as PostgreSQL success."""
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
import test_backend


class Evidence(unittest.TextTestResult):
    def startTest(self,test):
        self.started = time.monotonic()
        self.at = datetime.now(timezone.utc).isoformat()
        test.assertion_evidence = []
        for name in ('assertEqual','assertTrue'):
            original = getattr(test,name)
            def capture(*args,_original=original,_name=name,**kwargs):
                test.assertion_evidence.append(dict(assertion=_name,actual=repr(args[0]),
                    expected=repr(args[1]) if len(args)>1 else 'True'))
                return _original(*args,**kwargs)
            setattr(test,name,capture)
        super().startTest(test)

    def record(self,test,status,actual):
        records.append(dict(case=test.id(), reproduction=f'python -m unittest discover -s tests -p test_backend.py -k {test._testMethodName}',
            expected=test.shortDescription(), actual=actual, assertions=getattr(test,"assertion_evidence",[]), status=status, executed_at_utc=self.at,
            duration_seconds=round(time.monotonic()-self.started,4)))

    def addSuccess(self,test):
        self.record(test,'pass','All stated assertions matched')
        super().addSuccess(test)

    def addSkip(self,test,reason):
        self.record(test,'not_executed',reason)
        super().addSkip(test,reason)

    def addFailure(self,test,err):
        self.record(test,'fail',self._exc_info_to_string(err,test))
        super().addFailure(test,err)

    def addError(self,test,err):
        self.record(test,'error',self._exc_info_to_string(err,test))
        super().addError(test,err)


records = []
result = unittest.TextTestRunner(verbosity=2,resultclass=Evidence).run(unittest.defaultTestLoader.loadTestsFromModule(test_backend))
files = sorted(p for folder in ('backend','database','tests/fixtures') for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
files += [ROOT/'tests/test_backend.py',ROOT/'tests/run_backend.py',ROOT/'backend/README.md',ROOT/'.github/workflows/backend.yml']
files = sorted(set(files))
version = None
if os.environ.get('TEST_DATABASE_URL'):
    import psycopg
    with psycopg.connect(os.environ['TEST_DATABASE_URL']) as db:
        version = db.execute('SELECT version()').fetchone()[0]
report = dict(executed_at_utc=datetime.now(timezone.utc).isoformat(),
    base_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
    pc=platform.node(),os=platform.platform(),python=sys.version,
    packages={p:importlib.metadata.version(p) for p in ('fastapi','psycopg','starlette','httpx')},
    postgres_version=version, source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    postgres_executed=bool(version), success=result.wasSuccessful() and not result.skipped, cases=records)
path = ROOT/os.environ.get('BACKEND_REPORT','tests/results/s1-03-backend.json')
path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(f'Evidence: {path}; PostgreSQL executed: {bool(version)}')
sys.exit(0 if result.wasSuccessful() and (not os.environ.get('TEST_DATABASE_URL') or not result.skipped) else 1)
