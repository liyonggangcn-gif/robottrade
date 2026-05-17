"""
ResearchReasoner — 研究完成后，综合6路信号诊断市场
"""
from src.llm.base import BaseLLMNode


class ResearchReasoner(BaseLLMNode):
    """
    时机: ResearchRunner.run_all() 完成后
    输入: 6路信号（期货/政策新闻/北向/龙虎榜/热点/行业）
    输出: 市场诊断 + 重点板块 + 风险提示 + 仓位建议
    """
    NODE_NAME = "ResearchReasoner"

    def build_prompt(self, context: dict) -> str:
        signals = context.get('signals', {})
        news_analysis = context.get('news_analysis', {})
        industry_timing = context.get('industry_timing', {})

        signal_lines = []
        for name, data in signals.items():
            if isinstance(data, dict):
                summary = data.get('summary', '') or data.get('signal', '') or str(data)
            elif isinstance(data, list):
                summary = '; '.join(str(x)[:100] for x in data[:3])
            else:
                summary = str(data)
            signal_lines.append(f"- {name}: {summary}")

        out = f"""今日市场研究摘要:

{signals.get('news_sector_signals', '暂无')}
{signals.get('futures_sector_signals', '暂无')}
{signals.get('market_sentiment', '暂无')}
{signals.get('institutional_signals', '暂无')}
{signals.get('hot_topics_log', '暂无')}
{signals.get('sector_timing_log', '暂无')}

新闻舆情摘要: {news_analysis.get('summary', '暂无')}
风险等级: {news_analysis.get('risk_level', '未知')}
市场情绪: {news_analysis.get('market_sentiment', '未知')}

行业择机:
周期: {industry_timing.get('current_cycle', '未知')}
新兴行业: {industry_timing.get('emerging', '暂无')}
成熟行业: {industry_timing.get('mature', '暂无')}

请作为宽客猎手，综合以上信息:
1. 判断当前市场特征（趋势/震荡/轮动/防御）
2. 识别最值得关注的3个板块及理由
3. 识别最大风险点
4. 给出仓位建议（0%=清仓 ~ 150%=加杠杆）
5. 给出选股策略重点提示

用简洁的中文回答，重点突出可操作建议。"""
        return out
