#!/usr/bin/env python3
"""
ROE 数据回填脚本（从 financial_data → stock_daily）
==================================================
每日收盘后（15:50 fast_sync_today.py 之后）运行：
  1. 从 financial_data 取各股票最新 ROE（过滤极端值）
  2. UPDATE stock_daily（最新交易日）保留已有值，仅补空缺
  3. 过滤 financial_data 极端值（<-200% / >500%）
用法：
  python scripts/backfill_roe.py           # 增量（仅补空缺）
  python scripts/backfill_roe.py --force    # 强制全量（覆盖已有值）
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.utils.db_utils import DBUtils
from src.utils.log_utils import init_logger

logger = init_logger("backfill_roe")


def clean_extreme_values():
    """清理 financial_data 中的极端 ROE 值"""
    result = DBUtils.execute(
        "UPDATE financial_data SET roe = NULL "
        "WHERE roe IS NOT NULL AND (roe < -200 OR roe > 500)"
    )
    if result and result > 0:
        logger.info(f"[清洗] 清理了 {result} 条极端 ROE 值")


def get_valid_roe_map() -> dict:
    """
    从 financial_data 取各股票最新有效 ROE，
    返回 {ts_code: roe} dict
    """
    df = DBUtils.query_df("""
        SELECT fd.ts_code, fd.roe
        FROM financial_data fd
        INNER JOIN (
            SELECT ts_code, MAX(end_date) AS max_end
            FROM financial_data
            WHERE roe IS NOT NULL
              AND roe > -200 AND roe < 500
            GROUP BY ts_code
        ) latest ON fd.ts_code = latest.ts_code
             AND fd.end_date = latest.max_end
        WHERE fd.roe IS NOT NULL
          AND fd.roe > -200 AND fd.roe < 500
    """)
    if df is None or df.empty:
        return {}
    return dict(zip(df['ts_code'], df['roe']))


def get_latest_trade_date() -> str:
    r = DBUtils.query_df("SELECT MAX(trade_date) AS d FROM stock_daily")
    if r is None or r.empty:
        return None
    d = r.iloc[0]['d']
    return str(d)[:10] if d else None


def backfill_roe(force=False):
    """
    Args:
        force: True = 强制覆盖已有值；False = 仅补空缺
    """
    clean_extreme_values()

    latest_td = get_latest_trade_date()
    if not latest_td:
        logger.warning("[ROE回填] 无交易数据，跳过")
        return

    logger.info(f"[ROE回填] 最新交易日: {latest_td}")

    roe_map = get_valid_roe_map()
    logger.info(f"[ROE回填] financial_data 有 {len(roe_map)} 只股票有效 ROE")

    if not roe_map:
        logger.warning("[ROE回填] 无可用 ROE 数据")
        return

    total_updated = 0
    with DBUtils.get_conn() as conn:
        cursor = conn.cursor()
        for ts_code, roe in roe_map.items():
            if force:
                sql = "UPDATE stock_daily SET roe = %s WHERE ts_code = %s AND trade_date = %s"
                cursor.execute(sql, [roe, ts_code, latest_td])
            else:
                sql = ("UPDATE stock_daily SET roe = %s "
                       "WHERE ts_code = %s AND trade_date = %s AND roe IS NULL")
                cursor.execute(sql, [roe, ts_code, latest_td])
            total_updated += cursor.rowcount
        conn.commit()

    logger.info(f"[ROE回填] 更新了 {total_updated} 行")

    r = DBUtils.query_df(
        'SELECT COUNT(*) AS total, '
        'SUM(CASE WHEN roe IS NOT NULL THEN 1 ELSE 0 END) AS with_roe '
        'FROM stock_daily WHERE trade_date = %s',
        params=[latest_td]
    )
    if r is not None and not r.empty:
        total = r.iloc[0]['total'] or 1
        with_roe = r.iloc[0]['with_roe'] or 0
        pct = with_roe / total * 100
        logger.info(f"[ROE回填] 覆盖率: {with_roe}/{total} = {pct:.1f}%")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='ROE 数据回填')
    parser.add_argument('--force', action='store_true', help='强制覆盖已有值（默认仅补空缺）')
    args = parser.parse_args()
    backfill_roe(force=args.force)


if __name__ == '__main__':
    main()
