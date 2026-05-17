#!/usr/bin/env python3
"""
极简版早盘推送 - 不运行策略，直接使用缓存数据
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import glob
from datetime import datetime
from src.utils.notifier import send_alert

print("=" * 60)
print("  极简版早盘推送")
print("=" * 60)

# 读取现有选股结果
output_dir = os.path.join(os.path.dirname(__file__), '..', 'output')
csv_files = glob.glob(os.path.join(output_dir, 'hybrid_picks_*.csv'))

if csv_files:
    latest_file = sorted(csv_files)[-1]
    print(f"读取: {latest_file}")
    import pandas as pd
    df = pd.read_csv(latest_file)
    stocks = df.head(5)[['ts_code', 'final_score']].to_string(index=False)
else:
    stocks = "无历史数据"

# 直接推送
title = f"🌅 早盘推送 {datetime.now().strftime('%m月%d日')}"
content = f"""
### 早盘选股结果

**日期**: 2026-04-10
**策略**: hybrid

| 排名 | 股票代码 | 评分 |
|------|----------|------|
{stocks}

---
*极简推送测试*
"""

print("\n发送钉钉...")
result = send_alert(title, content, "morning_push")
print(f"发送结果: {result}")
print("完成!")