#!/usr/bin/env python3
"""
历史数据补全脚本 — backfill_history.py

背景：fast_sync_today.py 在 2026-02-09 才启用，之前每日只同步了约 1,457 只股票
     （深圳早期代码），缺少上海/创业板/科创板股票，导致回测样本偏差严重。

功能：针对指定区间内覆盖率不足（< MIN_STOCKS_PER_DATE）的交易日，
     使用 Tushare 按日期批量拉全市场行情，补全 stock_daily 表。

用法：
    python scripts/backfill_history.py                        # 默认补全 2025-07-01 至今
    python scripts/backfill_history.py --start 2025-07-01 --end 2026-02-08
    python scripts/backfill_history.py --check               # 只检查缺口，不写库
    python scripts/backfill_history.py --force-dates 2026-01-08,2026-01-09  # 强制指定日期
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
from src.utils.log_utils import init_logger

logger = init_logger("backfill_history")

# ── 阈值：低于此数则认为该日期数据不完整，需要补全 ──────────────────────
MIN_STOCKS_PER_DATE = 4500
# ── Tushare 限速：每次 API 调用后等待（秒）──────────────────────────────
SLEEP_BETWEEN_CALLS = 2     # daily() + daily_basic() 之间
SLEEP_BETWEEN_DATES = 5     # 相邻日期之间
# ── 每隔多少个日期打印进度条 ─────────────────────────────────────────────
LOG_INTERVAL = 5


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def get_coverage_by_date(start_date: str, end_date: str) -> pd.DataFrame:
    """查询区间内每个交易日的股票覆盖数量。"""
    df = DBUtils.query_df(
        f"SELECT trade_date, COUNT(DISTINCT ts_code) AS cnt "
        f"FROM stock_daily "
        f"WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}' "
        f"GROUP BY trade_date ORDER BY trade_date"
    )
    return df


def get_trade_calendar(pro, start_date: str, end_date: str) -> list:
    """从 Tushare 获取交易日历。"""
    logger.info(f"获取交易日历: {start_date} ~ {end_date}")
    td_start = start_date.replace("-", "")
    td_end   = end_date.replace("-", "")
    df = pro.trade_cal(exchange='SSE', start_date=td_start, end_date=td_end, is_open='1')
    if df is None or df.empty:
        logger.warning("未获取到交易日历，用现有 DB 日期代替")
        return []
    dates = sorted(df['cal_date'].tolist())
    # 转为 YYYY-MM-DD
    return [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]


def bulk_sync_date(pro, trade_date_str: str) -> int:
    """
    同步单日全市场行情到 stock_daily。
    完全复用 fast_sync_today.py 的逻辑，返回写入行数。
    """
    td = trade_date_str.replace("-", "")   # YYYYMMDD for Tushare

    # ── Step 1: 拉 OHLCV ─────────────────────────────────────────────────
    df_daily = None
    for attempt in range(3):
        try:
            df_daily = pro.daily(trade_date=td)
            if df_daily is not None and not df_daily.empty:
                break
            time.sleep(5)
        except Exception as e:
            logger.warning(f"  daily() attempt {attempt+1} failed: {e}")
            time.sleep(15)

    if df_daily is None or df_daily.empty:
        logger.warning(f"  [{trade_date_str}] 无 OHLCV 数据（非交易日或 API 限流）")
        return 0

    time.sleep(SLEEP_BETWEEN_CALLS)

    # ── Step 2: 拉 PE/总市值 ─────────────────────────────────────────────
    df_basic = None
    for attempt in range(3):
        try:
            df_basic = pro.daily_basic(
                trade_date=td,
                fields="ts_code,pe_ttm,pb,total_mv"
            )
            if df_basic is not None and not df_basic.empty:
                break
            time.sleep(5)
        except Exception as e:
            logger.warning(f"  daily_basic() attempt {attempt+1} failed: {e}")
            time.sleep(15)

    # ── Step 3: 合并 ─────────────────────────────────────────────────────
    df_merged = df_daily.copy()
    df_merged["trade_date"] = trade_date_str

    if df_basic is not None and not df_basic.empty:
        df_merged = df_merged.merge(
            df_basic[["ts_code", "pe_ttm", "pb", "total_mv"]],
            on="ts_code", how="left"
        )
    else:
        for col in ["pe_ttm", "pb", "total_mv"]:
            df_merged[col] = None

    # ── Step 4: 写库（UPSERT） ────────────────────────────────────────────
    insert_sql = """
    INSERT INTO stock_daily
        (trade_date, ts_code, open, high, low, close, vol, amount, pe_ttm, total_mv)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        open=VALUES(open), high=VALUES(high), low=VALUES(low),
        close=VALUES(close), vol=VALUES(vol), amount=VALUES(amount),
        pe_ttm=VALUES(pe_ttm), total_mv=VALUES(total_mv)
    """
    count = 0
    with DBUtils.get_conn() as conn:
        cursor = conn.cursor()
        for _, r in df_merged.iterrows():
            try:
                cursor.execute(insert_sql, [
                    r["trade_date"], r["ts_code"],
                    None if pd.isna(r.get("open"))     else float(r["open"]),
                    None if pd.isna(r.get("high"))     else float(r["high"]),
                    None if pd.isna(r.get("low"))      else float(r["low"]),
                    None if pd.isna(r.get("close"))    else float(r["close"]),
                    None if pd.isna(r.get("vol"))      else float(r["vol"]),
                    None if pd.isna(r.get("amount"))   else float(r["amount"]),
                    None if pd.isna(r.get("pe_ttm"))   else float(r["pe_ttm"]),
                    None if pd.isna(r.get("total_mv")) else float(r["total_mv"]),
                ])
                count += 1
            except Exception as e:
                logger.debug(f"  insert error {r.get('ts_code')}: {e}")
    return count


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="历史数据补全")
    parser.add_argument("--start", default="2025-07-01",
                        help="补全开始日期（默认 2025-07-01）")
    parser.add_argument("--end",   default="2026-02-08",
                        help="补全结束日期（默认 2026-02-08，即全市场同步前最后一天）")
    parser.add_argument("--check", action="store_true",
                        help="只检查缺口，不写库")
    parser.add_argument("--min-stocks", type=int, default=MIN_STOCKS_PER_DATE,
                        help=f"低于此数的日期视为不完整（默认 {MIN_STOCKS_PER_DATE}）")
    parser.add_argument("--force-dates", default="",
                        help="强制补全指定日期，逗号分隔，如 2026-01-08,2026-01-09")
    args = parser.parse_args()

    logger.info("=" * 65)
    logger.info("  历史数据补全  backfill_history.py")
    logger.info("=" * 65)
    logger.info(f"  补全区间: {args.start} ~ {args.end}")
    logger.info(f"  不完整阈值: < {args.min_stocks} 只/日")

    import tushare as ts
    pro = ts.pro_api(token=Config.tushare_token)
    logger.info("  Tushare API 初始化成功")

    # ── 1. 获取交易日历 ───────────────────────────────────────────────────
    trade_dates = get_trade_calendar(pro, args.start, args.end)
    if not trade_dates:
        # 回退：从 DB 已有日期推算（可能缺少没有任何数据的日期）
        coverage = get_coverage_by_date(args.start, args.end)
        trade_dates = coverage['trade_date'].tolist()
        logger.warning(f"  使用 DB 内已知交易日 {len(trade_dates)} 个（可能不完整）")
    else:
        logger.info(f"  交易日历: {len(trade_dates)} 个交易日")
    time.sleep(1)

    # ── 2. 查询现有覆盖情况 ──────────────────────────────────────────────
    logger.info("查询现有数据覆盖情况...")
    coverage = get_coverage_by_date(args.start, args.end)
    coverage_map = dict(zip(coverage['trade_date'].astype(str), coverage['cnt'].astype(int)))

    # ── 3. 确定需要补全的日期 ────────────────────────────────────────────
    if args.force_dates:
        need_sync = [d.strip() for d in args.force_dates.split(",") if d.strip()]
        logger.info(f"  强制补全 {len(need_sync)} 个日期")
    else:
        need_sync = []
        already_ok = []
        missing    = []  # DB 中根本没有这个日期

        for td in trade_dates:
            cnt = coverage_map.get(td, 0)
            if cnt == 0:
                missing.append(td)
                need_sync.append(td)
            elif cnt < args.min_stocks:
                need_sync.append(td)
            else:
                already_ok.append(td)

        logger.info(f"  已完整: {len(already_ok)} 天")
        logger.info(f"  需补全: {len(need_sync)} 天  "
                    f"（其中 {len(missing)} 天 DB 中完全没有数据）")

    if args.check:
        logger.info("\n── 缺口明细 ──")
        for td in need_sync:
            cnt = coverage_map.get(td, 0)
            logger.info(f"  {td}  现有 {cnt} 只股票  → 需补全")
        logger.info(f"\n共 {len(need_sync)} 个日期需补全，--check 模式不写库，退出。")
        return

    if not need_sync:
        logger.info("所有日期数据已完整，无需补全。")
        return

    # ── 4. 估算耗时 ──────────────────────────────────────────────────────
    est_seconds = len(need_sync) * (SLEEP_BETWEEN_CALLS + SLEEP_BETWEEN_DATES + 6)
    logger.info(f"\n预计耗时: {est_seconds//60} 分 {est_seconds%60} 秒 "
                f"（每日约 {SLEEP_BETWEEN_CALLS + SLEEP_BETWEEN_DATES + 6}s）")
    logger.info("开始补全...\n")

    # ── 5. 逐日补全 ──────────────────────────────────────────────────────
    total_inserted = 0
    failed_dates   = []
    t0 = time.time()

    for i, td in enumerate(need_sync, 1):
        old_cnt = coverage_map.get(td, 0)
        t_start = time.time()

        try:
            n = bulk_sync_date(pro, td)
        except Exception as e:
            logger.error(f"  [{i}/{len(need_sync)}] {td} 失败: {e}")
            failed_dates.append(td)
            time.sleep(15)
            continue

        total_inserted += n
        elapsed = time.time() - t_start

        # 验证写入后覆盖数
        check = DBUtils.query_df(
            f"SELECT COUNT(DISTINCT ts_code) AS cnt FROM stock_daily WHERE trade_date='{td}'"
        )
        new_cnt = int(check.iloc[0]['cnt']) if not check.empty else 0

        # 进度 & 剩余时间估算
        done_ratio   = i / len(need_sync)
        elapsed_total= time.time() - t0
        eta_seconds  = (elapsed_total / i) * (len(need_sync) - i)
        eta_str      = f"ETA {int(eta_seconds//60)}m{int(eta_seconds%60)}s"

        status = "✓" if new_cnt >= args.min_stocks else "△ 仍不完整"
        logger.info(
            f"  [{i:>3}/{len(need_sync)}] {td}  "
            f"{old_cnt}→{new_cnt} 只  +{n}行  {elapsed:.1f}s  {eta_str}  {status}"
        )

        # 每 LOG_INTERVAL 个日期汇总一次
        if i % LOG_INTERVAL == 0:
            logger.info(
                f"  ── 进度 {done_ratio*100:.0f}%  累计写入 {total_inserted:,} 行 ──"
            )

        time.sleep(SLEEP_BETWEEN_DATES)

    # ── 6. 最终报告 ──────────────────────────────────────────────────────
    total_elapsed = time.time() - t0
    logger.info("\n" + "=" * 65)
    logger.info("  补全完成")
    logger.info("=" * 65)
    logger.info(f"  处理日期: {len(need_sync)} 天")
    logger.info(f"  成功写入: {total_inserted:,} 行")
    logger.info(f"  失败日期: {len(failed_dates)} 天  {failed_dates[:10]}")
    logger.info(f"  总耗时  : {total_elapsed/60:.1f} 分钟")

    # 抽查最终覆盖情况
    logger.info("\n最终覆盖情况（抽查最后10个补全日期）：")
    sample = need_sync[-10:] if len(need_sync) >= 10 else need_sync
    for td in sample:
        check = DBUtils.query_df(
            f"SELECT COUNT(DISTINCT ts_code) AS cnt FROM stock_daily WHERE trade_date='{td}'"
        )
        cnt = int(check.iloc[0]['cnt']) if not check.empty else 0
        flag = "✓" if cnt >= args.min_stocks else "✗ 仍不完整"
        logger.info(f"  {td}: {cnt} 只  {flag}")


if __name__ == "__main__":
    main()
