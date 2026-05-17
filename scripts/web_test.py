#!/usr/bin/env python3
"""
Web UI功能测试
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
from datetime import datetime


def test_api():
    """测试API接口"""
    print("\n" + "="*60)
    print("API接口测试")
    print("="*60)
    
    results = {}
    import requests
    base_url = "http://localhost:8080"
    
    endpoints = [
        ("/api/strategies/available", "策略列表"),
        ("/api/selection/results", "选股结果"),
        ("/api/selection/pool", "股票池"),
        ("/api/positions", "持仓"),
        ("/api/market/overview", "市场概况"),
    ]
    
    for url, name in endpoints:
        try:
            r = requests.get(f"{base_url}{url}", timeout=15)
            results[name] = 'PASS' if r.status_code == 200 else f'FAIL ({r.status_code})'
            print(f"[{'OK' if r.status_code == 200 else 'FAIL'}] {name}")
        except Exception as e:
            results[name] = f'FAIL: {str(e)[:30]}'
            print(f"[FAIL] {name}")
    
    return results


def test_strategies():
    """测试选股策略"""
    print("\n" + "="*60)
    print("选股策略测试")
    print("="*60)
    
    results = {}
    strategies = [
        ("hybrid", "HybridStrategy"),
        ("dividend", "DividendStrategy"),
        ("value", "ValueStrategy"),
    ]
    
    for strat_id, strat_name in strategies:
        try:
            if strat_id == 'hybrid':
                from src.strategy.hybrid_strategy import HybridStrategy
                result = HybridStrategy().run(top_k=3)
            elif strat_id == 'dividend':
                from src.strategy.dividend_strategy import DividendStrategy
                result = DividendStrategy().run(top_k=3)
            elif strat_id == 'value':
                from src.strategy.value_strategy import ValueStrategy
                result = ValueStrategy().run(top_k=3)
            
            count = len(result) if result is not None else 0
            results[strat_name] = f'PASS ({count}只)'
            print(f"[OK] {strat_name}: {count}只")
        except Exception as e:
            results[strat_name] = f'FAIL: {str(e)[:40]}'
            print(f"[FAIL] {strat_name}")
    
    return results


def test_positions():
    """测试持仓管理"""
    print("\n" + "="*60)
    print("持仓管理测试")
    print("="*60)
    
    results = {}
    
    # PositionManager
    try:
        from src.portfolio.position_manager import PositionManager
        pm = PositionManager()
        pos = pm.get_current_positions()
        results['PositionManager'] = f'PASS ({len(pos)}只)'
        print(f"[OK] PositionManager: {len(pos)}只")
    except Exception as e:
        results['PositionManager'] = f'FAIL'
        print(f"[FAIL] PositionManager")
    
    # HoldingManager
    try:
        from src.portfolio.holding_manager import HoldingManager
        hm = HoldingManager()
        status = hm.get_position_status()
        results['HoldingManager'] = 'PASS' if status is not None else 'FAIL'
        print(f"[OK] HoldingManager")
    except Exception as e:
        results['HoldingManager'] = 'FAIL'
        print(f"[FAIL] HoldingManager")
    
    # SimBroker
    try:
        from src.broker.sim_broker import SimBroker
        broker = SimBroker()
        account = broker.get_account()
        results['SimBroker'] = 'PASS'
        print(f"[OK] SimBroker: {account.total_assets:,.0f}")
    except Exception as e:
        results['SimBroker'] = 'FAIL'
        print(f"[FAIL] SimBroker")
    
    return results


def main():
    print("="*60)
    print("Web功能测试")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)
    
    all_results = {}
    all_results['API接口'] = test_api()
    all_results['选股策略'] = test_strategies()
    all_results['持仓管理'] = test_positions()
    
    print("\n" + "="*60)
    print("汇总")
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
    
    report = f"web_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"报告: {report}")


if __name__ == '__main__':
    main()