#!/usr/bin/env python3
"""
采集AI相关板块及其成分股和新闻
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
from datetime import datetime, timedelta
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.utils.notifier import DingTalkNotifier

webhook = Config.get('notification.dingtalk.webhook')
secret = Config.get('notification.dingtalk.secret_word', '提醒')
notifier = DingTalkNotifier(webhook, secret_word=secret)

AI_KEYWORDS = [
    'AI', '人工智能', '大模型', 'AIGC', 'LLM', 'GPU', 'CPU', '芯片', '算力', '云',
    '光模块', 'CPO', '液冷', '存储', '封测', '先进封装', 'CoWoS', 'HBM',
    '大模型', '大语言模型', '垂类AI', 'AI芯片', 'AI服务器', '算力租赁', '智算'
]

def collect_ai_concepts():
    """从数据库获取AI相关板块"""
    df = DBUtils.query_df("SELECT DISTINCT concept_name FROM stock_concepts")
    ai_concepts = [c for c in df['concept_name'] if any(kw in c for kw in AI_KEYWORDS)]
    return list(set(ai_concepts))

def get_concept_stocks_ts(concept_names):
    """批量获取板块成分股"""
    if not concept_names:
        return set()
    
    placeholders = ','.join(['%s'] * len(concept_names))
    df = DBUtils.query_df(f"""
        SELECT DISTINCT ts_code
        FROM stock_concepts
        WHERE concept_name IN ({placeholders})
    """, tuple(concept_names))
    
    return set(df['ts_code']) if not df.empty else set()

def get_stock_names(ts_codes):
    """批量获取股票名称"""
    if not ts_codes:
        return {}
    placeholders = ','.join(['%s'] * len(ts_codes))
    df = DBUtils.query_df(f"""
        SELECT ts_code, name FROM stock_info WHERE ts_code IN ({placeholders})
    """, tuple(ts_codes))
    return dict(zip(df['ts_code'], df['name'])) if not df.empty else {}

def get_stock_news(ts_codes, hours=48):
    """获取个股新闻"""
    cutoff = datetime.now() - timedelta(hours=hours)
    news = DBUtils.query_df("""
        SELECT matched_stocks, title, fetched_at
        FROM news_cache
        WHERE fetched_at >= %s
        ORDER BY fetched_at DESC
    """, (cutoff,))
    
    matched = []
    for _, row in news.iterrows():
        try:
            ms = json.loads(row['matched_stocks']) if isinstance(row['matched_stocks'], str) else row['matched_stocks']
            for m in ms:
                if m.get('ts_code') in ts_codes:
                    matched.append({
                        'ts_code': m.get('ts_code'),
                        'name': m.get('name', ''),
                        'title': row['title'],
                        'time': str(row['fetched_at'])
                    })
                    break
        except:
            pass
    return matched

def run():
    print("=" * 60)
    print("  AI相关板块及个股新闻采集")
    print("=" * 60)
    
    ai_concepts = collect_ai_concepts()
    print(f"\n找到 {len(ai_concepts)} 个AI相关板块:")
    for c in ai_concepts:
        print(f"  - {c}")
    
    ts_codes = get_concept_stocks_ts(ai_concepts)
    stock_names = get_stock_names(ts_codes)
    
    print(f"\n共 {len(ts_codes)} 只AI相关股票")
    
    news = get_stock_news(ts_codes, hours=48)
    print(f"获取到 {len(news)} 条相关新闻")
    
    concept_counts = {}
    for concept in ai_concepts:
        df = DBUtils.query_df("""
            SELECT ts_code FROM stock_concepts WHERE concept_name = %s
        """, (concept,))
        concept_counts[concept] = len(df) if not df.empty else 0
    
    lines = [f"### AI相关板块资讯 ({datetime.now().strftime('%m-%d %H:%M')})\n"]
    lines.append(f"**板块数**: {len(ai_concepts)} | **个股数**: {len(ts_codes)} | **新闻数**: {len(news)}\n")
    
    lines.append("\n**板块成分统计(Top15)**\n")
    for concept, count in sorted(concept_counts.items(), key=lambda x: -x[1])[:15]:
        lines.append(f"- {concept}: {count}只")
    
    lines.append(f"\n**相关新闻({len(news)}条)**\n")
    for item in news[:30]:
        title = item['title'][:40]
        lines.append(f"- [{item['time'][-5:]}] {item['name'][:6]}: {title}")
    
    content = "\n".join(lines)
    notifier.send_message(f"AI板块资讯 {datetime.now().strftime('%H:%M')}", content)
    print("\n推送完成")
    print(content)
    
    return {
        'concepts': ai_concepts,
        'stocks': stock_names,
        'concept_counts': concept_counts,
        'news': news
    }

if __name__ == '__main__':
    run()