"""
数据修复模块

提供 A 股数据的标准化修复能力：
1. 停牌股票价格前向填充
2. 财务数据缺失值填充（行业均值 / 滚动均值 / 前值）
3. PE/PB 极值截断（winsorize）
4. 新股/次新股标记
5. 因子 NaN 填充（行业中位数）
6. 北向资金缺失日填充
7. 异常值检测（IQR / Z-Score）
"""

import numpy as np
import pandas as pd
from loguru import logger


class DataRepair:
    """A 股数据修复器"""

    PE_MAX = 500
    PB_MAX = 100
    ROE_MIN = -50
    ROE_MAX = 100
    NEW_STOCK_DAYS = 90

    @staticmethod
    def fill_suspended_prices(df: pd.DataFrame, price_cols=None) -> pd.DataFrame:
        """停牌股票价格前向填充（ffill）

        停牌期间成交量为 0，价格保持前一日收盘价。
        """
        if price_cols is None:
            price_cols = ['close', 'open', 'high', 'low']
        df = df.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
        for col in price_cols:
            if col in df.columns:
                df[col] = df.groupby('ts_code')[col].ffill()
        return df

    @staticmethod
    def fill_financial_na(
        df: pd.DataFrame,
        col: str,
        method: str = 'industry_median',
        industry_col: str = 'industry'
    ) -> pd.DataFrame:
        """财务数据缺失值填充

        Args:
            df: 数据表
            col: 需要填充的列名（如 roe / pe_ttm / pb）
            method: 填充策略
                - industry_median: 按行业中位数填充
                - rolling_mean: 按个股滚动均值填充
                - ffill: 前值填充
                - zero: 填充为 0
            industry_col: 行业列名
        """
        df = df.copy()
        nan_mask = df[col].isna()

        if nan_mask.sum() == 0:
            return df

        if method == 'industry_median':
            if industry_col in df.columns:
                medians = df.groupby(industry_col)[col].transform('median')
                df[col] = df[col].fillna(medians)
            nan_mask = df[col].isna()
            if nan_mask.sum() > 0:
                df[col] = df[col].fillna(df[col].median())

        elif method == 'rolling_mean':
            if 'ts_code' in df.columns:
                rolling = df.groupby('ts_code')[col].transform(
                    lambda x: x.ffill().rolling(20, min_periods=1).mean()
                )
                df[col] = df[col].fillna(rolling)
            else:
                df[col] = df[col].fillna(df[col].rolling(20, min_periods=1).mean())

        elif method == 'ffill':
            if 'ts_code' in df.columns:
                df[col] = df.groupby('ts_code')[col].ffill()
            else:
                df[col] = df[col].ffill()

        elif method == 'zero':
            df[col] = df[col].fillna(0)

        logger.debug(f"[DataRepair] {col}: 填充 {nan_mask.sum()} 个缺失值，方法={method}")
        return df

    @staticmethod
    def winsorize(
        df: pd.DataFrame,
        cols: list = None,
        lower: float = 0.01,
        upper: float = 0.99
    ) -> pd.DataFrame:
        """百分位截断（去极值）

        将超出 [lower, upper] 分位数的值裁剪到边界值。
        """
        if cols is None:
            cols = ['pe_ttm', 'pb', 'roe', 'total_mv']
        df = df.copy()
        for col in cols:
            if col not in df.columns:
                continue
            lo = df[col].quantile(lower)
            hi = df[col].quantile(upper)
            n_clipped = ((df[col] < lo) | (df[col] > hi)).sum()
            df[col] = df[col].clip(lo, hi)
            if n_clipped > 0:
                logger.debug(f"[DataRepair] {col}: 截断 {n_clipped} 个极值 [{lo:.2f}, {hi:.2f}]")
        return df

    @staticmethod
    def mark_new_stocks(df: pd.DataFrame, list_date_col: str = 'list_date') -> pd.DataFrame:
        """标记新股/次新股（上市不满 N 天）

        添加 days_listed 和 is_new_stock 列。
        """
        df = df.copy()
        today = pd.Timestamp.now()
        if list_date_col in df.columns:
            try:
                df[list_date_col] = pd.to_datetime(df[list_date_col], format='%Y%m%d', errors='coerce')
                df['days_listed'] = (today - df[list_date_col]).dt.days
            except Exception:
                df['days_listed'] = None
        else:
            df['days_listed'] = None
        df['is_new_stock'] = df['days_listed'].apply(
            lambda x: False if pd.isna(x) else x < DataRepair.NEW_STOCK_DAYS
        )
        return df

    @staticmethod
    def fill_factor_na(
        factor_df: pd.DataFrame,
        stock_info_df: pd.DataFrame = None
    ) -> pd.DataFrame:
        """因子表 NaN 填充

        策略：技术因子保留 NaN（后续策略层用 fillna(0) 处理），
        基本面因子用行业中位数填充。
        """
        factor_df = factor_df.copy()
        fundamental_cols = ['roe_factor', 'quality_score', 'pe_inv', 'pb_inv', 'growth_score']
        if stock_info_df is not None and 'industry' in stock_info_df.columns:
            stock_info = stock_info_df[['ts_code', 'industry']].drop_duplicates('ts_code')
            factor_df = factor_df.merge(stock_info, on='ts_code', how='left')
            for col in fundamental_cols:
                if col in factor_df.columns:
                    factor_df = DataRepair.fill_financial_na(
                        factor_df, col, method='industry_median', industry_col='industry'
                    )
        for col in fundamental_cols:
            if col in factor_df.columns:
                factor_df[col] = factor_df[col].fillna(factor_df[col].median())
        return factor_df

    @staticmethod
    def fill_northbound(df: pd.DataFrame) -> pd.DataFrame:
        """北向资金缺失日填充

        方法：前向填充净流入，缺失日标记。
        """
        df = df.copy().sort_values('trade_date').reset_index(drop=True)
        if 'north_net_inflow' in df.columns:
            df['north_net_inflow'] = df['north_net_inflow'].ffill().fillna(0)
            df['nb_filled'] = df['north_net_inflow'].isna()
            n_filled = df['nb_filled'].sum()
            if n_filled > 0:
                logger.debug(f"[DataRepair] northbound_flow: 填充 {n_filled} 个缺失日")
        return df

    @staticmethod
    def detect_outliers_iqr(
        df: pd.DataFrame,
        col: str,
        k: float = 1.5
    ) -> pd.DataFrame:
        """IQR 方法检测异常值"""
        Q1 = df[col].quantile(0.25)
        Q3 = df[col].quantile(0.75)
        IQR = Q3 - Q1
        lower = Q1 - k * IQR
        upper = Q3 + k * IQR
        return df[(df[col] < lower) | (df[col] > upper)].copy()

    @staticmethod
    def detect_outliers_zscore(
        df: pd.DataFrame,
        col: str,
        threshold: float = 3.0
    ) -> pd.DataFrame:
        """Z-Score 方法检测异常值"""
        mean = df[col].mean()
        std = df[col].std()
        if std == 0:
            return df.iloc[:0].copy()
        z_scores = np.abs((df[col] - mean) / std)
        return df[z_scores > threshold].copy()

    @staticmethod
    def clean_financial_extremes(df: pd.DataFrame) -> pd.DataFrame:
        """清理财务数据极端值

        - PE < 0 或 > PE_MAX → NULL
        - PB < 0 或 > PB_MAX → NULL
        - ROE < ROE_MIN 或 > ROE_MAX → NULL
        """
        df = df.copy()
        if 'pe_ttm' in df.columns:
            df.loc[(df['pe_ttm'] < 0) | (df['pe_ttm'] > DataRepair.PE_MAX), 'pe_ttm'] = np.nan
        if 'pb' in df.columns:
            df.loc[(df['pb'] < 0) | (df['pb'] > DataRepair.PB_MAX), 'pb'] = np.nan
        if 'roe' in df.columns:
            df.loc[(df['roe'] < DataRepair.ROE_MIN) | (df['roe'] > DataRepair.ROE_MAX), 'roe'] = np.nan
        return df

    @staticmethod
    def repair_stock_daily(df: pd.DataFrame) -> pd.DataFrame:
        """日线数据完整修复流水线

        1. 清理极端值
        2. 填充停牌价格
        3. 填充财务数据（行业均值）
        4. 标记新股
        """
        df = df.copy()
        df = DataRepair.clean_financial_extremes(df)
        df = DataRepair.fill_suspended_prices(df)
        for col in ['roe', 'pe_ttm', 'pb']:
            if col in df.columns:
                df = DataRepair.fill_financial_na(df, col, method='industry_median')
        df = DataRepair.mark_new_stocks(df)
        return df
