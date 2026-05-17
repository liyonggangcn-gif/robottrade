"""
个股深度分析 CLI

用法：
  # 分析单只股票
  python scripts/analyze_stock.py 600519.SH

  # 分析多只股票
  python scripts/analyze_stock.py 600519.SH 601857.SH

  # 分析所有股票池中今日有买入信号的股票
  python scripts/analyze_stock.py --pool-signals

  # 分析整个观察池（不限信号）
  python scripts/analyze_stock.py --watch-pool

  # 指定触发原因
  python scripts/analyze_stock.py 600519.SH --trigger earnings
"""

import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.stock_analyzer import StockAnalyzer
from src.strategy.pool_strategy import PoolStrategy
from src.universe.stock_pool import StockPool
from src.utils.notifier import NotifierFactory
from src.utils.config_loader import Config


def push_report(report_text: str, title: str = "个股深度分析"):
    """发送分析报告到钉钉"""
    try:
        notification_config = Config.get("notification", {})
        if not notification_config.get("enabled", False):
            return
        dingtalk_config = notification_config.get("dingtalk", {})
        webhook_url = dingtalk_config.get("webhook")
        secret_word = dingtalk_config.get("secret_word", "提醒")
        if not webhook_url:
            return
        notifier = NotifierFactory.create_notifier(
            "dingtalk", webhook_url=webhook_url, secret_word=secret_word
        )
        notifier.send_message(title, report_text, message_type="analysis")
    except Exception as e:
        print(f"[WARN] 钉钉推送失败: {e}")


def analyze_stocks(ts_codes: list, trigger: str = "manual", push: bool = True):
    """分析一批股票并输出报告"""
    analyzer = StockAnalyzer()
    results = []

    for ts_code in ts_codes:
        print(f"\n{'='*60}")
        result = analyzer.analyze(ts_code, trigger=trigger)
        results.append(result)

        # 控制台输出
        print(f"\n{result['report']}")

        # 钉钉推送
        if push:
            report_md = analyzer.format_report_for_dingtalk(result)
            push_report(report_md, title=f"提醒: 个股分析 {result['company_name']}")

    return results


def analyze_pool_signals(push: bool = True):
    """扫描股票池信号并分析有买入信号的股票"""
    from src.valuation.valuation_engine import ValuationEngine

    print("[analyze_stock] 更新估值数据...")
    ValuationEngine().update_pool()

    print("[analyze_stock] 扫描股票池买入信号...")
    pool_result = PoolStrategy().run(update_valuation=False)
    summary = pool_result.get("summary", "")
    print(f"[analyze_stock] 扫描结果: {summary}")

    signals = pool_result.get("signals")
    if signals is None or signals.empty:
        print("[analyze_stock] 今日无买入信号，跳过深度分析")
        return []

    ts_codes = signals["ts_code"].tolist()
    print(f"[analyze_stock] 对 {len(ts_codes)} 只有信号的股票生成深度分析...")
    return analyze_stocks(ts_codes, trigger="buy_signal", push=push)


def analyze_watch_pool(push: bool = False):
    """分析观察池中所有股票（不限信号）"""
    pool = StockPool()
    df = pool.get_pool(tier="watch")
    if df.empty:
        print("[analyze_stock] 观察池为空")
        return []

    ts_codes = df["ts_code"].tolist()
    print(f"[analyze_stock] 分析观察池 {len(ts_codes)} 只股票...")
    return analyze_stocks(ts_codes, trigger="manual", push=push)


def main():
    parser = argparse.ArgumentParser(description="个股深度分析")
    parser.add_argument("ts_codes", nargs="*", help="股票代码，如 600519.SH")
    parser.add_argument("--pool-signals", action="store_true",
                        help="分析今日有买入信号的池内股票")
    parser.add_argument("--watch-pool", action="store_true",
                        help="分析整个观察池")
    parser.add_argument("--trigger", default="manual",
                        choices=["manual", "earnings", "buy_signal", "news"],
                        help="触发原因（默认 manual）")
    parser.add_argument("--no-push", action="store_true",
                        help="不发送钉钉推送")
    args = parser.parse_args()

    push = not args.no_push

    if args.pool_signals:
        analyze_pool_signals(push=push)
    elif args.watch_pool:
        analyze_watch_pool(push=push)
    elif args.ts_codes:
        # 自动补全 .SH/.SZ 后缀
        codes = []
        for c in args.ts_codes:
            if "." not in c:
                suffix = ".SH" if c.startswith("6") else ".SZ"
                c = c + suffix
            codes.append(c)
        analyze_stocks(codes, trigger=args.trigger, push=push)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
