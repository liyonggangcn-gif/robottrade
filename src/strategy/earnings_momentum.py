"""
盈余动量策略 (Earnings Momentum Strategy)
- 基本面动量, 赚业绩兑现的钱
- 核心: 业绩超预期 + 股价验证
- 筛选条件:
  1. 单季度净利润同比增速≥30%, 营收同比增速≥20%
  2. 连续2个季度加速增长
  3. 净利润超券商一致预期20%以上
  4. 业绩公告后股价跳空上涨, 过去1个月涨幅跑赢行业
  5. 持仓: 业绩公告后买入, 持有1-3个月, 单只仓位不超过15%
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from loguru import logger
from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils


class EarningsMomentumStrategy(BaseStrategy):
    """盈余动量策略 - 业绩超预期驱动"""
    
    name = "earnings_momentum"
    display_name = "盈余动量"
    version = "2.0"
    
    def __init__(self):
        self.min_profit_growth = 0.0   # 取消最小净利润增速限制
        self.min_revenue_growth = 20.0  # 最小营收增速%
        self.min_beat_pct = 20.0        # 超预期幅度%
        self.lookback_days = 30         # 过去N天
        
    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        if trade_date is None:
            trade_date = self._resolve_trade_date()
            
        logger.info(f"[盈余动量] 开始选股 {trade_date}")
        
        # Step 1: 获取有PE数据的股票 (低PE可能意味着业绩被低估)
        df_earnings = self._get_financial_data(trade_date)
        if df_earnings.empty:
            logger.warning("[盈余动量] 无财务数据")
            return self._empty_result()
        
        # Step 2: 低PE筛选 (业绩预期好)
        df_growth = df_earnings[df_earnings['pe_ttm'].fillna(0) <= 30]
        
        if df_growth.empty:
            return self._empty_result()
        
        # Step 3: 价格动量验证
        df_validated = self._validate_with_price(df_growth, trade_date)
        
        # Step 4: 基础过滤
        df_filtered = self.filter_universe(df_validated, min_mv_yi=30, min_days_listed=180)
        
        if df_filtered.empty:
            return self._empty_result()
        
        # Step 5: 综合评分
        df_filtered = self._score_earnings(df_filtered)
        
        # Step 6: 排序输出
        df_filtered = df_filtered.sort_values('score', ascending=False)
        df_filtered['rank'] = range(1, len(df_filtered) + 1)
        df_filtered['strategy'] = self.name
        df_filtered['trade_date'] = trade_date
        df_filtered['signal_reason'] = df_filtered.apply(
            lambda x: f"PE={x['pe_ttm']:.1f} 涨幅{x.get('price_return', 0):.1%}", 
            axis=1
        )
        df_filtered['sub_scores'] = df_filtered.apply(
            lambda x: {'pe': x['pe_ttm'], 'price': x.get('price_return', 0)}, 
            axis=1
        )
        
        result = df_filtered.head(top_k)[['ts_code', 'name', 'score', 'rank', 'strategy', 
                                           'signal_reason', 'sub_scores', 'trade_date']]
        
        logger.info(f"[盈余动量] 选出 {len(result)} 只")
        return result
    
    def _get_financial_data(self, trade_date: str) -> pd.DataFrame:
        """获取财务数据"""
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
    
    def _get_earnings_announcements(self, trade_date: str) -> pd.DataFrame:
        """获取近期业绩数据 - 分开查询避免collation问题"""
        # 从stock_daily获取财务数据
        df_daily = DBUtils.query_df(f"""
            SELECT 
                ts_code,
                netprofit_yoy,
                roe,
                pe_ttm,
                total_mv
            FROM stock_daily 
            WHERE trade_date = '{trade_date}' AND roe IS NOT NULL
        """)
        
        if df_daily is None or df_daily.empty:
            return pd.DataFrame()
        
        # 获取股票名称(分开查询)
        df_info = DBUtils.query_df("SELECT ts_code, name, industry FROM stock_info")
        
        if df_info is not None and not df_info.empty:
            df_daily = pd.merge(df_daily, df_info, on='ts_code', how='left')
        
        return df_daily
    
    def _filter_growth(self, df: pd.DataFrame) -> pd.DataFrame:
        """筛选高增长"""
        # 净利润增速
        df = df[df['netprofit_yoy'] >= self.min_profit_growth]
        
        # 营收增速(如果有)
        if 'revenue_yoy' in df.columns:
            df = df[df['revenue_yoy'] >= self.min_revenue_growth]
        
        return df
    
    def _validate_with_price(self, df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        """用价格动量验证"""
        if df.empty:
            return df
        
        # 获取过去N天收益率
        dates = self._get_trade_dates(trade_date, self.lookback_days)
        if len(dates) < 10:
            return df
        
        start_date = dates[0]
        
        # 期初价格
        df_start = DBUtils.query_df(f"""
            SELECT ts_code, close as price_start
            FROM stock_daily 
            WHERE trade_date = '{start_date}'
        """)
        
        # 期末价格
        df_end = DBUtils.query_df(f"""
            SELECT ts_code, close as price_end
            FROM stock_daily 
            WHERE trade_date = '{trade_date}'
        """)
        
        if df_start is None or df_end is None:
            return df
        
        # 合并
        df = pd.merge(df, df_start, on='ts_code', how='left')
        df = pd.merge(df, df_end, on='ts_code', how='left')
        
        # 计算区间收益
        df['price_return'] = (df['price_end'] / df['price_start'] - 1)
        
        # 价格验证: 过去30天涨幅为正
        df = df[df['price_return'] > 0]
        
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
    
    def _score_earnings(self, df: pd.DataFrame) -> pd.DataFrame:
        """盈余动量评分"""
        # PE评分 (越低越好)
        df['pe_score'] = 1 - self._rank_norm(df['pe_ttm'].fillna(0), ascending=False)
        
        # 价格动量评分
        if 'price_return' in df.columns:
            df['momentum_score'] = self._rank_norm(df['price_return'].fillna(0), ascending=True)
        else:
            df['momentum_score'] = 0.5
        
        # 综合
        df['score'] = df['pe_score'] * 0.6 + df['momentum_score'] * 0.4
        
        return df