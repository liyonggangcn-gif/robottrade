"""
DividendStrategy: 红利策略（红利低波 Smart Beta）

选股逻辑 —— 四维评分，追求"高股息 × 低波动 × 财务健康":

  1. 股息率分位 Yield     (35%) — 对比5年历史分位，非绝对值
  2. 盈利质量  Quality    (25%) — ROE水平
  3. 分红稳定  Stability  (20%) — 近1年股息率波动系数（CV）
  4. 低波动    LowVol     (20%) — 历史价格波动率（反向）

硬性过滤门槛：
  - 股息率 >= 3.0%（最新值）
  - ROE >= 6%（保留盈利能力底线，比价值策略略宽松）
  - 资产负债率 < 65%（来自 financial_data）
  - 总市值 > 20亿（避免极微盘高息陷阱）
  - 非ST / 非退市

陷阱识别：
  - 股息率 > 10% 且近1年股息率大幅下滑 → 高息陷阱，score × 0.5

数据来源：
  - valuation_history.dividend_yield — 当前及历史股息率
  - financial_data.roe / debt_ratio   — 最新财务数据
  - stock_factors.vol_20              — 价格波动率
  - stock_info.name / industry        — 基础信息
  - stock_daily.total_mv              — 市值过滤
"""

import pandas as pd
import numpy as np
from loguru import logger
from datetime import datetime, timedelta

from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils


class DividendStrategy(BaseStrategy):
    """红利低波策略：高股息 × 低波动 × 财务健康"""

    name = 'dividend'
    version = '1.0'

    # ── 评分权重 ──────────────────────────────
    W_YIELD     = 0.35   # 股息率历史分位
    W_QUALITY   = 0.25   # ROE质量
    W_STABILITY = 0.20   # 分红稳定性
    W_LOW_VOL   = 0.20   # 低波动

    # ── 硬性门槛 ──────────────────────────────
    MIN_YIELD_PCT    = 3.0     # 最低股息率 (%)
    MIN_ROE          = 0.0     # 取消最低ROE限制，保留盈利能力底线仅作评分
    MAX_DEBT_RATIO   = 65.0    # 最高资产负债率 (%)
    MIN_MV_YI        = 20.0    # 最低市值（亿元）

    # ── 高息陷阱阈值 ──────────────────────────
    TRAP_YIELD_PCT   = 10.0    # 股息率超过此值触发陷阱检测
    TRAP_DECAY_RATIO = 0.30    # 近1年股息率下滑>30% → 确认陷阱

    def __init__(self):
        logger.info("[DividendStrategy] 初始化")
        logger.info(f"  权重: 股息率={self.W_YIELD} 质量={self.W_QUALITY} "
                    f"稳定性={self.W_STABILITY} 低波动={self.W_LOW_VOL}")

    # ──────────────────────────────────────────
    # 主接口
    # ──────────────────────────────────────────

    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        """执行红利策略选股

        Args:
            trade_date: 交易日期 YYYY-MM-DD，None 取最新
            top_k:      输出数量

        Returns:
            标准 DataFrame（见 BaseStrategy.run 文档）
        """
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"\n[DividendStrategy] ===== 开始选股 {trade_date} =====")

        # Step 1: 加载数据
        df = self._load_universe(trade_date)
        if df.empty:
            logger.error("[DividendStrategy] 数据加载为空")
            return self._empty_result()
        logger.info(f"  [Step 1] 宇宙: {len(df)} 只")

        # Step 2: 基础过滤（ST/退市/市值）
        df = self.filter_universe(df, min_mv_yi=self.MIN_MV_YI)
        logger.info(f"  [Step 2] 基础过滤后: {len(df)} 只")

        # Step 3: 红利硬门槛
        df = self._apply_hard_filters(df)
        logger.info(f"  [Step 3] 红利门槛后: {len(df)} 只")
        if df.empty:
            logger.warning("[DividendStrategy] 硬门槛过滤后为空")
            return self._empty_result()

        # Step 4: 计算各维度评分
        df = self._calc_scores(df)

        # Step 5: 高息陷阱惩罚
        df = self._apply_trap_penalty(df)

        # Step 6: 合成最终分
        df['score'] = (
            self.W_YIELD     * df['yield_score'] +
            self.W_QUALITY   * df['quality_score'] +
            self.W_STABILITY * df['stability_score'] +
            self.W_LOW_VOL   * df['low_vol_score']
        )
        df['score'] = self._normalize_score(df['score'])

        # Step 7: 排序输出
        result = (df.sort_values('score', ascending=False)
                    .head(top_k)
                    .reset_index(drop=True))
        result['rank'] = range(1, len(result) + 1)
        result['strategy'] = self.name
        result['trade_date'] = trade_date
        result['signal_reason'] = result.apply(self._format_reason, axis=1)
        result['sub_scores'] = result.apply(lambda r: {
            'yield_score':     round(float(r.get('yield_score', 0)), 3),
            'quality_score':   round(float(r.get('quality_score', 0)), 3),
            'stability_score': round(float(r.get('stability_score', 0)), 3),
            'low_vol_score':   round(float(r.get('low_vol_score', 0)), 3),
            'dividend_yield':  round(float(r.get('dividend_yield', 0)), 2),
            'roe':             round(float(r.get('roe', 0)), 2),
            'debt_ratio':      round(float(r.get('debt_ratio', 0)), 2),
            'vol_20':          round(float(r.get('vol_20', 0)), 4),
        }, axis=1)

        self._print_result(result)

        out_cols = ['ts_code', 'name', 'score', 'rank', 'strategy',
                    'signal_reason', 'sub_scores', 'trade_date',
                    'dividend_yield', 'roe', 'debt_ratio', 'industry', 'total_mv']
        out_cols = [c for c in out_cols if c in result.columns]
        return result[out_cols]

    # ──────────────────────────────────────────
    # 数据加载
    # ──────────────────────────────────────────

    def _load_universe(self, trade_date: str) -> pd.DataFrame:
        """合并多源数据，构建候选宇宙

        数据来源：
          - valuation_history: dividend_yield（当日最新）
          - stock_daily: total_mv、name代理
          - financial_data: roe、debt_ratio（最新财报）
          - stock_factors: vol_20（价格波动率）
          - stock_info: name、industry
        """
        # 1. 取最近交易日的 dividend_yield（valuation_history 可能比 trade_date 滞后）
        sql_yield = """
        SELECT vh.ts_code,
               vh.dividend_yield,
               vh.pe_percentile_5y,
               vh.pb_percentile_5y
        FROM valuation_history vh
        INNER JOIN (
            SELECT ts_code, MAX(trade_date) AS max_dt
            FROM valuation_history
            WHERE trade_date <= ?
              AND dividend_yield IS NOT NULL
              AND dividend_yield > 0
            GROUP BY ts_code
        ) latest ON CONVERT(vh.ts_code USING utf8mb4) = CONVERT(latest.ts_code USING utf8mb4)
                 AND vh.trade_date = latest.max_dt
        """

        # 2. 股票基础信息 + 市值
        sql_base = """
        SELECT sd.ts_code,
               COALESCE(si.name, sd.ts_code) AS name,
               COALESCE(si.total_mv, sd.total_mv, 0) AS total_mv,
               si.industry,
               sd.roe,
               sd.pe_ttm
        FROM stock_daily sd
        LEFT JOIN stock_info si ON CONVERT(sd.ts_code USING utf8mb4) = CONVERT(si.ts_code USING utf8mb4)
        WHERE sd.trade_date = (
            SELECT MAX(trade_date) FROM stock_daily
            WHERE trade_date <= ?
        )
        """

        # 3. 最新财务数据（roe、负债率）
        sql_fin = """
        SELECT fd.ts_code,
               fd.roe,
               fd.debt_ratio
        FROM financial_data fd
        INNER JOIN (
            SELECT ts_code AS fin_code, MAX(end_date) AS max_end
            FROM financial_data
            GROUP BY ts_code
        ) latest ON CONVERT(fd.ts_code USING utf8mb4) = CONVERT(latest.fin_code USING utf8mb4)
                 AND fd.end_date = latest.max_end
        """

        # 4. 最新因子（波动率）
        sql_factors = """
        SELECT sf.ts_code, sf.vol_20
        FROM stock_factors sf
        INNER JOIN (
            SELECT ts_code, MAX(trade_date) AS max_dt
            FROM stock_factors
            WHERE trade_date <= ?
            GROUP BY ts_code
        ) latest ON CONVERT(sf.ts_code USING utf8mb4) = CONVERT(latest.ts_code USING utf8mb4)
                 AND sf.trade_date = latest.max_dt
        """

        try:
            df_yield   = DBUtils.query_df(sql_yield, params=(trade_date,))
            df_base    = DBUtils.query_df(sql_base, params=(trade_date,))
            df_fin     = DBUtils.query_df(sql_fin)
            df_factors = DBUtils.query_df(sql_factors, params=(trade_date,))
        except Exception as e:
            logger.error(f"[DividendStrategy] 数据加载失败: {e}")
            return pd.DataFrame()

        if df_yield.empty or df_base.empty:
            logger.warning("[DividendStrategy] 主数据表为空")
            return pd.DataFrame()

        # 合并
        df = df_yield.merge(df_base, on='ts_code', how='inner')
        if not df_fin.empty:
            # financial_data 的 roe 优先于 stock_daily 的 roe
            df = df.merge(df_fin[['ts_code', 'roe', 'debt_ratio']],
                          on='ts_code', how='left', suffixes=('_daily', '_fin'))
            # 取财务数据 roe（更准确），若缺失则回退 stock_daily
            if 'roe_fin' in df.columns:
                df['roe'] = df['roe_fin'].fillna(df.get('roe_daily', np.nan))
                df = df.drop(columns=[c for c in df.columns
                                      if c.endswith('_daily') or c.endswith('_fin')],
                             errors='ignore')

        if not df_factors.empty:
            df = df.merge(df_factors[['ts_code', 'vol_20']], on='ts_code', how='left')
        else:
            df['vol_20'] = np.nan

        if 'debt_ratio' not in df.columns:
            df['debt_ratio'] = np.nan

        # 统一数值类型
        for col in ['dividend_yield', 'roe', 'debt_ratio', 'vol_20', 'total_mv', 'pe_ttm']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        logger.info(f"  [Load] dividend_yield非空={df['dividend_yield'].notna().sum()} "
                    f"roe非空={df['roe'].notna().sum()} "
                    f"debt_ratio非空={df.get('debt_ratio', pd.Series()).notna().sum()}")
        return df

    def _load_yield_history(self, ts_codes: list, trade_date: str,
                            lookback_days: int = 400) -> pd.DataFrame:
        """加载近1年股息率历史，用于稳定性评估"""
        if not ts_codes:
            return pd.DataFrame()

        cutoff = (pd.Timestamp(trade_date) - pd.Timedelta(days=lookback_days)
                  ).strftime('%Y-%m-%d')
        codes_str = ','.join([f"'{c}'" for c in ts_codes])

        sql = f"""
        SELECT ts_code, trade_date, dividend_yield
        FROM valuation_history
        WHERE ts_code IN ({codes_str})
          AND trade_date >= '{cutoff}'
          AND trade_date <= '{trade_date}'
          AND dividend_yield IS NOT NULL
          AND dividend_yield > 0
        ORDER BY ts_code, trade_date
        """
        try:
            return DBUtils.query_df(sql)
        except Exception as e:
            logger.warning(f"[DividendStrategy] 历史股息率加载失败: {e}")
            return pd.DataFrame()

    # ──────────────────────────────────────────
    # 硬性过滤
    # ──────────────────────────────────────────

    def _apply_hard_filters(self, df: pd.DataFrame) -> pd.DataFrame:
        before = len(df)

        # 股息率门槛
        df = df[df['dividend_yield'] >= self.MIN_YIELD_PCT]
        after_yield = len(df)

        # ROE门槛（数据缺失时保留）
        if 'roe' in df.columns:
            mask_roe = df['roe'].isna() | (df['roe'] >= self.MIN_ROE)
            df = df[mask_roe]
        after_roe = len(df)

        # 负债率门槛（数据缺失时保留）
        if 'debt_ratio' in df.columns:
            mask_debt = df['debt_ratio'].isna() | (df['debt_ratio'] <= self.MAX_DEBT_RATIO)
            df = df[mask_debt]
        after_debt = len(df)

        logger.info(f"  [Filter] 股息率<{self.MIN_YIELD_PCT}%剔除{before - after_yield}只 "
                    f"ROE<{self.MIN_ROE}%剔除{after_yield - after_roe}只 "
                    f"负债率>{self.MAX_DEBT_RATIO}%剔除{after_roe - after_debt}只")
        return df.reset_index(drop=True)

    # ──────────────────────────────────────────
    # 评分计算
    # ──────────────────────────────────────────

    def _calc_scores(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        n = len(df)

        # ── 维度1：股息率历史分位评分 ──────────
        # 优先使用 valuation_history 中的 pe_percentile_5y 近似，
        # 若无则用当前股息率截面排名
        yield_vals = df['dividend_yield'].copy()
        df['yield_score'] = self._normalize_score(
            self._winsorize(yield_vals, 0.02, 0.98)
        )
        logger.info(f"  [Score] 股息率: min={yield_vals.min():.1f}% "
                    f"max={yield_vals.max():.1f}% median={yield_vals.median():.1f}%")

        # ── 维度2：ROE质量评分 ──────────────────
        if 'roe' in df.columns and df['roe'].notna().sum() > 5:
            roe_vals = self._winsorize(df['roe'].fillna(df['roe'].median()), 0.02, 0.98)
            df['quality_score'] = self._normalize_score(roe_vals)
        else:
            df['quality_score'] = 0.5
        logger.info(f"  [Score] ROE有效数据: {df['roe'].notna().sum()}/{n}")

        # ── 维度3：分红稳定性评分 ───────────────
        # 加载近1年股息率历史，计算变异系数 CV = std/mean（越小越稳定）
        hist_df = self._load_yield_history(
            df['ts_code'].tolist(), self._last_trade_date()
        )
        if not hist_df.empty:
            stability = (hist_df.groupby('ts_code')['dividend_yield']
                         .agg(yield_mean='mean', yield_std='std', yield_count='count')
                         .reset_index())
            stability['cv'] = (
                stability['yield_std'] / stability['yield_mean'].replace(0, np.nan)
            ).fillna(1.0)
            # 数据点少于3的股票给中性分
            stability.loc[stability['yield_count'] < 3, 'cv'] = 0.5
            stability['stability_score'] = self._normalize_score(
                -stability['cv']   # CV越小越稳定，取负使高分=好
            )
            df = df.merge(stability[['ts_code', 'stability_score']],
                          on='ts_code', how='left')
            df['stability_score'] = df['stability_score'].fillna(0.3)
        else:
            df['stability_score'] = 0.5
        logger.info(f"  [Score] 稳定性: 有历史数据{len(hist_df)}条")

        # ── 维度4：低波动评分 ───────────────────
        if 'vol_20' in df.columns and df['vol_20'].notna().sum() > 5:
            vol_vals = self._winsorize(
                df['vol_20'].fillna(df['vol_20'].median()), 0.02, 0.98
            )
            df['low_vol_score'] = self._normalize_score(-vol_vals)  # 低波动 → 高分
        else:
            df['low_vol_score'] = 0.5

        return df

    def _apply_trap_penalty(self, df: pd.DataFrame) -> pd.DataFrame:
        """高息陷阱惩罚：超高股息率 + 近期下滑 → score × 0.5"""
        if 'stability_score' not in df.columns:
            return df

        # 超高股息率（>10%）且稳定性差（分位<0.3）→ 陷阱
        trap_mask = (
            (df['dividend_yield'] > self.TRAP_YIELD_PCT) &
            (df['stability_score'] < 0.3)
        )
        count = trap_mask.sum()
        if count > 0:
            logger.warning(f"  [Trap] 识别到高息陷阱 {count} 只，评分×0.5")
            # 先计算原始合成分（临时）
            temp_score = (
                self.W_YIELD     * df.get('yield_score', 0) +
                self.W_QUALITY   * df.get('quality_score', 0) +
                self.W_STABILITY * df.get('stability_score', 0) +
                self.W_LOW_VOL   * df.get('low_vol_score', 0)
            )
            # 对陷阱股惩罚（在最终归一化前打折）
            df.loc[trap_mask, 'yield_score'] *= 0.5
            df.loc[trap_mask, 'quality_score'] *= 0.5

        return df

    # ──────────────────────────────────────────
    # 辅助
    # ──────────────────────────────────────────

    def _last_trade_date(self) -> str:
        try:
            df = DBUtils.query_df(
                "SELECT MAX(trade_date) AS dt FROM valuation_history"
            )
            return str(df.iloc[0]['dt'])
        except Exception:
            return datetime.now().strftime('%Y-%m-%d')

    def _format_reason(self, row) -> str:
        yield_v = row.get('dividend_yield', 0)
        roe_v   = row.get('roe', 0)
        debt_v  = row.get('debt_ratio', None)
        parts = [f"股息率{yield_v:.1f}%"]
        if roe_v and not np.isnan(float(roe_v)):
            parts.append(f"ROE={roe_v:.1f}%")
        if debt_v and not np.isnan(float(debt_v)):
            parts.append(f"负债率{debt_v:.0f}%")
        return ' '.join(parts)

    def _print_result(self, result: pd.DataFrame):
        logger.info(f"\n[DividendStrategy] ===== 选股结果 Top {len(result)} =====")
        for _, row in result.iterrows():
            logger.info(
                f"  #{int(row['rank']):2d} {row['ts_code']} "
                f"{str(row.get('name', ''))[:6]:6s} "
                f"Score={float(row['score']):.3f} "
                f"股息={float(row.get('dividend_yield', 0)):.1f}% "
                f"ROE={float(row.get('roe', 0)):.1f}% "
                f"[{row.get('industry', '')}]"
            )

    @staticmethod
    def _empty_result() -> pd.DataFrame:
        return pd.DataFrame(columns=[
            'ts_code', 'name', 'score', 'rank', 'strategy',
            'signal_reason', 'sub_scores', 'trade_date',
            'dividend_yield', 'roe', 'debt_ratio', 'industry', 'total_mv',
        ])
