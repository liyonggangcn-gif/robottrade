"""
HighROE策略 - 增加趋势过滤降低回撤
- 回测: +12.8%/年, 回撤-47.3%
- 调整: 增加MA20过滤避免"价值陷阱"
"""

import pandas as pd
import numpy as np
from loguru import logger
from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils


class HighRoeStrategy(BaseStrategy):
    """高ROE策略 - 增加趋势过滤"""
    
    name = "high_roe"
    display_name = "高ROE"
    version = "3.0"
    
    def __init__(self):
        self.min_pe = 5
        self.max_pe = 30
        self.top_k = 10
        self.min_mv_yi = 50
        self.ma_days = 20
        
    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        if trade_date is None:
            trade_date = self._resolve_trade_date()
            
        logger.info(f"[高ROE] 开始选股 {trade_date}")
        
        df = self._get_data(trade_date)
        if df.empty:
            logger.warning("[高ROE] 无数据")
            return self._empty_result()
        
        df = df[(df['pe_ttm'] >= self.min_pe) & (df['pe_ttm'] <= self.max_pe)].copy()
        
        if df.empty:
            return self._empty_result()
        
        df['mv_yi'] = df['total_mv'].fillna(0) / 10000
        
        df_filtered = self.filter_universe(df, min_mv_yi=self.min_mv_yi, min_days_listed=365)
        
        # 趋势过滤: 价格在MA20之上
        df_filtered = self._filter_ma(df_filtered, trade_date)
        
        if df_filtered.empty:
            logger.warning("[高ROE] 趋势过滤后为空")
            return self._empty_result()
        
        df_filtered['pe_score'] = 1 - self._rank_norm(df_filtered['pe_ttm'], ascending=False)
        df_filtered['mv_score'] = self._rank_norm(df_filtered['mv_yi'], ascending=True)
        df_filtered['score'] = df_filtered['pe_score'] * 0.7 + df_filtered['mv_score'] * 0.3
        
        df_filtered = df_filtered.sort_values('score', ascending=False)
        df_filtered['rank'] = range(1, len(df_filtered) + 1)
        df_filtered['strategy'] = self.name
        df_filtered['trade_date'] = trade_date
        df_filtered['signal_reason'] = df_filtered.apply(
            lambda x: f"PE={x['pe_ttm']:.1f}", axis=1
        )
        df_filtered['sub_scores'] = df_filtered.apply(
            lambda x: {'pe': x['pe_ttm'], 'mv': x['mv_yi'], 'above_ma': x.get('above_ma20', False)}, 
            axis=1
        )
        
        result = df_filtered.head(top_k)[['ts_code', 'name', 'score', 'rank', 'strategy', 
                                           'signal_reason', 'sub_scores', 'trade_date']]
        
        logger.info(f"[高ROE] 选出 {len(result)} 只")
        return result
    
    def _get_data(self, trade_date: str) -> pd.DataFrame:
        df = DBUtils.query_df(f"""
            SELECT ts_code, pe_ttm, total_mv
            FROM stock_daily 
            WHERE trade_date = '{trade_date}' AND pe_ttm > 0
        """)
        
        if df is None or df.empty:
            return pd.DataFrame()
        
        df_info = DBUtils.query_df("SELECT ts_code, name FROM stock_info")
        if df_info is not None and not df_info.empty:
            df = pd.merge(df, df_info, on='ts_code', how='left')
        
        return df
    
    def _filter_ma(self, df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        if df.empty:
            return df
        
        dates = self._get_trade_dates(trade_date, self.ma_days + 5)
        if len(dates) < self.ma_days:
            return df
        
        ma_date = dates[-self.ma_days]
        
        df_ma = DBUtils.query_df(f"""
            SELECT ts_code, close as ma_price
            FROM stock_daily 
            WHERE trade_date = '{ma_date}'
        """)
        
        if df_ma is None or df_ma.empty:
            return df
        
        df_current = DBUtils.query_df(f"""
            SELECT ts_code, close as current_price
            FROM stock_daily 
            WHERE trade_date = '{trade_date}'
        """)
        
        if df_current is None or df_current.empty:
            return df
        
        df = pd.merge(df, df_ma, on='ts_code', how='left')
        df = pd.merge(df, df_current, on='ts_code', how='left')
        
        df['above_ma20'] = df['current_price'] >= df['ma_price']
        df = df[df['above_ma20'] == True]
        
        return df
    
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