#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场快讯 AI 解读脚本

功能：
  自动抓取多路财经快讯 → LLM 解读 → 板块影响分析 → 可选推送钉钉

用法：
  # 分析最近 4 小时快讯，打印报告
  python scripts/market_news_analysis.py

  # 分析最近 2 小时
  python scripts/market_news_analysis.py --hours 2

  # 分析 + 推送钉钉
  python scripts/market_news_analysis.py --notify

  # 结合手动输入的重大事件一起分析
  python scripts/market_news_analysis.py --event "美国对伊朗发动军事打击" --notify

  # 用于应急判断：如果风险≥高，自动执行减仓/清仓
  python scripts/market_news_analysis.py --notify --auto-action

定时建议（cron）：
  # 交易日 9:00、11:00、14:00、16:00 各跑一次，推送钉钉
  0 9,11,14,16 * * 1-5 cd /home/li/robottrade && venv/bin/python scripts/market_news_analysis.py --notify
"""

import sys
import os
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.utils.log_utils import init_logger
logger = init_logger("market_news")

from src.risk.market_news_analyzer import MarketNewsAnalyzer, ACTION_META
from src.utils.notifier import NotifierFactory
from src.utils.config_loader import Config


def parse_args():
    p = argparse.ArgumentParser(description="财经快讯 AI 解读")
    p.add_argument("--hours", type=float, default=4,
                   help="抓取最近 N 小时新闻（默认 4）")
    p.add_argument("--event", type=str, default="",
                   help="额外注入的重大事件描述（自然语言）")
    p.add_argument("--notify", action="store_true",
                   help="将分析结果推送到钉钉")
    p.add_argument("--auto-action", action="store_true",
                   help="风险≥高时自动触发 emergency_liquidate.py")
    p.add_argument("--max-news", type=int, default=60,
                   help="送给 LLM 的最大新闻条数（默认 60）")
    return p.parse_args()


def print_result(result: dict):
    risk = result.get("risk_level", "低")
    sentiment = result.get("market_sentiment", "中性")
    action = result.get("action", "hold")
    action_emoji, action_name = ACTION_META.get(action, ("ℹ️", action))
    risk_emoji = {"极高": "🚨", "高": "🔴", "中": "🟡", "低": "🟢"}.get(risk, "⚠️")

    print("\n" + "=" * 65)
    print(f"  {risk_emoji} 市场快讯 AI 解读报告")
    print("=" * 65)
    print(f"  风险等级  : {risk_emoji} {risk}  (置信度 {result.get('confidence', 0)*100:.0f}%)")
    print(f"  市场情绪  : {sentiment}")
    print(f"  操作建议  : {action_emoji} {action_name}")
    print(f"  核心判断  : {result.get('summary', '')}")
    print(f"  新闻覆盖  : {result.get('news_count', 0)} 条 | 最近 {result.get('hours', 4)}h")
    print(f"  数据来源  : {', '.join(result.get('sources', []))}")
    print("-" * 65)

    key_events = result.get("key_events", [])
    if key_events:
        print("  重大事件:")
        for ev in key_events[:5]:
            d = {"利好": "📈", "利空": "📉", "中性": "➡️"}.get(ev.get("direction", ""), "•")
            print(f"    {d} {ev.get('event', '')} — {ev.get('impact', '')}")

    sectors = result.get("sector_impacts", [])
    if sectors:
        print("\n  板块影响:")
        for s in sectors[:8]:
            d = {"利好": "📈", "利空": "📉", "中性": "➡️"}.get(s.get("direction", ""), "•")
            strength = s.get("strength", "")
            examples = "、".join(s.get("example_stocks", [])[:2])
            eg = f"（{examples}）" if examples else ""
            print(f"    {d} [{strength}] {s.get('sector', '')}{eg}: {s.get('reason', '')}")

    print(f"\n  操作建议: {result.get('recommendation', '')}")

    watch = result.get("watch_list", [])
    if watch:
        print("\n  重点跟踪:")
        for w in watch:
            print(f"    📊 {w}")

    print("=" * 65)

    # 最新快讯摘要
    top_news = result.get("top_news", [])
    if top_news:
        print("\n  最新快讯（前10条）:")
        for n in top_news:
            time_str = f"[{n.get('time', '')}]" if n.get("time") else ""
            print(f"    [{n.get('source', '')}]{time_str} {n.get('title', '')[:70]}")
    print()


def main():
    args = parse_args()

    analyzer = MarketNewsAnalyzer()

    if args.event:
        logger.info(f"注入事件: {args.event}")
        result = analyzer.analyze_with_event(event=args.event, hours=args.hours)
    else:
        result = analyzer.analyze(hours=args.hours, max_news=args.max_news)

    print_result(result)

    # 推送钉钉
    if args.notify:
        try:
            ding_cfg = Config.get('notification.dingtalk') or {}
            webhook_url = ding_cfg.get('webhook', '')
            secret_word = ding_cfg.get('secret_word', '提醒')
            if not webhook_url:
                print("[WARN] 钉钉推送未配置 webhook")
            else:
                notifier = NotifierFactory.create_notifier(
                    'dingtalk', webhook_url=webhook_url, secret_word=secret_word
                )
                title, content = analyzer.format_report(result)
                ok = notifier.send_message(title, content)
                if ok:
                    print("[INFO] 钉钉通知已发送")
                else:
                    print("[WARN] 钉钉通知发送失败")
        except Exception as e:
            print(f"[WARN] 推送失败: {e}")

    # 自动联动应急清仓
    if args.auto_action:
        risk = result.get("risk_level", "低")
        action = result.get("action", "hold")
        if risk in ("极高", "高") and action in ("full_liquidate", "reduce_major", "reduce_minor"):
            event_desc = args.event or result.get("summary", "市场快讯分析触发风控")
            notify_flag = "--notify" if args.notify else ""
            cmd = (
                f"python scripts/emergency_liquidate.py "
                f"--event \"{event_desc}\" {notify_flag} --auto"
            )
            print(f"\n⚠️  风险={risk}，自动联动应急清仓:")
            print(f"  {cmd}\n")
            os.system(cmd)


if __name__ == "__main__":
    main()
