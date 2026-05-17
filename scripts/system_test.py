#!/usr/bin/env python3
"""
综合测试方案 - 系统功能测试
测试项目：
1. 数据同步 - 数据源可用性
2. 策略选股 - 12个策略运行
3. Web API - 关键接口测试
4. 钉钉推送 - 通知功能
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime
import json

def test_data_sync():
    """测试1: 数据同步"""
    print("\n" + "="*60)
    print("测试1: 数据同步")
    print("="*60)
    
    results = {}
    
    try:
        from src.utils.db_utils import DBUtils
        
        # 1.1 数据库连接
        df = DBUtils.query_df("SELECT COUNT(*) as cnt FROM stock_daily LIMIT 1")
        results['db_connection'] = 'PASS' if not df.empty else 'FAIL'
        print(f"[{'OK' if results['db_connection'] == 'PASS' else 'FAIL'}] 数据库连接")
        
        # 1.2 今日数据
        today = datetime.now().strftime('%Y%m%d')
        df = DBUtils.query_df("SELECT COUNT(*) as cnt FROM stock_daily WHERE trade_date = ?", (today,))
        count = int(df['cnt'].iloc[0]) if not df.empty else 0
        results['today_data'] = 'PASS' if count > 0 else 'WARN (empty)'
        print(f"[{'OK' if count > 0 else 'WARN'}] 今日数据: {count} 条")
        
        # 1.3 因子数据
        df = DBUtils.query_df("SELECT COUNT(*) as cnt FROM stock_factors LIMIT 1")
        results['factors'] = 'PASS' if not df.empty else 'WARN'
        print(f"[{'OK' if not df.empty else 'WARN'}] 因子数据")
        
    except Exception as e:
        print(f"[FAIL] 数据库测试: {e}")
        results = {'error': str(e)}
    
    return results


def test_strategies():
    """测试2: 策略选股"""
    print("\n" + "="*60)
    print("测试2: 策略选股 (仅测试4个核心策略)")
    print("="*60)
    
    results = {}
    from src.strategy.center import StrategyCenter
    
    strategies = ['hybrid', 'dividend', 'value', 'sector_rotation']
    
    for strategy in strategies:
        try:
            center = StrategyCenter()
            result = center.run_strategy(strategy, top_k=3)
            count = len(result) if result is not None else 0
            results[strategy] = f'PASS ({count}只)'
            print(f"[OK] {strategy}: {count}只")
        except Exception as e:
            results[strategy] = f'FAIL: {str(e)[:50]}'
            print(f"[FAIL] {strategy}: {str(e)[:50]}")
    
    passed = sum(1 for v in results.values() if 'PASS' in v)
    print(f"\n策略通过: {passed}/{len(strategies)}")
    return results


def test_web_api():
    """测试3: Web API"""
    print("\n" + "="*60)
    print("测试3: Web API")
    print("="*60)
    
    results = {}
    import requests
    
    base_url = "http://localhost:8080"
    endpoints = [
        ("/api/health", "GET", "健康检查"),
        ("/api/strategies/available", "GET", "策略列表"),
        ("/api/selection/results", "GET", "选股结果"),
        ("/api/positions", "GET", "持仓查询"),
    ]
    
    for url, method, name in endpoints:
        try:
            r = requests.get(f"{base_url}{url}", timeout=10)
            results[name] = 'PASS' if r.status_code == 200 else f'FAIL ({r.status_code})'
            print(f"[{'OK' if r.status_code == 200 else 'FAIL'}] {name}")
        except Exception as e:
            results[name] = f'FAIL: {str(e)[:30]}'
            print(f"[FAIL] {name}: {str(e)[:30]}")
    
    return results


def test_dingtalk():
    """测试4: 钉钉推送"""
    print("\n" + "="*60)
    print("测试4: 钉钉推送")
    print("="*60)
    
    results = {}
    
    try:
        from src.utils.notifier import NotifierFactory
        notifier = NotifierFactory.create()
        success = notifier.send(f"🧪 系统测试 {datetime.now().strftime('%H:%M:%S')}")
        results['dingtalk'] = 'PASS' if success else 'FAIL'
        print(f"[{'OK' if success else 'FAIL'}] 钉钉推送")
    except Exception as e:
        results['dingtalk'] = f'FAIL: {str(e)[:50]}'
        print(f"[FAIL] 钉钉: {str(e)[:50]}")
    
    return results


def test_pool():
    """测试5: 股票池"""
    print("\n" + "="*60)
    print("测试5: 股票池策略")
    print("="*60)
    
    results = {}
    
    try:
        from src.strategy.pool_strategy import PoolStrategy
        result = PoolStrategy().run(update_valuation=False)
        signals = result.get('signals', [])
        results['pool_scan'] = f'PASS ({len(signals)}只)'
        print(f"[OK] 股票池: {len(signals)}只买入信号")
    except Exception as e:
        results['pool_scan'] = f'FAIL: {str(e)[:50]}'
        print(f"[FAIL] 股票池: {str(e)[:50]}")
    
    return results


def main():
    print("="*60)
    print("系统综合测试")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)
    
    all_results = {}
    all_results['数据同步'] = test_data_sync()
    all_results['策略选股'] = test_strategies()
    all_results['Web_API'] = test_web_api()
    all_results['钉钉推送'] = test_dingtalk()
    all_results['股票池'] = test_pool()
    
    print("\n" + "="*60)
    print("测试汇总")
    print("="*60)
    
    total_pass = 0
    total_fail = 0
    
    for category, results in all_results.items():
        print(f"\n【{category}】")
        for name, status in results.items():
            icon = "✓" if "PASS" in str(status) else "✗"
            print(f"  {icon} {name}: {status}")
            if "PASS" in str(status):
                total_pass += 1
            else:
                total_fail += 1
    
    print(f"\n总计: {total_pass} 通过, {total_fail} 失败")
    
    report_file = f"test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"报告: {report_file}")
    
    return 0 if total_fail == 0 else 1


if __name__ == '__main__':
    sys.exit(main())