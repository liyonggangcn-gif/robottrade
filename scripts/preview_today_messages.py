#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
预览今天的推送消息内容
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 强制清除代理
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

from src.utils.log_utils import init_logger
from scripts.morning_push import build_morning_message, get_morning_stock_picks, get_market_overview
from scripts.evening_push import build_evening_message, get_todays_performance, get_market_summary
from scripts.push_futures_etf_signal import format_signal_message
from src.portfolio.position_manager import PositionManager
from src.analysis.futures_etf_signal import FuturesETFSignalGenerator

logger = init_logger("preview_messages")


def preview_morning_message():
    """预览早盘推送消息"""
    print("=" * 80)
    print("早盘推送消息预览")
    print("=" * 80)
    print()
    
    try:
        # 获取选股结果
        result_df = get_morning_stock_picks(top_k=20)
        
        # 初始化仓位管理器
        pm = PositionManager()
        
        # 仓位分配
        position_df = None
        if result_df is not None and len(result_df) > 0:
            from src.utils.config_loader import Config
            method = Config.get('portfolio.position_method') or 'tiered'
            position_df = pm.allocate_positions(
                result_df, 
                score_col='final_score',
                method=method
            )
        
        # 获取市场概况
        market_info = get_market_overview()
        if market_info is None:
            market_info = {'latest_date': '-', 'prev_date': '-', 'total_stocks': 0, 'rise_count': 0, 'fall_count': 0, 'avg_change': 0}
        
        # 构建消息
        title, content = build_morning_message(result_df, market_info, position_df, pm)
        
        print(f"标题: {title}\n")
        print("=" * 80)
        print("消息内容:")
        print("=" * 80)
        print(content)
        print("=" * 80)
        
        return title, content
        
    except Exception as e:
        print(f"[ERROR] 生成早盘消息失败: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def preview_evening_message():
    """预览收盘推送消息"""
    print("\n" + "=" * 80)
    print("收盘推送消息预览")
    print("=" * 80)
    print()
    
    try:
        # 初始化仓位管理器
        pm = PositionManager()
        pm.update_position_prices()
        
        # 获取持仓汇总
        position_summary = pm.get_position_summary()
        
        # 检查止损止盈
        stop_loss_list, take_profit_list = pm.check_stop_loss_take_profit()
        
        # 获取今日推荐表现
        today_perf = get_todays_performance()
        
        # 获取市场概况
        market_summary = get_market_summary()
        if market_summary is None:
            market_summary = {'date': '-', 'total': 0, 'rise': 0, 'fall': 0, 'flat': 0, 'avg_change': 0, 'limit_up': 0, 'limit_down': 0}
        
        # 构建消息
        title, content = build_evening_message(
            today_perf, 
            market_summary, 
            position_summary, 
            stop_loss_list, 
            take_profit_list
        )
        
        print(f"标题: {title}\n")
        print("=" * 80)
        print("消息内容:")
        print("=" * 80)
        print(content)
        print("=" * 80)
        
        return title, content
        
    except Exception as e:
        print(f"[ERROR] 生成收盘消息失败: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def preview_futures_etf_message():
    """预览期货ETF信号消息"""
    print("\n" + "=" * 80)
    print("期货ETF信号推送消息预览")
    print("=" * 80)
    print()
    
    try:
        # 生成信号
        generator = FuturesETFSignalGenerator()
        signals = generator.get_all_sector_signals()
        
        # 格式化消息
        title, content = format_signal_message(signals)
        
        print(f"标题: {title}\n")
        print("=" * 80)
        print("消息内容:")
        print("=" * 80)
        print(content)
        print("=" * 80)
        
        return title, content
        
    except Exception as e:
        print(f"[ERROR] 生成期货ETF消息失败: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def main():
    """主函数"""
    print(f"\n生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # 1. 早盘推送
    morning_title, morning_content = preview_morning_message()
    
    # 2. 收盘推送
    evening_title, evening_content = preview_evening_message()
    
    # 3. 期货ETF信号
    futures_title, futures_content = preview_futures_etf_message()
    
    # 总结
    print("\n" + "=" * 80)
    print("消息生成总结")
    print("=" * 80)
    print(f"早盘推送: {'✅' if morning_title else '❌'}")
    print(f"收盘推送: {'✅' if evening_title else '❌'}")
    print(f"期货ETF信号: {'✅' if futures_title else '❌'}")
    print("=" * 80)


if __name__ == '__main__':
    main()
