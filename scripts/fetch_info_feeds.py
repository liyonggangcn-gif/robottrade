"""
拉取股票相关信息源（马斯克/特斯拉、部委政策等）并输出今日摘要与建议热点

用法:
    python scripts/fetch_info_feeds.py              # 拉取全部，输出摘要 + 建议热点
    python scripts/fetch_info_feeds.py --today      # 仅今日
    python scripts/fetch_info_feeds.py --no-filter  # 不做关键词过滤
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.feeds import InfoFeedAggregator, TopicMapper


def main():
    import argparse
    p = argparse.ArgumentParser(description='拉取信息源并输出摘要与建议热点')
    p.add_argument('--today', action='store_true', help='仅保留今日条目')
    p.add_argument('--no-filter', action='store_true', help='不对 feeds 做关键词过滤')
    p.add_argument('--max', type=int, default=30, help='最多显示条数')
    args = p.parse_args()

    print("=" * 60)
    print("  股票相关信息源聚合（马斯克/部委政策等）")
    print("=" * 60)

    aggregator = InfoFeedAggregator()
    items = aggregator.fetch_all(only_today=args.today, keyword_filter=not args.no_filter)
    items = items[: args.max]

    # 建议热点
    mapper = TopicMapper()
    titles = [x['title'] for x in items]
    suggested = mapper.suggest_topics(titles)
    print(f"\n建议热点（来自标题关键词）: {', '.join(suggested) if suggested else '无'}\n")

    # 按来源分组展示
    by_source = {}
    for x in items:
        s = x['source']
        if s not in by_source:
            by_source[s] = []
        by_source[s].append(x)

    for source in sorted(by_source.keys()):
        entries = by_source[source]
        print(f"\n### {source} ({len(entries)} 条)")
        print("-" * 50)
        for e in entries:
            pub = e.get('published')
            pub_str = pub.strftime('%m-%d %H:%M') if pub else ''
            print(f"  [{pub_str}] {e['title'][:70]}")
            if e.get('link'):
                print(f"    {e['link'][:80]}")
        print()

    print("=" * 60)
    print(f"  共 {len(items)} 条 | 建议在 config/settings.yaml 的 hot_topics 中参考: {suggested[:8]}")
    print("=" * 60)


if __name__ == '__main__':
    main()
