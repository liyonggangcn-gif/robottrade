"""
网格交易策略 - 震荡市躺赚

核心逻辑：
  - 针对宽基ETF设定固定价格网格
  - 每下跌一定幅度买入一份，每上涨一定幅度卖出一份
  - 无需择时，震荡市反复赚取价差

适合市场：70%时间处于震荡行情的A股

数据源：etf_daily 表
"""

import numpy as np
import pandas as pd
from typing import Optional, List, Dict
from loguru import logger

from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class GridTradingStrategy(BaseStrategy):
    """网格交易策略"""

    name = 'grid_trading'
    version = '1.0'
    display_name = '网格交易策略'

    def __init__(self):
        cfg = Config.get('grid_trading') or {}
        self.GRID_SIZE_PCT = cfg.get('grid_size_pct', 3.0)    # 网格间距 %
        self.MAX_GRIDS = cfg.get('max_grids', 5)              # 最大持仓网格数
        self.MIN_AMOUNT_WAN = cfg.get('min_amount_wan', 5000)  # 最小成交额(万)
        self.ETF_CODES = cfg.get('etf_codes', [
            '510300', '510500', '159919', '159915',  # 沪深300, 中证500, 创业板, 科创50
            '510050', '159920', '588000', '159825'   # 上证50, 深证100, 科创板, 恒生ETF
        ])
        
        logger.info(f"[GridTrading] 初始化 网格间距={self.GRID_SIZE_PCT}% 最大网格={self.MAX_GRIDS}")

    def run(self, trade_date: str = None, top_k: int = 10) -> pd.DataFrame:
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"\n[GridTrading] ===== 选股 {trade_date} =====")

        etf_df = self._load_etf_data(trade_date)
        if etf_df.empty:
            return self._empty_result()

        # 计算网格信号
        grid_signals = []
        for _, row in etf_df.iterrows():
            signal = self._analyze_grid(row, trade_date)
            if signal:
                grid_signals.append(signal)

        if not grid_signals:
            logger.info("[GridTrading] 无网格信号")
            return self._empty_result()

        df = pd.DataFrame(grid_signals)
        df = df.sort_values('grid_score', ascending=False).head(top_k).reset_index(drop=True)
        df['rank'] = range(1, len(df) + 1)
        df['strategy'] = self.name
        df['trade_date'] = trade_date
        df['signal_reason'] = df.apply(self._format_reason, axis=1)

        logger.info(f"[GridTrading] 选出 {len(df)} 只ETF")
        return df

    def _load_etf_data(self, trade_date: str) -> pd.DataFrame:
        """加载ETF数据"""
        try:
            sql = """
            SELECT code, name, price, pct_chg, amount,
                   close_5d, close_20d, close_60d,
                   vol_5d, vol_20d
            FROM etf_daily
            WHERE trade_date = (
                SELECT MAX(trade_date) FROM etf_daily WHERE trade_date <= ?
            )
            AND amount >= ?
            """
            df = DBUtils.query_df(sql, params=(trade_date, self.MIN_AMOUNT_WAN * 10000))
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.warning(f"[GridTrading] 加载失败: {e}")
            return pd.DataFrame()

    def _analyze_grid(self, row: pd.Series, trade_date: str) -> Optional[Dict]:
        """分析网格信号"""
        try:
            code = row['code']
            name = row['name']
            price = row.get('price', 0)
            pct_chg = row.get('pct_chg', 0) or 0
            amount = row.get('amount', 0)
            
            if not price or price <= 0:
                return None

            # 计算近期波动率
            close_5d = row.get('close_5d')
            close_20d = row.get('close_20d')
            
            if close_20d and close_5d and close_20d > 0:
                volatility_20d = abs(close_5d - close_20d) / close_20d
            else:
                volatility_20d = abs(pct_chg) / 100

            # 波动率太低不适合网格（没波动）
            if volatility_20d < 0.02:
                return None

            # 计算网格密度（波动越大，网格越密集越好）
            grid_density = volatility_20d / (self.GRID_SIZE_PCT / 100)
            
            # 计算价格位置（相对于20日区间）
            if close_20d and close_5d:
                price_position = (price - close_20d) / close_20d if close_20d != 0 else 0
            else:
                price_position = pct_chg / 100

            # 网格评分：在中间位置最好（上下都有空间）
            position_score = 1 - abs(price_position)
            
            # 综合评分
            grid_score = (
                grid_density * 0.4 +
                position_score * 0.4 +
                (amount / 100000000) * 0.2  # 成交额加权
            )

            # 确定操作建议
            if price_position < -0.03:
                action = "买入分批建仓"
            elif price_position > 0.03:
                action = "卖出分批止盈"
            else:
                action = "持有观察"

            return {
                'ts_code': code,
                'name': name,
                'price': price,
                'pct_chg': pct_chg,
                'amount': amount,
                'volatility_20d': round(volatility_20d * 100, 2),
                'price_position': round(price_position * 100, 2),
                'grid_score': round(grid_score, 3),
                'action': action,
                'grid_size': self.GRID_SIZE_PCT,
                'max_grids': self.MAX_GRIDS,
            }
        except Exception as e:
            logger.warning(f"[GridTrading] 分析失败: {e}")
            return None

    def _format_reason(self, row: pd.Series) -> str:
        """格式化选股理由"""
        parts = [
            f"20日波动{row.get('volatility_20d', 0):.1f}%",
            f"价格位置{int(row.get('price_position', 0)):+d}%",
            f"网格分数{int(row.get('grid_score', 0)*100)}",
            row.get('action', '')
        ]
        return " | ".join([p for p in parts if p])


if __name__ == '__main__':
    strategy = GridTradingStrategy()
    result = strategy.run(top_k=10)
    if not result.empty:
        print(result[['ts_code', 'name', 'price', 'pct_chg', 'volatility_20d', 'price_position', 'action']])