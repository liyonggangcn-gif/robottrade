"""
QuantFactorStrategy: 量化多因子策略（Barra CNE6 风格）

因子体系（7类，基于A股实证IC排序）：

  Tier 1（核心Alpha，IC稳定>5%）：
    rev_1m          负向  1月收益率反转     ——A股最强异象，散户过度反应+T+1
    turnover_vol_20 负向  换手率波动率20日  ——高波动=游资=不稳定

  Tier 2（条件有效，IC 3-7%）：
    pe_inv          正向  E/P盈利收益率     ——价值因子，中国版优于B/P
    roe_factor      正向  ROE质量           ——盈利能力
    vol_20          负向  价格波动率        ——低波动异象
    log_mv          负向  对数市值          ——小市值效应

  Tier 3（辅助因子，IC 2-5%）：
    growth_score    正向  成长性评分        ——盈利加速

因子处理流水线：
  1. 去极值（Winsorize 1%-99%，按日期截面）
  2. 行业中性化（减去行业均值，消除行业 bet）
  3. 市值中性化（OLS 残差，消除规模 bet）
  4. Z-score 标准化（均值0，方差1）
  5. 动态权重（60日 IC 均值加权，冷启动时用固定权重）

冷启动固定权重（factor_ic_log 无数据时使用）：
  rev_1m:-0.25  turnover_vol_20:-0.20  pe_inv:+0.20
  roe_factor:+0.20  vol_20:-0.15  log_mv:-0.10  growth_score:+0.10
"""

import numpy as np
import pandas as pd
from loguru import logger

from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils


# 因子方向（+1正向，-1负向，最终分越高越好）
# Tier 1-3 原始因子方向
CORE_FACTORS = {
    'rev_1m':          -1,
    'turnover_vol_20': -1,
    'pe_inv':          +1,
    'roe_factor':      +1,
    'vol_20':          -1,
    'log_mv':          -1,
    'growth_score':    +1,
}

# Alpha101 因子方向（基于 A 股实证，正负号表示排序方向）
ALPHA101_FACTORS = {
    'alpha_001': -1,   # 趋势强度：A股反转效应，负向
    'alpha_003': +1,   # 量价背离：负相关→买入
    'alpha_004': +1,   # 最低价排名趋势：低位→买入
    'alpha_005': +1,   # VWAP偏离：开盘高于VWAP→强势
    'alpha_006': +1,   # 量价负相关→买入信号
    'alpha_007': +1,   # 成交量确认趋势：可信趋势→买入
    'alpha_008': +1,   # 量价同步改善→买入
    'alpha_012': +1,   # 量价反转：放量下跌→买入
    'alpha_014': +1,   # 短期反转：3日收益变差→买入
    'alpha_016': +1,   # 量价协方差负→买入
    'alpha_018': -1,   # 高波动+长实体→卖出
    'alpha_020': +1,   # 价格在区间上轨→强势
    'alpha_026': +1,   # 量确认动量→买入
    'alpha_035': +1,   # 低开高量→吸筹信号
    'alpha_041': +1,   # 买方强势→买入
}

# 合并全部因子
FACTOR_DIRECTIONS = {**CORE_FACTORS, **ALPHA101_FACTORS}

# 冷启动固定权重（归一化后，核心因子占主导）
COLD_START_WEIGHTS = {
    'rev_1m':          0.09,
    'turnover_vol_20': 0.09,
    'pe_inv':          0.08,
    'roe_factor':      0.08,
    'vol_20':          0.06,
    'log_mv':          0.05,
    'growth_score':    0.05,
    'alpha_001':       0.05,
    'alpha_003':       0.03,
    'alpha_004':       0.03,
    'alpha_005':       0.03,
    'alpha_006':       0.03,
    'alpha_007':       0.03,
    'alpha_008':       0.03,
    'alpha_012':       0.03,
    'alpha_014':       0.03,
    'alpha_016':       0.03,
    'alpha_018':       0.03,
    'alpha_020':       0.04,
    'alpha_026':       0.04,
    'alpha_035':       0.03,
    'alpha_041':       0.04,
}


class QuantFactorStrategy(BaseStrategy):
    """量化多因子策略：IC加权 × 行业市值中性化"""

    name = 'quant'
    version = '1.0'

    # 分组过滤门槛
    MIN_MV_YI   = 15.0    # 最低市值（亿元）
    MAX_PE      = 200.0   # PE上限（剔除负PE在加载时处理）

    def __init__(self):
        logger.info("[QuantFactorStrategy] 初始化")
        self._weights = None   # 懒加载

    # ──────────────────────────────────────────
    # 主接口
    # ──────────────────────────────────────────

    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        """执行量化多因子选股

        Args:
            trade_date: 交易日期，None 取最新
            top_k:      输出数量

        Returns:
            标准 DataFrame
        """
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"\n[QuantFactorStrategy] ===== 选股 {trade_date} =====")

        # Step 1: 加载因子数据
        df = self._load_factors(trade_date)
        if df.empty:
            logger.error("[QuantFactorStrategy] 因子数据为空")
            return self._empty_result()
        logger.info(f"  [Step 1] 原始数据: {len(df)} 只")

        # Step 2: 基础过滤
        df = self.filter_universe(df, min_mv_yi=self.MIN_MV_YI)
        # 剔除 PE 异常（负PE或超大PE）
        if 'pe_inv' in df.columns:
            df = df[df['pe_inv'] >= 0]   # pe_inv = 1/PE，负值=亏损
        logger.info(f"  [Step 2] 基础过滤后: {len(df)} 只")

        if len(df) < 30:
            logger.warning("[QuantFactorStrategy] 候选股不足30只，结果不可靠")
            if df.empty:
                return self._empty_result()

        # Step 3: 因子处理流水线
        df = self._process_factors(df)
        logger.info(f"  [Step 3] 因子处理完成")

        # Step 4: 读取 IC 动态权重
        weights = self._get_weights()
        logger.info(f"  [Step 4] 因子权重: { {k: round(v,3) for k,v in weights.items()} }")

        # Step 5: 加权合成综合分
        df['raw_score'] = 0.0
        used_factors = []
        for factor, w in weights.items():
            col = f'_proc_{factor}'
            if col in df.columns and df[col].notna().sum() > 0:
                direction = FACTOR_DIRECTIONS.get(factor, 1)
                df['raw_score'] += direction * w * df[col].fillna(0)
                used_factors.append(factor)
        logger.info(f"  [Step 5] 使用因子: {used_factors}")

        df['score'] = self._normalize_score(df['raw_score'])

        # Step 6: 输出
        result = (df.sort_values('score', ascending=False)
                    .head(top_k)
                    .reset_index(drop=True))
        result['rank'] = range(1, len(result) + 1)
        result['strategy'] = self.name
        result['trade_date'] = trade_date
        result['signal_reason'] = result.apply(
            lambda r: self._format_reason(r, used_factors), axis=1
        )
        result['sub_scores'] = result.apply(
            lambda r: {f: round(float(r.get(f'_proc_{f}', 0) or 0), 3)
                       for f in used_factors},
            axis=1
        )

        self._print_result(result, used_factors)

        out_cols = ['ts_code', 'name', 'score', 'rank', 'strategy',
                    'signal_reason', 'sub_scores', 'trade_date', 'industry']
        out_cols = [c for c in out_cols if c in result.columns]
        return result[out_cols]

    # ──────────────────────────────────────────
    # 数据加载
    # ──────────────────────────────────────────

    def _load_factors(self, trade_date: str) -> pd.DataFrame:
        """加载最新因子数据（分开查询 stock_info 避免 collation 冲突）"""
        sql_max = "SELECT MAX(trade_date) AS dt FROM stock_factors WHERE trade_date <= ?"
        alpha_cols_str = ', '.join([f'sf.{f}' for f in ALPHA101_FACTORS])
        sql_factors = f"""
        SELECT sf.ts_code,
               sf.rev_1m,
               sf.turnover_vol_20,
               sf.pe_inv,
               sf.roe_factor,
               sf.vol_20,
               sf.log_mv,
               sf.growth_score,
               sf.mom_20,
               sf.quality_score,
               {alpha_cols_str}
        FROM stock_factors sf
        WHERE sf.trade_date = ?
          AND sf.pe_inv IS NOT NULL
        """
        sql_info = "SELECT ts_code, name, industry, total_mv FROM stock_info"
        try:
            max_dt = DBUtils.query_df(sql_max, params=(trade_date,)).iloc[0]['dt']
            df      = DBUtils.query_df(sql_factors, params=(max_dt,))
            df_info = DBUtils.query_df(sql_info)

            # Python 端 merge（避免跨表 collation 问题）
            df = df.merge(df_info, on='ts_code', how='left')
            df['name']     = df['name'].where(df['name'].notna(), df['ts_code'])
            df['industry'] = df.get('industry', pd.Series('', index=df.index)).fillna('')
            df['total_mv'] = pd.to_numeric(df.get('total_mv', 0), errors='coerce').fillna(0)

            for col in FACTOR_DIRECTIONS:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            return df
        except Exception as e:
            logger.error(f"[QuantFactorStrategy] 数据加载失败: {e}")
            return pd.DataFrame()

    # ──────────────────────────────────────────
    # 因子处理流水线
    # ──────────────────────────────────────────

    def _process_factors(self, df: pd.DataFrame) -> pd.DataFrame:
        """去极值 → 行业中性化 → 市值中性化 → Z-score"""
        df = df.copy()
        factors = [f for f in FACTOR_DIRECTIONS if f in df.columns]

        for factor in factors:
            col = df[factor].copy()

            # 1. 去极值
            col = self._winsorize(col, 0.01, 0.99)

            # 2. 行业中性化（减去行业均值）
            if 'industry' in df.columns and df['industry'].notna().any():
                industry_mean = df.groupby('industry')[factor].transform('mean')
                col = col - industry_mean.fillna(0)

            # 3. 市值中性化（OLS残差，回归掉 log_mv 的影响）
            if factor != 'log_mv' and 'log_mv' in df.columns:
                col = self._neutralize_size(col, df['log_mv'])

            # 4. Z-score 标准化
            col = self._zscore(col)

            df[f'_proc_{factor}'] = col

        return df

    @staticmethod
    def _neutralize_size(factor: pd.Series, log_mv: pd.Series) -> pd.Series:
        """OLS 回归残差，消除市值暴露"""
        valid = factor.notna() & log_mv.notna()
        if valid.sum() < 20:
            return factor

        X = log_mv[valid].values
        y = factor[valid].values
        # 最小二乘回归
        X_c = np.column_stack([X, np.ones(len(X))])
        try:
            beta, _, _, _ = np.linalg.lstsq(X_c, y, rcond=None)
            residuals = y - X_c @ beta
            result = factor.copy()
            result[valid] = residuals
            return result
        except Exception:
            return factor

    # ──────────────────────────────────────────
    # 动态权重（IC加权）
    # ──────────────────────────────────────────

    def _get_weights(self) -> dict:
        """从 factor_ic_log 读取60日 ICIR 均值，归一化为权重

        ICIR = mean(IC) / std(IC)，比纯 IC 更稳定：
          - 高 IC + 低波动 → 高权重（可靠因子）
          - 高 IC + 高波动 → 中权重（不稳定因子）
          - 低 IC → 低权重

        冷启动时使用固定权重
        """
        if self._weights is not None:
            return self._weights

        try:
            # 读取最近120日的IC历史（ICIR需要足够样本）
            sql_hist = """
            SELECT factor_name, calc_date, ic_1d, is_valid
            FROM factor_ic_log
            WHERE calc_date >= DATE_SUB((SELECT MAX(calc_date) FROM factor_ic_log), INTERVAL 120 DAY)
              AND factor_name IN ({})
            ORDER BY factor_name, calc_date
            """.format(','.join([f"'{f}'" for f in FACTOR_DIRECTIONS]))

            # 同时读取最新有效性标记
            sql_valid = """
            SELECT factor_name, is_valid
            FROM factor_ic_log
            INNER JOIN (
                SELECT MAX(calc_date) AS max_dt FROM factor_ic_log
            ) latest ON calc_date = latest.max_dt
            WHERE factor_name IN ({})
            """.format(','.join([f"'{f}'" for f in FACTOR_DIRECTIONS]))

            df_hist = DBUtils.query_df(sql_hist)
            df_valid = DBUtils.query_df(sql_valid)

            if df_hist.empty or len(df_hist) < len(FACTOR_DIRECTIONS):
                logger.info("[QuantFactorStrategy] factor_ic_log 数据不足，使用冷启动权重")
                self._weights = COLD_START_WEIGHTS.copy()
                return self._weights

            # 有效因子集合
            valid_set = set(df_valid[df_valid['is_valid'] == 1]['factor_name']) if not df_valid.empty else set()

            # 对每个因子计算 ICIR = abs(mean(ic) / std(ic))
            factor_icir = {}
            for factor in FACTOR_DIRECTIONS:
                f_ic = df_hist[df_hist['factor_name'] == factor]['ic_1d'].dropna()
                if len(f_ic) < 20:
                    factor_icir[factor] = IC_VALID_THRESHOLD  # 样本不足，给最低权重
                    continue
                ic_mean = f_ic.mean()
                ic_std = f_ic.std()
                if ic_std == 0 or pd.isna(ic_std):
                    factor_icir[factor] = IC_VALID_THRESHOLD
                else:
                    icir = abs(ic_mean / ic_std)
                    factor_icir[factor] = max(icir, IC_VALID_THRESHOLD)

            # 无效因子降权至 0.5 倍
            weights_raw = {}
            for factor in FACTOR_DIRECTIONS:
                icir_val = factor_icir.get(factor, IC_VALID_THRESHOLD)
                mult = 1.0 if factor in valid_set else 0.3
                weights_raw[factor] = max(icir_val * mult, 0.005)

            # 归一化
            total = sum(weights_raw.values())
            self._weights = {k: v / total for k, v in weights_raw.items()}
            n_valid = len(valid_set)
            logger.info(f"[QuantFactorStrategy] ICIR动态权重加载成功 "
                        f"(有效因子={n_valid}/{len(FACTOR_DIRECTIONS)}, "
                        f"因子数={len(factor_icir)})")

        except Exception as e:
            logger.warning(f"[QuantFactorStrategy] 读取ICIR权重失败({e})，使用冷启动权重")
            self._weights = COLD_START_WEIGHTS.copy()

        return self._weights

    # ──────────────────────────────────────────
    # 辅助
    # ──────────────────────────────────────────

    def _format_reason(self, row, factors: list) -> str:
        parts = []
        for f in factors[:3]:   # 取权重最大的前3个因子说明
            val = row.get(f'_proc_{f}', None)
            if val is not None and not np.isnan(float(val or 0)):
                direction = FACTOR_DIRECTIONS.get(f, 1)
                flag = '↑' if float(val) * direction > 0.5 else ''
                parts.append(f"{f}{flag}")
        return ' '.join(parts) if parts else self.name

    def _print_result(self, result: pd.DataFrame, factors: list):
        logger.info(f"\n[QuantFactorStrategy] ===== Top {len(result)} =====")
        for _, row in result.iterrows():
            logger.info(
                f"  #{int(row['rank']):2d} {row['ts_code']} "
                f"{str(row.get('name', ''))[:6]:6s} "
                f"Score={float(row['score']):.3f} "
                f"[{row.get('industry', '')}]"
            )

    @staticmethod
    def _empty_result() -> pd.DataFrame:
        return pd.DataFrame(columns=[
            'ts_code', 'name', 'score', 'rank', 'strategy',
            'signal_reason', 'sub_scores', 'trade_date', 'industry',
        ])


# 导入供 factor_ic_log 读取权重使用
IC_VALID_THRESHOLD = 0.02
