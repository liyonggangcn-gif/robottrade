"""
DailyReporter — 收盘后复盘每日决策
"""
from src.llm.base import BaseLLMNode


class DailyReporter(BaseLLMNode):
    """
    时机: 每日收盘后 (evening_push 或日终定时任务)
    输入: 当日选股结果 + 持仓盈亏 + 历史表现
    输出: 决策复盘 + 改进建议 + 策略评分
    """
    NODE_NAME = "DailyReporter"

    def build_prompt(self, context: dict) -> str:
        picks = context.get('picks', [])
        performance = context.get('performance', {})
        tracker_info = context.get('tracker_info', '')
        ab_results = context.get('ab_results', {})
        factor_attr = context.get('factor_attribution', {})

        picks_text = '暂无'
        if picks:
            lines = []
            for p in picks[:10]:
                name = p.get('name', '')
                code = p.get('ts_code', '')[:6]
                final = p.get('final_score', 0)
                track = p.get('track', '')
                lines.append(f"- {name}({code}) [{track}] 总分={final:.3f}")
            picks_text = '\n'.join(lines)

        perf_summary = f"近期({performance.get('period_days', 30)}天): 总选股{performance.get('total_picks', 0)}只 胜率{performance.get('win_rate_5d', 0):.1%} 均收益{performance.get('avg_ret_5d', 0):.2%}"

        factor_text = '\n'.join(
            f"- {k}: {v:+.2f}%" for k, v in factor_attr.items() if v != 0
        ) if factor_attr else '暂无'

        ab_text = '\n'.join(
            f"- {exp}: winner={data.get('winner', 'TBD')} p={data.get('p_value', 'N/A')}"
            for exp, data in ab_results.items()
        ) if ab_results else '暂无运行中实验'

        out = f"""今日复盘报告:

选股结果:
{picks_text}

近期绩效摘要:
{perf_summary}

持仓追踪:
{tracker_info or '暂无持仓数据'}

因子归因 (近期):
{factor_text}

A/B实验状态:
{ab_text}

请作为宽客猎手，进行收盘复盘:
1. 今日选股质量评价（1-10分）
2. 哪些决策做得好？哪些决策有问题？
3. 各因子贡献度是否合理？是否需要调整权重？
4. A/B实验是否有值得关注的发现？
5. 明日操作重点建议

用简洁的中文回答，输出格式:
复盘评分: X/10
决策评价: ...
因子分析: ...
明日建议: ..."""
        return out
