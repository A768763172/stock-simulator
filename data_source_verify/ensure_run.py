import os, sys, subprocess, time

HERE = r'C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\stock-simulator'
RES = os.path.join(HERE, 'data_source_verify', 'live_run_result.txt')


def kill_existing():
    pids = []
    try:
        r = subprocess.run(
            ['wmic', 'process', 'where', 'name="python.exe"',
             'get', 'ProcessId,CommandLine', '/FORMAT:CSV'],
            capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            if 'live_run.py' in line:
                for tok in line.split(','):
                    tok = tok.strip()
                    if tok.isdigit():
                        pids.append(int(tok))
        for pid in set(pids):
            try:
                subprocess.run(['taskkill', '/PID', str(pid), '/F', '/T'],
                               capture_output=True, timeout=20)
            except Exception:
                pass
    except Exception as e:
        return ['kill_err:' + str(e)]
    return sorted(set(pids))


killed = kill_existing()

sys.path.insert(0, HERE)
os.chdir(HERE)
sys.argv = ['live_run.py', '--force']
import live_run

status = 'UNKNOWN'
try:
    live_run.main()
    status = 'OK'
except SystemExit:
    status = 'SYSTEMEXIT'
except Exception:
    import traceback
    status = 'ERROR: ' + traceback.format_exc()[:3000]

with open(RES, 'w', encoding='utf-8') as f:
    f.write('killed_pids=' + str(killed) + '\n')
    f.write('status=' + status + '\n')
    f.write('finished_at=' + time.strftime('%Y-%m-%d %H:%M:%S') + '\n')
