#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时新闻抓取脚本
- 从财联社/东方财富/同花顺/华尔街见闻抓取最近8小时新闻
- 与自选股池做关键词映射，存入 news_cache 表
- 清理7天前的旧数据
- 建议每30分钟运行一次（交易时段），非交易时段每2小时一次

用法:
  python scripts/fetch_news.py          # 默认抓最近8小时
  python scripts/fetch_news.py --hours 4
"""
import sys
import os
import json
import re
import argparse
import hashlib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.utils.log_utils import init_logger
from src.utils.db_utils import DBUtils

logger = init_logger("fetch_news")


def _ensure_table():
    DBUtils.execute("""
        CREATE TABLE IF NOT EXISTS news_cache (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash    VARCHAR(64) UNIQUE,
            title       TEXT NOT NULL,
            summary     TEXT DEFAULT '',
            source      TEXT DEFAULT '',
            url         TEXT DEFAULT '',
            published_at DATETIME,
            fetched_at  DATETIME,
            matched_stocks TEXT DEFAULT '[]'
        )
    """)
    # 加索引（已存在时忽略）
    try:
        DBUtils.execute("CREATE INDEX IF NOT EXISTS idx_news_fetched ON news_cache(fetched_at)")
        DBUtils.execute("CREATE INDEX IF NOT EXISTS idx_news_published ON news_cache(published_at)")
    except Exception:
        pass


def _url_hash(url: str, title: str) -> str:
    """用 url 或 title 生成去重 key"""
    key = url.strip() if url.strip() else title.strip()
    return hashlib.md5(key.encode('utf-8', errors='replace')).hexdigest()


def _load_pool_stocks() -> list:
    """加载自选股池 + agent持仓，返回 [{ts_code, name, keywords}]"""
    import pandas as pd

    pool_df = pd.DataFrame()
    try:
        pool_df = DBUtils.query_df(
            "SELECT ts_code, company_name AS name FROM stock_pool WHERE is_active=1"
        )
    except Exception as e:
        logger.warning(f"[FetchNews] stock_pool 查询失败: {e}")

    try:
        pos_df = DBUtils.query_df(
            "SELECT ts_code, stock_name AS name FROM agent_sim_positions"
        )
        if not pos_df.empty:
            pool_df = pd.concat([pool_df, pos_df], ignore_index=True).drop_duplicates('ts_code')
    except Exception:
        pass

    stocks = []
    for _, row in pool_df.iterrows():
        ts_code = str(row['ts_code'])
        name = str(row.get('name') or '')
        code6 = ts_code.split('.')[0]
        short_name = re.sub(r'(股份|集团|控股|科技|有限公司|有限|公司|银行|证券|保险)$', '', name)
        keywords = list(dict.fromkeys([kw for kw in [name, short_name, code6] if len(kw) >= 2]))
        stocks.append({'ts_code': ts_code, 'name': name, 'keywords': keywords})
    return stocks


def _match_stocks(text: str, stocks: list) -> list:
    """返回文本中匹配到的股票列表 [{ts_code, name}]"""
    matched = []
    for s in stocks:
        if any(kw in text for kw in s['keywords']):
            matched.append({'ts_code': s['ts_code'], 'name': s['name']})
    return matched


def fetch_and_store(hours: float = 8):
    _ensure_table()

    from src.feeds.news_fetcher import NewsFetcher
    logger.info(f"[FetchNews] 开始抓取最近 {hours} 小时新闻...")

    fetcher = NewsFetcher()
    items = fetcher.fetch(hours=hours, limit_per_source=50)
    logger.info(f"[FetchNews] 抓到 {len(items)} 条原始新闻")

    stocks = _load_pool_stocks()
    logger.info(f"[FetchNews] 自选股池 {len(stocks)} 只")

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    inserted = 0
    updated = 0

    for item in items:
        url_hash = _url_hash(item.url or '', item.title)
        pub_str = ''
        if item.published:
            try:
                pub_str = item.published.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pub_str = str(item.published)[:19]

        text = (item.title or '') + ' ' + (item.summary or '')
        matched = _match_stocks(text, stocks)
        matched_json = json.dumps(matched, ensure_ascii=False)

        # 先查是否已存在
        existing = DBUtils.query_df(
            "SELECT id, matched_stocks FROM news_cache WHERE url_hash=?", (url_hash,)
        )
        if not existing.empty:
            # 如果匹配到新的股票，更新 matched_stocks（取并集）
            try:
                old_matched = json.loads(existing.iloc[0]['matched_stocks'] or '[]')
                old_codes = {m['ts_code'] for m in old_matched}
                for m in matched:
                    if m['ts_code'] not in old_codes:
                        old_matched.append(m)
                        old_codes.add(m['ts_code'])
                if len(old_matched) > len(json.loads(existing.iloc[0]['matched_stocks'] or '[]')):
                    DBUtils.execute(
                        "UPDATE news_cache SET matched_stocks=?, fetched_at=? WHERE url_hash=?",
                        (json.dumps(old_matched, ensure_ascii=False), now_str, url_hash)
                    )
                    updated += 1
            except Exception:
                pass
            continue

        try:
            DBUtils.execute(
                """INSERT INTO news_cache
                   (url_hash, title, summary, source, url, published_at, fetched_at, matched_stocks)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (url_hash,
                 item.title,
                 (item.summary or '')[:500],
                 item.source or '',
                 item.url or '',
                 pub_str,
                 now_str,
                 matched_json)
            )
            inserted += 1
        except Exception as e:
            logger.debug(f"[FetchNews] 插入失败: {e}")

    logger.info(f"[FetchNews] 新增 {inserted} 条，更新 {updated} 条")

    # 清理7天前旧数据
    cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    try:
        DBUtils.execute("DELETE FROM news_cache WHERE fetched_at < ?", (cutoff,))
        logger.info(f"[FetchNews] 已清理 7 天前旧数据（cutoff={cutoff}）")
    except Exception as e:
        logger.warning(f"[FetchNews] 清理旧数据失败: {e}")

    return inserted


def main():
    parser = argparse.ArgumentParser(description='定时抓取财经新闻存库')
    parser.add_argument('--hours', type=float, default=8, help='抓取最近N小时新闻（默认8）')
    args = parser.parse_args()
    fetch_and_store(hours=args.hours)


if __name__ == '__main__':
    main()
