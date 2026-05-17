#!/usr/bin/env python3
"""Fast bulk sync for A-share market: fetches ALL stocks for specified dates
using Tushare date-based bulk queries (2 API calls per date vs 5485 calls).
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import time
import argparse
import pandas as pd
from datetime import datetime, timedelta
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils


def get_recent_trade_dates(n=5):
    """Get last N trade dates from DB."""
    df = DBUtils.query_df(
        f"SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT {n}"
    )
    return df["trade_date"].tolist() if not df.empty else []


def get_missing_trade_dates(pro, last_known_date_str):
    """用 Tushare 交易日历找出库里完全缺失的交易日（last_known_date 之后到今天）。"""
    last_dt = datetime.strptime(last_known_date_str, "%Y-%m-%d")
    today_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if last_dt >= today_dt:
        return []

    start = (last_dt + timedelta(days=1)).strftime("%Y%m%d")
    end = today_dt.strftime("%Y%m%d")

    try:
        df_cal = pro.trade_cal(exchange='SSE', start_date=start, end_date=end, is_open='1')
        if df_cal is None or df_cal.empty:
            return []
        dates = df_cal['cal_date'].tolist()
        return [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in sorted(dates)]
    except Exception as e:
        print(f"  trade_cal failed: {e}, falling back to weekday check")
        # 降级：枚举工作日，bulk_sync_date 会对非交易日返回 0
        missing = []
        cur = last_dt + timedelta(days=1)
        while cur <= today_dt:
            if cur.weekday() < 5:
                missing.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
        return missing


def bulk_sync_date(pro, trade_date_str):
    """Sync ALL stocks for a single date using bulk Tushare queries."""
    td = trade_date_str.replace("-", "")  # YYYYMMDD for Tushare
    print(f"\n=== Bulk sync for {trade_date_str} ===")

    # 1. OHLCV for all A-shares for this date
    print("  Fetching OHLCV (daily)...")
    df_daily = None
    for attempt in range(3):
        try:
            df_daily = pro.daily(trade_date=td)
            if df_daily is not None and not df_daily.empty:
                break
        except Exception as e:
            print(f"  Retry {attempt+1}: {e}")
            time.sleep(10)
    if df_daily is None or df_daily.empty:
        print(f"  No OHLCV data for {trade_date_str}")
        return 0

    print(f"  Got {len(df_daily)} stocks from daily()")

    # 2. Daily basics: pe_ttm, pb, total_mv for all stocks
    print("  Fetching daily basics (pe_ttm, total_mv)...")
    df_basic = None
    for attempt in range(3):
        try:
            df_basic = pro.daily_basic(
                trade_date=td,
                fields="ts_code,pe_ttm,pb,total_mv"
            )
            if df_basic is not None and not df_basic.empty:
                break
        except Exception as e:
            print(f"  Retry {attempt+1}: {e}")
            time.sleep(10)

    if df_basic is not None and not df_basic.empty:
        print(f"  Got {len(df_basic)} stocks from daily_basic()")

    # 3. Merge
    df_merged = df_daily.copy()
    if df_basic is not None and not df_basic.empty:
        df_merged = df_merged.merge(
            df_basic[["ts_code", "pe_ttm", "pb", "total_mv"]],
            on="ts_code", how="left"
        )
    else:
        for col in ["pe_ttm", "pb", "total_mv"]:
            df_merged[col] = None

    # 4. Normalize trade_date to YYYY-MM-DD
    df_merged["trade_date"] = trade_date_str

    # 5. Insert/update stock_daily
    print(f"  Inserting {len(df_merged)} rows into stock_daily...")
    with DBUtils.get_conn() as conn:
        cursor = conn.cursor()
        insert_sql = """
        INSERT INTO stock_daily
            (trade_date, ts_code, open, high, low, close, vol, amount, pe_ttm, total_mv)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            open=VALUES(open), high=VALUES(high), low=VALUES(low),
            close=VALUES(close), vol=VALUES(vol), amount=VALUES(amount),
            pe_ttm=VALUES(pe_ttm), total_mv=VALUES(total_mv),
            roe=IFNULL(VALUES(roe), roe),
            gpr=IFNULL(VALUES(gpr), gpr),
            netprofit_yoy=IFNULL(VALUES(netprofit_yoy), netprofit_yoy)
        """
        count = 0
        for _, r in df_merged.iterrows():
            try:
                cursor.execute(insert_sql, [
                    r["trade_date"], r["ts_code"],
                    None if pd.isna(r.get("open")) else float(r["open"]),
                    None if pd.isna(r.get("high")) else float(r["high"]),
                    None if pd.isna(r.get("low")) else float(r["low"]),
                    None if pd.isna(r.get("close")) else float(r["close"]),
                    None if pd.isna(r.get("vol")) else float(r["vol"]),
                    None if pd.isna(r.get("amount")) else float(r["amount"]),
                    None if pd.isna(r.get("pe_ttm")) else float(r["pe_ttm"]),
                    None if pd.isna(r.get("total_mv")) else float(r["total_mv"]),
                ])
                count += 1
            except Exception:
                pass
    print(f"  Inserted/updated {count} rows in stock_daily")

    # 6. Update stock_info with latest pe_ttm, total_mv (upsert)
    if df_basic is not None and not df_basic.empty:
        print(f"  Updating stock_info for {len(df_basic)} stocks...")
        with DBUtils.get_conn() as conn:
            cursor = conn.cursor()
            upsert_sql = """
            INSERT INTO stock_info (ts_code, pe_ttm, pb, total_mv)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                pe_ttm=VALUES(pe_ttm), pb=VALUES(pb), total_mv=VALUES(total_mv)
            """
            si_count = 0
            for _, r in df_basic.iterrows():
                try:
                    cursor.execute(upsert_sql, [
                        r["ts_code"],
                        None if pd.isna(r.get("pe_ttm")) else float(r["pe_ttm"]),
                        None if pd.isna(r.get("pb")) else float(r["pb"]),
                        None if pd.isna(r.get("total_mv")) else float(r["total_mv"]),
                    ])
                    si_count += 1
                except Exception:
                    pass
        print(f"  Updated {si_count} rows in stock_info")

    return count


def sync_stock_names(pro):
    """One-time bulk name sync from Tushare stock_basic."""
    print("\n=== Syncing stock names ===")
    try:
        df = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name')
    except Exception as e:
        print(f"  Failed: {e}")
        return 0
    if df is None or df.empty:
        return 0
    print(f"  Got {len(df)} stocks from stock_basic()")
    with DBUtils.get_conn() as conn:
        cursor = conn.cursor()
        count = 0
        for _, r in df.iterrows():
            try:
                cursor.execute(
                    "UPDATE stock_info SET name=? WHERE ts_code=? AND (name IS NULL OR name='')",
                    [r['name'], r['ts_code']]
                )
                if cursor.rowcount > 0:
                    count += 1
            except Exception:
                pass
    print(f"  Updated {count} stock names in stock_info")
    return count


def main():
    parser = argparse.ArgumentParser(description='Fast bulk sync for A-share market data')
    parser.add_argument('--date', type=str, help='指定同步日期 YYYY-MM-DD，不填则自动检测缺失日期')
    args = parser.parse_args()

    import tushare as ts
    pro = ts.pro_api(token=Config.tushare_token)
    print("Tushare API initialized")

    sync_stock_names(pro)

    # 手动指定日期模式
    if args.date:
        print(f"\n[手动模式] 同步指定日期: {args.date}")
        total = bulk_sync_date(pro, args.date)
        print(f"\n=== Sync complete: {total} rows inserted/updated ===")
        return

    # 自动模式：检查已有日期是否完整
    known_dates = get_recent_trade_dates(10)
    print(f"Known dates in DB: {known_dates[:5]}")

    dates_to_sync = set()

    for td in known_dates:
        df = DBUtils.query_df(
            f"SELECT COUNT(*) as cnt FROM stock_daily WHERE trade_date = '{td}'"
        )
        cnt = int(df.iloc[0]["cnt"])
        if cnt < 3000:  # less than 3000 means partial/missing
            dates_to_sync.add(td)
            print(f"  {td}: {cnt} stocks → will sync (incomplete)")
        else:
            print(f"  {td}: {cnt} stocks → OK")

    # 检查最新已知日期之后是否有完全缺失的交易日
    if known_dates:
        last_known = known_dates[0]  # DESC 排序，第一个是最新
        missing = get_missing_trade_dates(pro, last_known)
        if missing:
            print(f"\n  发现 {len(missing)} 个缺失交易日: {missing}")
            dates_to_sync.update(missing)
        else:
            print(f"\n  {last_known} 之后无缺失交易日")
    else:
        # 库里完全没数据，同步今天
        today_str = datetime.now().strftime("%Y-%m-%d")
        dates_to_sync.add(today_str)

    if not dates_to_sync:
        print("All recent dates have full coverage, nothing to sync")
        return

    total = 0
    for td in sorted(dates_to_sync):
        n = bulk_sync_date(pro, td)
        total += n
        time.sleep(3)

    # Verify result
    print(f"\n=== Sync complete: {total} total rows inserted/updated ===")
    df_check = DBUtils.query_df(
        "SELECT trade_date, COUNT(*) as cnt FROM stock_daily "
        "GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5"
    )
    print(df_check.to_string())

    # Check stock_info coverage
    df_si = DBUtils.query_df(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN total_mv > 0 THEN 1 ELSE 0 END) as with_mv "
        "FROM stock_info"
    )
    print(f"\nstock_info: {df_si.to_string()}")


if __name__ == "__main__":
    main()
