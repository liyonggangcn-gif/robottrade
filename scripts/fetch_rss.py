#!/usr/bin/env python3
"""
从RSSHub订阅财经RSS，保存到news_cache
"""
import sys
import os
sys.path.insert(0, '.')

import requests
import hashlib
from datetime import datetime
import time

RSHUB_HOST = "http://192.168.3.51:1200"

# 财经RSS订阅源
RSS_SOURCES = [
    ("cls", "财联社电报", "https://www.cls.cn/nodeapi/updateFeedSubject?id=3&app=CailianpressApp"),
    ("10jqka", "同花顺", "https://www.10jqka.com.cn"),
    ("eastmoney", "东方财富", "https://news.eastmoney.com"),
    ("sina", "新浪财经", "https://finance.sina.com.cn"),
    ("qq", "腾讯财经", "https://finance.qq.com"),
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}


def fetch_rsshub(url):
    """通过RSSHub获取RSS"""
    try:
        # 通过RSSHub代理
        proxy_url = f"{RSHUB_HOST}/?source={url}"
        resp = requests.get(proxy_url, headers=HEADERS, timeout=15)
        return resp.text, resp.status_code
    except Exception as e:
        return None, str(e)


def fetch_direct(url, name):
    """直接获取"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.encoding = 'utf-8'
        
        # 尝试解析RSS
        items = []
        from bs4 import BeautifulSoup
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 提取新闻标题
        for a in soup.select('a[href*="stock"]')[:10]:
            title = a.get_text().strip()
            if title and len(title) > 5:
                items.append({
                    'title': title[:100],
                    'source': name,
                    'url': a.get('href', '')
                })
        
        # 也尝试从script提取
        for script in soup.find_all('script'):
            text = script.string or ''
            if 'title' in text.lower():
                # 简单提取
                pass
        
        return items
    except Exception as e:
        return []


def url_hash(url, title):
    key = url.strip() if url.strip() else title.strip()
    return hashlib.md5(key.encode('utf-8')).hexdigest()


def save_news(items, hours=24):
    """保存到news_cache"""
    if not items:
        return 0
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    count = 0
    
    from src.utils.db_utils import DBUtils
    for item in items:
        h = url_hash(item.get('url', ''), item.get('title', ''))
        
        try:
            DBUtils.execute("""
                INSERT OR IGNORE INTO news_cache
                (url_hash, title, source, url, fetched_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                h,
                item.get('title', ''),
                item.get('source', ''),
                item.get('url', ''),
                now
            ))
            count += 1
        except:
            pass
    
    return count


def main():
    print("=" * 40)
    print("RSSHub财经订阅采集")
    print("=" * 40)
    
    all_items = []
    
    # 使用已有的fetch_news采集（它调用AKShare，已有很多新闻）
    print("[1] 使用fetch_news.py已有的新闻源...")
    
    # 手动抓取财联社
    print("[2] 抓取财联社...")
    try:
        url = "https://www.cls.cn/nodeapi/updateFeedSubject?id=3&app=CailianpressApp"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()
        
        if 'data' in data:
            items = []
            for row in data['data'][:20]:
                items.append({
                    'title': f"{row.get('title', '')}",
                    'source': '财联社',
                    'url': f"https://www.cls.cn{row.get('abs_url', '')}"
                })
            all_items.extend(items)
            print(f"   财联社: {len(items)}")
    except Exception as e:
        print(f"   error: {e}")
    
    # 抓取东方财富
    print("[3] 抓取东方财富...")
    try:
        url = "https://np-anotice.eastmoney.com/EM_XRJG_jsb/html/index.html"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.encoding = 'utf-8'
        
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        items = []
        for a in soup.select('div.news_list a')[:10]:
            title = a.get_text().strip()
            if title:
                items.append({
                    'title': title[:80],
                    'source': '东方财富',
                    'url': a.get('href', '')
                })
        all_items.extend(items)
        print(f"   东方财富: {len(items)}")
    except Exception as e:
        print(f"   error: {e}")
    
    # 抓取华尔街见闻
    print("[4] 抓取华尔街见闻...")
    try:
        url = "https://api.wallstreetcn.com/apiv/content/lives"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()
        
        items = []
        for row in data.get('data', [])[:20]:
            items.append({
                'title': f"{row.get('title', '')}",
                'source': '华尔街见闻',
                'url': f"https://www.wallstreetcn.com/news/{row.get('id', '')}"
            })
        all_items.extend(items)
        print(f"   华尔街见闻: {len(items)}")
    except Exception as e:
        print(f"   error: {e}")
    
    # 保存
    if all_items:
        count = save_news(all_items)
        print(f"[OK] saved {count} news")
    else:
        print("[WARN] no news")
    
    return len(all_items)


if __name__ == '__main__':
    main()