"""
patch_roe.py — 修补 stock_daily 最新行的 ROE / netprofit_yoy

数据优先级：
  1. qmt.company_financials  (当天实时，最准)
  2. Tushare fina_indicator  (季报，兜底)

运行方式：
  python scripts/patch_roe.py
  python scripts/patch_roe.py --dry-run   # 只打印不写库
  python scripts/patch_roe.py --source tushare   # 只用 Tushare
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql

from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils


# ─── 参数 ─────────────────────────────────────────────────────────────────────

def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--source", choices=["auto", "qmt", "tushare"], default="auto")
    p.add_argument("--limit", type=int, default=0, help="只处理前N只，0=全部（调试用）")
    return p.parse_args()


# ─── QMT 数据源 ───────────────────────────────────────────────────────────────

def _qmt_conn():
    mysql = Config.mysql if hasattr(Config, "mysql") else {}
    return pymysql.connect(
        host=mysql.get("host", "192.168.3.41"),
        port=int(mysql.get("port", 3306)),
        user=mysql.get("user", "root"),
        password=mysql.get("password", ""),
        database="qmt",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=10,
    )


def _fetch_qmt_roe() -> dict[str, dict]:
    """返回 ts_code -> {roe, netprofit_yoy}（qmt.company_financials）"""
    try:
        conn = _qmt_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT stock_code, roe, netprofit_yoy FROM company_financials WHERE total_mv > 0"
        )
        rows = cur.fetchall()
        conn.close()
        result = {}
        for r in rows:
            roe = _safe_float(r.get("roe"))
            yoy = _safe_float(r.get("netprofit_yoy"))
            # 归一化：若 >1 说明是百分比
            if roe is not None and abs(roe) > 1:
                roe = roe / 100
            if yoy is not None and abs(yoy) > 2:
                yoy = yoy / 100
            result[r["stock_code"]] = {"roe": roe, "netprofit_yoy": yoy}
        print(f"[QMT] 读取 {len(result)} 只财务数据")
        return result
    except Exception as e:
        print(f"[QMT] 读取失败: {e}")
        return {}


# ─── Tushare 数据源 ────────────────────────────────────────────────────────────

def _fetch_tushare_roe(ts_codes: list[str]) -> dict[str, dict]:
    """批量拉取 Tushare fina_indicator 最新一期 ROE / netprofit_yoy"""
    try:
        import tushare as ts
        token = Config.tushare_token
        if not token:
            print("[Tushare] 无 token，跳过")
            return {}
        ts.set_token(token)
        pro = ts.pro_api()
    except Exception as e:
        print(f"[Tushare] 初始化失败: {e}")
        return {}

    result: dict[str, dict] = {}
    # 每次最多查 50 只（Tushare 限频）
    batch = 50
    for i in range(0, len(ts_codes), batch):
        chunk = ts_codes[i : i + batch]
        for code in chunk:
            try:
                df = pro.fina_indicator(
                    ts_code=code,
                    fields="ts_code,end_date,roe,netprofit_yoy",
                )
                if df is None or df.empty:
                    continue
                df = df.sort_values("end_date", ascending=False)
                row = df.iloc[0]
                result[code] = {
                    "roe": _safe_float(row.get("roe")),
                    "netprofit_yoy": _safe_float(row.get("netprofit_yoy")),
                }
            except Exception:
                pass
        if i + batch < len(ts_codes):
            time.sleep(0.5)  # 防限频
    print(f"[Tushare] 读取 {len(result)} 只财务数据")
    return result


# ─── 工具 ─────────────────────────────────────────────────────────────────────

def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except Exception:
        return None


# ─── 主流程 ───────────────────────────────────────────────────────────────────

def run_patch(source: str = "auto", dry_run: bool = False, limit: int = 0) -> dict:
    """
    修补 stock_daily 最新行 ROE / netprofit_yoy。
    可直接调用（被 daily_alpha_run.py 集成）或通过 CLI 运行。

    Returns:
        dict: {updated, skipped, no_data}
    """
    # 1. 查出所有活跃股票池（A股）
    pool_df = DBUtils.query_df(
        "SELECT ts_code FROM stock_pool WHERE is_active = 1 AND ts_code LIKE '%.S%'"
    )
    ts_codes = pool_df["ts_code"].tolist()
    if limit:
        ts_codes = ts_codes[:limit]
    print(f"[ROE补丁] 股票池 {len(ts_codes)} 只")

    if not ts_codes:
        return {"updated": 0, "skipped": 0, "no_data": 0}

    # 2. 取各股票最新 trade_date
    latest_df = DBUtils.query_df(
        """
        SELECT sd.ts_code, sd.trade_date, sd.roe, sd.netprofit_yoy
        FROM stock_daily sd
        INNER JOIN (
            SELECT ts_code, MAX(trade_date) AS max_date
            FROM stock_daily GROUP BY ts_code
        ) t ON sd.ts_code = t.ts_code AND sd.trade_date = t.max_date
        WHERE sd.ts_code IN ({})
        """.format(",".join(["?"] * len(ts_codes))),
        ts_codes,
    )
    print(f"[ROE补丁] 最新日线行 {len(latest_df)} 只，其中已有ROE: "
          f"{latest_df['roe'].notna().sum()} 只")

    # 3. 拉财务数据
    qmt_map: dict = {}
    ts_map: dict = {}

    if source in ("auto", "qmt"):
        qmt_map = _fetch_qmt_roe()

    need_tushare = [c for c in ts_codes if c not in qmt_map]
    if source in ("auto", "tushare") and need_tushare:
        print(f"[Tushare] 补充 {len(need_tushare)} 只...")
        ts_map = _fetch_tushare_roe(need_tushare)

    # 4. 合并：qmt 优先
    fin_map: dict[str, dict] = {**ts_map, **qmt_map}

    # 5. 逐行 UPDATE
    updated = skipped = no_data = 0
    for _, row in latest_df.iterrows():
        code = row["ts_code"]
        trade_date = row["trade_date"]
        fin = fin_map.get(code)

        if not fin:
            no_data += 1
            continue

        new_roe = fin.get("roe")
        new_yoy = fin.get("netprofit_yoy")

        # 已有有效数据且新数据为 None 时，跳过（保留原值）
        existing_roe = _safe_float(row.get("roe"))
        if existing_roe is not None and existing_roe != 0 and new_roe is None:
            skipped += 1
            continue

        if dry_run:
            print(f"[DRY] {code} {trade_date}: roe={new_roe} yoy={new_yoy}")
            updated += 1
            continue

        try:
            DBUtils.execute(
                "UPDATE stock_daily SET roe=?, netprofit_yoy=? WHERE ts_code=? AND trade_date=?",
                params=[new_roe, new_yoy, code, trade_date],
            )
            updated += 1
        except Exception as e:
            print(f"[ERROR] {code}: {e}")

    print(f"[ROE补丁] 完成 updated={updated} skipped={skipped} no_data={no_data}")
    return {"updated": updated, "skipped": skipped, "no_data": no_data}


def main():
    args = _args()
    run_patch(source=args.source, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
