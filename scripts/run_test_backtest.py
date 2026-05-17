#!/usr/bin/env python3
"""直接在服务器上运行回测"""
import sys
sys.path.insert(0, '/home/li/robottrade')

from src.backtest.backtest_engine import get_backtest_engine
import json

print("=== 回测测试 ===")

engine = get_backtest_engine()

# 运行回测: 2024-01-01 到 2024-06-30
result = engine.run(
    strategy='hybrid',
    start_date='2024-01-01',
    end_date='2024-06-30',
    top_k=10,
    rebalance_days=20,
    cost_rate=0.001
)

print(f"Success: {result.get('success')}")

if result.get('success'):
    metrics = result.get('metrics', {})
    print(f"\n=== 回测结果 ===")
    print(f"总收益率: {metrics.get('total_return', 0):.2f}%")
    print(f"年化收益率: {metrics.get('annualized_return', 0):.2f}%")
    print(f"夏普比率: {metrics.get('sharpe_ratio', 0):.2f}")
    print(f"最大回撤: {metrics.get('max_drawdown', 0):.2f}%")
    print(f"胜率: {metrics.get('win_rate', 0):.2f}%")
    print(f"交易次数: {metrics.get('total_trades', 0)}")
    
    # 打印月度收益
    daily = result.get('daily_returns', [])
    print(f"\n交易日数: {len(daily)}")
    
    # 打印前5行
    for i, d in enumerate(daily[:5]):
        print(f"  {d.get('date')}: {d.get('return', 0):.2f}%")
else:
    print(f"错误: {result.get('error')}")
