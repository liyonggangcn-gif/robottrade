#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多路财经快讯抓取器（AKShare + 华尔街见闻直接 API）

数据源（均无需代理，国内可直接访问）：
1. 财联社电报         akshare.stock_info_global_cls  — A股政策/事件
2. 东方财富快讯       akshare.stock_info_global_em   — 个股/板块热点（200条）
3. 新浪财经快讯       akshare.stock_info_global_sina — 全球宏观
4. 同花顺快讯         akshare.stock_info_global_ths  — A股新闻
5. 华尔街见闻 Live    wallstreetcn.com API           — 全球宏观/大宗商品
"""

import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from dataclasses import dataclass

import requests
from loguru import logger

from src.utils.config_loader import Config
from src.utils.network_utils import clear_proxy_env

clear_proxy_env()

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "application/json, text/plain, */*",
}
_TIMEOUT = 12


@dataclass
class NewsItem:
    title: str
    summary: str = ""
    source: str = ""
    url: str = ""
    published: Optional[datetime] = None

    def age_hours(self) -> float:
        if not self.published:
            return 0.0
        now = datetime.utcnow()
        pub = self.published
        if pub.tzinfo is not None:
            pub = pub.astimezone(timezone.utc).replace(tzinfo=None)
        return max(0.0, (now - pub).total_seconds() / 3600)

    def text(self) -> str:
        parts = [self.title]
        if self.summary and self.summary != self.title:
            parts.append(self.summary[:200])
        return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# 各数据源适配器
# ─────────────────────────────────────────────────────────────────────────────

def _parse_dt(dt_str: str) -> Optional[datetime]:
    """解析常见日期时间格式，返回 naive datetime（本地时间）"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(dt_str.strip(), fmt)
        except Exception:
            pass
    return None


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """将本地时间（中国 UTC+8）转为 UTC，用于统一比较"""
    if dt is None:
        return None
    # 中国标准时间 = UTC + 8h
    return dt - timedelta(hours=8)


def fetch_cls() -> List[NewsItem]:
    """财联社电报（akshare）"""
    try:
        import akshare as ak
        df = ak.stock_info_global_cls()
        items = []
        for _, row in df.iterrows():
            title = str(row.get("标题", "")).strip()
            content = str(row.get("内容", "")).strip()
            date_val = row.get("发布日期")
            time_val = row.get("发布时间")
            pub = None
            try:
                pub = datetime.combine(date_val, time_val) if date_val and time_val else None
                pub = _to_utc(pub)
            except Exception:
                pass
            items.append(NewsItem(
                title=title or content[:80],
                summary=content[:300] if content != title else "",
                source="财联社",
                published=pub,
            ))
        return items
    except Exception as e:
        logger.debug(f"[NewsFetcher] 财联社 失败: {e}")
        return []


def fetch_eastmoney() -> List[NewsItem]:
    """东方财富快讯（akshare，200条）"""
    try:
        import akshare as ak
        df = ak.stock_info_global_em()
        items = []
        for _, row in df.iterrows():
            title = str(row.get("标题", "")).strip()
            summary = str(row.get("摘要", "")).strip()
            pub_str = str(row.get("发布时间", "")).strip()
            url = str(row.get("链接", "")).strip()
            pub = _to_utc(_parse_dt(pub_str))
            items.append(NewsItem(
                title=title,
                summary=summary[:300] if summary != title else "",
                source="东方财富",
                url=url,
                published=pub,
            ))
        return items
    except Exception as e:
        logger.debug(f"[NewsFetcher] 东方财富 失败: {e}")
        return []


def fetch_sina() -> List[NewsItem]:
    """新浪财经快讯（akshare）"""
    try:
        import akshare as ak
        df = ak.stock_info_global_sina()
        items = []
        for _, row in df.iterrows():
            content = str(row.get("内容", "")).strip()
            pub_str = str(row.get("时间", "")).strip()
            pub = _to_utc(_parse_dt(pub_str))
            items.append(NewsItem(
                title=content[:100],
                summary=content[100:300] if len(content) > 100 else "",
                source="新浪财经",
                published=pub,
            ))
        return items
    except Exception as e:
        logger.debug(f"[NewsFetcher] 新浪财经 失败: {e}")
        return []


def fetch_ths() -> List[NewsItem]:
    """同花顺快讯（akshare）"""
    try:
        import akshare as ak
        df = ak.stock_info_global_ths()
        items = []
        for _, row in df.iterrows():
            title = str(row.get("标题", "")).strip()
            content = str(row.get("内容", "")).strip()
            pub_str = str(row.get("发布时间", "")).strip()
            url = str(row.get("链接", "")).strip()
            pub = _to_utc(_parse_dt(pub_str))
            items.append(NewsItem(
                title=title,
                summary=content[:300] if content != title else "",
                source="同花顺",
                url=url,
                published=pub,
            ))
        return items
    except Exception as e:
        logger.debug(f"[NewsFetcher] 同花顺 失败: {e}")
        return []


def fetch_wallstreetcn(limit: int = 30) -> List[NewsItem]:
    """华尔街见闻 Live 快讯（全球宏观/大宗商品，直接 API）"""
    url = (
        "https://api.wallstreetcn.com/apiv1/content/lives"
        f"?channel=global-channel&include=article&accept=article&limit={limit}"
    )
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        items = []
        for item in data.get("data", {}).get("items", [])[:limit]:
            content = item.get("content_text") or item.get("title") or ""
            title = content[:100].replace("\n", " ").strip()
            summary = content[100:300].replace("\n", " ").strip()
            created_at = item.get("created_at")
            pub = None
            if created_at:
                try:
                    # 华尔街见闻时间戳为 UTC
                    pub = datetime.utcfromtimestamp(int(created_at))
                except Exception:
                    pass
            items.append(NewsItem(
                title=title,
                summary=summary,
                source="华尔街见闻",
                published=pub,
            ))
        return items
    except Exception as e:
        logger.debug(f"[NewsFetcher] 华尔街见闻 失败: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 主聚合器
# ─────────────────────────────────────────────────────────────────────────────

class NewsFetcher:
    """
    多路财经快讯聚合器（全用国内可访问 API）

    用法::

        fetcher = NewsFetcher()
        news = fetcher.fetch(hours=4)
        for item in news:
            print(item.source, item.title)
    """

    def fetch(self, hours: float = 8, limit_per_source: int = 50) -> List[NewsItem]:
        """
        抓取各路新闻，过滤最近 N 小时内的条目，去重后按时间降序返回。

        Args:
            hours: 只保留最近 N 小时的新闻（0 = 不过滤）
            limit_per_source: 每源最多条数（目前由各源 API 决定，此参数用于截断）

        Returns:
            按时间降序的 NewsItem 列表
        """
        all_items: List[NewsItem] = []
        cutoff_utc = datetime.utcnow() - timedelta(hours=hours) if hours > 0 else None

        fetchers = [
            ("财联社",    fetch_cls),
            ("东方财富",  fetch_eastmoney),
            ("新浪财经",  fetch_sina),
            ("同花顺",    fetch_ths),
            ("华尔街见闻", lambda: fetch_wallstreetcn(limit_per_source)),
        ]

        for name, fn in fetchers:
            try:
                items = fn()
                # 截断
                items = items[:limit_per_source]
                logger.debug(f"[NewsFetcher] {name}: 抓取 {len(items)} 条")
                all_items.extend(items)
            except Exception as e:
                logger.warning(f"[NewsFetcher] {name} 失败: {e}")
            time.sleep(0.3)

        # 时间过滤（无时间的保留）
        if cutoff_utc:
            filtered = []
            for item in all_items:
                if item.published is None:
                    filtered.append(item)
                else:
                    pub = item.published
                    if pub.tzinfo is not None:
                        pub = pub.astimezone(timezone.utc).replace(tzinfo=None)
                    if pub >= cutoff_utc:
                        filtered.append(item)
            all_items = filtered

        # 按标题去重（保留首次出现）
        seen = set()
        unique = []
        for item in all_items:
            key = item.title.strip()[:50]
            if key and key not in seen:
                seen.add(key)
                unique.append(item)

        # 时间降序
        unique.sort(key=lambda x: x.published or datetime.min, reverse=True)
        logger.info(f"[NewsFetcher] 共抓取 {len(unique)} 条去重新闻（最近 {hours}h）")
        return unique
