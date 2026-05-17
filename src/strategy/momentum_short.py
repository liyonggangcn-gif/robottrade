"""
中期动量策略 (Medium-term Momentum Strategy)
- 基于40天动量因子 (回测多空收益+240%)
- 避免短期噪音，只做中期趋势
"""

import pandas as pd
import numpy as np
from loguru import logger
from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils


class MomentumShortTermStrategy(BaseStrategy):
    """中期动量策略 - 40天趋势"""
    
    name = "momentum_short"
    display_name = "中期动量"
    version = "5.0"
    
    def __init__(self):
        self.lookback_days = 40    # 40天动量 (有效)
        self.top_pct = 0.10        # 取前10%强势股
        self.min_mv_yi = 30        # 最小市值30亿
        self.min_pe = 5
        self.max_pe = 50
        
    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        if trade_date is None:
            trade_date = self._resolve_trade_date()
            
        logger.info(f"[中期动量] 开始选股 {trade_date}")
        
        # Step 1: 获取过去N天的交易日
        dates = self._get_trade_dates(trade_date, self.lookback_days + 10)
        if len(dates) < self.lookback_days:
            logger.warning(f"[中期动量] 交易日不足: {len(dates)}")
            return self._empty_result()
        
        # Step 2: 计算动量收益
        df = self._calculate_momentum(dates)
        if df.empty:
            return self._empty_result()
        
        # Step 3: 基础过滤 (ST/退市/极小市值)
        df = self.filter_universe(df, min_mv_yi=self.min_mv_yi, min_days_listed=60)
        
        # Step 4: 仅过滤负PE (避免亏损股)，NaN视为有效
        if 'pe_ttm' in df.columns:
            df = df[(df['pe_ttm'] > 0) | (df['pe_ttm'].isna())]
        
        if df.empty:
            return self._empty_result()
        
        # Step 5: 取涨幅前10% (动量效应)
        df = df.sort_values('momentum_return', ascending=False)
        n_select = max(int(len(df) * self.top_pct), top_k)
        df_top = df.head(n_select).copy()
        
        if len(df_top) == 0:
            return self._empty_result()
        
        # Step 6: 评分
        df_top['score'] = self._rank_norm(df_top['momentum_return'], ascending=True)
        
        # Step 7: 输出
        df_top['rank'] = range(1, len(df_top) + 1)
        df_top['strategy'] = self.name
        df_top['trade_date'] = trade_date
        df_top['signal_reason'] = df_top.apply(
            lambda x: f"40日涨{x['momentum_return']:.1%}", axis=1
        )
        df_top['sub_scores'] = df_top.apply(
            lambda x: {'momentum': x['momentum_return'], 'pe': x.get('pe_ttm', 0)}, 
            axis=1
        )
        
        result = df_top.head(top_k)[['ts_code', 'name', 'score', 'rank', 'strategy', 
                                       'signal_reason', 'sub_scores', 'trade_date']]
        
        logger.info(f"[中期动量] 选出 {len(result)} 只")
        return result
    
    def _get_trade_dates(self, end_date: str, n: int) -> list:
        df = DBUtils.query_df(f"""
            SELECT DISTINCT trade_date FROM stock_daily 
            WHERE trade_date <= '{end_date}' 
            ORDER BY trade_date DESC 
            LIMIT {n}
        """)
        if df is None or df.empty:
            return []
        return df['trade_date'].tolist()[::-1]
    
    def _calculate_momentum(self, dates: list) -> pd.DataFrame:
        if len(dates) < 2:
            return pd.DataFrame()
        
        start_date = dates[0]
        end_date = dates[-1]
        
        df_start = DBUtils.query_df(f"""
            SELECT ts_code, close as price_start, total_mv, pe_ttm
            FROM stock_daily 
            WHERE trade_date = '{start_date}'
        """)
        
        df_info = DBUtils.query_df("SELECT ts_code, name FROM stock_info")
        
        df_end = DBUtils.query_df(f"""
            SELECT ts_code, close as price_end
            FROM stock_daily 
            WHERE trade_date = '{end_date}'
        """)
        
        if df_start is None or df_start.empty or df_end is None or df_end.empty:
            return pd.DataFrame()
        
        df = pd.merge(df_start, df_end, on='ts_code', how='inner')
        
        if df_info is not None and not df_info.empty:
            df = pd.merge(df, df_info, on='ts_code', how='left')
        
        # 计算40天动量收益
        df['momentum_return'] = (df['price_end'] / df['price_start'] - 1)
        
        # 不再剔除涨跌幅，40天可能涨很多
        
        if 'name' not in df.columns:
            df['name'] = df['ts_code']
        
        return df