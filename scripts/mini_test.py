#!/usr/bin/env python3
"""极简测试"""
import sys
os = __import__('os')
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

print("[1] 测试导入...")
try:
    from src.utils.db_utils import DBUtils
    print("  DBUtils OK")
except Exception as e:
    print(f"  FAIL: {e}")

print("[2] 测试查询...")
try:
    df = DBUtils.query_df("SELECT 1 as test")
    print(f"  OK - 查询成功")
except Exception as e:
    print(f"  FAIL: {e}")

print("\n完成")