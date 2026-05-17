#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成模拟数据，用于测试QuantAgent Alpha系统
"""

import os
import duckdb
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# 确保数据库目录存在
os.makedirs('data', exist_ok=True)

# 连接DuckDB
conn = duckdb.connect('data/quant.db')

# 创建股票列表
stock_codes = [f"{i:06d}.SH" for i in range(1000, 1100)] + [f"{i:06d}.SZ" for i in range(2000, 2100)]
stock_names = [f"股票{i}" for i in range(1, 201)]

# 生成日期范围
dates = pd.date_range(start='2024-01-01', end=datetime.now().strftime('%Y-%m-%d'), freq='B')

# 创建stock_daily表
conn.execute('''
CREATE TABLE IF NOT EXISTS stock_daily (
    trade_date DATE,
    ts_code VARCHAR,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    pre_close DOUBLE,
    change DOUBLE,
    pct_chg DOUBLE,
    vol DOUBLE,
    amount DOUBLE,
    PRIMARY KEY (trade_date, ts_code)
)
''')

# 创建stock_factors表
conn.execute('''
CREATE TABLE IF NOT EXISTS stock_factors (
    trade_date DATE,
    ts_code VARCHAR,
    mom_20 DOUBLE,
    vol_20 DOUBLE,
    rsi_14 DOUBLE,
    PRIMARY KEY (trade_date, ts_code)
)
''')

# 生成模拟数据
print("生成模拟数据...")
daily_data = []
factor_data = []

for date in dates:
    date_str = date.strftime('%Y-%m-%d')
    
    for i, ts_code in enumerate(stock_codes):
        # 生成基础价格
        base_price = 10 + i * 0.1
        
        # 添加随机波动
        daily_change = np.random.normal(0, 0.02)
        
        # 计算价格
        open_price = base_price * (1 + np.random.normal(0, 0.01))
        high_price = open_price * (1 + np.random.normal(0, 0.01))
        low_price = open_price * (1 - np.random.normal(0, 0.01))
        close_price = open_price * (1 + daily_change)
        pre_close = base_price
        
        # 计算其他字段
        change = close_price - pre_close
        pct_chg = change / pre_close * 100
        vol = np.random.uniform(1000000, 10000000)
        amount = close_price * vol
        
        # 添加到日线数据
        daily_data.append({
            'trade_date': date_str,
            'ts_code': ts_code,
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'pre_close': pre_close,
            'change': change,
            'pct_chg': pct_chg,
            'vol': vol,
            'amount': amount
        })
        
        # 生成因子数据（从2024-02-01开始）
        if date >= pd.to_datetime('2024-02-01'):
            mom_20 = np.random.normal(0, 0.1)
            vol_20 = np.random.normal(0.02, 0.01)
            rsi_14 = np.random.uniform(30, 70)
            
            factor_data.append({
                'trade_date': date_str,
                'ts_code': ts_code,
                'mom_20': mom_20,
                'vol_20': vol_20,
                'rsi_14': rsi_14
            })
    
    print(f"处理日期: {date_str}")

# 批量插入数据
print("插入日线数据...")
df_daily = pd.DataFrame(daily_data)
conn.register('df_daily', df_daily)
conn.execute('''
INSERT INTO stock_daily
SELECT * FROM df_daily
ON CONFLICT (trade_date, ts_code) DO NOTHING
''')

print("插入因子数据...")
df_factors = pd.DataFrame(factor_data)
conn.register('df_factors', df_factors)
conn.execute('''
INSERT INTO stock_factors
SELECT * FROM df_factors
ON CONFLICT (trade_date, ts_code) DO NOTHING
''')

print(f"生成完成！日线数据: {len(daily_data)} 条，因子数据: {len(factor_data)} 条")

# 关闭连接
conn.close()
