#!/usr/bin/env python3
"""
极简版早盘推送 - 只用缓存，不用策略
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import glob
import json
import pandas as pd
from datetime import datetime

print("=" * 60)
print("  极简版早盘推送 (使用缓存)")
print("=" * 60)

# Step 1: 读取缓存
output_dir = os.path.join(os.path.dirname(__file__), '..', 'output')
cache_files = glob.glob(os.path.join(output_dir, 'multi_strategy_*.json'))

if not cache_files:
    print("ERROR: 无缓存文件")
    sys.exit(1)

best_file = sorted(cache_files)[-1]
print(f"读取缓存: {best_file}")

with open(best_file, 'r', encoding='utf-8') as f:
    data = json.load(f)

picks = data.get('picks', [])
print(f"选出 {len(picks)} 只股票")

if not picks:
    print("ERROR: 无选股结果")
    sys.exit(1)

# Step 2: 显示前5只
print("\n=== Top 5 股票 ===")
for i, p in enumerate(picks[:5]):
    print(f"{i+1}. {p.get('ts_code')} {p.get('name')} Score={p.get('final_score', 0):.3f}")

# Step 3: AI分析（只分析前3只）
print("\n=== AI分析 ===")
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

def analyze_remote(ts_code):
    try:
        cmd = f"cd /home/li/ai_fund/ai-hedge-fund && /home/li/robottrade/venv/bin/python run_cn.py {ts_code}"
        env = os.environ.copy()
        env['DEEPSEEK_API_KEY'] = 'sk-e4cd8339e40c42cb9275d6a16e0f56a1'
        result = subprocess.run(
            ['ssh', 'li@192.168.3.22', cmd],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=120,
            env=env
        )
        if result.returncode == 0:
            return {'ts_code': ts_code, 'success': True, 'output': result.stdout}
        else:
            return {'ts_code': ts_code, 'success': False}
    except Exception as e:
        return {'ts_code': ts_code, 'success': False, 'error': str(e)}

stock_codes = [p['ts_code'] for p in picks[:3]]
print(f"分析: {stock_codes}")

results = []
with ThreadPoolExecutor(max_workers=3) as executor:
    futures = {executor.submit(analyze_remote, code): code for code in stock_codes}
    for future in as_completed(futures):
        r = future.result()
        results.append(r)
        print(f"  [{('OK' if r['success'] else 'FAIL')}] {r['ts_code']}")

# Step 4: 解析结果
buy_signals = []
sell_signals = []
for r in results:
    if not r['success']:
        continue
    output = r['output']
    bullish = output.count('bullish')
    bearish = output.count('bearish')
    if bullish > bearish:
        buy_signals.append(r['ts_code'])
    elif bearish > bullish:
        sell_signals.append(r['ts_code'])

print(f"买入: {buy_signals}, 卖出: {sell_signals}")

# Step 5: 推送
from src.utils.notifier import send_alert

title = f"🌅 早盘推送 {datetime.now().strftime('%m月%d日')}"
content = f"""
### 早盘选股 ({datetime.now().strftime('%Y-%m-%d')})

**Top 5 股票**:
"""

for i, p in enumerate(picks[:5]):
    content += f"{i+1}. **{p.get('name')}**({p.get('ts_code')}) Score={p.get('final_score', 0):.3f}\n"

content += f"""
---
### 🤖 AI大师分析

**买入信号**: {', '.join(buy_signals) if buy_signals else '无'}
**卖出信号**: {', '.join(sell_signals) if sell_signals else '无'}

---
*缓存数据日期: {best_file.split('_')[-1].replace('.json', '')}*
"""

print("\n发送钉钉...")
result = send_alert(title, content, "morning_push")
print(f"发送结果: {'OK' if result else 'FAIL'}")