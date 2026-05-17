#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个股新闻 & 公告抓取器

数据源：
  1. 东方财富个股新闻   akshare.stock_news_em(symbol)   — 最近20条新闻
  2. 东方财富个股公告   akshare.stock_notice_report      — 最近公告标题
  3. 泛市场快讯过滤     NewsFetcher 结果中包含股票名称/代码的条目

用法：
    from src.feeds.stock_news_fetcher import fetch_stock_news_batch
    news_map = fetch_stock_news_batch(['000001.SZ', '600519.SH'], hours=24)
    # news_map['000001.SZ'] → List[dict]  每条: {title, source, time, type}
"""

import time
from datetime import datetime, timedelta
from typing import Dict, List

from loguru import logger
from src.utils.network_utils import clear_proxy_env

clear_proxy_env()


def _strip_suffix(ts_code: str) -> str:
    """000001.SZ → 000001"""
    return ts_code.split('.')[0] if '.' in ts_code else ts_code


def _fetch_news_fallback(ts_code: str, max_items: int = 10) -> List[dict]:
    """使用 web_search 获取个股新闻作为 fallback"""
    try:
        from src.utils.web_search import search_stock_news
        code = _strip_suffix(ts_code)
        results = search_stock_news(code, max_results=max_items)
        items = []
        for r in results:
            items.append({
                'title': r.get('title', ''),
                'content': r.get('snippet', '')[:300],
                'source': r.get('source', 'web'),
                'time': r.get('date', '')[:16],
                'type': 'news',
            })
        return items
    except Exception as e:
        logger.debug(f"[StockNews] fallback 获取新闻失败: {e}")
        return []


def fetch_stock_news_em(ts_code: str, max_items: int = 10) -> List[dict]:
    """东方财富个股新闻（最近N条）"""
    symbol = _strip_suffix(ts_code)
    try:
        import akshare as ak
        try:
            df = ak.stock_news_em(symbol=symbol)
        except Exception as e:
            logger.debug(f"[StockNews] stock_news_em 失败，尝试替代方案: {e}")
            return _fetch_news_fallback(ts_code, max_items)
        if df is None or df.empty:
            return []
        items = []
        for _, row in df.head(max_items).iterrows():
            title   = str(row.get('新闻标题', '') or row.get('标题', '')).strip()
            content = str(row.get('新闻内容', '') or row.get('内容', '')).strip()
            pub_str = str(row.get('发布时间', '') or row.get('时间', '')).strip()
            source  = str(row.get('文章来源', '东方财富')).strip()
            if not title:
                continue
            items.append({
                'title':   title,
                'content': content[:300] if content != title else '',
                'source':  source,
                'time':    pub_str[:16],
                'type':    'news',
            })
        return items
    except Exception as e:
        logger.debug(f"[StockNews] 东方财富个股新闻 {ts_code} 失败: {e}")
        return []


def fetch_stock_notices_em(ts_code: str, max_items: int = 5) -> List[dict]:
    """东方财富个股公告（最近N条标题）"""
    symbol = _strip_suffix(ts_code)
    try:
        import akshare as ak
        try:
            df = ak.stock_notice_report(symbol=symbol)
        except Exception:
            logger.debug("[StockNews] stock_notice_report 失败，跳过公告")
            return []
        if df is None or df.empty:
            return []
        items = []
        for _, row in df.head(max_items).iterrows():
            title  = str(row.get('公告标题', '') or row.get('标题', '')).strip()
            pub_str = str(row.get('公告日期', '') or row.get('发布日期', '')).strip()
            if not title:
                continue
            items.append({
                'title':   title,
                'content': '',
                'source':  '公告',
                'time':    pub_str[:10],
                'type':    'notice',
            })
        return items
    except Exception as e:
        logger.debug(f"[StockNews] 公告 {ts_code} 失败: {e}")
        return []


def filter_general_news_by_stock(general_news: List, stock_name: str,
                                  ts_code: str, hours: float = 24) -> List[dict]:
    """
    从泛市场快讯中过滤出与指定股票相关的条目。
    匹配逻辑：新闻标题/内容包含股票名称或代码（去后缀）
    """
    code_bare = _strip_suffix(ts_code)
    # 股票名称取前4字（去掉"股份""有限"等后缀干扰）
    name_key = stock_name[:4] if len(stock_name) >= 4 else stock_name

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    items = []
    for news in general_news:
        # 时间过滤
        if news.published and news.published < cutoff:
            continue
        text = (news.title + ' ' + news.summary).lower()
        if name_key in text or code_bare in text:
            items.append({
                'title':   news.title,
                'content': news.summary[:200] if news.summary else '',
                'source':  news.source,
                'time':    news.published.strftime('%m-%d %H:%M') if news.published else '',
                'type':    'market_news',
            })
    return items[:8]


def fetch_stock_news_batch(positions: List[dict],
                            general_news: List = None,
                            max_news_per_stock: int = 8,
                            sleep_sec: float = 0.5) -> Dict[str, List[dict]]:
    """
    批量抓取多只持仓股票的新闻公告。

    Args:
        positions: [{'ts_code': '000001.SZ', 'name': '平安银行'}, ...]
        general_news: 已抓取的泛市场快讯列表（避免重复拉取），None则跳过
        max_news_per_stock: 每只股票最多N条新闻
        sleep_sec: 每只股票拉取间隔（避免频控）

    Returns:
        {ts_code: [news_dict, ...]}
    """
    result = {}
    total = len(positions)

    for i, pos in enumerate(positions):
        ts_code = pos.get('ts_code', '')
        name    = pos.get('name', ts_code)
        if not ts_code:
            continue

        logger.debug(f"[StockNews] 抓取 {name}({ts_code}) [{i+1}/{total}]")
        items = []

        # 1. 个股新闻（东方财富）
        news = fetch_stock_news_em(ts_code, max_items=max_news_per_stock)
        items.extend(news)

        # 2. 个股公告
        notices = fetch_stock_notices_em(ts_code, max_items=3)
        items.extend(notices)

        # 3. 泛市场快讯过滤
        if general_news:
            filtered = filter_general_news_by_stock(general_news, name, ts_code)
            # 避免与已有条目重复
            existing_titles = {x['title'][:20] for x in items}
            for n in filtered:
                if n['title'][:20] not in existing_titles:
                    items.append(n)

        result[ts_code] = items[:max_news_per_stock]

        if i < total - 1:
            time.sleep(sleep_sec)

    return result
