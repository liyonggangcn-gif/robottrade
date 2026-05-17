#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日自动交易脚本（审计模式）

本脚本仅记录选股信号到 trade_signals 表。
实际交易执行由 TradingAgent (src/agent/trading_agent.py) 负责。
"""

import sys
import os
from datetime import datetime

# 添加项目根目录到路径
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

# 强制清除代理
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

from src.utils.log_utils import init_logger
from src.trading.auto_trader import AutoTrader
from src.strategy.hybrid_strategy import HybridStrategy
from src.utils.config_loader import Config

logger = init_logger("auto_trade_daily")


def main():
    """主函数：记录选股信号"""
    print("=" * 80)
    print("  每日交易信号记录")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()

    try:
        # 初始化记录器
        trader = AutoTrader()

        # 获取股票选股结果
        print("[步骤1] 获取股票选股结果...")
        strategy = HybridStrategy()
        stock_picks = strategy.run(top_k=20)

        if stock_picks is None or stock_picks.empty:
            print("[WARN] 未获取到股票选股结果")
            stock_results = {'buy_count': 0, 'sell_count': 0, 'hold_count': 0, 'buy_list': []}
        else:
            print(f"[OK] 获取到 {len(stock_picks)} 只推荐股票")
            stock_results = trader.auto_trade_stocks(stock_picks)

        # 打印汇总
        print("\n" + "=" * 80)
        print("  信号记录汇总")
        print("=" * 80)
        print(f"\n已记录买入意向: {stock_results['buy_count']} 只")

        if stock_results['buy_list']:
            print("\n推荐列表 (前10):")
            for item in stock_results['buy_list'][:10]:
                print(f"  + {item['name']}({item['ts_code']}) 评分: {item['score']:.4f}")

        today_signals = trader.get_trading_summary()
        if today_signals:
            print(f"\n今日 trade_signals 汇总: {today_signals}")

        print("\n" + "=" * 80)
        print(f"  [OK] 信号记录完成")
        print(f"  结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)
        return True

    except Exception as e:
        logger.error(f"信号记录失败: {e}", exc_info=True)
        print(f"\n[ERROR] 信号记录失败: {e}")
        return False


if __name__ == '__main__':
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n[WARN] 用户中断执行")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n[ERROR] 执行过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
