#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时行情获取
优先 xtquant（国信iQuant），fallback akshare
"""
import time
from typing import Dict, List, Optional
from loguru import logger

_xt_client = None
_xt_port = None

# 进程级行情缓存，60秒内复用批量结果，避免逐只请求
_quote_cache: Dict[str, dict] = {}
_quote_cache_ts: float = 0.0
_CACHE_TTL: float = 60.0


def _try_connect_xtdata() -> Optional[object]:
    """尝试连接 xtquant 行情服务"""
    global _xt_client, _xt_port
    if _xt_client is not None:
        return _xt_client
    try:
        from xtquant import xtdata
        xtdata.enable_hello = False
        # 优先 58600（iQuant 主进程），fallback 58610（miniquote）
        for port in [58600, 58610]:
            try:
                cl = xtdata.connect(port=port)
                if cl and cl.is_connected():
                    # 验证能否拿到股票列表（不需认证的基础接口）
                    stocks = xtdata.get_stock_list_in_sector('沪深A股')
                    if stocks:
                        _xt_client = cl
                        _xt_port = port
                        logger.info(f"[RealtimeQuote] xtquant 连接成功 port={port}，股票池={len(stocks)}只")
                        return cl
            except Exception:
                continue
    except ImportError:
        pass
    return None


def get_realtime_quotes(codes: List[str]) -> Dict[str, dict]:
    """
    获取多只股票实时行情
    返回: {ts_code: {last_price, pct_chg, volume, amount, open, high, low, pre_close}}

    带60秒进程级缓存：同一进程内逐只查询时复用批量结果，不重复发起HTTP请求。
    """
    global _quote_cache, _quote_cache_ts

    now = time.time()
    missing = [c for c in codes if c not in _quote_cache]

    # 若所有请求均在缓存内且未过期，直接返回
    if not missing and (now - _quote_cache_ts) < _CACHE_TTL:
        return {c: _quote_cache[c] for c in codes if c in _quote_cache}

    # 需要刷新：只请求缺失或缓存过期的股票（过期时全量刷新）
    fetch_codes = codes if (now - _quote_cache_ts) >= _CACHE_TTL else missing

    # 1. 尝试 xtquant
    new_data: Dict[str, dict] = {}
    cl = _try_connect_xtdata()
    if cl:
        new_data = _get_xtquant_quotes(fetch_codes)

    # 2. fallback: 腾讯财经
    if not new_data:
        new_data = _get_tencent_quotes(fetch_codes)

    if new_data:
        _quote_cache.update(new_data)
        _quote_cache_ts = now

    return {c: _quote_cache[c] for c in codes if c in _quote_cache}


def _get_xtquant_quotes(codes: List[str]) -> Dict[str, dict]:
    """通过 xtquant 获取实时快照"""
    try:
        from xtquant import xtdata
        # 先订阅，触发数据推送
        for code in codes:
            try:
                xtdata.subscribe_quote(code, period='tick', count=-1)
            except Exception:
                pass
        time.sleep(0.5)

        quotes = xtdata.get_full_tick(codes)
        if not quotes:
            return {}

        result = {}
        for code, q in quotes.items():
            result[code] = {
                'last_price': q.get('lastPrice', 0),
                'pct_chg':    q.get('pctChg', 0),
                'volume':     q.get('volume', 0),
                'amount':     q.get('amount', 0),
                'open':       q.get('open', 0),
                'high':       q.get('high', 0),
                'low':        q.get('low', 0),
                'pre_close':  q.get('lastClose', 0),
                'source':     'xtquant',
            }
        return result
    except Exception as e:
        logger.debug(f"[RealtimeQuote] xtquant 行情失败: {e}")
        return {}


def _get_tencent_quotes(codes: List[str]) -> Dict[str, dict]:
    """通过腾讯财经接口获取实时行情（稳定无需认证）"""
    try:
        import requests, re

        def to_qq(ts_code: str) -> str:
            code, market = ts_code.split('.')
            return ('sh' if market == 'SH' else 'sz') + code

        qq_map = {to_qq(c): c for c in codes if '.' in c}
        if not qq_map:
            return {}

        url = 'https://qt.gtimg.cn/q=' + ','.join(qq_map.keys())
        session = requests.Session()
        session.proxies = {'http': '', 'https': ''}
        r = session.get(url, timeout=8)
        r.encoding = 'gbk'

        result = {}
        for line in r.text.split(';'):
            m = re.match(r'v_(\w+)="(.+)"', line.strip())
            if not m:
                continue
            qq_code = m.group(1)
            fields = m.group(2).split('~')
            ts_code = qq_map.get(qq_code)
            if not ts_code or len(fields) < 35:
                continue
            result[ts_code] = {
                'name':       fields[1],
                'last_price': float(fields[3] or 0),
                'pre_close':  float(fields[4] or 0),
                'open':       float(fields[5] or 0),
                'volume':     int(fields[6] or 0) * 100,   # 手→股
                'amount':     float(fields[37] or 0) * 10000 if len(fields) > 37 else 0,
                'high':       float(fields[33] or 0),
                'low':        float(fields[34] or 0),
                'pct_chg':    float(fields[32] or 0),
                'source':     'tencent',
            }
        return result
    except Exception as e:
        logger.warning(f"[RealtimeQuote] 腾讯行情失败: {e}")
        return {}


def get_latest_price(ts_code: str) -> float:
    """获取单只股票最新价"""
    quotes = get_realtime_quotes([ts_code])
    if ts_code in quotes:
        return float(quotes[ts_code].get('last_price', 0))
    return 0.0


def format_quotes_table(quotes: Dict[str, dict]) -> str:
    """格式化为可读表格"""
    if not quotes:
        return "（无行情数据）"
    lines = [f"{'代码':12s} {'最新价':>8s} {'涨跌幅':>8s} {'来源':>10s}"]
    lines.append("-" * 45)
    for code, q in sorted(quotes.items()):
        price = q.get('last_price', 0)
        pct   = q.get('pct_chg', 0)
        src   = q.get('source', '')
        sign  = '+' if pct >= 0 else ''
        lines.append(f"{code:12s} {price:>8.2f} {sign}{pct:>7.2f}% {src:>10s}")
    return "\n".join(lines)
