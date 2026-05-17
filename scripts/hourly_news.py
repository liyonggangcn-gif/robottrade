#!/usr/bin/env python3
"""
每小时新闻汇总推送 - 实时抓取最新财经新闻
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from datetime import datetime, timedelta
from src.utils.notifier import DingTalkNotifier
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.feeds.news_fetcher import NewsFetcher

webhook = Config.get('notification.dingtalk.webhook')
secret = Config.get('notification.dingtalk.secret_word', '提醒')
notifier = DingTalkNotifier(webhook, secret_word=secret)

print("=" * 60)
print("  每小时新闻汇总推送")
print("=" * 60)

now = datetime.now()
print(f"时间: {now.strftime('%H:%M')}")

lines = [f"### 市场资讯汇总 {now.strftime('%H:%M')}\n"]

# 1. 抓取最新财经新闻
try:
    print("抓取最新财经新闻...")
    fetcher = NewsFetcher()
    news_items = fetcher.fetch(hours=2, limit_per_source=50)
    
    titles = []
    seen = set()
    for n in news_items:
        title = n.title.strip()
        if title and title not in seen and len(title) > 5:
            seen.add(title)
            titles.append(title)
    
    if titles:
        lines.append("\n#### 财经要闻")
        print(f"  获取到 {len(titles)} 条新闻")
        
        for title in titles[:8]:
            lines.append(f"- {title[:50]}")
    else:
        print("  无新闻")
        lines.append("\n暂无最新财经资讯")
except Exception as e:
    print(f"抓取失败: {e}")

# 2. 政府政策
try:
    print("获取政府政策...")
    cutoff = now - timedelta(hours=24)
    gov = DBUtils.query_df("""
        SELECT title, fetched_at 
        FROM gov_news 
        WHERE fetched_at >= %s
        ORDER BY fetched_at DESC 
        LIMIT 5
    """, (cutoff,))
    if not gov.empty:
        lines.append("\n#### 政策速递")
        for _, row in gov.iterrows():
            title = (row.get('title') or '')[:40]
            if title:
                lines.append(f"- {title}")
        print(f"  政策OK: {len(gov)}条")
except Exception as e:
    print(f"政策: {e}")

content = "\n".join(lines)
notifier.send_message(f"市场资讯 {now.strftime('%H:%M')}", content)
print("\n推送完成")
print(content[:600] + "..." if len(content) > 600 else content)