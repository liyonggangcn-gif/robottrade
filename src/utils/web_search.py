#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebSearch 工具：给 Agent 提供网页搜索能力
- search()         通用搜索（DuckDuckGo HTML + 东方财富搜索）
- search_news()    财经新闻搜索（东财/新浪财经）
- search_stock()   个股新闻快搜（东财股票新闻 API）
不依赖任何额外 API key，使用公开 HTTP 接口。
"""
import re
import time
from typing import List, Dict, Optional
import sys

from loguru import logger

# 禁用代理并设置递归深度限制
_NO_PROXY_SESSION = requests.Session()
_NO_PROXY_SESSION.proxies = {"http": "", "https": ""}
_NO_PROXY_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
})
_TIMEOUT = 8

# 递归深度保护
sys.setrecursionlimit(500)


def _get(url: str, params: dict = None, retry=1, **kwargs) -> Optional[requests.Response]:
    """带重试的GET请求"""
    for attempt in range(retry):
        try:
            r = _NO_PROXY_SESSION.get(url, params=params, timeout=_TIMEOUT, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt < retry - 1:
                time.sleep(1)
                continue
            logger.debug(f"[WebSearch] GET {url} failed: {e}")
            return None
    return None
    try:
        r = _NO_PROXY_SESSION.get(url, params=params, timeout=_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        logger.debug(f"[WebSearch] GET {url} failed: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# 1. 东方财富资讯搜索（财经新闻，无需 key）
# ──────────────────────────────────────────────────────────────
def _eastmoney_news_search(query: str, max_results: int = 8) -> List[Dict]:
    """东方财富全文搜索接口"""
    import json as _json
    cb = f"cb{int(time.time()*1000)}"
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    params = {
        "cb": cb,
        "param": _json.dumps({
            "uid": "", "keyword": query,
            "type": ["cmsArticle"],
            "count": max_results,
            "pageIndex": 1,
            "preTag": "", "postTag": ""
        }, ensure_ascii=False),
        "_": str(int(time.time() * 1000)),
    }
    r = _get(url, params=params)
    if r is None:
        return []
    try:
        text = r.text
        # 剥离 JSONP wrapper（动态 callback 名）
        m = re.search(r'\w+\((.*)\)\s*$', text, re.DOTALL)
        if not m:
            return []
        data = _json.loads(m.group(1))
        items = data.get("result", {}).get("cmsArticle", [])
        results = []
        for it in items:
            title = re.sub(r'<[^>]+>', '', it.get("Title") or it.get("title", ""))
            snippet = re.sub(r'<[^>]+>', '', it.get("Content") or it.get("content", ""))[:200]
            date = str(it.get("Date") or it.get("date", ""))
            url_ = it.get("Url") or it.get("url", "")
            if title:
                results.append({"title": title, "snippet": snippet,
                                 "date": date, "url": url_, "source": "eastmoney"})
        return results
    except Exception as e:
        logger.debug(f"[WebSearch] 东财搜索解析失败: {e}")
        return []


# ──────────────────────────────────────────────────────────────
# 2. 个股新闻（东方财富股票新闻 API）
# ──────────────────────────────────────────────────────────────
def _eastmoney_stock_news(stock_code: str, max_results: int = 10) -> List[Dict]:
    """东方财富个股新闻（按代码拉取）"""
    # 代码转换：000001.SZ → 1.000001
    code = stock_code.split('.')[0]
    market = "0" if stock_code.endswith(".SZ") else "1"
    url = "https://np-listapi.eastmoney.com/comm/web/getListInfo"
    params = {
        "cb": "cb",
        "client": "web",
        "type": "1",
        "mTypeAndCode": f"{market}.{code}",
        "pageSize": max_results,
        "pageIndex": "1",
        "callback": "cb",
    }
    # 带重试
    r = _get(url, params=params, retry=2)
    if r is None:
        return []
    try:
        text = r.text
        m = re.search(r'cb\((.*)\)', text, re.DOTALL)
        if not m:
            return []
        import json
        data = json.loads(m.group(1))
        items = data.get("data", {}).get("list", [])
        results = []
        for it in items:
            title = it.get("title", "")
            snippet = it.get("digest", "")[:200]
            date = it.get("publishDate", "")
            url_ = it.get("articleUrl", "")
            if title:
                results.append({"title": title, "snippet": snippet,
                                 "date": date, "url": url_, "source": "eastmoney_stock"})
        return results
    except Exception as e:
        logger.debug(f"[WebSearch] 东财个股新闻解析失败: {e}")
        return []


# ──────────────────────────────────────────────────────────────
# 3. DuckDuckGo 轻量搜索（通用，中英文皆可）
# ──────────────────────────────────────────────────────────────
def _ddg_search(query: str, max_results: int = 6) -> List[Dict]:
    """DuckDuckGo lite 无 JS 搜索，解析 HTML"""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    url = "https://lite.duckduckgo.com/lite/"
    try:
        r = _NO_PROXY_SESSION.post(
            url,
            data={"q": query, "kl": "cn-zh"},
            timeout=_TIMEOUT,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        if not r.ok:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        # DuckDuckGo lite 结果在 <a class="result-link"> 标签里
        for a in soup.select("a.result-link")[:max_results]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            # 摘要在下一个 <td class="result-snippet">
            parent_tr = a.find_parent("tr")
            snippet = ""
            if parent_tr:
                next_tr = parent_tr.find_next_sibling("tr")
                if next_tr:
                    snippet = next_tr.get_text(strip=True)[:200]
            if title:
                results.append({"title": title, "snippet": snippet,
                                 "url": href, "source": "duckduckgo"})
        return results
    except Exception as e:
        logger.debug(f"[WebSearch] DDG 搜索失败: {e}")
        return []


# ──────────────────────────────────────────────────────────────
# 公开接口
# ──────────────────────────────────────────────────────────────
def search(query: str, max_results: int = 8) -> List[Dict]:
    """
    通用搜索：优先东财（中文财经），fallback DuckDuckGo
    返回: [{title, snippet, url, date, source}, ...]
    """
    results = _eastmoney_news_search(query, max_results)
    if len(results) < 3:
        results += _ddg_search(query, max_results - len(results))
    return results[:max_results]


def search_stock_news(stock_code: str, stock_name: str = "",
                      max_results: int = 10) -> List[Dict]:
    """
    个股新闻搜索：优先用 akshare（stock_news_em），fallback 东财 HTTP API
    """
    # 优先使用 akshare（更稳定）
    try:
        from src.feeds.stock_news_fetcher import fetch_stock_news_em
        raw = fetch_stock_news_em(stock_code, max_items=max_results)
        if raw:
            return [{"title": r["title"], "snippet": r.get("content", "")[:200],
                     "date": r.get("time", ""), "url": "", "source": r.get("source", "eastmoney")}
                    for r in raw]
    except Exception as e:
        logger.debug(f"[WebSearch] akshare 个股新闻失败: {e}")

    # fallback: 东财 HTTP API
    results = _eastmoney_stock_news(stock_code, max_results)
    if len(results) < 3 and stock_name:
        results += _eastmoney_news_search(stock_name, max_results - len(results))
    return results[:max_results]


def search_news(topic: str, max_results: int = 8) -> List[Dict]:
    """财经主题新闻搜索（宏观/行业/政策）"""
    return search(topic, max_results)


def format_for_llm(results: List[Dict], max_chars: int = 1500) -> str:
    """将搜索结果格式化为 LLM 友好的文本"""
    if not results:
        return "（无搜索结果）"
    lines = []
    total = 0
    for i, r in enumerate(results, 1):
        date_str = f"[{r['date'][:10]}] " if r.get("date") else ""
        line = f"{i}. {date_str}{r['title']}"
        if r.get("snippet"):
            line += f"\n   {r['snippet'][:120]}"
        lines.append(line)
        total += len(line)
        if total > max_chars:
            break
    return "\n".join(lines)
