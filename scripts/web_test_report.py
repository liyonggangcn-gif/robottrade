#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web API 完整功能+性能测试报告
===============================
测试目标: 192.168.3.22:8080
"""
import requests
import time
import json
import sys
import os
from datetime import datetime

BASE = 'http://127.0.0.1:8080'
T = 30

def test_all():
    results = []
    slow = []
    errors = []
    passed = 0
    failed = 0
    
    # ============================================================
    # 1. 健康检查
    # ============================================================
    tests = [
        ('GET', '/api/status', 200, '健康检查'),
        ('GET', '/api/pool/status', 200, '股票池状态'),
    ]
    
    # ============================================================
    # 2. 策略接口
    # ============================================================
    tests += [
        ('GET', '/api/strategies/available', 200, '可用策略列表'),
        ('GET', '/api/strategy/list', 200, '策略详情列表'),
        ('GET', '/api/strategy/run?strategy=hybrid', 200, '单策略运行(hybrid)'),
        ('GET', '/api/strategy/run?strategy=nonexistent', 400, '无效策略名'),
        ('GET', '/api/strategy/run', 422, '缺少策略参数'),
        ('GET', '/api/strategies/run_cached', 200, '多策略缓存'),
    ]
    
    # ============================================================
    # 3. 持仓/交易
    # ============================================================
    tests += [
        ('GET', '/api/positions', 200, '持仓列表'),
        ('GET', '/api/transactions?page=1&page_size=5', 200, '交易记录分页'),
    ]
    
    # ============================================================
    # 4. 板块/新闻
    # ============================================================
    tests += [
        ('GET', '/api/sectors/hot', 200, '热门板块'),
        ('GET', '/api/news/gov', 200, '政府新闻'),
        ('GET', '/api/news/latest', 200, '最新新闻'),
        ('GET', '/api/news/pool_mapping', 200, '新闻股票映射'),
    ]
    
    # ============================================================
    # 5. 同步/数据源
    # ============================================================
    tests += [
        ('GET', '/api/sync/status', 200, '同步状态'),
    ]
    
    # ============================================================
    # 6. 消息
    # ============================================================
    tests += [
        ('GET', '/api/messages', 200, '消息列表'),
    ]
    
    # ============================================================
    # 7. 股票池
    # ============================================================
    tests += [
        ('GET', '/api/pool/list?page=1&page_size=5', 200, '股票池列表'),
        ('GET', '/api/pool/signals', 200, '买入信号'),
        ('GET', '/api/pool/health_check', 200, '健康检查'),
        ('GET', '/api/pool/valuation', 200, '估值一览'),
    ]
    
    # ============================================================
    # 8. ETF/可转债
    # ============================================================
    tests += [
        ('GET', '/api/etf/picks', 200, 'ETF选股'),
        ('GET', '/api/etf/qmt', 200, 'ETF QMT持仓'),
        ('GET', '/api/cb/strategy', 200, '可转债策略'),
    ]
    
    # ============================================================
    # 9. 回测
    # ============================================================
    tests += [
        ('POST', '/api/backtest/run', 200, '回测运行', {'start_date':'2025-01-01','end_date':'2025-04-01','strategies':['tech']}),
        ('GET', '/api/backtest/status/dummy', 200, '回测状态'),
    ]
    
    # ============================================================
    # 10. Agent
    # ============================================================
    tests += [
        ('GET', '/api/agent/status', 200, 'Agent状态'),
        ('GET', '/api/agent/decisions', 200, 'Agent决策'),
        ('GET', '/api/agent/nav_history', 200, 'Agent净值'),
    ]
    
    # ============================================================
    # 11. 日志
    # ============================================================
    tests += [
        ('GET', '/api/logs/files', 200, '日志文件列表'),
        ('GET', '/api/logs/content?file=app.log&lines=10', 200, '日志内容'),
    ]
    
    # ============================================================
    # 12. 页面
    # ============================================================
    tests += [
        ('GET', '/', 200, '首页'),
        ('GET', '/selection', 200, '选股中心'),
        ('GET', '/api/nonexistent', 404, '404未知路由'),
    ]
    
    # ============================================================
    # 13. 错误处理
    # ============================================================
    tests += [
        ('POST', '/api/backtest/run', 422, '无效JSON', 'not json'),
    ]
    
    # ============================================================
    # Run tests
    # ============================================================
    print("="*90)
    print("Web API 功能+性能测试报告")
    print("目标: 192.168.3.22:8080")
    print("时间: %s" % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print("="*90)
    
    for item in tests:
        method = item[0]
        path = item[1]
        expected = item[2]
        name = item[3]
        payload = item[4] if len(item) > 4 else None
        
        t0 = time.time()
        try:
            if method == 'GET':
                r = requests.get(BASE + path, timeout=T)
            else:
                if isinstance(payload, str):
                    r = requests.post(BASE + path, data=payload, headers={'Content-Type':'application/json'}, timeout=T)
                else:
                    r = requests.post(BASE + path, json=payload, timeout=T)
            
            elapsed = time.time() - t0
            status_ok = r.status_code == expected
            
            if status_ok:
                passed += 1
                marker = 'PASS'
            else:
                failed += 1
                marker = 'FAIL'
                errors.append('%s: 期望%d, 实际%d' % (name, expected, r.status_code))
            
            if elapsed > 10:
                slow.append((name, path, elapsed))
            
            results.append({
                'name': name,
                'method': method,
                'path': path,
                'status': r.status_code,
                'expected': expected,
                'time': elapsed,
                'ok': status_ok,
            })
            
            print('[%-4s] %-30s %3d (期望%d) %6.2fs' % (marker, name, r.status_code, expected, elapsed))
            
        except Exception as e:
            failed += 1
            elapsed = time.time() - t0
            errors.append('%s: %s' % (name, str(e)[:100]))
            results.append({
                'name': name,
                'method': method,
                'path': path,
                'status': 0,
                'expected': expected,
                'time': elapsed,
                'ok': False,
                'error': str(e)[:100],
            })
            print('[ERR ] %-30s ERROR  %6.2fs  %s' % (name, elapsed, str(e)[:60]))
    
    # ============================================================
    # Performance summary
    # ============================================================
    times = [r['time'] for r in results if r['ok']]
    if times:
        avg_time = sum(times) / len(times)
        max_time = max(times)
        min_time = min(times)
        p50 = sorted(times)[len(times)//2]
        p90 = sorted(times)[int(len(times)*0.9)]
        p99 = sorted(times)[min(int(len(times)*0.99), len(times)-1)]
    else:
        avg_time = max_time = min_time = p50 = p90 = p99 = 0
    
    # ============================================================
    # Report
    # ============================================================
    print("\n" + "="*90)
    print("测试汇总")
    print("="*90)
    print("总计: %d | 通过: %d | 失败: %d | 成功率: %.1f%%" % (
        passed+failed, passed, failed, passed*100/max(passed+failed,1)))
    
    print("\n性能统计:")
    print("  平均响应: %.2fs" % avg_time)
    print("  最快: %.2fs" % min_time)
    print("  最慢: %.2fs" % max_time)
    print("  P50: %.2fs" % p50)
    print("  P90: %.2fs" % p90)
    print("  P99: %.2fs" % p99)
    
    if slow:
        print("\n慢速端点 (>10s):")
        for name, path, t in sorted(slow, key=lambda x: -x[2]):
            print("  %-30s %6.2fs  %s" % (name, t, path))
    
    if errors:
        print("\n错误详情:")
        for e in errors:
            print("  - %s" % e)
    
    # ============================================================
    # Save report
    # ============================================================
    report = {
        'target': '192.168.3.22:8080',
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'summary': {
            'total': passed+failed,
            'passed': passed,
            'failed': failed,
            'success_rate': round(passed*100/max(passed+failed,1), 1),
        },
        'performance': {
            'avg': round(avg_time, 2),
            'min': round(min_time, 2),
            'max': round(max_time, 2),
            'p50': round(p50, 2),
            'p90': round(p90, 2),
            'p99': round(p99, 2),
        },
        'slow_endpoints': [{'name': n, 'path': p, 'time': round(t, 2)} for n, p, t in slow],
        'errors': errors,
        'results': results,
    }
    
    out_path = '/tmp/web_test_report.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print("\n报告已保存: %s" % out_path)
    print("="*90)
    
    return passed, failed

if __name__ == '__main__':
    passed, failed = test_all()
    sys.exit(0 if failed == 0 else 1)
