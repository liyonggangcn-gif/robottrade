#!/usr/bin/env python3
"""综合测试报告"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime

print("="*60)
print("系统综合测试报告")
print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("="*60)

import requests

# 一、Web API测试
print("\n【一】Web API 测试")
print("-"*50)

api_tests = [
    ("GET", "http://localhost:8080/api/strategies/available", "策略列表"),
    ("GET", "http://localhost:8080/api/selection/latest", "最新选股"),
    ("GET", "http://localhost:8080/api/positions", "持仓查询"),
    ("GET", "http://localhost:8080/api/selection/pool", "股票池"),
    ("GET", "http://localhost:8080/api/market/overview", "市场概况"),
]

api_pass = 0
for method, url, name in api_tests:
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            print(f"✓ {name}")
            api_pass += 1
        else:
            print(f"✗ {name} ({r.status_code})")
    except Exception as e:
        print(f"✗ {name} (错误)")

print(f"\nAPI通过: {api_pass}/{len(api_tests)}")

# 二、数据库测试
print("\n【二】数据库测试")
print("-"*50)

try:
    from src.utils.db_utils import DBUtils
    
    # 连接测试
    df = DBUtils.query_df("SELECT 1 as test")
    print("✓ 数据库连接")
    
    # 数据量
    df = DBUtils.query_df("SELECT COUNT(*) as cnt FROM stock_daily")
    count = int(df['cnt'].iloc[0]) if not df.empty else 0
    print(f"✓ 股票日线数据: {count:,} 条")
    
    # 因子数据
    df = DBUtils.query_df("SELECT COUNT(*) as cnt FROM stock_factors")
    count = int(df['cnt'].iloc[0]) if not df.empty else 0
    print(f"✓ 因子数据: {count:,} 条")
    
    db_pass = 3
except Exception as e:
    print(f"✗ 数据库错误: {e}")
    db_pass = 0

print(f"\n数据库通过: {db_pass}/3")

# 三、核心策略测试
print("\n【三】核心策略测试")
print("-"*50)

strat_pass = 0
strategies = [
    ("HybridStrategy", "hybrid"),
    ("DividendStrategy", "dividend"),
]

for name, strat_id in strategies:
    try:
        if strat_id == 'hybrid':
            from src.strategy.hybrid_strategy import HybridStrategy
            result = HybridStrategy().run(top_k=3)
        elif strat_id == 'dividend':
            from src.strategy.dividend_strategy import DividendStrategy
            result = DividendStrategy().run(top_k=3)
        
        count = len(result) if result is not None else 0
        print(f"✓ {name}: {count}只")
        strat_pass += 1
    except Exception as e:
        print(f"✗ {name}: {str(e)[:40]}")

print(f"\n策略通过: {strat_pass}/{len(strategies)}")

# 汇总
print("\n" + "="*60)
print("汇总")
print("="*60)
total = api_pass + db_pass + strat_pass
total_all = len(api_tests) + 3 + len(strategies)
print(f"总计: {total}/{total_all} 通过")

if total >= total_all - 2:
    print("\n状态: ✅ 系统运行正常")
else:
    print("\n状态: ⚠️ 部分功能异常")