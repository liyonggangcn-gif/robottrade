#!/usr/bin/env python3
"""
简化版早盘推送 - 跳过慢策略
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from datetime import datetime
from src.utils.config_loader import Config
from src.utils.notifier import NotifierFactory, send_alert
from src.portfolio.position_manager import PositionManager
from src.utils.db_utils import DBUtils

print("=" * 60)
print("  简化版早盘推送")
print("=" * 60)

# 只获取必要的3个策略
from src.strategy.hybrid_strategy import HybridStrategy
from src.strategy.dividend_strategy import DividendStrategy  
from src.strategy.value_strategy import ValueStrategy

strategies = [
    ('hybrid', HybridStrategy),
    ('dividend', DividendStrategy),
    ('value', ValueStrategy)
]

results = []
for name, cls in strategies:
    try:
        print(f"[{name}] 策略执行中...")
        s = cls()
        df = s.run(trade_date='2026-04-10', top_k=5)
        if df is not None and not df.empty:
            results.append(df)
            print(f"  选出 {len(df)} 只")
    except Exception as e:
        print(f"  [{name}] 失败: {e}")

# 合并结果
if results:
    import pandas as pd
    all_picks = pd.concat(results, ignore_index=True)
    print(f"\n共选出 {len(all_picks)} 只股票")
else:
    print("\n无选股结果")

# 推送
title = f"🌅 早盘推送 {datetime.now().strftime('%m月%d日')}"
content = f"""
### 简化版早盘推送

- 选股策略: hybrid, dividend, value
- 选出股票: {len(all_picks) if results else 0}只
- 数据日期: 2026-04-10

---
*测试推送*
"""

print("\n发送钉钉...")
send_alert(title, content, "morning_push")
print("完成!")