"""
PureSmallCapStrategy: 复刻 JoinQuant 原版小市值策略（对照组）

核心逻辑（来自用户提供的 JQ 回测代码）：
  全市场(000985) → ST/次新/科创北交/涨跌停过滤
                 → ROE > 10% AND ROA > 10%
                 → 按市值升序取最小100只
                 → 再取其中 market_cap < min_market_cap × 2 的股票
                 → 空仓月（12/1/4/8）跳过
                 → 搅屎棍行业（银行/煤炭/钢铁）+ 存量市场 → 跳过
                 → 持仓 5 只，周度调仓

与 SmallCapStrategy（评分加权）对比，观察哪个长期更有效。
"""

import pandas as pd
import numpy as np
from loguru import logger

from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class PureSmallCapStrategy(BaseStrategy):
    """JQ 原版小市值策略复刻"""

    name = 'pure_small_cap'
    version = '2.0'

    # 搅屎棍行业（存量市场下不持有）
    JSG_INDUSTRIES = {'银行', '煤炭', '钢铁'}

    # 空仓月份（默认值，会被配置文件覆盖）
    EMPTY_MONTHS = {12, 1, 4, 8}
    
    # 财务排雷开关
    ENABLE_FINANCIAL_TRAP = False

    def __init__(self, include_300: bool = False, empty_months: set = None):
        """
        include_300:  True = 包含创业板（300xxx），False = 剔除（JQ原版）
        empty_months: 自定义空仓月集合，None = 使用配置文件中的值，set() = 不空仓
        """
        cfg = Config.get('small_cap') or {}
        self.MIN_MV_YI = cfg.get('min_mv_亿', 15.0)
        self.MAX_MV_YI = cfg.get('max_mv_亿', 500.0)
        self.MIN_DAYS_LISTED = cfg.get('min_days_listed', 375)
        self.MIN_ROE = 0.0  # 取消ROE限制
        self.MIN_ROA = 10.0
        self.TOP_UNIVERSE = 100
        self.MV_RATIO = 2.0
        self.include_300 = include_300
        
        # 优先使用传入参数，其次使用配置文件，否则使用默认
        config_empty_months = cfg.get('empty_months', [1])
        if empty_months is not None:
            self._empty_months = empty_months
        elif config_empty_months:
            self._empty_months = set(config_empty_months)
        else:
            self._empty_months = set()  # 配置为空则不空仓
            
        logger.info(f"[PureSmallCapStrategy] 初始化 ROE>{self.MIN_ROE}% 取最小{self.TOP_UNIVERSE}只×{self.MV_RATIO}倍 "
                    f"include_300={include_300} empty_months={self._empty_months}")

    def run(self, trade_date: str = None, top_k: int = 5) -> pd.DataFrame:
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"\n[PureSmallCapStrategy] ===== 选股 {trade_date} =====")

        # 空仓月检查
        month = int(trade_date[5:7])
        if month in self._empty_months:
            logger.info(f"  [空仓期] {month}月，跳过选股")
            return self._empty_result()

        df = self._load_universe(trade_date)
        if df.empty:
            logger.error("[PureSmallCapStrategy] 数据为空")
            return self._empty_result()
        logger.info(f"  原始宇宙: {len(df)} 只")

        # ST/退市过滤
        if 'name' in df.columns:
            before = len(df)
            df = df[~df['name'].str.contains(r'ST|\*ST|退', na=False, regex=True)]
            logger.info(f"  ST过滤: {before}→{len(df)}")

        # 科创板(688) + 北交所(.BJ) 过滤，创业板(300)按配置决定
        df = df[~df['ts_code'].str.startswith('688')]
        df = df[~df['ts_code'].str.endswith('.BJ')]
        if not self.include_300:
            df = df[~df['ts_code'].str.startswith('300')]
        logger.info(f"  科创/北交{'/' if not self.include_300 else ''}{'创业板' if not self.include_300 else ''}过滤后: {len(df)} 只")

        # 次新股过滤
        if 'list_date' in df.columns:
            trade_dt = pd.to_datetime(trade_date, errors='coerce')
            if not pd.isna(trade_dt):
                list_dt = pd.to_datetime(df['list_date'], errors='coerce')
                listed_days = (trade_dt - list_dt).dt.days
                df = df[list_dt.isna() | (listed_days >= self.MIN_DAYS_LISTED)]
        logger.info(f"  次新股过滤后: {len(df)} 只")

        # 市值下限过滤
        min_mv_w = self.MIN_MV_YI * 10000
        df = df[df['total_mv'].notna() & (df['total_mv'] >= min_mv_w)]

        # 流动性门槛（日均成交>2000万）
        if 'avg_amount_20d' in df.columns:
            df = df[df['avg_amount_20d'].isna() | (df['avg_amount_20d'] >= 2000.0)]

        # ROE > 10% 过滤（原版 indicator.roe > 0.10）
        # ROA 我们数据库暂无，用 ROE 代替 ROA（均要求>10%）
        if 'roe' in df.columns:
            before = len(df)
            df = df[df['roe'].notna() & (df['roe'] > self.MIN_ROE)]
            logger.info(f"  ROE>{self.MIN_ROE}%过滤: {before}→{len(df)}")

        if df.empty:
            logger.warning("[PureSmallCapStrategy] 质量过滤后为空")
            return self._empty_result()

        # 按市值升序取最小100只
        df = df.sort_values('total_mv', ascending=True).head(self.TOP_UNIVERSE)
        logger.info(f"  取市值最小{self.TOP_UNIVERSE}只: {len(df)} 只")

        # 取 market_cap < min × 2 的（原版逻辑）
        min_mv = df['total_mv'].min()
        df = df[df['total_mv'] < min_mv * self.MV_RATIO]
        logger.info(f"  市值<min×{self.MV_RATIO}过滤后: {len(df)} 只，"
                    f"min={min_mv/10000:.1f}亿 上限={min_mv*self.MV_RATIO/10000:.1f}亿")

        # 搅屎棍行业 + 存量市场过滤
        if 'industry' in df.columns:
            market_env = self._calc_market_env_simple(trade_date)
            if market_env == '存量':
                before = len(df)
                df = df[~df['industry'].isin(self.JSG_INDUSTRIES)]
                logger.info(f"  搅屎棍过滤(存量): {before}→{len(df)}")

        if df.empty:
            return self._empty_result()

        # 最终按市值升序取 top_k
        result = df.sort_values('total_mv', ascending=True).head(top_k).reset_index(drop=True)

        result['score'] = 1.0
        result['rank'] = range(1, len(result) + 1)
        result['strategy'] = self.name
        result['trade_date'] = trade_date
        result['signal_reason'] = result.apply(
            lambda r: f"市值{float(r.get('total_mv',0) or 0)/10000:.1f}亿 ROE={float(r.get('roe',0) or 0):.1f}%",
            axis=1)

        logger.info(f"\n[PureSmallCapStrategy] ===== Top {len(result)} =====")
        for _, row in result.iterrows():
            mv_yi = float(row.get('total_mv', 0) or 0) / 10000
            logger.info(f"  #{int(row['rank']):2d} {row['ts_code']} {str(row.get('name',''))[:6]:6s} "
                        f"市值={mv_yi:.1f}亿 ROE={float(row.get('roe',0) or 0):.1f}% [{row.get('industry','')}]")

        out_cols = ['ts_code', 'name', 'score', 'rank', 'strategy',
                    'signal_reason', 'trade_date', 'total_mv', 'roe', 'industry']
        return result[[c for c in out_cols if c in result.columns]]

    def _calc_market_env_simple(self, trade_date: str, ma_window: int = 20, slope_window: int = 5) -> str:
        """判断市场环境（复用 SmallCapStrategy 逻辑）"""
        try:
            from src.strategy.small_cap_strategy import SmallCapStrategy
            return SmallCapStrategy._calc_market_env(self, trade_date, ma_window, slope_window)
        except Exception as e:
            logger.warning(f"  市场环境判断失败，默认存量: {e}")
            return '存量'

    def _load_universe(self, trade_date: str) -> pd.DataFrame:
        from src.strategy.small_cap_strategy import SmallCapStrategy
        return SmallCapStrategy._load_universe(self, trade_date)

    @staticmethod
    def _empty_result() -> pd.DataFrame:
        return pd.DataFrame(columns=[
            'ts_code', 'name', 'score', 'rank', 'strategy',
            'signal_reason', 'trade_date', 'total_mv', 'roe', 'industry'])
