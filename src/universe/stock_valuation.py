"""
股票池批量估值 — 读取 qmt.company_financials + stock_info 做行业估值，
结果缓存到 valuation_cache 表（周级，7天内复用）。

公开接口
--------
run_valuation(force_refresh=False) -> pd.DataFrame
get_valuation_map()                -> dict[ts_code -> dict]
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from src.universe.valuation import valuate, verdict
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils

# 缓存有效期（天）
CACHE_TTL_DAYS = 7


# ─── 工具函数 ────────────────────────────────────────────────────────────────

def _f(v, default=None) -> Optional[float]:
    if v is None:
        return default
    try:
        fv = float(v)
        return default if (math.isnan(fv) or math.isinf(fv)) else fv
    except Exception:
        return default


def _qmt_conn():
    """直接连接 qmt 数据库（与 quant_trade 同实例，不同 DB）"""
    import pymysql
    mysql = Config.mysql if hasattr(Config, 'mysql') else {}
    return pymysql.connect(
        host=mysql.get('host', '192.168.3.41'),
        port=int(mysql.get('port', 3306)),
        user=mysql.get('user', 'root'),
        password=mysql.get('password', ''),
        database='qmt',
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=10,
    )


# ─── 缓存表管理 ─────────────────────────────────────────────────────────────

def _ensure_cache_table():
    DBUtils.execute("""
        CREATE TABLE IF NOT EXISTS valuation_cache (
            ts_code      VARCHAR(12) NOT NULL,
            calc_date    DATE NOT NULL,
            company_name VARCHAR(100),
            itype        VARCHAR(20),
            val_method   VARCHAR(30),
            upside_pct   DOUBLE,
            verdict      VARCHAR(20),
            val_detail   TEXT,
            PRIMARY KEY (ts_code)
        )
    """)


def _load_cache() -> Optional[pd.DataFrame]:
    """若缓存在 TTL 内则返回，否则返回 None。"""
    try:
        df = DBUtils.query_df(
            "SELECT * FROM valuation_cache ORDER BY upside_pct DESC"
        )
        if df.empty:
            return None
        # 检查缓存日期（取最早的一条判断整体是否过期）
        oldest = pd.to_datetime(df["calc_date"]).min().date()
        if (date.today() - oldest).days < CACHE_TTL_DAYS:
            return df
        return None
    except Exception:
        return None


def _nan_to_none(v):
    """将 float NaN / inf 转成 None，MySQL 不接受 NaN。"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return v


def _save_cache(rows: list[dict]):
    """批量 upsert valuation_cache。"""
    if not rows:
        return
    today = date.today().isoformat()
    ok = fail = 0
    for r in rows:
        try:
            DBUtils.execute(
                """
                INSERT INTO valuation_cache
                    (ts_code, calc_date, company_name, itype, val_method,
                     upside_pct, verdict, val_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    calc_date    = VALUES(calc_date),
                    company_name = VALUES(company_name),
                    itype        = VALUES(itype),
                    val_method   = VALUES(val_method),
                    upside_pct   = VALUES(upside_pct),
                    verdict      = VALUES(verdict),
                    val_detail   = VALUES(val_detail)
                """,
                params=[
                    r["ts_code"], today, r.get("company_name", ""),
                    r.get("itype", ""), r.get("val_method", ""),
                    _nan_to_none(r.get("upside_pct")),
                    r.get("verdict", "数据不足"),
                    r.get("val_detail", ""),
                ],
            )
            ok += 1
        except Exception as e:
            fail += 1
            if fail <= 3:
                print(f"[估值缓存] 写入失败 {r.get('ts_code')}: {e}")
    print(f"[估值缓存] 写入完成 ok={ok} fail={fail}")


# ─── 数据获取 ────────────────────────────────────────────────────────────────

def _fetch_qmt_financials() -> dict:
    """从 qmt.company_financials 拉全量财务数据，返回 ts_code → row dict。"""
    try:
        conn = _qmt_conn()
        cur  = conn.cursor()
        cur.execute("SELECT * FROM company_financials WHERE total_mv > 0")
        rows = cur.fetchall()
        conn.close()
        # qmt 用 stock_code 字段，格式与 quant_trade 一致（000001.SZ）
        return {r["stock_code"]: r for r in rows}
    except Exception as e:
        print(f"[估值] qmt.company_financials 读取失败: {e}")
        return {}


def _fetch_pool() -> pd.DataFrame:
    return DBUtils.query_df(
        """
        SELECT sp.ts_code, sp.company_name, sp.company_type, sp.sector,
               si.industry, si.pe_ttm, si.pb, si.total_mv
        FROM stock_pool sp
        LEFT JOIN stock_info si
            ON si.ts_code COLLATE utf8mb4_general_ci = sp.ts_code COLLATE utf8mb4_general_ci
        WHERE sp.is_active = 1
        """
    )


def _fetch_local_roe() -> dict:
    """从 stock_daily 取最新 ROE / netprofit_yoy，作为兜底。"""
    try:
        df = DBUtils.query_df(
            """
            SELECT sd.ts_code, sd.roe, sd.netprofit_yoy
            FROM stock_daily sd
            INNER JOIN (
                SELECT ts_code, MAX(trade_date) AS max_date
                FROM stock_daily GROUP BY ts_code
            ) t ON sd.ts_code = t.ts_code AND sd.trade_date = t.max_date
            """
        )
        return {r["ts_code"]: r.to_dict() for _, r in df.iterrows()}
    except Exception as e:
        print(f"[估值] stock_daily 读取失败: {e}")
        return {}


def _build_fin(si_row: dict, qmt_fin: Optional[dict], local_roe: Optional[dict]) -> dict:
    """
    构建 valuate() 所需的 fin dict。
    优先级：qmt.company_financials > stock_info + stock_daily
    """
    if qmt_fin and (_f(qmt_fin.get("total_mv"), 0) or 0) > 0:
        # qmt 数据完整，直接用（注意单位：qmt.total_mv 单位是万元还是元？）
        # 从 batch_pool_valuation.py 可知 qmt.total_mv 单位是万元（与 stock_info 相同）
        fin = dict(qmt_fin)
        # gross_margin / roe 在 qmt 里可能是百分比或小数，需要归一化
        gm = _f(fin.get("gross_margin"))
        if gm is not None and gm > 1:   fin["gross_margin"] = gm / 100
        roe = _f(fin.get("roe"))
        if roe is not None and roe > 1:  fin["roe"] = roe / 100
        nm = _f(fin.get("net_margin"))
        if nm is not None and nm > 1:    fin["net_margin"] = nm / 100
        yoy = _f(fin.get("netprofit_yoy"))
        if yoy is not None and abs(yoy) > 2: fin["netprofit_yoy"] = yoy / 100
        return fin

    # 兜底：从 stock_info + stock_daily 拼凑
    mv_wan = _f(si_row.get("total_mv"), 0)
    pe_ttm = _f(si_row.get("pe_ttm"))
    pb     = _f(si_row.get("pb"))

    # PE>80 或 PE<0 时反算净利润不可靠（亏损/高增长股），置为 None 触发"数据不足"
    if pe_ttm and 0 < pe_ttm <= 80 and mv_wan > 0:
        net_profit = mv_wan * 1e4 / pe_ttm   # 万元→元
    else:
        net_profit = None

    roe = 0.10
    yoy = None
    if local_roe:
        r = _f(local_roe.get("roe"))
        if r is not None:
            roe = r / 100 if abs(r) > 1 else r
        y = _f(local_roe.get("netprofit_yoy"))
        if y is not None:
            yoy = y / 100 if abs(y) > 2 else y

    return {
        "industry":      si_row.get("industry") or "",
        "pe_ttm":        pe_ttm,
        "pb":            pb,
        "total_mv":      mv_wan,
        "net_profit":    net_profit,
        "gross_margin":  0.20,
        "net_margin":    0.05,
        "roe":           roe,
        "netprofit_yoy": yoy,
        "revenue":       0.0,
        "ebitda":        None,
        "net_debt":      None,
        "rd_exp":        None,
        "dv_ratio":      0.0,
        "forecast_profit": None,
    }


# ─── 主流程 ─────────────────────────────────────────────────────────────────

def run_valuation(force_refresh: bool = False) -> pd.DataFrame:
    """
    批量估值全池股票，返回 DataFrame。
    force_refresh=True 时忽略缓存重新计算。
    """
    _ensure_cache_table()

    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            print(f"[估值] 使用缓存（{len(cached)} 只）")
            return cached

    print("[估值] 开始批量计算...")
    t0 = time.time()

    pool_df    = _fetch_pool()
    qmt_map    = _fetch_qmt_financials()
    local_map  = _fetch_local_roe()

    print(f"[估值] 股池 {len(pool_df)} 只，QMT财务 {len(qmt_map)} 只，耗时 {time.time()-t0:.1f}s")

    results = []
    ok = err = no_data = 0

    for _, si in pool_df.iterrows():
        code = si["ts_code"]
        name = si.get("company_name") or code

        try:
            qmt_fin   = qmt_map.get(code)
            local_roe = local_map.get(code)
            fin       = _build_fin(si.to_dict(), qmt_fin, local_roe)

            mv_yi = (_f(fin.get("total_mv"), 0) or 0) / 10000  # 万元 → 亿元

            # growth: 用于 adj_growth（BANK戈登/SEMICON PEG 等）
            # yoy 在 ±30% 内用真实值；>30% cap 到 30%；无数据给 0（不假设增速）
            raw_yoy = _f(fin.get("netprofit_yoy"))
            if raw_yoy is None:
                growth = 0.0
            elif raw_yoy > 0.30:
                growth = 0.30
            elif raw_yoy < -0.30:
                growth = max(raw_yoy, -0.50)
            else:
                growth = raw_yoy

            itype, method, upside, detail, _ = valuate(name, fin, growth, mv_yi, 0)
            vd = verdict(upside)

            results.append({
                "ts_code":      code,
                "company_name": name,
                "itype":        itype,
                "val_method":   method,
                "upside_pct":   round(upside, 1) if upside is not None else None,
                "verdict":      vd,
                "val_detail":   detail,
            })
            ok += 1

        except Exception as e:
            err += 1
            if err <= 5:
                print(f"[估值] ERR {code} {name}: {e}")
            results.append({
                "ts_code":      code,
                "company_name": name,
                "itype":        "",
                "val_method":   "",
                "upside_pct":   None,
                "verdict":      "数据不足",
                "val_detail":   "",
            })

    df = pd.DataFrame(results)

    # 按上行空间排序：数据不足放最后
    df["_sort"] = df["upside_pct"].fillna(-999)
    df = df.sort_values("_sort", ascending=False).drop(columns=["_sort"]).reset_index(drop=True)

    _save_cache(df.to_dict("records"))

    elapsed = time.time() - t0
    dist = df["verdict"].value_counts().to_dict()
    print(f"[估值] 完成 ok={ok} err={err}，耗时 {elapsed:.1f}s")
    print(f"[估值] 分布: {dist}")

    return df


def get_valuation_map(force_refresh: bool = False) -> dict:
    """
    返回 ts_code → {itype, val_method, upside_pct, verdict, val_detail} 的字典，
    供 HybridStrategy 直接查询。
    """
    try:
        df = run_valuation(force_refresh=force_refresh)
        return {
            r["ts_code"]: {
                "itype":      r.get("itype", ""),
                "val_method": r.get("val_method", ""),
                "upside_pct": r.get("upside_pct"),
                "verdict":    r.get("verdict", "数据不足"),
                "val_detail": r.get("val_detail", ""),
            }
            for _, r in df.iterrows()
        }
    except Exception as e:
        print(f"[估值] get_valuation_map 失败: {e}")
        return {}
