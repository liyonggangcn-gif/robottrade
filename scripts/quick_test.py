#!/usr/bin/env python3
"""快速系统测试"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from datetime import datetime

print("="*50)
print("快速系统测试", datetime.now().strftime('%H:%M:%S'))
print("="*50)

# 测试1: 数据库
print("\n[1] 数据库...")
try:
    from src.utils.db_utils import DBUtils
    df = DBUtils.query_df("SELECT COUNT(*) as cnt FROM stock_daily LIMIT 1")
    print(f"  OK - 数据库连接成功")
except Exception as e:
    print(f"  FAIL - {e}")

# 测试2: 策略
print("\n[2] 策略...")
try:
    from src.strategy.center import StrategyCenter
    center = StrategyCenter()
    r = center.run_strategy('hybrid', top_k=3)
    print(f"  OK - Hybrid策略: {len(r) if r else 0}只")
except Exception as e:
    print(f"  FAIL - {str(e)[:60]}")

# 测试3: 股票池
print("\n[3] 股票池...")
try:
    from src.strategy.pool_strategy import PoolStrategy
    result = PoolStrategy().run(update_valuation=False)
    print(f"  OK - 股票池: {len(result.get('signals', []))}只信号")
except Exception as e:
    print(f"  FAIL - {str(e)[:60]}")

# 测试4: Web API
print("\n[4] Web API...")
try:
    import requests
    r = requests.get("http://localhost:8080/api/strategies/available", timeout=10)
    print(f"  OK - API响应: {r.status_code}")
except Exception as e:
    print(f"  FAIL - {str(e)[:40]}")

print("\n完成!")