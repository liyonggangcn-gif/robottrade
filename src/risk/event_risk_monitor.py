#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重大事件风险监控模块

功能：
1. 接收重大事件描述（如：美国打击伊朗、中美贸易摩擦升级等）
2. 调用 LLM 分析事件对 A 股市场的冲击程度
3. 返回风险等级 + 操作建议（清仓/减仓/持仓）
4. 可与仓位管理器联动，触发应急操作
"""

import json
from datetime import datetime
from loguru import logger
from src.utils.llm_client import LLMClient


# 风险等级定义
RISK_LEVELS = {
    "极高": {
        "action": "full_liquidate",
        "action_name": "立即清仓",
        "reduce_pct": 1.0,
        "emoji": "🚨",
        "color": "red",
    },
    "高": {
        "action": "reduce_major",
        "action_name": "大幅减仓50%",
        "reduce_pct": 0.5,
        "emoji": "🔴",
        "color": "orange",
    },
    "中": {
        "action": "reduce_minor",
        "action_name": "小幅减仓20%",
        "reduce_pct": 0.2,
        "emoji": "🟡",
        "color": "yellow",
    },
    "低": {
        "action": "hold",
        "action_name": "继续持仓，密切关注",
        "reduce_pct": 0.0,
        "emoji": "🟢",
        "color": "green",
    },
}


# LLM 系统提示词
SYSTEM_PROMPT = """你是一位顶级的宏观经济和金融市场分析师，专注于全球重大事件对中国A股市场的影响分析。

分析框架：
1. **直接冲击评估**：事件对全球风险情绪、大宗商品、汇率的即时影响
2. **A股传导路径**：通过外资流出/流入、相关行业板块、避险情绪等传导至A股
3. **历史对标**：类似事件（如海湾战争、2018中美贸易战、2020新冠疫情）下A股表现
4. **政策对冲可能**：中国央行、监管层可能的应对措施

风险等级标准（严格遵守）：
- **极高**：事件可能导致A股短期下跌超过5%，系统性风险高（如重大战争、全球金融危机）
- **高**：事件可能导致A股短期下跌3-5%，部分板块承压（如重要制裁、供应链断裂）
- **中**：事件可能导致A股短期波动1-3%，结构性影响（如区域冲突、贸易摩擦加剧）
- **低**：事件对A股影响有限（<1%），或存在较明显的利好对冲

你必须以JSON格式返回分析结果，不要输出任何JSON之外的内容。"""


USER_PROMPT_TEMPLATE = """请分析以下重大事件对中国A股市场的影响：

【事件描述】
{event}

【当前时间】
{datetime}

请返回以下格式的JSON（不要输出任何JSON之外的文字）：
{{
  "risk_level": "极高/高/中/低",
  "confidence": 0.85,
  "summary": "事件简述（50字以内）",
  "impact_analysis": "详细影响分析（200字以内，包含传导路径和历史对标）",
  "affected_sectors": [
    {{"sector": "板块名称", "impact": "利空/利好/中性", "reason": "原因（30字以内）"}},
    ...
  ],
  "recommendation": "操作建议（100字以内）",
  "action": "full_liquidate/reduce_major/reduce_minor/hold",
  "key_risks": ["风险点1", "风险点2"],
  "monitoring_indicators": ["需关注的指标1", "指标2"]
}}"""


class EventRiskMonitor:
    """重大事件风险监控器"""

    def __init__(self):
        self.llm = LLMClient()
        if not self.llm.is_available():
            logger.warning("[EventRiskMonitor] LLM 客户端不可用，风险分析功能受限")

    def analyze_event(self, event_description: str) -> dict:
        """
        分析重大事件对 A 股的影响

        Args:
            event_description: 事件描述（自然语言）

        Returns:
            分析结果字典，包含 risk_level、action、impact_analysis 等
        """
        logger.info(f"[EventRiskMonitor] 开始分析事件: {event_description[:80]}...")

        if not self.llm.is_available():
            return self._fallback_result(event_description)

        user_prompt = USER_PROMPT_TEMPLATE.format(
            event=event_description,
            datetime=datetime.now().strftime("%Y年%m月%d日 %H:%M"),
        )

        try:
            raw = self.llm._call_llm(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=0.3,   # 低温度保证稳定性
                max_tokens=1500,
            )
            if not raw:
                return self._fallback_result(event_description)

            # 提取 JSON（模型有时会在 JSON 外包裹说明文字）
            result = self._parse_json(raw)
            result["event"] = event_description
            result["analyzed_at"] = datetime.now().isoformat()

            # 补充风险等级元数据
            level = result.get("risk_level", "低")
            if level not in RISK_LEVELS:
                level = "低"
            result["risk_level"] = level
            result.update(RISK_LEVELS[level])

            logger.info(
                f"[EventRiskMonitor] 分析完成: 风险={level} | 行动={result.get('action_name')}"
            )
            return result

        except Exception as e:
            logger.error(f"[EventRiskMonitor] LLM 分析失败: {e}")
            return self._fallback_result(event_description)

    # ------------------------------------------------------------------ helpers

    def _parse_json(self, text: str) -> dict:
        """从 LLM 输出中提取 JSON"""
        text = text.strip()
        # 找到第一个 { 和最后一个 }
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        raise ValueError(f"无法从输出中提取 JSON: {text[:200]}")

    def _fallback_result(self, event_description: str) -> dict:
        """LLM 不可用时的降级结果"""
        return {
            "event": event_description,
            "analyzed_at": datetime.now().isoformat(),
            "risk_level": "高",
            "confidence": 0.0,
            "summary": "LLM 分析不可用，默认高风险处理",
            "impact_analysis": "LLM 服务不可用，无法自动分析。建议人工评估事件影响，谨慎操作。",
            "affected_sectors": [],
            "recommendation": "LLM 不可用，建议人工判断。默认升至高风险等级，请谨慎决策。",
            "action": "reduce_major",
            "action_name": "大幅减仓50%",
            "reduce_pct": 0.5,
            "emoji": "🔴",
            "key_risks": ["LLM 分析不可用，风险等级为人工默认值"],
            "monitoring_indicators": [],
        }

    def format_alert_message(self, result: dict) -> tuple[str, str]:
        """
        将分析结果格式化为钉钉推送消息

        Returns:
            (title, content) 元组
        """
        level = result.get("risk_level", "低")
        emoji = result.get("emoji", "⚠️")
        action_name = result.get("action_name", "待定")
        confidence = result.get("confidence", 0)
        summary = result.get("summary", "")
        impact = result.get("impact_analysis", "")
        recommendation = result.get("recommendation", "")
        sectors = result.get("affected_sectors", [])
        key_risks = result.get("key_risks", [])
        indicators = result.get("monitoring_indicators", [])
        analyzed_at = result.get("analyzed_at", "")[:16].replace("T", " ")

        title = f"{emoji}【重大事件风险预警 提醒】风险等级：{level} | {action_name}"

        # 板块影响表格
        sector_lines = ""
        if sectors:
            sector_lines = "\n**受影响板块：**\n"
            for s in sectors[:6]:
                impact_icon = "📉" if s.get("impact") == "利空" else ("📈" if s.get("impact") == "利好" else "➡️")
                sector_lines += f"• {impact_icon} {s.get('sector', '')} — {s.get('reason', '')}\n"

        # 关键风险
        risk_lines = ""
        if key_risks:
            risk_lines = "\n**关键风险点：**\n" + "".join(f"• ⚠️ {r}\n" for r in key_risks[:4])

        # 监测指标
        indicator_lines = ""
        if indicators:
            indicator_lines = "\n**需重点关注：**\n" + "".join(f"• 📊 {i}\n" for i in indicators[:4])

        content = f"""### {emoji} 重大事件风险预警（提醒）

---

**事件：** {result.get('event', '')}

**风险等级：** {emoji} **{level}**（置信度 {confidence*100:.0f}%）

**建议操作：** 🎯 **{action_name}**

---

**影响分析：**
{impact}

**操作建议：**
{recommendation}
{sector_lines}{risk_lines}{indicator_lines}
---
*分析时间：{analyzed_at}*"""

        return title, content
