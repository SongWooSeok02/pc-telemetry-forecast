"""S1-02b: real .NET agent -> HTTP reference API integration on a Windows PC.

Runs agents/windows PcTelemetry.Agent against tests/reference_api.py (loopback, synthetic
token and machine_id). Sensor configs are registered from the agent's sensor-configs/
directory after a delay so the SENSOR_CONFIG_PENDING retry path is exercised.

The report keeps aggregates only (counts, intervals, quality rates). It does not record
the host name, user name or raw sensor time series (README section 13).

    python tests/run_agent_integration.py --exe <PcTelemetry.Agent.exe> --seconds 600 --pc-label pc-a
"""
import argparse
import hashlib
import json
import platform
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from reference_api import Store, server  # noqa: E402


def utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def git(*args):
    try:
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


class ReferenceBackend:
    """tests/reference_api.py (SQLite) on loopback."""
    kind = 'agent -> reference HTTP API integration (real sensors, loopback)'

    def __init__(self, work, token, machine):
        self.db = work / 'reference.sqlite3'
        self.store = Store(str(self.db), {}, token, machine)
        self.httpd = server(self.store)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self.machine = machine

    def register_config(self, cid, sensors):
        self.store.configs[cid] = {'machine_id': self.machine, 'sensors': sensors}

    def close(self):
        self.httpd.shutdown()

    def bodies(self):
        with sqlite3.connect(self.db) as db:
            return [json.loads(b) for (b,) in db.execute('SELECT body FROM batches')]

    def counts(self):
        with sqlite3.connect(self.db) as db:
            return {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                    for t in ('batches', 'samples', 'measurements')}


class ComposeBackend:
    """S1-03 FastAPI/PostgreSQL from infra/compose.yaml. Credentials are provisioned through an
    administrator path (psql in the db container), as backend/README requires before S2-03."""
    kind = 'agent -> S1-03 FastAPI/PostgreSQL (infra/compose.yaml, real sensors, loopback)'

    def __init__(self, project, env_file, url, token, machine):
        self.base = ['docker', 'compose', '-p', project, '--env-file', env_file,
                     '-f', str(ROOT / 'infra/compose.yaml')]
        env = dict(line.split('=', 1) for line in Path(env_file).read_text(encoding='utf-8').splitlines()
                   if '=' in line and not line.startswith('#'))
        self.user, self.dbname = env['POSTGRES_USER'], env['POSTGRES_DB']
        self.url, self.machine = url, machine
        self.psql("INSERT INTO device_credentials VALUES('%s','%s',now()+interval '1 day',false)"
                  % (hashlib.sha256(token.encode()).hexdigest(), machine))

    def psql(self, sql):
        out = subprocess.run([*self.base, 'exec', '-T', 'db', 'psql', '-U', self.user, '-d', self.dbname,
                              '-v', 'ON_ERROR_STOP=1', '-At', '-c', sql],
                             capture_output=True, text=True, encoding='utf-8', check=True)
        return out.stdout

    def register_config(self, cid, sensors):
        cfg = json.dumps({'machine_id': self.machine, 'sensors': sensors})
        assert '$cfg$' not in cfg
        self.psql(f"INSERT INTO sensor_configs VALUES('{cid}','{self.machine}',$cfg${cfg}$cfg$::jsonb)")

    def close(self):
        pass

    def bodies(self):
        rows = self.psql(f"SELECT canonical_body FROM ingestion_batches WHERE machine_id='{self.machine}'")
        return [json.loads(line) for line in rows.splitlines() if line]

    def counts(self):
        m = self.machine
        return {t: int(self.psql(f"SELECT count(*) FROM {t} WHERE machine_id='{m}'").strip())
                for t in ('ingestion_batches', 'ingestion_samples', 'measurements')}


def register_loop(backend, state, delay, stop, registered):
    folder = state / 'sensor-configs'
    while not stop.is_set():
        for path in sorted(folder.glob('*.json')) if folder.exists() else []:
            cid = path.stem
            if cid in registered:
                continue
            first_seen = registered.setdefault('_seen_' + cid, time.monotonic())
            if time.monotonic() - first_seen < delay:
                continue
            payload = json.loads(path.read_text(encoding='utf-8'))
            # 관리자 등록 대체(backend/README: S2-03 전 sensor_configs 직접 등록).
            backend.register_config(cid, payload['sensors'])
            registered['_payload_' + cid] = payload['sensors']
            registered[cid] = utc()
        stop.wait(0.5)


def analyse(bodies, counts, configs):
    samples = sorted(((b['boot_session_id'], s, b['sensor_config_id']) for b in bodies for s in b['samples']),
                     key=lambda x: (x[0], x[1]['sample_seq']))
    seqs = [s['sample_seq'] for _, s, _ in samples]
    gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)
    # 연속 순번 사이만 수집 간격으로 본다. 재시작으로 건너뛴 순번 구간은 간격 통계에서 제외한다.
    intervals = [b['monotonic_ms'] - a['monotonic_ms'] for (ba, a, _), (bb, b, _) in zip(samples, samples[1:])
                 if ba == bb and b['sample_seq'] == a['sample_seq'] + 1]
    quality = defaultdict(Counter)
    support = {}
    for _, s, cid in samples:
        for r in s['readings']:
            quality[r['sensor_id']][r['quality'] + (':' + r['reason'] if 'reason' in r else '')] += 1
            support[r['sensor_id']] = configs[cid][r['sensor_id']]['support']
    supported = {k: v for k, v in quality.items() if support[k] == 'supported'}
    ok = sum(c['ok'] for c in supported.values())
    total = sum(sum(c.values()) for c in supported.values())
    zero_non_ok = sum(1 for _, s, _ in samples for r in s['readings'] if r['quality'] != 'ok' and r['value'] is not None)
    on_time = sum(1 for i in intervals if abs(i - 1000) <= 100)
    return {
        'server_counts': counts,
        'boot_sessions': len({b for b, _, _ in samples}),
        'sensor_configs_used': sorted({c for _, _, c in samples}),
        'samples': len(samples),
        'seq_first': seqs[0] if seqs else None,
        'seq_last': seqs[-1] if seqs else None,
        'seq_duplicates': len(seqs) - len(set(zip([b for b, _, _ in samples], seqs))),
        'seq_gaps': gaps,
        'interval_ms': {
            'count': len(intervals),
            'min': min(intervals) if intervals else None,
            'max': max(intervals) if intervals else None,
            'on_time_pm10pct': on_time,
            'on_time_ratio': round(on_time / len(intervals), 6) if intervals else None,
        },
        'supported_ok_ratio': round(ok / total, 6) if total else None,
        'non_ok_with_value': zero_non_ok,
        'quality_counts': {k: dict(v) for k, v in sorted(quality.items())},
        'support': dict(sorted(support.items())),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--exe', required=True)
    p.add_argument('--seconds', type=int, default=120)
    p.add_argument('--register-delay', type=float, default=5.0)
    p.add_argument('--pc-label', required=True, help='anonymous PC label, e.g. pc-a')
    p.add_argument('--out', default=None)
    p.add_argument('--keep-state', action='store_true')
    p.add_argument('--restarts', type=int, default=0, help='restart the agent N times on the same state (same boot)')
    p.add_argument('--expect-resume', action='store_true', help='operator sleeps/resumes the PC during the run')
    p.add_argument('--backend', choices=('reference', 'compose'), default='reference')
    p.add_argument('--compose-project', default='pctf-s1-02b')
    p.add_argument('--compose-env', help='env file used for docker compose (never committed)')
    p.add_argument('--api-url', default='http://127.0.0.1:8000')
    a = p.parse_args()

    work = Path(tempfile.mkdtemp(prefix='pctf-s1-02b-'))
    state = work / 'state'
    machine = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)          # synthetic, never written to the report
    token_file = work / 'token'
    token_file.write_text(token, encoding='ascii')
    if a.backend == 'compose':
        backend = ComposeBackend(a.compose_project, a.compose_env, a.api_url, token, machine)
    else:
        backend = ReferenceBackend(work, token, machine)
    url = backend.url

    stop = threading.Event()
    registered = {}
    reg = threading.Thread(target=register_loop, args=(backend, state, a.register_delay, stop, registered), daemon=True)
    reg.start()

    command = [str(Path(a.exe).resolve()), 'run', '--state-dir', str(state), '--seconds', str(a.seconds),
               '--server', url, '--machine-id', machine, '--token-file', str(token_file),
               '--status-file', str(work / 'agent-status.json')]
    started = utc()
    runs, collected = [], 0
    for _ in range(a.restarts + 1):
        proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=a.seconds + 3600)
        run_stats = json.loads((state / 'stats.json').read_text(encoding='utf-8'))
        collected += run_stats['samples']
        runs.append({'exit_code': proc.returncode, 'samples': run_stats['samples'],
                     'boot_session_id': run_stats['boot_session_id']})
    finished = utc()
    stop.set()
    reg.join(2)
    backend.close()

    stats = json.loads((state / 'stats.json').read_text(encoding='utf-8'))
    events = [json.loads(line) for line in (state / 'events.jsonl').read_text(encoding='utf-8').splitlines() if line]
    bodies = backend.bodies()
    configs = {k[len('_payload_'):]: v for k, v in registered.items() if k.startswith('_payload_')}
    result = analyse(bodies, backend.counts(), configs)
    # S2-01: 동결 배치는 state/buffer.sqlite3에 있다(파일 스풀 대체).
    with closing(sqlite3.connect(state / 'buffer.sqlite3')) as buf:
        states = dict(buf.execute('SELECT state, count(*) FROM batches GROUP BY state').fetchall())
        pending = states.get('pending', 0) + buf.execute('SELECT count(*) FROM open_samples').fetchone()[0]
        quarantined = states.get('quarantined', 0)
    event_types = Counter(e['type'] for e in events)

    checks = [
        ('agent exit codes', [0] * len(runs), [r['exit_code'] for r in runs]),
        ('samples accepted by API == samples collected', collected, result['samples']),
        ('boot sessions (restarts keep the session)', 1, result['boot_sessions']),
        ('duplicate (boot_session_id, sample_seq)', 0, result['seq_duplicates']),
        ('sample_seq gaps (only reservation skips at restarts)', a.restarts, result['seq_gaps']),
        ('non-ok readings carrying a value (0 instead of null)', 0, result['non_ok_with_value']),
        ('buffer pending (batches + open samples) after drain', 0, pending),
        ('quarantined batches', 0, quarantined),
        ('SENSOR_CONFIG_PENDING retried then accepted', True, event_types['sensor_config_pending'] >= 1 and result['samples'] > 0),
        ('interval on-time ratio (|dt-1000|<=100ms) >= 0.99', True, (result['interval_ms']['on_time_ratio'] or 0) >= 0.99),
        ('supported sensor ok ratio >= 0.99', True, (result['supported_ok_ratio'] or 0) >= 0.99),
    ]
    if a.expect_resume:
        resumes = [e for e in events if e['type'] == 'suspend_resume']
        crossing = 0
        seq_batches = {}
        for b in bodies:
            for smp in b['samples']:
                seq_batches[smp['sample_seq']] = b['batch_id']
        for e in resumes:
            last = e['details']['last_seq_before']
            if last + 1 in seq_batches and seq_batches.get(last) == seq_batches[last + 1]:
                crossing += 1
        checks += [
            ('suspend_resume events', True, len(resumes) >= 1),
            ('batches spanning a suspend', 0, crossing),
            ('clock_jump false positives across suspend', 0, event_types['clock_jump']),
        ]
    report = {
        'task': 'S1-02b',
        'kind': backend.kind,
        'executed_at_utc': {'started': started, 'finished': finished},
        'build': {
            'base_commit': git('rev-parse', 'HEAD'),
            'dirty': bool(git('status', '--porcelain')),
            'agent_version': stats['host'].get('agent_version'),
            'lhm_version': stats['host'].get('lhm_version'),
            'python': platform.python_version(),
        },
        'environment': {
            'pc_label': a.pc_label,
            'os_version': stats['host'].get('os_version'),
            'is_elevated': stats['host'].get('is_elevated'),
            'is_system': stats['host'].get('is_system'),
            'pawnio_installed': stats['host'].get('pawnio_installed'),
            'pawnio_version': stats['host'].get('pawnio_version'),
            'sensor_sources': sorted({s['source'] for s in stats['sensors'] or []}),
        },
        'reproduction': [
            'dotnet build agents/windows/src/PcTelemetry.Agent -c Release',
            f'python tests/run_agent_integration.py --exe <PcTelemetry.Agent.exe> --seconds {a.seconds} '
            f'--register-delay {a.register_delay} --pc-label {a.pc_label} --restarts {a.restarts}'
            + (' --expect-resume' if a.expect_resume else '')
            + (f' --backend compose --compose-project {a.compose_project} --compose-env <env file>'
               if a.backend == 'compose' else ''),
        ],
        'duration_s_per_run': a.seconds,
        'runs': runs,
        'checks': [{'name': n, 'expected': e, 'actual': v, 'pass': e == v} for n, e, v in checks],
        'result': result,
        'agent_stats': {k: stats[k] for k in ('cycles', 'samples', 'source_errors', 'cycle_failures', 'resumes',
                                               'clock_jumps', 'intervals', 'upload', 'lhm_open_ms')},
        'events': dict(sorted(event_types.items())),
        'config_registration_utc': {k: v for k, v in registered.items() if not k.startswith(('_seen_', '_payload_'))},
        'agent_stderr_tail': proc.stderr[-2000:],
    }
    report['success'] = all(c['pass'] for c in report['checks'])
    out = Path(a.out) if a.out else ROOT / 'tests/results/s1-02b' / f'{a.pc_label}-{started[:19].replace(":", "").replace("-", "")}Z-{a.backend}-integration.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'success': report['success'], 'report': str(out),
                      'checks': [(c['name'], c['pass'], c['actual']) for c in report['checks']]}, indent=1, ensure_ascii=False))
    if not a.keep_state:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print('state kept:', work)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
