#!/usr/bin/env python3
"""
动量策略优化版 - 使用ClickHouse + 减少换手
"""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from loguru import logger
from src.strategy.base import BaseStrategy

try:
    import clickhouse_connect
    _CH_AVAILABLE = True
except:
    _CH_AVAILABLE = False
    print("ClickHouse not available")

class MomentumOptimizedStrategy(BaseStrategy):
    """优化的动量策略 - 使用ClickHouse + 减少换手"""
    
    name = "momentum_optimized"
    display_name = "优化动量"
    version = "1.0"
    
    def __init__(self):
        self.lookback_days = 60       # 60天动量（更长更稳定）
        self.top_pct = 0.10           # 前10%
        self.min_mv_yi = 30           # 30亿市值
        self.min_days_listed = 90      # 上市90天（更稳定）
        self._ch_client = None
        self._use_ch = False
        
        # 初始化ClickHouse
        if _CH_AVAILABLE:
            try:
                self._ch_client = clickhouse_connect.get_client(
                    host='192.168.3.51', port=8123,
                    username='default', password='clickhouse123'
                )
                self._use_ch = True
                print("[MomentumOptimized] 使用ClickHouse")
            except Exception as e:
                print(f"[MomentumOptimized] ClickHouse连接失败: {e}")
    
    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        if trade_date is None:
            from datetime import datetime
            trade_date = datetime.now().strftime('%Y-%m-%d')
        
        # 获取交易日期
        dates = self._get_trade_dates(trade_date, self.lookback_days)
        if len(dates) < self.lookback_days * 0.8:
            return pd.DataFrame()
        
        start_date = dates[0]
        end_date = dates[-1]
        
        # 从ClickHouse获取数据
        if self._use_ch:
            try:
                df = self._from_clickhouse(start_date, end_date)
            except Exception as e:
                print(f"ClickHouse错误: {e}")
                df = self._from_mysql(start_date, end_date)
        else:
            df = self._from_mysql(start_date, end_date)
        
        if df.empty:
            return pd.DataFrame()
        
        # 基础过滤
        df = self.filter_universe(df, min_mv_yi=self.min_mv_yi, 
                                  min_days_listed=self.min_days_listed)
        
        # PE过滤（只过滤负PE）
        if 'pe_ttm' in df.columns:
            df = df[df['pe_ttm'] > 0]
        
        # 排序取Top
        df = df.sort_values('momentum_return', ascending=False)
        n_select = max(int(len(df) * self.top_pct), top_k)
        df_top = df.head(n_select).copy()
        
        # 评分
        df_top['score'] = self._rank_norm(df_top['momentum_return'], ascending=True)
        df_top['rank'] = range(1, len(df_top) + 1)
        df_top['strategy'] = self.name
        df_top['trade_date'] = trade_date
        df_top['signal_reason'] = df_top.apply(
            lambda x: f"{self.lookback_days}日涨{x['momentum_return']:.1%}", axis=1
        )
        
        result = df_top[['ts_code', 'name', 'score', 'rank', 'strategy', 
                        'signal_reason', 'trade_date']].head(top_k)
        
        logger.info(f"[优化动量] 选出 {len(result)} 只")
        return result
    
    def _from_clickhouse(self, start_date: str, end_date: str) -> pd.DataFrame:
        """从ClickHouse获取动量数据"""
        q = f"""
        SELECT 
            s.ts_code,
            s1.close as price_start,
            s2.close as price_end,
            s2.total_mv,
            s2.pe_ttm,
            si.name
        FROM stock_daily s
        INNER JOIN stock_daily s1 ON s.ts_code = s1.ts_code AND s1.trade_date = '{start_date}'
        INNER JOIN stock_daily s2 ON s.ts_code = s2.ts_code AND s2.trade_date = '{end_date}'
        LEFT JOIN stock_info si ON s.ts_code = si.ts_code
        WHERE s.trade_date = '{start_date}'
        """
        df = self._ch_client.query(q).result_set()
        if df is None or len(df) == 0:
            return pd.DataFrame()
        
        # 转换为DataFrame
        columns = ['ts_code', 'price_start', 'price_end', 'total_mv', 'pe_ttm', 'name']
        df = pd.DataFrame([dict(zip(columns, row)) for row in df])
        
        df['momentum_return'] = (df['price_end'] / df['price_start'] - 1)
        return df
    
    def _from_mysql(self, start_date: str, end_date: str) -> pd.DataFrame:
        """从MySQL获取数据（备用）"""
        from src.utils.db_utils import DBUtils
        
        df_start = DBUtils.query_df(f"""
            SELECT ts_code, close as price_start, total_mv, pe_ttm
            FROM stock_daily WHERE trade_date = '{start_date}'
        """)
        
        df_end = DBUtils.query_df(f"""
            SELECT ts_code, close as price_end
            FROM stock_daily WHERE trade_date = '{end_date}'
        """)
        
        df_info = DBUtils.query_df("SELECT ts_code, name FROM stock_info")
        
        if df_start is None or df_end is None:
            return pd.DataFrame()
        
        df = df_start.merge(df_end, on='ts_code', how='inner')
        df = df.merge(df_info, on='ts_code', how='left')
        df['momentum_return'] = (df['price_end'] / df['price_start'] - 1)
        
        return df
    
    def _get_trade_dates(self, end_date: str, n: int) -> list:
        if self._use_ch:
            try:
                q = f"""
                SELECT DISTINCT trade_date FROM stock_daily 
                WHERE trade_date <= '{end_date}' 
                ORDER BY trade_date DESC 
                LIMIT {n}
                """
                dates = self._ch_client.query(q).result_set()
                if dates:
                    return [row[0] for row in dates][::-1]
            except:
                pass
        
        # 备用MySQL
        from src.utils.db_utils import DBUtils
        df = DBUtils.query_df(f"""
            SELECT DISTINCT trade_date FROM stock_daily 
            WHERE trade_date <= '{end_date}' 
            ORDER BY trade_date DESC 
            LIMIT {n}
        """)
        if df is None or df.empty:
            return []
        return df['trade_date'].tolist()[::-1]


# 测试
if __name__ == '__main__':
    print("=" * 60)
    print("OPTIMIZED MOMENTUM WITH CLICKHOUSE")
    print("=" * 60)
    
    s = MomentumOptimizedStrategy()
    result = s.run(trade_date='2026-04-10', top_k=30)
    
    if result.empty:
        print("No results!")
    else:
        print(f"\nSelected: {len(result)} stocks")
        
        # 688系列
        stocks_688 = result[result['ts_code'].str.startswith('688')]
        print(f"688 series: {len(stocks_688)}")
        for _, row in stocks_688.head(10).iterrows():
            print(f"  {row['ts_code']} rank:{row['rank']}")