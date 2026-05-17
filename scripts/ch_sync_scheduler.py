#!/usr/bin/env python3
"""
ClickHouse 增量同步调度脚本
每天 7:00 运行，同步昨日数据到 ClickHouse
"""
import sys
import os
import time
import datetime
from datetime import datetime, timedelta
import subprocess

# 同步脚本路径
SYNC_SCRIPT = os.path.join(os.path.dirname(__file__), 'sync_mysql_to_clickhouse.py')

def run_sync():
    """运行同步"""
    print("="*60)
    print("ClickHouse 增量同步")
    print("="*60)
    
    try:
        result = subprocess.run(
            ['python3', SYNC_SCRIPT],
            capture_output=True,
            text=True,
            timeout=3600  # 1小时超时
        )
        
        if result.returncode == 0:
            print("同步成功")
            return True
        else:
            print(f"同步失败: {result.stderr}")
            return False
    except Exception as e:
        print(f"同步异常: {e}")
        return False

if __name__ == '__main__':
    run_sync()
