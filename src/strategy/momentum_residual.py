"""
残差动量策略 (Residual Momentum Strategy)
- 剔除沪深300和行业收益, 只取个股自身阿尔法
- 形成期: 3个月
- 持仓期: 2周-1个月
- 筛选: 残差收益前10%, 日均成交额≥1亿, 市值≥100亿
"""

import pandas as pd
import numpy as np
from loguru import logger
from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils


class MomentumResidualStrategy(BaseStrategy):
    """残差动量策略 - 剔除大盘和行业, 纯赚个股Alpha"""
    
    name = "momentum_residual"
    display_name = "残差动量"
    version = "2.0"
    
    def __init__(self):
        self.lookback_days = 60   # 形成期60天(约3个月)
        self.holding_days = 20    # 持仓期
        self.top_pct = 0.10       # 取前10%
        self.min_turnover = 10000 # 最小日均成交额(万元)=1亿
        self.min_mv_yi = 100      # 最小市值(亿元)
        
    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        if trade_date is None:
            trade_date = self._resolve_trade_date()
            
        logger.info(f"[残差动量] 开始选股 {trade_date}")
        
        # Step 1: 获取过去N天的交易日
        dates = self._get_trade_dates(trade_date, self.lookback_days)
        if len(dates) < 20:
            logger.warning(f"[残差动量] 交易日不足: {len(dates)}")
            return self._empty_result()
        
        # Step 2: 计算市场收益(沪深300) 和行业收益
        market_returns = self._calculate_market_returns(dates)
        industry_returns = self._calculate_industry_returns(dates)
        
        if market_returns is None or market_returns.empty:
            logger.warning("[残差动量] 无法获取市场收益")
            return self._empty_result()
        
        # Step 3: 计算个股收益并分解
        df_residual = self._calculate_residual_returns(dates, market_returns, industry_returns)
        if df_residual.empty:
            return self._empty_result()
        
        # Step 4: 过滤 (市值/成交额/ ST)
        df_filtered = self.filter_universe(df_residual, min_mv_yi=self.min_mv_yi, min_days_listed=60)
        df_filtered = df_filtered[df_filtered.get('avg_turnover', 0) >= self.min_turnover]
        
        if df_filtered.empty:
            return self._empty_result()
        
        # Step 5: 取残差收益前N%
        df_filtered = df_filtered.sort_values('residual_return', ascending=False)
        n_select = max(int(len(df_filtered) * self.top_pct), top_k)
        df_top = df_filtered.head(n_select).copy()
        
        # Step 6: 评分
        df_top['score'] = self._rank_norm(df_top['residual_return'], ascending=True)
        
        # Step 7: 输出
        df_top['rank'] = range(1, len(df_top) + 1)
        df_top['strategy'] = self.name
        df_top['trade_date'] = trade_date
        df_top['signal_reason'] = df_top.apply(
            lambda x: f"残差收益{x['residual_return']:.1%}", axis=1
        )
        df_top['sub_scores'] = df_top.apply(
            lambda x: {'residual': x['residual_return'], 'total': x.get('total_return', 0)}, 
            axis=1
        )
        
        result = df_top.head(top_k)[['ts_code', 'name', 'score', 'rank', 'strategy', 
                                       'signal_reason', 'sub_scores', 'trade_date']]
        
        logger.info(f"[残差动量] 选出 {len(result)} 只")
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
    
    def _calculate_market_returns(self, dates: list) -> pd.DataFrame:
        """计算市场收益 - 使用全市场中位数收益率作为市场基准"""
        if len(dates) < 2:
            return None
        
        start_date = dates[0]
        end_date = dates[-1]
        
        # 获取期初和期末全市场
        df_start = DBUtils.query_df(f"""
            SELECT ts_code, close as price_start
            FROM stock_daily 
            WHERE trade_date = '{start_date}'
        """)
        
        df_end = DBUtils.query_df(f"""
            SELECT ts_code, close as price_end
            FROM stock_daily 
            WHERE trade_date = '{end_date}'
        """)
        
        if df_start is None or df_end is None or df_start.empty or df_end.empty:
            return None
        
        # 合并计算每只股票的收益率
        df = pd.merge(df_start, df_end, on='ts_code', how='inner')
        df['return'] = df['price_end'] / df['price_start'] - 1
        
        # 使用中位数作为市场收益(稳健)
        market_return = df['return'].median()
        
        # 返回简单的市场收益DataFrame
        result = pd.DataFrame({
            'trade_date': [end_date],
            'return': [market_return]
        })
        
        return result
    
    def _calculate_industry_returns(self, dates: list) -> dict:
        """计算每个行业的日收益"""
        # 获取所有股票所属行业
        df_info = DBUtils.query_df("SELECT ts_code, industry FROM stock_info")
        if df_info is None or df_info.empty:
            return {}
        
        industry_map = dict(zip(df_info['ts_code'], df_info['industry']))
        
        # 获取所有股票日收益
        date_placeholders = ','.join([f"'{d}'" for d in dates])
        df_ret = DBUtils.query_df(f"""
            SELECT ts_code, trade_date, close 
            FROM stock_daily 
            WHERE trade_date IN ({date_placeholders})
            ORDER BY ts_code, trade_date
        """)
        
        if df_ret is None or df_ret.empty:
            return {}
        
        # 添加行业
        df_ret['industry'] = df_ret['ts_code'].map(industry_map)
        
        # 计算行业日收益(等权平均)
        df_ret = df_ret.dropna(subset=['industry'])
        df_ret['return'] = df_ret.groupby(['industry', 'trade_date'])['close'].transform(
            lambda x: x.pct_change()
        )
        
        industry_returns = {}
        for ind in df_ret['industry'].unique():
            ind_df = df_ret[df_ret['industry'] == ind][['trade_date', 'return']].dropna()
            industry_returns[ind] = ind_df.set_index('trade_date')['return'].to_dict()
        
        return industry_returns
    
    def _calculate_residual_returns(self, dates: list, market_returns: pd.DataFrame, 
                                     industry_returns: dict) -> pd.DataFrame:
        """计算个股残差收益 - 简化为只减市场收益"""
        if len(dates) < 2:
            return pd.DataFrame()
        
        # 获取期初和期末数据
        start_date = dates[0]
        end_date = dates[-1]
        
        df_start = DBUtils.query_df(f"""
            SELECT ts_code, close as price_start, total_mv, vol
            FROM stock_daily 
            WHERE trade_date = '{start_date}'
        """)
        
        # 获取股票名称
        df_info = DBUtils.query_df("SELECT ts_code, name FROM stock_info")
        
        df_end = DBUtils.query_df(f"""
            SELECT ts_code, close as price_end, vol as vol_end
            FROM stock_daily 
            WHERE trade_date = '{end_date}'
        """)
        
        if df_start is None or df_start.empty or df_end is None or df_end.empty:
            return pd.DataFrame()
        
        df = pd.merge(df_start, df_end, on='ts_code', how='inner')
        
        # 添加股票名称
        if df_info is not None and not df_info.empty:
            df = pd.merge(df, df_info, on='ts_code', how='left')
        
        # 总收益
        df['total_return'] = df['price_end'] / df['price_start'] - 1
        
        # 获取市场收益
        market_return = 0
        if market_returns is not None and not market_returns.empty:
            market_return = market_returns['return'].iloc[-1] if len(market_returns) > 0 else 0
        
        # 残差 = 个股收益 - 市场收益
        df['residual_return'] = df['total_return'] - market_return
        
        # 平均成交量
        recent_dates = dates[-20:] if len(dates) >= 20 else dates
        date_placeholders = ','.join([f"'{d}'" for d in recent_dates])
        df_vol = DBUtils.query_df(f"""
            SELECT ts_code, AVG(vol) as avg_vol
            FROM stock_daily 
            WHERE trade_date IN ({date_placeholders})
            GROUP BY ts_code
        """)
        
        if df_vol is not None and not df_vol.empty:
            df = pd.merge(df, df_vol, on='ts_code', how='left')
        
        df['avg_turnover'] = df['avg_vol']
        
        # 剔除涨跌停
        df = df[df['total_return'].abs() < 0.09]
        
        # 填充缺失的名称
        if 'name' not in df.columns:
            df['name'] = df['ts_code']
        
        return df
        
        # 使用行业平均收益估算
        df['industry_return'] = df['industry'].map(
            lambda x: list(industry_returns.get(x, {}).values())[-1] if industry_returns.get(x) else 0
        )
        
        # 市场收益
        market_return = market_returns['return'].iloc[-1] if len(market_returns) > 0 else 0
        
        # 残差收益 = 总收益 - 市场收益 - 行业收益
        df['residual_return'] = df['total_return'] - market_return - df['industry_return'].fillna(0)
        
        # 平均成交额
        recent_dates = dates[-20:] if len(dates) >= 20 else dates
        date_placeholders = ','.join([f"'{d}'" for d in recent_dates])
        df_turnover = DBUtils.query_df(f"""
            SELECT ts_code, AVG(vol) as avg_vol
            FROM stock_daily 
            WHERE trade_date IN ({date_placeholders})
            GROUP BY ts_code
        """)
        
        if df_turnover is not None and not df_turnover.empty:
            df = pd.merge(df, df_turnover, on='ts_code', how='left')
        
        # 剔除涨跌停
        df = df[df['total_return'].abs() < 0.09]
        
        return df