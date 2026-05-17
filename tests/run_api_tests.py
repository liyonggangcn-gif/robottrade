#!/usr/bin/env python3
"""Run all web API tests against a live server"""
import sys, os, time, subprocess, requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Start server
proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'src.web.app:app', '--host', '127.0.0.1', '--port', '8765', '--log-level', 'error'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE
)
time.sleep(5)

BASE = 'http://127.0.0.1:8765'
T = 30

tests = [
    ('GET', '/api/status'),
    ('GET', '/api/strategies/available'),
    ('GET', '/api/strategy/list'),
    ('GET', '/api/strategy/run?strategy=hybrid'),
    ('GET', '/api/strategy/run?strategy=nonexistent'),
    ('GET', '/api/strategy/run'),
    ('GET', '/api/strategies/run_cached'),
    ('GET', '/api/positions'),
    ('GET', '/api/transactions?page=1&page_size=5'),
    ('GET', '/api/sectors/hot'),
    ('GET', '/api/news/gov'),
    ('GET', '/api/news/latest'),
    ('GET', '/api/news/pool_mapping'),
    ('GET', '/api/sync/status'),
    ('GET', '/api/messages'),
    ('GET', '/api/pool/status'),
    ('GET', '/api/pool/list?page=1&page_size=5'),
    ('GET', '/api/pool/signals'),
    ('GET', '/api/etf/picks'),
    ('GET', '/api/etf/qmt'),
    ('GET', '/api/cb/strategy'),
    ('POST', '/api/backtest/run', {'start_date':'2025-01-01','end_date':'2025-04-01','strategies':['tech']}),
    ('GET', '/api/backtest/status/dummy'),
    ('GET', '/api/agent/status'),
    ('GET', '/api/agent/decisions'),
    ('GET', '/api/agent/nav_history'),
    ('GET', '/api/logs/files'),
    ('GET', '/api/logs/content?file=app.log&lines=10'),
    ('GET', '/'),
    ('GET', '/selection'),
    ('GET', '/api/nonexistent'),
    ('POST', '/api/backtest/run', 'not json'),
]

SEP = '=' * 80
print('Testing %d endpoints' % len(tests))
print(SEP)
passed = failed = 0
slow = []
errors = []

for item in tests:
    method = item[0]
    path = item[1]
    t0 = time.time()
    try:
        if method == 'GET':
            r = requests.get(BASE + path, timeout=T)
        else:
            payload = item[2] if len(item) > 2 else None
            if isinstance(payload, str):
                r = requests.post(BASE + path, data=payload, headers={'Content-Type':'application/json'}, timeout=T)
            else:
                r = requests.post(BASE + path, json=payload, timeout=T)
        elapsed = time.time() - t0
        ok = r.status_code in (200, 302, 307, 400, 404, 422)
        if ok:
            passed += 1
            m = 'PASS'
        else:
            failed += 1
            m = 'FAIL'
            errors.append('%s: %d' % (path, r.status_code))
        if elapsed > 5:
            slow.append((path, elapsed))
        print('  [%-4s] %-4s %-55s %3d  %.2fs' % (m, method, path, r.status_code, elapsed))
    except Exception as e:
        failed += 1
        elapsed = time.time() - t0
        errors.append('%s: %s' % (path, e))
        print('  [ERR ] %-4s %-55s ERR  %.2fs' % (method, path, elapsed))

proc.terminate()

print('\n' + SEP)
print('Total: %d | Passed: %d | Failed: %d' % (passed+failed, passed, failed))
if slow:
    print('\nSLOW (>5s):')
    for p, t in slow:
        print('  %s: %.1fs' % (p, t))
if errors:
    print('\nERRORS:')
    for e in errors:
        print('  ' + e)
print(SEP)
