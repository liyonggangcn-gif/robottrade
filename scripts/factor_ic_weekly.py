"""
factor_ic_weekly.py: 因子有效性监控（每周一运行）

功能：
  1. 从 stock_factors 取近60交易日因子值
  2. 从 stock_daily 取对应次日收益率（预测目标）
  3. 计算每个因子的 RankIC（Spearman相关）
  4. 输出60日滚动IC均值、IC_IR（信息比率）
  5. 结果写入 factor_ic_log 表
  6. 打印因子有效性排名，IC_mean < 0.02 的标记无效

用法：
    python scripts/factor_ic_weekly.py

建表：首次运行自动建 factor_ic_log 表
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import datetime
from scipy import stats
from loguru import logger

from src.utils.db_utils import DBUtils


# ── 建表 DDL ──────────────────────────────────
_CREATE_IC_TABLE_SQLITE = """
CREATE TABLE IF NOT EXISTS factor_ic_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    calc_date   VARCHAR(20) NOT NULL,
    factor_name VARCHAR(50) NOT NULL,
    ic_value    REAL,
    ic_mean_60d REAL,
    ic_ir       REAL,
    is_valid    INTEGER DEFAULT 1,
    UNIQUE (calc_date, factor_name)
)
"""

_CREATE_IC_TABLE_MYSQL = """
CREATE TABLE IF NOT EXISTS factor_ic_log (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    calc_date   VARCHAR(20) NOT NULL,
    factor_name VARCHAR(50) NOT NULL,
    ic_value    DOUBLE,
    ic_mean_60d DOUBLE,
    ic_ir       DOUBLE,
    is_valid    TINYINT DEFAULT 1,
    UNIQUE KEY uq_ic (calc_date, factor_name)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci
"""

# 监控因子列表（含方向：1=正向，-1=负向）
FACTORS = {
    'rev_1m':          -1,   # 1月反转（负向，越低分越好）★★★★★
    'turnover_vol_20': -1,   # 换手率波动率（负向）★★★★★
    'pe_inv':          +1,   # 盈利收益率 E/P（正向）★★★★
    'roe_factor':      +1,   # ROE质量（正向）★★★★
    'vol_20':          -1,   # 价格波动率（负向）★★★
    'log_mv':          -1,   # 市值（负向，小市值）★★★
    'growth_score':    +1,   # 成长性（正向）★★★
    'mom_20':          +1,   # 20日动量（正向，A股弱于反转）★★
    'quality_score':   +1,   # 综合质量分（正向）★★★
    'macd_hist':       +1,   # MACD柱（正向）★★
    'rsi_14':          -1,   # RSI（负向，高RSI反转）★★
    'turnover_ratio':  -1,   # 相对换手率（负向）★★
    'price_pos_52w':   +1,   # 52周价格位置（正向）★★
    'drawdown_20':     -1,   # 最大回撤（负向）★
}

# IC有效性门槛
IC_VALID_THRESHOLD = 0.02    # IC均值 > 0.02 才算有效
IC_IR_THRESHOLD    = 0.30    # IC_IR > 0.3 才算稳定


def _ensure_table():
    from src.utils.config_loader import Config
    is_mysql = Config.get('db_type', 'sqlite') == 'mysql'
    ddl = _CREATE_IC_TABLE_MYSQL if is_mysql else _CREATE_IC_TABLE_SQLITE
    DBUtils.execute(ddl)


def load_factor_return_data(lookback_days: int = 80) -> pd.DataFrame:
    """加载近N日因子值 + 次日收益率

    Returns:
        DataFrame: trade_date, ts_code, [factors...], next_ret
    """
    # 先在 Python 端获取最近 N 个交易日（避免 MariaDB 不支持 LIMIT in subquery）
    df_dates = DBUtils.query_df(
        "SELECT DISTINCT trade_date FROM stock_factors ORDER BY trade_date DESC"
    )
    if df_dates.empty:
        return pd.DataFrame()

    recent_dates = df_dates['trade_date'].head(lookback_days + 5).tolist()
    if not recent_dates:
        return pd.DataFrame()

    cutoff_date = recent_dates[-1]   # 最早日期作为下界

    sql_factors = """
    SELECT sf.trade_date, sf.ts_code,
           sf.rev_1m, sf.turnover_vol_20,
           sf.pe_inv, sf.roe_factor, sf.vol_20, sf.log_mv,
           sf.growth_score, sf.mom_20, sf.quality_score,
           sf.macd_hist, sf.rsi_14, sf.turnover_ratio,
           sf.price_pos_52w, sf.drawdown_20
    FROM stock_factors sf
    WHERE sf.trade_date >= ?
    """

    logger.info(f"[IC] 加载近{lookback_days}日因子数据 (>= {cutoff_date})...")
    df_factors = DBUtils.query_df(sql_factors, params=(cutoff_date,))
    logger.info(f"[IC] 因子数据: {len(df_factors)} 行")

    if df_factors.empty:
        return pd.DataFrame()

    # 获取对应时段收盘价，在 Python 端计算次日收益率
    sql_close = "SELECT trade_date, ts_code, close FROM stock_daily WHERE trade_date >= ?"
    df_close = DBUtils.query_df(sql_close, params=(cutoff_date,))
    if not df_close.empty:
        df_close = df_close.sort_values(['ts_code', 'trade_date'])
        df_close['next_ret'] = df_close.groupby('ts_code')['close'].transform(
            lambda x: x.shift(-1) / x - 1
        )
        df_factors = df_factors.merge(
            df_close[['trade_date', 'ts_code', 'next_ret']],
            on=['trade_date', 'ts_code'],
            how='left'
        )
    else:
        df_factors['next_ret'] = np.nan

    return df_factors


def calc_rank_ic(df: pd.DataFrame, factor: str, direction: int) -> pd.Series:
    """按日期截面计算 RankIC（Spearman 相关）

    Args:
        df:        含 trade_date / factor / next_ret 列的 DataFrame
        factor:    因子列名
        direction: +1 正向 / -1 负向

    Returns:
        Series，index=trade_date，值=当日 IC（已乘以 direction 统一方向）
    """
    ic_series = {}
    for date, grp in df.groupby('trade_date'):
        sub = grp[['ts_code', factor, 'next_ret']].dropna()
        if len(sub) < 20:
            continue
        ic, _ = stats.spearmanr(sub[factor] * direction, sub['next_ret'])
        if np.isnan(ic):
            continue
        ic_series[date] = ic

    return pd.Series(ic_series, name=factor).sort_index()


def run():
    """主函数：计算所有因子IC并写入数据库"""
    _ensure_table()
    calc_date = datetime.now().strftime('%Y-%m-%d')
    logger.info(f"\n{'='*60}")
    logger.info(f"[IC] 因子有效性监控  calc_date={calc_date}")
    logger.info(f"{'='*60}")

    # 加载数据
    df = load_factor_return_data(lookback_days=80)
    if df.empty:
        logger.error("[IC] 数据加载失败，退出")
        return

    logger.info(f"[IC] 数据: {len(df)} 行，"
                f"日期范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")

    # 计算各因子 IC
    results = []
    for factor, direction in FACTORS.items():
        if factor not in df.columns:
            logger.warning(f"[IC] 因子 {factor} 不在数据中，跳过")
            continue

        ic_series = calc_rank_ic(df, factor, direction)
        if len(ic_series) == 0:
            logger.warning(f"[IC] {factor}: 无有效截面，跳过")
            continue

        # 取最近60日（若数据不足则取全部）
        ic_60 = ic_series.tail(60)
        ic_mean = float(ic_60.mean())
        ic_std  = float(ic_60.std())
        ic_ir   = float(ic_mean / ic_std) if ic_std > 1e-6 else 0.0
        ic_latest = float(ic_series.iloc[-1]) if len(ic_series) > 0 else 0.0
        is_valid = 1 if (abs(ic_mean) >= IC_VALID_THRESHOLD and
                         abs(ic_ir) >= IC_IR_THRESHOLD) else 0

        results.append({
            'factor':    factor,
            'ic_latest': ic_latest,
            'ic_mean':   ic_mean,
            'ic_std':    ic_std,
            'ic_ir':     ic_ir,
            'n_dates':   len(ic_60),
            'is_valid':  is_valid,
        })

    if not results:
        logger.error("[IC] 无有效因子IC计算结果")
        return

    # 打印排名
    result_df = pd.DataFrame(results).sort_values('ic_ir', ascending=False)
    logger.info(f"\n{'─'*70}")
    logger.info(f"{'因子':<22} {'IC均值':>8} {'IC_IR':>7} {'最新IC':>8} {'有效':>4}")
    logger.info(f"{'─'*70}")
    for _, row in result_df.iterrows():
        valid_tag = '✓' if row['is_valid'] else '✗'
        logger.info(
            f"  {row['factor']:<20} "
            f"{row['ic_mean']:>+8.4f} "
            f"{row['ic_ir']:>7.3f} "
            f"{row['ic_latest']:>+8.4f} "
            f"  {valid_tag}"
        )

    invalid = result_df[result_df['is_valid'] == 0]['factor'].tolist()
    if invalid:
        logger.warning(f"\n[IC] 无效因子（建议降权）: {invalid}")

    # 写入数据库（兼容 SQLite 和 MySQL）
    from src.utils.config_loader import Config
    is_mysql = Config.get('db_type', 'sqlite') == 'mysql'

    rows = [
        (calc_date, r['factor'], r['ic_latest'], r['ic_mean'], r['ic_ir'], r['is_valid'])
        for _, r in result_df.iterrows()
    ]
    ok, fail = 0, 0
    for row in rows:
        try:
            if is_mysql:
                DBUtils.execute(
                    "INSERT INTO factor_ic_log"
                    " (calc_date, factor_name, ic_value, ic_mean_60d, ic_ir, is_valid)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON DUPLICATE KEY UPDATE ic_value=VALUES(ic_value),"
                    "  ic_mean_60d=VALUES(ic_mean_60d), ic_ir=VALUES(ic_ir),"
                    "  is_valid=VALUES(is_valid)",
                    row
                )
            else:
                DBUtils.execute(
                    "INSERT OR REPLACE INTO factor_ic_log"
                    " (calc_date, factor_name, ic_value, ic_mean_60d, ic_ir, is_valid)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    row
                )
            ok += 1
        except Exception as e:
            fail += 1
            logger.warning(f"[IC] 写入失败 ({row[1]}): {e}")
    logger.info(f"\n[IC] 写入 factor_ic_log {ok} 条，失败 {fail} 条")

    logger.info(f"\n[IC] 完成。有效因子: "
                f"{result_df['is_valid'].sum()}/{len(result_df)}")


if __name__ == '__main__':
    run()
