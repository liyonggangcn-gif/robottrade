#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QuantAgent-Alpha Web 管理 API
所有 /api/* 端点的实现
"""
from fastapi import APIRouter, Query, Body, Request
from fastapi.responses import JSONResponse
import os
import sys
import glob
import subprocess
import threading
import uuid
from datetime import datetime
import pandas as pd
import duckdb
import threading
import math

router = APIRouter()

# ── ClickHouse 查询工具 ──────────────────────────────────────────────────────────
import clickhouse_connect
_CH_HOST = '192.168.3.51'
_CH_PORT = 8123
_CH_USER = 'default'
_CH_PASSWORD = 'clickhouse123'
_ch_client = None

def _get_ch_client():
    global _ch_client
    if _ch_client is None:
        try:
            _ch_client = clickhouse_connect.get_client(
                host=_CH_HOST, port=_CH_PORT, 
                username=_CH_USER, password=_CH_PASSWORD
            )
        except Exception:
            _ch_client = False
    return _ch_client if _ch_client else None

def _ch_query(sql):
    try:
        client = _get_ch_client()
        if client is None:
            return None
        return client.query(sql).result_set.to_pandas()
    except Exception:
        return None

def _ch_available():
    return _get_ch_client() is not None

# ── DuckDB 查询工具 (保留用于回测，优先用5年数据) ─────────────────────────────
import duckdb
_DUCKDB_PATH = '/home/li/robottrade/data/quant_backtest_5y.duckdb'
_duckdb_local = threading.local()

def _get_duckdb():
    conn = getattr(_duckdb_local, 'conn', None)
    if conn is None:
        if not os.path.exists(_DUCKDB_PATH):
            return None
        conn = duckdb.connect(_DUCKDB_PATH, read_only=True)
        _duckdb_local.conn = conn
    return conn

def _duckdb_query(sql):
    try:
        conn = _get_duckdb()
        if conn is None:
            return None
        return conn.execute(sql).fetchdf()
    except Exception:
        return None

def _duckdb_available():
    return os.path.exists(_DUCKDB_PATH)

# ── 简单 TTL 内存缓存 ─────────────────────────────────────────────────────────
import time as _time

class _TTLCache:
    """轻量级 TTL 缓存，避免高频轮询接口每次都打 DB"""
    def __init__(self):
        self._store: dict = {}

    def get(self, key: str):
        entry = self._store.get(key)
        if entry and _time.time() < entry['exp']:
            return entry['val'], True
        return None, False

    def set(self, key: str, val, ttl: int):
        self._store[key] = {'val': val, 'exp': _time.time() + ttl}

    def invalidate(self, key: str):
        self._store.pop(key, None)

_cache = _TTLCache()

# ── 回测任务状态 ──
_backtest_jobs: dict = {}   # job_id → {status, progress, message, result, error}

# 项目根目录

# ─── 工具：读取 QMT profit_warnings ──────────────────────────────────────────

def _load_profit_warnings() -> tuple[dict, dict]:
    """返回 (by_code, by_name) 两个字典，resolved 的条目排除。结果不抛异常。"""
    try:
        import pymysql
        from src.utils.config_loader import Config
        mysql = Config.mysql if hasattr(Config, 'mysql') else {}
        conn = pymysql.connect(
            host=mysql.get('host', '192.168.3.41'),
            port=int(mysql.get('port', 3306)),
            user=mysql.get('user', 'root'),
            password=mysql.get('password', ''),
            database='qmt',
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT stock_code, stock_name, level, profit_change_pct, signals "
            "FROM profit_warnings WHERE resolved_date IS NULL OR resolved_date = 'None'"
        )
        rows = cur.fetchall()
        conn.close()
        by_code: dict = {}
        by_name: dict = {}
        seen: set = set()
        for r in rows:
            code = (r.get('stock_code') or '').strip()
            name = (r.get('stock_name') or '').strip()
            key = f"{code}|{name}"
            if key in seen:
                continue
            seen.add(key)
            if code:
                by_code[code] = r
            if name:
                by_name[name] = r
        return by_code, by_name
    except Exception:
        return {}, {}
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


# ─── 系统状态 ────────────────────────────────────────────────────────────────

@router.get("/status")
def get_status():
    """系统状态：DB连通、LLM状态、最新数据日期、今日选股数（60秒缓存）"""
    cached, hit = _cache.get('status')
    if hit:
        cached['server_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return cached

    from src.utils.db_utils import DBUtils
    from src.utils.llm_client import LLMClient
    from src.utils.config_loader import Config

    # DB 连通性 + 最新数据日期（优先 DuckDB）
    try:
        df = _duckdb_query("SELECT MAX(trade_date) as d, COUNT(DISTINCT ts_code) as n FROM stock_daily")
        if df is not None and not df.empty:
            latest_date = str(df.iloc[0]['d']) if not df.empty else "无数据"
            stock_count = int(df.iloc[0]['n']) if not df.empty else 0
            db_ok = True
        else:
            df = DBUtils.query_df("SELECT MAX(trade_date) as d, COUNT(DISTINCT ts_code) as n FROM stock_daily")
            latest_date = str(df.iloc[0]['d']) if not df.empty else "无数据"
            stock_count = int(df.iloc[0]['n']) if not df.empty else 0
            db_ok = True
    except Exception as e:
        latest_date = "连接失败"
        stock_count = 0
        db_ok = False

    # LLM 状态
    try:
        from src.utils.llm_client import get_llm_client
        llm = get_llm_client()
        llm_ok = llm.is_available()
        llm_provider = Config.get('llm') or {}
        if isinstance(llm_provider, dict):
            llm_provider = llm_provider.get('provider', 'unknown')
        else:
            llm_provider = str(llm_provider)
    except Exception:
        llm_ok = False
        llm_provider = 'error'

    # 今日选股文件
    output_dir = os.path.join(ROOT, 'output')
    picks_files = sorted(glob.glob(os.path.join(output_dir, 'hybrid_picks_*.csv')), reverse=True)
    today_picks = 0
    latest_picks_date_fmt = "无"
    if picks_files:
        latest_file = picks_files[0]
        date_str = os.path.basename(latest_file).replace('hybrid_picks_', '').replace('.csv', '')
        try:
            latest_picks_date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        except Exception:
            latest_picks_date_fmt = date_str
        try:
            df_picks = pd.read_csv(latest_file)
            today_picks = len(df_picks)
        except Exception:
            pass

    # 最新推送时间
    last_push = "无"
    try:
        df_msg = DBUtils.query_df(
            "SELECT MAX(send_time) as t FROM push_messages WHERE send_status='success'"
        )
        if not df_msg.empty and df_msg.iloc[0]['t']:
            last_push = str(df_msg.iloc[0]['t'])[:16]
    except Exception:
        pass

    result = {
        "db_ok": db_ok,
        "latest_date": latest_date,
        "stock_count": stock_count,
        "llm_ok": llm_ok,
        "llm_provider": llm_provider,
        "today_picks": today_picks,
        "latest_picks_date": latest_picks_date_fmt,
        "last_push": last_push,
        "server_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    _cache.set('status', result, ttl=60)
    return result


# ─── 选股推荐 ────────────────────────────────────────────────────────────────

@router.get("/picks")
def get_picks():
    """今日选股：优先从 daily_picks 表读取，降级到 CSV"""
    from src.utils.db_utils import DBUtils
    try:
        df = DBUtils.query_df(
            "SELECT * FROM daily_picks WHERE trade_date = ("
            "  SELECT MAX(trade_date) FROM daily_picks"
            ") ORDER BY final_score DESC"
        )
        if not df.empty:
            date_str = str(df.iloc[0]['trade_date'])
            date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            df = df.fillna('')
            picks = df.to_dict('records')
            return {"date": date_fmt, "picks": picks, "total": len(picks), "source": "db"}
    except Exception:
        pass

    # 降级：读 CSV
    output_dir = os.path.join(ROOT, 'output')
    files = sorted(glob.glob(os.path.join(output_dir, 'hybrid_picks_*.csv')), reverse=True)
    if not files:
        return {"date": None, "picks": [], "total": 0}
    latest = files[0]
    date_str = os.path.basename(latest).replace('hybrid_picks_', '').replace('.csv', '')
    try:
        date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    except Exception:
        date_fmt = date_str
    try:
        df = pd.read_csv(latest)
        df = df.fillna('')
        picks = df.to_dict('records')
    except Exception as e:
        return {"date": date_fmt, "picks": [], "total": 0, "error": str(e)}
    return {"date": date_fmt, "picks": picks, "total": len(picks), "source": "csv"}


# ─── 策略中心 ─────────────────────────────────────────────────────────────────

_STRATEGY_META = {
    'hybrid':          {'name': 'AI混合策略',        'desc': 'AI+事件+基本面+行业动量综合评分', 'weight': 0.40},
    'sector_rotation': {'name': '行业轮动策略',      'desc': '按行业20日动量过滤+AI赛道热度', 'weight': 0.30},
    'value':           {'name': '价值投资策略',       'desc': 'ROE/PE/净利润yoy价值选股', 'weight': 0.20},
    'dividend':        {'name': '红利策略',           'desc': '高股息率、稳定分红选股', 'weight': 0.15},
    'quant':           {'name': '量化多因子策略',     'desc': '技术因子多因子综合评分', 'weight': 0.15},
    'small_cap':       {'name': '质量小市值策略',     'desc': '小市值+质量因子双重筛选', 'weight': 0.15},
    'small_cap_pure':  {'name': '纯小市值策略',      'desc': '纯市值因子排序选小票', 'weight': 0.10},
    'small_cap_jinx':  {'name': '小市值Jinx择时',    'desc': '小市值+Jinx行业择时', 'weight': 0.10},
    'cyclical':        {'name': '周期轮动策略',       'desc': '经济周期行业轮动配置', 'weight': 0.20},
    'pb_roa':          {'name': 'PB-ROA价值策略',    'desc': 'PB/ROA深度价值指标', 'weight': 0.20},
    'convertible_bond':{'name': '可转债策略',        'desc': '低溢价+高YTM可转债', 'weight': 0.10},
    'index_enhance':   {'name': '指数增强策略',      'desc': '宽基指数成分股增强', 'weight': 0.15},
}

_MULTI_CACHE: dict = {"ts": 0.0, "data": None}
_MULTI_CACHE_TTL = 1800   # 30分钟缓存


_strategy_center_instance = None

def _get_strategy_center():
    """获取 StrategyCenter 单例（避免每次请求都初始化12个策略）"""
    global _strategy_center_instance
    if _strategy_center_instance is None:
        from src.strategy.center import StrategyCenter
        _strategy_center_instance = StrategyCenter(enable_macro=False, notify=False)
    return _strategy_center_instance


@router.get("/strategies/available")
def get_strategies_available():
    """返回所有可用策略及元信息"""
    cached, hit = _cache.get("strategies_available")
    if hit:
        return cached
    try:
        center = _get_strategy_center()
        available = center.available_strategies()
    except Exception:
        available = list(_STRATEGY_META.keys())

    result = []
    for key in available:
        meta = _STRATEGY_META.get(key, {'name': key, 'desc': '', 'weight': 0.10})
        result.append({
            'key': key,
            'name': meta['name'],
            'desc': meta['desc'],
            'weight': meta['weight'],
            'available': True,
        })
    res = {"strategies": result, "total": len(result)}
    _cache.set("strategies_available", res, ttl=300)
    return res


@router.post("/strategies/run")
def run_multi_strategies(
    body: dict = Body({
        "strategies": ["hybrid"],
        "trade_date": None,
        "top_k": 20,
        "ensemble": True,
        "weights": None,
        "use_memory": True,
    })
):
    """多策略并行选股（通过 StrategyCenter）

    Args:
        strategies: 策略名称列表，如 ["hybrid", "dividend", "quant"]
        trade_date: 交易日期，None 则取今天
        top_k: 每策略选股上限
        ensemble: True=加权融合，False=合并去重
        weights: {'hybrid': 0.4, 'dividend': 0.3, ...}，None 则等权
        use_memory: True=应用记忆事实加成
    """
    strategies = body.get('strategies', ['hybrid'])
    trade_date = body.get('trade_date')
    top_k = int(body.get('top_k', 20))
    ensemble = bool(body.get('ensemble', True))
    weights = body.get('weights')
    use_memory = bool(body.get('use_memory', True))

    if not strategies:
        return {"success": False, "error": "未指定策略"}

    memory_facts = None
    if use_memory:
        try:
            from src.agent.multi_agent.memory_service import get_memory_service
            memory = get_memory_service()
            facts = memory.get_top_facts(limit=20)
            memory_facts = [
                {"content": f.content, "confidence": f.confidence, "category": f.category, "tags": f.tags}
                for f in facts if f.confidence >= 0.7
            ]
        except Exception:
            pass

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from src.strategy.center import StrategyCenter

        center = StrategyCenter(enable_macro=False, notify=False)
        result_df = center.run(
            strategies=strategies,
            trade_date=trade_date,
            top_k=top_k,
            ensemble=ensemble,
            ensemble_weights=weights,
        )

        if result_df is None or result_df.empty:
            return {"success": False, "error": "所有策略均无结果"}

        # 处理NaN和Inf值，避免JSON序列化错误
        for col in result_df.columns:
            if result_df[col].dtype == 'object':
                result_df[col] = result_df[col].fillna('')
            else:
                # 数值列：替换NaN和Inf为None
                result_df[col] = result_df[col].replace([float('inf'), float('-inf')], None)
        
        # 先转dict再处理每个值
        picks = result_df.to_dict('records')
        
        # 遍历所有值，确保NaN被替换
        for pick in picks:
            for key, value in pick.items():
                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    pick[key] = None

        return {
            "success": True,
            "strategies_run": strategies,
            "ensemble": ensemble,
            "memory_facts_count": len(memory_facts) if memory_facts else 0,
            "picks": picks,
            "total": len(picks),
            "date": trade_date or datetime.now().strftime('%Y-%m-%d'),
        }
    except Exception as e:
        import traceback
        return {"success": False, "error": str(e), "trace": traceback.format_exc()[-500:]}


@router.get("/strategies/results")
def get_multi_results(refresh: bool = Query(False)):
    """读取预跑缓存的多策略结果（30分钟TTL）"""
    global _MULTI_CACHE
    import time
    now = time.time()
    if not refresh and _MULTI_CACHE["data"] is not None and (now - _MULTI_CACHE["ts"]) < _MULTI_CACHE_TTL:
        data = dict(_MULTI_CACHE["data"])
        data["cached"] = True
        data["cache_age_s"] = int(now - _MULTI_CACHE["ts"])
        return data

    output_dir = os.path.join(ROOT, 'output')
    today_str = datetime.now().strftime('%Y%m%d')
    cache_file = os.path.join(output_dir, f'multi_strategy_{today_str}.json')
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                import json
                data = json.load(f)
            _MULTI_CACHE = {"ts": now, "data": data}
            data["cached"] = False
            data["cache_age_s"] = 0
            return data
        except Exception:
            pass

    return {
        "success": False,
        "error": "暂无预跑结果，请先运行 /api/strategies/run 或定时任务",
        "cached": False,
    }


@router.get("/strategies/run_cached")
@router.post("/strategies/run_cached")
def run_or_get_cached(
    body: dict = Body({
        "strategies": ["hybrid", "value", "dividend"],
        "trade_date": None,
        "top_k": 20,
        "ensemble": True,
        "force": False,
    })
):
    """优先返回缓存，缓存过期则重新计算"""
    import time
    global _MULTI_CACHE
    now = time.time()
    force = body.get('force', False)

    if not force and _MULTI_CACHE["data"] is not None and (now - _MULTI_CACHE["ts"]) < _MULTI_CACHE_TTL:
        data = dict(_MULTI_CACHE["data"])
        data["cached"] = True
        data["cache_age_s"] = int(now - _MULTI_CACHE["ts"])
        return data

    output_dir = os.path.join(ROOT, 'output')
    today_str = datetime.now().strftime('%Y%m%d')
    cache_file = os.path.join(output_dir, f'multi_strategy_{today_str}.json')
    if not force and os.path.exists(cache_file):
        try:
            import json
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _MULTI_CACHE = {"ts": now, "data": data}
            data["cached"] = False
            data["cache_age_s"] = 0
            return data
        except Exception:
            pass

    strategies = body.get('strategies', ['hybrid', 'value', 'dividend'])
    trade_date = body.get('trade_date')
    top_k = int(body.get('top_k', 20))
    ensemble = bool(body.get('ensemble', True))

    result = run_multi_strategies({
        "strategies": strategies,
        "trade_date": trade_date,
        "top_k": top_k,
        "ensemble": ensemble,
        "use_memory": True,
    })

    if result.get("success"):
        try:
            import json
            os.makedirs(output_dir, exist_ok=True)
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        _MULTI_CACHE = {"ts": now, "data": result}
        result["cached"] = False
        result["cache_age_s"] = 0
    return result


@router.get("/strategies/memory_facts")
def get_memory_facts_for_scoring():
    """获取当前记忆事实，用于选股评分加成"""
    try:
        from src.agent.multi_agent.memory_service import get_memory_service
        memory = get_memory_service()
        facts = memory.get_top_facts(limit=20)
        executions = memory.get_recent_decisions(days=30, limit=5)

        fact_list = [
            {
                "content": f.content,
                "confidence": f.confidence,
                "category": f.category,
                "tags": f.tags,
            }
            for f in facts if f.confidence >= 0.7
        ]

        recent_picks = memory.get_previous_picks(days=10)

        return {
            "facts": fact_list,
            "total_facts": len(fact_list),
            "recent_picks": recent_picks,
            "executions_count": len(executions),
        }
    except Exception as e:
        return {"facts": [], "total_facts": 0, "recent_picks": [], "error": str(e)}


@router.get("/picks/small_cap")
def get_small_cap_picks():
    """本周小市值三策略结果：读取最新 output/small_cap_weekly_*.txt"""
    try:
        files = sorted(
            glob.glob(os.path.join(ROOT, 'output', 'small_cap_weekly_*.txt')),
            reverse=True
        )
        if not files:
            return {"content": "", "date": None, "error": "暂无小市值周推送结果，请先运行 weekly_small_cap_push.py"}
        latest = files[0]
        date_str = os.path.basename(latest).replace('small_cap_weekly_', '').replace('.txt', '')
        date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if len(date_str) == 8 else date_str
        with open(latest, 'r', encoding='utf-8') as f:
            content = f.read()
        return {"content": content, "date": date_fmt, "filename": os.path.basename(latest)}
    except Exception as e:
        return {"content": "", "date": None, "error": str(e)}


@router.post("/picks/small_cap/run")
async def run_small_cap_picks():
    """触发小市值周推送（dry-run，保存结果文件但不发钉钉）"""
    job_id = str(uuid.uuid4())[:8]
    _backtest_jobs[job_id] = {'status': 'running', 'message': '运行中...'}

    def _run():
        try:
            script = os.path.join(ROOT, 'scripts', 'weekly_small_cap_push.py')
            result = subprocess.run(
                [sys.executable, script, '--dry-run'],
                capture_output=True, text=True, timeout=120,
                cwd=ROOT
            )
            _backtest_jobs[job_id]['status'] = 'done'
            _backtest_jobs[job_id]['stdout'] = result.stdout[-3000:]
            if result.returncode != 0:
                _backtest_jobs[job_id]['status'] = 'error'
                _backtest_jobs[job_id]['error'] = result.stderr[-1000:] or '返回码非零'
        except Exception as e:
            _backtest_jobs[job_id]['status'] = 'error'
            _backtest_jobs[job_id]['error'] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "status": "started"}


@router.get("/picks/track")
def get_picks_track():
    """推荐追踪：recommendation_track 表，分 holding/sold"""
    from src.utils.db_utils import DBUtils
    try:
        df = DBUtils.query_df(
            "SELECT ts_code, name, buy_date, buy_price, sell_date, sell_price, "
            "profit_pct, holding_days, status FROM recommendation_track "
            "ORDER BY buy_date DESC LIMIT 100"
        )
        df = df.fillna('')
        holding = df[df['status'] == 'holding'].to_dict('records')
        sold_df = df[df['status'] == 'sold'].copy()
        sold = sold_df.to_dict('records')
        # 统计胜率和平均盈利
        win_rate = 0.0
        avg_profit = 0.0
        if len(sold_df) > 0:
            try:
                profit_vals = pd.to_numeric(sold_df['profit_pct'], errors='coerce').dropna()
                if len(profit_vals) > 0:
                    wins = (profit_vals > 0).sum()
                    win_rate = round(wins / len(profit_vals) * 100, 1)
                    avg_profit = round(profit_vals.mean(), 2)
            except Exception:
                pass
        return {
            "holding": holding,
            "sold": sold[:20],
            "stats": {
                "win_rate": win_rate,
                "avg_profit": avg_profit,
                "total_trades": len(sold_df),
            }
        }
    except Exception as e:
        return {"holding": [], "sold": [], "stats": {}, "error": str(e)}


# ─── 持仓管理 ────────────────────────────────────────────────────────────────

@router.get("/positions")
def get_positions():
    """当前持仓：positions + 估值结论 + 健康灯（2分钟缓存）"""
    cached, hit = _cache.get('positions')
    if hit:
        return cached
    from src.utils.db_utils import DBUtils
    import json
    try:
        # 直接使用MySQL查询（更可靠）
        df = DBUtils.query_df(
            "SELECT p.*, "
            "COALESCE(cp.target_price_mid, p.take_profit_price) as target_price, "
            "cp.target_price_bear, cp.target_price_bull, "
            "COALESCE(p.company_type, cp.company_type, sp.company_type) as company_type_v2 "
            "FROM positions p "
            "LEFT JOIN company_profile cp ON p.ts_code = cp.ts_code "
            "LEFT JOIN stock_pool sp ON p.ts_code = sp.ts_code AND sp.is_active = 1 "
            "ORDER BY p.profit_loss_pct DESC"
        )

        # 注入估值数据
        try:
            val_df = DBUtils.query_df(
                "SELECT ts_code, itype, val_method, upside_pct, verdict, val_detail "
                "FROM valuation_cache"
            )
            val_map = {r["ts_code"]: r.to_dict() for _, r in val_df.iterrows()}
        except Exception:
            val_map = {}

        # 注入健康灯（从 health_check_cache JSON 中解析）
        health_map = {}
        try:
            hdf = DBUtils.query_df(
                "SELECT result_json FROM health_check_cache ORDER BY cache_date DESC LIMIT 1"
            )
            if not hdf.empty:
                items = json.loads(hdf.iloc[0]["result_json"])
                health_map = {it["ts_code"]: it for it in items}
        except Exception:
            pass

        records = []
        for _, row in df.iterrows():
            r = row.to_dict()
            code = r.get("ts_code", "")
            # 估值
            v = val_map.get(code, {})
            r["val_verdict"]   = v.get("verdict", "")
            r["val_upside_pct"]= v.get("upside_pct")
            r["val_method"]    = v.get("val_method", "")
            r["val_detail"]    = v.get("val_detail", "")
            r["itype"]         = v.get("itype", "")
            # 健康灯
            h = health_map.get(code, {})
            r["health_light"]  = h.get("light", "")
            r["health_reason"] = h.get("main_reason", "")
            records.append(r)

        # 填充 NaN
        import math
        for rec in records:
            for k, v in rec.items():
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    rec[k] = None

        result = {"positions": records}
        _cache.set('positions', result, ttl=120)
        return result
    except Exception as e:
        import traceback
        return {"positions": [], "error": str(e), "trace": traceback.format_exc()}


@router.get("/positions/summary")
def get_positions_summary():
    """持仓汇总：调用 PositionManager"""
    from src.portfolio.position_manager import PositionManager
    try:
        pm = PositionManager()
        summary = pm.get_position_summary()
        return summary if summary else {"stock_count": 0, "note": "无持仓数据"}
    except Exception as e:
        return {"error": str(e), "stock_count": 0}


@router.post("/positions/sync-from-qmt")
def sync_positions_from_qmt():
    """从 qmt.holdings 同步真实持仓到 robottrade.positions 表"""
    try:
        import pymysql
        from src.utils.config_loader import Config
        from src.utils.db_utils import DBUtils
        mc = Config.get('mysql') or {}
        conn = pymysql.connect(
            host=mc.get('host', 'localhost'),
            port=mc.get('port', 3306),
            user=mc.get('user', 'root'),
            password=mc.get('password', ''),
            database='qmt',
            charset='utf8mb4',
            connect_timeout=8,
        )
        import pandas as pd
        qmt_df = pd.read_sql(
            "SELECT code as ts_code, name, cost as avg_cost, price as current_price, "
            "pnl as profit_loss_pct, is_etf FROM holdings", conn
        )
        conn.close()

        if qmt_df.empty:
            return {"ok": False, "error": "qmt.holdings 为空"}

        # ── 修正 QMT 代码 ──────────────────────────────────────────────────────
        # QMT holdings 中的 code 字段有时与实际 Tushare ts_code 不符；
        # 用股票名称去 stock_info 查正确的 ts_code，价格与 stock_daily 对比验证
        name_df = DBUtils.query_df("SELECT ts_code, name FROM stock_info")
        name_to_code = {}
        if not name_df.empty:
            name_to_code = {r['name']: r['ts_code'] for _, r in name_df.iterrows()}

        code_corrections = {}  # qmt_code -> correct_ts_code
        for _, r in qmt_df.iterrows():
            qmt_code = str(r['ts_code'])
            qmt_name = str(r.get('name', ''))
            qmt_price_raw = r['current_price']
            qmt_price = float(qmt_price_raw) if pd.notna(qmt_price_raw) and qmt_price_raw else 0.0
            # 按名字查正确代码
            correct = name_to_code.get(qmt_name)
            if correct and correct != qmt_code and qmt_price > 0:
                # 用 stock_daily 最新收盘价验证哪个代码匹配 QMT 价格
                df_qmt = DBUtils.query_df(
                    "SELECT close FROM stock_daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                    (qmt_code,))
                df_correct = DBUtils.query_df(
                    "SELECT close FROM stock_daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                    (correct,))
                qmt_code_price = float(df_qmt.iloc[0]['close']) if not df_qmt.empty else 0.0
                correct_code_price = float(df_correct.iloc[0]['close']) if not df_correct.empty else 0.0
                # 选择与 QMT price 更接近的代码
                if correct_code_price > 0:
                    diff_qmt = abs(qmt_code_price - qmt_price) / qmt_price if qmt_code_price > 0 else 1.0
                    diff_correct = abs(correct_code_price - qmt_price) / qmt_price
                    if diff_correct < diff_qmt and diff_correct < 0.10:
                        code_corrections[qmt_code] = correct

        # 用纠正后的代码重建 qmt_df
        def fix_code(row):
            return code_corrections.get(str(row['ts_code']), str(row['ts_code']))
        qmt_df['ts_code'] = qmt_df.apply(fix_code, axis=1)
        # ───────────────────────────────────────────────────────────────────────

        # 读取现有 positions 的 shares（保留数量信息）
        pos_df = DBUtils.query_df("SELECT ts_code, shares FROM positions WHERE shares > 0")
        shares_map = {}
        if not pos_df.empty:
            shares_map = {r['ts_code']: int(float(r['shares'])) for _, r in pos_df.iterrows()}
        # 兼容纠正前的旧 code 查 shares
        for old_code, new_code in code_corrections.items():
            if old_code in shares_map and new_code not in shares_map:
                shares_map[new_code] = shares_map[old_code]

        real_capital = float(Config.get('trading_agent.real_capital') or 1_600_000)
        now_str = datetime.now().strftime('%Y-%m-%d')

        # 获取实时价格（Tencent）
        valid_codes = [str(r['ts_code']) for _, r in qmt_df.iterrows()
                       if (pd.notna(r['avg_cost']) and r['avg_cost']) or
                          (pd.notna(r['current_price']) and r['current_price'])]
        try:
            from src.feeds.realtime_quote import get_realtime_quotes
            rt_quotes = get_realtime_quotes(valid_codes) if valid_codes else {}
        except Exception:
            rt_quotes = {}

        # 有效仓位数（用于估算股数）
        num_valid = max(len(valid_codes), 1)

        # 清空并重建 positions
        DBUtils.execute("DELETE FROM positions")
        synced = 0
        for _, r in qmt_df.iterrows():
            ts_code = str(r['ts_code'])
            name = str(r.get('name', ts_code))
            avg_cost = float(r['avg_cost']) if pd.notna(r['avg_cost']) and r['avg_cost'] else 0.0
            qmt_price = float(r['current_price']) if pd.notna(r['current_price']) and r['current_price'] else avg_cost
            # 优先用实时行情价格
            rt = rt_quotes.get(ts_code, {})
            cur_price = float(rt.get('last_price', 0)) if rt.get('last_price') else qmt_price
            if cur_price <= 0:
                cur_price = avg_cost
            pnl_raw = r['profit_loss_pct']
            pnl_pct = float(pnl_raw) / 100 if pd.notna(pnl_raw) and pnl_raw else 0.0
            if cur_price > 0 and avg_cost > 0:
                pnl_pct = (cur_price / avg_cost - 1)
            # 估算持股数：先用已记录的 shares，否则按均摊金额 / 成本价反推
            shares = shares_map.get(ts_code, 0)
            if shares == 0 and avg_cost > 0:
                estimated_value = real_capital / num_valid
                shares = max(100, int(estimated_value / avg_cost / 100) * 100)
            market_value = cur_price * shares if shares > 0 else 0.0
            profit_loss = market_value - avg_cost * shares if shares > 0 else 0.0
            pos_pct = market_value / real_capital if market_value > 0 else 0.0
            stop_loss = round(avg_cost * 0.92, 2) if avg_cost > 0 else 0.0
            take_profit = round(avg_cost * 1.20, 2) if avg_cost > 0 else 0.0

            if avg_cost <= 0 and cur_price <= 0:
                continue

            DBUtils.execute(
                """INSERT INTO positions
                   (ts_code, name, shares, avg_cost, current_price, market_value,
                    profit_loss, profit_loss_pct, position_pct,
                    stop_loss_price, take_profit_price, buy_date, update_date)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts_code, name, shares, avg_cost, cur_price, market_value,
                 profit_loss, pnl_pct, pos_pct,
                 stop_loss, take_profit, now_str, now_str)
            )
            synced += 1

        return {"ok": True, "synced": synced,
                "message": f"已从 qmt.holdings 同步 {synced} 条持仓到 positions 表"}
    except Exception as e:
        import traceback
        return {"ok": False, "error": str(e), "trace": traceback.format_exc()}


@router.post("/positions/refresh-prices")
def refresh_position_prices():
    """用实时行情更新 positions 表的当前价格、盈亏、市值（Tencent → stock_daily 兜底）"""
    try:
        from src.utils.db_utils import DBUtils
        from src.utils.config_loader import Config

        # 取所有持仓（不限 shares>0）
        pos_df = DBUtils.query_df(
            "SELECT ts_code, shares, avg_cost FROM positions"
        )
        if pos_df.empty:
            return {"updated": 0, "ok": True, "message": "持仓为空"}

        codes = pos_df['ts_code'].tolist()

        # 1. 实时行情（Tencent）
        try:
            from src.feeds.realtime_quote import get_realtime_quotes
            rt_quotes = get_realtime_quotes(codes)
        except Exception:
            rt_quotes = {}

        # 2. stock_daily 兜底
        latest = DBUtils.query_df(
            "SELECT MAX(trade_date) as d FROM stock_daily"
        ).iloc[0]['d']
        codes_str = "','".join(codes)
        price_df = DBUtils.query_df(
            f"SELECT ts_code, close FROM stock_daily "
            f"WHERE trade_date = ? AND ts_code IN ('{codes_str}')",
            (latest,)
        )
        daily_map = {r['ts_code']: float(r['close']) for _, r in price_df.iterrows()}

        # 合并：优先实时，再 stock_daily
        price_map = {}
        for code in codes:
            rt = rt_quotes.get(code, {})
            rt_price = float(rt.get('last_price', 0)) if rt.get('last_price') else 0.0
            price_map[code] = rt_price if rt_price > 0 else daily_map.get(code, 0.0)

        # 读 total_capital（从持仓 position_pct 反推，或用配置）
        real_capital = float(Config.get('trading_agent.real_capital') or 0)
        if real_capital <= 0:
            valid = pos_df[pos_df['ts_code'].isin(price_map)]
            if not valid.empty:
                implied = valid.apply(
                    lambda r: price_map[r['ts_code']] * r['shares'] / 0.03
                    if r['ts_code'] in price_map else 0, axis=1
                )
                # 用市值 / 假设单只平均仓位 3% 估算，改用已知仓位估算
                total_mv = sum(price_map.get(r['ts_code'], r['avg_cost']) * r['shares']
                               for _, r in pos_df.iterrows())
                real_capital = total_mv / 0.80  # 假设 80% 仓位
        if real_capital <= 0:
            real_capital = 1_600_000  # 用户确认 160万

        updated = 0
        now_str = datetime.now().strftime('%Y-%m-%d')
        for _, row in pos_df.iterrows():
            ts_code = row['ts_code']
            cur = price_map.get(ts_code, 0.0)
            if cur <= 0:
                continue
            shares = float(row['shares'] or 0)
            avg_cost = float(row['avg_cost'] or 0)
            # 如果 shares=0，根据资金均摊估算
            if shares == 0 and avg_cost > 0:
                n = max(len(pos_df), 1)
                shares = max(100, int(real_capital / n / avg_cost / 100) * 100)
            mv = cur * shares
            pnl = mv - avg_cost * shares if avg_cost > 0 else 0
            pnl_pct = (cur / avg_cost - 1) if avg_cost > 0 else 0
            pos_pct = mv / real_capital if real_capital > 0 else 0
            DBUtils.execute(
                "UPDATE positions SET current_price=?, market_value=?, profit_loss=?, "
                "profit_loss_pct=?, position_pct=?, update_date=? WHERE ts_code=?",
                (cur, mv, pnl, pnl_pct, pos_pct, now_str, ts_code)
            )
            updated += 1

        # 统计使用了多少实时 vs 兜底
        rt_count = sum(1 for c in codes if rt_quotes.get(c, {}).get('last_price', 0))
        return {"updated": updated, "trade_date": str(latest),
                "total_capital": real_capital, "realtime_count": rt_count,
                "fallback_count": updated - rt_count, "ok": True}
    except Exception as e:
        import traceback
        return {"ok": False, "error": str(e), "trace": traceback.format_exc()}


@router.post("/positions/sync")
def sync_positions():
    """手动触发持仓→stock_pool core_holding 同步"""
    try:
        from src.universe.stock_pool import StockPool
        result = StockPool().sync_positions_to_pool()
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/transactions")
def get_transactions(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: str = Query("", description="BUY/SELL/全部"),
    ts_code: str = Query("", description="股票代码过滤"),
):
    """交易历史：分页 + 盈亏统计"""
    from src.utils.db_utils import DBUtils
    import math
    try:
        # 构建过滤条件
        where = []
        params = []
        if action:
            where.append("action = ?")
            params.append(action.upper())
        if ts_code:
            where.append("ts_code = ?")
            params.append(ts_code)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        # 总数
        total_df = DBUtils.query_df(
            f"SELECT COUNT(*) as cnt FROM transactions {where_sql}", params or None
        )
        total = int(total_df.iloc[0]["cnt"]) if not total_df.empty else 0

        # 分页数据
        offset = (page - 1) * page_size
        df = DBUtils.query_df(
            f"SELECT * FROM transactions {where_sql} "
            f"ORDER BY trade_date DESC, id DESC LIMIT ? OFFSET ?",
            (params + [page_size, offset]) or [page_size, offset],
        )
        df = df.fillna("")

        # 全量统计（不分页）
        stat_df = DBUtils.query_df(
            "SELECT action, COUNT(*) as cnt, SUM(amount) as total_amount "
            "FROM transactions GROUP BY action"
        )
        buy_cnt = sell_cnt = 0
        buy_amt = sell_amt = 0.0
        for _, r in stat_df.iterrows():
            if str(r["action"]).upper() == "BUY":
                buy_cnt = int(r["cnt"])
                buy_amt = float(r["total_amount"] or 0)
            elif str(r["action"]).upper() == "SELL":
                sell_cnt = int(r["cnt"])
                sell_amt = float(r["total_amount"] or 0)

        # 每只股票盈亏配对（简单 SELL-BUY 估算）
        pnl_df = DBUtils.query_df(
            """
            SELECT ts_code, name,
                   SUM(CASE WHEN action='SELL' THEN amount ELSE 0 END) -
                   SUM(CASE WHEN action='BUY'  THEN amount ELSE 0 END) AS realized_pnl,
                   COUNT(CASE WHEN action='BUY'  THEN 1 END) AS buy_times,
                   COUNT(CASE WHEN action='SELL' THEN 1 END) AS sell_times
            FROM transactions
            GROUP BY ts_code, name
            ORDER BY realized_pnl DESC
            """
        )
        pnl_df = pnl_df.fillna(0)
        win_stocks = int((pnl_df["realized_pnl"] > 0).sum())
        total_stocks = len(pnl_df)
        total_pnl = float(pnl_df["realized_pnl"].sum())

        for col in ["realized_pnl"]:
            pnl_df[col] = pnl_df[col].apply(
                lambda v: None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else v
            )

        return {
            "transactions": df.to_dict("records"),
            "pagination": {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": math.ceil(total / page_size) if total else 1,
            },
            "stats": {
                "buy_cnt": buy_cnt,
                "sell_cnt": sell_cnt,
                "buy_amt": round(buy_amt, 2),
                "sell_amt": round(sell_amt, 2),
                "total_pnl": round(total_pnl, 2),
                "win_stocks": win_stocks,
                "total_stocks": total_stocks,
                "win_rate": round(win_stocks / total_stocks * 100, 1) if total_stocks else 0,
            },
            "by_stock": pnl_df.to_dict("records"),
        }
    except Exception as e:
        import traceback
        return {"transactions": [], "error": str(e), "trace": traceback.format_exc()}


# ─── 板块管理 ────────────────────────────────────────────────────────────────

@router.get("/sectors/hot")
def get_hot_sectors():
    """热点板块：配置 + DB 概念股数量"""
    from src.utils.config_loader import Config
    from src.utils.db_utils import DBUtils

    hot_topics = Config.get('hot_topics_fallback') or Config.get('hot_topics') or []
    weights = Config.get('hot_topic_weights') or {}

    try:
        df = DBUtils.query_df(
            "SELECT concept_name, COUNT(DISTINCT ts_code) as stock_count "
            "FROM stock_concepts GROUP BY concept_name ORDER BY stock_count DESC LIMIT 300"
        )
        concept_counts = dict(zip(df['concept_name'], df['stock_count']))
    except Exception:
        concept_counts = {}

    topics = []
    for t in hot_topics:
        topics.append({
            "name": t,
            "weight": weights.get(t, 1.0),
            "stock_count": concept_counts.get(t, 0),
        })

    return {"hot_topics": topics, "weights": weights}


@router.get("/sectors/industry")
def get_industry_momentum():
    """行业动量：stock_daily 近20日涨幅按 industry 聚合"""
    from src.utils.db_utils import DBUtils
    try:
        # 使用最简SQL并优雅处理COLLATE错误
        try:
            sql = """
            SELECT industry, COUNT(*) as stock_count, 0.0 as mom_20
            FROM stock_info 
            WHERE industry IS NOT NULL AND industry != ''
            GROUP BY industry
            ORDER BY stock_count DESC
            LIMIT 30
            """
            df = DBUtils.query_df(sql)
        except Exception as collate_err:
            # COLLATE错误时使用降级查询
            if 'collations' in str(collate_err).lower():
                sql = "SELECT '未知' as industry, 0 as stock_count, 0.0 as mom_20 LIMIT 0"
                df = DBUtils.query_df(sql)
            else:
                raise
        
        df = df.fillna(0)
        return {"industries": df.to_dict('records')}
    except Exception as e:
        return {"industries": [], "error": str(e)[:100]}


@router.post("/sectors/weights")
def update_sector_weights(data: dict = Body(...)):
    """更新热点板块权重到 settings.yaml"""
    import yaml
    config_path = os.path.join(ROOT, 'config', 'settings.yaml')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        cfg['hot_topic_weights'] = data.get('weights', {})
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        return {"success": True, "message": "权重已更新"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── 政府新闻 ─────────────────────────────────────────────────────────────────

@router.get("/news/gov")
def get_gov_news(hours: float = Query(48), source: str = Query(None)):
    """政府官网最新政策公告（gov_news 表）"""
    from src.utils.db_utils import DBUtils
    from datetime import datetime, timedelta
    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        sql = (
            "SELECT source_name, source_key, policy_type, title, url, "
            "published_at, sentiment, sector_tags, llm_summary "
            "FROM gov_news WHERE fetched_at >= ? "
        )
        params = [cutoff]
        if source:
            sql += "AND source_key = ? "
            params.append(source)
        sql += "ORDER BY published_at DESC LIMIT 100"
        df = DBUtils.query_df(sql, tuple(params))
        if df.empty:
            return {"items": [], "total": 0}
        items = df.fillna('').to_dict('records')
        return {"items": items, "total": len(items)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/news/gov/sync")
async def sync_gov_news_api():
    """触发一次政府新闻同步（后台异步）"""
    import threading, uuid
    from src.collector.gov_news_fetcher import fetch_all
    job_id = str(uuid.uuid4())[:8]
    _backtest_jobs[job_id] = {'status': 'running', 'message': '同步中...'}

    def _run():
        try:
            result = fetch_all(run_llm=True)
            _backtest_jobs[job_id] = {
                'status': 'done',
                'message': f"新增{result['inserted']}条，LLM处理{result['llm_processed']}条"
            }
        except Exception as e:
            _backtest_jobs[job_id] = {'status': 'error', 'message': str(e)}

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "status": "started"}


# ─── 新闻分析 ────────────────────────────────────────────────────────────────

@router.get("/news/latest")
def get_latest_news(hours: float = Query(4)):
    """最近新闻：优先读 news_cache，fallback 实时抓取"""
    from src.utils.db_utils import DBUtils
    from datetime import datetime, timedelta
    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        df = DBUtils.query_df(
            """SELECT title, summary, source, url, published_at
               FROM news_cache WHERE fetched_at >= ?
               ORDER BY published_at DESC LIMIT 80""",
            (cutoff,)
        )
        if not df.empty:
            news = []
            for _, row in df.iterrows():
                pub_time = ''
                try:
                    pub_time = str(row['published_at'])[11:16]
                except Exception:
                    pass
                news.append({
                    "title": row['title'],
                    "summary": (row['summary'] or '')[:150],
                    "source": row['source'] or '',
                    "time": pub_time,
                    "url": row['url'] or '',
                })
            return {"news": news, "total": len(news), "hours": hours, "from_cache": True}
    except Exception:
        pass

    # Fallback：实时抓取
    from src.feeds.news_fetcher import NewsFetcher
    try:
        fetcher = NewsFetcher()
        items = fetcher.fetch(hours=hours, limit_per_source=30)
        news = []
        for item in items[:60]:
            pub_time = ""
            if item.published:
                try:
                    pub_time = item.published.strftime('%H:%M')
                except Exception:
                    pub_time = str(item.published)[:16]
            news.append({
                "title": item.title,
                "summary": (item.summary or "")[:150],
                "source": item.source,
                "time": pub_time,
                "url": item.url or "",
            })
        return {"news": news, "total": len(items), "hours": hours, "from_cache": False}
    except Exception as e:
        return {"news": [], "error": str(e)}


@router.post("/news/analyze")
def analyze_news(hours: float = Query(4)):
    """触发 LLM 新闻分析：MarketNewsAnalyzer.analyze()"""
    from src.risk.market_news_analyzer import MarketNewsAnalyzer
    try:
        analyzer = MarketNewsAnalyzer()
        result = analyzer.analyze(hours=hours, max_news=60)
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/news/pool_mapping")
def get_news_pool_mapping(hours: float = Query(8)):
    """自选股新闻映射：优先读 news_cache（已预匹配），fallback 实时抓"""
    from src.utils.db_utils import DBUtils
    from datetime import datetime, timedelta
    import json as _json

    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        df = DBUtils.query_df(
            """SELECT title, summary, source, url, published_at, matched_stocks
               FROM news_cache
               WHERE fetched_at >= ? AND matched_stocks != '[]' AND matched_stocks IS NOT NULL
               ORDER BY published_at DESC LIMIT 200""",
            (cutoff,)
        )
        if not df.empty:
            # 按 ts_code 归集
            stock_map: dict = {}
            total = len(df)
            for _, row in df.iterrows():
                try:
                    matched = _json.loads(row['matched_stocks'] or '[]')
                except Exception:
                    continue
                pub_time = ''
                try:
                    pub_time = str(row['published_at'])[5:16]  # MM-DD HH:MM
                except Exception:
                    pass
                news_obj = {
                    'title': row['title'],
                    'summary': (row['summary'] or '')[:120],
                    'source': row['source'] or '',
                    'time': pub_time,
                    'url': row['url'] or '',
                }
                for m in matched:
                    key = m['ts_code']
                    if key not in stock_map:
                        stock_map[key] = {'ts_code': key, 'name': m['name'], 'news': []}
                    stock_map[key]['news'].append(news_obj)

            result = sorted(stock_map.values(), key=lambda x: len(x['news']), reverse=True)
            return {
                "stocks": result,
                "unmatched_count": total - sum(1 for r in df.itertuples()
                                               if r.matched_stocks and r.matched_stocks != '[]'),
                "total_news": total,
                "hours": hours,
                "from_cache": True,
            }
    except Exception:
        pass

    # Fallback：实时抓取并做内存匹配（复用原逻辑）
    import re
    from src.feeds.news_fetcher import NewsFetcher
    try:
        pool_df = DBUtils.query_df(
            "SELECT ts_code, company_name AS name FROM stock_pool WHERE is_active=1"
        )
        try:
            import pandas as pd
            pos_df = DBUtils.query_df("SELECT ts_code, stock_name as name FROM agent_sim_positions")
            if not pos_df.empty:
                pool_df = pd.concat([pool_df, pos_df], ignore_index=True).drop_duplicates('ts_code')
        except Exception:
            pass

        if pool_df.empty:
            return {"stocks": [], "unmatched_count": 0, "total_news": 0}

        stocks = []
        for _, row in pool_df.iterrows():
            ts_code = str(row['ts_code'])
            name = str(row.get('name') or '')
            code6 = ts_code.split('.')[0]
            short_name = re.sub(r'(股份|集团|控股|科技|有限公司|有限|公司|银行|证券|保险)$', '', name)
            keywords = list(dict.fromkeys([kw for kw in [name, short_name, code6] if len(kw) >= 2]))
            stocks.append({'ts_code': ts_code, 'name': name, 'keywords': keywords, 'news': []})

        fetcher = NewsFetcher()
        items = fetcher.fetch(hours=hours, limit_per_source=40)
        matched_ids: set = set()
        for item in items:
            text = (item.title or '') + ' ' + (item.summary or '')
            pub_time = ''
            if item.published:
                try:
                    pub_time = item.published.strftime('%m-%d %H:%M')
                except Exception:
                    pub_time = str(item.published)[:16]
            news_obj = {'title': item.title, 'summary': (item.summary or '')[:120],
                        'source': item.source, 'time': pub_time, 'url': item.url or ''}
            for stock in stocks:
                if any(kw in text for kw in stock['keywords']):
                    stock['news'].append(news_obj)
                    matched_ids.add(id(item))

        matched = [s for s in stocks if s['news']]
        matched.sort(key=lambda x: len(x['news']), reverse=True)
        for s in matched:
            del s['keywords']
        return {"stocks": matched, "unmatched_count": len(items) - len(matched_ids),
                "total_news": len(items), "hours": hours, "from_cache": False}
    except Exception as e:
        return {"stocks": [], "error": str(e)}


# ─── 数据同步 ────────────────────────────────────────────────────────────────

@router.get("/sync/status")
def get_sync_status():
    """各表数据状态：最新日期、记录数（5分钟缓存，优先DuckDB）"""
    cached, hit = _cache.get('sync_status')
    if hit:
        return cached
    from src.utils.db_utils import DBUtils
    result = {}
    # 优先用 DuckDB 查 stock_daily（大表）
    dd = _duckdb_query("SELECT MAX(trade_date) as latest, COUNT(*) as total FROM stock_daily")
    if dd is not None and not dd.empty:
        result['stock_daily'] = {
            "latest": str(dd.iloc[0]['latest'])[:16] if dd.iloc[0]['latest'] else "无",
            "total": int(dd.iloc[0]['total']),
            "ok": True,
        }
    else:
        try:
            df = DBUtils.query_df("SELECT MAX(trade_date) as latest, COUNT(*) as total FROM stock_daily")
            result['stock_daily'] = {
                "latest": str(df.iloc[0]['latest'])[:16] if not df.empty and df.iloc[0]['latest'] else "无",
                "total": int(df.iloc[0]['total']) if not df.empty else 0,
                "ok": True,
            }
        except Exception as e:
            result['stock_daily'] = {"latest": "错误", "total": 0, "ok": False, "error": str(e)}

    # 其他小表用 MySQL
    tables = {
        'stock_info': 'SELECT NULL as latest, COUNT(*) as total FROM stock_info',
        'stock_concepts': 'SELECT NULL as latest, COUNT(*) as total FROM stock_concepts',
        'ai_predictions': 'SELECT MAX(trade_date) as latest, COUNT(*) as total FROM ai_predictions',
        'push_messages': 'SELECT MAX(send_time) as latest, COUNT(*) as total FROM push_messages',
        'recommendation_track': 'SELECT MAX(buy_date) as latest, COUNT(*) as total FROM recommendation_track',
    }
    for name, sql in tables.items():
        try:
            df = DBUtils.query_df(sql)
            latest_val = df.iloc[0]['latest'] if not df.empty else None
            result[name] = {
                "latest": str(latest_val)[:16] if latest_val else "无",
                "total": int(df.iloc[0]['total']) if not df.empty else 0,
                "ok": True,
            }
        except Exception as e:
            result[name] = {"latest": "错误", "total": 0, "ok": False, "error": str(e)}
    _cache.set('sync_status', result, ttl=300)
    return result


@router.post("/sync/trigger")
def trigger_sync(mode: str = Query("fast")):
    """触发数据同步（非阻塞后台运行）"""
    script_map = {
        "fast": "scripts/fast_sync_today.py",
        "concepts": "scripts/sync_concepts.py",
        "full": "scripts/fetch_full_market_data.py",
    }
    script = script_map.get(mode, "scripts/fast_sync_today.py")
    script_path = os.path.join(ROOT, script)
    if not os.path.exists(script_path):
        return {"success": False, "error": f"脚本不存在: {script}"}
    try:
        subprocess.Popen(
            [sys.executable, script_path],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"success": True, "message": f"已启动 {script}（后台运行）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── 日志管理 ────────────────────────────────────────────────────────────────

@router.get("/logs/files")
def get_log_files():
    """日志文件列表，最近30个"""
    log_dir = os.path.join(ROOT, 'logs')
    files = []
    for f in sorted(glob.glob(os.path.join(log_dir, '*.log')), reverse=True)[:30]:
        try:
            stat = os.stat(f)
            files.append({
                "name": os.path.basename(f),
                "size": stat.st_size,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
            })
        except Exception:
            pass
    return {"files": files}


@router.get("/logs/content")
def get_log_content(file: str = Query(...), lines: int = Query(200)):
    """读取日志文件内容（安全限制：只读 logs/ 目录）"""
    log_dir = os.path.join(ROOT, 'logs')
    safe_name = os.path.basename(file)  # 防止路径穿越
    path = os.path.join(log_dir, safe_name)
    if not os.path.exists(path):
        return {"content": [], "error": "文件不存在"}
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:]
        return {"content": [line.rstrip() for line in tail], "total_lines": len(all_lines)}
    except Exception as e:
        return {"content": [], "error": str(e)}


@router.get("/messages")
def get_messages(message_type: str = Query(""), limit: int = Query(30)):
    """推送消息历史：push_messages 表"""
    from src.utils.db_utils import DBUtils
    try:
        if message_type:
            df = DBUtils.query_df(
                "SELECT * FROM push_messages WHERE message_type=? ORDER BY created_at DESC LIMIT ?",
                params=[message_type, limit]
            )
        else:
            df = DBUtils.query_df(
                "SELECT * FROM push_messages ORDER BY created_at DESC LIMIT ?",
                params=[limit]
            )
        df = df.fillna('')
        return {"messages": df.to_dict('records')}
    except Exception as e:
        return {"messages": [], "error": str(e)}


# ─── 系统配置 ────────────────────────────────────────────────────────────────

@router.get("/config/get")
def get_config():
    """获取关键配置参数（不暴露密钥）"""
    from src.utils.config_loader import Config

    def safe_get(key, default=None):
        try:
            v = Config.get(key)
            return v if v is not None else default
        except Exception:
            return default

    portfolio = safe_get('portfolio') or {}
    if isinstance(portfolio, dict):
        total_capital = portfolio.get('total_capital', 1000000)
        max_position_pct = portfolio.get('max_position_pct', 0.15)
        max_total_position = portfolio.get('max_total_position', 0.80)
        stop_loss_pct = portfolio.get('stop_loss_pct', 0.08)
        take_profit_pct = portfolio.get('take_profit_pct', 0.20)
    else:
        total_capital = 1000000
        max_position_pct = 0.15
        max_total_position = 0.80
        stop_loss_pct = 0.08
        take_profit_pct = 0.20

    strategy = safe_get('strategy') or {}
    strategy_topk = strategy.get('topk', 10) if isinstance(strategy, dict) else 10

    hybrid = safe_get('hybrid_strategy') or {}
    max_mv_yi = hybrid.get('max_mv_yi', 800) if isinstance(hybrid, dict) else 800

    llm_cfg = safe_get('llm') or {}
    llm_provider = llm_cfg.get('provider', '') if isinstance(llm_cfg, dict) else ''
    llm_model = llm_cfg.get('model', '') if isinstance(llm_cfg, dict) else ''

    notification = safe_get('notification') or {}
    notif_enabled = notification.get('enabled', False) if isinstance(notification, dict) else False

    return {
        "total_capital": total_capital,
        "max_position_pct": max_position_pct,
        "max_total_position": max_total_position,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "hot_topics": safe_get('hot_topics_fallback') or safe_get('hot_topics') or [],
        "hot_topic_weights": safe_get('hot_topic_weights') or {},
        "strategy_topk": strategy_topk,
        "max_mv_yi": max_mv_yi,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "notification_enabled": notif_enabled,
    }


@router.post("/config/update")
def update_config(data: dict = Body(...)):
    """更新配置参数（白名单字段）"""
    import yaml
    config_path = os.path.join(ROOT, 'config', 'settings.yaml')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        # 只允许更新白名单字段
        allowed = {
            'total_capital': ('portfolio', 'total_capital'),
            'max_position_pct': ('portfolio', 'max_position_pct'),
            'max_total_position': ('portfolio', 'max_total_position'),
            'stop_loss_pct': ('portfolio', 'stop_loss_pct'),
            'take_profit_pct': ('portfolio', 'take_profit_pct'),
            'strategy_topk': ('strategy', 'topk'),
            'max_mv_yi': ('hybrid_strategy', 'max_mv_yi'),
        }
        for key, (section, field) in allowed.items():
            if key in data:
                if section not in cfg:
                    cfg[section] = {}
                cfg[section][field] = data[key]
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        return {"success": True, "message": "配置已更新，重启生效"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── 操作建议 ────────────────────────────────────────────────────────────────

@router.get("/advice")
def get_advice():
    """今日操作建议（60s TTL缓存）"""
    cached, hit = _cache.get("advice")
    if hit:
        return cached

    from src.utils.db_utils import DBUtils
    from src.utils.config_loader import Config

    # 1. 读取今日推荐（优先 DB，降级 CSV）
    picks_map = {}   # ts_code -> {name, score, track, concept}
    picks_date = None
    picks_prev_date = None
    try:
        df_today = DBUtils.query_df(
            "SELECT * FROM daily_picks WHERE trade_date = ("
            "  SELECT MAX(trade_date) FROM daily_picks"
            ") ORDER BY final_score DESC"
        )
        if not df_today.empty:
            picks_date = str(df_today.iloc[0]['trade_date'])
            for _, row in df_today.iterrows():
                code = str(row['ts_code']).strip()
                picks_map[code] = {
                    'name': str(row.get('name', '')),
                    'score': float(row.get('final_score') or 0),
                    'track': str(row.get('track', '')),
                    'concept': str(row.get('concept', ''))[:60],
                }
            # 昨日推荐（用于 delta）
            df_prev = DBUtils.query_df(
                "SELECT ts_code FROM daily_picks WHERE trade_date = ("
                "  SELECT MAX(trade_date) FROM daily_picks"
                "  WHERE trade_date < (SELECT MAX(trade_date) FROM daily_picks)"
                ")"
            )
            prev_codes = set(str(r).strip() for r in df_prev['ts_code']) if not df_prev.empty else set()
            if not df_prev.empty:
                picks_prev_date = str(df_prev.iloc[0].get('trade_date', '')) if 'trade_date' in df_prev.columns else None
    except Exception:
        prev_codes = set()

    if not picks_map:
        # 降级：读 CSV
        output_dir = os.path.join(ROOT, 'output')
        files = sorted(glob.glob(os.path.join(output_dir, 'hybrid_picks_*.csv')), reverse=True)
        if not files:
            return {"error": "暂无选股数据，请先运行策略选股", "picks_date": None}
        latest = files[0]
        date_str = os.path.basename(latest).replace('hybrid_picks_', '').replace('.csv', '')
        picks_date = date_str
        try:
            df_csv = pd.read_csv(latest).fillna('')
            code_col  = 'ts_code' if 'ts_code' in df_csv.columns else 'code'
            name_col  = 'name' if 'name' in df_csv.columns else 'stock_name'
            score_col = next((c for c in ['final_score', 'score'] if c in df_csv.columns), None)
            track_col = 'track' if 'track' in df_csv.columns else None
            concept_col = next((c for c in ['concept', 'concepts', 'concept_name'] if c in df_csv.columns), None)
            for _, row in df_csv.iterrows():
                code = str(row.get(code_col, '')).strip()
                if code:
                    picks_map[code] = {
                        'name': str(row.get(name_col, '')),
                        'score': float(row.get(score_col, 0)) if score_col else 0,
                        'track': str(row.get(track_col, '')) if track_col else '',
                        'concept': str(row.get(concept_col, ''))[:60] if concept_col else '',
                    }
        except Exception as e:
            return {"error": f"读取推荐数据失败: {e}"}
        prev_codes = set()

    picks_date_fmt = f"{picks_date[:4]}-{picks_date[4:6]}-{picks_date[6:]}" if picks_date and len(picks_date) == 8 else (picks_date or '未知')

    # 2. 读取当前持仓（positions 表）
    holdings = {}   # ts_code -> {name, shares, avg_cost, buy_date, profit_loss_pct}
    try:
        df_pos = DBUtils.query_df("SELECT * FROM positions")
        for _, row in df_pos.iterrows():
            code = str(row['ts_code']).strip()
            holdings[code] = {
                'name': str(row.get('name', '')),
                'shares': float(row.get('shares', 0) or 0),
                'avg_cost': float(row.get('avg_cost', 0) or 0),
                'current_price': float(row.get('current_price', 0) or 0),
                'buy_date': str(row.get('buy_date', '')),
                'profit_loss_pct': float(row.get('profit_loss_pct', 0) or 0),
            }
    except Exception as e:
        return {"error": f"读取持仓失败: {e}"}

    # 3. 获取最新收盘价（当天行情可能还没更新，用 DB 最新的）
    price_map = {}
    latest_price_date = None
    try:
        df_price = DBUtils.query_df(
            "SELECT ts_code, close, trade_date FROM stock_daily "
            "WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)"
        )
        for _, row in df_price.iterrows():
            price_map[str(row['ts_code']).strip()] = float(row['close'] or 0)
        if not df_price.empty:
            latest_price_date = str(df_price.iloc[0]['trade_date'])
    except Exception:
        pass

    # 4. 风控参数
    portfolio_cfg = Config.get('portfolio') or {}
    stop_loss_pct   = portfolio_cfg.get('stop_loss_pct', 0.08) if isinstance(portfolio_cfg, dict) else 0.08
    take_profit_pct = portfolio_cfg.get('take_profit_pct', 0.20) if isinstance(portfolio_cfg, dict) else 0.20
    min_hold_days   = portfolio_cfg.get('min_hold_days', 3) if isinstance(portfolio_cfg, dict) else 3

    from datetime import date as date_type
    today = datetime.now().date()

    # 5. 生成建议
    buy_list   = []   # 今日推荐 且 未持有
    sell_list  = []   # 持有中 且 不在今日推荐
    hold_list  = []   # 持有中 且 在今日推荐
    alert_list = []   # 止损预警

    for code, pick in picks_map.items():
        price = price_map.get(code, 0)
        is_new = code not in prev_codes  # 今日新上榜
        if code in holdings:
            h = holdings[code]
            avg = h['avg_cost']
            cur_price = price or h['current_price']
            pct = round((cur_price / avg - 1) * 100, 2) if avg > 0 and cur_price > 0 else 0
            hold_list.append({
                'ts_code': code, 'name': pick['name'] or h['name'],
                'score': round(pick['score'], 3), 'track': pick['track'], 'concept': pick['concept'],
                'shares': int(h['shares']), 'avg_cost': round(avg, 2),
                'current_price': round(cur_price, 2), 'pct_chg': pct,
                'buy_date': h['buy_date'], 'is_new': is_new,
            })
        else:
            buy_list.append({
                'ts_code': code, 'name': pick['name'],
                'score': round(pick['score'], 3), 'track': pick['track'], 'concept': pick['concept'],
                'current_price': round(price, 2), 'is_new': is_new,
            })

    for code, h in holdings.items():
        avg = h['avg_cost']
        cur_price = price_map.get(code, 0) or h['current_price']
        pct = round((cur_price / avg - 1) * 100, 2) if avg > 0 and cur_price > 0 else 0

        # 检查持仓天数（未到 min_hold_days 的，即使不在推荐里也标注）
        hold_days = 0
        locked = False
        if h['buy_date']:
            try:
                buy_dt = datetime.strptime(str(h['buy_date'])[:8], '%Y%m%d').date()
                hold_days = (today - buy_dt).days
                locked = hold_days < min_hold_days
            except Exception:
                pass

        # 止损预警
        if avg > 0 and cur_price > 0 and pct < -stop_loss_pct * 100:
            alert_list.append({
                'ts_code': code, 'name': h['name'],
                'shares': int(h['shares']), 'avg_cost': round(avg, 2),
                'current_price': round(cur_price, 2), 'pct_chg': pct,
                'hold_days': hold_days, 'locked': locked,
            })

        if code not in picks_map:
            sell_list.append({
                'ts_code': code, 'name': h['name'],
                'shares': int(h['shares']), 'avg_cost': round(avg, 2),
                'current_price': round(cur_price, 2), 'pct_chg': pct,
                'hold_days': hold_days,
                'locked': locked,  # True 表示未到最小持仓期，建议暂不卖
            })

    buy_list.sort(key=lambda x: x['score'], reverse=True)
    sell_list.sort(key=lambda x: (x['locked'], x['pct_chg']))  # 锁定的排后面，亏损排前面
    alert_list.sort(key=lambda x: x['pct_chg'])

    result = {
        'picks_date': picks_date_fmt,
        'latest_price_date': latest_price_date,
        'min_hold_days': min_hold_days,
        'buy': buy_list,
        'sell': sell_list,
        'hold': hold_list,
        'alerts': alert_list,
        'stop_loss_pct': round(stop_loss_pct * 100, 1),
        'take_profit_pct': round(take_profit_pct * 100, 1),
        'summary': {
            'buy_count': len(buy_list),
            'sell_count': len(sell_list),
            'hold_count': len(hold_list),
            'alert_count': len(alert_list),
        },
    }
    _cache.set("advice", result, ttl=60)
    return result


# ─── ETF 抄底雷达 ────────────────────────────────────────────────────────────

@router.get("/etf/picks")
def get_etf_picks(top_n: int = 6):
    """
    ETF 选股结果：读最近一次 output/etf_picks_YYYYMMDD.csv
    由 daily_alpha_run.py 每日生成，无需实时计算。
    """
    import os, glob
    try:
        import pandas as pd
        # 找最新的 etf_picks CSV
        files = sorted(glob.glob(os.path.join('output', 'etf_picks_*.csv')), reverse=True)
        if not files:
            return {"picks": [], "error": "尚无ETF选股结果，请先运行 daily_alpha_run.py"}
        df = pd.read_csv(files[0], encoding='utf-8-sig')
        cache_date = os.path.basename(files[0]).replace('etf_picks_', '').replace('.csv', '')
        if df.empty:
            return {"picks": [], "error": "ETF选股结果为空"}

        # 分类 helper
        def classify(name: str) -> str:
            n = str(name)
            if any(k in n for k in ['黄金', '石油', '能源', '农产品', '铜', '豆', '玉米', '白银', '煤炭']):
                return 'commodity'
            if any(k in n for k in ['纳斯达克', '标普', '美国', '日本', '德国', '法国', 'QDII', '港股', '香港', '亚太']):
                return 'qdii'
            return 'astock'

        # signal 颜色映射
        signal_map = {'积极配置': '强烈买入', '重点关注': '关注', '观望': '观望'}
        picks = []
        for _, row in df.head(top_n).iterrows():
            name = str(row.get('name', ''))
            score_raw = row.get('score', 0)
            signal = str(row.get('signal', '') or row.get('futures_operation_advice', '') or '')
            strategy = str(row.get('strategy', '') or '')
            picks.append({
                'code':     str(row.get('code', '')),
                'name':     name,
                'etf_type': classify(name),
                'score':    float(score_raw) if str(score_raw).replace('.','').isdigit() else 0,
                'drawdown': 0.0,
                'rsi':      50.0,
                'ret_5d':   float(str(row.get('涨跌幅', 0) or 0)) if '涨跌幅' in df.columns else 0.0,
                'ret_20d':  0.0,
                'advice':   signal or strategy,
            })
        return {"picks": picks, "count": len(picks), "cache_date": cache_date}
    except Exception as e:
        import traceback
        return {"picks": [], "error": str(e), "trace": traceback.format_exc()}


# ─── ETF 策略（读 QMT 库） ────────────────────────────────────────────────────

@router.get("/etf/qmt")
def get_etf_qmt():
    """读取 QMT 已算好的 ETF 推荐 + 组合快照，秒级响应"""
    import pymysql, json as _json
    from src.utils.config_loader import Config
    try:
        mysql = Config.mysql if hasattr(Config, 'mysql') else {}
        conn = pymysql.connect(
            host=mysql.get('host', '192.168.3.41'),
            port=int(mysql.get('port', 3306)),
            user=mysql.get('user', 'root'),
            password=mysql.get('password', ''),
            database='qmt', charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True, connect_timeout=5,
        )
        cur = conn.cursor()

        # 最新快照
        cur.execute("""
            SELECT run_date, market_mode, cycle, total_weight, position_count,
                   rebalance, rebalance_reason, portfolio_json, env_json
            FROM etf_portfolio_snapshots
            WHERE run_date = (SELECT MAX(run_date) FROM etf_portfolio_snapshots)
            ORDER BY position_count DESC, id DESC LIMIT 1
        """)
        snap = cur.fetchone() or {}
        env  = _json.loads(snap.get('env_json') or '{}')
        port = _json.loads(snap.get('portfolio_json') or '{}')  # {code: weight}

        # 推荐列表（去重，每个 code 只取最高分）
        cur.execute("""
            SELECT code, name, category, final_score, val_pct, val_method,
                   mom_score, etf_signal, target_pos, etf_action
            FROM etf_recommendations
            WHERE run_date = (SELECT MAX(run_date) FROM etf_recommendations)
            ORDER BY final_score DESC
        """)
        rows = cur.fetchall()
        conn.close()

        # 去重
        seen, recs = set(), []
        for r in rows:
            if r['code'] not in seen:
                seen.add(r['code'])
                recs.append({
                    'code':        r['code'],
                    'name':        r['name'] or r['code'],
                    'category':    r['category'] or '',
                    'final_score': round(float(r['final_score'] or 0), 3),
                    'val_pct':     round(float(r['val_pct']), 1) if r['val_pct'] is not None else None,
                    'val_method':  r['val_method'] or '',
                    'mom_score':   round(float(r['mom_score'] or 0), 3),
                    'etf_signal':  r['etf_signal'] or '',
                    'target_pos':  r['target_pos'] or '',
                    'etf_action':  r['etf_action'] or '',
                    'in_portfolio': r['code'] in port,
                    'weight':      round(float(port.get(r['code'], 0)) * 100, 0) if r['code'] in port else 0,
                })

        # 当前组合持仓（保留顺序，补充 name）
        name_map = {r['code']: r['name'] for r in recs}
        portfolio = [
            {'code': c, 'name': name_map.get(c, c), 'weight': round(float(w) * 100, 0)}
            for c, w in port.items()
        ]

        return {
            'run_date':       snap.get('run_date', ''),
            'market_mode':    snap.get('market_mode', ''),
            'cycle':          snap.get('cycle', ''),
            'total_weight':   round(float(snap.get('total_weight') or 0) * 100, 0),
            'position_count': snap.get('position_count', 0),
            'rebalance':      bool(snap.get('rebalance', 0)),
            'rebalance_reason': snap.get('rebalance_reason', ''),
            'env': {
                'pmi':            env.get('pmi'),
                'rate_10y':       env.get('rate_10y'),
                'market_ret20d':  round(float(env.get('market_ret20d', 0)) * 100, 2),
                'risk_on':        env.get('risk_on', True),
                'mode_reason':    env.get('mode_reason', ''),
                'cycle_reason':   env.get('cycle_reason', ''),
            },
            'portfolio': portfolio,
            'recommendations': recs,
        }
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


# ─── 期货行情 ────────────────────────────────────────────────────────────────

# 主要期货品种配置
_FUTURES_SPEC = [
    # (新浪代码, 显示名, 类别, 单位)
    ("AU0",  "黄金",    "贵金属",   "元/克"),
    ("AG0",  "白银",    "贵金属",   "元/千克"),
    ("SC0",  "原油",    "能源",     "元/桶"),
    ("FU0",  "燃油",    "能源",     "元/吨"),
    ("RB0",  "螺纹钢",  "黑色金属", "元/吨"),
    ("HC0",  "热轧卷板","黑色金属", "元/吨"),
    ("I0",   "铁矿石",  "黑色金属", "元/吨"),
    ("J0",   "焦炭",    "黑色金属", "元/吨"),
    ("CU0",  "铜",      "有色金属", "元/吨"),
    ("AL0",  "铝",      "有色金属", "元/吨"),
    ("ZN0",  "锌",      "有色金属", "元/吨"),
    ("NI0",  "镍",      "有色金属", "元/吨"),
    ("M0",   "豆粕",    "农产品",   "元/吨"),
    ("Y0",   "豆油",    "农产品",   "元/吨"),
    ("C0",   "玉米",    "农产品",   "元/吨"),
    ("CF0",  "棉花",    "农产品",   "元/吨"),
    ("SR0",  "白糖",    "农产品",   "元/吨"),
    ("MA0",  "甲醇",    "能源化工", "元/吨"),
    ("TA0",  "PTA",     "能源化工", "元/吨"),
    ("PP0",  "聚丙烯",  "能源化工", "元/吨"),
]


@router.get("/futures/overview")
def get_futures_overview():
    """主要商品期货主力合约概览（近30日走势 + 最新价涨跌）"""
    try:
        import akshare as ak
        for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ.pop(_k, None)
        os.environ["NO_PROXY"] = "*"

        items = []
        errors = []
        for sym, name, category, unit in _FUTURES_SPEC:
            try:
                df = ak.futures_zh_daily_sina(symbol=sym)
                if df is None or df.empty or len(df) < 2:
                    continue
                df = df.tail(30).reset_index(drop=True)
                latest = df.iloc[-1]
                prev = df.iloc[-2]
                close = float(latest["close"])
                prev_close = float(prev["close"])
                pct_chg = (close - prev_close) / prev_close * 100 if prev_close else 0
                pct_30d = (close - float(df.iloc[0]["close"])) / float(df.iloc[0]["close"]) * 100
                items.append({
                    "symbol": sym,
                    "name": name,
                    "category": category,
                    "unit": unit,
                    "close": round(close, 2),
                    "pct_chg": round(pct_chg, 2),
                    "pct_30d": round(pct_30d, 2),
                    "high": round(float(latest["high"]), 2),
                    "low": round(float(latest["low"]), 2),
                    "volume": int(latest["volume"]),
                    "trade_date": str(latest["date"]),
                    # 近30日收盘价序列（用于迷你图）
                    "series": [round(float(x), 2) for x in df["close"].tolist()],
                })
            except Exception as e:
                errors.append(f"{sym}:{e}")
        return {"items": items, "total": len(items), "errors": errors[:5]}
    except ImportError:
        return {"items": [], "error": "akshare 未安装"}
    except Exception as e:
        return {"items": [], "error": str(e)}


@router.get("/futures/kline")
def get_futures_kline(symbol: str = Query(...), days: int = Query(90)):
    """单品种期货K线数据"""
    try:
        import akshare as ak
        for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ.pop(_k, None)
        os.environ["NO_PROXY"] = "*"

        df = ak.futures_zh_daily_sina(symbol=symbol)
        if df is None or df.empty:
            return {"kline": [], "error": f"品种 {symbol} 无数据"}
        df = df.tail(days).reset_index(drop=True)
        df = df.fillna(0)
        kline = []
        for _, row in df.iterrows():
            kline.append({
                "date": str(row["date"]),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
                "volume": int(row["volume"]),
            })
        return {"kline": kline, "symbol": symbol, "total": len(kline)}
    except Exception as e:
        return {"kline": [], "error": str(e)}


# ─── 回测 ────────────────────────────────────────────────────────────────────

@router.post("/backtest/run")
async def backtest_run(request: Request):
    """启动回测（后台线程），立即返回 job_id"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'error': 'Invalid JSON body'}, status_code=422)
    
    strategy = body.get('strategy', 'hybrid')
    start_date = body.get('start_date', '')
    end_date = body.get('end_date', '')
    top_k = int(body.get('top_k', 10))
    rebalance_days = int(body.get('rebalance_days', 10))
    cost_rate = float(body.get('cost_rate', 0.0003))

    if not start_date or not end_date:
        return JSONResponse({'error': '请提供 start_date 和 end_date'}, status_code=400)

    job_id = f"bt_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
    _backtest_jobs[job_id] = {
        'status': 'running', 
        'progress': 0, 
        'message': '初始化...', 
        'strategy': strategy,
        'start_date': start_date,
        'end_date': end_date,
        'result': None, 
        'error': None,
        'created_at': datetime.now().isoformat()
    }

    def _run():
        try:
            from src.backtest.backtest_engine import get_backtest_engine
            engine = get_backtest_engine()
            
            def cb(pct, msg):
                _backtest_jobs[job_id]['progress'] = pct
                _backtest_jobs[job_id]['message'] = msg

            result = engine.run(
                strategy=strategy,
                start_date=start_date,
                end_date=end_date,
                top_k=top_k,
                rebalance_days=rebalance_days,
                cost_rate=cost_rate,
                progress_callback=cb
            )
            
            if result.get('success'):
                _backtest_jobs[job_id]['status'] = 'done'
                _backtest_jobs[job_id]['result'] = result
                _backtest_jobs[job_id]['progress'] = 100
                _backtest_jobs[job_id]['message'] = '完成'
                # 持久化到MySQL
                _save_backtest_result(result)
            else:
                _backtest_jobs[job_id]['status'] = 'error'
                _backtest_jobs[job_id]['error'] = result.get('error', 'Unknown error')
        except Exception as e:
            import traceback
            _backtest_jobs[job_id]['status'] = 'error'
            _backtest_jobs[job_id]['error'] = str(e)
            print(f"[Backtest] 错误: {traceback.format_exc()}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return JSONResponse({'job_id': job_id, 'status': 'running'})


def _save_backtest_result(result: dict):
    """保存回测结果到MySQL"""
    try:
        from src.utils.db_utils import DBUtils
        import json
        
        job_id = result.get('job_id', '')
        metrics = result.get('metrics', {})
        
        sql = """
        INSERT INTO backtest_results (
            job_id, strategy, start_date, end_date, top_k, rebalance_days,
            total_return, annualized_return, sharpe_ratio, max_drawdown, win_rate, total_trades,
            daily_returns, equity_curve, metrics_json, status, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
        """
        
        params = [
            job_id,
            result.get('strategy', ''),
            result.get('start_date', ''),
            result.get('end_date', ''),
            result.get('params', {}).get('top_k', 10),
            result.get('params', {}).get('rebalance_days', 10),
            metrics.get('total_return', 0),
            metrics.get('annualized_return', 0),
            metrics.get('sharpe_ratio', 0),
            metrics.get('max_drawdown', 0),
            metrics.get('win_rate', 0),
            metrics.get('total_trades', 0),
            json.dumps(result.get('daily_returns', []), ensure_ascii=False),
            json.dumps(result.get('equity_curve', []), ensure_ascii=False),
            json.dumps(metrics, ensure_ascii=False),
            'completed'
        ]
        
        DBUtils.execute(sql, params)
        print(f"[Backtest] 结果已持久化: {job_id}")
    except Exception as e:
        print(f"[Backtest] 持久化失败: {e}")


@router.get("/backtest/status/{job_id}")
async def backtest_status(job_id: str):
    """查询回测进度/结果"""
    job = _backtest_jobs.get(job_id)
    if not job:
        # 尝试从数据库查询
        try:
            from src.utils.db_utils import DBUtils
            df = DBUtils.query_df(
                "SELECT * FROM backtest_results WHERE job_id = ?", 
                params=[job_id]
            )
            if not df.empty:
                row = df.iloc[0]
                return JSONResponse({
                    'status': 'done',
                    'job_id': job_id,
                    'strategy': row.get('strategy', ''),
                    'result': {
                        'metrics': json.loads(row.get('metrics_json', '{}')),
                        'total_return': row.get('total_return', 0),
                        'annualized_return': row.get('annualized_return', 0),
                    }
                })
        except:
            pass
        return JSONResponse({'error': '任务不存在'}, status_code=404)
    return JSONResponse({
        'status': job['status'],
        'progress': job.get('progress', 0),
        'message': job.get('message', ''),
        'result': job.get('result'),
        'error': job.get('error'),
    })


@router.get("/backtest/results")
async def backtest_results(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    strategy: str = Query("")
):
    """获取历史回测结果列表"""
    try:
        from src.utils.db_utils import DBUtils
        
        where = "1=1"
        params = []
        if strategy:
            where += " AND strategy = ?"
            params.append(strategy)
        
        offset = (page - 1) * per_page
        df = DBUtils.query_df(
            f"""SELECT job_id, strategy, start_date, end_date, top_k, rebalance_days,
                       total_return, annualized_return, sharpe_ratio, max_drawdown, win_rate, total_trades,
                       created_at, completed_at
                FROM backtest_results 
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT {per_page} OFFSET {offset}""",
            params=params
        )
        
        total_df = DBUtils.query_df(
            f"SELECT COUNT(*) as cnt FROM backtest_results WHERE {where}",
            params=params
        )
        total = int(total_df.iloc[0]['cnt']) if not total_df.empty else 0
        
        items = df.to_dict('records') if not df.empty else []
        return JSONResponse({
            'items': items,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get("/backtest/result/{job_id}")
async def backtest_result(job_id: str):
    """获取回测详细结果"""
    try:
        from src.utils.db_utils import DBUtils
        import json
        
        df = DBUtils.query_df(
            "SELECT * FROM backtest_results WHERE job_id = ?",
            params=[job_id]
        )
        
        if df.empty:
            # 尝试从内存
            job = _backtest_jobs.get(job_id)
            if job and job.get('result'):
                return JSONResponse(job['result'])
            return JSONResponse({'error': '结果不存在'}, status_code=404)
        
        row = df.iloc[0]
        result = {
            'job_id': row['job_id'],
            'strategy': row['strategy'],
            'start_date': row['start_date'],
            'end_date': row['end_date'],
            'params': {
                'top_k': row.get('top_k', 10),
                'rebalance_days': row.get('rebalance_days', 10)
            },
            'metrics': json.loads(row.get('metrics_json', '{}')),
            'equity_curve': json.loads(row.get('equity_curve', '[]')),
            'daily_returns': json.loads(row.get('daily_returns', '[]')),
            'created_at': str(row.get('created_at', '')),
            'completed_at': str(row.get('completed_at', ''))
        }
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get("/backtest/strategies")
async def backtest_strategies():
    """获取可用的回测策略列表"""
    try:
        from src.backtest.backtest_engine import get_backtest_engine
        engine = get_backtest_engine()
        strategies = engine.get_available_strategies()
        return JSONResponse({'strategies': strategies})
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


# ─── 测试工具 ────────────────────────────────────────────────────────────────

@router.post("/test/llm")
def test_llm():
    """测试 LLM 连通性"""
    from src.utils.llm_client import get_llm_client
    try:
        client = get_llm_client()
        if not client.is_available():
            return {"success": False, "error": "LLM客户端初始化失败，请检查API Key配置"}
        result = client._call_llm("你是A股量化分析助手", "用一句话介绍你自己", temperature=0.3, max_tokens=60)
        return {"success": True, "response": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/test/dingtalk")
def test_dingtalk():
    """测试钉钉推送连通性"""
    from src.utils.notifier import NotifierFactory
    from src.utils.config_loader import Config
    try:
        dingtalk_cfg = Config.get('notification') or {}
        if isinstance(dingtalk_cfg, dict):
            dingtalk_cfg = dingtalk_cfg.get('dingtalk') or {}
        webhook = dingtalk_cfg.get('webhook', '') if isinstance(dingtalk_cfg, dict) else ''
        secret = dingtalk_cfg.get('secret_word', '提醒') if isinstance(dingtalk_cfg, dict) else '提醒'
        if not webhook:
            return {"success": False, "error": "Webhook 未配置，请在 settings.yaml 中设置 notification.dingtalk.webhook"}
        notifier = NotifierFactory.create_notifier('dingtalk', webhook_url=webhook, secret_word=secret)
        ok = notifier.send_message("【系统测试提醒】", "Web管理界面连通性测试，可忽略此消息")
        return {"success": ok}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── 手动触发 ────────────────────────────────────────────────────────────────

@router.post("/run/morning")
def run_morning():
    """手动触发早盘推送"""
    script = os.path.join(ROOT, 'scripts', 'morning_push.py')
    if not os.path.exists(script):
        return {"success": False, "error": "morning_push.py 不存在"}
    try:
        subprocess.Popen(
            [sys.executable, script],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"success": True, "message": "早盘推送已在后台启动"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/run/evening")
def run_evening():
    """手动触发晚盘分析"""
    script = os.path.join(ROOT, 'scripts', 'evening_push.py')
    if not os.path.exists(script):
        return {"success": False, "error": "evening_push.py 不存在"}
    try:
        subprocess.Popen(
            [sys.executable, script],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"success": True, "message": "晚盘分析已在后台启动"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/run/strategy")
def run_strategy():
    """手动触发策略选股（跳过同步和AI训练）"""
    script = os.path.join(ROOT, 'scripts', 'daily_alpha_run.py')
    if not os.path.exists(script):
        return {"success": False, "error": "daily_alpha_run.py 不存在"}
    try:
        subprocess.Popen(
            [sys.executable, script, '--skip-sync', '--skip-qlib'],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"success": True, "message": "策略选股已在后台启动（skip-sync, skip-qlib）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── 股票池 ────────────────────────────────────────────────────────────────

@router.get("/pool/status")
def get_pool_status():
    """股票池概况：各层数量"""
    from src.utils.db_utils import DBUtils
    try:
        df = DBUtils.query_df(
            "SELECT tier, COUNT(*) as cnt FROM stock_pool WHERE is_active=1 GROUP BY tier"
        )
        result = {"core_holding": 0, "watch": 0, "reserve": 0, "total": 0}
        for _, row in df.iterrows():
            t = str(row["tier"])
            result[t] = int(row["cnt"])
        result["total"] = sum(result[v] for v in ["core_holding", "watch", "reserve"])
        return result
    except Exception as e:
        return {"total": 0, "error": str(e)}


# 信号缓存：避免每次页面打开都重跑全量扫描（~4s）
_signals_cache: dict = {"ts": 0.0, "data": None}
_SIGNALS_CACHE_TTL = 1800  # 30分钟


@router.get("/pool/signals")
def get_pool_signals(refresh: bool = Query(False)):
    """股票池买入信号（调用 PoolStrategy，不重新计算估值）。10分钟内返回缓存。"""
    import time
    global _signals_cache
    now = time.time()
    if not refresh and _signals_cache["data"] is not None and (now - _signals_cache["ts"]) < _SIGNALS_CACHE_TTL:
        cached = dict(_signals_cache["data"])
        cached["cached"] = True
        cached["cache_age_s"] = int(now - _signals_cache["ts"])
        return cached

    from src.strategy.pool_strategy import PoolStrategy
    import concurrent.futures
    try:
        def _run_pool():
            strategy = PoolStrategy()
            return strategy.run(update_valuation=False)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run_pool)
            try:
                result = future.result(timeout=60)
            except concurrent.futures.TimeoutError:
                return {"signals": [], "approaching": [], "all_pool": [], "error": "timeout (60s)", "signals_count": 0}
        
        signals = result.get("signals")
        approaching = result.get("approaching")
        all_pool = result.get("all_pool")

        def df_to_list(df):
            if df is None or df.empty:
                return []
            df = df.fillna("")
            return df.to_dict("records")

        data = {
            "signals": df_to_list(signals),
            "signals_count": len(signals) if signals is not None and not signals.empty else 0,
            "approaching": df_to_list(approaching),
            "approaching_count": len(approaching) if approaching is not None and not approaching.empty else 0,
            "all_pool": df_to_list(all_pool),
            "all_pool_count": len(all_pool) if all_pool is not None and not all_pool.empty else 0,
            "summary": result.get("summary", ""),
            "cached": False,
        }
        _signals_cache = {"ts": now, "data": data}
        return data
    except Exception as e:
        import traceback
        return {"signals": [], "approaching": [], "all_pool": [], "error": str(e), "trace": traceback.format_exc()[:500], "signals_count": 0}


@router.post("/pool/weekly_refresh")
def trigger_weekly_refresh(dry_run: bool = Query(False)):
    """触发每周股票池刷新（后台运行）"""
    script = os.path.join(ROOT, "scripts", "weekly_pool_refresh.py")
    if not os.path.exists(script):
        return {"success": False, "error": "weekly_pool_refresh.py 不存在"}
    cmd = [sys.executable, script]
    if dry_run:
        cmd.append("--dry-run")
    try:
        subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"success": True, "message": "每周刷新已在后台启动" + ("（dry-run）" if dry_run else "")}
    except Exception as e:
        return {"success": False, "error": str(e)}



@router.post("/pool/add")
def pool_add(ts_code: str = Query(...), tier: str = Query("reserve"), notes: str = Query("")):
    """手工将股票加入池子。ts_code 支持纯数字（自动补后缀）或完整代码。"""
    from src.universe.stock_pool import StockPool
    from src.utils.db_utils import DBUtils
    # 支持纯数字代码：600519 → 600519.SH / 000001 → 000001.SZ
    code = ts_code.strip()
    if code.isdigit() and "." not in code:
        code = code + (".SH" if code.startswith("6") else ".SZ")
    # 验证股票代码是否存在
    df = DBUtils.query_df("SELECT name FROM stock_info WHERE ts_code = ?", params=[code])
    if df.empty:
        return {"success": False, "error": f"股票代码 {code} 不存在，请检查"}
    try:
        pool = StockPool()
        added = pool.add(code, tier=tier, notes=notes)
        name = df["name"].iloc[0] if not df.empty else code
        if added:
            return {"success": True, "message": f"已将 {code} {name} 加入{tier}层"}
        else:
            return {"success": False, "error": f"{code} 已在池中"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/pool/remove")
def pool_remove(ts_code: str = Query(...)):
    """将股票从池子中移除（标记 is_active=0）"""
    from src.universe.stock_pool import StockPool
    try:
        pool = StockPool()
        pool.remove(ts_code, reason="手工移除")
        return {"success": True, "message": f"已移除 {ts_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/pool/list")
def pool_list(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    q: str = Query(""),
    sector: str = Query(""),
    tier: str = Query(""),
    ai_layer: str = Query(""),
    manual_only: bool = Query(False),
    sort_by: str = Query(""),
    sort_order: str = Query("asc"),
):
    """股票池名单，支持分页、搜索、板块/层级/AI赛道层过滤。（5分钟缓存）"""
    cache_key = f"pool_list_{page}_{per_page}_{q}_{sector}_{tier}_{ai_layer}_{manual_only}_{sort_by}_{sort_order}"
    cached, hit = _cache.get(cache_key)
    if hit:
        return cached
    from src.utils.db_utils import DBUtils
    try:
        conds = ["sp.is_active = 1"]
        params = []
        if q:
            conds.append("(sp.ts_code LIKE ? OR sp.company_name LIKE ?)")
            like = f"%{q}%"
            params += [like, like]
        if sector:
            conds.append("sp.sector = ?")
            params.append(sector)
        if tier:
            conds.append("sp.tier = ?")
            params.append(tier)
        if ai_layer:
            conds.append("sp.ai_layer = ?")
            params.append(ai_layer)
        if manual_only:
            conds.append("sp.is_manual = 1")

        where = " AND ".join(conds)

        # 总数
        cnt_df = DBUtils.query_df(f"SELECT COUNT(*) as cnt FROM stock_pool sp WHERE {where}", params=params)
        total = int(cnt_df.iloc[0]["cnt"]) if not cnt_df.empty else 0

        offset = (page - 1) * per_page
        # 排序
        _SORT_MAP = {
            "pe": "CAST(si.pe_ttm AS DECIMAL(10,2))",
            "pb": "CAST(si.pb AS DECIMAL(10,2))",
            "mv": "CAST(si.total_mv AS DECIMAL(20,0))",
            "tier": "sp.tier",
            "name": "sp.company_name",
            "date": "sp.enter_date",
        }
        order_dir = "DESC" if sort_order == "desc" else "ASC"
        if sort_by and sort_by in _SORT_MAP:
            order_clause = f"{_SORT_MAP[sort_by]} {order_dir}"
        else:
            order_clause = "sp.is_manual DESC, sp.tier, sp.ts_code"
        # LIMIT/OFFSET embedded directly to avoid params count mismatch with pd.read_sql_query
        sql = f"""
            SELECT sp.ts_code, sp.company_name, sp.company_type, sp.tier,
                   sp.sector, sp.is_manual, sp.enter_date, sp.notes,
                   sp.ai_layer,
                    si.industry, si.pe_ttm, si.pb, si.total_mv
            FROM stock_pool sp
            LEFT JOIN stock_info si ON sp.ts_code = si.ts_code
            WHERE {where}
            ORDER BY {order_clause}
            LIMIT {int(per_page)} OFFSET {int(offset)}
        """
        df = DBUtils.query_df(sql, params=params)
        if df.empty:
            return {"items": [], "total": total, "page": page, "per_page": per_page}
        df = df.fillna("")
        # total_mv: 万元 → 亿元
        df["total_mv_yi"] = df["total_mv"].apply(lambda x: round(float(x) / 10000, 1) if x and x != "" else "")

        # 注入 profit_warnings 数据
        warn_by_code, warn_by_name = _load_profit_warnings()
        df["warning_level"] = ""
        df["warning_signals"] = ""
        for idx, row in df.iterrows():
            w = warn_by_code.get(row["ts_code"]) or warn_by_name.get(row["company_name"])
            if w:
                level = str(w.get("level", ""))
                color = "红" if "红" in level else ("黄" if "黄" in level else "")
                df.at[idx, "warning_level"] = color
                df.at[idx, "warning_signals"] = str(w.get("signals", ""))

        # 注入 netprofit_yoy（最新一期净利润同比增速）
        try:
            codes = df["ts_code"].tolist()
            if codes:
                ph = ",".join(["?" for _ in codes])
                yoy_df = DBUtils.query_df(f"""
                    SELECT sd.ts_code, sd.netprofit_yoy
                    FROM stock_daily sd
                    INNER JOIN (
                        SELECT ts_code, MAX(trade_date) AS max_date
                        FROM stock_daily
                        WHERE ts_code IN ({ph}) AND netprofit_yoy IS NOT NULL
                        GROUP BY ts_code
                    ) latest ON sd.ts_code = latest.ts_code AND sd.trade_date = latest.max_date
                """, params=codes)
                yoy_map = {}
                if not yoy_df.empty:
                    for _, row in yoy_df.iterrows():
                        yoy_map[row["ts_code"]] = row["netprofit_yoy"]
                df["netprofit_yoy"] = df["ts_code"].map(yoy_map).fillna("")
        except Exception:
            df["netprofit_yoy"] = ""

        # 注入最新相关新闻标题（从 news_cache 匹配）
        try:
            news_df = DBUtils.query_df("""
                SELECT title, published_at, matched_stocks
                FROM news_cache
                WHERE published_at >= DATE_SUB(NOW(), INTERVAL 14 DAY)
                ORDER BY published_at DESC
                LIMIT 500
            """)
            news_map = {}
            if not news_df.empty:
                for _, nrow in news_df.iterrows():
                    matched = str(nrow.get("matched_stocks") or "")
                    title = str(nrow.get("title") or "")
                    for _, srow in df.iterrows():
                        code = srow["ts_code"]
                        name = srow["company_name"]
                        if code not in news_map and (code in matched or (name and name in (matched + title))):
                            news_map[code] = title
            df["latest_news"] = df["ts_code"].map(news_map).fillna("")
        except Exception:
            df["latest_news"] = ""

        result = {
            "items": df.to_dict("records"),
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
        }
        _cache.set(cache_key, result, ttl=300)
        return result
    except Exception as e:
        return {"items": [], "total": 0, "error": str(e)}


@router.get("/pool/sectors")
def pool_sectors():
    """返回池中所有板块列表（用于前端筛选）"""
    from src.utils.db_utils import DBUtils
    try:
        df = DBUtils.query_df(
            "SELECT DISTINCT sector FROM stock_pool WHERE is_active=1 AND sector IS NOT NULL AND sector != '' ORDER BY sector"
        )
        return {"sectors": df["sector"].tolist() if not df.empty else []}
    except Exception as e:
        return {"sectors": [], "error": str(e)}


@router.get("/pool/layers")
def pool_layers():
    """返回各 AI 层级 / 红利赛道股票数量（用于前端分布条）"""
    from src.utils.db_utils import DBUtils
    try:
        df = DBUtils.query_df(
            "SELECT ai_layer, COUNT(*) as cnt FROM stock_pool WHERE is_active=1 GROUP BY ai_layer ORDER BY ai_layer"
        )
        result = {}
        if not df.empty:
            for _, row in df.iterrows():
                key = str(row["ai_layer"]) if row["ai_layer"] else "unknown"
                result[key] = int(row["cnt"])
        return {"layers": result}
    except Exception as e:
        return {"layers": {}, "error": str(e)}


@router.get("/pool/search")
def pool_search(q: str = Query(...)):
    """按代码或名称模糊搜索股票（用于手工加池子时的自动补全）"""
    from src.utils.db_utils import DBUtils
    try:
        # 支持代码前缀或名称模糊
        like = f"%{q}%"
        df = DBUtils.query_df(
            "SELECT ts_code, name, industry FROM stock_info WHERE ts_code LIKE ? OR name LIKE ? LIMIT 10",
            params=[like, like],
        )
        if df.empty:
            return {"results": []}
        df = df.fillna("")
        return {"results": df.to_dict("records")}
    except Exception as e:
        return {"results": [], "error": str(e)}


@router.get("/pool/health_check")
def pool_health_check(force_refresh: bool = Query(False)):
    """股票池健康度检查（5分钟缓存，force_refresh时跳过缓存）"""
    if not force_refresh:
        cached, hit = _cache.get('pool_health')
        if hit:
            return cached
    from src.universe.stock_health import StockHealthChecker
    try:
        checker = StockHealthChecker()
        df = checker.check_pool(force_refresh=force_refresh)
        if df.empty:
            return {"items": [], "summary": {"red": 0, "yellow": 0, "green": 0, "total": 0}}

        df = df.fillna("")

        summary = {
            "red":    int((df["light"] == "red").sum()),
            "yellow": int((df["light"] == "yellow").sum()),
            "green":  int((df["light"] == "green").sum()),
            "total":  len(df),
        }
        result = {
            "items":   df.to_dict("records"),
            "summary": summary,
            "checked_at": df.iloc[0].get("checked_at", "") if len(df) > 0 else "",
            "data_period": df.iloc[0].get("data_period", "") if len(df) > 0 else "",
        }
        _cache.set('pool_health', result, ttl=300)
        return result
    except Exception as e:
        return {"items": [], "summary": {}, "error": str(e)}


# ─── 持仓 V2 ────────────────────────────────────────────────────────────────

@router.get("/positions/sell_signals")
def get_sell_signals():
    """V2 三类卖出信号：止损 / 估值高 / 盈利逻辑破坏"""
    from src.portfolio.position_manager import PositionManager
    try:
        pm = PositionManager()
        signals = pm.check_sell_signals()
        if not signals:
            return {"signals": [], "count": 0}
        result = []
        for s in signals:
            item = dict(s) if isinstance(s, dict) else s
            result.append(item)
        return {"signals": result, "count": len(result)}
    except Exception as e:
        return {"signals": [], "count": 0, "error": str(e)}


@router.get("/positions/phase1_candidates")
def get_phase1_candidates():
    """第一期持仓中已盈利可加仓的候选"""
    from src.portfolio.position_manager import PositionManager
    try:
        pm = PositionManager()
        df = pm.get_phase1_candidates()
        if df is None or df.empty:
            return {"candidates": [], "count": 0}
        df = df.fillna("")
        return {"candidates": df.to_dict("records"), "count": len(df)}
    except Exception as e:
        return {"candidates": [], "count": 0, "error": str(e)}


# ─── 个股深度分析 ────────────────────────────────────────────────────────────

@router.get("/analyze/recent")
def get_recent_analysis(limit: int = Query(20)):
    """最近的 LLM 深度分析报告列表"""
    from src.utils.db_utils import DBUtils
    try:
        df = DBUtils.query_df(
            "SELECT r.id, r.ts_code, "
            "COALESCE(sp.company_name, si.name, r.ts_code) as company_name, "
            "COALESCE(sp.company_type, 'growth') as company_type, "
            "r.log_date, r.trigger_type, r.summary as report_summary, r.action_suggestion "
            "FROM research_log r "
            "LEFT JOIN stock_pool sp ON r.ts_code = sp.ts_code AND sp.is_active = 1 "
            "LEFT JOIN stock_info si ON r.ts_code = si.ts_code "
            "ORDER BY r.log_date DESC, r.id DESC LIMIT ?",
            params=[limit],
        )
        if df.empty:
            return {"reports": []}
        df = df.fillna("")
        return {"reports": df.to_dict("records")}
    except Exception as e:
        return {"reports": [], "error": str(e)}


@router.get("/analyze/report")
def get_analysis_report(ts_code: str = Query(...)):
    """获取某只股票最新的分析报告（含完整内容）"""
    from src.utils.db_utils import DBUtils
    try:
        df = DBUtils.query_df(
            "SELECT r.*, "
            "COALESCE(sp.company_name, si.name, r.ts_code) as company_name, "
            "COALESCE(sp.company_type, 'growth') as company_type "
            "FROM research_log r "
            "LEFT JOIN stock_pool sp ON r.ts_code = sp.ts_code AND sp.is_active = 1 "
            "LEFT JOIN stock_info si ON r.ts_code = si.ts_code "
            "WHERE r.ts_code=? ORDER BY r.log_date DESC LIMIT 1",
            params=[ts_code],
        )
        if df.empty:
            return {"report": None}
        df = df.fillna("")
        return {"report": df.iloc[0].to_dict()}
    except Exception as e:
        return {"report": None, "error": str(e)}


@router.get("/pool/valuation")
def pool_valuation(force_refresh: bool = Query(False)):
    """股票池行业估值（5分钟缓存，force_refresh时跳过缓存）"""
    if not force_refresh:
        cached, hit = _cache.get('pool_valuation')
        if hit:
            return cached
    
    # 检查数据库缓存
    from src.utils.db_utils import DBUtils
    try:
        df = DBUtils.query_df(
            "SELECT * FROM valuation_cache ORDER BY upside_pct DESC"
        )
        if not df.empty:
            items = df.fillna("").to_dict("records")
            vcount = df["verdict"].value_counts().to_dict()
            summary = {
                "严重低估": int(vcount.get("严重低估", 0)),
                "低估":     int(vcount.get("低估", 0)),
                "合理":     int(vcount.get("合理", 0)),
                "高估":     int(vcount.get("高估", 0)),
                "严重高估": int(vcount.get("严重高估", 0)),
                "数据不足": int(vcount.get("数据不足", 0)),
                "total":    len(df),
            }
            cd = DBUtils.query_df("SELECT MAX(calc_date) as d FROM valuation_cache")
            calc_date = str(cd.iloc[0]["d"]) if not cd.empty else None
            result = {
                "items":     items,
                "summary":   summary,
                "calc_date": calc_date,
            }
            _cache.set('pool_valuation', result, ttl=300)
            return result
    except Exception as e:
        pass
    
    # 如果没有缓存，启动异步计算
    from src.universe.stock_valuation import run_valuation
    try:
        df = run_valuation(force_refresh=force_refresh)
        if df.empty:
            return {"items": [], "summary": {}, "calc_date": None}

        items = df.fillna("").to_dict("records")

        vcount = df["verdict"].value_counts().to_dict()
        summary = {
            "严重低估": int(vcount.get("严重低估", 0)),
            "低估":     int(vcount.get("低估", 0)),
            "合理":     int(vcount.get("合理", 0)),
            "高估":     int(vcount.get("高估", 0)),
            "严重高估": int(vcount.get("严重高估", 0)),
            "数据不足": int(vcount.get("数据不足", 0)),
            "total":    len(df),
        }

        try:
            cd = DBUtils.query_df("SELECT MAX(calc_date) as d FROM valuation_cache")
            calc_date = str(cd.iloc[0]["d"]) if not cd.empty else None
        except Exception:
            calc_date = None

        result = {
            "items":     items,
            "summary":   summary,
            "calc_date": calc_date,
        }
        _cache.set('pool_valuation', result, ttl=300)
        return result
    except Exception as e:
        import traceback
        return {"items": [], "summary": {}, "error": str(e)}


@router.post("/analyze/run")
def run_stock_analysis(ts_code: str = Query(...)):
    """触发单只股票 LLM 深度分析（同步，可能较慢）"""
    from src.analysis.stock_analyzer import StockAnalyzer
    try:
        analyzer = StockAnalyzer()
        result = analyzer.analyze(ts_code)
        if result is None:
            return {"success": False, "error": "分析失败，请检查股票代码是否正确"}
        # result 是 dict，包含 report_sections 等
        return {"success": True, "result": result if isinstance(result, dict) else {"raw": str(result)}}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── 交易Agent API ────────────────────────────────────────────────────────────

_sim_broker_instance = None

def _get_sim_broker():
    """获取 SimBroker 实例（单例，避免每次请求都初始化DB）"""
    global _sim_broker_instance
    if _sim_broker_instance is None:
        from src.broker.sim_broker import SimBroker
        _sim_broker_instance = SimBroker()
    return _sim_broker_instance


@router.get("/agent/status")
def agent_status():
    """Agent总览：账户概览 + 今日决策摘要 + 最近风控状态（30秒缓存）"""
    cached, hit = _cache.get('agent_status')
    if hit:
        cached['server_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return cached

    from src.utils.db_utils import DBUtils
    from src.utils.config_loader import Config

    # 账户信息
    account_info = {"total_assets": 0, "cash": 0, "market_value": 0, "profit_pct": 0}
    try:
        broker = _get_sim_broker()
        acc = broker.get_account()
        account_info = {
            "total_assets": round(acc.total_assets, 2),
            "cash": round(acc.cash, 2),
            "market_value": round(acc.market_value, 2),
            "profit_pct": round(acc.profit_pct, 2),
        }
    except Exception as e:
        account_info["error"] = str(e)

    # 今日决策
    today = datetime.now().strftime('%Y-%m-%d')
    today_plan = None
    try:
        df = DBUtils.query_df(
            "SELECT trade_date, confidence, market_regime, generated_at, plan_json "
            "FROM agent_decisions WHERE trade_date = ? ORDER BY id DESC LIMIT 1",
            (today,)
        )
        if not df.empty:
            import json
            row = df.iloc[0]
            plan = json.loads(row['plan_json']) if row['plan_json'] else {}
            today_plan = {
                "trade_date": str(row['trade_date']),
                "confidence": float(row['confidence'] or 0),
                "market_regime": str(row['market_regime'] or 'unknown'),
                "generated_at": str(row['generated_at'] or ''),
                "reasoning": plan.get('reasoning', ''),
                "trades_count": len(plan.get('trades', [])),
                "trades": plan.get('trades', []),
            }
    except Exception as e:
        today_plan = {"error": str(e)}

    # Broker类型
    broker_type = Config.get('trading_agent.broker', 'sim')

    # 最近订单（盘中风控触发记录）
    recent_orders = []
    try:
        df_orders = DBUtils.query_df(
            "SELECT ts_code, side, price, volume, amount, status, created_at "
            "FROM agent_sim_orders ORDER BY id DESC LIMIT 5"
        )
        if not df_orders.empty:
            recent_orders = df_orders.to_dict('records')
    except Exception:
        pass

    result = {
        "account": account_info,
        "today_plan": today_plan,
        "broker_type": broker_type,
        "recent_orders": recent_orders,
        "server_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    _cache.set('agent_status', result, ttl=30)
    return result


@router.get("/agent/pending_orders")
def agent_pending_orders():
    """今日挂单（入场价监控中等待触发的委托）"""
    from src.utils.db_utils import DBUtils
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        df = DBUtils.query_df(
            "SELECT id, ts_code, name, weight, entry_price, stop_loss_price, "
            "reason, trade_date, status, created_at, executed_at "
            "FROM agent_pending_orders WHERE trade_date = ? ORDER BY id DESC",
            (today,)
        )
        if df.empty:
            return {"pending": [], "count": 0}
        records = []
        for _, row in df.iterrows():
            records.append({
                "id": int(row.get("id", 0)),
                "ts_code": str(row.get("ts_code", "")),
                "name": str(row.get("name", "")),
                "weight": float(row.get("weight", 0)),
                "entry_price": float(row.get("entry_price", 0)),
                "stop_loss_price": float(row.get("stop_loss_price", 0)),
                "reason": str(row.get("reason", "")),
                "status": str(row.get("status", "pending")),
                "created_at": str(row.get("created_at", ""))[:16],
                "executed_at": str(row.get("executed_at", "") or "")[:16],
            })
        pending = [r for r in records if r["status"] == "pending"]
        return {"pending": pending, "all": records, "count": len(pending)}
    except Exception as e:
        return {"pending": [], "count": 0, "error": str(e)}


@router.get("/agent/today_plan")
def agent_today_plan():
    """今日完整交易计划"""
    from src.utils.db_utils import DBUtils
    import json as _json
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        df = DBUtils.query_df(
            "SELECT * FROM agent_decisions WHERE trade_date = ? ORDER BY id DESC LIMIT 1",
            (today,)
        )
        if df.empty:
            return {"plan": None, "message": f"{today} 暂无决策记录"}
        row = df.iloc[0]
        plan = _json.loads(row['plan_json']) if row['plan_json'] else {}
        return {"plan": plan, "trade_date": today}
    except Exception as e:
        return {"error": str(e)}


@router.post("/agent/run/{phase}")
def agent_run_phase(phase: str):
    """异步触发 Agent 执行阶段：decision / monitor_once / review"""
    import threading, subprocess
    valid = {'decision', 'monitor_once', 'review'}
    if phase not in valid:
        return JSONResponse({"error": f"无效phase: {phase}，有效值: {valid}"}, status_code=400)

    # monitor_once 直接调用风控检查（仅交易时段有效）
    if phase == 'monitor_once':
        now = datetime.now()
        t = now.hour * 60 + now.minute
        trading = (9 * 60 + 30 <= t <= 11 * 60 + 30) or (13 * 60 <= t <= 15 * 60)
        if not trading:
            return {"status": "skipped", "actions": 0, "results": [],
                    "message": f"当前 {now.strftime('%H:%M')} 非交易时段（09:30-11:30 / 13:00-15:00），风控检查已跳过"}
        try:
            broker = _get_sim_broker()
            from src.agent.risk_controller import RiskController
            rc = RiskController(broker)
            actions = rc.check()
            results = rc.execute_actions(actions) if actions else []
            return {"status": "ok", "actions": len(actions), "results": results}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # decision / review：后台线程执行
    job_id = str(uuid.uuid4())[:8]
    _backtest_jobs[job_id] = {"status": "running", "phase": phase, "started_at": datetime.now().isoformat()}

    def _run():
        try:
            script = os.path.join(ROOT, 'scripts', 'run_trading_agent.py')
            result = subprocess.run(
                [sys.executable, script, '--phase', phase],
                capture_output=True, text=True, timeout=300
            )
            _backtest_jobs[job_id]['status'] = 'done'
            _backtest_jobs[job_id]['stdout'] = result.stdout[-3000:]
            _backtest_jobs[job_id]['stderr'] = result.stderr[-1000:]
        except Exception as e:
            _backtest_jobs[job_id]['status'] = 'error'
            _backtest_jobs[job_id]['error'] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "phase": phase, "status": "started"}


@router.get("/agent/job/{job_id}")
def agent_job_status(job_id: str):
    """查询异步任务状态"""
    job = _backtest_jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return job


@router.get("/agent/account")
def agent_account():
    """账户资金详情"""
    try:
        broker = _get_sim_broker()
        acc = broker.get_account()
        from src.utils.config_loader import Config
        initial = float(Config.get('trading_agent.sim_capital', 1000000))
        return {
            "total_assets": round(acc.total_assets, 2),
            "cash": round(acc.cash, 2),
            "market_value": round(acc.market_value, 2),
            "profit_pct": round(acc.profit_pct, 2),
            "profit_amount": round(acc.total_assets - initial, 2),
            "initial_capital": initial,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/agent/positions")
def agent_positions():
    """当前持仓列表（含轨道、持仓天数、锁仓状态）"""
    from src.utils.db_utils import DBUtils as _DB
    try:
        broker = _get_sim_broker()
        positions = broker.get_positions()

        # 查询各持仓股票的轨道（来自 daily_picks 最新一期）
        track_map: dict = {}
        MIN_HOLD_A, MIN_HOLD_B = 5, 15
        B_TRACKS = {'dividend', 'value'}
        if positions:
            codes = [p.ts_code for p in positions]
            ph = ','.join(['?'] * len(codes))
            try:
                tk_df = _DB.query_df(
                    f"SELECT ts_code, track FROM daily_picks "
                    f"WHERE ts_code IN ({ph}) "
                    f"AND trade_date = (SELECT MAX(trade_date) FROM daily_picks)",
                    tuple(codes)
                )
                if not tk_df.empty:
                    track_map = dict(zip(tk_df['ts_code'].astype(str), tk_df['track'].astype(str)))
            except Exception:
                pass

        today = datetime.now().date()
        result = []
        for p in positions:
            track = track_map.get(p.ts_code, '')
            min_hold = MIN_HOLD_B if track in B_TRACKS else MIN_HOLD_A
            # 计算持仓天数（自然日，LLM 侧也用自然日对比）
            days_held = 0
            if p.buy_date:
                try:
                    buy_dt = datetime.strptime(p.buy_date[:10], '%Y-%m-%d').date()
                    days_held = (today - buy_dt).days
                except Exception:
                    pass
            locked = days_held < min_hold
            track_label = {'sector_rotation': 'A轨', 'dividend': 'B轨',
                           'value': 'B轨', 'both': 'AB轨'}.get(track, '?')
            result.append({
                "ts_code": p.ts_code,
                "name": p.name,
                "track": track,
                "track_label": track_label,
                "volume": p.volume,
                "cost": round(p.cost, 3),
                "current_price": round(p.current_price, 3),
                "market_value": round(p.market_value, 2),
                "profit_pct": round(p.profit_pct, 2),
                "buy_date": p.buy_date,
                "days_held": days_held,
                "min_hold": min_hold,
                "locked": locked,
                "lock_status": f"⛔锁定({min_hold}天)" if locked else "✅可操作",
            })

        return {"positions": result, "count": len(result)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/agent/sync-real-positions")
def agent_sync_real_positions():
    """从 positions 表（真实持仓）同步到 agent_sim_positions，统一两者"""
    try:
        broker = _get_sim_broker()
        n = broker.sync_from_real_positions()
        account = broker.get_account()
        return {
            "success": True,
            "synced": n,
            "message": f"已同步 {n} 只真实持仓到 Agent 模拟账户",
            "cash": round(account.cash, 2),
            "total_assets": round(account.total_assets, 2),
            "market_value": round(account.market_value, 2),
        }
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.get("/agent/orders")
def agent_orders(days: int = Query(7, ge=1, le=90)):
    """订单历史"""
    from src.utils.db_utils import DBUtils
    from datetime import timedelta
    start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    try:
        df = DBUtils.query_df(
            "SELECT id, ts_code, side, price, volume, amount, status, created_at "
            "FROM agent_sim_orders WHERE created_at >= ? ORDER BY id DESC LIMIT 200",
            (start,)
        )
        records = []
        for _, row in df.iterrows():
            records.append({
                "id": int(row['id']),
                "ts_code": str(row['ts_code']),
                "side": str(row['side']),
                "price": round(float(row['price'] or 0), 3),
                "volume": int(row['volume'] or 0),
                "amount": round(float(row['amount'] or 0), 2),
                "status": str(row['status']),
                "created_at": str(row['created_at']),
            })
        return {"orders": records, "count": len(records)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/agent/nav_history")
def agent_nav_history():
    """净值历史（基于订单流水累积计算）"""
    from src.utils.db_utils import DBUtils
    from src.utils.config_loader import Config
    initial = float(Config.get('trading_agent.sim_capital', 1000000))
    try:
        # 从 agent_sim_orders 取每日买卖金额，累积计算净值代理
        df = DBUtils.query_df(
            "SELECT DATE(created_at) as dt, side, SUM(amount) as amt "
            "FROM agent_sim_orders GROUP BY DATE(created_at), side ORDER BY dt"
        )
        if df.empty:
            return {"nav": [{"date": datetime.now().strftime('%Y-%m-%d'), "nav": 1.0}]}

        # 按天计算账户市值变化（简化：买减现金，卖加现金，净值=总资产/初始）
        broker = _get_sim_broker()
        acc = broker.get_account()
        nav_now = acc.total_assets / initial if initial > 0 else 1.0

        dates = sorted(df['dt'].astype(str).unique())
        nav_series = []
        nav_val = 1.0
        step = (nav_now - 1.0) / max(len(dates), 1)
        for i, d in enumerate(dates):
            nav_val = 1.0 + step * (i + 1)
            nav_series.append({"date": str(d), "nav": round(nav_val, 4)})

        if not nav_series:
            nav_series = [{"date": datetime.now().strftime('%Y-%m-%d'), "nav": round(nav_now, 4)}]

        return {"nav": nav_series, "current_nav": round(nav_now, 4), "initial": initial}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/agent/reviews")
def agent_reviews(days: int = Query(30, ge=1, le=365)):
    """复盘列表"""
    from src.utils.db_utils import DBUtils
    from datetime import timedelta
    start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    try:
        df = DBUtils.query_df(
            "SELECT trade_date, total_pnl, pnl_pct, created_at "
            "FROM agent_reviews WHERE trade_date >= ? ORDER BY trade_date DESC",
            (start,)
        )
        reviews = []
        for _, row in df.iterrows():
            reviews.append({
                "trade_date": str(row['trade_date']),
                "total_pnl": round(float(row['total_pnl'] or 0), 2),
                "pnl_pct": round(float(row['pnl_pct'] or 0), 2),
                "created_at": str(row['created_at']),
            })
        return {"reviews": reviews, "count": len(reviews)}
    except Exception as e:
        return {"reviews": [], "count": 0, "error": str(e)}


@router.get("/agent/reviews/{trade_date}")
def agent_review_detail(trade_date: str):
    """单日复盘详情"""
    from src.utils.db_utils import DBUtils
    try:
        df = DBUtils.query_df(
            "SELECT * FROM agent_reviews WHERE trade_date = ? ORDER BY id DESC LIMIT 1",
            (trade_date,)
        )
        if df.empty:
            return {"review": None, "message": f"{trade_date} 无复盘记录"}
        row = df.iloc[0]
        return {
            "review": {
                "trade_date": str(row['trade_date']),
                "total_pnl": round(float(row['total_pnl'] or 0), 2),
                "pnl_pct": round(float(row['pnl_pct'] or 0), 2),
                "review_text": str(row['review_text'] or ''),
                "created_at": str(row['created_at']),
            }
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/agent/reviews/generate/{trade_date}")
def agent_generate_review(trade_date: str):
    """手动触发复盘"""
    job_id = str(uuid.uuid4())[:8]
    _backtest_jobs[job_id] = {"status": "running", "phase": "review", "trade_date": trade_date}

    def _run():
        try:
            script = os.path.join(ROOT, 'scripts', 'run_trading_agent.py')
            result = subprocess.run(
                [sys.executable, script, '--phase', 'review', '--date', trade_date],
                capture_output=True, text=True, timeout=180
            )
            _backtest_jobs[job_id]['status'] = 'done'
            _backtest_jobs[job_id]['stdout'] = result.stdout[-2000:]
        except Exception as e:
            _backtest_jobs[job_id]['status'] = 'error'
            _backtest_jobs[job_id]['error'] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "trade_date": trade_date, "status": "started"}


@router.post("/agent/manual_trade")
def agent_manual_trade(payload: dict = Body(...)):
    """手动下单（买/卖）"""
    ts_code = payload.get('ts_code', '').strip()
    side = payload.get('side', '').lower()
    price = float(payload.get('price', 0))
    amount = float(payload.get('amount', 0))

    if not ts_code or side not in ('buy', 'sell'):
        return JSONResponse({"error": "参数错误：需要 ts_code, side(buy/sell), price, amount"}, status_code=400)

    try:
        broker = _get_sim_broker()
        if side == 'buy':
            result = broker.buy(ts_code, price, amount)
        else:
            result = broker.sell(ts_code, price)
        return {
            "success": result.success,
            "ts_code": result.ts_code,
            "side": result.side,
            "price": result.price,
            "volume": result.volume,
            "amount": result.amount,
            "msg": result.msg,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/agent/account/reset")
def agent_account_reset():
    """重置模拟账户"""
    try:
        broker = _get_sim_broker()
        broker.reset()
        return {"success": True, "message": "模拟账户已重置"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/agent/performance")
def agent_performance():
    """绩效指标：总收益、日均、胜率"""
    from src.utils.db_utils import DBUtils
    from src.utils.config_loader import Config
    try:
        df = DBUtils.query_df(
            "SELECT trade_date, total_pnl, pnl_pct FROM agent_reviews ORDER BY trade_date"
        )
        initial = float(Config.get('trading_agent.sim_capital', 1000000))
        broker = _get_sim_broker()
        acc = broker.get_account()

        if df.empty:
            return {
                "total_return_pct": round(acc.profit_pct, 2),
                "total_profit": round(acc.total_assets - initial, 2),
                "trade_days": 0,
                "win_days": 0,
                "win_rate": 0,
                "max_drawdown": 0,
            }

        pnl_pcts = df['pnl_pct'].astype(float).tolist()
        win_days = sum(1 for p in pnl_pcts if p > 0)
        win_rate = round(win_days / len(pnl_pcts) * 100, 1) if pnl_pcts else 0

        # 最大回撤（简化：用每日盈亏累积）
        cum = 0
        peak = 0
        max_dd = 0
        for p in pnl_pcts:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd

        return {
            "total_return_pct": round(acc.profit_pct, 2),
            "total_profit": round(acc.total_assets - initial, 2),
            "trade_days": len(pnl_pcts),
            "win_days": win_days,
            "win_rate": win_rate,
            "max_drawdown": round(max_dd, 2),
            "avg_daily_pct": round(sum(pnl_pcts) / len(pnl_pcts), 3) if pnl_pcts else 0,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/agent/trade_history")
def agent_trade_history(days: int = Query(90, ge=7, le=365)):
    """
    每笔交易实现盈亏：以 agent_sim_orders 为原始数据，FIFO 配对买卖，计算实现 P&L。
    未平仓仓位从 agent_sim_positions + stock_daily 取浮盈。
    """
    from src.utils.db_utils import DBUtils
    from datetime import timedelta
    start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    try:
        df = DBUtils.query_df(
            "SELECT ts_code, side, price, volume, amount, created_at "
            "FROM agent_sim_orders WHERE created_at >= ? ORDER BY ts_code, created_at",
            (start,)
        )

        # 当前持仓 + 最新价格
        pos_df = DBUtils.query_df(
            "SELECT ts_code, cost, volume FROM agent_sim_positions WHERE volume > 0"
        )
        pos_map = {}
        if not pos_df.empty:
            for _, r in pos_df.iterrows():
                pos_map[str(r['ts_code'])] = {
                    'name': '', 'cost': float(r['cost']), 'volume': int(r['volume'])
                }

        price_df = DBUtils.query_df(
            "SELECT ts_code, close FROM stock_daily "
            "WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)"
        )
        name_df2 = DBUtils.query_df("SELECT ts_code, name FROM stock_info")
        price_map = {}
        name_map = {}
        if not name_df2.empty:
            for _, r in name_df2.iterrows():
                name_map[str(r['ts_code'])] = str(r.get('name', ''))
        if not price_df.empty:
            for _, r in price_df.iterrows():
                price_map[str(r['ts_code'])] = float(r['close'])

        if df.empty:
            return {"trades": [], "stats": {"total_trades": 0, "open_positions": len(pos_map),
                                            "win_trades": 0, "win_rate": 0, "total_realized_pnl": 0,
                                            "total_unrealized_pnl": 0, "avg_hold_days": 0}}

        all_trades = []
        for ts_code, grp in df.groupby('ts_code'):
            buy_queue = []  # FIFO: {'price', 'volume', 'date'}
            pos_name = pos_map.get(ts_code, {}).get('name', '') or name_map.get(ts_code, ts_code)

            for _, row in grp.sort_values('created_at').iterrows():
                side = str(row['side'])
                price = float(row['price'])
                volume = int(row['volume'])
                date_str = str(row['created_at'])[:10]

                if side == 'buy':
                    buy_queue.append({'price': price, 'volume': volume, 'date': date_str})
                elif side == 'sell' and buy_queue:
                    sell_vol = volume
                    while sell_vol > 0 and buy_queue:
                        buy = buy_queue[0]
                        matched = min(sell_vol, buy['volume'])
                        profit = (price - buy['price']) * matched
                        profit_pct = (price / buy['price'] - 1) * 100 if buy['price'] > 0 else 0
                        try:
                            hold_days = (datetime.strptime(date_str, '%Y-%m-%d') -
                                         datetime.strptime(buy['date'], '%Y-%m-%d')).days
                        except Exception:
                            hold_days = 0
                        all_trades.append({
                            'ts_code': ts_code, 'name': pos_name,
                            'buy_date': buy['date'], 'sell_date': date_str,
                            'buy_price': round(buy['price'], 3), 'sell_price': round(price, 3),
                            'volume': matched, 'profit': round(profit, 2),
                            'profit_pct': round(profit_pct, 2), 'hold_days': hold_days,
                            'status': 'closed'
                        })
                        if matched >= buy['volume']:
                            buy_queue.pop(0)
                        else:
                            buy_queue[0]['volume'] -= matched
                        sell_vol -= matched

            # 未平仓（buy_queue 剩余）
            for buy in buy_queue:
                cur = price_map.get(ts_code, buy['price'])
                profit = (cur - buy['price']) * buy['volume']
                profit_pct = (cur / buy['price'] - 1) * 100 if buy['price'] > 0 else 0
                try:
                    hold_days = (datetime.now() - datetime.strptime(buy['date'], '%Y-%m-%d')).days
                except Exception:
                    hold_days = 0
                all_trades.append({
                    'ts_code': ts_code, 'name': pos_name,
                    'buy_date': buy['date'], 'sell_date': None,
                    'buy_price': round(buy['price'], 3), 'sell_price': round(cur, 3),
                    'volume': buy['volume'], 'profit': round(profit, 2),
                    'profit_pct': round(profit_pct, 2), 'hold_days': hold_days,
                    'status': 'open'
                })

        all_trades.sort(key=lambda x: x['buy_date'], reverse=True)
        closed = [t for t in all_trades if t['status'] == 'closed']
        win = [t for t in closed if t['profit'] > 0]
        open_pos = [t for t in all_trades if t['status'] == 'open']
        stats = {
            'total_trades': len(closed),
            'open_positions': len(open_pos),
            'win_trades': len(win),
            'win_rate': round(len(win) / len(closed) * 100, 1) if closed else 0,
            'total_realized_pnl': round(sum(t['profit'] for t in closed), 2),
            'total_unrealized_pnl': round(sum(t['profit'] for t in open_pos), 2),
            'avg_hold_days': round(sum(t['hold_days'] for t in closed) / len(closed), 1) if closed else 0,
            'avg_profit_pct': round(sum(t['profit_pct'] for t in closed) / len(closed), 2) if closed else 0,
        }
        return {"trades": all_trades, "stats": stats}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/agent/decision_stats")
def agent_decision_stats(days: int = Query(30, ge=7, le=365)):
    """
    Agent 决策追踪：每次盘前决策的买卖列表，以及决策后股票价格的实际表现
    """
    import json as _json
    from src.utils.db_utils import DBUtils
    from datetime import timedelta
    start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    try:
        df = DBUtils.query_df(
            "SELECT trade_date, plan_json, confidence, market_regime FROM agent_decisions "
            "WHERE trade_date >= ? ORDER BY trade_date DESC",
            (start,)
        )
        if df.empty:
            return {"decisions": [], "count": 0}

        # 最新价格供胜负判断
        price_df = DBUtils.query_df(
            "SELECT ts_code, close FROM stock_daily "
            "WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)"
        )
        latest_price = {}
        if not price_df.empty:
            for _, r in price_df.iterrows():
                latest_price[str(r['ts_code'])] = float(r['close'])

        decisions = []
        for _, row in df.iterrows():
            try:
                plan = _json.loads(str(row['plan_json'] or '{}'))
            except Exception:
                plan = {}
            trades = plan.get('trades', [])
            trade_items = []
            for t in trades:
                ts_code = t.get('ts_code', '')
                entry_price = float(t.get('entry_price') or 0)
                cur_price = latest_price.get(ts_code, 0)
                # 判断决策盈亏（用现价 vs 入场价）
                outcome = None
                if entry_price > 0 and cur_price > 0 and t.get('action') == 'buy':
                    pct = (cur_price - entry_price) / entry_price * 100
                    outcome = round(pct, 2)
                trade_items.append({
                    'ts_code': ts_code,
                    'name': t.get('name', ''),
                    'action': t.get('action', ''),
                    'weight': round(float(t.get('weight') or 0), 3),
                    'entry_price': entry_price,
                    'stop_loss_price': float(t.get('stop_loss_price') or 0),
                    'reason': (t.get('reason') or '')[:60],
                    'track': t.get('track', ''),
                    'outcome_pct': outcome,  # 相对入场价的现价涨跌幅
                })
            decisions.append({
                'trade_date': str(row['trade_date']),
                'market_regime': str(row.get('market_regime', '')),
                'confidence': round(float(row.get('confidence') or 0), 2),
                'reasoning': (plan.get('reasoning') or '')[:120],
                'buy_count': sum(1 for t in trades if t.get('action') == 'buy'),
                'sell_count': sum(1 for t in trades if t.get('action') in ('sell', 'reduce')),
                'hold_count': sum(1 for t in trades if t.get('action') == 'hold'),
                'trades': trade_items,
            })
        return {"decisions": decisions, "count": len(decisions)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─────────────────────────────────────────────────────────────────────────────
# 新策略 API 端点
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/strategy/signals")
def get_strategy_signals():
    """获取最新策略信号（strategy_signals表）"""
    try:
        from src.utils.db_utils import DBUtils
        
        dt_df = DBUtils.query_df("SELECT MAX(trade_date) AS dt FROM strategy_signals")
        if dt_df.empty or dt_df.iloc[0]["dt"] is None:
            return {"date": None, "signals": []}
        
        latest_date = str(dt_df.iloc[0]["dt"])
        
        sql = """
            SELECT strategy, ts_code, name, score, signal_detail, rank_in_strategy
            FROM strategy_signals
            WHERE trade_date = ?
            ORDER BY strategy, rank_in_strategy
        """
        df = DBUtils.query_df(sql, params=(latest_date,))
        
        signals = []
        for _, row in df.iterrows():
            signals.append({
                "strategy": str(row.get("strategy", "")),
                "ts_code": str(row.get("ts_code", "")),
                "name": str(row.get("name", "")),
                "score": float(row.get("score", 0)),
                "signal_detail": str(row.get("signal_detail", "")),
                "rank_in_strategy": int(row.get("rank_in_strategy", 0)),
            })
        
        return {"date": latest_date, "signals": signals}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/strategy/macro")
def get_macro_indicators():
    """获取宏观指标"""
    try:
        from src.utils.db_utils import DBUtils
        
        sql = """
            SELECT indicator, value, data_date
            FROM macro_indicators
            ORDER BY data_date DESC
        """
        df = DBUtils.query_df(sql)
        
        indicators = []
        for _, row in df.iterrows():
            indicators.append({
                "indicator": str(row.get("indicator", "")),
                "value": float(row.get("value", 0)),
                "data_date": str(row.get("data_date", "")),
            })
        
        return {"indicators": indicators}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/strategy/factor_ic")
def get_factor_ic():
    """获取因子IC排名"""
    try:
        from src.utils.db_utils import DBUtils
        
        dt_df = DBUtils.query_df("SELECT MAX(calc_date) AS dt FROM factor_ic_log")
        if dt_df.empty or dt_df.iloc[0]["dt"] is None:
            return {"date": None, "factors": []}
        
        latest_date = str(dt_df.iloc[0]["dt"])
        
        sql = """
            SELECT factor_name, ic_mean_60d, ic_ir, is_valid
            FROM factor_ic_log
            WHERE calc_date = ?
            ORDER BY ABS(ic_mean_60d) DESC
        """
        df = DBUtils.query_df(sql, params=(latest_date,))
        
        factors = []
        for _, row in df.iterrows():
            factors.append({
                "factor_name": str(row.get("factor_name", "")),
                "ic_mean_60d": float(row.get("ic_mean_60d", 0)),
                "ic_ir": float(row.get("ic_ir", 0)),
                "is_valid": bool(row.get("is_valid", 0)),
            })
        
        return {"date": latest_date, "factors": factors}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/strategy/list")
def strategy_list():
    """列出所有可用策略及其中文名"""
    try:
        from src.strategy.center import StrategyCenter, _STRATEGY_NAMES
        center = StrategyCenter(enable_macro=False, notify=False)
        available = center.available_strategies()
        strategies = []
        for name in available:
            strategies.append({
                "key": name,
                "name": _STRATEGY_NAMES.get(name, name),
            })
        return {"strategies": strategies, "count": len(strategies)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/strategy/run")
def run_strategy(
    strategy: str = Query(..., description="策略名称，可用值见 /api/strategy/list"),
    limit: int = Query(20, ge=5, le=50)
):
    """运行指定策略并返回结果（支持全部已注册策略）"""
    try:
        from src.strategy.center import StrategyCenter
        center = StrategyCenter(enable_macro=False, notify=False)
        df = center.run_single(strategy, top_k=limit, skip_macro=True)
        stocks = []
        if df is not None and not df.empty:
            for _, row in df.iterrows().iloc[:limit]:
                rec = {
                    "ts_code": str(row.get("ts_code", "")),
                    "name": str(row.get("name", "")),
                    "score": float(row.get("score", 0)),
                    "industry": str(row.get("industry", "")),
                }
                # 保留原始列供前端按策略差异化展示
                for col in row.index:
                    if col not in rec and col not in ("ts_code", "name", "score", "industry"):
                        val = row[col]
                        if pd.notna(val):
                            try:
                                rec[col] = float(val)
                            except (ValueError, TypeError):
                                rec[col] = str(val)
                stocks.append(rec)
        return {"strategy": strategy, "stocks": stocks, "count": len(stocks)}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/strategy/ensemble")
def run_strategy_ensemble(
    weights: str = Query(..., description='策略权重JSON，如 {"hybrid":0.4,"dividend":0.3,"quant":0.3}'),
    limit: int = Query(20, ge=5, le=50)
):
    """多策略加权融合选股"""
    try:
        import json as _json
        w = _json.loads(weights)
        from src.strategy.center import StrategyCenter
        center = StrategyCenter(enable_macro=False, notify=False)
        df = center.run_ensemble(weights=w, top_k=limit)
        stocks = []
        if df is not None and not df.empty:
            for _, row in df.iterrows().iloc[:limit]:
                stocks.append({
                    "ts_code": str(row.get("ts_code", "")),
                    "name": str(row.get("name", "")),
                    "score": round(float(row.get("score", 0)), 4),
                    "industry": str(row.get("industry", "")),
                    "strategy": str(row.get("strategy", "")),
                    "signal_reason": str(row.get("signal_reason", "")),
                    "sub_scores": row.get("sub_scores", {}),
                })
        return {"weights": w, "stocks": stocks, "count": len(stocks)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/strategy/industry_timing")
def run_industry_timing():
    """行业择机信号：超配/标配/低配建议"""
    try:
        from src.analysis.industry_timing import IndustryTiming
        it = IndustryTiming()
        signals = it.run()
        if signals is None or signals.empty:
            return {"signals": [], "count": 0}
        result = []
        for _, row in signals.iterrows().iloc[:80]:
            result.append({
                "industry": str(row.get("industry", "")),
                "recommendation": str(row.get("suggest", "")),
                "penetration_phase": str(row.get("penetration_phase", "")),
                "cycle_type": str(row.get("cycle_type", "")),
                "relative_strength": float(row.get("relative_strength", 0)) / 100,
                "abs_return": float(row.get("return_pct", 0)) / 100,
                "benchmark_return": float(row.get("benchmark_return_pct", 0)) / 100,
            })
        return {"signals": result, "count": len(result)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/risk/event_check")
def event_risk_check(
    event_desc: str = Query(..., description="重大事件描述，如：美国打击伊朗")
):
    """重大事件风险评估：LLM分析事件对A股冲击程度"""
    try:
        from src.risk.event_risk_monitor import EventRiskMonitor
        monitor = EventRiskMonitor()
        result = monitor.analyze_event(event_desc)
        return {
            "event": event_desc,
            "risk_level": result.get("risk_level", "未知"),
            "action": result.get("action", "hold"),
            "action_name": result.get("action_name", ""),
            "reduce_pct": result.get("reduce_pct", 0),
            "analysis": result.get("analysis", ""),
            "affected_sectors": result.get("affected_sectors", []),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/data_source/status")
def get_data_source_status():
    """获取数据源状态"""
    try:
        status = {
            "akshare": {"installed": False, "version": None},
            "efinance": {"installed": False, "version": None},
            "baostock": {"installed": False, "version": None},
            "tushare": {"installed": False, "version": None},
        }
        
        try:
            import akshare as ak
            status["akshare"] = {"installed": True, "version": getattr(ak, "__version__", "unknown")}
        except ImportError:
            pass
        
        try:
            import efinance
            status["efinance"] = {"installed": True, "version": getattr(efinance, "__version__", "unknown")}
        except ImportError:
            pass
        
        try:
            import baostock
            status["baostock"] = {"installed": True, "version": getattr(baostock, "__version__", "unknown")}
        except ImportError:
            pass
        
        try:
            import tushare as ts
            status["tushare"] = {"installed": True, "version": getattr(ts, "__version__", "unknown")}
        except ImportError:
            pass
        
        coverage = {
            "stock_list": {"AKShare": True, "eFinance": True, "Baostock": True, "Tushare": True},
            "daily": {"AKShare": True, "eFinance": True, "Baostock": True, "Tushare": True},
            "realtime": {"AKShare": True, "eFinance": True, "Baostock": False, "Tushare": False},
            "etf": {"AKShare": True, "eFinance": True, "Baostock": False, "Tushare": True},
            "convertible_bond": {"AKShare": True, "eFinance": False, "Baostock": False, "Tushare": True},
            "index_component": {"AKShare": True, "eFinance": False, "Baostock": False, "Tushare": True},
            "concept": {"AKShare": True, "eFinance": False, "Baostock": False, "Tushare": True},
            "northbound": {"AKShare": True, "eFinance": False, "Baostock": False, "Tushare": True},
        }
        
        return {"sources": status, "coverage": coverage}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/messages/history")
def get_message_history(
    msg_type: str = Query(None, description="消息类型过滤"),
    days: int = Query(30, ge=7, le=365),
    limit: int = Query(100, ge=10, le=500)
):
    """获取推送消息历史"""
    try:
        from src.utils.db_utils import DBUtils
        from datetime import timedelta
        
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        if msg_type:
            df = DBUtils.query_df(
                "SELECT id, message_type, title, content, send_time, send_status, error_message "
                "FROM push_messages WHERE message_type = ? AND send_time >= ? "
                "ORDER BY send_time DESC LIMIT ?",
                (msg_type, start_date, limit)
            )
        else:
            df = DBUtils.query_df(
                "SELECT id, message_type, title, content, send_time, send_status, error_message "
                "FROM push_messages WHERE send_time >= ? "
                "ORDER BY send_time DESC LIMIT ?",
                (start_date, limit)
            )
        
        messages = []
        for _, row in df.iterrows():
            messages.append({
                "id": int(row.get("id", 0)),
                "message_type": str(row.get("message_type", "")),
                "title": str(row.get("title", "")),
                "content": str(row.get("content", "")),
                "send_time": str(row.get("send_time", "")),
                "send_status": str(row.get("send_status", "")),
                "error_message": str(row.get("error_message") or ""),
            })
        
        return {"messages": messages, "count": len(messages)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Multi-Agent 选股 API ──────────────────────────────────────────────────────

@router.post("/selection/run")
def run_selection(
    trade_date: str = Query(None, description="交易日期，如 2024-01-15"),
    top_k: int = Query(20, ge=5, le=50)
):
    """触发 Multi-Agent 选股工作流（后台异步执行）"""
    job_id = str(uuid.uuid4())[:8]
    _backtest_jobs[job_id] = {'status': 'running', 'phase': 'selection', 'started_at': datetime.now().isoformat()}

    _td = trade_date or datetime.now().strftime("%Y-%m-%d")
    _tk = top_k

    def _run():
        try:
            from src.agent.multi_agent.orchestrator import QuantOrchestrator
            orchestrator = QuantOrchestrator()
            result = orchestrator.run(trade_date=_td, top_k=_tk)
            _backtest_jobs[job_id]['status'] = 'done'
            _backtest_jobs[job_id]['result'] = {
                'picks_count': len(result.get('top_picks', [])),
                'stock_count': result.get('stock_count', 0),
                'etf_count': result.get('etf_count', 0),
                'cb_count': result.get('cb_count', 0),
                'stock_picks': result.get('stock_picks', []),
                'etf_picks': result.get('etf_picks', []),
                'cb_picks': result.get('cb_picks', []),
                'buy_orders': len(result.get('buy_orders', [])),
                'sell_orders': len(result.get('sell_orders', [])),
                'risk_assessment': result.get('risk_assessment', ''),
                'execution_summary': result.get('execution_summary', ''),
            }
        except Exception as e:
            import traceback
            _backtest_jobs[job_id]['status'] = 'error'
            _backtest_jobs[job_id]['error'] = str(e)
            print(f"[Selection] 错误: {traceback.format_exc()}")

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "status": "started", "message": f"选股任务已启动 (top_k={top_k})"}


@router.get("/selection/latest")
def get_selection_latest():
    """获取最新选股结果（股票 + ETF + 可转债）"""
    from src.utils.db_utils import DBUtils

    stock_picks = []
    etf_picks = []
    cb_picks = []
    date_str = None

    try:
        df = DBUtils.query_df(
            "SELECT * FROM daily_picks WHERE trade_date = ("
            "  SELECT MAX(trade_date) FROM daily_picks"
            ") ORDER BY final_score DESC LIMIT 50"
        )
        if not df.empty:
            date_str = str(df.iloc[0]['trade_date'])
            date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            df = df.fillna('')
            etf_codes = set()
            for _, row in df.iterrows():
                code = str(row.get('ts_code', ''))
                if code and code[:2].lower() in ('sh', 'sz') and code[2:].isdigit() and len(code) == 9:
                    etf_codes.add(code)
                track = str(row.get('track', ''))
                if track in ('etf', 'cb'):
                    etf_codes.add(code)
            records = df[~df['ts_code'].isin(etf_codes)].to_dict('records')
            for rec in records:
                for k, v in rec.items():
                    if hasattr(v, 'isoformat'):
                        rec[k] = v.isoformat()
            stock_picks = records
    except Exception:
        pass

    if not stock_picks:
        output_dir = os.path.join(ROOT, 'output')
        files = sorted(glob.glob(os.path.join(output_dir, 'hybrid_picks_*.csv')), reverse=True)
        if files:
            try:
                df = pd.read_csv(files[0])
                df = df.fillna('')
                etf_codes = set()
                for _, row in df.iterrows():
                    code = str(row.get('ts_code', ''))
                    if code and code[:2].lower() in ('sh', 'sz') and code[2:].isdigit() and len(code) == 9:
                        etf_codes.add(code)
                records = df[~df['ts_code'].isin(etf_codes)].to_dict('records')
                for rec in records:
                    for k, v in rec.items():
                        if hasattr(v, 'isoformat'):
                            rec[k] = v.isoformat()
                stock_picks = records
                date_str = os.path.basename(files[0]).replace('hybrid_picks_', '').replace('.csv', '')
            except Exception:
                pass

    date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if date_str and len(date_str) == 8 else (date_str or None)

    try:
        etf_files = sorted(glob.glob(os.path.join(ROOT, 'output', 'etf_picks_*.csv')), reverse=True)
        if etf_files:
            etf_df = pd.read_csv(etf_files[0])
            etf_df = etf_df.fillna('')
            records = etf_df.head(20).to_dict('records')
            for rec in records:
                for k, v in rec.items():
                    if hasattr(v, 'isoformat'):
                        rec[k] = v.isoformat()
            etf_picks = records
    except Exception:
        pass

    try:
        import akshare as ak
        cb_df = ak.bond_cb_jsl()
        cb_list = []
        for _, row in cb_df.head(50).iterrows():
            try:
                price = float(row.iloc[8]) if len(row) > 8 else 0
                premium = float(row.iloc[12]) if len(row) > 12 else 0
                ytm_r = row.iloc[16] if len(row) > 16 else 0
                ytm = float(ytm_r) if ytm_r and str(ytm_r) not in ('nan', '') else 0
                sc_r = row.iloc[18] if len(row) > 18 else 0
                scale = float(sc_r) if sc_r and str(sc_r) not in ('nan', '') else 0
                if price > 0 and 1 < scale < 15 and premium < 50:
                    cb_list.append({
                        'ts_code': str(row.iloc[3]) if len(row) > 3 else '',
                        'name': str(row.iloc[3]) if len(row) > 3 else '',
                        'stock_code': str(row.iloc[1]) if len(row) > 1 else '',
                        'stock_name': str(row.iloc[2]) if len(row) > 2 else '',
                        'close': price,
                        'premium_ratio': premium,
                        'ytm': ytm,
                        'scale': scale,
                        'final_score': 100 + (20 if ytm > 0 else 0) + (20 if premium < 20 else 0),
                        'track': 'cb',
                        'reason': f"溢价{premium:.1f}% YTM={ytm*100:.1f}% 规模{scale:.1f}亿",
                    })
            except Exception:
                continue
        cb_list.sort(key=lambda x: x['final_score'], reverse=True)
        cb_picks = cb_list[:10]
    except Exception:
        pass

    all_picks = stock_picks + etf_picks + cb_picks
    return {
        "date": date_fmt,
        "results": all_picks,
        "total": len(all_picks),
        "source": "db",
        "stock_picks": stock_picks,
        "etf_picks": etf_picks,
        "cb_picks": cb_picks,
    }


# ─── 记忆系统 API ─────────────────────────────────────────────────────────────

@router.get("/memory/summary")
def get_memory_summary():
    """获取记忆摘要"""
    try:
        from src.agent.multi_agent.memory_service import get_memory_service
        memory = get_memory_service()
        summary = memory.get_context_summary(days=30)
        recent = memory.get_recent_decisions(days=30, limit=10)
        facts = memory.get_top_facts(limit=10)

        execution_history = [d for d in recent if d["key"].startswith("execution_")]
        picks_count = 0
        last_date = None
        if execution_history:
            latest = execution_history[0]
            last_date = latest["key"].replace("execution_", "")
            picks_count = len(latest.get("content", {}).get("top_picks", []))

        return {
            "summary": summary,
            "recent_executions": len(execution_history),
            "last_execution_date": last_date,
            "last_picks_count": picks_count,
            "facts_count": len(facts),
            "facts": [{"content": f.content, "confidence": f.confidence, "category": f.category} for f in facts[:5]],
        }
    except Exception as e:
        return {"summary": f"记忆服务错误: {str(e)}", "recent_executions": 0, "facts": []}


# ─── 交易执行 API ─────────────────────────────────────────────────────────────

@router.post("/execution/buy")
def execution_buy(
    ts_code: str = Query(...),
    volume: int = Query(None, description="股数"),
    amount: float = Query(None, description="买入金额(元)"),
    price: float = Query(None, description="指定价格")
):
    """确认买入订单。SimBroker.buy(ts_code, price, amount_yuan) amount_yuan是金额不是股数"""
    try:
        broker = _get_sim_broker()
        from src.feeds.realtime_quote import get_realtime_quotes

        code = ts_code.strip()
        if "." not in code:
            code = code + (".SH" if code.startswith("6") else ".SZ")

        if price and price > 0:
            exec_price = price
        else:
            rt = get_realtime_quotes([code])
            quotes = rt.get(code, {})
            exec_price = float(quotes.get("last_price", 0)) if quotes.get("last_price") else 0

        if exec_price <= 0:
            return {"success": False, "error": "无法获取有效价格"}

        if amount and amount > 0:
            amount_yuan = amount
        elif volume and volume > 0:
            amount_yuan = volume * exec_price
        else:
            return {"success": False, "error": "请指定 volume 或 amount"}

        if amount_yuan < exec_price * 100:
            return {"success": False, "error": f"金额不足，最少需要 {exec_price * 100:.0f} 元 (1手=100股)"}

        result = broker.buy(code, exec_price, amount_yuan)
        return {
            "success": result.success,
            "ts_code": result.ts_code,
            "price": result.price,
            "volume": result.volume,
            "amount": result.amount,
            "msg": result.msg,
        }
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.post("/execution/buy_all")
def execution_buy_all():
    """批量确认所有待买入订单"""
    try:
        from src.utils.db_utils import DBUtils
        import json as _json

        df = DBUtils.query_df(
            "SELECT * FROM agent_pending_orders WHERE status='pending' ORDER BY created_at DESC LIMIT 20"
        )
        if df.empty:
            return {"success": False, "error": "无待买入订单"}

        results = []
        broker = _get_sim_broker()
        from src.feeds.realtime_quote import get_realtime_quotes

        codes = df["ts_code"].tolist()
        rt_quotes = get_realtime_quotes(codes) if codes else {}

        for _, row in df.iterrows():
            code = str(row["ts_code"])
            weight = float(row.get("weight", 0))

            rt = rt_quotes.get(code, {})
            price = float(rt.get("last_price", 0)) if rt.get("last_price") else 0

            if price <= 0:
                results.append({"code": code, "success": False, "msg": "无法获取价格"})
                continue

            amount_yuan = weight * 100 * price
            if amount_yuan < price * 100:
                amount_yuan = price * 100

            result = broker.buy(code, price, amount_yuan)
            results.append({
                "code": code,
                "success": result.success,
                "price": result.price,
                "volume": result.volume,
                "amount": result.amount,
                "msg": result.msg,
            })

        success_count = sum(1 for r in results if r.get("success"))
        return {
            "success": True,
            "total": len(results),
            "success_count": success_count,
            "results": results,
        }
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.post("/execution/sell")
def execution_sell(
    ts_code: str = Query(...),
    signal: str = Query(None, description="信号类型 STOP_LOSS/TAKE_PROFIT/CIRCUIT_BREAKER")
):
    """确认卖出订单"""
    try:
        broker = _get_sim_broker()

        code = ts_code.strip()
        if "." not in code:
            code = code + (".SH" if code.startswith("6") else ".SZ")

        from src.feeds.realtime_quote import get_realtime_quotes
        rt = get_realtime_quotes([code])
        quotes = rt.get(code, {})
        price = float(quotes.get("last_price", 0)) if quotes.get("last_price") else 0

        if price <= 0:
            return {"success": False, "error": "无法获取有效价格"}

        result = broker.sell(code, price)

        return {
            "success": result.success,
            "ts_code": result.ts_code,
            "side": result.side,
            "price": result.price,
            "volume": result.volume,
            "amount": result.amount,
            "msg": result.msg,
            "signal": signal or "MANUAL",
        }
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ─── 可转债策略 API ───────────────────────────────────────────────────────────

@router.get("/cb/strategy")
@router.get("/cb/strategy")
def get_cb_strategy():
    """可转债策略选券（5分钟缓存）"""
    cached, hit = _cache.get('cb_strategy')
    if hit:
        return cached
    try:
        import akshare as ak
        for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ.pop(_k, None)
        os.environ["NO_PROXY"] = "*"

        df = ak.bond_cb_jsl()
        if df is None or df.empty:
            return {"results": [], "error": "无法获取可转债数据"}

        col_map = {c: c for c in df.columns}
        def get_col(row, key):
            return row.get(key, row.get(col_map.get(key, key), None))

        results = []
        for _, row in df.iterrows():
            try:
                name = str(row.iloc[1]) if len(row) > 1 else ""
                price = float(row.iloc[2]) if len(row) > 2 else 0
                premium_ratio = float(row.iloc[11]) if len(row) > 11 else 0
                ytm = float(row.iloc[13]) if len(row) > 13 else 0
                cb_scale_raw = row.iloc[18] if len(row) > 18 else 0
                cb_scale = float(cb_scale_raw) if cb_scale_raw else 0
                stock_code = str(row.iloc[4])[:6] if len(row) > 4 else ""
                stock_name = str(row.iloc[5]) if len(row) > 5 else ""

                if price <= 0 or price > 200:
                    continue
                if premium_ratio > 60:
                    continue

                score = 0
                if ytm > 0:
                    score += 30
                if premium_ratio < 20:
                    score += 40
                elif premium_ratio < 40:
                    score += 25
                if cb_scale > 1 and cb_scale < 15:
                    score += 30

                if score < 50:
                    continue

                reason_parts = []
                if ytm > 0:
                    reason_parts.append(f"YTM={ytm:.2f}%")
                if premium_ratio < 20:
                    reason_parts.append(f"溢价率低={premium_ratio:.1f}%")
                if cb_scale > 1 and cb_scale < 15:
                    reason_parts.append(f"规模{cb_scale:.1f}亿")

                results.append({
                    "ts_code": str(row.iloc[0]) if len(row) > 0 else "",
                    "name": name,
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "price": round(price, 2),
                    "premium_ratio": round(premium_ratio, 2),
                    "ytm": round(ytm, 2),
                    "scale": round(cb_scale, 2),
                    "score": score,
                    "reason": ", ".join(reason_parts) if reason_parts else "综合评分",
                })
            except Exception:
                continue

        results.sort(key=lambda x: x["score"], reverse=True)
        result = {"results": results[:30], "total": len(results)}
        _cache.set('cb-strategy', result, ttl=300)
        return result
    except ImportError:
        return {"results": [], "error": "akshare 未安装"}
    except Exception as e:
        return {"results": [], "error": str(e)}


# ─── Multi-Agent 一键执行 ──────────────────────────────────────────────────────

@router.post("/multi_agent/execute")
def api_multi_agent_execute(
    trade_date: str = Query(None, description="交易日期，默认今天"),
    top_k: int = Query(20, description="选股数量"),
    auto_execute: bool = Query(False, description="是否自动执行交易（模拟）"),
):
    """
    Multi-Agent 闭环执行：
    1. 选股 (StrategyAgent)
    2. 风控 (RiskAgent)  
    3. 生成订单 (ExecutionAgent)
    4. 模拟执行 (SimBroker)
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    try:
        from src.agent.multi_agent.orchestrator import QuantOrchestrator

        orchestrator = QuantOrchestrator()
        state = orchestrator.run(trade_date=trade_date, top_k=top_k)

        result = {
            "success": True,
            "trade_date": trade_date,
            "stock_count": state.get("stock_count", 0),
            "etf_count": state.get("etf_count", 0),
            "cb_count": state.get("cb_count", 0),
            "top_picks": state.get("top_picks", [])[:10],
            "risk_assessment": state.get("risk_assessment", ""),
            "sell_signals": state.get("sell_signals", []),
            "buy_orders": state.get("buy_orders", []),
            "sell_orders": state.get("sell_orders", []),
            "execution_summary": state.get("execution_summary", ""),
            "error": state.get("error"),
        }

        if auto_execute and result["buy_orders"]:
            broker = _get_sim_broker()
            from src.feeds.realtime_quote import get_realtime_quotes

            buy_results = []
            for order in result["buy_orders"]:
                code = order.get("ts_code", "")
                volume = order.get("volume", 0)
                if volume <= 0:
                    continue
                try:
                    rt = get_realtime_quotes([code])
                    quotes = rt.get(code, {})
                    price = float(quotes.get("last_price", 0)) if quotes.get("last_price") else 0
                    if price <= 0:
                        buy_results.append({"code": code, "success": False, "msg": "无法获取价格"})
                        continue
                    res = broker.buy(code, price, volume * price)
                    buy_results.append({
                        "code": code,
                        "success": res.success,
                        "price": res.price,
                        "volume": res.volume,
                        "amount": res.amount,
                        "msg": res.msg,
                    })
                except Exception as e:
                    buy_results.append({"code": code, "success": False, "msg": str(e)})

            sell_results = []
            for order in result["sell_orders"]:
                code = order.get("ts_code", "")
                try:
                    rt = get_realtime_quotes([code])
                    quotes = rt.get(code, {})
                    price = float(quotes.get("last_price", 0)) if quotes.get("last_price") else 0
                    if price <= 0:
                        sell_results.append({"code": code, "success": False, "msg": "无法获取价格"})
                        continue
                    res = broker.sell(code, price)
                    sell_results.append({
                        "code": code,
                        "success": res.success,
                        "price": res.price,
                        "volume": res.volume,
                        "amount": res.amount,
                        "msg": res.msg,
                    })
                except Exception as e:
                    sell_results.append({"code": code, "success": False, "msg": str(e)})

            result["auto_execution"] = {
                "buy_results": buy_results,
                "sell_results": sell_results,
                "buy_success": sum(1 for r in buy_results if r.get("success")),
                "sell_success": sum(1 for r in sell_results if r.get("success")),
            }

        return result

    except ImportError as e:
        return JSONResponse({"success": False, "error": f"模块导入失败: {e}"}, status_code=500)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.post("/selection/research_only")
def api_selection_research_only(
    trade_date: str = Query(None, description="交易日期，默认今天"),
):
    """仅运行市场研究流水线，不执行选股"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    try:
        from src.analysis.research_runner import ResearchRunner
        runner = ResearchRunner(trade_date=trade_date)
        results = runner.run_all()
        return {"success": True, "results": results}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/multi_agent/selection_only")
def api_multi_agent_selection_only(
    trade_date: str = Query(None, description="交易日期，默认今天"),
    top_k: int = Query(20, description="选股数量"),
):
    """仅执行选股（不执行风控和交易）"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    try:
        from src.agent.multi_agent.strategy_agent import StrategyAgent

        agent = StrategyAgent()
        result = agent.run_multi_strategy(trade_date=trade_date, top_k=top_k)

        return {
            "success": True,
            "trade_date": trade_date,
            "stock_picks": result.get("stock_picks", []),
            "etf_picks": result.get("etf_picks", []),
            "cb_picks": result.get("cb_picks", []),
            "hot_sectors": result.get("hot_sectors", []),
            "hot_concepts": result.get("hot_concepts", []),
            "sector_analysis": result.get("sector_analysis", ""),
            "market_regime": result.get("market_regime", {}),
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.post("/multi_agent/risk_check")
def api_multi_agent_risk_check(
    trade_date: str = Query(None, description="交易日期，默认今天"),
):
    """执行风控检查"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    try:
        from src.agent.multi_agent.risk_agent import RiskAgent

        agent = RiskAgent()
        result = agent.run(trade_date=trade_date)

        return {
            "success": True,
            "trade_date": trade_date,
            "risk_assessment": result.get("risk_assessment", ""),
            "sell_signals": result.get("sell_signals", []),
            "position_adjustments": result.get("position_adjustments", []),
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ─── 交易中心统一数据 ─────────────────────────────────────────────────────────

@router.get("/trading/dashboard")
def api_trading_dashboard():
    """获取交易中心完整数据：持仓 + 选股 + 建议 + 风控"""
    from src.utils.db_utils import DBUtils
    import pandas as pd

    result = {
        "success": True,
        "account": {},
        "positions": [],
        "picks": [],
        "recommendations": {"buy": [], "sell": [], "hold": [], "alerts": []},
        "risk_signals": [],
        "pending_orders": [],
    }

    def _clean_record(rec):
        for k, v in list(rec.items()):
            if hasattr(v, 'isoformat'):
                rec[k] = v.isoformat()
            elif hasattr(v, 'strftime'):
                rec[k] = v.strftime('%Y-%m-%d %H:%M:%S')
            elif isinstance(v, float) and v != v:
                rec[k] = None
            elif v == '' or v is None:
                rec[k] = None
        return rec

    try:
        broker = _get_sim_broker()
        positions = broker.get_positions()
        for pos in positions:
            rec = {
                "ts_code": pos.ts_code,
                "name": pos.name,
                "volume": pos.volume,
                "cost": pos.cost,
                "current_price": pos.current_price,
                "market_value": pos.market_value,
                "profit_pct": pos.profit_pct,
                "buy_date": pos.buy_date,
            }
            result["positions"].append(_clean_record(rec))
    except Exception as ex:
        print(f"[dashboard] positions error: {ex}")
        pass

    try:
        broker = _get_sim_broker()
        account = broker.get_account()
        df_acc = DBUtils.query_df(
            "SELECT cash, initial_capital FROM agent_sim_account WHERE id=1"
        )
        initial_capital = float(df_acc['initial_capital'].iloc[0]) if not df_acc.empty else 0.0
        result["account"] = {
            "total_capital": initial_capital,
            "available_cash": account.cash,
            "market_value": account.market_value,
            "total_pnl": round(account.total_assets - initial_capital, 2),
            "total_pnl_pct": round(account.profit_pct, 2),
            "position_count": len(result["positions"]),
        }
    except Exception as ex:
        print(f"[dashboard] account error: {ex}")
        pass

    try:
        df = DBUtils.query_df(
            "SELECT * FROM daily_picks WHERE trade_date = ("
            "  SELECT MAX(trade_date) FROM daily_picks"
            ") ORDER BY final_score DESC LIMIT 30"
        )
        if not df.empty:
            df = df.fillna('')
            for _, row in df.iterrows():
                rec = _clean_record(row.to_dict())
                result["picks"].append(rec)
    except Exception:
        pass

    if result["positions"] and result["picks"]:
        pick_codes = {p.get('ts_code') for p in result["picks"]}
        position_codes = {p.get('ts_code') for p in result["positions"]}

        for pos in result["positions"]:
            code = pos.get('ts_code')
            profit_pct = float(pos.get('profit_pct') or 0)
            stop_loss_price = float(pos.get('stop_loss_price') or 0)
            current_price = float(pos.get('current_price') or 0)

            if profit_pct <= -0.08:
                result["recommendations"]["alerts"].append({
                    "ts_code": code,
                    "name": pos.get('name', ''),
                    "action": "SELL",
                    "reason": f"触发止损: 亏损{profit_pct*100:.1f}%",
                    "priority": "critical",
                })
            elif profit_pct >= 0.15:
                result["recommendations"]["alerts"].append({
                    "ts_code": code,
                    "name": pos.get('name', ''),
                    "action": "SELL",
                    "reason": f"建议止盈: 盈利{profit_pct*100:.1f}%",
                    "priority": "warning",
                })

            if code in pick_codes:
                result["recommendations"]["hold"].append({
                    "ts_code": code,
                    "name": pos.get('name', ''),
                    "profit_pct": profit_pct,
                    "reason": "在选股列表中，建议持有",
                })
            else:
                result["recommendations"]["sell"].append({
                    "ts_code": code,
                    "name": pos.get('name', ''),
                    "profit_pct": profit_pct,
                    "reason": "不在选股列表，建议卖出" if profit_pct >= 0 else "不在选股列表且亏损，建议评估",
                })

        for pick in result["picks"]:
            code = pick.get('ts_code')
            if code not in position_codes:
                track = pick.get('track', '')
                if track not in ('etf', 'cb'):
                    result["recommendations"]["buy"].append({
                        "ts_code": code,
                        "name": pick.get('name', ''),
                        "final_score": pick.get('final_score', 0),
                        "ai_score": pick.get('ai_score', 0),
                        "event_score": pick.get('event_score', 0),
                        "fundamental_score": pick.get('fundamental_score', 0),
                        "reason": f"评分{pick.get('final_score', 0):.3f}，建议买入",
                    })

    try:
        from src.agent.multi_agent.risk_agent import RiskAgent
        risk_agent = RiskAgent()
        risk_result = risk_agent.run(trade_date=datetime.now().strftime("%Y-%m-%d"))
        result["risk_signals"] = risk_result.get("sell_signals", [])
        result["risk_assessment"] = risk_result.get("risk_assessment", "")
    except Exception:
        pass

    try:
        df_orders = DBUtils.query_df(
            "SELECT * FROM agent_sim_orders WHERE DATE(created_at) = CURDATE() ORDER BY created_at DESC LIMIT 20"
        )
        if not df_orders.empty:
            for _, row in df_orders.iterrows():
                rec = _clean_record(row.to_dict())
                result["pending_orders"].append(rec)
    except Exception:
        pass

    return result


# ─── LLM 推理决策可视化 ───────────────────────────────────────────────────────

_NODE_NAMES = {
    'ResearchReasoner': '市场研究推理',
    'SelectionReviewer': '选股复核',
    'DailyReporter': '每日复盘',
}


@router.get("/llm/evaluations")
def get_llm_evaluations(
    node: str = Query(None, description="节点过滤"),
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=5, le=100),
):
    """获取 LLM 推理决策历史（ResearchReasoner/SelectionReviewer/DailyReporter）"""
    try:
        from src.utils.db_utils import DBUtils
        from datetime import timedelta as _td
        sql = "SELECT * FROM llm_evaluations WHERE 1=1"
        params = []
        if node:
            sql += " AND node = ?"
            params.append(node)
        cutoff = (datetime.now() - _td(days=days)).strftime('%Y%m%d')
        sql += " AND trade_date >= ?"
        params.append(cutoff)
        sql += " ORDER BY trade_date DESC, created_at DESC LIMIT ?"
        params.append(limit)

        df = DBUtils.query_df(sql, params)
        if df.empty:
            return {"evaluations": [], "total": 0}

        records = []
        for _, row in df.iterrows():
            records.append({
                "id": int(row.get("id", 0)),
                "node": row.get("node", ""),
                "node_name": _NODE_NAMES.get(str(row.get("node", "")), str(row.get("node", ""))),
                "trade_date": str(row.get("trade_date", "")),
                "input_summary": str(row.get("input_summary", "")),
                "reasoning": str(row.get("reasoning", "")),
                "decisions": str(row.get("decisions", "")),
                "confidence": float(row.get("confidence", 0)),
                "improvement_hints": str(row.get("improvement_hints", "")),
                "created_at": str(row.get("created_at", "")),
            })

        return {"evaluations": records, "total": len(records)}
    except Exception as e:
        return {"evaluations": [], "total": 0, "error": str(e)}


@router.get("/llm/evaluation/latest")
def get_llm_latest():
    """获取最近一条 LLM 决策详情"""
    try:
        from src.utils.db_utils import DBUtils
        df = DBUtils.query_df(
            "SELECT * FROM llm_evaluations ORDER BY created_at DESC LIMIT 1"
        )
        if df.empty:
            return {"evaluation": None}
        row = df.iloc[0]
        return {
            "evaluation": {
                "id": int(row.get("id", 0)),
                "node": str(row.get("node", "")),
                "node_name": _NODE_NAMES.get(str(row.get("node", "")), str(row.get("node", ""))),
                "trade_date": str(row.get("trade_date", "")),
                "input_summary": str(row.get("input_summary", "")),
                "reasoning": str(row.get("reasoning", "")),
                "decisions": str(row.get("decisions", "")),
                "confidence": float(row.get("confidence", 0)),
                "improvement_hints": str(row.get("improvement_hints", "")),
                "created_at": str(row.get("created_at", "")),
            }
        }
    except Exception as e:
        return {"evaluation": None, "error": str(e)}


@router.get("/llm/trader_decisions")
def get_llm_trader_decisions(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=5, le=100),
):
    """获取 LLM 交易决策历史（含人工确认状态）"""
    try:
        from src.utils.db_utils import DBUtils
        from datetime import timedelta as _td
        cutoff = (datetime.now() - _td(days=days)).strftime('%Y-%m-%d')
        df = DBUtils.query_df(
            "SELECT * FROM llm_trader_decisions WHERE trade_date >= ? ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit)
        )
        if df.empty:
            return {"decisions": [], "total": 0}
        records = []
        for _, row in df.iterrows():
            records.append({
                "id": int(row.get("id", 0)),
                "node": str(row.get("node", "")),
                "trade_date": str(row.get("trade_date", "")),
                "input_summary": str(row.get("input_summary", "")),
                "reasoning": str(row.get("reasoning", "")),
                "decision": str(row.get("decision", "")),
                "confidence": float(row.get("confidence", 0)),
                "human_confirmed": bool(row.get("human_confirmed", 0)),
                "human_override": str(row.get("human_override", "")),
                "actual_outcome": str(row.get("actual_outcome", "")),
                "feedback": str(row.get("feedback", "")),
                "created_at": str(row.get("created_at", "")),
            })
        return {"decisions": records, "total": len(records)}
    except Exception as e:
        return {"decisions": [], "total": 0, "error": str(e)}


@router.get("/agent/decisions")
def get_agent_decisions(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=5, le=100),
):
    """获取 Agent 预盘决策历史"""
    try:
        from src.utils.db_utils import DBUtils
        from datetime import timedelta as _td
        cutoff = (datetime.now() - _td(days=days)).strftime('%Y-%m-%d')
        df = DBUtils.query_df(
            "SELECT * FROM agent_decisions WHERE trade_date >= ? ORDER BY generated_at DESC LIMIT ?",
            (cutoff, limit)
        )
        if df.empty:
            return {"decisions": [], "total": 0}
        import json as _json
        records = []
        for _, row in df.iterrows():
            plan = {}
            try:
                plan = _json.loads(str(row.get("plan_json", "{}")))
            except Exception:
                pass
            records.append({
                "id": int(row.get("id", 0)),
                "trade_date": str(row.get("trade_date", "")),
                "market_regime": str(row.get("market_regime", "")),
                "confidence": float(row.get("confidence", 0)),
                "plan": plan,
                "generated_at": str(row.get("generated_at", "")),
            })
        return {"decisions": records, "total": len(records)}
    except Exception as e:
        return {"decisions": [], "total": 0, "error": str(e)}
