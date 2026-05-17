"""
ETFSelectionAgent — 综合评分 + 最终排名 Agent

权重汇总：
  估值分位  40%  （核心，防止高估位买入）
  景气度    30%  （周期匹配15% + 相对强度15%）
  技术面    20%  （回调/RSI/量能/趋势）
  情绪      10%  （目前用市场整体强弱代替）

输出：按综合分排序的 ETF 推荐列表，含建仓建议
"""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import pandas as pd
import numpy as np

from .base_agent import BaseAgent

warnings.filterwarnings("ignore")


class ETFSelectionAgent(BaseAgent):
    """
    输入:
      etf_list          — 来自 ETFUniverseAgent
      valuation_scores  — 来自 ETFValuationAgent
      momentum_scores   — 来自 ETFMomentumAgent
      market_mode       — offensive / balanced / defensive
      market_ret20d     — 市场近20日涨跌幅（情绪代理）

    输出:
      recommendations: List[Dict]  排序后的推荐列表
    """

    # ── 权重（可在 defensive 模式下调整） ───────────────────────────────────
    WEIGHTS_OFFENSIVE  = {"val": 0.35, "mom": 0.35, "tech": 0.20, "sent": 0.10}
    WEIGHTS_BALANCED   = {"val": 0.40, "mom": 0.30, "tech": 0.20, "sent": 0.10}
    WEIGHTS_DEFENSIVE  = {"val": 0.50, "mom": 0.20, "tech": 0.15, "sent": 0.15}

    # 估值分位超过此值直接排除（高估否决）
    MAX_VAL_PERCENTILE = 75

    def run(
        self,
        etf_list: List[Dict],
        valuation_scores: Dict,
        momentum_scores: Dict,
        market_mode: str = "balanced",
        market_ret20d: float = 0.0,
        **kwargs,
    ) -> Dict[str, Any]:

        weights = {
            "offensive": self.WEIGHTS_OFFENSIVE,
            "balanced":  self.WEIGHTS_BALANCED,
            "defensive": self.WEIGHTS_DEFENSIVE,
        }.get(market_mode, self.WEIGHTS_BALANCED)

        # 情绪评分（市场20日涨跌幅归一化）
        sent_score = min(1.0, max(0.0, (market_ret20d + 0.10) / 0.20))

        results = []
        for etf in etf_list:
            code = etf["code"]
            val_info = valuation_scores.get(code, {})
            mom_info = momentum_scores.get(code, {})

            val_score = val_info.get("score")
            val_pct   = val_info.get("percentile")
            mom_score = mom_info.get("score", 0.5)

            # 高估否决
            if val_pct is not None and val_pct > self.MAX_VAL_PERCENTILE:
                continue
            if val_score is None:
                val_score = 0.5

            # 技术面评分（简化版，避免每只ETF再拉历史）
            tech_score = self._quick_tech_score(etf, val_pct)

            # 综合评分
            final = (
                val_score  * weights["val"]  +
                mom_score  * weights["mom"]  +
                tech_score * weights["tech"] +
                sent_score * weights["sent"]
            )

            # 信号等级
            signal, reason = self._classify_signal(
                final, val_pct, mom_info.get("cycle_match", False)
            )

            # 建仓建议（基于估值分位，目标年化10%）
            pos_advice = self._position_advice(val_pct, final, market_mode)

            results.append({
                "code":          code,
                "name":          etf["name"],
                "category":      etf["category"],
                "index_code":    etf.get("index_code"),
                "final_score":   round(final, 3),
                "val_score":     round(val_score, 3),
                "val_percentile": round(val_pct, 1) if val_pct is not None else None,
                "val_method":    val_info.get("method", ""),
                "val_detail":    val_info.get("detail", ""),
                "mom_score":     round(mom_score, 3),
                "cycle_match":   mom_info.get("cycle_match", False),
                "rs_detail":     mom_info.get("detail", ""),
                "tech_score":    round(tech_score, 3),
                "signal":        signal,
                "reason":        reason,
                "target_pos":    pos_advice["target_pos"],
                "add_condition": pos_advice["add_condition"],
                "stop_loss":     pos_advice["stop_loss"],
                "amount_wan":    etf.get("amount_wan", 0),
                "mv_yi":         etf.get("mv_yi", 0),
            })

        # 按综合分排序
        results.sort(key=lambda x: x["final_score"], reverse=True)

        # 计算建议持仓数量（目标年化10%需要分散持仓）
        top_n = self._recommend_count(market_mode)
        top   = results[:top_n]

        self.logger.info(
            f"选股完成: 总{len(results)}只有效 → 推荐{len(top)}只 "
            f"(模式={market_mode})"
        )
        return {
            "recommendations": results,
            "top_picks":       top,
            "market_mode":     market_mode,
            "weights_used":    weights,
        }

    # ── 技术面快速评分（避免再拉数据，用已有字段） ─────────────────────────
    @staticmethod
    def _quick_tech_score(etf: Dict, val_pct: Optional[float]) -> float:
        """
        在没有拉 K 线的情况下，用估值分位代理"价格超卖"程度
        val_pct 低 → 技术面也偏低位 → 得分高
        """
        if val_pct is None:
            return 0.5
        # 低估区间 (<30%) 给技术面加分，高估反之
        if val_pct < 20:  return 0.85
        if val_pct < 35:  return 0.70
        if val_pct < 50:  return 0.55
        if val_pct < 65:  return 0.35
        return 0.20

    # ── 信号分类 ────────────────────────────────────────────────────────────
    @staticmethod
    def _classify_signal(score: float, val_pct: Optional[float], cycle_match: bool):
        reasons = []

        if val_pct is not None:
            if val_pct < 20:
                reasons.append(f"估值极低({val_pct:.0f}%分位)")
            elif val_pct < 35:
                reasons.append(f"估值偏低({val_pct:.0f}%分位)")

        if cycle_match:
            reasons.append("周期匹配")

        if score >= 0.75:
            return "强推荐", "; ".join(reasons) or "综合评分极高"
        if score >= 0.60:
            return "推荐",   "; ".join(reasons) or "综合评分良好"
        if score >= 0.45:
            return "关注",   "评分中等，可轻仓观察"
        return "观望",       "估值或动量偏弱"

    # ── 建仓建议（核心：围绕10%年化目标） ──────────────────────────────────
    @staticmethod
    def _position_advice(val_pct: Optional[float], score: float, mode: str) -> Dict:
        """
        仓位逻辑：
          - 极度低估 (<20%分位) + 高评分 → 重仓 15%
          - 低估 (20-35%) + 好评分    → 标配 10%
          - 合理偏低 (35-50%)         → 轻仓 5%
          防守模式整体砍半
        """
        if val_pct is None:
            val_pct = 50

        if val_pct < 20 and score >= 0.65:
            base_pos, stop = "15%", "估值回到50%分位以上时减仓"
            add = "确认连续3日收涨后可加至20%"
        elif val_pct < 35 and score >= 0.55:
            base_pos, stop = "10%", "估值回到60%分位以上时减仓"
            add = "价格企稳放量后加至15%"
        elif val_pct < 50:
            base_pos, stop = "5%",  "估值回到70%分位以上清仓"
            add = "等待更好的买点（估值进一步下降）"
        else:
            base_pos, stop = "0%",  "不建议建仓"
            add = "等待估值回到50%分位以下"

        # 防守模式仓位减半
        if mode == "defensive":
            pos_num = int(base_pos.replace("%", "")) // 2
            base_pos = f"{pos_num}%"
            add = "防守模式，保守建仓"

        return {"target_pos": base_pos, "add_condition": add, "stop_loss": stop}

    @staticmethod
    def _recommend_count(mode: str) -> int:
        """目标年化10%：防守5只，均衡8只，进攻10只"""
        return {"offensive": 10, "balanced": 8, "defensive": 5}.get(mode, 8)
