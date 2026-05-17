#!/usr/bin/env python3
"""
早盘推送1: 多策略选股结果
只推送策略选股结果，不包含AI执行计划
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import glob
import json
import pandas as pd
from datetime import datetime
from src.utils.notifier import send_alert

print("=" * 60)
print("  早盘推送 - 多策略选股结果")
print("=" * 60)

# 读取缓存
output_dir = os.path.join(os.path.dirname(__file__), '..', 'output')
cache_files = glob.glob(os.path.join(output_dir, 'multi_strategy_*.json'))

if not cache_files:
    print("ERROR: 无缓存文件")
    send_alert("📊 多策略选股结果", "无选股缓存数据", "morning_strategy")
    sys.exit(1)

best_file = sorted(cache_files)[-1]
print(f"读取缓存: {best_file}")

with open(best_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

picks = data.get('picks', [])
if not picks:
    print("ERROR: 无选股结果")
    send_alert("📊 多策略选股结果", "无选股结果", "morning_strategy")
    sys.exit(1)

print(f"选出 {len(picks)} 只股票")

# 按策略分组
picks_df = pd.DataFrame(picks)
strategies = picks_df['strategy'].unique() if 'strategy' in picks_df.columns else ['hybrid']

# 构建内容
now = datetime.now()
title = f"📊 多策略选股 {now.strftime('%m月%d日')}"

content = f"### 多策略选股结果 ({now.strftime('%Y-%m-%d')})\n\n"

for strat in strategies:
    strat_picks = picks_df[picks_df['strategy'] == strat].head(5) if 'strategy' in picks_df.columns else picks_df.head(5)
    strat_name = {'hybrid': '混合策略', 'value': '价值策略', 'dividend': '红利策略'}.get(strat, strat)
    content += f"**◆ {strat_name}**\n"
    for i, row in strat_picks.iterrows():
        name = row.get('name', row.get('ts_code', ''))[:6]
        code = row.get('ts_code', '')[:9]
        score = row.get('final_score', 0)
        content += f"{i+1}. **{name}**({code}) {score:.3f}\n"
    content += "\n"

content += f"""
> 共 {len(picks)} 只股票

---
*数据来源: {best_file.split('/')[-1]}*
"""

print("\n发送钉钉...")
result = send_alert(title, content, "morning_strategy")
print(f"发送结果: {'OK' if result else 'FAIL'}")