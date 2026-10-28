"""Read-only Windows probe; never changes wall clock or suspends the PC."""
import argparse
import ctypes
import json
import platform
from datetime import datetime, timezone
from pathlib import Path


def run(previous=None):
    if platform.system() != 'Windows':
        raise SystemExit('This probe requires a real Windows PC')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    realtime = ctypes.WinDLL('api-ms-win-core-realtime-l1-1-1.dll', use_last_error=True)
    for name in ('QueryUnbiasedInterruptTime', 'GetSystemTimePreciseAsFileTime'):
        getattr(kernel, name).argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
    kernel.QueryUnbiasedInterruptTime.restype = ctypes.c_int
    realtime.QueryInterruptTime.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
    realtime.QueryInterruptTime.restype = None
    kernel.GetSystemTimePreciseAsFileTime.restype = None

    def read():
        wall, biased, unbiased = (ctypes.c_ulonglong() for _ in range(3))
        if not kernel.QueryUnbiasedInterruptTime(ctypes.byref(unbiased)):
            raise ctypes.WinError(ctypes.get_last_error())
        realtime.QueryInterruptTime(ctypes.byref(biased))
        kernel.GetSystemTimePreciseAsFileTime(ctypes.byref(wall))
        return dict(wall_100ns=wall.value, biased_100ns=biased.value, unbiased_100ns=unbiased.value)

    readings = [read() for _ in range(50000)]
    steps = [b['wall_100ns']-a['wall_100ns'] for a, b in zip(readings, readings[1:])
             if b['wall_100ns'] > a['wall_100ns']]
    report = dict(contract_version='0.1.0-draft.2', executed_at_utc=datetime.now(timezone.utc).isoformat(),
        pc=platform.node(), environment=platform.platform(),
        source='QueryUnbiasedInterruptTime (100ns units, sleep excluded)',
        sample=readings[-1], probe_samples=len(readings),
        observed_wall_step_us_min=min(steps)/10 if steps else None,
        accuracy_verified=False, physical_sleep_verified=False,
        note='Call spacing is not guaranteed clock accuracy/resolution; synchronization needs separate evidence.')
    if previous:
        before = json.loads(Path(previous).read_text(encoding='utf-8'))
        if before['pc'] != report['pc']:
            raise SystemExit('Cannot compare different PCs')
        delta = {k: (report['sample'][k]-before['sample'][k])/10000 for k in report['sample']}
        reboot = delta['biased_100ns'] < 0 or delta['unbiased_100ns'] < 0
        excluded = delta['biased_100ns']-delta['unbiased_100ns']
        report.update(previous_file=str(previous), elapsed_ms=delta,
            excluded_sleep_ms=excluded, physical_sleep_verified=not reboot and excluded >= 1000,
            clock_jump_suspected=abs(delta['wall_100ns']-delta['biased_100ns']) >= 2000,
            requires_same_boot_confirmation=True, comparison_valid=not reboot)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--previous')
    args = parser.parse_args()
    report = run(args.previous)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
