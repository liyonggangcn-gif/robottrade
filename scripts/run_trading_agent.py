#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易Agent入口脚本
用法:
  python scripts/run_trading_agent.py --phase decision
  python scripts/run_trading_agent.py --phase monitor
  python scripts/run_trading_agent.py --phase review
  python scripts/run_trading_agent.py --phase all
  python scripts/run_trading_agent.py --phase reset  # 重置模拟账户
  python scripts/run_trading_agent.py --phase status # 查看账户状态
"""
import sys
import os
import argparse
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.agent.trading_agent import TradingAgent


def main():
    parser = argparse.ArgumentParser(description='交易Agent')
    parser.add_argument(
        '--phase',
        choices=['decision', 'monitor', 'review', 'all', 'reset', 'status'],
        default='decision',
        help='运行阶段'
    )
    parser.add_argument(
        '--date',
        default='',
        help='指定日期 YYYY-MM-DD，默认今天'
    )
    args = parser.parse_args()

    trade_date = args.date or datetime.now().strftime('%Y-%m-%d')
    print(f"\n{'='*60}")
    print(f"  交易Agent  |  {trade_date}  |  阶段: {args.phase}")
    print(f"{'='*60}\n")

    agent = TradingAgent()

    if args.phase == 'reset':
        # 重置模拟账户
        if hasattr(agent.broker, 'reset'):
            agent.broker.reset()
            print("[Agent] 模拟账户已重置")
        else:
            print("[Agent] 当前 broker 不支持 reset 操作（仅 SimBroker 支持）")

    elif args.phase == 'status':
        # 查看账户和持仓状态
        try:
            acc = agent.broker.get_account()
            pos = agent.broker.get_positions()
            print(f"账户资产: {acc.total_assets:,.0f}  "
                  f"现金: {acc.cash:,.0f}  "
                  f"市值: {acc.market_value:,.0f}  "
                  f"盈亏: {acc.profit_pct:+.2f}%")
            print(f"\n当前持仓 {len(pos)} 只:")
            for p in pos:
                print(f"  {p.ts_code:12s} {p.name:10s}  "
                      f"{p.volume:6d}股  "
                      f"成本 {p.cost:8.2f}  "
                      f"现价 {p.current_price:8.2f}  "
                      f"{p.profit_pct:+6.1f}%  "
                      f"市值 {p.market_value:>12,.0f}")
            if not pos:
                print("  （无持仓）")
        except Exception as e:
            print(f"[Agent] 获取状态失败: {e}")

    elif args.phase == 'decision':
        agent.run_decision(trade_date)

    elif args.phase == 'monitor':
        agent.run_monitor()

    elif args.phase == 'review':
        agent.run_review(trade_date)

    elif args.phase == 'all':
        agent.run_all(trade_date)


if __name__ == '__main__':
    main()
