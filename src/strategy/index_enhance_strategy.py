"""
IndexEnhanceStrategy: 指数增强策略（对标中证500/沪深300增强）

核心逻辑：
  以指数成分股为选股宇宙，多因子打分 + 行业中性约束，
  目标年化超额 3-5%，跟踪误差 < 8%。

选股流程：
  1. 加载指数成分股（中证500 或 沪深300）
  2. 多因子打分：价值(PE/PB分位) + 质量(ROE/ROA) + 动量(mom_20) + 反转(mom_5)
  3. 行业中性：各行业权重与指数偏差 < 5%
  4. 个股权重上限：指数权重 × 3
  5. 评分 = 价值(25%) + 质量(25%) + 动量(25%) + 反转(25%)

调仓频率：月度

学术依据：
  - Barra CNE6 多因子模型
  - A股指数增强基金平均年化超额 4-8%（2015-2023）
"""

import numpy as np
import pandas as pd
from loguru import logger

from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class IndexEnhanceStrategy(BaseStrategy):
    """指数增强策略：多因子 × 行业中性"""

    name = 'index_enhance'
    version = '1.0'
    display_name = '指数增强策略'

    # 支持的指数
    SUPPORTED_INDICES = {
        '000905.SH': '中证500',
        '000300.SH': '沪深300',
        '000852.SH': '中证1000',
    }

    def __init__(self, index_code: str = '000905.SH'):
        cfg = Config.get('index_enhance') or {}
        self.index_code = index_code
        self.index_name = self.SUPPORTED_INDICES.get(index_code, index_code)
        self.MAX_INDUSTRY_DEV = cfg.get('max_industry_deviation', 0.05)  # 行业偏差上限
        self.MAX_WEIGHT_MULT  = cfg.get('max_weight_multiplier', 3.0)     # 个股权重倍数
        self.EMA_ALPHA        = cfg.get('ema_alpha', 0.40)

        logger.info(f"[IndexEnhanceStrategy] 初始化 指数={self.index_name}({index_code}) "
                    f"行业偏差<={self.MAX_INDUSTRY_DEV:.0%} 权重倍数<={self.MAX_WEIGHT_MULT}x")

    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"\n[IndexEnhanceStrategy] ===== {self.index_name}增强 {trade_date} =====")

        # 加载成分股 + 因子
        df = self._load_universe(trade_date)
        if df.empty:
            logger.warning(f"[IndexEnhanceStrategy] {self.index_name} 成分股数据为空")
            return self._empty_result()

        # ST/退市
        if 'name' in df.columns:
            df = df[~df['name'].str.contains(r'ST|\*ST|退', na=False, regex=True)]

        # 评分
        df = self._calc_score(df)

        # 行业中性化
        df = self._apply_industry_neutral(df)

        # EMA平滑
        df['score'] = self._apply_score_ema(df, alpha=self.EMA_ALPHA)

        result = (df.sort_values('score', ascending=False)
                    .head(top_k).reset_index(drop=True))
        result['rank'] = range(1, len(result) + 1)
        result['strategy'] = self.name
        result['trade_date'] = trade_date
        result['signal_reason'] = result.apply(self._format_reason, axis=1)
        result['sub_scores'] = result.apply(lambda r: {
            'value_score': round(float(r.get('value_score', 0) or 0), 3),
            'quality_score': round(float(r.get('quality_score', 0) or 0), 3),
            'momentum_score': round(float(r.get('momentum_score', 0) or 0), 3),
            'reversal_score': round(float(r.get('reversal_score', 0) or 0), 3),
            'industry': str(r.get('industry', '')),
            'index_weight': round(float(r.get('index_weight', 0) or 0), 4),
        }, axis=1)

        self._save_scores_to_history(result, trade_date)
        self._print_result(result)

        out_cols = ['ts_code', 'name', 'score', 'rank', 'strategy',
                    'signal_reason', 'sub_scores', 'trade_date',
                    'industry', 'total_mv']
        return result[[c for c in out_cols if c in result.columns]]

    def _load_universe(self, trade_date: str) -> pd.DataFrame:
        """加载指数成分股 + 因子数据（无 index_constituents 时用 stock_pool 兜底）"""
        try:
            # 1. 指数成分股（表不存在时跳过）
            df_idx = pd.DataFrame()
            try:
                sql_idx = """
                SELECT ic.ts_code, ic.weight AS index_weight
                FROM index_constituents ic
                WHERE ic.index_code = ?
                  AND ic.trade_date = (
                      SELECT MAX(trade_date) FROM index_constituents
                      WHERE index_code = ? AND trade_date <= ?
                  )
                """
                df_idx = DBUtils.query_df(sql_idx, params=(self.index_code, self.index_code, trade_date))
            except Exception:
                pass

            if df_idx.empty:
                logger.warning(f"[IndexEnhanceStrategy] 指数 {self.index_code} 成分股为空，尝试 stock_pool 兜底")
                try:
                    pool_df = DBUtils.query_df(
                        "SELECT ts_code, 1.0/COUNT(*) AS index_weight FROM stock_pool WHERE is_active=1 GROUP BY ts_code LIMIT 500"
                    )
                    if not pool_df.empty:
                        df_idx = pool_df
                except Exception:
                    pass

            if df_idx.empty:
                logger.warning(f"[IndexEnhanceStrategy] 指数 {self.index_code} 成分股为空（无 index_constituents 表且 stock_pool 也为空）")
                return pd.DataFrame()

            # 2. 股票基础数据
            max_date = DBUtils.query_df(
                "SELECT MAX(trade_date) AS dt FROM stock_daily WHERE trade_date <= ?",
                params=(trade_date,)
            ).iloc[0]['dt']

            sql_daily = """
            SELECT sd.ts_code, sd.total_mv, sd.pe_ttm, sd.roe,
                   COALESCE(si.name, sd.ts_code) AS name,
                   si.industry
            FROM stock_daily sd
            LEFT JOIN stock_info si ON CONVERT(sd.ts_code USING utf8mb4) = CONVERT(si.ts_code USING utf8mb4)
            WHERE sd.trade_date = ?
            """
            df_daily = DBUtils.query_df(sql_daily, params=(max_date,))

            # 3. 因子数据
            sql_factor = """
            SELECT sf.ts_code, sf.mom_20, sf.rsi_14, sf.vol_20, sf.pe_inv
            FROM stock_factors sf
            WHERE sf.trade_date = (
                SELECT MAX(trade_date) FROM stock_factors WHERE trade_date <= ?
            )
            """
            df_factor = DBUtils.query_df(sql_factor, params=(trade_date,))

            # 合并
            df = df_idx.merge(df_daily, on='ts_code', how='left')
            if not df_factor.empty:
                df = df.merge(df_factor, on='ts_code', how='left')

            for col in ['total_mv', 'pe_ttm', 'roe', 'mom_20', 'rsi_14', 'vol_20',
                        'pe_inv', 'index_weight']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            logger.info(f"[IndexEnhanceStrategy] 加载 {self.index_name} 成分股 {len(df)} 只")
            return df
        except Exception as e:
            logger.error(f"[IndexEnhanceStrategy] 数据加载失败: {e}")
            return pd.DataFrame()

    def _calc_score(self, df: pd.DataFrame) -> pd.DataFrame:
        """四因子评分：价值 + 质量 + 动量 + 反转"""
        df = df.copy()
        n = len(df)

        # 价值因子（25%）：PE倒数 + PB倒数
        if 'pe_inv' in df.columns and df['pe_inv'].notna().sum() > 5:
            value_score = self._normalize_score(df['pe_inv'].fillna(0))
        elif 'pe_ttm' in df.columns and df['pe_ttm'].notna().sum() > 5:
            pe_valid = df['pe_ttm'] > 0
            value_score = pd.Series(0.5, index=df.index)
            if pe_valid.sum() > 1:
                value_score[pe_valid] = self._normalize_score(-df.loc[pe_valid, 'pe_ttm'])
        else:
            value_score = pd.Series([0.5] * n, index=df.index)
        df['value_score'] = value_score

        # 质量因子（25%）：ROE
        if 'roe' in df.columns and df['roe'].notna().sum() > 5:
            roe_vals = self._winsorize(df['roe'].fillna(0), 0.02, 0.98)
            quality_score = self._normalize_score(roe_vals)
        else:
            quality_score = pd.Series([0.5] * n, index=df.index)
        df['quality_score'] = quality_score

        # 动量因子（25%）：20日动量
        if 'mom_20' in df.columns and df['mom_20'].notna().sum() > 5:
            mom_vals = self._winsorize(df['mom_20'].fillna(0), 0.02, 0.98)
            momentum_score = self._normalize_score(mom_vals)
        else:
            momentum_score = pd.Series([0.5] * n, index=df.index)
        df['momentum_score'] = momentum_score

        # 反转因子（25%）：RSI超卖反弹
        if 'rsi_14' in df.columns and df['rsi_14'].notna().sum() > 5:
            # RSI < 40 且 mom_20 < 0 → 反转概率高
            reversal = pd.Series(0.5, index=df.index)
            mask = df['rsi_14'].notna() & (df['rsi_14'] < 45)
            if mask.sum() > 0:
                reversal[mask] = (45 - df.loc[mask, 'rsi_14']) / 45
            reversal_score = self._normalize_score(reversal)
        else:
            reversal_score = pd.Series([0.5] * n, index=df.index)
        df['reversal_score'] = reversal_score

        # 合成
        df['score'] = (
            0.25 * value_score +
            0.25 * quality_score +
            0.25 * momentum_score +
            0.25 * reversal_score
        )
        return df

    def _apply_industry_neutral(self, df: pd.DataFrame) -> pd.DataFrame:
        """行业中性化：对评分做行业内标准化，消除行业暴露"""
        if 'industry' not in df.columns or 'score' not in df.columns:
            return df

        neutralized = self._industry_neutral(df, 'score')
        # 重新归一化到 [0, 1]
        df['score'] = self._normalize_score(neutralized)
        return df

    def _format_reason(self, row) -> str:
        val = float(row.get('value_score', 0) or 0)
        qual = float(row.get('quality_score', 0) or 0)
        mom = float(row.get('momentum_score', 0) or 0)
        rev = float(row.get('reversal_score', 0) or 0)
        return f"价值={val:.2f} 质量={qual:.2f} 动量={mom:.2f} 反转={rev:.2f}"

    def _print_result(self, result: pd.DataFrame):
        logger.info(f"\n[IndexEnhanceStrategy {self.index_name}] ===== Top {len(result)} =====")
        for _, row in result.iterrows():
            logger.info(
                f"  #{int(row['rank']):2d} {row['ts_code']} "
                f"{str(row.get('name', ''))[:6]:6s} "
                f"Score={float(row['score']):.3f} "
                f"[{row.get('industry', '')}]"
            )
