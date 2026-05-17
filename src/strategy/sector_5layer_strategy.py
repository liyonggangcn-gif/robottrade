"""
5层量化赛道轮动策略 (Sector5LayerStrategy)

核心逻辑：赛道得分 = 趋势×权重 + 资金×权重 + 景气度×权重 + 流动性×权重 + 拥挤度反向×权重
1. 先筛行业（保留top 50%）
2. 再在头部行业中选股
"""
import numpy as np
import pandas as pd
from loguru import logger
from datetime import datetime, timedelta

from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils


class Sector5LayerStrategy(BaseStrategy):
    name = "sector_5layer"
    display_name = "5层量化赛道策略"
    version = "1.0"

    # 5层权重
    W_TREND = 0.35        # 趋势层
    W_MONEY = 0.25        # 资金层
    W_FUNDA = 0.20        # 景气度层
    W_LIQUID = 0.10       # 流动性层
    W_CROWD = 0.10        # 拥挤度层(反向)

    # 行业保留比例
    SECTOR_KEEP_PCT = 0.50

    def run(self, trade_date=None, top_k=20):
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"[{self.name}] 开始选股 date={trade_date} top_k={top_k}")

        # 1. 加载基础数据
        stocks = self._load_stock_universe()
        if stocks.empty:
            return self._empty_result()

        # 2. 加载行情 & 因子数据
        prices = self._load_price_data(trade_date)
        factors = self._load_factors()
        hist = self._load_60d_history(trade_date)

        # 3. 合并
        merged = stocks.merge(prices, on='ts_code', how='left')
        merged = merged.merge(factors, on='ts_code', how='left')
        merged['name'] = merged.get('name', merged.get('name_x', ''))

        # 4. 基础过滤
        merged = self.filter_universe(merged)

        # 5. 计算行业层面5层得分
        ind_scores = self._score_industries(merged, hist, trade_date)

        # 6. 保留top50%行业
        ind_scores = ind_scores.sort_values('composite', ascending=False)
        keep_n = max(1, int(len(ind_scores) * self.SECTOR_KEEP_PCT))
        top_industries = set(ind_scores.head(keep_n)['industry'])
        logger.info(f"[{self.name}] 保留 {keep_n}/{len(ind_scores)} 个行业: {top_industries}")

        # 7. 在头部行业中选股
        candidates = merged[merged['industry'].isin(top_industries)].copy()
        if candidates.empty:
            logger.warning(f"[{self.name}] 头部行业无候选股")
            return self._empty_result()

        candidates = self._score_stocks(candidates, ind_scores)
        candidates = candidates.sort_values('score', ascending=False).head(top_k)

        # 8. 组装输出
        result = []
        for i, (_, row) in enumerate(candidates.iterrows()):
            result.append({
                'ts_code': row['ts_code'],
                'name': row.get('name', ''),
                'score': round(float(row.get('score', 0) or 0), 4),
                'rank': i + 1,
                'strategy': self.name,
                'signal_reason': f"行业:{row.get('industry','')} 赛道分:{float(row.get('_ind_composite', 0) or 0):.1f}",
                'sub_scores': {
                    'trend': round(float(row.get('_trend_score', 0) or 0), 2),
                    'money': round(float(row.get('_money_score', 0) or 0), 2),
                    'funda': round(float(row.get('_funda_score', 0) or 0), 2),
                    'liquid': round(float(row.get('_liquid_score', 0) or 0), 2),
                    'crowd': round(float(row.get('_crowd_score', 0) or 0), 2),
                    'ind_composite': round(float(row.get('_ind_composite', 0) or 0), 2),
                    'stock_score': round(float(row.get('_stock_score', 0) or 0), 4),
                },
                'trade_date': trade_date,
            })

        df_result = pd.DataFrame(result)
        logger.info(f"[{self.name}] 选出 {len(df_result)} 只")
        return df_result

    # ────────────────────────────────────
    # 数据加载
    # ────────────────────────────────────

    def _load_stock_universe(self):
        """加载A股股票池（含行业分类）"""
        sql = """
            SELECT ts_code, name, industry, pe_ttm as info_pe, pb as info_pb,
                   total_mv as info_mv
            FROM stock_info
            WHERE market = 'A'
              AND industry IS NOT NULL AND industry != ''
        """
        df = DBUtils.query_df(sql)
        if df is not None and not df.empty:
            logger.info(f"[{self.name}] 股票池 {len(df)} 只")
        return df

    def _load_price_data(self, trade_date):
        """加载最新交易日行情"""
        sql = """
            SELECT ts_code, close, open, high, low, vol, amount,
                   pe_ttm, total_mv
            FROM stock_daily
            WHERE trade_date = ?
        """
        df = DBUtils.query_df(sql, params=[trade_date])
        if df is not None and not df.empty:
            # vol是成交量(股), amount是成交额(元)
            # 计算日均成交额(亿元)
            df['turnover_yi'] = df['amount'] / 1e8
            df['mv_yi'] = df['total_mv'] / 1e4  # total_mv是万元→亿
            logger.info(f"[{self.name}] 行情数据 {len(df)} 条")
        return df

    def _load_factors(self):
        """加载最新因子数据（质量/成长/换手等）"""
        try:
            sql = """
                SELECT sf.ts_code, sf.quality_score, sf.growth_score,
                       sf.roe_factor, sf.pe_inv, sf.pb_inv,
                       sf.turnover_ratio, sf.vol_ratio, sf.rsi_14
                FROM stock_factors sf
                INNER JOIN (
                    SELECT ts_code, MAX(trade_date) as max_date
                    FROM stock_factors GROUP BY ts_code
                ) lm ON sf.ts_code = lm.ts_code AND sf.trade_date = lm.max_date
            """
            df = DBUtils.query_df(sql)
            if df is not None and not df.empty:
                logger.info(f"[{self.name}] 因子数据 {len(df)} 条 (最新日≈{df.iloc[0]['max_date'] if 'max_date' in df.columns else '?'})")
                # Drop max_date if present (from internal join)
                if 'max_date' in df.columns:
                    df = df.drop(columns=['max_date'])
            return df
        except Exception as e:
            logger.warning(f"[{self.name}] 因子加载失败: {e}")
            return pd.DataFrame()

    def _load_60d_history(self, trade_date):
        """加载近60个交易日的收盘价（仅限股票池中的股票）"""
        try:
            # 先获取股票池的ts_code列表，减少数据量
            stock_codes = DBUtils.query_df(
                "SELECT ts_code FROM stock_info WHERE market='A' AND industry!=''"
            )
            if stock_codes is None or stock_codes.empty:
                return pd.DataFrame()
            codes = tuple(stock_codes['ts_code'].tolist())
            ph = ','.join(['?'] * len(codes))

            start = (pd.Timestamp(trade_date) - timedelta(days=90)).strftime('%Y-%m-%d')
            sql = f"""
                SELECT ts_code, trade_date, close, vol
                FROM stock_daily
                WHERE ts_code IN ({ph})
                  AND trade_date >= ? AND trade_date <= ?
                ORDER BY ts_code, trade_date
            """
            params = list(codes) + [start, trade_date]
            df = DBUtils.query_df(sql, params=params)
            if df is not None and not df.empty:
                logger.info(f"[{self.name}] 历史数据 {len(df)} 条, 日期范围 {df['trade_date'].min()} ~ {df['trade_date'].max()}")
            return df
        except Exception as e:
            logger.warning(f"[{self.name}] 历史数据加载失败: {e}")
            return pd.DataFrame()

    # ────────────────────────────────────
    # 5层行业评分
    # ────────────────────────────────────

    def _score_industries(self, stocks, hist, trade_date):
        """为每个行业计算5层得分"""
        if stocks is None or stocks.empty:
            return pd.DataFrame(columns=['industry', 'composite'])

        # 构建: ts_code → industry 映射
        ind_map = stocks[['ts_code', 'industry']].dropna(subset=['industry']).set_index('ts_code')['industry'].to_dict()
        if not ind_map:
            logger.warning(f"[{self.name}] 无行业映射数据")
            return pd.DataFrame(columns=['industry', 'composite'])

        # 将行业映射到历史数据
        if hist is not None and not hist.empty:
            hist['industry'] = hist['ts_code'].map(ind_map)
            hist = hist.dropna(subset=['industry'])

        # 计算各行业指标
        # 先确保日期排序
        if hist is not None and not hist.empty:
            hist = hist.sort_values(['ts_code', 'trade_date'])

            # 计算每只股票的20日动量
            hist['mom_20'] = hist.groupby('ts_code')['close'].transform(
                lambda x: x.pct_change(periods=min(20, len(x) - 1)) if len(x) > 20 else 0
            )

            # 计算MA20 (20日均线)
            hist['ma20'] = hist.groupby('ts_code')['close'].transform(
                lambda x: x.rolling(min(20, len(x)), min_periods=1).mean()
            )

            # 标记是否站上MA20
            hist['above_ma20'] = hist['close'] >= hist['ma20']

            # 最近交易日标记（trade_date列等于目标日期）
            latest = hist[hist['trade_date'] == trade_date].copy()
        else:
            latest = pd.DataFrame()

        # 行业层面聚合
        ind_stats = {}

        for ts_code, industry in ind_map.items():
            if industry not in ind_stats:
                ind_stats[industry] = {
                    'n_stocks': 0,
                    'mom_20_vals': [],
                    'above_ma20_cnt': 0,
                    'above_ma20_total': 0,
                    'turnover_vals': [],
                    'mv_vals': [],
                    'pe_vals': [],
                    'quality_vals': [],
                    'growth_vals': [],
                    'roe_vals': [],
                    'pe_inv_vals': [],
                }

            stats = ind_stats[industry]
            stats['n_stocks'] += 1

            # 最新行情
            row = stocks[stocks['ts_code'] == ts_code]
            if not row.empty:
                r = row.iloc[0]
                # 流动性
                if pd.notna(r.get('turnover_yi')):
                    stats['turnover_vals'].append(r['turnover_yi'])
                if pd.notna(r.get('mv_yi')):
                    stats['mv_vals'].append(r['mv_yi'])
                # PE
                if pd.notna(r.get('pe_ttm')) and r['pe_ttm'] > 0 and r['pe_ttm'] < 10000:
                    stats['pe_vals'].append(r['pe_ttm'])

            # 因子数据
            fr = stocks[stocks['ts_code'] == ts_code]
            if not fr.empty:
                r = fr.iloc[0]
                if pd.notna(r.get('quality_score')):
                    stats['quality_vals'].append(r['quality_score'])
                if pd.notna(r.get('growth_score')):
                    stats['growth_vals'].append(r['growth_score'])
                if pd.notna(r.get('roe_factor')):
                    stats['roe_vals'].append(r['roe_factor'])
                if pd.notna(r.get('pe_inv')):
                    stats['pe_inv_vals'].append(r['pe_inv'])

            # 历史数据：动量和MA20
            if not latest.empty:
                lr = latest[latest['ts_code'] == ts_code]
                if not lr.empty:
                    l = lr.iloc[0]
                    if pd.notna(l.get('mom_20')):
                        stats['mom_20_vals'].append(l['mom_20'])
                    stats['above_ma20_total'] += 1
                    if l.get('above_ma20', False):
                        stats['above_ma20_cnt'] += 1

        # 计算每行业得分
        rows = []
        for industry, s in ind_stats.items():
            d = {'industry': industry, 'n_stocks': s['n_stocks']}

            # 趋势层
            avg_mom = np.mean(s['mom_20_vals']) if s['mom_20_vals'] else 0
            pct_above = s['above_ma20_cnt'] / max(s['above_ma20_total'], 1)
            d['trend_mom'] = avg_mom
            d['trend_ma20'] = pct_above

            # 资金层(成交量趋势)
            vol_trend = 0
            if not latest.empty and industry in ind_stats:
                # 计算行业成交额趋势(5d vs 20d)
                ind_codes = [c for c, ind in ind_map.items() if ind == industry and c in stocks['ts_code'].values]
                if ind_codes and hist is not None and not hist.empty:
                    ind_hist = hist[hist['ts_code'].isin(ind_codes)].copy()
                    if not ind_hist.empty:
                        # 按日期聚合
                        daily = ind_hist.groupby('trade_date')['vol'].sum()
                        if len(daily) >= 20:
                            vol_5d = daily.tail(5).mean()
                            vol_20d = daily.tail(20).mean()
                            vol_trend = (vol_5d / vol_20d - 1) if vol_20d > 0 else 0
            d['vol_trend'] = vol_trend

            # 景气度层
            avg_quality = np.mean(s['quality_vals']) if s['quality_vals'] else 0
            avg_roe = np.mean(s['roe_vals']) if s['roe_vals'] else 0
            d['quality'] = avg_quality
            d['roe'] = avg_roe

            # 流动性层
            avg_turnover = np.mean(s['turnover_vals']) if s['turnover_vals'] else 0
            avg_mv = np.mean(s['mv_vals']) if s['mv_vals'] else 0
            d['turnover'] = avg_turnover
            d['mv'] = avg_mv

            # 拥挤度层(PE百分位 - 越高越拥挤)
            pe_pct = 0.5
            if s['pe_vals']:
                pe_median = np.median(s['pe_vals'])
                # PE合理范围 5-50 (超过50的高PE股多=拥挤)
                pe_pct = min(1.0, pe_median / 50.0) if pe_median > 0 else 0.5
            d['pe_crowded'] = pe_pct

            rows.append(d)

        ind_df = pd.DataFrame(rows)
        if ind_df.empty:
            return pd.DataFrame(columns=['industry', 'composite'])

        # 各层归一化打分(0-100)
        # 趋势层: 动量z-score归一化
        ind_df['_t'] = self._norm_score(ind_df['trend_mom'], higher_better=True) * 0.6 + \
                       self._norm_score(ind_df['trend_ma20'], higher_better=True) * 0.4
        # 资金层: 成交量趋势
        ind_df['_m'] = self._norm_score(ind_df['vol_trend'], higher_better=True)
        # 景气度层: 质量+ROE
        ind_df['_f'] = self._norm_score(ind_df['quality'], higher_better=True) * 0.5 + \
                       self._norm_score(ind_df['roe'], higher_better=True) * 0.5
        # 流动性层: 成交额 + 市值(取log)
        ind_df['_l'] = self._norm_score(np.log1p(ind_df['turnover']), higher_better=True) * 0.6 + \
                       self._norm_score(np.log1p(ind_df['mv']), higher_better=True) * 0.4
        # 拥挤度层(反向): PE越低越好
        ind_df['_c'] = self._norm_score(-ind_df['pe_crowded'], higher_better=True)

        # 综合
        ind_df['composite'] = (
            ind_df['_t'] * self.W_TREND +
            ind_df['_m'] * self.W_MONEY +
            ind_df['_f'] * self.W_FUNDA +
            ind_df['_l'] * self.W_LIQUID +
            ind_df['_c'] * self.W_CROWD
        ) * 100

        logger.info(f"[{self.name}] 行业评分完成 {len(ind_df)} 个行业, "
                     f"最佳={ind_df['composite'].max():.1f} 最差={ind_df['composite'].min():.1f}")
        return ind_df

    # ────────────────────────────────────
    # 行业内选股
    # ────────────────────────────────────

    def _score_stocks(self, candidates, ind_scores):
        """在头部行业中给个股打分，返回[0,1]归一化score"""
        if candidates.empty:
            return candidates

        stocks = []
        ind_scores_map = ind_scores.set_index('industry').to_dict('index')

        for _, row in candidates.iterrows():
            ts = row['ts_code']
            ind = row.get('industry', '')

            # 个股质量分
            qs = float(row.get('quality_score', 0) or 0)
            roe = float(row.get('roe_factor', 0) or 0)
            pe_inv = float(row.get('pe_inv', 0) or 0)
            stock_raw = qs * 0.5 + roe * 0.3 + pe_inv * 100 * 0.2

            # 行业得分
            ind_info = ind_scores_map.get(ind, {})
            ind_composite = ind_info.get('composite', 50)

            entry = {
                'ts_code': ts,
                'ind_composite': ind_composite,
                'stock_raw': stock_raw,
                '_trend_score': ind_info.get('_t', 0.5) * 100,
                '_money_score': ind_info.get('_m', 0.5) * 100,
                '_funda_score': ind_info.get('_f', 0.5) * 100,
                '_liquid_score': ind_info.get('_l', 0.5) * 100,
                '_crowd_score': ind_info.get('_c', 0.5) * 100,
            }
            stocks.append(entry)

        df = pd.DataFrame(stocks)
        if df.empty:
            return candidates

        # 归一化个股原始分到[0,1]
        mn, mx = df['stock_raw'].min(), df['stock_raw'].max()
        df['stock_score'] = (df['stock_raw'] - mn) / (mx - mn) if mx > mn else 0.5

        # 最终分 = 行业(0-100归一化)×0.7 + 个股(0-1)×0.3
        df['score'] = (df['ind_composite'] / 100.0) * 0.7 + df['stock_score'] * 0.3
        df['score'] = df['score'].clip(0, 1)

        # 合并回candidates
        keep_cols = ['ts_code', 'score', 'ind_composite', 'stock_score',
                     '_trend_score', '_money_score', '_funda_score', '_liquid_score', '_crowd_score']
        result = candidates.merge(df[keep_cols], on='ts_code', how='left')
        result['_ind_composite'] = result['ind_composite']
        result['_stock_score'] = result['stock_score']

        logger.info(f"[{self.name}] _score_stocks: 评分{len(df)}只, "
                     f"行业分范围[{df['ind_composite'].min():.0f}-{df['ind_composite'].max():.0f}], "
                     f"最终分范围[{df['score'].min():.3f}-{df['score'].max():.3f}]")
        return result

    # ────────────────────────────────────
    # 工具
    # ────────────────────────────────────

    @staticmethod
    def _norm_score(series, higher_better=True):
        """归一化到[0,1]，higher_better控制方向"""
        if isinstance(series, pd.Series):
            vals = series.values
        else:
            vals = np.array(series)

        if not higher_better:
            vals = -vals

        mn, mx = np.nanmin(vals), np.nanmax(vals)
        if mx == mn or np.isnan(mx - mn):
            return pd.Series(np.full(len(vals), 0.5), index=series.index if isinstance(series, pd.Series) else range(len(vals)))

        result = (vals - mn) / (mx - mn)
        result = np.nan_to_num(result, nan=0.5)
        return pd.Series(result, index=series.index if isinstance(series, pd.Series) else range(len(vals)))
