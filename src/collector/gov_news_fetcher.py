#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
政府机构新闻抓取器
抓取以下机构最近30天内的公告/新闻：
  央行/证监会/统计局/发改委/工信部/财政部/金监局/
  市场监管总局/商务部/能源局/科技部/国务院（部分）

存入 gov_news 表，并通过 LLM 提取政策信号（受影响板块、利多/利空）

注意：只抓公开页面，不登录、不绕过任何安全措施。
"""
import hashlib
import re
import sys
import os
from datetime import datetime, timedelta
from typing import List, Optional
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.utils.db_utils import DBUtils
from src.utils.network_utils import clear_proxy_env

clear_proxy_env()

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_TIMEOUT = 15


# ─────────────────────────────────────────────────────────────────────────────
#  数据源配置
#  selectors: CSS选择器列表（按优先级依次尝试），取第一个能抓到内容的
#  date_selectors: 日期字段选择器
# ─────────────────────────────────────────────────────────────────────────────
SOURCES = [
    {
        'key': 'gov',
        'name': '国务院',
        'policy_type': 'macro',
        'urls': [
            # 国务院常务会议 — 简单HTML列表
            'https://www.gov.cn/guowuyuan/gwyhysdq.htm',
            # 政策发布 — 政策文件列表
            'https://www.gov.cn/zhengce/zhengcewenjian/index.htm',
            # 新华社滚动（gov.cn同步）
            'https://www.gov.cn/xinwen/index.htm',
        ],
        'base_url': 'https://www.gov.cn',
        'item_selectors': [
            'ul.news_box li a',
            '.news_list li a',
            'ul li a[href*="content_"]',
            'ul li a[href*="/zhengce/"]',
            'ul li a[href*="/xinwen/"]',
            'ul li a[href*="/guowuyuan/"]',
        ],
        'date_selectors': ['span.date', 'span.time', '.list_date', 'em'],
    },
    {
        'key': 'pbc',
        'name': '中国人民银行',
        'policy_type': 'monetary',
        'urls': [
            'http://www.pbc.gov.cn/xinwen/index.html',
            'http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html',
        ],
        'base_url': 'http://www.pbc.gov.cn',
        'item_selectors': [
            '.newsbox li a',
            'ul.newsList li a',
            '.listContent li a',
            '.list li a',
            'ul li a[href*="pbc.gov.cn"]',
        ],
        'date_selectors': ['span.date', '.list_date', 'em', 'span.time'],
    },
    {
        'key': 'csrc',
        'name': '中国证监会',
        'policy_type': 'regulatory',
        'urls': [
            'http://www.csrc.gov.cn/csrc/c100028/zfxxgk_zdgk.shtml',
            'https://www.csrc.gov.cn/csrc/c100028/zfxxgk_zdgk.shtml',
        ],
        'base_url': 'http://www.csrc.gov.cn',
        'item_selectors': [
            '.list-box li a',
            '.news_list li a',
            'ul.list li a',
            '.listContent li a',
            'ul li a[href*="/csrc/"]',
        ],
        'date_selectors': ['span.date', '.list_date', 'em', 'span.time'],
    },
    {
        'key': 'stats',
        'name': '国家统计局',
        'policy_type': 'macro_data',
        'urls': [
            'http://www.stats.gov.cn/xxgk/sjfb/zxfb2020/',
            'https://www.stats.gov.cn/xxgk/sjfb/zxfb2020/',
        ],
        'base_url': 'http://www.stats.gov.cn',
        'item_selectors': [
            '.center_list li a',
            'ul.list li a',
            '.news_list li a',
            'ul li a[href*="stats.gov.cn"]',
        ],
        'date_selectors': ['span.date', '.list_date', 'em', 'span.time'],
    },
    {
        'key': 'ndrc',
        'name': '国家发展改革委',
        'policy_type': 'industrial',
        'urls': [
            'https://www.ndrc.gov.cn/xwdt/xwfb/',
            'https://www.ndrc.gov.cn/xwdt/tzgg/',
        ],
        'base_url': 'https://www.ndrc.gov.cn',
        'item_selectors': [
            '.u-list li a',
            'ul.list li a',
            '.news_list li a',
            '.listContent li a',
        ],
        'date_selectors': ['span.date', 'em', '.list_date'],
    },
    {
        'key': 'miit',
        'name': '工业和信息化部',
        'policy_type': 'industrial',
        'urls': [
            # 部长信箱/通知公告 — 简单HTML
            'https://www.miit.gov.cn/zwgk/zcwj/index.html',
            'https://www.miit.gov.cn/zwgk/tzgg/index.html',
        ],
        'base_url': 'https://www.miit.gov.cn',
        'item_selectors': [
            'ul.list li a',
            '.news_list li a',
            '.listContent li a',
            'ul li a[href*="miit.gov.cn"]',
        ],
        'date_selectors': ['span.date', 'em', '.list_date'],
    },
    {
        'key': 'mof',
        'name': '财政部',
        'policy_type': 'fiscal',
        'urls': [
            'http://www.mof.gov.cn/zhengwuxinxi/xinwenlianbo/',
        ],
        'base_url': 'http://www.mof.gov.cn',
        'item_selectors': [
            'ul.list li a',
            '.news_list li a',
            '.listContent li a',
            'ul li a[href*="mof.gov.cn"]',
        ],
        'date_selectors': ['span.date', 'em', '.list_date'],
    },
    {
        'key': 'nfra',
        'name': '国家金融监督管理总局',
        'policy_type': 'financial_regulation',
        'urls': [
            'https://www.nfra.gov.cn/cn/view/pages/ItemList.html?itemPId=929&itemId=4113',
            'https://www.nfra.gov.cn/cn/view/pages/ItemList.html',
        ],
        'base_url': 'https://www.nfra.gov.cn',
        'item_selectors': [
            '.list_box li a',
            'ul.list li a',
            '.news_list li a',
            'ul li a[href*="nfra.gov.cn"]',
        ],
        'date_selectors': ['span.date', 'em', 'span'],
    },
    {
        'key': 'samr',
        'name': '国家市场监管总局',
        'policy_type': 'market_regulation',
        'urls': [
            'https://www.samr.gov.cn/xw/zj/',
            'https://www.samr.gov.cn/zw/zcjd/',
        ],
        'base_url': 'https://www.samr.gov.cn',
        'item_selectors': [
            'ul.list li a',
            '.news_list li a',
            '.listContent li a',
            'ul li a[href*="samr.gov.cn"]',
            'ul li a[href*="/xw/"]',
            'ul li a[href*="/zw/"]',
        ],
        'date_selectors': ['span.date', 'em', '.list_date'],
    },
    {
        'key': 'mofcom',
        'name': '商务部',
        'policy_type': 'trade',
        'urls': [
            'http://www.mofcom.gov.cn/zcfb/',   # 政策文件（优先）
            'http://www.mofcom.gov.cn/xwfb/',   # 新闻发布
        ],
        'base_url': 'http://www.mofcom.gov.cn',
        'item_selectors': [
            'ul.list li a',
            '.news_list li a',
            'ul li a[href*="article/"]',
        ],
        'date_selectors': ['span.date', 'em', '.list_date', 'span.time'],
    },
    {
        'key': 'nea',
        'name': '国家能源局',
        'policy_type': 'energy',
        'urls': [
            'https://www.nea.gov.cn/',
        ],
        'base_url': 'https://www.nea.gov.cn',
        'item_selectors': [
            'ul.list li a',
            '.news_list li a',
            'ul li a[href*="nea.gov.cn"]',
            'ul li a[href*="/2026"]',
            'ul li a[href*="/2025"]',
        ],
        'date_selectors': ['span.date', 'em', '.list_date'],
    },
    {
        'key': 'most',
        'name': '科学技术部',
        'policy_type': 'tech_policy',
        'urls': [
            'http://www.most.gov.cn/kjbgz/',
            'http://www.most.gov.cn/tztg/',
        ],
        'base_url': 'http://www.most.gov.cn',
        'item_selectors': [
            'ul.list li a',
            '.news_list li a',
            'ul li a[href*="most.gov.cn"]',
        ],
        'date_selectors': ['span.date', 'em', '.list_date'],
    },
]


@dataclass
class GovNewsItem:
    source_key: str
    source_name: str
    policy_type: str
    title: str
    url: str = ''
    published_at: Optional[str] = None  # 'YYYY-MM-DD HH:MM:SS'


# ─────────────────────────────────────────────────────────────────────────────
#  建表
# ─────────────────────────────────────────────────────────────────────────────
def ensure_table():
    DBUtils.execute("""
        CREATE TABLE IF NOT EXISTS gov_news (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash     VARCHAR(64) UNIQUE,
            source_key   VARCHAR(20),
            source_name  VARCHAR(50),
            policy_type  VARCHAR(30),
            title        TEXT NOT NULL,
            url          TEXT DEFAULT '',
            published_at DATETIME,
            fetched_at   DATETIME,
            sector_tags  TEXT DEFAULT '',
            sentiment    VARCHAR(20) DEFAULT 'neutral',
            llm_summary  TEXT DEFAULT '',
            llm_processed TINYINT DEFAULT 0
        )
    """)
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_gov_news_fetched ON gov_news(fetched_at)",
        "CREATE INDEX IF NOT EXISTS idx_gov_news_source ON gov_news(source_key)",
        "CREATE INDEX IF NOT EXISTS idx_gov_news_llm ON gov_news(llm_processed)",
    ]:
        try:
            DBUtils.execute(idx_sql)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  抓取单个来源
# ─────────────────────────────────────────────────────────────────────────────
def _url_hash(url: str, title: str) -> str:
    key = url.strip() if url.strip() else title.strip()
    return hashlib.md5(key.encode('utf-8', errors='replace')).hexdigest()


def _parse_date(text: str) -> Optional[str]:
    """从文本中提取日期，返回 'YYYY-MM-DD HH:MM:SS' 或 None"""
    text = text.strip()
    patterns = [
        (r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})', '%Y-%m-%d %H:%M'),
        (r'(\d{4})/(\d{2})/(\d{2})', '%Y/%m/%d'),
        (r'(\d{4})-(\d{2})-(\d{2})', '%Y-%m-%d'),
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日', None),
    ]
    for pattern, fmt in patterns:
        m = re.search(pattern, text)
        if m:
            try:
                if fmt:
                    dt = datetime.strptime(m.group(0).strip(), fmt)
                else:
                    dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                continue
    return None


_MAX_AGE_DAYS = 30  # 只保留近N天内的条目


def _is_too_old(date_str: Optional[str], max_age_days: int = _MAX_AGE_DAYS) -> bool:
    """判断日期是否超过 max_age_days"""
    if not date_str:
        return False  # 无日期信息，保留（无法判断）
    try:
        dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
        return (datetime.now() - dt).days > max_age_days
    except Exception:
        return False


def fetch_source(src: dict, max_items: int = 30) -> List[GovNewsItem]:
    """抓取单个政府网站，返回 GovNewsItem 列表"""
    items = []
    session = requests.Session()
    session.headers.update(_HEADERS)

    for url in src['urls']:
        try:
            resp = session.get(url, timeout=_TIMEOUT, allow_redirects=True)
            resp.encoding = resp.apparent_encoding or 'utf-8'
            if resp.status_code != 200:
                logger.debug(f"[GovNews] {src['name']} {url} → HTTP {resp.status_code}")
                continue

            soup = BeautifulSoup(resp.text, 'html.parser')

            # 依次尝试各选择器，取第一个有结果的
            links = []
            for selector in src['item_selectors']:
                try:
                    found = soup.select(selector)
                    if found:
                        links = found
                        logger.debug(f"[GovNews] {src['name']} 命中选择器: {selector} ({len(found)}条)")
                        break
                except Exception:
                    continue

            if not links:
                # 最终兜底：抓所有 <a> 中包含 "通知|公告|意见|决定|办法|规定|政策" 的
                links = [a for a in soup.find_all('a', href=True)
                         if re.search(r'通知|公告|意见|决定|办法|规定|政策|数据|报告', a.get_text())]
                if links:
                    logger.debug(f"[GovNews] {src['name']} 关键词兜底抓到 {len(links)} 条")

            # href必须包含指定字符串（排除导航菜单等），可选配置
            href_must = src.get('href_must_contain', '')

            for a_tag in links[:max_items * 3]:  # 多取一些，过滤后再截断
                title = a_tag.get_text(strip=True)
                if not title or len(title) < 8:
                    continue
                # 排除纯菜单/导航标题（通常很短或为固定服务名）
                if len(title) <= 10 and not re.search(r'[年月日通知公告关于]', title):
                    continue
                href = a_tag.get('href', '')
                if href_must and href_must not in href:
                    continue
                if href.startswith('http'):
                    full_url = href
                elif href.startswith('//'):
                    full_url = 'https:' + href
                elif href.startswith('/'):
                    full_url = src['base_url'].rstrip('/') + href
                else:
                    full_url = src['base_url'].rstrip('/') + '/' + href

                # 尝试提取相邻日期节点
                published = None
                parent = a_tag.parent
                for date_sel in src.get('date_selectors', []):
                    try:
                        date_el = parent.select_one(date_sel)
                        if date_el:
                            published = _parse_date(date_el.get_text())
                            if published:
                                break
                    except Exception:
                        continue

                item = GovNewsItem(
                    source_key=src['key'],
                    source_name=src['name'],
                    policy_type=src['policy_type'],
                    title=title,
                    url=full_url,
                    published_at=published,
                )
                # 日期已知且超过30天的跳过
                if _is_too_old(published):
                    continue
                items.append(item)

            items = items[:max_items]  # 截断到 max_items
            if items:
                break  # 第一个URL成功就不再尝试备用URL

        except requests.exceptions.ConnectionError:
            logger.debug(f"[GovNews] {src['name']} 连接失败: {url}")
        except requests.exceptions.Timeout:
            logger.debug(f"[GovNews] {src['name']} 超时: {url}")
        except Exception as e:
            logger.debug(f"[GovNews] {src['name']} 抓取异常: {e}")

    return items


# ─────────────────────────────────────────────────────────────────────────────
#  存入数据库（去重）
# ─────────────────────────────────────────────────────────────────────────────
def save_items(items: List[GovNewsItem]) -> int:
    """存入 gov_news 表，已存在则跳过。返回新增条数"""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    inserted = 0
    for item in items:
        h = _url_hash(item.url, item.title)
        try:
            DBUtils.execute(
                """INSERT INTO gov_news
                   (url_hash, source_key, source_name, policy_type,
                    title, url, published_at, fetched_at, llm_processed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (h, item.source_key, item.source_name, item.policy_type,
                 item.title, item.url, item.published_at, now_str),
            )
            inserted += 1
        except Exception as e:
            err_str = str(e).lower()
            if 'unique' in err_str or 'duplicate' in err_str or '1062' in err_str:
                pass  # 正常去重
            else:
                logger.debug(f"[GovNews] 存储失败 [{item.title[:20]}]: {e}")
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
#  LLM 政策信号提取（批量处理未标注条目）
# ─────────────────────────────────────────────────────────────────────────────
def extract_policy_signals(max_batch: int = 20) -> int:
    """对未处理的 gov_news 条目批量调用 LLM 提取：受影响板块 + 利多/利空/中性
    返回处理条数
    """
    try:
        from src.utils.llm_router import LLMRouter
        router = LLMRouter()
        if not router.is_available():
            logger.debug("[GovNews] LLM不可用，跳过政策信号提取")
            return 0
    except Exception as e:
        logger.debug(f"[GovNews] LLMRouter初始化失败: {e}")
        return 0

    df = DBUtils.query_df(
        """SELECT id, source_name, policy_type, title
           FROM gov_news WHERE llm_processed = 0
           ORDER BY fetched_at DESC LIMIT ?""",
        (max_batch,)
    )
    if df.empty:
        return 0

    # 构建批量提示
    lines = []
    for _, row in df.iterrows():
        lines.append(f"[{row['id']}] [{row['source_name']}] {row['title']}")

    prompt = (
        "以下是来自中国政府官网的最新政策/公告标题，请对每条逐一输出：\n"
        "格式：ID | 受影响A股板块(2-4个，逗号分隔) | 情绪(利多/利空/中性) | 一句话政策信号(25字内)\n\n"
        "板块选项（尽量用以下标准名称）：银行、保险、证券、房地产、新能源、半导体、人工智能、\n"
        "消费、医药、军工、有色金属、钢铁煤炭、化工、基建、互联网、汽车、机器人、通用\n\n"
        + '\n'.join(lines) +
        "\n\n只输出表格行，无需其他说明。"
    )

    try:
        resp = router.analyze(prompt, max_tokens=800)
    except Exception as e:
        logger.warning(f"[GovNews] LLM调用失败: {e}")
        return 0

    if not resp:
        return 0

    # 解析响应
    processed = 0
    for line in resp.strip().split('\n'):
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 4:
            continue
        try:
            row_id = int(re.sub(r'\D', '', parts[0]))
            sectors = parts[1].strip()
            sentiment_raw = parts[2].strip()
            summary = parts[3].strip()
            sentiment = 'positive' if '利多' in sentiment_raw else (
                'negative' if '利空' in sentiment_raw else 'neutral'
            )
            DBUtils.execute(
                """UPDATE gov_news
                   SET sector_tags=?, sentiment=?, llm_summary=?, llm_processed=1
                   WHERE id=?""",
                (sectors, sentiment, summary, row_id)
            )
            processed += 1
        except Exception:
            continue

    logger.info(f"[GovNews] LLM政策信号提取完成，处理 {processed}/{len(df)} 条")
    return processed


# ─────────────────────────────────────────────────────────────────────────────
#  清理旧数据
# ─────────────────────────────────────────────────────────────────────────────
def cleanup_old(days: int = 30):
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    DBUtils.execute("DELETE FROM gov_news WHERE fetched_at < ?", (cutoff,))


# ─────────────────────────────────────────────────────────────────────────────
#  主入口：抓取 + 存储
# ─────────────────────────────────────────────────────────────────────────────
def fetch_all(source_keys: Optional[List[str]] = None, run_llm: bool = True) -> dict:
    """
    抓取所有（或指定）政府网站，存入数据库，可选运行LLM提取信号
    Args:
        source_keys: 只抓指定来源的 key 列表，None 表示全部
        run_llm: 是否在抓取后运行LLM信号提取
    Returns:
        {'total_fetched': N, 'inserted': N, 'llm_processed': N, 'sources': {...}}
    """
    ensure_table()

    sources = SOURCES if not source_keys else [s for s in SOURCES if s['key'] in source_keys]
    total_fetched = 0
    total_inserted = 0
    source_stats = {}

    for src in sources:
        logger.info(f"[GovNews] 抓取 {src['name']} ...")
        items = fetch_source(src)
        inserted = save_items(items) if items else 0
        total_fetched += len(items)
        total_inserted += inserted
        source_stats[src['key']] = {'fetched': len(items), 'inserted': inserted}
        logger.info(f"[GovNews]   {src['name']}: 抓到{len(items)}条，新增{inserted}条")

    llm_cnt = 0
    if run_llm and total_inserted > 0:
        llm_cnt = extract_policy_signals()

    cleanup_old(days=30)

    return {
        'total_fetched': total_fetched,
        'inserted': total_inserted,
        'llm_processed': llm_cnt,
        'sources': source_stats,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  快捷查询：供决策引擎读取
# ─────────────────────────────────────────────────────────────────────────────
def get_recent_signals(hours: float = 48, min_sentiment: str = None) -> list:
    """
    读取最近 N 小时内经LLM处理的政策信号，返回 list of dict
    Args:
        hours: 时间窗口
        min_sentiment: 若指定则只返回 'positive'/'negative'
    Returns:
        [{'source_name', 'title', 'sector_tags', 'sentiment', 'llm_summary', 'published_at'}]
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    sql = (
        "SELECT source_name, title, url, sector_tags, sentiment, llm_summary, published_at "
        "FROM gov_news WHERE fetched_at >= ? AND llm_processed = 1 "
    )
    params = [cutoff]
    if min_sentiment:
        sql += "AND sentiment = ? "
        params.append(min_sentiment)
    sql += "ORDER BY published_at DESC LIMIT 50"
    df = DBUtils.query_df(sql, tuple(params))
    if df.empty:
        return []
    return df.to_dict('records')
