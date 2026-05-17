#!/usr/bin/env python3
"""
从RSSHub获取财经RSS，保存到news_cache
支持: 雪球、微博、韭研公社、华尔街见闻、财联社等
"""
import sys
import os
sys.path.insert(0, '.')

import requests
import xml.etree.ElementTree as ET
import hashlib
from datetime import datetime

RSHUB = "http://192.168.3.51:1200"

# 财经RSS源 - 完整配置
RSS_SOURCES = [
    # 雪球
    ("/xueqiu/today", "雪球-今日话题"),
    ("/xueqiu/hots", "雪球-热帖"),
    
    # 韭研公社
    ("/jiuyangongshe/community", "韭研公社-社群"),
    ("/jiuyangongshe/study_publish", "韭研公社-研报"),
    
    # 财经快讯
    ("/wallstreetcn/live/a-stock", "华尔街见闻-A股"),
    ("/cls/telegraph", "财联社-电报"),
    ("/cls/telegraph/red", "财联社-重要"),
    
    # 微博财经
    ("/weibo/user/1642088277?excludeRts=1&excludeReplies=1", "微博-财联社"),
    ("/weibo/user/1878948095?excludeRts=1&excludeReplies=1", "微博-华尔街见闻"),
    
    # 研报公告
    ("/eastmoney/report/stock", "东财-个股研报"),
    ("/jin10/1", "金十快讯-重要"),
]

def fetch_rss(path):
    """获取RSS"""
    try:
        r = requests.get(RSHUB + path, timeout=20)
        if r.status_code != 200:
            return []
        
        root = ET.fromstring(r.text)
        items = root.findall('.//item')
        
        results = []
        for item in items[:15]:
            title = item.find('title')
            link = item.find('link')
            pub = item.find('pubDate')
            
            if title is not None:
                results.append({
                    'title': title.text[:100] if title.text else '',
                    'url': link.text if link is not None and link.text else '',
                    'published': pub.text if pub is not None else '',
                })
        
        return results
    except Exception as e:
        print(f"  error: {e}")
        return []


def url_hash(url, title):
    # Use title + source as key to avoid dedup issues
    key = (title or '').strip()
    return hashlib.md5(key.encode('utf-8')).hexdigest()


def save_news(items, source):
    """保存到数据库"""
    if not items:
        return 0
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    count = 0
    
    from src.utils.db_utils import DBUtils
    
    # 先获取已有hash
    existing = DBUtils.query_df("SELECT url_hash FROM news_cache")
    existing_hashes = set(existing['url_hash'].tolist()) if not existing.empty else set()
    
    for item in items:
        title = item.get('title', '')
        url = item.get('url', '')
        h = url_hash(url, title)
        
        # 跳过已存在的
        if h in existing_hashes:
            continue
        
        try:
            DBUtils.execute("""
                INSERT IGNORE INTO news_cache
                (url_hash, title, source, url, published_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                h,
                title,
                source,
                url,
                item.get('published', '')[:19],
                now
            ))
            count += 1
            existing_hashes.add(h)  # 更新已存集合
        except:
            pass
    
    return count


def main():
    print("="*40)
    print("RSSHub财经订阅采集")
    print("="*40)
    
    total = 0
    
    for path, name in RSS_SOURCES:
        print(f"[{name}]...", end=" ")
        items = fetch_rss(path)
        
        if items:
            c = save_news(items, name)
            print(f"{len(items)} ({c} new)")
            total += c
        else:
            print("0")
    
    print(f"\n[OK] Saved {total} news items")
    return total


if __name__ == '__main__':
    main()