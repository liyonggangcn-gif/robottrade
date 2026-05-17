"""
SelectionReviewer — 选股完成后，复核候选池，识别遗漏/异常
"""
from src.llm.base import BaseLLMNode


class SelectionReviewer(BaseLLMNode):
    """
    时机: HybridStrategy.run() 完成后
    输入: 选股结果 (result_df) + 研究信号
    输出: 调整后 picks + 理由 + 遗漏检查
    """
    NODE_NAME = "SelectionReviewer"

    def build_prompt(self, context: dict) -> str:
        picks = context.get('picks', [])
        research_signals = context.get('research_signals', {})
        prev_picks = context.get('prev_picks', [])

        if picks:
            picks_lines = []
            for i, p in enumerate(picks[:15], 1):
                name = p.get('name', '')
                code = p.get('ts_code', '')[:6]
                track = p.get('track', '')
                ai = p.get('ai_score', 0)
                evt = p.get('event_score', 0)
                fund = p.get('fundamental_score', 0)
                final = p.get('final_score', 0)
                concepts = str(p.get('concepts', ''))[:50]
                picks_lines.append(
                    f"{i}. {name}({code}) [{track}] "
                    f"总分={final:.3f} AI={ai:.2f} Evt={evt:.2f} Fund={fund:.2f} {concepts}"
                )
            picks_text = '\n'.join(picks_lines)
        else:
            picks_text = '暂无选股结果'

        prev_text = ', '.join(str(c)[:6] for c in prev_picks[:10]) if prev_picks else '无'

        signal_text = '\n'.join(
            f"- {k}: {str(v)[:150]}" for k, v in research_signals.items() if v
        ) if research_signals else '暂无'

        out = f"""今日选股结果 (前15只):

{picks_text}

上期推荐: {prev_text}

市场研究信号:
{signal_text}

请作为宽客猎手，复核以上选股结果:
1. 检查是否有明显遗漏（某板块强势但未入选）
2. 检查是否有异常入选（评分虚高或概念不匹配）
3. 对每只股票给出简要评价（符合逻辑/存疑/建议剔除）
4. 是否需要替换任何股票？请给出具体替换建议

用简洁的中文回答，重点关注可量化的异常。"""
        return out
