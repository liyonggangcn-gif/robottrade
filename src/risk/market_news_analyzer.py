#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场快讯 LLM 解读器

流程：
  NewsFetcher.fetch() → 整理新闻列表 → LLM prompt → 结构化分析结果
  → format_report() → 钉钉推送
"""

import json
from datetime import datetime
from typing import List, Optional
from loguru import logger

from src.feeds.news_fetcher import NewsFetcher, NewsItem
from src.utils.llm_client import LLMClient


# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是一位服务于A股量化基金的首席宏观策略分析师。
你的任务：快速阅读一批财经快讯，提炼出对中国股市最有影响力的信息，给出专业、精准、可执行的分析结论。

分析原则：
1. 聚焦 A 股：一切分析落脚于对 A 股的影响（外围市场通过风险情绪、大宗商品、汇率传导）
2. 重大事件优先：战争/制裁/金融危机/重大政策 > 行业政策 > 一般财经数据
3. 板块影响必须具体：直接点名受影响的 A 股板块/概念，不泛泛而谈
4. 风险等级诚实：若当前无重大利空/利好，如实给出"中性"或"低风险"
5. JSON 格式输出：不输出 JSON 之外的任何内容

风险等级标准：
- 极高：可能引发 A 股系统性大跌（>5%），需立刻应对
- 高：短期冲击 3~5%，需减仓
- 中：波动 1~3%，结构性机会/风险
- 低：影响有限，正常持仓即可"""

_USER_PROMPT_TEMPLATE = """以下是过去 {hours} 小时的财经快讯（共 {count} 条，已按时间排序）：

{news_text}

---
请以 JSON 格式返回分析结果：
{{
  "risk_level": "极高/高/中/低",
  "market_sentiment": "偏多/中性/偏空",
  "confidence": 0.80,
  "summary": "一句话总结当前市场核心矛盾（50字内）",
  "key_events": [
    {{
      "event": "事件标题（30字内）",
      "impact": "对A股的具体影响（50字内）",
      "direction": "利好/利空/中性"
    }}
  ],
  "sector_impacts": [
    {{
      "sector": "板块/概念名称",
      "direction": "利好/利空/中性",
      "strength": "强/中/弱",
      "reason": "简要原因（40字内）",
      "example_stocks": ["股票/ETF举例（可选）"]
    }}
  ],
  "recommendation": "今日仓位操作建议（80字内）",
  "action": "full_liquidate/reduce_major/reduce_minor/hold/add_position",
  "watch_list": ["需要重点跟踪的指标或事件（3~5条）"],
  "analyzed_at": "{now}"
}}"""


# ─────────────────────────────────────────────────────────────────────────────
# 核心分析器
# ─────────────────────────────────────────────────────────────────────────────

ACTION_META = {
    "full_liquidate": ("🚨", "立即清仓"),
    "reduce_major":   ("🔴", "大幅减仓50%"),
    "reduce_minor":   ("🟡", "小幅减仓20%"),
    "hold":           ("🟢", "继续持仓"),
    "add_position":   ("💹", "逢低加仓"),
}

# 持仓比例建议矩阵
# action → 基准仓位
_ACTION_BASE_RATIO = {
    "full_liquidate": 0,
    "reduce_major":   25,
    "reduce_minor":   50,
    "hold":           70,
    "add_position":   85,
}

# 风险等级 → 仓位乘数 & 上限
_RISK_ADJUST = {
    "极高": (0.50, 20),   # 乘数, 上限
    "高":   (0.75, 45),
    "中":   (1.00, 80),
    "低":   (1.10, 90),
}

# 情绪 → 仓位增减（百分点）
_SENTIMENT_DELTA = {
    "偏多": +8,
    "中性":  0,
    "偏空": -12,
}

SENTIMENT_EMOJI = {
    "偏多": "📈",
    "中性": "➡️",
    "偏空": "📉",
}


class MarketNewsAnalyzer:
    """抓取财经快讯 → LLM 解读 → 结构化分析"""

    def __init__(self):
        self.fetcher = NewsFetcher()
        self.llm = LLMClient()
        if not self.llm.is_available():
            logger.warning("[MarketNewsAnalyzer] LLM 不可用，分析结果将为空")

    # ------------------------------------------------------------------ public

    def analyze(self, hours: float = 4, max_news: int = 60) -> dict:
        """
        抓取最近 N 小时快讯，调用 LLM 分析，返回结构化结果。

        Args:
            hours: 抓取时间窗口（小时）
            max_news: 送给 LLM 的最大条数（超出截断，避免 token 过多）

        Returns:
            分析结果字典
        """
        logger.info(f"[MarketNewsAnalyzer] 开始抓取最近 {hours}h 快讯...")
        news_items = self.fetcher.fetch(hours=hours)

        if not news_items:
            logger.warning("[MarketNewsAnalyzer] 无新闻数据，返回空结果")
            return self._empty_result(hours)

        # 重要新闻优先 + 截断
        items_to_analyze = self._sort_and_truncate(news_items, max_news)
        news_text = self._format_news_for_llm(items_to_analyze)

        logger.info(f"[MarketNewsAnalyzer] 送入 LLM: {len(items_to_analyze)} 条新闻")
        result = self._call_llm(news_text, hours, len(items_to_analyze))

        # 附加元数据
        result["news_count"] = len(news_items)
        result["hours"] = hours
        result["sources"] = list({item.source for item in news_items})
        result["top_news"] = [
            {"title": item.title, "source": item.source,
             "time": item.published.strftime("%H:%M") if item.published else ""}
            for item in items_to_analyze[:10]
        ]
        return result

    def analyze_with_event(self, event: str, hours: float = 4) -> dict:
        """
        在自动抓取快讯的基础上，额外注入用户手动描述的事件，一起分析。
        适合"我看到了某条消息，帮我结合最新行情评估影响"的场景。
        """
        news_items = self.fetcher.fetch(hours=hours)
        # 将手动事件插到最前
        from src.feeds.news_fetcher import NewsItem
        manual = NewsItem(
            title=f"【用户报告重大事件】{event}",
            summary="",
            source="用户输入",
            published=datetime.utcnow(),
        )
        news_items.insert(0, manual)
        items_to_analyze = news_items[:60]
        news_text = self._format_news_for_llm(items_to_analyze)
        result = self._call_llm(news_text, hours, len(items_to_analyze))
        result["manual_event"] = event
        result["news_count"] = len(news_items)
        result["hours"] = hours
        result["sources"] = list({item.source for item in news_items})
        return result

    @staticmethod
    def recommend_position_ratio(result: dict) -> dict:
        """
        根据新闻分析结果，给出量化持仓比例建议。

        三维打分：
          1. 操作信号（action）      → 基准仓位
          2. 风险等级（risk_level）   → 乘数 + 上限
          3. 市场情绪（sentiment）    → 增减调整
          4. 置信度（confidence）     → 低置信度时向中性50%收敛

        Returns:
            {
              "ratio":       65,          # 最终建议仓位（%）
              "base":        70,          # action基准
              "after_risk":  70,          # 风险调整后
              "after_sent":  70,          # 情绪调整后
              "reasoning":   "...",       # 文字说明
              "band":        (55, 75),    # 合理区间
            }
        """
        action     = result.get("action", "hold")
        risk       = result.get("risk_level", "中")
        sentiment  = result.get("market_sentiment", "中性")
        confidence = float(result.get("confidence", 0.7))

        # ── Step 1：action 基准仓位 ──
        base = _ACTION_BASE_RATIO.get(action, 70)

        # ── Step 2：风险等级调整 ──
        multiplier, cap = _RISK_ADJUST.get(risk, (1.0, 80))
        after_risk = min(round(base * multiplier), cap)

        # ── Step 3：情绪调整 ──
        delta = _SENTIMENT_DELTA.get(sentiment, 0)
        after_sent = max(0, min(after_risk + delta, 90))

        # ── Step 4：置信度收敛（低置信度→向50%中性靠拢）──
        NEUTRAL = 50
        if confidence < 0.6:
            # 置信度不足时，按比例向中性收敛
            weight = confidence / 0.6   # 0→0, 0.6→1
            ratio = round(NEUTRAL + (after_sent - NEUTRAL) * weight)
        else:
            ratio = after_sent

        # 取整到5%刻度，更易执行
        ratio = round(ratio / 5) * 5
        ratio = max(0, min(ratio, 90))  # 永不超过90%（留10%现金）

        # ── 建议区间（±10%） ──
        band = (max(0, ratio - 10), min(90, ratio + 10))

        # ── 文字说明 ──
        reasoning_parts = [
            f"操作信号「{action}」→ 基准 {base}%",
            f"风险「{risk}」× {multiplier} 上限{cap}% → {after_risk}%",
            f"情绪「{sentiment}」{'+' if delta >= 0 else ''}{delta}pp → {after_sent}%",
        ]
        if confidence < 0.6:
            reasoning_parts.append(
                f"置信度偏低({confidence:.0%})，向中性50%收敛 → {ratio}%"
            )

        return {
            "ratio":      ratio,
            "base":       base,
            "after_risk": after_risk,
            "after_sent": after_sent,
            "reasoning":  " → ".join(reasoning_parts),
            "band":       band,
            "action":     action,
            "risk":       risk,
            "sentiment":  sentiment,
            "confidence": confidence,
        }

    def format_report(self, result: dict) -> tuple[str, str]:
        """
        将分析结果格式化为钉钉推送的 (title, content)。
        """
        risk = result.get("risk_level", "低")
        sentiment = result.get("market_sentiment", "中性")
        confidence = result.get("confidence", 0)
        summary = result.get("summary", "")
        recommendation = result.get("recommendation", "")
        action = result.get("action", "hold")
        hours = result.get("hours", 4)
        news_count = result.get("news_count", 0)
        analyzed_at = result.get("analyzed_at", "")[:16].replace("T", " ")
        sources = result.get("sources", [])

        action_emoji, action_name = ACTION_META.get(action, ("ℹ️", action))
        sentiment_emoji = SENTIMENT_EMOJI.get(sentiment, "➡️")

        risk_emoji = {"极高": "🚨", "高": "🔴", "中": "🟡", "低": "🟢"}.get(risk, "⚠️")

        # Key events
        key_events = result.get("key_events", [])
        events_text = ""
        if key_events:
            events_text = "\n**重大事件：**\n"
            for ev in key_events[:5]:
                d = {"利好": "📈", "利空": "📉", "中性": "➡️"}.get(ev.get("direction", ""), "•")
                events_text += f"• {d} {ev.get('event', '')} — {ev.get('impact', '')}\n"

        # Sector impacts
        sectors = result.get("sector_impacts", [])
        sector_text = ""
        if sectors:
            sector_text = "\n**板块影响：**\n\n"
            for s in sectors[:8]:
                d = {"利好": "📈", "利空": "📉", "中性": "➡️"}.get(s.get("direction", ""), "•")
                strength = s.get("strength", "")
                strength_map = {"强": "🔴强", "中": "🟡中", "弱": "🟢弱"}
                strength_label = strength_map.get(strength, strength)
                examples = "、".join(s.get("example_stocks", [])[:3])
                stocks_line = f"> 代表股：{examples}\n" if examples else ""
                sector_text += (
                    f"{d} **{s.get('sector', '')}** [{strength_label}]\n"
                    f"{stocks_line}"
                    f"> {s.get('reason', '')}\n\n"
                )

        # Watch list
        watch = result.get("watch_list", [])
        watch_text = ""
        if watch:
            watch_text = "\n**重点跟踪：**\n" + "".join(f"• 📊 {w}\n" for w in watch[:5])

        # Manual event banner
        manual_banner = ""
        if result.get("manual_event"):
            manual_banner = f"\n> ⚡ **用户触发事件：** {result['manual_event']}\n"

        # 持仓比例建议
        pos = self.recommend_position_ratio(result)
        ratio = pos["ratio"]
        band_lo, band_hi = pos["band"]

        # 仓位可视化进度条（每5%一格，共18格=90%）
        filled = ratio // 5
        bar = "█" * filled + "░" * (18 - filled)
        ratio_line = f"`[{bar}]` **{ratio}%**  合理区间 {band_lo}%~{band_hi}%"

        title = (
            f"{risk_emoji}【市场快讯解读 提醒】"
            f"{sentiment_emoji}{sentiment} | 风险:{risk} | 建议仓位:{ratio}%"
        )

        content = f"""### {risk_emoji} 市场快讯 AI 解读（提醒）
{manual_banner}
---

**整体判断：** {sentiment_emoji} {sentiment}（置信度 {confidence*100:.0f}%）
**风险等级：** {risk_emoji} **{risk}**
**操作建议：** {action_emoji} **{action_name}**

> {summary}

---

### 📊 持仓比例建议

{ratio_line}

| 维度 | 信号 | 调整 |
|------|------|------|
| 操作信号 | {action} | 基准 {pos['base']}% |
| 风险等级 | {risk} | → {pos['after_risk']}% |
| 市场情绪 | {sentiment} | → {pos['after_sent']}% |
| 置信度 | {confidence*100:.0f}% | **最终 {ratio}%** |

{events_text}{sector_text}
**详细建议：**
{recommendation}
{watch_text}
---
*覆盖最近 {hours}h · 快讯 {news_count} 条 · 来源：{", ".join(sources[:4])} 等 · {analyzed_at}*"""

        return title, content

    # ------------------------------------------------------------------ private

    # 重大事件关键词：命中这些词的新闻优先送给 LLM，不被条数截断
    _PRIORITY_KEYWORDS = [
        '301调查', '232调查', '关税', '贸易战', '制裁', '反倾销',
        '加征关税', '贸易调查', '出口管制', '实体清单',
        '降息', '加息', '美联储', 'FOMC', '利率决议',
        '战争', '军事打击', '冲突升级', '核',
        '金融危机', '银行倒闭', '流动性危机',
        '重大政策', '国务院', '中央经济工作', '降准',
    ]

    @classmethod
    def _priority_score(cls, item: 'NewsItem') -> int:
        """命中重大关键词返回1（优先），否则0"""
        text = item.title + (item.summary or '')
        return 1 if any(k in text for k in cls._PRIORITY_KEYWORDS) else 0

    def _sort_and_truncate(self, items: List[NewsItem], max_news: int) -> List[NewsItem]:
        """
        先保留所有命中重大关键词的新闻（不超过 max_news//3 条），
        再用剩余名额按时间降序填充普通新闻，总数不超过 max_news。
        """
        priority = [n for n in items if self._priority_score(n)]
        normal   = [n for n in items if not self._priority_score(n)]

        # 重要新闻最多占 1/3 名额，避免把普通行情全挤走
        priority_cap = max(max_news // 3, len(priority))  # 有多少放多少，最多占1/3
        priority_cap = min(priority_cap, max_news // 3 + 5)
        selected_priority = priority[:priority_cap]
        remaining = max_news - len(selected_priority)
        selected_normal = normal[:remaining]

        if selected_priority:
            print(f"  [NewsAnalyzer] 重要新闻优先: {len(selected_priority)} 条 "
                  f"(命中关键词: {[n.title[:30] for n in selected_priority[:3]]})")

        # 合并后按时间降序重排（保持 LLM 看到的顺序）
        merged = selected_priority + selected_normal
        merged.sort(key=lambda x: x.published or __import__('datetime').datetime.min, reverse=True)
        return merged

    def _format_news_for_llm(self, items: List[NewsItem]) -> str:
        lines = []
        for i, item in enumerate(items, 1):
            time_str = item.published.strftime("%m-%d %H:%M") if item.published else "未知"
            text = item.text()[:150]
            lines.append(f"{i}. [{item.source} {time_str}] {text}")
        return "\n".join(lines)

    def _call_llm(self, news_text: str, hours: float, count: int) -> dict:
        if not self.llm.is_available():
            return self._empty_result(hours)

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            hours=hours,
            count=count,
            news_text=news_text,
            now=datetime.now().isoformat()[:16],
        )

        try:
            raw = self.llm._call_llm(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                temperature=0.25,
                max_tokens=2000,
            )
            if not raw:
                return self._empty_result(hours)
            result = self._parse_json(raw)
            return result
        except Exception as e:
            logger.error(f"[MarketNewsAnalyzer] LLM 调用失败: {e}")
            return self._empty_result(hours)

    def _parse_json(self, text: str) -> dict:
        text = text.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        raise ValueError(f"无法提取 JSON: {text[:200]}")

    def _empty_result(self, hours: float) -> dict:
        return {
            "risk_level": "低",
            "market_sentiment": "中性",
            "confidence": 0.0,
            "summary": "暂无新闻数据或 LLM 不可用",
            "key_events": [],
            "sector_impacts": [],
            "recommendation": "LLM 不可用，无法自动分析，请人工判断",
            "action": "hold",
            "watch_list": [],
            "analyzed_at": datetime.now().isoformat()[:16],
            "hours": hours,
            "news_count": 0,
            "sources": [],
            "top_news": [],
        }
