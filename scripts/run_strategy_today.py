#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行策略并获取今日推荐股票
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 初始化日志
from src.utils.log_utils import init_logger
logger = init_logger("run_strategy_today")

import pandas as pd
from src.collector.data_loader import DataLoader
from src.strategy.topk_strategy import TopKStrategy
from src.strategy.small_cap_jinx import SmallCapJinxStrategy

if __name__ == '__main__':
    print("🚀 运行策略并获取今日推荐股票")
    print("=" * 50)
    
    # 前天的日期
    today = (pd.Timestamp.now() - pd.Timedelta(days=2)).strftime('%Y-%m-%d')
    print(f"📅 前天日期: {today}")
    
    # 1. 同步最新数据
    print("\n📊 同步最新数据...")
    loader = DataLoader()
    try:
        loader.sync_daily_data(limit=10)
        print("✅ 数据同步完成！")
    finally:
        loader.close()
    
    # 2. 运行 TopK 策略
    print("\n📈 运行 TopK 策略...")
    topk_strategy = TopKStrategy()
    topk_stocks = topk_strategy.get_top_stocks(today, top_k=5)
    
    if topk_stocks is not None and not topk_stocks.empty:
        print("✅ TopK 策略推荐股票:")
        print(topk_stocks)
    else:
        print("❌ TopK 策略未选出股票")
    
    # 3. 运行小市值策略
    print("\n📉 运行小市值策略...")
    small_cap_strategy = SmallCapJinxStrategy()
    small_cap_stocks = small_cap_strategy.get_top_stocks(today, top_k=5)
    
    if not small_cap_stocks.empty:
        print("✅ 小市值策略推荐股票:")
        print(small_cap_stocks)
    else:
        print("❌ 小市值策略未选出股票")
    
    print("\n=" * 50)
    print("🎯 策略运行完成！")
