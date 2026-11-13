"""S2-02: Windows service lifecycle with the installed agent (LocalSystem) against the HTTP reference API.

Run from an elevated prompt on a test PC. The service is installed with Install-PcTelemetry.ps1 from a publish
folder, uploads to a reference API on a fixed loopback port (synthetic token and machine_id), and real sensors.

Phases (the work folder keeps the reference ledger, synthetic credentials and checkpoints between phases):

  install   clean install, then automated scenarios: access check, graceful stop/start (server up and down),
            forced kill -> SCM recovery, UI close, token file unreadable, state folder write denied -> start failure
            -> SCM recovery. Ends with a checkpoint; the reference API stops when this process exits.
  resume    after a reboot (--expect reboot), a sign-out/sign-in (--expect logoff) or nothing (--expect none):
            restarts the reference API with the same ledger, waits until the buffer drains and checks continuity
            since the last checkpoint. Ends with a new checkpoint.
  uninstall Uninstall-PcTelemetry.ps1 -RemoveData (cleanup of this test installation).

The report keeps aggregates and event IDs only: no host name, user name, SID, token or sensor series.

    python tests/run_service_lifecycle.py --publish desktop/windows/publish --pc-label pc-a install
    (reboot, sign in)  python tests/run_service_lifecycle.py --publish desktop/windows/publish --pc-label pc-a resume --expect reboot
"""
import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import run_buffer_recovery  # noqa: E402
from reference_api import Store  # noqa: E402
from run_agent_integration import register_loop  # noqa: E402

SERVICE = 'PcTelemetryAgent'
DISPLAY = 'PC Telemetry Agent'
DATA = Path(os.environ['ProgramData']) / 'PcTelemetry'
STATE = DATA / 'state'
STATUS = DATA / 'status' / 'agent-status.json'
INSTALL_DIR = Path(os.environ['ProgramFiles']) / 'PcTelemetry'
POWERSHELL = Path(os.environ['SystemRoot']) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'


def utc(t=None):
    return (t or datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def parse_utc(s):
    return datetime.strptime(s[:26].rstrip('Z'), '%Y-%m-%dT%H:%M:%S.%f').replace(tzinfo=timezone.utc)


def git(*args):
    try:
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def ps(script, check=True):
    """Windows PowerShell 5.1 (the installer's target shell)."""
    r = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    if check and r.returncode != 0:
        raise RuntimeError(f'powershell failed ({r.returncode}): {r.stderr.strip()[:500]}')
    return r


def ps_json(script):
    out = ps(f'$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; & {{ {script} }} | ConvertTo-Json -Depth 4 -Compress').stdout.strip()
    if not out:
        return []
    v = json.loads(out)
    return v if isinstance(v, list) else [v]


class PersistentStore(Store):
    """reference_api.Store that reopens an existing ledger (phases run in separate processes, e.g. across a reboot)."""

    def __init__(self, path, configs, token, machine):
        if Path(path).exists():
            self.path, self.configs, self.token, self.machine = path, configs, token, machine
            self.fail_before_commit = False
            self.clock_synced = True
            self.lock = threading.Lock()
        else:
            super().__init__(path, configs, token, machine)


class Api:
    """Restartable reference API on the fixed port baked into the service arguments."""

    def __init__(self, work, creds):
        run_buffer_recovery.Store = PersistentStore
        self.server = run_buffer_recovery.Server(work, creds['token'], creds['machine_id'])
        self.server.port = creds['port']
        self.server.url = f'http://127.0.0.1:{creds["port"]}'
        self.stop_reg = threading.Event()
        self.registered = {}
        self.thread = None

    def start(self):
        self.server.start()
        if self.thread is None:
            self.thread = threading.Thread(target=register_loop, args=(self.server, STATE, 0, self.stop_reg, self.registered),
                                           daemon=True)
            self.thread.start()

    def stop(self):
        self.server.stop()

    def close(self):
        self.stop_reg.set()
        if self.server.httpd is not None:
            self.server.stop()


# ---------------------------------------------------------------- service / agent observations

def sc_query():
    out = subprocess.run(['sc.exe', 'queryex', SERVICE], capture_output=True, text=True, errors='replace').stdout
    def num(key):
        m = re.search(rf'{key}\s*:\s*(\d+)', out)
        return int(m.group(1)) if m else None
    # 표시 언어에 따라 "STATE" 이름표가 번역되므로 값(숫자 + 영문 상태)으로 찾는다.
    state = re.search(r':\s*\d\s+(STOPPED|START_PENDING|STOP_PENDING|RUNNING|CONTINUE_PENDING|PAUSE_PENDING|PAUSED)\b', out)
    return {'state': state.group(1) if state else None, 'pid': num('PID'),
            'win32_exit_code': num('WIN32_EXIT_CODE'), 'service_exit_code': num('SERVICE_EXIT_CODE')}


def process_info(pid):
    if not pid:
        return None
    rows = ps_json(f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if($p){{[ordered]@{{session_id=$p.SessionId; "
                   f"started_utc=$p.StartTime.ToUniversalTime().ToString('o')}}}}")
    return rows[0] if rows else None


def read_events(offset=0):
    path = STATE / 'events.jsonl'
    if not path.exists():
        return [], offset
    with open(path, 'rb') as f:
        f.seek(offset)
        data = f.read()
    lines = data.split(b'\n')
    complete, rest = lines[:-1], lines[-1]
    events = [json.loads(x) for x in complete if x.strip()]
    return events, offset + len(data) - len(rest)


class EventTail:
    def __init__(self):
        _, self.offset = read_events(0)
        self.seen = []

    def poll(self):
        new, self.offset = read_events(self.offset)
        self.seen.extend(new)
        return new

    def of(self, kind):
        return [e for e in self.seen if e['type'] == kind]


def status():
    try:
        return json.loads(STATUS.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def wait(pred, timeout, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return pred()


def buffer_stats():
    path = STATE / 'buffer.sqlite3'
    if not path.exists():
        return None
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=10)) as db:
        q = lambda sql: db.execute(sql).fetchone()[0]   # noqa: E731
        return {'pending': q("SELECT count(*) FROM batches WHERE state='pending'"),
                'quarantined': q("SELECT count(*) FROM batches WHERE state='quarantined'"),
                'sent': q("SELECT count(*) FROM batches WHERE state='sent'"),
                'open_samples': q('SELECT count(*) FROM open_samples'),
                'gaps': q('SELECT count(*) FROM gaps')}


def win_events(since, log, provider, ids=None):
    """Event IDs, levels and times. Messages only for our own provider (no user names there)."""
    t = since.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    flt = f"@{{LogName='{log}'; ProviderName='{provider}'; StartTime=[DateTime]::Parse('{t}',$null,'RoundtripKind').ToLocalTime()"
    if ids:
        flt += f"; Id={','.join(map(str, ids))}"
    flt += '}'
    own = provider == SERVICE
    script = (f"Get-WinEvent -FilterHashtable {flt} -ErrorAction SilentlyContinue | Sort-Object TimeCreated | ForEach-Object {{ "
              f"[ordered]@{{id=$_.Id; level=$_.LevelDisplayName; time_utc=$_.TimeCreated.ToUniversalTime().ToString('o'); "
              f"p0=[string]$_.Properties[0].Value{'; message=$_.Message' if own else ''}}} }}")
    rows = ps_json(script)
    if provider == 'Service Control Manager':
        rows = [r for r in rows if r.get('p0') == DISPLAY]
    for r in rows:
        if not own:
            r.pop('p0', None)
    return rows


def last_boot_utc():
    return ps_json("(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')")[0]


# ---------------------------------------------------------------- server-side ledger analysis

def ledger(api):
    """Server rows -> per boot session sorted sample list (seq, observed_at)."""
    with closing(sqlite3.connect(api.server.store.path)) as db:
        bodies = [json.loads(b) for (b,) in db.execute('SELECT body FROM batches')]
        sample_rows = db.execute('SELECT count(*) FROM samples').fetchone()[0]
    sessions = {}
    for b in bodies:
        for s in b['samples']:
            sessions.setdefault(b['boot_session_id'], []).append((s['sample_seq'], s['observed_at']))
    for v in sessions.values():
        v.sort()
    return bodies, sample_rows, sessions


def continuity(samples, since=None, until=None):
    """Max observed gap and seq holes between consecutive samples of one boot session in [since, until]."""
    rows = [(q, parse_utc(o)) for q, o in samples
            if (since is None or parse_utc(o) >= since) and (until is None or parse_utc(o) <= until)]
    holes, max_gap, gaps = 0, 0.0, []
    for (q1, t1), (q2, t2) in zip(rows, rows[1:]):
        g = (t2 - t1).total_seconds()
        max_gap = max(max_gap, g)
        if q2 != q1 + 1:
            holes += 1
        if g > 2.5:
            gaps.append({'from': utc(t1), 'to': utc(t2), 'seconds': round(g, 1), 'seq_jump': q2 - q1})
    return {'samples': len(rows), 'seq_holes': holes, 'max_gap_s': round(max_gap, 3), 'gaps_over_2_5s': gaps}


# ---------------------------------------------------------------- runner

class Run:
    def __init__(self, args):
        self.args = args
        self.work = Path(args.work).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.report = {
            'task': 'S2-02', 'phase': args.phase, 'pc_label': args.pc_label,
            'started_at_utc': utc(), 'scenarios': [],
        }
        self.creds = self.load_creds()
        self.api = None
        self.tail = None

    def load_creds(self):
        path = self.work / 'creds.json'
        if path.exists():
            return json.loads(path.read_text())
        creds = {'token': secrets.token_urlsafe(32), 'machine_id': str(uuid.uuid4()), 'port': self.args.port}
        path.write_text(json.dumps(creds))
        (self.work / 'device.token').write_text(creds['token'])
        return creds

    def build(self):
        manifest = Path(self.args.publish) / 'manifest.json'
        m = json.loads(manifest.read_text(encoding='utf-8-sig')) if manifest.exists() else {}
        installed = INSTALL_DIR / 'agent' / 'PcTelemetry.Agent.dll'
        return {
            'commit': m.get('commit'), 'dirty': m.get('dirty'), 'built_at_utc': m.get('built_at_utc'),
            'dotnet_sdk': m.get('dotnet_sdk'),
            'agent_dll_sha256': next((f['sha256'] for f in m.get('files', []) if f['path'].endswith('PcTelemetry.Agent.dll')), None),
            'installed_agent_dll_sha256': hashlib.sha256(installed.read_bytes()).hexdigest() if installed.exists() else None,
            'runner_commit': git('rev-parse', 'HEAD'),
        }

    def scenario(self, name, steps, expected):
        sc = {'name': name, 'started_at_utc': utc(), 'steps': steps, 'expected': expected, 'actual': {}, 'checks': {}}
        self.report['scenarios'].append(sc)
        print(f'== {name}', flush=True)
        return sc

    def finish(self, sc, **checks):
        sc['checks'].update(checks)
        sc['success'] = all(bool(v) for v in sc['checks'].values())
        sc['finished_at_utc'] = utc()
        print(f'   {"PASS" if sc["success"] else "FAIL"} {json.dumps(sc["checks"])}', flush=True)

    # ---- helpers on the live service

    def service_running_and_collecting(self, timeout=90):
        def ok():
            q = sc_query()
            s = status()
            return q['state'] == 'RUNNING' and s and s.get('collecting') and \
                (datetime.now(timezone.utc) - parse_utc(s['updated_at_utc'])).total_seconds() < 15
        return bool(wait(ok, timeout, 1))

    def stop_service(self):
        t0 = time.monotonic()
        r = subprocess.run(['sc.exe', 'stop', SERVICE], capture_output=True, text=True, errors='replace')
        stopped = wait(lambda: sc_query()['state'] == 'STOPPED', 60, 0.1)
        return {'sc_stop_exit': r.returncode, 'stopped': bool(stopped), 'stop_s': round(time.monotonic() - t0, 2)}

    def start_service(self):
        subprocess.run(['sc.exe', 'start', SERVICE], capture_output=True, text=True, errors='replace')
        return self.service_running_and_collecting()

    def closed_cleanly(self):
        wal = STATE / 'buffer.sqlite3-wal'
        return {
            'run_marker_removed': not (STATE / 'agent.running').exists(),
            # 마지막 연결을 닫으면 SQLite가 WAL을 체크포인트하고 지운다.
            'wal_removed_or_empty': (not wal.exists()) or wal.stat().st_size == 0,
            'status_collecting_false': (status() or {}).get('collecting') is False,
        }

    def max_server_seq(self, boot):
        _, _, sessions = ledger(self.api)
        return max((q for q, _ in sessions.get(boot, [])), default=None)

    def drain(self, timeout):
        def drained():
            b = buffer_stats()
            return b and b['pending'] == 0 and b['quarantined'] == 0
        return bool(wait(drained, timeout, 2))

    # ---- phases

    def phase_install(self):
        a = self.args
        self.api = Api(self.work, self.creds)
        self.api.start()
        since = datetime.now(timezone.utc) - timedelta(seconds=2)

        # S0 install
        sc = self.scenario('install', [
            'Install-PcTelemetry.ps1 -SkipWebView2 -ServerUrl http://127.0.0.1:<port> -MachineId <synthetic> -TokenFile <synthetic>',
            'read back service configuration, wait until collecting'],
            'LocalSystem, Automatic, quoted path, recovery restart 60s/60s/300s, failure flag on, event source, Running+collecting')
        if (INSTALL_DIR / 'Uninstall-PcTelemetry.ps1').exists() or sc_query()['state']:
            ps(f"& '{INSTALL_DIR / 'Uninstall-PcTelemetry.ps1'}' -RemoveData | Out-Null", check=False)
        if DATA.exists() and (STATE.exists() or STATUS.parent.exists()):
            raise RuntimeError(f'{DATA} still has state/status; clean it up before a clean-install run')
        report_path = self.work / 'install-report.json'
        r = ps(f"& '{Path(a.publish).resolve() / 'Install-PcTelemetry.ps1'}' -DashboardUrl '{self.api.server.url}/' "
               f"-ServerUrl '{self.api.server.url}' -MachineId '{self.creds['machine_id']}' "
               f"-TokenFile '{self.work / 'device.token'}' -SkipWebView2 -ReportPath '{report_path}'", check=False)
        install = json.loads(report_path.read_text(encoding='utf-8-sig')) if report_path.exists() else {}
        svc = install.get('steps', {}).get('service', {})
        self.tail = EventTail.__new__(EventTail)
        self.tail.offset, self.tail.seen = 0, []
        collecting = self.service_running_and_collecting()
        q = sc_query()
        pinfo = process_info(q['pid'])
        sc['actual'] = {'installer_exit': r.returncode, 'installer_success': install.get('success'), 'service': svc,
                        'sc': q, 'process_session_id': pinfo and pinfo['session_id']}
        self.report['build'] = self.build()
        self.finish(sc, installer_ok=r.returncode == 0 and install.get('success'),
                    local_system=svc.get('account') == 'LocalSystem', automatic=svc.get('start_mode') == 'Auto',
                    path_quoted=svc.get('path_quoted'), recovery=svc.get('recovery') == [60000, 60000, 300000],
                    recovery_on_error_exit=svc.get('recovery_on_error_exit'), event_source=svc.get('event_source'),
                    least_privilege=svc.get('required_privileges') == ['SeChangeNotifyPrivilege'] and svc.get('service_sid_type') == 1,
                    collecting=collecting, session0=bool(pinfo) and pinfo['session_id'] == 0,
                    installed_dll_matches_manifest=self.report['build']['agent_dll_sha256'] == self.report['build']['installed_agent_dll_sha256'])

        # S1 access check (service account: sensors, SQLite, token file)
        sc = self.scenario('access_check', ['wait for service_access_check event (after sensor warm-up)'],
                           'account local_system, state dir and buffer ok, token file ok, no driver_unavailable sensors')
        check = wait(lambda: (self.tail.poll(), self.tail.of('service_access_check'))[1], 60, 1)
        d = check[-1]['details'] if check else {}
        sc['actual'] = d
        self.finish(sc, event=bool(check), ok=d.get('ok') is True, account=d.get('account') == 'local_system',
                    token=d.get('token_file') == 'ok', buffer=d.get('buffer') == 'ok',
                    driver_sensors_supported=d.get('driver_unavailable') == 0 and (d.get('driver_sensors_supported') or 0) > 0,
                    token_privileges_reduced=d.get('privileges') == ['SeChangeNotifyPrivilege'])

        time.sleep(a.settle)
        boot = self.boot_session()

        # S2 graceful stop/start with the server up
        sc = self.scenario('graceful_stop_start', ['sc stop', 'inspect state folder', 'sc start'],
                           'stops < 5 s (OS shutdown wait limit on this PC), run marker removed, WAL checkpointed, '
                           'collecting=false; restart keeps boot session, seq continues above previous max, no unclean flag')
        self.drain(60)
        before_max = self.max_server_seq(boot)
        stop = self.stop_service()
        closed = self.closed_cleanly()
        self.tail.poll()
        stopped_event = bool(self.tail.of('agent_stopped'))
        mark = len(self.tail.seen)
        started = self.start_service()
        self.tail.poll()
        after = self.tail.seen[mark:]
        restarted = [e for e in after if e['type'] == 'agent_started']
        sc['actual'] = {'stop': stop, 'closed': closed, 'server_max_seq_before': before_max,
                        'next_seq_after': restarted[0]['details']['next_seq'] if restarted else None,
                        'boot_session_same': bool(restarted) and restarted[0]['boot_session_id'] == boot}
        self.finish(sc, stopped=stop['stopped'], stop_under_5s=stop['stop_s'] < 5, **closed, agent_stopped_event=stopped_event,
                    restarted=started, same_boot_session=sc['actual']['boot_session_same'],
                    seq_not_reused=bool(restarted) and before_max is not None and restarted[0]['details']['next_seq'] > before_max,
                    no_unclean_flag=not any(e['type'] == 'unclean_shutdown_detected' for e in after))

        # S3 graceful stop while the server is down (pending batches -> drain is bounded)
        sc = self.scenario('graceful_stop_server_down', ['stop reference API', f'collect {a.outage}s', 'sc stop', 'sc start',
                                                          'start reference API'],
                           'stop still < 5 s with pending batches, nothing lost: pending batches resent after restart')
        self.api.stop()
        time.sleep(a.outage)
        pending_before = (buffer_stats() or {}).get('pending')
        stop = self.stop_service()
        closed = self.closed_cleanly()
        pending_stopped = (buffer_stats() or {}).get('pending')
        started = self.start_service()
        self.api.start()
        drained = self.drain(180)
        sc['actual'] = {'stop': stop, 'closed': closed, 'pending_before_stop': pending_before,
                        'pending_after_stop': pending_stopped, 'drained_after_restart': drained}
        self.finish(sc, stopped=stop['stopped'], stop_under_5s=stop['stop_s'] < 5, **closed, restarted=started,
                    had_pending=(pending_before or 0) > 0, drained=drained)

        # S4 forced termination -> SCM recovery (no manual start)
        sc = self.scenario('forced_kill_recovery', ['taskkill /F the service process', 'wait for SCM recovery restart'],
                           'SCM restarts the service by itself (first recovery delay 60 s), unclean shutdown recorded, '
                           'open samples restored, same boot session, seq not reused')
        q0 = sc_query()
        self.drain(60)
        before_max = self.max_server_seq(boot)
        mark = len((self.tail.poll(), self.tail.seen)[1])
        t0 = time.monotonic()
        subprocess.run(['taskkill', '/F', '/PID', str(q0['pid'])], capture_output=True)
        died = wait(lambda: sc_query()['pid'] != q0['pid'], 10, 0.2)
        back = wait(lambda: sc_query()['state'] == 'RUNNING' and sc_query()['pid'] not in (0, q0['pid']), 150, 1)
        restart_s = round(time.monotonic() - t0, 1)
        collecting = self.service_running_and_collecting(60)
        time.sleep(3)
        self.tail.poll()
        after = self.tail.seen[mark:]
        unclean = [e for e in after if e['type'] == 'unclean_shutdown_detected']
        opened = [e for e in after if e['type'] == 'buffer_opened']
        restarted = [e for e in after if e['type'] == 'agent_started']
        sc['actual'] = {'killed_pid_gone': bool(died), 'restart_after_s': restart_s,
                        'unclean': unclean[0]['details'] if unclean else None,
                        'buffer_opened': opened[0]['details'] if opened else None,
                        'next_seq_after': restarted[0]['details']['next_seq'] if restarted else None,
                        'server_max_seq_before': before_max}
        self.finish(sc, scm_restarted=bool(back), restart_by_recovery_delay=55 <= restart_s <= 150, collecting=collecting,
                    unclean_recorded=bool(unclean), buffer_restored=bool(opened),
                    same_boot_session=bool(restarted) and restarted[0]['boot_session_id'] == boot,
                    seq_not_reused=bool(restarted) and before_max is not None and restarted[0]['details']['next_seq'] > before_max)

        # S5 UI close does not affect collection
        sc = self.scenario('ui_close', ['start installed shell', 'close its window', 'check service'],
                           'shell exits, service PID unchanged, status keeps updating')
        q0 = sc_query()
        shell = INSTALL_DIR / 'shell' / 'PcTelemetry.Shell.exe'
        proc = subprocess.Popen([str(shell)])
        time.sleep(8)
        closed_win = ps(f"$p=Get-Process -Id {proc.pid} -ErrorAction SilentlyContinue; if($p){{[void]$p.CloseMainWindow()}}", check=False)
        try:
            exit_code = proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
            exit_code = None
        t_close = datetime.now(timezone.utc)
        time.sleep(12)
        s = status()
        sc['actual'] = {'shell_exit': exit_code, 'close_cmd_exit': closed_win.returncode,
                        'status_updated_after_close': bool(s) and parse_utc(s['updated_at_utc']) > t_close}
        self.finish(sc, shell_exited=exit_code is not None, service_pid_unchanged=sc_query()['pid'] == q0['pid'],
                    status_fresh=sc['actual']['status_updated_after_close'], collecting=bool(s) and s.get('collecting'))

        # S6 token file unreadable (permission error on the credential) -> pause, keep collecting, recover
        sc = self.scenario('token_unreadable', ['rename state\\device.token', 'wait', 'restore it'],
                           'upload_paused_auth(TOKEN_FILE_UNREADABLE), collection continues (pending grows), '
                           'upload_auth_recovered within the 60 s pause, buffer drains')
        token = STATE / 'device.token'
        hidden = STATE / 'device.token.hidden'
        mark = len((self.tail.poll(), self.tail.seen)[1])
        token.rename(hidden)
        paused = wait(lambda: (self.tail.poll(), [e for e in self.tail.seen[mark:] if e['type'] == 'upload_paused_auth'])[1], 30, 1)
        time.sleep(15)
        pending_paused = (buffer_stats() or {}).get('pending')
        collecting_paused = (status() or {}).get('collecting')
        hidden.rename(token)
        recovered = wait(lambda: (self.tail.poll(), [e for e in self.tail.seen[mark:] if e['type'] == 'upload_auth_recovered'])[1], 90, 1)
        drained = self.drain(60)
        sc['actual'] = {'paused': paused[0]['details'] if paused else None, 'pending_while_paused': pending_paused,
                        'recovered': bool(recovered), 'drained': drained}
        self.finish(sc, paused=bool(paused) and paused[0]['details'].get('code') == 'TOKEN_FILE_UNREADABLE',
                    kept_collecting=collecting_paused is True and (pending_paused or 0) > 0,
                    recovered=bool(recovered), drained=drained, service_pid_unchanged=sc_query()['pid'] == q0['pid'])

        # S6b uploader dies at run time (SQLite write error) -> service failure path -> SCM recovery (PR #31 review P2)
        sc = self.scenario('upload_fault', [
            'hold an exclusive write lock on state\\buffer.sqlite3 (uploader MarkSent fails after busy_timeout 5 s)',
            'release the lock as soon as upload_failed is emitted', 'wait for SCM recovery restart (no manual start)'],
            'upload_failed (Event 1019) and Event 1005, collection stopped cleanly (agent_stopped, run marker removed), '
            'service exits with code 1, SCM restarts it, no unclean_shutdown_detected, same boot session, seq not reused, '
            'buffer drains')
        q0 = sc_query()
        boot = self.boot_session()
        self.drain(60)
        before_max = self.max_server_seq(boot)
        mark = len((self.tail.poll(), self.tail.seen)[1])
        t_lock = datetime.now(timezone.utc)
        t0 = time.monotonic()

        def seen(kind):
            self.tail.poll()
            return [e for e in self.tail.seen[mark:] if e['type'] == kind]
        lock = sqlite3.connect(str(STATE / 'buffer.sqlite3'), timeout=10, isolation_level=None)
        try:
            lock.execute('BEGIN EXCLUSIVE')
            failed = wait(lambda: seen('upload_failed'), 60, 0.1)
        finally:
            # 수집 종료 시 남은 샘플 동결(쓰기)이 busy_timeout 안에 끝나도록 바로 푼다.
            lock.execute('ROLLBACK')
            lock.close()
        t_failed = round(time.monotonic() - t0, 1)
        exited = wait(lambda: sc_query()['pid'] != q0['pid'], 30, 0.2)
        q_exit = sc_query()
        back = wait(lambda: sc_query()['state'] == 'RUNNING' and sc_query()['pid'] not in (0, q0['pid']), a.recovery_wait, 1)
        restart_s = round(time.monotonic() - t0, 1)
        collecting = self.service_running_and_collecting(60)
        time.sleep(3)
        after = (self.tail.poll(), self.tail.seen[mark:])[1]
        stopped = [e for e in after if e['type'] == 'agent_stopped']
        unclean = [e for e in after if e['type'] == 'unclean_shutdown_detected']
        restarted = [e for e in after if e['type'] == 'agent_started']
        app = win_events(t_lock, 'Application', SERVICE, [1005, 1019])
        drained = self.drain(120)
        sc['actual'] = {'upload_failed': failed[0]['details'] if failed else None, 'upload_failed_after_s': t_failed,
                        'sc_after_exit': q_exit, 'restart_after_s': restart_s,
                        'events': [{'id': e['id'], 'time_utc': e['time_utc'], 'message': e.get('message', '')[:300]} for e in app],
                        'next_seq_after': restarted[0]['details']['next_seq'] if restarted else None,
                        'server_max_seq_before': before_max, 'drained': drained}
        self.finish(sc, upload_failed=bool(failed), logged_1019=any(e['id'] == 1019 for e in app),
                    logged_1005=any(e['id'] == 1005 and 'upload failed' in (e.get('message') or '') for e in app),
                    collection_stopped_cleanly=bool(stopped) and not unclean, process_exited=bool(exited),
                    exit_code_1=q_exit['service_exit_code'] == 1 or q_exit['win32_exit_code'] == 1,
                    scm_restarted=bool(back), collecting=collecting,
                    same_boot_session=bool(restarted) and restarted[0]['boot_session_id'] == boot,
                    seq_not_reused=bool(restarted) and before_max is not None and restarted[0]['details']['next_seq'] > before_max,
                    drained=drained)

        # S7 state folder write denied for LocalSystem -> start failure logged -> SCM recovery after the fix
        sc = self.scenario('state_write_denied', [
            'sc stop', 'icacls state /deny SYSTEM:(OI)(CI)(W)', 'sc start (fails)', 'wait for a recovery attempt',
            'icacls state /remove:d SYSTEM', 'wait (no manual start)'],
            'Event 1002 start failure with the cause, service stops with exit code 1, SCM retries on its own; '
            'after the ACL is fixed the next recovery attempt runs and collects')
        stop = self.stop_service()
        t_deny = datetime.now(timezone.utc)
        subprocess.run(['icacls', str(STATE), '/deny', '*S-1-5-18:(OI)(CI)(W)'], capture_output=True)
        subprocess.run(['sc.exe', 'start', SERVICE], capture_output=True)
        def stopped_with_error():
            q = sc_query()
            return q['state'] == 'STOPPED' and (q['service_exit_code'] or q['win32_exit_code'])
        failed = wait(stopped_with_error, 60, 0.5)
        q_failed = sc_query()
        # SCM이 한 번 이상 다시 시도하는 것을 본다(복구 지연 60초 또는 실패 횟수에 따라 5분).
        retried = wait(lambda: len([e for e in win_events(t_deny, 'Application', SERVICE, [1002])]) >= 2, a.recovery_wait, 5)
        t_fix = datetime.now(timezone.utc)
        subprocess.run(['icacls', str(STATE), '/remove:d', '*S-1-5-18'], capture_output=True)
        recovered = self.service_running_and_collecting(a.recovery_wait)
        t_rec = datetime.now(timezone.utc)
        app = win_events(t_deny, 'Application', SERVICE, [1000, 1002])
        scm = win_events(t_deny, 'System', 'Service Control Manager')
        sc['actual'] = {'stop': stop, 'sc_after_failure': q_failed,
                        'start_failed_events': [{'time_utc': e['time_utc'], 'message': e.get('message', '')[:300]}
                                                for e in app if e['id'] == 1002],
                        'scm_events': [{'id': e['id'], 'time_utc': e['time_utc']} for e in scm],
                        'recovered_after_fix_s': round((t_rec - t_fix).total_seconds(), 1)}
        self.finish(sc, start_failed=bool(failed), exit_code_1=q_failed['service_exit_code'] == 1 or q_failed['win32_exit_code'] == 1,
                    failure_logged=any('UnauthorizedAccess' in (e.get('message') or '') for e in app if e['id'] == 1002),
                    scm_retried=bool(retried), recovered_without_manual_start=recovered)

        self.drain(180)
        self.common_checks(since)
        self.checkpoint()

    def boot_session(self):
        """Current boot session from the agent's state (survives service restarts within one OS boot)."""
        if self.tail:
            self.tail.poll()
        try:
            return json.loads((STATE / 'boot-session.json').read_text(encoding='utf-8-sig'))['boot_session_id']
        except (OSError, ValueError, KeyError):
            started = self.tail.of('agent_started') if self.tail else []
            return started[-1]['boot_session_id'] if started else None

    def common_checks(self, since):
        sc = self.scenario('ledger_and_logs', ['compare server ledger with buffer', 'collect Windows event IDs'],
                           'no duplicate or conflicting samples, nothing pending/quarantined, seq unique per boot session')
        bodies, sample_rows, sessions = ledger(self.api)
        b = buffer_stats()
        dup = sum(len(v) - len({q for q, _ in v}) for v in sessions.values())
        app = win_events(since, 'Application', SERVICE)
        scm = win_events(since, 'System', 'Service Control Manager')
        counts = {}
        for e in app:
            counts[str(e['id'])] = counts.get(str(e['id']), 0) + 1
        sc['actual'] = {'server_batches': len(bodies), 'server_sample_rows': sample_rows,
                        'boot_sessions': {k[:8] + '…': {'samples': len(v), 'first_seq': v[0][0], 'last_seq': v[-1][0]}
                                          for k, v in sessions.items()},
                        'buffer': b, 'replays': self.api.server.replays,
                        'event_log_app_counts': counts,
                        'event_log_scm_ids': [e['id'] for e in scm]}
        self.finish(sc, no_duplicate_samples=dup == 0 and sample_rows == sum(len(v) for v in sessions.values()),
                    nothing_pending=bool(b) and b['pending'] == 0, nothing_quarantined=bool(b) and b['quarantined'] == 0)

    def checkpoint(self):
        _, _, sessions = ledger(self.api)
        boot = self.boot_session()
        q = sc_query()
        pinfo = process_info(q['pid'])
        cp = {'at_utc': utc(), 'boot_session_id': boot,
              'last_seq': sessions[boot][-1][0] if boot in sessions else None,
              'service_pid': q['pid'], 'service_started_utc': pinfo and pinfo['started_utc'],
              'last_boot_utc': last_boot_utc(), 'events_offset': self.tail.offset if self.tail else 0}
        (self.work / 'checkpoint.json').write_text(json.dumps(cp))
        self.report['checkpoint'] = {k: v for k, v in cp.items() if k not in ('boot_session_id', 'service_pid', 'events_offset')}

    def phase_resume(self):
        a = self.args
        cp = json.loads((self.work / 'checkpoint.json').read_text())
        since = parse_utc(cp['at_utc'])
        self.api = Api(self.work, self.creds)
        self.api.start()
        self.tail = EventTail.__new__(EventTail)
        self.tail.offset, self.tail.seen = cp['events_offset'], []
        boot_now = last_boot_utc()
        q = sc_query()
        pinfo = process_info(q['pid'])
        collecting = self.service_running_and_collecting(120)
        drained = self.drain(a.drain_wait)
        self.tail.poll()
        _, _, sessions = ledger(self.api)
        new_boot = self.boot_session()
        app = win_events(since, 'Application', SERVICE)
        scm = win_events(since, 'System', 'Service Control Manager')
        kernel = win_events(since, 'System', 'Microsoft-Windows-Kernel-General', [12, 13])
        winlogon = win_events(since, 'System', 'Microsoft-Windows-Winlogon', [7001, 7002])
        self.report['build'] = self.build()
        sc_text = subprocess.run(['sc.exe', 'queryex', SERVICE], capture_output=True, text=True, errors='replace').stdout
        accepts = re.search(r'\(([A-Z_, ]+)\)', sc_text)
        common = {'accepted_controls': accepts.group(1).replace(' ', '').split(',') if accepts else None,
                  'process_session_id': pinfo and pinfo['session_id'],
                  'service_pid_same': q['pid'] == cp['service_pid'], 'collecting': collecting, 'drained': drained,
                  'last_boot_utc_before': cp['last_boot_utc'], 'last_boot_utc_now': boot_now,
                  'event_log_app': [{'id': e['id'], 'time_utc': e['time_utc']} for e in app],
                  'scm': [{'id': e['id'], 'time_utc': e['time_utc']} for e in scm],
                  'kernel_boot_shutdown': [{'id': e['id'], 'time_utc': e['time_utc']} for e in kernel],
                  'winlogon': [{'id': e['id'], 'time_utc': e['time_utc']} for e in winlogon]}
        old = sessions.get(cp['boot_session_id'], [])
        if a.expect == 'reboot':
            sc = self.scenario('reboot', ['checkpoint (install/resume phase)', 'restart Windows', 'sign in', 'resume phase'],
                               'new boot session with seq from 0, service auto-started after boot, previous session stopped '
                               'cleanly before shutdown (no unclean flag), all buffered samples delivered')
            new = sessions.get(new_boot, [])
            started = [e for e in self.tail.seen if e['type'] == 'agent_started' and e['boot_session_id'] == new_boot]
            reason = [e['details'].get('reason') for e in self.tail.seen if e['type'] == 'boot_session_started']
            unclean = [e for e in self.tail.seen if e['type'] == 'unclean_shutdown_detected']
            stopped = [e for e in self.tail.seen if e['type'] == 'agent_stopped' and e['boot_session_id'] == cp['boot_session_id']]
            shutdown = [e for e in kernel if e['id'] == 13]
            svc_start = parse_utc(pinfo['started_utc']) if pinfo else None
            boot_t = parse_utc(boot_now)
            last_old = parse_utc(old[-1][1]) if old else None
            first_new = parse_utc(new[0][1]) if new else None
            # 로그인 전 수집(로그아웃 상태와 같은 조건): 부팅 후 첫 사용자 로그인 전까지 관측된 샘플.
            # 로그인 시각: Winlogon 7001(부팅 이후 첫 건), 없으면 이 사용자 세션 explorer.exe 시작 시각.
            logons = [parse_utc(e['time_utc']) for e in winlogon if e['id'] == 7001 and parse_utc(e['time_utc']) > boot_t]
            explorer = ps_json("$sid=(Get-Process -Id $PID).SessionId; (Get-Process explorer -ErrorAction SilentlyContinue | "
                               "Where-Object SessionId -eq $sid | Sort-Object StartTime | Select-Object -First 1).StartTime."
                               "ToUniversalTime().ToString('o')")
            logon_t = min(logons) if logons else (parse_utc(explorer[0]) if explorer else None)
            before_logon = continuity(new, until=logon_t) if logon_t else None
            sc['actual'] = {**common, 'service_started_after_boot_s': svc_start and round((svc_start - boot_t).total_seconds(), 1),
                            'first_sample_after_boot_s': first_new and round((first_new - boot_t).total_seconds(), 1),
                            'previous_session_last_sample_utc': last_old and utc(last_old),
                            'os_shutdown_utc': shutdown[-1]['time_utc'] if shutdown else None,
                            'boot_session_started_reason': reason, 'unclean': [e['details'] for e in unclean],
                            'previous_session_continuity_since_checkpoint': continuity(old, since=since),
                            'new_session': {'first_seq': new[0][0] if new else None, 'samples': len(new)},
                            'new_session_continuity': continuity(new),
                            'first_logon_after_boot_s': logon_t and round((logon_t - boot_t).total_seconds(), 1),
                            'first_logon_source': 'winlogon_7001' if logons else ('explorer_start' if logon_t else None),
                            # 관측값(판정 아님): 자동 로그인 PC는 로그인 전 구간이 짧거나 없다.
                            'service_started_before_logon': bool(svc_start and logon_t and svc_start < logon_t),
                            'collected_before_logon': before_logon}
            self.finish(sc, new_boot_session=bool(new_boot) and new_boot != cp['boot_session_id'],
                        reason_reboot='reboot' in reason, seq_from_zero=bool(new) and new[0][0] == 0,
                        auto_started=bool(started) and collecting, previous_stopped_cleanly=bool(stopped) and not unclean,
                        drained=drained)
        else:
            sc = self.scenario(a.expect if a.expect != 'none' else 'continuity',
                               ['checkpoint', 'sign out and sign in again' if a.expect == 'logoff' else 'wait', 'resume phase'],
                               'same service process and boot session, collection continuous during the sign-out '
                               '(no sample gap > 2.5 s, no seq hole), buffered samples delivered')
            cont = continuity(old, since=since)
            sc['actual'] = {**common, 'continuity_since_checkpoint': cont}
            self.finish(sc, same_process=q['pid'] == cp['service_pid'], same_boot_session=new_boot == cp['boot_session_id'],
                        continuous=cont['samples'] > 0 and cont['seq_holes'] == 0 and cont['max_gap_s'] <= 2.5,
                        signed_out_in_window=a.expect != 'logoff' or any(e['id'] == 7002 for e in winlogon),
                        drained=drained)
        self.common_checks(since)
        self.checkpoint()

    def phase_uninstall(self):
        sc = self.scenario('uninstall', ['Uninstall-PcTelemetry.ps1 -RemoveData'], 'service, files, event source and data removed')
        report_path = self.work / 'uninstall-report.json'
        r = ps(f"& '{INSTALL_DIR / 'Uninstall-PcTelemetry.ps1'}' -RemoveData -ReportPath '{report_path}' | Out-Null", check=False)
        u = json.loads(report_path.read_text(encoding='utf-8-sig')) if report_path.exists() else {}
        sc['actual'] = {'exit': r.returncode, 'residue': u.get('residue')}
        res = u.get('residue') or {}
        self.finish(sc, success=u.get('success'), no_service=res.get('service') is False,
                    no_event_source=res.get('event_source') is False, no_data=res.get('data_root') is False)

    def run(self):
        try:
            getattr(self, f'phase_{self.args.phase}')()
        finally:
            if self.api:
                self.api.close()
        self.report['environment'] = {
            'os': platform.platform(), 'python': platform.python_version(),
            'wait_to_kill_service_timeout_ms': int(ps_json("(Get-ItemProperty HKLM:\\SYSTEM\\CurrentControlSet\\Control).WaitToKillServiceTimeout")[0]),
            'pawnio': (ps_json("(Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\PawnIO' -ErrorAction SilentlyContinue).DisplayVersion") or [None])[0],
            'elevated': bool(ctypes.windll.shell32.IsUserAnAdmin()),
        }
        self.report['finished_at_utc'] = utc()
        self.report['success'] = all(s.get('success') for s in self.report['scenarios'])
        text = json.dumps(self.report, indent=2, ensure_ascii=False)
        # 비식별 확인: 토큰·machine_id·호스트·계정 이름이 결과에 없어야 한다.
        for secret in (self.creds['token'], self.creds['machine_id'], platform.node(), os.environ.get('USERNAME', '\0')):
            if secret and secret.lower() in text.lower():
                raise RuntimeError('report contains an identifying value; not written')
        out = Path(self.args.out) if self.args.out else ROOT / 'tests' / 'results' / 's2-02' / (
            f'{self.args.pc_label}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{self.args.phase}'
            f'{"-" + self.args.expect if self.args.phase == "resume" else ""}.json')
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + '\n', encoding='utf-8')
        print(f'report: {out}  success={self.report["success"]}')
        return 0 if self.report['success'] else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument('phase', choices=('install', 'resume', 'uninstall'))
    p.add_argument('--publish', required=True, help='Publish-PcTelemetry.ps1 output folder')
    p.add_argument('--pc-label', required=True, help='anonymous PC label, e.g. pc-a')
    p.add_argument('--work', default=str(Path(os.environ.get('LOCALAPPDATA', '.')) / 'pctf-s2-02'),
                   help='keeps the reference ledger, synthetic credentials and checkpoint between phases')
    p.add_argument('--port', type=int, default=47199)
    p.add_argument('--expect', choices=('reboot', 'logoff', 'none'), default='none')
    p.add_argument('--settle', type=int, default=20)
    p.add_argument('--outage', type=int, default=30)
    p.add_argument('--recovery-wait', type=int, default=420, help='seconds to wait for an SCM recovery restart')
    p.add_argument('--drain-wait', type=int, default=600)
    p.add_argument('--out')
    args = p.parse_args()
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print('run from an elevated prompt', file=sys.stderr)
        return 2
    return Run(args).run()


if __name__ == '__main__':
    sys.exit(main())
