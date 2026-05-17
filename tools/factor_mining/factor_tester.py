#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
因子有效性测试工具

功能：
1. 单因子IC测试
2. 分组回测
3. 因子相关性分析
4. 因子正交化
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from scipy import stats
from typing import Dict, List, Tuple
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from src.utils.db_utils import DBUtils


class FactorTester:
    """因子测试器"""
    
    def __init__(self, start_date: str = '2023-01-01', end_date: str = '2026-01-01'):
        self.start_date = start_date
        self.end_date = end_date
        self.db = DBUtils
    
    def load_data(self):
        """加载股票数据"""
        sql = f"""
        SELECT 
            ts_code,
            trade_date,
            close,
            open,
            high,
            low,
            vol,
            amount,
            pe_ttm,
            pb,
            total_mv,
            roe
        FROM stock_daily
        WHERE trade_date >= '{self.start_date}'
          AND trade_date <= '{self.end_date}'
        ORDER BY trade_date, ts_code
        """
        
        df = self.db.query_df(sql)
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df = df.set_index(['trade_date', 'ts_code'])
        
        # 计算收益率
        df['return_1d'] = df.groupby('ts_code')['close'].pct_change()
        
        return df
    
    def calculate_ic(self, factor: pd.Series, returns: pd.Series, method='spearman') -> pd.Series:
        """
        计算IC（Information Coefficient）
        
        Args:
            factor: 因子值序列（MultiIndex: date, stock）
            returns: 收益率序列（MultiIndex: date, stock）
            method: 相关系数方法（pearson/spearman）
        
        Returns:
            IC时间序列
        """
        ic_values = []
        dates = []
        
        for date in factor.index.get_level_values('trade_date').unique():
            try:
                factor_day = factor.loc[date]
                return_day = returns.loc[date]
                
                # 对齐数据
                common_stocks = factor_day.index.intersection(return_day.index)
                if len(common_stocks) < 10:
                    continue
                
                f = factor_day.loc[common_stocks]
                r = return_day.loc[common_stocks]
                
                # 去除NaN
                valid = ~(f.isna() | r.isna())
                if valid.sum() < 10:
                    continue
                
                f = f[valid]
                r = r[valid]
                
                # 计算相关系数
                if method == 'spearman':
                    ic, _ = stats.spearmanr(f, r)
                else:
                    ic, _ = stats.pearsonr(f, r)
                
                ic_values.append(ic)
                dates.append(date)
            except Exception as e:
                continue
        
        return pd.Series(ic_values, index=dates)
    
    def test_factor(self, factor: pd.Series, returns: pd.Series, factor_name: str = 'factor') -> Dict:
        """
        单因子全面测试
        
        Returns:
            测试结果字典
        """
        print(f"\n{'='*60}")
        print(f"因子测试: {factor_name}")
        print(f"{'='*60}")
        
        results = {}
        
        # 1. IC分析
        ic_series = self.calculate_ic(factor, returns)
        
        if len(ic_series) == 0:
            print("[ERROR] 无法计算IC，请检查数据")
            return results
        
        results['ic_mean'] = ic_series.mean()
        results['ic_std'] = ic_series.std()
        results['ir'] = ic_series.mean() / ic_series.std() if ic_series.std() > 0 else 0
        results['ic_positive_rate'] = (ic_series > 0).sum() / len(ic_series)
        
        print(f"\n【IC分析】")
        print(f"  IC均值: {results['ic_mean']:.4f}")
        print(f"  IC标准差: {results['ic_std']:.4f}")
        print(f"  IR（信息比率）: {results['ir']:.4f}")
        print(f"  IC胜率: {results['ic_positive_rate']:.2%}")
        
        # 2. 分组测试
        n_groups = 10
        group_returns = self._group_backtest(factor, returns, n_groups)
        
        if group_returns is not None:
            # 多空收益
            top_return = group_returns[n_groups - 1]
            bottom_return = group_returns[0]
            long_short = top_return.mean() - bottom_return.mean()
            
            results['long_short_return'] = long_short
            results['top_group_return'] = top_return.mean()
            results['bottom_group_return'] = bottom_return.mean()
            
            # Sharpe比率
            long_short_series = top_return - bottom_return
            sharpe = long_short_series.mean() / long_short_series.std() * np.sqrt(252) if long_short_series.std() > 0 else 0
            results['long_short_sharpe'] = sharpe
            
            print(f"\n【分组测试】(十分组)")
            print(f"  Top组平均收益: {results['top_group_return']:.4%}")
            print(f"  Bottom组平均收益: {results['bottom_group_return']:.4%}")
            print(f"  多空收益: {results['long_short_return']:.4%}")
            print(f"  多空Sharpe: {results['long_short_sharpe']:.4f}")
            
            # 单调性
            group_mean_returns = {i: group_returns[i].mean() for i in range(n_groups)}
            monotonicity, _ = stats.spearmanr(list(group_mean_returns.keys()), list(group_mean_returns.values()))
            results['monotonicity'] = monotonicity
            print(f"  单调性: {results['monotonicity']:.4f}")
        
        # 3. 评价
        print(f"\n【综合评价】")
        if abs(results['ic_mean']) > 0.05 and results['ic_positive_rate'] > 0.55:
            print("  ✅ 因子有效性：优秀")
        elif abs(results['ic_mean']) > 0.03 and results['ic_positive_rate'] > 0.50:
            print("  ⭐ 因子有效性：良好")
        elif abs(results['ic_mean']) > 0.01:
            print("  ⚠️ 因子有效性：一般")
        else:
            print("  ❌ 因子有效性：较弱")
        
        return results
    
    def _group_backtest(self, factor: pd.Series, returns: pd.Series, n_groups: int = 10) -> Dict[int, pd.Series]:
        """
        分组回测
        
        Returns:
            {group_id: 收益率序列}
        """
        group_returns = {i: [] for i in range(n_groups)}
        dates = []
        
        for date in factor.index.get_level_values('trade_date').unique():
            try:
                factor_day = factor.loc[date]
                return_day = returns.loc[date]
                
                # 对齐
                common_stocks = factor_day.index.intersection(return_day.index)
                if len(common_stocks) < n_groups * 2:
                    continue
                
                f = factor_day.loc[common_stocks]
                r = return_day.loc[common_stocks]
                
                # 去NaN
                valid = ~(f.isna() | r.isna())
                f = f[valid]
                r = r[valid]
                
                if len(f) < n_groups * 2:
                    continue
                
                # 分组
                quantiles = pd.qcut(f, n_groups, labels=False, duplicates='drop')
                
                # 各组收益
                for group in range(n_groups):
                    mask = (quantiles == group)
                    if mask.sum() > 0:
                        group_return = r[mask].mean()
                        group_returns[group].append(group_return)
                
                dates.append(date)
            except Exception as e:
                continue
        
        # 转为Series
        for group in range(n_groups):
            group_returns[group] = pd.Series(group_returns[group], index=dates)
        
        return group_returns
    
    def plot_ic_series(self, ic_series: pd.Series, factor_name: str = 'factor'):
        """绘制IC时间序列"""
        plt.figure(figsize=(12, 6))
        
        # IC曲线
        plt.subplot(2, 1, 1)
        plt.plot(ic_series.index, ic_series.values, alpha=0.7)
        plt.axhline(y=0, color='r', linestyle='--', alpha=0.5)
        plt.axhline(y=ic_series.mean(), color='g', linestyle='--', alpha=0.5, label=f'IC均值: {ic_series.mean():.4f}')
        plt.title(f'{factor_name} - IC时间序列')
        plt.ylabel('IC')
        plt.legend()
        plt.grid(alpha=0.3)
        
        # IC分布
        plt.subplot(2, 1, 2)
        plt.hist(ic_series.values, bins=50, alpha=0.7, edgecolor='black')
        plt.axvline(x=0, color='r', linestyle='--', alpha=0.5)
        plt.axvline(x=ic_series.mean(), color='g', linestyle='--', alpha=0.5)
        plt.title(f'{factor_name} - IC分布')
        plt.xlabel('IC')
        plt.ylabel('频数')
        plt.grid(alpha=0.3)
        
        plt.tight_layout()
        plt.savefig(f'output/{factor_name}_ic_analysis.png', dpi=150)
        print(f"\n✅ IC分析图已保存至: output/{factor_name}_ic_analysis.png")


def demo_test():
    """演示：测试PE因子"""
    print("\n" + "="*60)
    print("因子测试工具 - 演示")
    print("="*60)
    
    # 初始化
    tester = FactorTester(start_date='2024-01-01', end_date='2026-01-01')
    
    # 加载数据
    print("\n正在加载数据...")
    df = tester.load_data()
    print(f"加载完成: {len(df)} 条记录")
    
    # 测试PE倒数因子（PE越低越好）
    factor = 1 / df['pe_ttm'].replace(0, np.nan)
    returns = df.groupby('ts_code')['return_1d'].shift(-1)  # 下一日收益
    
    # 执行测试
    results = tester.test_factor(factor, returns, factor_name='PE_INV')
    
    # 绘图
    if 'ic_mean' in results:
        ic_series = tester.calculate_ic(factor, returns)
        tester.plot_ic_series(ic_series, factor_name='PE_INV')


if __name__ == '__main__':
    # 确保输出目录存在
    os.makedirs('output', exist_ok=True)
    
    demo_test()
