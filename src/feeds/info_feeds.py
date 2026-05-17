"""
InfoFeedAggregator: 聚合股票相关新闻信息源

支持 RSSHub 提供的财经/政务 RSS：
- 马斯克/特斯拉：东方财富搜索、财联社等
- 部委政策：中国人民银行、税务总局、统计局等

拉取结果可配合 TopicMapper 提取建议热点，供 EventDriver 或日报使用。
"""

import re
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from urllib.parse import quote

import feedparser
import requests

from src.utils.config_loader import Config


def _normalize_date(entry) -> Optional[datetime]:
    """从 feed 条目解析发布时间"""
    for key in ('published_parsed', 'updated_parsed', 'created_parsed'):
        parsed = getattr(entry, key, None)
        if parsed and len(parsed) >= 6:
            try:
                return datetime(*parsed[:6])
            except Exception:
                pass
    return None


def _sanitize_html(html: str) -> str:
    """简单去除 HTML 标签"""
    if not html:
        return ""
    return re.sub(r'<[^>]+>', '', html).strip()[:500]


class InfoFeedAggregator:
    """聚合配置中的 RSS 信息源，返回统一结构的条目列表"""

    def __init__(
        self,
        rsshub_base: str = None,
        feeds: List[dict] = None,
        policy_feeds: List[dict] = None,
        max_items_per_feed: int = 15,
        request_timeout: int = 15,
    ):
        """
        Args:
            rsshub_base: RSSHub 根 URL，如 https://rsshub.app
            feeds: 财经/人物类 feed 列表，每项 {name, url_path, keywords?(可选)}
            policy_feeds: 部委政策类 feed 列表，每项 {name, url_path}
            max_items_per_feed: 每个源最多取条数
            request_timeout: 请求超时秒数
        """
        cfg = Config.get('info_sources') or {}
        self.rsshub_base = (rsshub_base or cfg.get('rsshub_base') or 'https://rsshub.app').rstrip('/')
        self.feeds = feeds if feeds is not None else (cfg.get('feeds') or [])
        self.policy_feeds = policy_feeds if policy_feeds is not None else (cfg.get('policy_feeds') or [])
        self.max_items_per_feed = max_items_per_feed
        self.request_timeout = request_timeout

    def _feed_url(self, url_path: str) -> str:
        """拼出完整 RSS URL（路径中的中文等会按段编码）"""
        path = url_path.lstrip('/')
        # 仅对路径中可能含中文的最后一节编码（如 eastmoney/search/特斯拉）
        parts = path.split('/')
        encoded = [quote(p, safe='') for p in parts]
        return f"{self.rsshub_base}/{'/'.join(encoded)}"

    def _fetch_feed(self, url: str) -> feedparser.FeedParserDict:
        """拉取单路 RSS，使用 feedparser 解析。失败时返回空 feed 不抛错。"""
        try:
            resp = requests.get(url, timeout=self.request_timeout)
            resp.raise_for_status()
            return feedparser.parse(resp.content)
        except Exception as e:
            # 单源 403/超时等不影响其他源，返回空 feed
            return feedparser.parse('')

    def _entries_to_items(
        self,
        feed_name: str,
        feed_type: str,
        parsed: feedparser.FeedParserDict,
        keywords: List[str],
    ) -> List[Dict[str, Any]]:
        """将 feed 条目转为统一结构"""
        items = []
        entries = getattr(parsed, 'entries', [])[: self.max_items_per_feed]
        for entry in entries:
            title = getattr(entry, 'title', '') or ''
            link = getattr(entry, 'link', '') or getattr(entry, 'id', '')
            summary = _sanitize_html(getattr(entry, 'summary', '') or getattr(entry, 'description', '') or '')
            pub_dt = _normalize_date(entry)
            items.append({
                'title': title,
                'link': link,
                'summary': summary[:200] if summary else '',
                'source': feed_name,
                'source_type': feed_type,
                'published': pub_dt,
                'keywords': keywords,
            })
        return items

    def fetch_all(
        self,
        only_today: bool = False,
        keyword_filter: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        拉取所有配置的 feeds 和 policy_feeds，合并去重（按 link）。

        Args:
            only_today: 是否只保留今日发布的条目（按 published 判断）
            keyword_filter: 对 feeds 中配置了 keywords 的源，是否只保留标题命中关键词的条目（政策源不按关键词过滤）

        Returns:
            条目列表，每项含 title, link, summary, source, source_type, published, keywords
        """
        all_items = []
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        for item in self.feeds:
            name = item.get('name', '')
            path = item.get('url_path', '')
            keywords = item.get('keywords') or []
            if not path:
                continue
            url = self._feed_url(path)
            parsed = self._fetch_feed(url)
            if getattr(parsed, 'bozo', False) and not getattr(parsed, 'entries', []):
                continue
            entries = self._entries_to_items(name, 'finance', parsed, keywords)
            for e in entries:
                if keyword_filter and keywords and not any(k in (e['title'] + e.get('summary', '')) for k in keywords):
                    continue
                if only_today and e.get('published') and e['published'] < today_start:
                    continue
                all_items.append(e)
            time.sleep(0.3)

        for item in self.policy_feeds:
            name = item.get('name', '')
            path = item.get('url_path', '')
            if not path:
                continue
            url = self._feed_url(path)
            parsed = self._fetch_feed(url)
            if getattr(parsed, 'bozo', False) and not getattr(parsed, 'entries', []):
                continue
            entries = self._entries_to_items(name, 'policy', parsed, [])
            for e in entries:
                if only_today and e.get('published') and e['published'] < today_start:
                    continue
                all_items.append(e)
            time.sleep(0.3)

        # 按发布时间降序（无时间的放前面）
        all_items.sort(key=lambda x: (x.get('published') or datetime.min), reverse=True)
        return all_items
