#!/usr/bin/env python3
"""
ETF策略推送脚本 - 推送多个ETF策略信号

包含：
- 网格交易策略
- ETF折溢价套利策略
- 趋势动量策略
- 双动量策略
- 抄底反弹策略
"""

import sys
sys.path.insert(0, '.')

import pandas as pd
from datetime import datetime
from src.strategy.grid_trading_strategy import GridTradingStrategy
from src.strategy.etf_arbitrage_strategy import ETFArbitrageStrategy
from src.strategy.etf_strategy_suite import ETFMomentumStrategy, ETFDualMomentumStrategy
from src.strategy.etf_bottom_fish_strategy import ETFBottomFishStrategy
from src.utils.notifier import DingTalkNotifier
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils

def get_latest_trade_date():
    """获取最新有数据的交易日"""
    try:
        df = DBUtils.query_df('SELECT MAX(trade_date) as d FROM etf_daily')
        if df is not None and not df.empty:
            return df.iloc[0]['d']
    except:
        pass
    return datetime.now().strftime('%Y-%m-%d')

def format_grid_message(df: pd.DataFrame) -> str:
    """格式化网格交易消息"""
    if df.empty:
        return "今日无网格交易信号"
    
    lines = ["### 网格交易信号", "", "| 代码 | 名称 | 价格 | 20日波动 | 价格位置 | 操作建议 |", "|------|------|------|---------|---------|---------|"]
    for _, row in df.head(8).iterrows():
        lines.append(f"| {row['ts_code']} | {row['name'][:8]} | {row['price']:.3f} | {row.get('volatility_20d', 0):.1f}% | {int(row.get('price_position', 0)):+d}% | {row.get('action', '')} |")
    
    return "\n".join(lines)

def format_etf_message(df: pd.DataFrame, name: str) -> str:
    """格式化ETF消息"""
    if df.empty:
        return f"今日无{name}信号"
    
    lines = [f"### {name}", "", "| 代码 | 名称 | 涨跌幅 | 操作建议 |", "|------|------|---------|---------|"]
    for _, row in df.head(6).iterrows():
        pct = row.get('pct_chg', 0) or 0
        action = row.get('advice', row.get('action', '观察'))
        lines.append(f"| {row['ts_code']} | {row['name'][:8]} | {pct:+.2f}% | {action[:10]} |")
    
    return "\n".join(lines)

def main():
    print(f"=== ETF策略推送 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    
    trade_date = get_latest_trade_date()
    print(f"交易日期: {trade_date}")
    
    # 获取钉钉配置
    webhook = Config.get('notification.dingtalk.webhook')
    secret = Config.get('notification.dingtalk.secret_word', '提醒')
    
    if not webhook:
        print("[WARN] 未配置钉钉 webhook")
        return
    
    notifier = DingTalkNotifier(webhook, secret_word=secret)
    
    all_messages = []
    
    # 1. 网格交易策略
    try:
        print("\n[1] 网格交易策略...")
        grid_strategy = GridTradingStrategy()
        grid_df = grid_strategy.run(trade_date=trade_date, top_k=8)
        if not grid_df.empty:
            msg = format_grid_message(grid_df)
            all_messages.append(msg)
            print(f"  选出 {len(grid_df)} 只")
    except Exception as e:
        print(f"  错误: {e}")
    
    # 2. ETF折溢价套利
    try:
        print("\n[2] ETF折溢价套利...")
        arb_strategy = ETFArbitrageStrategy()
        arb_df = arb_strategy.run(trade_date=trade_date, top_k=6)
        if not arb_df.empty:
            msg = f"### ETF折溢价套利\n\n{arb_df[['ts_code', 'name', 'pct_chg', 'action']].to_markdown(index=False)}"
            all_messages.append(msg)
            print(f"  选出 {len(arb_df)} 只")
    except Exception as e:
        print(f"  错误: {e}")
    
    # 3. 趋势动量策略
    try:
        print("\n[3] 趋势动量策略...")
        momentum = ETFMomentumStrategy()
        mom_df = momentum.run(top_n=6, hist_days=60, sleep_sec=0)
        if mom_df is not None and not mom_df.empty:
            msg = format_etf_message(mom_df, "趋势动量")
            all_messages.append(msg)
            print(f"  选出 {len(mom_df)} 只")
    except Exception as e:
        print(f"  错误: {e}")
    
    # 4. 双动量策略
    try:
        print("\n[4] 双动量策略...")
        dual = ETFDualMomentumStrategy()
        dual_df = dual.run(top_n=5, hist_days=90, sleep_sec=0)
        if dual_df is not None and not dual_df.empty:
            msg = format_etf_message(dual_df, "双动量")
            all_messages.append(msg)
            print(f"  选出 {len(dual_df)} 只")
    except Exception as e:
        print(f"  错误: {e}")
    
    # 5. 抄底反弹策略
    try:
        print("\n[5] 抄底反弹策略...")
        bottom = ETFBottomFishStrategy()
        bottom_df = bottom.run(top_n=6)
        if bottom_df is not None and not bottom_df.empty:
            msg = format_etf_message(bottom_df, "抄底反弹")
            all_messages.append(msg)
            print(f"  选出 {len(bottom_df)} 只")
    except Exception as e:
        print(f"  错误: {e}")
    
    # 发送汇总消息
    if all_messages:
        title = f"📊 ETF多策略信号 {datetime.now().strftime('%m月%d日')}"
        content = "\n\n---\n\n".join(all_messages)
        notifier.send_message(title, content)
        print("\n[OK] 钉钉推送成功")
    else:
        print("\n[INFO] 无任何ETF信号，跳过推送")

if __name__ == '__main__':
    main()