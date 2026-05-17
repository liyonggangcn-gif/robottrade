"""
ConvertibleBondStrategy: 可转债策略

核心逻辑：
  可转债 = "下有保底（债底），上不封顶（转股）"，震荡市利器。

选股流程：
  1. 全量可转债 → 排除剩余期限 < 0.5 年（即将到期）
  2. 到期收益率(YTM) > 0（保底安全）
  3. 转股溢价率 < 40%（有弹性）
  4. 正股动量：近20日涨幅 > -5%（正股不能太弱）
  5. 剩余规模 < 15亿（小盘弹性大，但排除 < 1 亿的迷你债）
  6. 评分 = YTM排名(30%) + 溢价率排名(30%) + 正股动量(25%) + 规模排名(15%)

调仓频率：周频（每周一）

数据源：Tushare cb_basic + cb_daily
"""

import numpy as np
import pandas as pd
from loguru import logger

from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class ConvertibleBondStrategy(BaseStrategy):
    """可转债策略：下有保底 × 上不封顶"""

    name = 'convertible_bond'
    version = '1.0'
    display_name = '可转债策略'

    def __init__(self):
        cfg = Config.get('convertible_bond') or {}
        self.MIN_YTM            = cfg.get('min_ytm', 0.0)        # 最低到期收益率
        self.MAX_PREMIUM_PCT    = cfg.get('max_premium_pct', 40) # 最高转股溢价率
        self.MAX_SIZE_YI        = cfg.get('max_size_亿', 15.0)   # 最大剩余规模
        self.MIN_SIZE_YI        = cfg.get('min_size_亿', 1.0)    # 最小剩余规模
        self.MIN_MATURITY_YR    = cfg.get('min_maturity_yr', 0.5) # 最短剩余期限
        self.MAX_STOCK_MOM20    = cfg.get('max_stock_mom20', -0.05) # 正股20日跌幅上限
        self.EMA_ALPHA          = cfg.get('ema_alpha', 0.40)

        logger.info(f"[ConvertibleBondStrategy] 初始化 "
                    f"YTM>{self.MIN_YTM}% 溢价率<{self.MAX_PREMIUM_PCT}% "
                    f"规模[{self.MIN_SIZE_YI},{self.MAX_SIZE_YI}]亿")

    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"\n[ConvertibleBondStrategy] ===== 选股 {trade_date} =====")

        df = self._load_universe(trade_date)
        if df.empty:
            return self._empty_result()

        # 到期收益率
        before = len(df)
        df = df[df['ytm'] >= self.MIN_YTM]
        logger.info(f"  [Filter] YTM>={self.MIN_YTM}%: {before}→{len(df)}")

        # 转股溢价率
        before = len(df)
        df = df[df['conversion_premium'] <= self.MAX_PREMIUM_PCT]
        logger.info(f"  [Filter] 溢价率<={self.MAX_PREMIUM_PCT}%: {before}→{len(df)}")

        # 剩余规模
        before = len(df)
        df = df[
            (df['remaining_size'] >= self.MIN_SIZE_YI) &
            (df['remaining_size'] <= self.MAX_SIZE_YI)
        ]
        logger.info(f"  [Filter] 规模[{self.MIN_SIZE_YI},{self.MAX_SIZE_YI}]亿: {before}→{len(df)}")

        # 剩余期限
        before = len(df)
        df = df[df['maturity_years'] >= self.MIN_MATURITY_YR]
        logger.info(f"  [Filter] 期限>={self.MIN_MATURITY_YR}年: {before}→{len(df)}")

        # 正股动量
        before = len(df)
        df = df[df['stock_mom_20'] >= self.MAX_STOCK_MOM20]
        logger.info(f"  [Filter] 正股20日>={self.MAX_STOCK_MOM20*100:.0f}%: {before}→{len(df)}")

        if df.empty:
            return self._empty_result()

        # 评分
        df = self._calc_score(df)

        # EMA平滑
        df['score'] = self._apply_score_ema(df, alpha=self.EMA_ALPHA)

        result = (df.sort_values('score', ascending=False)
                    .head(top_k).reset_index(drop=True))
        result['rank'] = range(1, len(result) + 1)
        result['strategy'] = self.name
        result['trade_date'] = trade_date
        result['signal_reason'] = result.apply(self._format_reason, axis=1)
        result['sub_scores'] = result.apply(lambda r: {
            'ytm': round(float(r.get('ytm', 0) or 0), 2),
            'premium': round(float(r.get('conversion_premium', 0) or 0), 1),
            'size_亿': round(float(r.get('remaining_size', 0) or 0), 2),
            'stock_mom_20': round(float(r.get('stock_mom_20', 0) or 0), 4),
        }, axis=1)

        self._save_scores_to_history(result, trade_date)
        self._print_result(result)

        out_cols = ['ts_code', 'name', 'score', 'rank', 'strategy',
                    'signal_reason', 'sub_scores', 'trade_date']
        return result[[c for c in out_cols if c in result.columns]]

    def _load_universe(self, trade_date: str) -> pd.DataFrame:
        """从数据库加载可转债数据

        需要预先运行 sync 脚本将 Tushare cb_basic + cb_daily 数据入库。
        预期表：convertible_bond (cb_code, cb_name, stock_code, stock_name,
                    ytm, conversion_premium, remaining_size, maturity_years,
                    stock_mom_20, cb_price)
        """
        try:
            sql = """
            SELECT cb_code AS ts_code,
                   cb_name AS name,
                   stock_code,
                   stock_name,
                   ytm,
                   conversion_premium,
                   remaining_size,
                   maturity_years,
                   stock_mom_20,
                   cb_price
            FROM convertible_bond
            WHERE trade_date = (
                SELECT MAX(trade_date) FROM convertible_bond WHERE trade_date <= ?
            )
            AND cb_price > 0
            """
            df = DBUtils.query_df(sql, params=(trade_date,))

            for col in ['ytm', 'conversion_premium', 'remaining_size',
                        'maturity_years', 'stock_mom_20', 'cb_price']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            return df
        except Exception as e:
            logger.warning(f"[ConvertibleBondStrategy] 数据加载失败: {e}")
            logger.warning("  提示：需先运行 sync 脚本将 Tushare cb_* 数据入库")
            return pd.DataFrame()

    def _calc_score(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        n = len(df)

        # YTM评分（越高越好，保底越安全）
        if 'ytm' in df.columns and df['ytm'].notna().sum() > 5:
            ytm_score = self._normalize_score(df['ytm'].fillna(0))
        else:
            ytm_score = pd.Series([0.5] * n, index=df.index)

        # 转股溢价率评分（越低越好，弹性越大）
        if 'conversion_premium' in df.columns and df['conversion_premium'].notna().sum() > 5:
            premium_score = self._normalize_score(-df['conversion_premium'].fillna(50))
        else:
            premium_score = pd.Series([0.5] * n, index=df.index)

        # 正股动量评分（越高越好）
        if 'stock_mom_20' in df.columns and df['stock_mom_20'].notna().sum() > 5:
            mom_vals = self._winsorize(df['stock_mom_20'].fillna(0), 0.02, 0.98)
            mom_score = self._normalize_score(mom_vals)
        else:
            mom_score = pd.Series([0.5] * n, index=df.index)

        # 规模评分（越小越好，弹性大）
        if 'remaining_size' in df.columns and df['remaining_size'].notna().sum() > 5:
            size_score = self._normalize_score(-df['remaining_size'].fillna(10))
        else:
            size_score = pd.Series([0.5] * n, index=df.index)

        df['score'] = (
            0.30 * ytm_score +
            0.30 * premium_score +
            0.25 * mom_score +
            0.15 * size_score
        )
        return df

    def _format_reason(self, row) -> str:
        ytm = float(row.get('ytm', 0) or 0)
        prem = float(row.get('conversion_premium', 0) or 0)
        size = float(row.get('remaining_size', 0) or 0)
        return f"YTM={ytm:.1f}% 溢价率={prem:.0f}% 规模={size:.1f}亿"

    def _print_result(self, result: pd.DataFrame):
        logger.info(f"\n[ConvertibleBondStrategy] ===== Top {len(result)} =====")
        for _, row in result.iterrows():
            logger.info(
                f"  #{int(row['rank']):2d} {row.get('ts_code', '')} "
                f"{str(row.get('name', ''))[:8]:8s} "
                f"Score={float(row['score']):.3f}"
            )
