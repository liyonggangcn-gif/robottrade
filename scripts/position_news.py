#!/usr/bin/env python3
"""
持仓个股资讯推送 - 从news_cache读取
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
from datetime import datetime, timedelta
from src.utils.notifier import DingTalkNotifier
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config

webhook = Config.get('notification.dingtalk.webhook')
secret = Config.get('notification.dingtalk.secret_word', '提醒')
notifier = DingTalkNotifier(webhook, secret_word=secret)

print("=" * 60)
print("  持仓个股资讯推送")
print("=" * 60)

now = datetime.now()
cutoff = now - timedelta(hours=24)

positions = DBUtils.query_df("SELECT ts_code, name, position_pct FROM positions ORDER BY position_pct DESC")
pcodes = set(positions['ts_code'])

print(f"持仓: {len(pcodes)}只")
print(f"时间: {now.strftime('%H:%M')}")

lines = [f"### 持仓个股资讯 {now.strftime('%m-%d %H:%M')}\n"]

if positions.empty:
    notifier.send_message("持仓资讯", "暂无持仓")
else:
    lines.append(f"**持仓({len(positions)}只)**\n")
    
    for _, row in positions.iterrows():
        lines.append(f"- {row['name']}: {row['position_pct']*100:.1f}%")
    
    news = DBUtils.query_df("""
        SELECT matched_stocks, title, source, fetched_at 
        FROM news_cache 
        WHERE fetched_at >= %s
        ORDER BY fetched_at DESC 
        LIMIT 50
    """, (cutoff,))
    
    matched = []
    if not news.empty:
        for _, row in news.iterrows():
            try:
                ms = json.loads(row['matched_stocks']) if isinstance(row['matched_stocks'], str) else row['matched_stocks']
                for m in ms:
                    if m.get('ts_code') in pcodes:
                        matched.append({
                            'ts_code': m.get('ts_code'),
                            'title': row['title'],
                            'time': str(row['fetched_at'])[-5:]
                        })
                        break
            except:
                pass
    
    lines.append(f"\n**近期资讯({len(matched)}条)**\n")
    for item in matched[:15]:
        lines.append(f"- [{item['time']}] {item['title'][:35]}")
    
    if len(matched) == 0:
        lines.append("\n无最新资讯")

content = "\n".join(lines)
notifier.send_message(f"持仓资讯 {now.strftime('%H:%M')}", content)
print("\n推送完成")
print(content)