"""S2-01: SQLite buffer recovery with the real .NET agent against the HTTP reference API.

Scenarios (loopback, synthetic token and machine_id, real sensors on this PC):

  outage         server down for --outage-seconds (default 1800 = 30 min), optional forced kills meanwhile
  kill           agent force-terminated (TerminateProcess) --kills times while the server is up
  response-loss  server commits the batch, then drops the connection without a response (every --drop-every-th new batch)
  cap-age        server down longer than --buffer-max-age-seconds; oldest unsent data is pruned
  cap-bytes      server down until unsent bytes exceed --buffer-max-bytes; oldest unsent data is pruned

Checks compare the server ledger/sample rows with the agent's SQLite buffer (sent ledger, gaps) and the
sample_seq coverage. The report keeps aggregates only: no host name, user name, token or sensor series.

    python tests/run_buffer_recovery.py --exe <PcTelemetry.Agent.exe> --scenario outage --pc-label pc-a
"""
import argparse
import hashlib
import json
import platform
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from reference_api import Store, error  # noqa: E402
from run_agent_integration import register_loop  # noqa: E402


def utc(t=None):
    return (t or datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def git(*args):
    try:
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


class Server:
    """reference_api.Store behind a restartable HTTP server on a fixed loopback port."""

    def __init__(self, work, token, machine, drop_every=0, drop_max=0):
        self.store = Store(str(work / 'reference.sqlite3'), {}, token, machine)
        self.machine = machine
        self.drop_every, self.drop_max = drop_every, drop_max
        self.seen, self.dropped, self.replays, self.requests = set(), 0, 0, 0
        self.lock = threading.Lock()
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.httpd = None
        self.downtime = []
        self.down = False
        self.conns = set()

    def register_config(self, cid, sensors):
        self.store.configs[cid] = {'machine_id': self.machine, 'sensors': sensors}

    def handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            # keep-alive 연결도 서버 중지 때 끊어야 실제 장애가 된다(처리 스레드는 shutdown 후에도 남는다).
            def setup(self):
                super().setup()
                with outer.lock:
                    outer.conns.add(self.connection)

            def finish(self):
                with outer.lock:
                    outer.conns.discard(self.connection)
                super().finish()

            def do_POST(self):
                if outer.down:
                    self.close_connection = True
                    return
                try:
                    size = int(self.headers.get('Content-Length', '-1'))
                except ValueError:
                    size = -1
                raw = self.rfile.read(size) if 0 <= size <= 1048576 else b''
                status, body = (error(413, 'PAYLOAD_TOO_LARGE') if not raw and size != 0
                                else outer.store.dispatch(self.path, raw, self.headers))
                drop = False
                if self.path == '/v1/telemetry/batches' and status == 200:
                    bid = body['batch_id']
                    with outer.lock:
                        outer.requests += 1
                        if bid in outer.seen:
                            outer.replays += 1
                        else:
                            outer.seen.add(bid)
                            if (outer.drop_every and outer.dropped < outer.drop_max
                                    and len(outer.seen) % outer.drop_every == 0):
                                outer.dropped += 1
                                drop = True
                if drop:
                    # Committed, but the client never sees a response (contract: commit state unknown).
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                if status == 503:
                    self.send_header('Retry-After', '5')
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_):
                pass
        return Handler

    def start(self):
        for _ in range(50):
            try:
                self.httpd = ThreadingHTTPServer(('127.0.0.1', self.port), self.handler())
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError('could not rebind reference API port')
        self.down = False
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        if self.downtime and self.downtime[-1][1] is None:
            self.downtime[-1][1] = utc()

    def stop(self):
        self.down = True
        self.downtime.append([utc(), None])     # 새 요청 거부 시점부터 장애로 본다
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None
        with self.lock:
            live = list(self.conns)
        for c in live:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass

    def rows(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            batches = {i: (b, s) for i, b, s in db.execute('SELECT id, body, sha FROM batches')}
            counts = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                      for t in ('batches', 'samples', 'measurements', 'reaggregate')}
        return batches, counts


def buffer_rows(state):
    path = state / 'buffer.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        batches = [dict(r) for r in db.execute(
            'SELECT batch_id, boot_session_id, first_seq, last_seq, sample_count, sha256, state, attempts, '
            'quarantine_reason FROM batches ORDER BY seq')]
        gaps = [dict(r) for r in db.execute('SELECT * FROM gaps ORDER BY id')]
        open_samples = db.execute('SELECT count(*) FROM open_samples').fetchone()[0]
        dropped = db.execute("SELECT value FROM meta WHERE key='dropped_samples_total'").fetchone()
    return batches, gaps, open_samples, int(dropped[0]) if dropped else 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--exe', required=True)
    p.add_argument('--scenario', required=True, choices=('outage', 'kill', 'response-loss', 'cap-age', 'cap-bytes'))
    p.add_argument('--pc-label', required=True, help='anonymous PC label, e.g. pc-a')
    p.add_argument('--warmup-seconds', type=int, default=60)
    p.add_argument('--outage-seconds', type=int, default=1800)
    p.add_argument('--after-seconds', type=int, default=90, help='collection after the server is back')
    p.add_argument('--kills', type=int, default=3)
    p.add_argument('--kill-interval', type=int, default=40)
    p.add_argument('--kill-during-outage', type=int, default=1, help='outage scenario: forced kills while the server is down')
    p.add_argument('--drop-every', type=int, default=5)
    p.add_argument('--drop-max', type=int, default=10)
    p.add_argument('--buffer-max-age-seconds', type=int, default=120)
    p.add_argument('--buffer-max-bytes', type=int, default=150000)
    p.add_argument('--out', default=None)
    p.add_argument('--keep-state', action='store_true')
    a = p.parse_args()

    work = Path(tempfile.mkdtemp(prefix='pctf-s2-01-'))
    state = work / 'state'
    status_file = work / 'agent-status.json'
    machine = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)          # synthetic, never written to the report
    token_file = work / 'token'
    token_file.write_text(token, encoding='ascii')
    drop = (a.drop_every, a.drop_max) if a.scenario == 'response-loss' else (0, 0)
    server = Server(work, token, machine, *drop)
    server.start()

    stop_reg = threading.Event()
    registered = {}
    threading.Thread(target=register_loop, args=(server, state, 0.0, stop_reg, registered), daemon=True).start()

    # 시나리오 일정: (시작 후 초, 동작)
    w, o, after = a.warmup_seconds, a.outage_seconds, a.after_seconds
    plan = []
    extra = []
    if a.scenario == 'outage':
        total = w + o + after
        plan += [(w, 'server_stop'), (w + o, 'server_start')]
        for i in range(a.kill_during_outage):
            plan.append((w + o * (i + 1) // (a.kill_during_outage + 1), 'kill'))
    elif a.scenario == 'kill':
        total = w + a.kills * a.kill_interval + after
        plan += [(w + i * a.kill_interval, 'kill') for i in range(a.kills)]
    elif a.scenario == 'response-loss':
        total = w + after + a.drop_every * a.drop_max * 3 * 2
    else:
        o = a.outage_seconds if a.outage_seconds != 1800 else (a.buffer_max_age_seconds * 3 if a.scenario == 'cap-age' else 300)
        total = w + o + after
        plan += [(w, 'server_stop'), (w + o, 'server_start')]
        extra = (['--buffer-max-age-seconds', str(a.buffer_max_age_seconds)] if a.scenario == 'cap-age'
                 else ['--buffer-max-bytes', str(a.buffer_max_bytes)])
    plan.sort()

    def command(seconds):
        return [str(Path(a.exe).resolve()), 'run', '--state-dir', str(state), '--seconds', str(seconds),
                '--server', server.url, '--machine-id', machine, '--token-file', str(token_file),
                '--status-file', str(status_file), *extra]

    started_mono = time.monotonic()
    started = utc()
    timeline, runs = [], []
    proc = subprocess.Popen(command(total), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            encoding='utf-8', errors='replace')
    run_started = utc()
    status_seen = []
    for at, action in plan:
        while time.monotonic() - started_mono < at:
            time.sleep(0.2)
            if status_file.exists() and a.scenario.startswith('cap'):
                try:
                    st = json.loads(status_file.read_text(encoding='utf-8'))
                    if st['upload'].get('last_gap'):
                        status_seen.append(st['upload'])
                except (ValueError, KeyError, OSError):
                    pass
        timeline.append({'at_s': round(time.monotonic() - started_mono, 1), 'utc': utc(), 'action': action})
        if action == 'server_stop':
            server.stop()
        elif action == 'server_start':
            server.start()
        elif action == 'kill':
            proc.kill()                                  # TerminateProcess: no drain, no flush
            proc.wait()
            runs.append({'started': run_started, 'ended': utc(), 'exit_code': proc.returncode, 'forced_kill': True})
            time.sleep(3)
            remaining = max(5, int(total - (time.monotonic() - started_mono)))
            proc = subprocess.Popen(command(remaining), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                    encoding='utf-8', errors='replace')
            run_started = utc()
            timeline.append({'at_s': round(time.monotonic() - started_mono, 1), 'utc': utc(), 'action': 'restart',
                             'seconds': remaining})
    stderr = proc.communicate(timeout=total + 600)[1]
    runs.append({'started': run_started, 'ended': utc(), 'exit_code': proc.returncode, 'forced_kill': False})
    finished = utc()
    stop_reg.set()
    status = json.loads(status_file.read_text(encoding='utf-8'))
    server.stop()

    server_batches, counts = server.rows()
    local, gaps, open_samples, dropped_total = buffer_rows(state)
    events = [json.loads(line) for line in (state / 'events.jsonl').read_text(encoding='utf-8').splitlines() if line]
    event_types = Counter(e['type'] for e in events)

    # 서버 순번 범위
    seqs = {}
    for body, _ in server_batches.values():
        b = json.loads(body)
        for s in b['samples']:
            seqs.setdefault(b['boot_session_id'], []).append((s['sample_seq'], s['observed_at']))
    server_samples = sum(len(v) for v in seqs.values())
    dup = server_samples - sum(len({x for x, _ in v}) for v in seqs.values())
    gap_seqs = {(g['boot_session_id'], s) for g in gaps for s in range(g['first_seq'], g['last_seq'] + 1)}
    server_keys = {(boot, s) for boot, v in seqs.items() for s, _ in v}
    holes = []
    for boot, v in seqs.items():
        present = sorted({s for s, _ in v} | {s for b, s in gap_seqs if b == boot})
        holes += [(boot, x, y) for x, y in zip(present, present[1:]) if y != x + 1]
    sent = {r['batch_id']: r['sha256'] for r in local if r['state'] == 'sent'}
    pending = sum(1 for r in local if r['state'] == 'pending')
    quarantined = sum(1 for r in local if r['state'] == 'quarantined')
    restarts = sum(1 for r in runs if r['forced_kill'])

    checks = [
        ('agent final exit code', 0, runs[-1]['exit_code']),
        ('duplicate (boot_session_id, sample_seq) on server', 0, dup),
        ('server ledger batches == agent sent ledger (ids)', sorted(sent), sorted(server_batches)),
        ('server ledger sha256 == agent frozen sha256', True, all(server_batches[i][1] == sent.get(i) for i in server_batches)),
        ('server samples == sum(sample_count) of sent ledger', sum(r['sample_count'] for r in local if r['state'] == 'sent'),
         server_samples),
        ('server samples rows == ledger samples', server_samples, counts['samples']),
        ('pending batches after recovery', 0, pending),
        ('open samples after recovery', 0, open_samples),
        ('quarantined batches', 0, quarantined),
        ('server seqs intersect pruned ranges', 0, len(server_keys & gap_seqs)),
        ('seq holes not explained by pruning (<= forced restarts)', True, len(holes) <= restarts),
        ('ack mismatches', 0, event_types['upload_ack_mismatch']),
    ]
    opened = [e['details'] for e in events if e['type'] == 'buffer_opened']
    restore = {'restored_open_samples': [d.get('restored_open_samples') for d in opened],
               'restored_batches_frozen': [d.get('restored_batches_frozen') for d in opened],
               'pending_at_open': [d.get('pending_batches') for d in opened]}
    detail = {}
    if a.scenario == 'outage':
        down, up = server.downtime[0]
        during = [t for v in seqs.values() for _, t in v if down <= t <= up]
        dur = (datetime.fromisoformat(up.replace('Z', '+00:00')) - datetime.fromisoformat(down.replace('Z', '+00:00'))).total_seconds()
        lost_to_restart = 3 * a.kill_during_outage + 10 * a.kill_during_outage   # 재시작 대기 + LHM 초기화 여유
        detail = {'outage_s': round(dur, 1), 'samples_observed_during_outage_on_server': len(during)}
        checks += [
            ('outage length >= requested - 1 s', True, dur >= o - 1),
            ('samples observed during outage delivered (>= outage_s - restart allowance)', True, len(during) >= dur - lost_to_restart),
            ('upload_recovered events', True, event_types['upload_recovered'] >= 1),
            ('agent actually retried during outage (max attempts > 1)', True, max((r['attempts'] for r in local), default=0) > 1),
            ('no pruning in 30-min outage', 0, dropped_total),
        ]
    if a.scenario == 'kill':
        checks.append(('forced kills', a.kills, restarts))
        checks.append(('restart restored buffer (buffer_opened events)', a.kills + 1, event_types['buffer_opened']))
    if a.scenario == 'response-loss':
        detail = {'dropped_responses': server.dropped, 'replayed_requests': server.replays, 'ok_requests': server.requests}
        checks += [
            ('responses dropped after commit', a.drop_max, server.dropped),
            ('each dropped response replayed with same id (server returned stored 200)', True, server.replays >= server.dropped),
            ('server batches == unique ids', len(server.seen), counts['batches']),
        ]
    if a.scenario.startswith('cap'):
        reason = 'max_age' if a.scenario == 'cap-age' else 'max_bytes'
        detail = {'gaps': [{k: g[k] for k in ('reason', 'state', 'first_seq', 'last_seq', 'first_observed_at',
                                              'last_observed_at', 'batches', 'samples', 'bytes')} for g in gaps],
                  'status_last_gap_seen': status_seen[-1].get('last_gap') if status_seen else None}
        checks += [
            ('pruned samples > 0', True, dropped_total > 0),
            ('gap reason', [reason], sorted({g['reason'] for g in gaps})),
            ('dropped_samples_total == sum(gap samples)', sum(g['samples'] for g in gaps), dropped_total),
            ('buffer_pruned events', True, event_types['buffer_pruned'] >= 1),
            ('status file showed last_gap', True, bool(status_seen) or bool(status['upload'].get('last_gap'))),
            ('server seqs + pruned ranges cover collected range', True, len(holes) <= restarts),
        ]

    report = {
        'task': 'S2-01',
        'scenario': a.scenario,
        'kind': 'real agent -> tests/reference_api.py Store (SQLite ledger) over loopback HTTP',
        'executed_at_utc': {'started': started, 'finished': finished},
        'build': {'base_commit': git('rev-parse', 'HEAD'), 'dirty': bool(git('status', '--porcelain')),
                  'agent_version': status.get('agent_version'), 'python': platform.python_version(),
                  # 작업 트리가 커밋 전이어도 실제로 실행한 코드를 특정한다(exe는 공통 호스트라 dll을 기록).
                  'dll_sha256': {n: hashlib.sha256((Path(a.exe).resolve().parent / n).read_bytes()).hexdigest()
                                 for n in ('PcTelemetry.Agent.dll', 'PcTelemetry.Collector.dll')}},
        'environment': {'pc_label': a.pc_label, 'os': f'{platform.system()} {platform.version()}'},
        'reproduction': [
            'dotnet build agents/windows/src/PcTelemetry.Agent -c Release',
            'python tests/run_buffer_recovery.py --exe <PcTelemetry.Agent.exe> ' + ' '.join(sys.argv[3:]).replace(str(a.exe), '<exe>'),
        ],
        'plan_s': {'total': total, 'actions': plan},
        'timeline': timeline,
        'server_downtime_utc': server.downtime,
        'runs': runs,
        'checks': [{'name': n, 'expected': e, 'actual': v, 'pass': e == v} for n, e, v in checks],
        'detail': detail,
        'server_counts': counts,
        'agent_buffer': {'sent_ledger': len(sent), 'pending': pending, 'quarantined': quarantined, 'open_samples': open_samples,
                         'dropped_samples_total': dropped_total, 'max_attempts': max((r['attempts'] for r in local), default=0)},
        'seq_holes': [{'first_missing': x + 1, 'last_missing': y - 1} for _, x, y in holes],
        'restart_restore': restore,
        'events': dict(sorted(event_types.items())),
        'final_status_upload': status['upload'],
        'agent_stderr_tail': stderr[-2000:],
    }
    # 실패 항목에서 긴 ID 목록을 줄인다.
    for c in report['checks']:
        if isinstance(c['expected'], list) and len(c['expected']) > 5:
            c['expected'] = f'{len(c["expected"])} ids'
            c['actual'] = f'{len(c["actual"])} ids' + ('' if c['pass'] else ' (differs)')
    report['success'] = all(c['pass'] for c in report['checks'])
    out = Path(a.out) if a.out else ROOT / 'tests/results/s2-01' / f'{a.pc_label}-{started[:19].replace(":", "").replace("-", "")}Z-{a.scenario}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    assert token not in text and machine not in text
    out.write_text(text, encoding='utf-8')
    print(json.dumps({'success': report['success'], 'report': str(out),
                      'checks': [(c['name'], c['pass'], c['actual']) for c in report['checks']]}, indent=1, ensure_ascii=False))
    if a.keep_state:
        print('state kept:', work)
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
