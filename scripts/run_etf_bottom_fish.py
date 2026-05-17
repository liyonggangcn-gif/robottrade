"""
ETF 板块轮动抄底反弹策略 - 命令行入口

用法：
    python scripts/run_etf_bottom_fish.py
    python scripts/run_etf_bottom_fish.py --top 8
    python scripts/run_etf_bottom_fish.py --top 6 --notify      # 推送钉钉
    python scripts/run_etf_bottom_fish.py --top 6 --days 45     # 用45日历史
"""

import sys
import os
import argparse
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.utils.log_utils import init_logger
logger = init_logger("etf_bottom_fish")

from src.strategy.etf_bottom_fish_strategy import ETFBottomFishStrategy, format_dingtalk_message


def parse_args():
    parser = argparse.ArgumentParser(description="ETF 抄底反弹策略")
    parser.add_argument("--top",    type=int,   default=8,    help="选出 Top N 只 ETF")
    parser.add_argument("--days",   type=int,   default=60,   help="历史 K 线天数")
    parser.add_argument("--sleep",  type=float, default=0.3,  help="每只 ETF 拉取间隔(秒)")
    parser.add_argument("--notify", action="store_true",      help="结果推送钉钉")
    parser.add_argument("--out",    type=str,   default=None, help="输出 CSV 路径")
    return parser.parse_args()


def main():
    args = parse_args()

    strategy = ETFBottomFishStrategy()
    result = strategy.run(top_n=args.top, hist_days=args.days, sleep_sec=args.sleep)

    if result is None or result.empty:
        print("[WARN] 未获取到抄底反弹 ETF 结果")
        return

    # 保存 CSV
    out_path = args.out or os.path.join(
        os.path.dirname(__file__), "..", "output",
        f"etf_bottom_fish_{datetime.date.today().strftime('%Y%m%d')}.csv"
    )
    result.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n[OK] 结果已保存: {out_path}")

    # 钉钉推送
    if args.notify:
        try:
            from src.utils.notifier import Notifier
            from src.utils.config_loader import Config

            msg = format_dingtalk_message(result)
            webhook = Config.get("notification.dingtalk.webhook")
            notifier = Notifier(webhook_url=webhook)
            notifier.send_message(
                title=f"📉 ETF抄底反弹雷达 {datetime.date.today().strftime('%m月%d日')}（提醒）",
                content=msg,
                msg_type="markdown",
            )
            print("[OK] 钉钉推送成功")
        except Exception as e:
            print(f"[WARN] 钉钉推送失败: {e}")


if __name__ == "__main__":
    main()
