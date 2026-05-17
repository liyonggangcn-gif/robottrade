"""
CyclicalStrategy: 周期轮动策略

两层模型：

第一层 — 宏观周期判断（每月更新）
  五维信号打分，输出 cycle_stage：
    early_recovery  复苏早段  PMI↑ 信贷↑ PPI底部  → 金融/建材/工业
    mid_expansion   扩张中段  PPI↑ 企业盈利↑       → 有色/能源/化工
    late_cycle      过热晚段  CPI↑ 利率↑           → 资源/大宗
    recession       衰退防御  PMI↓ 信贷↓           → 消费/医疗/公用事业/红利

第二层 — 行业内三指标选股
  intra_sector_score = 0.40 × 行业内相对动量（20日）
                     + 0.30 × 行业内ROE排名
                     + 0.30 × 行业内估值排名（pe_inv）
  拥挤度风控：行业内换手率Z分>2.0 → 该行业权重砍半

宏观数据来源（macro_indicators 表）：
  - PMI：AKShare ak.macro_china_pmi_monthly()
  - PPI：AKShare ak.macro_china_ppi_monthly()
  - M1/M2：AKShare ak.macro_china_m2_yearly()
  - MLF利率：Tushare shibor 接口
  若表无数据，降级到仅用行业动量判断（无宏观层）

配套脚本：scripts/sync_macro_data.py（月度运行）
"""

import numpy as np
import pandas as pd
from loguru import logger
from typing import Tuple

from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils


# ── 周期-行业映射表 ───────────────────────────
CYCLE_SECTOR_MAP = {
    'early_recovery': {
        'target':  ['银行', '非银金融', '建筑材料', '建筑装饰', '机械设备',
                    '电力设备', '交通运输'],
        'avoid':   ['公用事业', '电信服务'],
        'label':   '复苏早段',
    },
    'mid_expansion': {
        'target':  ['有色金属', '石油石化', '基础化工', '煤炭', '钢铁',
                    '电子', '计算机'],
        'avoid':   ['食品饮料', '医药生物'],
        'label':   '扩张中段',
    },
    'late_cycle': {
        'target':  ['有色金属', '石油石化', '煤炭', '农林牧渔'],
        'avoid':   ['计算机', '电子', '传媒'],
        'label':   '过热晚段',
    },
    'recession': {
        'target':  ['食品饮料', '医药生物', '公用事业', '家用电器',
                    '商贸零售', '美容护理'],
        'avoid':   ['有色金属', '煤炭', '石油石化', '钢铁'],
        'label':   '衰退防御',
    },
}

# 拥挤度风控阈值
CROWDING_ZSCORE_THRESHOLD = 2.0


class CyclicalStrategy(BaseStrategy):
    """周期轮动策略：宏观周期判断 + 行业内三指标选股"""

    name = 'cyclical'
    version = '1.0'

    def __init__(self):
        logger.info("[CyclicalStrategy] 初始化")
        self._cycle_stage = None   # 缓存本次运行的周期判断

    # ──────────────────────────────────────────
    # 主接口
    # ──────────────────────────────────────────

    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        """执行周期轮动策略

        Args:
            trade_date: 交易日期，None 取最新
            top_k:      输出数量

        Returns:
            标准 DataFrame
        """
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"\n[CyclicalStrategy] ===== 选股 {trade_date} =====")

        # Step 1: 判断宏观周期阶段
        cycle_stage, cycle_confidence = self._judge_cycle(trade_date)
        self._cycle_stage = cycle_stage
        sector_info = CYCLE_SECTOR_MAP.get(cycle_stage, CYCLE_SECTOR_MAP['mid_expansion'])
        logger.info(f"  [Step 1] 周期判断: {sector_info['label']} "
                    f"(置信度={cycle_confidence:.0%}) "
                    f"目标行业={sector_info['target'][:3]}...")

        # Step 2: 加载行业数据
        df = self._load_universe(trade_date)
        if df.empty:
            logger.error("[CyclicalStrategy] 数据为空")
            return self._empty_result()
        logger.info(f"  [Step 2] 全市场: {len(df)} 只")

        # Step 3: 筛选目标行业
        df = self._filter_target_sectors(df, sector_info)
        logger.info(f"  [Step 3] 目标行业过滤后: {len(df)} 只")

        if df.empty:
            # 降级：目标行业无数据时，用行业动量最强的行业
            df_all = self._load_universe(trade_date)
            df = self._fallback_momentum_sectors(df_all)
            logger.warning(f"  [Step 3] 降级到动量选行业: {len(df)} 只")

        # Step 4: 基础质量过滤
        df = self.filter_universe(df, min_mv_yi=10.0)
        logger.info(f"  [Step 4] 基础过滤后: {len(df)} 只")

        if df.empty:
            return self._empty_result()

        # Step 5: 行业内三指标评分
        df = self._score_in_sector(df)

        # Step 6: 拥挤度风控
        df = self._apply_crowding_control(df)

        # Step 7: 排序输出
        result = (df.sort_values('score', ascending=False)
                    .head(top_k)
                    .reset_index(drop=True))
        result['rank'] = range(1, len(result) + 1)
        result['strategy'] = self.name
        result['trade_date'] = trade_date
        result['cycle_stage'] = cycle_stage
        result['signal_reason'] = result.apply(
            lambda r: f"{sector_info['label']} [{r.get('industry', '')}] "
                      f"动量{float(r.get('mom_20', 0) or 0)*100:+.1f}%",
            axis=1
        )
        result['sub_scores'] = result.apply(lambda r: {
            'cycle_stage':   cycle_stage,
            'momentum_rank': round(float(r.get('mom_rank', 0) or 0), 3),
            'roe_rank':      round(float(r.get('roe_rank', 0) or 0), 3),
            'val_rank':      round(float(r.get('val_rank', 0) or 0), 3),
        }, axis=1)

        self._print_result(result, sector_info)

        out_cols = ['ts_code', 'name', 'score', 'rank', 'strategy',
                    'signal_reason', 'sub_scores', 'trade_date', 'industry']
        out_cols = [c for c in out_cols if c in result.columns]
        return result[out_cols]

    # ──────────────────────────────────────────
    # 宏观周期判断
    # ──────────────────────────────────────────

    def _judge_cycle(self, trade_date: str) -> Tuple[str, float]:
        """从 macro_indicators 表读取宏观信号，判断周期阶段

        五维信号（各取最近可用值）：
          pmi_trend       PMI趋势（当月-3月均值）
          credit_impulse  社融同比增速
          ppi_yoy         PPI同比
          rate_direction  MLF利率方向（+1涨/-1降/0不变）
          m1_m2_spread    M1-M2增速差

        Returns:
            (cycle_stage, confidence)
        """
        macro_data = self._load_macro_data()

        if not macro_data:
            # 无宏观数据：回退到行业动量判断（默认扩张中段）
            logger.warning("[CyclicalStrategy] 无宏观数据，默认 mid_expansion")
            return 'mid_expansion', 0.4

        # 打分（-1~+1 范围）
        signals = {}

        pmi = macro_data.get('pmi')
        if pmi is not None:
            # PMI > 50 且上升趋势 → 复苏信号
            pmi_trend = macro_data.get('pmi_trend', 0)
            signals['pmi'] = np.clip(pmi_trend / 2.0, -1, 1)   # 标准化

        ppi = macro_data.get('ppi_yoy')
        if ppi is not None:
            signals['ppi'] = np.clip(ppi / 5.0, -1, 1)   # PPI涨→扩张/过热

        m1_m2 = macro_data.get('m1_m2_spread')
        if m1_m2 is not None:
            signals['m1_m2'] = np.clip(m1_m2 / 5.0, -1, 1)   # M1>M2→企业活跃

        credit = macro_data.get('credit_impulse')
        if credit is not None:
            signals['credit'] = np.clip(credit / 5.0, -1, 1)

        if not signals:
            return 'mid_expansion', 0.4

        composite = np.mean(list(signals.values()))
        confidence = min(len(signals) / 5.0 + 0.2, 1.0)

        # 周期映射
        if composite > 0.3:
            if ppi is not None and ppi > 3.0:
                stage = 'late_cycle'
            else:
                stage = 'mid_expansion'
        elif composite > 0.0:
            stage = 'early_recovery'
        else:
            stage = 'recession'

        logger.info(f"  [Macro] composite={composite:.2f} 信号={signals}")
        return stage, confidence

    def _load_macro_data(self) -> dict:
        """从 macro_indicators 表读取最新宏观指标"""
        indicators = ['pmi', 'pmi_trend', 'ppi_yoy', 'm1_m2_spread',
                      'credit_impulse', 'rate_direction']
        placeholders = ','.join([f"'{i}'" for i in indicators])

        sql = f"""
        SELECT mi.indicator, mi.value
        FROM macro_indicators mi
        INNER JOIN (
            SELECT indicator, MAX(data_date) AS max_dt
            FROM macro_indicators
            WHERE indicator IN ({placeholders})
            GROUP BY indicator
        ) latest ON mi.indicator = latest.indicator
                 AND mi.data_date = latest.max_dt
        """
        try:
            df = DBUtils.query_df(sql)
            if df.empty:
                return {}
            return dict(zip(df['indicator'], df['value']))
        except Exception:
            return {}

    # ──────────────────────────────────────────
    # 数据加载
    # ──────────────────────────────────────────

    def _load_universe(self, trade_date: str) -> pd.DataFrame:
        """加载全市场基础数据（分开查询 stock_info 避免 collation 冲突）"""
        sql_max_daily  = "SELECT MAX(trade_date) AS dt FROM stock_daily WHERE trade_date <= ?"
        sql_max_factor = "SELECT MAX(trade_date) AS dt FROM stock_factors WHERE trade_date <= ?"

        sql_daily = """
        SELECT sd.ts_code,
               sd.total_mv,
               sd.roe,
               sd.pe_ttm
        FROM stock_daily sd
        WHERE sd.trade_date = ?
        """
        sql_factor = """
        SELECT sf.ts_code,
               sf.mom_20,
               sf.pe_inv,
               sf.turnover_approx,
               sf.log_mv
        FROM stock_factors sf
        WHERE sf.trade_date = ?
        """
        sql_info = "SELECT ts_code, name, industry FROM stock_info WHERE industry IS NOT NULL AND industry != ''"

        try:
            max_daily  = DBUtils.query_df(sql_max_daily, params=(trade_date,)).iloc[0]['dt']
            max_factor = DBUtils.query_df(sql_max_factor, params=(trade_date,)).iloc[0]['dt']

            df      = DBUtils.query_df(sql_daily, params=(max_daily,))
            df_fac  = DBUtils.query_df(sql_factor, params=(max_factor,))
            df_info = DBUtils.query_df(sql_info)

            # Python 端 merge（避免跨表 collation 问题）
            df = df.merge(df_fac,  on='ts_code', how='left')
            df = df.merge(df_info, on='ts_code', how='inner')  # inner：只保留有行业的股票

            df['name']     = df['name'].where(df['name'].notna(), df['ts_code'])
            df['industry'] = df['industry'].fillna('')

            for col in ['total_mv', 'pe_ttm', 'roe', 'mom_20',
                        'pe_inv', 'turnover_approx', 'log_mv']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            return df
        except Exception as e:
            logger.error(f"[CyclicalStrategy] 数据加载失败: {e}")
            return pd.DataFrame()

    # ──────────────────────────────────────────
    # 行业过滤
    # ──────────────────────────────────────────

    def _filter_target_sectors(self, df: pd.DataFrame,
                                sector_info: dict) -> pd.DataFrame:
        """筛选目标行业，排除规避行业"""
        targets = sector_info.get('target', [])
        avoids  = sector_info.get('avoid', [])

        # 行业匹配（模糊匹配：industry 包含目标关键词）
        def match_any(industry: str, keywords: list) -> bool:
            if not industry:
                return False
            return any(kw in industry for kw in keywords)

        target_mask = df['industry'].apply(lambda x: match_any(str(x), targets))
        avoid_mask  = df['industry'].apply(lambda x: match_any(str(x), avoids))

        result = df[target_mask & ~avoid_mask].reset_index(drop=True)
        return result

    def _fallback_momentum_sectors(self, df: pd.DataFrame,
                                    top_n_sectors: int = 3) -> pd.DataFrame:
        """降级：选行业动量最强的前N个行业"""
        if 'mom_20' not in df.columns or df['mom_20'].isna().all():
            return df.head(200)

        sector_mom = (df.groupby('industry')['mom_20']
                      .mean()
                      .nlargest(top_n_sectors)
                      .index.tolist())
        return df[df['industry'].isin(sector_mom)].reset_index(drop=True)

    # ──────────────────────────────────────────
    # 行业内三指标评分
    # ──────────────────────────────────────────

    def _score_in_sector(self, df: pd.DataFrame) -> pd.DataFrame:
        """在各行业内分别排名，再合成跨行业可比分"""
        df = df.copy()

        # 按行业内排名（0~1之间）
        def intra_rank(series: pd.Series, group: pd.Series,
                       ascending: bool = True) -> pd.Series:
            """在组内做排名，返回 [0,1] 分位值"""
            result = pd.Series(0.5, index=series.index)
            for g, idx in group.groupby(group).groups.items():
                sub = series.loc[idx].dropna()
                if len(sub) < 2:
                    result.loc[idx] = 0.5
                    continue
                ranked = sub.rank(ascending=ascending, method='average')
                result.loc[sub.index] = (ranked - 1) / (len(sub) - 1)
            return result

        # 行业内相对动量（高→好）
        df['mom_rank'] = intra_rank(
            df['mom_20'].fillna(0), df['industry'], ascending=True
        )
        # 行业内ROE排名（高→好）
        df['roe_rank'] = intra_rank(
            df['roe'].fillna(0), df['industry'], ascending=True
        )
        # 行业内估值排名（pe_inv高=PE低=便宜→好）
        df['val_rank'] = intra_rank(
            df['pe_inv'].fillna(df['pe_inv'].median()),
            df['industry'], ascending=True
        )

        # 合成
        df['raw_score'] = (
            0.40 * df['mom_rank'] +
            0.30 * df['roe_rank'] +
            0.30 * df['val_rank']
        )
        df['score'] = self._normalize_score(df['raw_score'])
        return df

    def _apply_crowding_control(self, df: pd.DataFrame) -> pd.DataFrame:
        """拥挤度风控：行业换手率Z分>2 → 该行业stock分×0.6"""
        if 'turnover_approx' not in df.columns:
            return df

        # 计算各行业的换手率Z分（用整体均值标准差）
        global_mean = df['turnover_approx'].mean()
        global_std  = df['turnover_approx'].std()
        if global_std < 1e-8:
            return df

        sector_turn = (df.groupby('industry')['turnover_approx']
                       .mean()
                       .reset_index()
                       .rename(columns={'turnover_approx': 'sector_turn'}))
        sector_turn['turn_zscore'] = (
            (sector_turn['sector_turn'] - global_mean) / global_std
        )

        crowded = sector_turn[
            sector_turn['turn_zscore'] > CROWDING_ZSCORE_THRESHOLD
        ]['industry'].tolist()

        if crowded:
            logger.warning(f"  [Crowding] 拥挤行业（score×0.6）: {crowded}")
            mask = df['industry'].isin(crowded)
            df.loc[mask, 'score'] *= 0.6

        return df

    # ──────────────────────────────────────────
    # 辅助
    # ──────────────────────────────────────────

    def _print_result(self, result: pd.DataFrame, sector_info: dict):
        logger.info(f"\n[CyclicalStrategy] ===== Top {len(result)} "
                    f"[{sector_info['label']}] =====")
        for _, row in result.iterrows():
            logger.info(
                f"  #{int(row['rank']):2d} {row['ts_code']} "
                f"{str(row.get('name', ''))[:6]:6s} "
                f"Score={float(row['score']):.3f} "
                f"动量={float(row.get('mom_20', 0) or 0)*100:+.1f}% "
                f"[{row.get('industry', '')}]"
            )

    @staticmethod
    def _empty_result() -> pd.DataFrame:
        return pd.DataFrame(columns=[
            'ts_code', 'name', 'score', 'rank', 'strategy',
            'signal_reason', 'sub_scores', 'trade_date', 'industry',
        ])
