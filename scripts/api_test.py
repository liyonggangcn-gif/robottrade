#!/usr/bin/env python3
"""Web API 测试 V2"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

print("="*50)
print("Web API 测试 V2")
print("="*50)

import requests

# 更完整的API测试
tests = [
    ("http://localhost:8080/api/strategies/available", "策略列表"),
    ("http://localhost:8080/api/strategies/run", "GET", "策略运行"),
    ("http://localhost:8080/api/selection/latest", "GET", "最新选股"),
    ("http://localhost:8080/api/selection/pool", "GET", "股票池"),
    ("http://localhost:8080/api/positions", "GET", "持仓查询"),
    ("http://localhost:8080/api/market/overview", "GET", "市场概况"),
]

for item in tests:
    url = item[0]
    method = item[1] if len(item) > 2 else "GET"
    name = item[-1]
    
    try:
        if method == "GET":
            r = requests.get(url, timeout=10)
        else:
            r = requests.post(url, timeout=30)
        status = "OK" if r.status_code == 200 else f"FAIL({r.status_code})"
        print(f"[{status}] {name}")
    except Exception as e:
        print(f"[FAIL] {name}: {str(e)[:30]}")

print("\n完成")