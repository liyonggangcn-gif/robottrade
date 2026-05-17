"""
ValueStrategy: 价值选股策略

选股逻辑 —— 四维综合评分，追求"GARP"(Growth At Reasonable Price):

  1. 高增长 Growth      (30%) — 净利润同比增长、营收增长
  2. 高利润 Profitability(25%) — ROE、毛利率(GPR)
  3. 高护城河 Moat       (25%) — ROE稳定性、毛利率水平、市场规模
  4. 低估值 Valuation    (20%) — PE、PB、PEG(PE/成长率)

硬性过滤门槛（不达标直接排除）:
  - 非ST / 非退市
  - 净利润正增长(netprofit_yoy > -30%)
  - ROE > 8%（最低盈利质量要求）
  - 0 < PE < 80（有盈利且不太离谱）
  - 总市值 > 20亿（排除微型公司，护城河一般很弱）

护城河判断逻辑:
  - 毛利率 > 40%：定价权强（如茅台、片仔癀）
  - ROE > 20%：资本回报率优秀
  - ROE 连续多期稳定（用历史数据检验）
  - 市值越大一般护城河越深（用log_mv作为辅助）

PEG = PE / (netprofit_yoy * 100)，PEG < 1 为黄金指标
"""

import pandas as pd
import numpy as np
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class ValueStrategy:
    """价值选股策略: 高增长 × 高利润 × 高护城河 × 低估值"""

    # 四维权重
    W_GROWTH = 0.30
    W_PROFITABILITY = 0.25
    W_MOAT = 0.25
    W_VALUATION = 0.20

    # 硬性过滤门槛
    MIN_ROE = 0.0           # 取消最低ROE限制，仅作评分
    MAX_PE = 80.0           # 最高PE
    MIN_PE = 0.0            # 最低PE（必须盈利）
    MIN_MV_YI = 20.0        # 最低总市值（亿元）
    MIN_PROFIT_YOY = -50.0  # 最低净利润增长率(%)，放宽以保留更多候选股

    def __init__(self):
        print("[ValueStrategy] 初始化完成")
        print(f"  权重: 增长={self.W_GROWTH}, 盈利={self.W_PROFITABILITY}, "
              f"护城河={self.W_MOAT}, 估值={self.W_VALUATION}")

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _load_universe(self, trade_date):
        """加载股票池，含全量财务指标

        Args:
            trade_date: str, YYYY-MM-DD

        Returns:
            DataFrame with: ts_code, name, close, pe_ttm, pb, roe,
                            gpr, netprofit_yoy, total_mv, industry
        """
        sql = """
        SELECT
            sd.ts_code,
            COALESCE(si.name, sd.ts_code) AS name,
            sd.close,
            sd.pe_ttm,
            COALESCE(si.pb, 0)  AS pb,
            COALESCE(fd.roe, sd.roe, 0)  AS roe,
            sd.gpr,
            sd.netprofit_yoy,
            COALESCE(si.total_mv, sd.total_mv, 0)  AS total_mv,
            COALESCE(si.industry, '') AS industry
        FROM stock_daily sd
        LEFT JOIN stock_info si ON CONVERT(sd.ts_code USING utf8mb4) = CONVERT(si.ts_code USING utf8mb4)
        LEFT JOIN (
            SELECT ts_code, roe FROM financial_data
            WHERE end_date = (SELECT MAX(end_date) FROM financial_data fd2 WHERE fd2.ts_code = financial_data.ts_code)
        ) fd ON CONVERT(sd.ts_code USING utf8mb4) = CONVERT(fd.ts_code USING utf8mb4)
        WHERE sd.trade_date = ?
          AND sd.close IS NOT NULL
          AND sd.close > 0
        ORDER BY sd.ts_code
        """
        df = DBUtils.query_df(sql, params=[trade_date])
        # 确保数值类型
        numeric_cols = ['pe_ttm', 'pb', 'roe', 'gpr', 'netprofit_yoy', 'total_mv']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        print(f"[ValueStrategy] 加载股票池: {len(df)} 只 (日期: {trade_date})")
        return df

    def _load_roe_history(self, ts_codes, lookback_days=250):
        """加载近1年 ROE 历史，用于护城河稳定性评估

        Returns:
            DataFrame: ts_code, roe_std (越小越稳定), roe_min (最差期ROE)
        """
        try:
            codes_str = ','.join(f"'{c}'" for c in ts_codes[:500])  # 避免超长SQL
            sql = f"""
            SELECT ts_code, roe
            FROM stock_daily
            WHERE ts_code IN ({codes_str})
              AND roe IS NOT NULL AND roe > 0
              AND trade_date >= (
                  SELECT trade_date FROM (
                      SELECT DISTINCT trade_date FROM stock_daily
                      ORDER BY trade_date DESC LIMIT {lookback_days}
                  ) t ORDER BY trade_date ASC LIMIT 1
              )
            """
            df = DBUtils.query_df(sql)
            if df.empty:
                return pd.DataFrame(columns=['ts_code', 'roe_std', 'roe_min', 'roe_periods'])
            df['roe'] = pd.to_numeric(df['roe'], errors='coerce')
            hist = df.groupby('ts_code')['roe'].agg(
                roe_std='std',
                roe_min='min',
                roe_periods='count'
            ).reset_index()
            return hist
        except Exception as e:
            print(f"  [ValueStrategy] ROE历史加载失败: {e}")
            return pd.DataFrame(columns=['ts_code', 'roe_std', 'roe_min', 'roe_periods'])

    def _get_latest_trade_date(self):
        result = DBUtils.query_df("SELECT MAX(trade_date) as d FROM stock_daily")
        if not result.empty and pd.notna(result.iloc[0]['d']):
            return str(result.iloc[0]['d'])
        return None

    # ------------------------------------------------------------------
    # 过滤
    # ------------------------------------------------------------------

    def _apply_filters(self, df):
        """应用硬性过滤门槛"""
        before = len(df)

        # ST / 退市
        if 'name' in df.columns:
            df = df[~df['name'].str.contains('ST|退', na=False)]

        # 市值 > 20亿（万元单位: 20亿 = 200000万）
        df = df[df['total_mv'] >= self.MIN_MV_YI * 10000]

        # 必须盈利: 0 < PE < 80
        df = df[(df['pe_ttm'] > self.MIN_PE) & (df['pe_ttm'] < self.MAX_PE)]

        # ROE > 8%
        df = df[df['roe'] >= self.MIN_ROE]

        # 净利润不能严重萎缩
        # 没有netprofit_yoy数据的保留（数据缺失不等于不好）
        mask_yoy = (df['netprofit_yoy'].isna()) | (df['netprofit_yoy'] >= self.MIN_PROFIT_YOY)
        df = df[mask_yoy]

        after = len(df)
        print(f"  [Filter] {before} → {after} 只 "
              f"(过滤 {before - after} 只: ST/退市/低ROE/亏损/高PE)")
        return df.reset_index(drop=True)

    # ------------------------------------------------------------------
    # 四维评分
    # ------------------------------------------------------------------

    def _score_growth(self, df):
        """增长评分 (0~1)

        指标:
          - netprofit_yoy: 净利润同比增长（主要）
          - 超高增长（>100%）给额外加分，但做上限避免异常值主导
        """
        n = len(df)
        col = df['netprofit_yoy'].copy()

        # 有数据：按排名归一化（正增长优先）
        has_data = col.notna()
        scores = pd.Series(0.5, index=df.index)  # 无数据给中性0.5

        if has_data.sum() > 5:
            # clip: 排除异常值（>300% 视为一次性非经常性利润）
            col_clipped = col.clip(lower=-50, upper=300)
            rank = col_clipped[has_data].rank(method='average', ascending=True)
            norm = (rank - 1) / max(has_data.sum() - 1, 1)
            scores[has_data] = norm

        # 额外奖励：增长超过30%
        bonus_mask = has_data & (col >= 30)
        scores[bonus_mask] = (scores[bonus_mask] * 0.8 + 0.2).clip(upper=1.0)

        print(f"  [Growth] 有效数据 {has_data.sum()}/{n} 只, "
              f"增长>30%: {bonus_mask.sum()} 只")
        return scores.values

    def _score_profitability(self, df):
        """盈利评分 (0~1): ROE 60% + 毛利率 40%"""
        n = len(df)

        # ROE 排名
        roe = df['roe'].copy()
        roe_has = roe.notna() & (roe > 0)
        roe_score = pd.Series(0.3, index=df.index)
        if roe_has.sum() > 5:
            rank = roe[roe_has].rank(ascending=True)
            roe_score[roe_has] = (rank - 1) / max(roe_has.sum() - 1, 1)

        # 毛利率排名
        gpr = df['gpr'].copy()
        gpr_has = gpr.notna() & (gpr > 0)
        gpr_score = pd.Series(0.3, index=df.index)
        if gpr_has.sum() > 5:
            rank = gpr[gpr_has].rank(ascending=True)
            gpr_score[gpr_has] = (rank - 1) / max(gpr_has.sum() - 1, 1)

        combined = 0.6 * roe_score + 0.4 * gpr_score
        print(f"  [Profit] ROE有效 {roe_has.sum()}/{n} 只, "
              f"GPR有效 {gpr_has.sum()}/{n} 只, "
              f"ROE>20%: {(roe >= 20).sum()} 只")
        return combined.values

    def _score_moat(self, df, roe_history):
        """护城河评分 (0~1)

        三个维度:
          1. 毛利率水平 (40%): gpr > 40% 护城河强
          2. ROE稳定性  (35%): ROE历史波动小、最差期ROE高
          3. 市场规模   (25%): 大市值本身是护城河的一个体现
        """
        n = len(df)

        # --- 维度1: 毛利率水平 ---
        gpr = df['gpr'].copy()
        gpr_has = gpr.notna()
        gpr_moat = pd.Series(0.3, index=df.index)
        if gpr_has.sum() > 5:
            rank = gpr[gpr_has].rank(ascending=True)
            gpr_moat[gpr_has] = (rank - 1) / max(gpr_has.sum() - 1, 1)

        # --- 维度2: ROE稳定性 ---
        roe_stability = pd.Series(0.3, index=df.index)
        if not roe_history.empty:
            # 合并历史ROE统计
            merged = df[['ts_code']].merge(roe_history, on='ts_code', how='left')
            merged.index = df.index

            # 稳定性 = 1 / (1 + std)，std越小越稳定
            std = merged['roe_std'].fillna(10)  # 无历史数据给较大std
            roe_min = merged['roe_min'].fillna(0)

            stability_raw = 1.0 / (1.0 + std)
            # 最差期ROE > 15%额外加分
            min_bonus = (roe_min >= 15).astype(float) * 0.1

            stab_score = (stability_raw + min_bonus).clip(upper=1.0)
            # 归一化
            mn, mx = stab_score.min(), stab_score.max()
            if mx > mn:
                roe_stability = (stab_score - mn) / (mx - mn)
            else:
                roe_stability = stab_score

        # --- 维度3: 市场规模 ---
        mv = df['total_mv'].copy()
        mv_has = mv > 0
        mv_score = pd.Series(0.3, index=df.index)
        if mv_has.sum() > 5:
            log_mv = np.log(mv.clip(lower=1))
            rank = log_mv[mv_has].rank(ascending=True)
            mv_score[mv_has] = (rank - 1) / max(mv_has.sum() - 1, 1)

        combined = 0.40 * gpr_moat + 0.35 * roe_stability + 0.25 * mv_score
        print(f"  [Moat] 护城河评分完成 "
              f"(ROE历史覆盖 {len(roe_history)} 只)")
        return combined.values

    def _score_valuation(self, df):
        """估值评分 (0~1): PE越低越好，PEG最佳

        PEG = PE / (netprofit_yoy / 100)
          - PEG < 0.5: 极度低估
          - PEG < 1.0: 合理偏低
          - PEG > 2.0: 偏贵

        PE分段打分 + PEG修正
        """
        n = len(df)

        # --- PE评分（倒数排名）---
        pe = df['pe_ttm'].copy()
        pe_score = pd.Series(0.5, index=df.index)
        pe_has = pe.notna() & (pe > 0) & (pe < self.MAX_PE)
        if pe_has.sum() > 5:
            # pe越低排名越高，所以 ascending=False
            rank = pe[pe_has].rank(ascending=False)
            pe_score[pe_has] = (rank - 1) / max(pe_has.sum() - 1, 1)

        # --- PEG评分（修正PE, 考虑成长性）---
        peg_score = pe_score.copy()
        yoy = df['netprofit_yoy']
        yoy_valid = yoy.notna() & (yoy > 5)  # 增长率至少5%才有意义算PEG

        if yoy_valid.sum() > 5:
            peg = pe / (yoy / 100).clip(lower=0.05)  # 避免除以很小的数
            peg = peg.where(yoy_valid, other=np.nan)

            peg_has = peg.notna() & (peg > 0) & (peg < 50)
            rank_peg = peg[peg_has].rank(ascending=False)  # peg越低越好
            peg_norm = pd.Series(np.nan, index=df.index)
            peg_norm[peg_has] = (rank_peg - 1) / max(peg_has.sum() - 1, 1)

            # 有PEG的用 PE*0.4 + PEG*0.6，没有PEG的纯用PE
            has_both = pe_has & peg_has
            peg_score[has_both] = (0.4 * pe_score[has_both] + 0.6 * peg_norm[has_both])

        # PB 轻微修正（PB很高时降分）
        pb = df['pb'].copy()
        pb_has = pb.notna() & (pb > 0)
        if pb_has.sum() > 5:
            pb_penalty = (pb > 10).astype(float) * 0.05  # PB>10扣0.05
            peg_score -= pb_penalty

        peg_score = peg_score.clip(0.0, 1.0)
        peg_count = yoy_valid.sum()
        print(f"  [Valuation] PE有效 {pe_has.sum()}/{n} 只, "
              f"可计算PEG: {peg_count} 只")
        return peg_score.values

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(self, trade_date=None, top_k=10):
        """执行价值选股

        Args:
            trade_date: YYYY-MM-DD, None 则用最新交易日
            top_k: 返回前 N 只

        Returns:
            DataFrame: ts_code, name, close, pe_ttm, pb, roe, gpr,
                       netprofit_yoy, peg, growth_score, profit_score,
                       moat_score, valuation_score, value_score, industry
        """
        print("\n" + "=" * 60)
        print("  ValueStrategy - 价值选股（高增长×高利润×高护城河×低估值）")
        print("=" * 60)

        # 确定日期
        if trade_date is None:
            trade_date = self._get_latest_trade_date()
        if trade_date is None:
            print("[ERROR] 无可用交易数据")
            return self._empty_result()
        trade_date = pd.Timestamp(trade_date).strftime('%Y-%m-%d')
        print(f"\n[Step 1] 交易日期: {trade_date}")

        # 加载股票池
        df = self._load_universe(trade_date)
        if df.empty:
            return self._empty_result()

        # 硬性过滤
        print(f"\n[Step 2] 应用过滤门槛...")
        df = self._apply_filters(df)
        if df.empty:
            print("[WARN] 过滤后无符合条件股票")
            return self._empty_result()

        # 加载ROE历史（护城河评分需要）
        print(f"\n[Step 3] 加载ROE历史（用于护城河评估）...")
        roe_history = self._load_roe_history(df['ts_code'].tolist())

        # 四维评分
        print(f"\n[Step 4] 四维评分...")
        df['growth_score'] = self._score_growth(df)
        df['profit_score'] = self._score_profitability(df)
        df['moat_score'] = self._score_moat(df, roe_history)
        df['valuation_score'] = self._score_valuation(df)

        # 综合得分
        df['value_score'] = (
            self.W_GROWTH * df['growth_score'] +
            self.W_PROFITABILITY * df['profit_score'] +
            self.W_MOAT * df['moat_score'] +
            self.W_VALUATION * df['valuation_score']
        )

        # PEG 展示列
        yoy = df['netprofit_yoy']
        df['peg'] = (df['pe_ttm'] / (yoy / 100).clip(lower=0.05)).where(
            yoy.notna() & (yoy > 5), other=np.nan
        ).round(2)

        # 排序取 Top K
        df = df.sort_values('value_score', ascending=False).reset_index(drop=True)
        result = df.head(top_k).copy()

        # 输出列
        out_cols = ['ts_code', 'name', 'close', 'pe_ttm', 'pb', 'roe',
                    'gpr', 'netprofit_yoy', 'peg', 'total_mv', 'industry',
                    'growth_score', 'profit_score', 'moat_score',
                    'valuation_score', 'value_score']
        out_cols = [c for c in out_cols if c in result.columns]
        result = result[out_cols]

        # 打印摘要
        print(f"\n{'=' * 60}")
        print(f"  价值选股结果: Top {len(result)} / {len(df)} 只")
        print(f"{'=' * 60}")
        for i, (_, row) in enumerate(result.iterrows(), 1):
            roe_s = f"ROE={row['roe']:.1f}%" if pd.notna(row.get('roe')) else ""
            pe_s = f"PE={row['pe_ttm']:.1f}" if pd.notna(row.get('pe_ttm')) else ""
            peg_s = f"PEG={row['peg']:.2f}" if pd.notna(row.get('peg')) else ""
            yoy_s = f"增长={row['netprofit_yoy']:.1f}%" if pd.notna(row.get('netprofit_yoy')) else ""
            ind_s = f"[{row.get('industry', '')}]" if row.get('industry') else ""
            print(f"  #{i:2d} {row['ts_code']} {row.get('name', '')[:6]:>6s} "
                  f"Score={row['value_score']:.3f} {roe_s} {pe_s} {peg_s} {yoy_s} {ind_s}")

        return result

    @staticmethod
    def _empty_result():
        return pd.DataFrame(columns=[
            'ts_code', 'name', 'close', 'pe_ttm', 'pb', 'roe', 'gpr',
            'netprofit_yoy', 'peg', 'total_mv', 'industry',
            'growth_score', 'profit_score', 'moat_score',
            'valuation_score', 'value_score'
        ])


# ------------------------------------------------------------------
# 命令行入口
# ------------------------------------------------------------------
if __name__ == '__main__':
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    strategy = ValueStrategy()
    result = strategy.run(top_k=10)
    if result is not None and not result.empty:
        print(f"\n[DONE] 价值选股完成，共 {len(result)} 只")
    else:
        print("[DONE] 无选股结果")
