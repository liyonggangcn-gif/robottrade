#!/usr/bin/env python3
"""
雪球财经新闻直接抓取
"""
import requests
import hashlib
import sys
import os
sys.path.insert(0, '.')

from datetime import datetime

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Cookie': 'xq_a_token=',
    'Accept': '*/*'
}

def fetch_xueqiu():
    """抓取雪球财经"""
    items = []
    try:
        # 雪球财经最新
        url = 'https://xueqiu.com/ajax/feed/list'
        params = {
            'type': '68',  # 财经
            'page': 1,
            'size': 20
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        data = resp.json()
        
        if 'list' in data:
            for item in data['list']:
                user = item.get('user', {})
                text = item.get('text', '')
                if text:
                    items.append({
                        'title': text[:80],
                        'source': '雪球',
                        'url': f"https://xueqiu.com/status/{item.get('id', '')}"
                    })
    except Exception as e:
        print(f"  error: {e}")
    
    return items


def fetch_jrj():
    """金融界"""
    items = []
    try:
        url = "https://news.jrj.com.cn"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.encoding = 'utf-8'
        
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        for a in soup.select('div.main-gg a')[:15]:
            title = a.get_text().strip()
            if title and len(title) > 5:
                items.append({
                    'title': title[:80],
                    'source': '金融界',
                    'url': a.get('href', '')
                })
    except Exception as e:
        print(f"  error: {e}")
    
    return items


def save_news(items):
    """保存"""
    if not items:
        return 0
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    count = 0
    
    from src.utils.db_utils import DBUtils
    for item in items:
        key = (item.get('url', '') or item.get('title', '')).strip()
        h = hashlib.md5(key.encode('utf-8')).hexdigest()
        
        try:
            DBUtils.execute("""
                INSERT OR IGNORE INTO news_cache
                (url_hash, title, source, url, fetched_at)
                VALUES (?, ?, ?, ?, ?)
            """, (h, item.get('title', ''), item.get('source', ''), item.get('url', ''), now))
            count += 1
        except:
            pass
    
    return count


def main():
    print("="*40)
    print("雪球财经采集")
    print("="*40)
    
    all_items = []
    
    print("[1] 雪球...")
    items = fetch_xueqiu()
    all_items.extend(items)
    print(f"   {len(items)}")
    
    print("[2] 金融界...")
    items = fetch_jrj()
    all_items.extend(items)
    print(f"   {len(items)}")
    
    if all_items:
        c = save_news(all_items)
        print(f"[OK] saved {c}")
    else:
        print("[WARN] no news")
    
    return len(all_items)


if __name__ == '__main__':
    main()