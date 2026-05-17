"""
简化小市值策略 - 使用DuckDB数据
- 市值最小 + ROE > 0
- 避免高PE
"""

import pandas as pd
import numpy as np
from loguru import logger
from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils


class SimpleSmallCapStrategy(BaseStrategy):
    """简化小市值策略"""
    
    name = "small_cap_simple"
    display_name = "小市值"
    version = "1.0"
    
    def __init__(self):
        self.max_mv_yi = 100  # 最大市值100亿
        self.min_roe = 5     # 最小ROE 5%
        self.max_pe = 50     # 最大PE 50
        
    def run(self, trade_date: str = None, top_k: int = 10) -> pd.DataFrame:
        if trade_date is None:
            trade_date = self._resolve_trade_date()
            
        logger.info(f"[小市值] 开始选股 {trade_date}")
        
        # 获取数据
        df = self._get_data(trade_date)
        if df.empty:
            return self._empty_result()
        
        # 过滤: 市值、ROE、PE、北交所
        df['mv_yi'] = df['total_mv'] / 10000
        df = df[~df['ts_code'].str.endswith('.BJ')]  # 剔除北交所
        df = df[df['mv_yi'] <= self.max_mv_yi]
        df = df[df['roe'].notna() & (df['roe'] > self.min_roe)]
        
        if 'pe_ttm' in df.columns:
            df = df[(df['pe_ttm'] > 0) & (df['pe_ttm'] < self.max_pe)]
        
        if df.empty:
            return self._empty_result()
        
        # 按市值升序 + ROE降序
        df = df.sort_values(['total_mv', 'roe'], ascending=[True, False])
        
        # 评分
        df['score'] = 1 - self._rank_norm(df['total_mv'], ascending=False) * 0.7 + \
                      self._rank_norm(df['roe'], ascending=True) * 0.3
        
        df['rank'] = range(1, len(df) + 1)
        df['strategy'] = self.name
        df['trade_date'] = trade_date
        df['signal_reason'] = df.apply(
            lambda x: f"市值{x['mv_yi']:.1f}亿 ROE={x['roe']:.1f}%", axis=1
        )
        
        result = df.head(top_k)[['ts_code', 'name', 'score', 'rank', 'strategy', 
                                   'signal_reason', 'trade_date']]
        
        logger.info(f"[小市值] 选出 {len(result)} 只")
        return result
    
    def _get_data(self, trade_date: str) -> pd.DataFrame:
        df = DBUtils.query_df(f"""
            SELECT ts_code, pe_ttm, roe, total_mv
            FROM stock_daily 
            WHERE trade_date = '{trade_date}' 
              AND total_mv IS NOT NULL AND total_mv > 0
        """)
        
        if df is None or df.empty:
            return pd.DataFrame()
        
        df_info = DBUtils.query_df("SELECT ts_code, name FROM stock_info")
        if df_info is not None:
            df = pd.merge(df, df_info, on='ts_code', how='left')
        
        return df