"""
行业择机：按渗透率阶段 + 经济周期输出行业超配/标配/低配建议

用法:
    python scripts/run_industry_timing.py           # 默认回溯60日
    python scripts/run_industry_timing.py --days 20
    python scripts/run_industry_timing.py --top 15  # 只显示前15行
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 初始化日志
from src.utils.log_utils import init_logger
logger = init_logger("run_industry_timing")

from src.analysis.industry_timing import IndustryTiming


def main():
    import argparse
    p = argparse.ArgumentParser(description='行业择机（渗透率+周期）')
    p.add_argument('--days', type=int, default=None, help='回溯天数，默认用 config industry_timing.lookback_days')
    p.add_argument('--top', type=int, default=25, help='展示前 N 个行业（按相对强度排序）')
    p.add_argument('--max-industries', type=int, default=50, help='最多计算 N 个行业（减少请求量）')
    p.add_argument('--out', type=str, default='', help='保存 CSV 路径，如 output/industry_timing.csv')
    args = p.parse_args()

    print("=" * 64)
    print("  行业择机（渗透率 + 经济周期）")
    print("=" * 64)

    timing = IndustryTiming()
    if args.days is not None:
        timing.lookback_days = args.days

    df = timing.run(max_industries=args.max_industries)
    if df.empty:
        print("未获取到行业数据，请检查网络或 akshare 接口。")
        return

    display = df.head(args.top)
    print(f"\n回溯 {timing.lookback_days} 日 | 基准: 沪深300 | 共 {len(df)} 个行业\n")
    print(display.to_string(index=False))

    over = df[df['suggest'] == '超配']
    under = df[df['suggest'] == '低配']
    print(f"\n超配 ({len(over)}): {', '.join(over['industry'].tolist()[:12])}{'...' if len(over) > 12 else ''}")
    print(f"低配 ({len(under)}): {', '.join(under['industry'].tolist()[:12])}{'...' if len(under) > 12 else ''}")

    if args.out:
        out_path = os.path.join(os.path.dirname(__file__), '..', args.out) if not os.path.isabs(args.out) else args.out
        os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
        df.to_csv(out_path, index=False, encoding='utf-8-sig')
        print(f"\n已保存: {out_path}")

    print("=" * 64)


if __name__ == '__main__':
    main()
