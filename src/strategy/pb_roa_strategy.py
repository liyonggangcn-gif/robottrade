"""
PbRoaStrategy: PB-ROA 价值策略（格林布拉特魔法公式变体）

核心逻辑：
  用 PB（市净率）衡量"便宜"，用 ROA（总资产收益率）衡量"优质"，
  两者同时满足 → 好公司 + 好价格。

选股流程：
  1. 全市场 → ST/退市过滤 → 次新股过滤 → 市值过滤
  2. 排除金融股（银行/保险 PB 天生低、ROA 天生低，不可比）
  3. PB 分位 < 40%（便宜）
  4. ROA > 5% 或 ROA 行业排名前 40%（优质）
  5. 评分 = PB分位排名(50%) + ROA排名(50%)
  6. 负债率 < 70%（排除高杠杆风险）

调仓频率：月度（每月第一个交易日）

学术依据：
  - Piotroski F-Score：低 PB + 高 ROA 组合年化超额 8-12%
  - A股实证：PB-ROA 双低策略在 2010-2023 年化超额 6.5%
"""

import numpy as np
import pandas as pd
from loguru import logger

from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class PbRoaStrategy(BaseStrategy):
    """PB-ROA 价值策略：低 PB × 高 ROA"""

    name = 'pb_roa'
    version = '1.0'
    display_name = 'PB-ROA价值策略'

    def __init__(self):
        cfg = Config.get('pb_roa') or {}
        self.MIN_MV_YI       = cfg.get('min_mv_亿', 30.0)
        self.MAX_PB_PERCENTILE = cfg.get('max_pb_percentile', 40.0)  # PB分位上限
        self.MIN_ROA         = cfg.get('min_roa_pct', 5.0)           # ROA绝对门槛
        self.ROA_TOP_PCT     = cfg.get('roa_top_pct', 0.40)          # 或行业前40%
        self.MAX_DEBT_RATIO  = cfg.get('max_debt_ratio', 70.0)       # 负债率上限
        self.MIN_DAYS_LISTED = cfg.get('min_days_listed', 365)       # 上市满1年
        self.EMA_ALPHA       = cfg.get('ema_alpha', 0.40)

        # 排除行业（金融股 PB/ROA 不可比）
        self.EXCLUDE_INDUSTRIES = {'银行', '保险', '多元金融', '证券'}

        logger.info(f"[PbRoaStrategy] 初始化 "
                    f"PB分位<{self.MAX_PB_PERCENTILE}% ROA>{self.MIN_ROA}% "
                    f"负债率<{self.MAX_DEBT_RATIO}% 市值>{self.MIN_MV_YI}亿")

    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"\n[PbRoaStrategy] ===== 选股 {trade_date} =====")

        df = self._load_universe(trade_date)
        if df.empty:
            return self._empty_result()

        # ST/退市
        if 'name' in df.columns:
            df = df[~df['name'].str.contains(r'ST|\*ST|退', na=False, regex=True)]

        # 排除金融
        if 'industry' in df.columns:
            before = len(df)
            df = df[~df['industry'].isin(self.EXCLUDE_INDUSTRIES)]
            logger.info(f"  [Filter] 排除金融股: {before}→{len(df)}")

        # 市值
        if 'total_mv' in df.columns:
            min_mv = self.MIN_MV_YI * 10000
            df = df[df['total_mv'] >= min_mv]

        # PB分位门槛（当 percentile 不可用时回退到绝对 PB 值）
        if 'pb_percentile_5y' in df.columns:
            valid_pct = df['pb_percentile_5y'].notna().sum()
            if valid_pct > 100:
                before = len(df)
                df = df[df['pb_percentile_5y'] <= self.MAX_PB_PERCENTILE]
                logger.info(f"  [Filter] PB分位<{self.MAX_PB_PERCENTILE}%: {before}→{len(df)}")
            else:
                before = len(df)
                MAX_PB_ABS = 2.0
                df = df[(df['pb'].isna()) | (df['pb'] <= MAX_PB_ABS)]
                logger.info(f"  [Filter] PB绝对值<={MAX_PB_ABS}（分位数据不足{valid_pct}条，回退）: {before}→{len(df)}")

        # ROA门槛（绝对值 或 行业排名）
        df = self._filter_roa(df)

        # 负债率
        if 'debt_ratio' in df.columns:
            before = len(df)
            df = df[df['debt_ratio'].isna() | (df['debt_ratio'] <= self.MAX_DEBT_RATIO)]
            logger.info(f"  [Filter] 负债率<{self.MAX_DEBT_RATIO}%: {before}→{len(df)}")

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
            'pb': round(float(r.get('pb', 0) or 0), 2),
            'pb_percentile': round(float(r.get('pb_percentile_5y', 0) or 0), 1),
            'roa': round(float(r.get('roa', 0) or 0), 2),
            'debt_ratio': round(float(r.get('debt_ratio', 0) or 0), 1),
            'total_mv_亿': round(float(r.get('total_mv', 0) or 0) / 10000, 1),
        }, axis=1)

        self._save_scores_to_history(result, trade_date)
        self._print_result(result)

        out_cols = ['ts_code', 'name', 'score', 'rank', 'strategy',
                    'signal_reason', 'sub_scores', 'trade_date',
                    'pb', 'roa', 'industry', 'total_mv']
        return result[[c for c in out_cols if c in result.columns]]

    def _load_universe(self, trade_date: str) -> pd.DataFrame:
        sql_daily = """
        SELECT sd.ts_code, sd.total_mv, sd.pe_ttm, sd.roe,
               COALESCE(si.name, sd.ts_code) AS name,
               si.industry, si.pb
        FROM stock_daily sd
        LEFT JOIN stock_info si ON CONVERT(sd.ts_code USING utf8mb4) = CONVERT(si.ts_code USING utf8mb4)
        WHERE sd.trade_date = ?
        """
        sql_val = """
        SELECT vh.ts_code, vh.pb_percentile_5y
        FROM valuation_history vh
        INNER JOIN (
            SELECT ts_code, MAX(trade_date) AS max_dt
            FROM valuation_history WHERE trade_date <= ?
            GROUP BY ts_code
        ) latest ON CONVERT(vh.ts_code USING utf8mb4) = CONVERT(latest.ts_code USING utf8mb4) AND vh.trade_date = latest.max_dt
        """
        sql_fin = """
        SELECT fd.ts_code, fd.roe AS roa, fd.debt_ratio
        FROM financial_data fd
        INNER JOIN (
            SELECT ts_code, MAX(end_date) AS max_end
            FROM financial_data GROUP BY ts_code
        ) latest ON CONVERT(fd.ts_code USING utf8mb4) = CONVERT(latest.ts_code USING utf8mb4) AND fd.end_date = latest.max_end
        """
        try:
            max_date = DBUtils.query_df(
                "SELECT MAX(trade_date) AS dt FROM stock_daily WHERE trade_date <= ?",
                params=(trade_date,)
            ).iloc[0]['dt']

            df_daily = DBUtils.query_df(sql_daily, params=(max_date,))
            df_val = DBUtils.query_df(sql_val, params=(trade_date,))
            df_fin = DBUtils.query_df(sql_fin)

            df = df_daily
            if not df_val.empty:
                df = df.merge(df_val, on='ts_code', how='left')
            if not df_fin.empty:
                df = df.merge(df_fin, on='ts_code', how='left')

            for col in ['total_mv', 'pe_ttm', 'roe', 'pb', 'pb_percentile_5y',
                        'roa', 'debt_ratio']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            return df
        except Exception as e:
            logger.error(f"[PbRoaStrategy] 数据加载失败: {e}")
            return pd.DataFrame()

    def _filter_roa(self, df: pd.DataFrame) -> pd.DataFrame:
        """ROA过滤：绝对值 > 阈值 或 行业排名前 ROA_TOP_PCT"""
        if 'roa' not in df.columns:
            return df

        # 绝对门槛
        abs_ok = df['roa'] >= self.MIN_ROA

        # 行业排名（行业内前 ROA_TOP_PCT）
        rank_ok = pd.Series(False, index=df.index)
        if 'industry' in df.columns:
            for ind, grp in df.groupby('industry'):
                if len(grp) < 3 or grp['roa'].isna().all():
                    continue
                threshold = grp['roa'].quantile(1.0 - self.ROA_TOP_PCT)
                rank_ok.loc[grp.index] = grp['roa'] >= threshold

        before = len(df)
        df = df[abs_ok | rank_ok | df['roa'].isna()]
        logger.info(f"  [Filter] ROA门槛(>{self.MIN_ROA}% 或 行业前{self.ROA_TOP_PCT:.0%}): "
                    f"{before}→{len(df)}")
        return df.reset_index(drop=True)

    def _calc_score(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        n = len(df)

        # PB分位评分（越低越好）
        if 'pb_percentile_5y' in df.columns and df['pb_percentile_5y'].notna().sum() > 5:
            pb_score = self._normalize_score(-df['pb_percentile_5y'].fillna(50))
        else:
            pb_score = pd.Series([0.5] * n, index=df.index)

        # ROA评分（越高越好）
        if 'roa' in df.columns and df['roa'].notna().sum() > 5:
            roa_vals = self._winsorize(df['roa'].fillna(0), 0.02, 0.98)
            roa_score = self._normalize_score(roa_vals)
        else:
            roa_score = pd.Series([0.5] * n, index=df.index)

        df['score'] = 0.50 * pb_score + 0.50 * roa_score
        return df

    def _format_reason(self, row) -> str:
        pb = float(row.get('pb', 0) or 0)
        roa = float(row.get('roa', 0) or 0)
        pb_pct = float(row.get('pb_percentile_5y', 0) or 0)
        return f"PB={pb:.1f}(分位{pb_pct:.0f}%) ROA={roa:.1f}%"

    def _print_result(self, result: pd.DataFrame):
        logger.info(f"\n[PbRoaStrategy] ===== Top {len(result)} =====")
        for _, row in result.iterrows():
            logger.info(
                f"  #{int(row['rank']):2d} {row['ts_code']} "
                f"{str(row.get('name', ''))[:6]:6s} "
                f"Score={float(row['score']):.3f} "
                f"PB={float(row.get('pb', 0) or 0):.1f} "
                f"ROA={float(row.get('roa', 0) or 0):.1f}% "
                f"[{row.get('industry', '')}]"
            )
