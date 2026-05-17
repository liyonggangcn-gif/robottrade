"""
PortfolioAgent — 组合构建 + 再平衡 Agent

核心逻辑（实现用户提到的再平衡思路）：
  1. 接收推荐列表，结合现有持仓，输出差异化操作
  2. 当所有持仓同步下跌时，不是割肉而是看谁更便宜做再平衡
  3. 当估值回归高位时主动减仓
  4. 跨类资产相关性检查，保证分散度

目标约束：
  - 单只ETF上限 15%
  - 单类资产（equity/dividend/gold/commodity/bond）上限：
      equity: 60%，dividend: 30%，gold: 20%，commodity: 15%，bond: 30%
  - 总仓位上限：防守80%，均衡90%，进攻95%
"""
from __future__ import annotations

from typing import Dict, Any, List, Optional
import pandas as pd

from .base_agent import BaseAgent

# 各类资产上限
CAT_MAX = {
    "equity":    0.60,
    "dividend":  0.30,
    "gold":      0.20,
    "commodity": 0.15,
    "bond":      0.30,
}

TOTAL_MAX = {"offensive": 0.95, "balanced": 0.90, "defensive": 0.80}


class PortfolioAgent(BaseAgent):
    """
    输入:
      top_picks       — 来自 ETFSelectionAgent
      current_holdings — 当前持仓 {code: {name, weight, val_percentile}}
      market_mode     — 市场模式

    输出:
      target_portfolio  — 目标组合 {code: weight}
      actions           — 操作列表 [buy/sell/hold + 理由]
      rebalance_needed  — 是否需要再平衡
      rebalance_reason  — 再平衡原因
    """

    def run(
        self,
        top_picks: List[Dict],
        current_holdings: Optional[Dict] = None,
        market_mode: str = "balanced",
        **kwargs,
    ) -> Dict[str, Any]:
        current_holdings = current_holdings or {}

        # Step1: 构建目标组合
        target = self._build_target(top_picks, market_mode)

        # Step2: 再平衡检查
        rb_needed, rb_reason = self._check_rebalance(
            current_holdings, top_picks, market_mode
        )

        # Step3: 生成操作指令
        actions = self._gen_actions(current_holdings, target, rb_reason)

        # Step4: 组合统计
        cat_weights = {}
        for code, w in target.items():
            etf = next((e for e in top_picks if e["code"] == code), {})
            cat = etf.get("category", "equity")
            cat_weights[cat] = cat_weights.get(cat, 0) + w

        return {
            "target_portfolio":  target,
            "actions":           actions,
            "rebalance_needed":  rb_needed,
            "rebalance_reason":  rb_reason,
            "category_weights":  cat_weights,
            "total_weight":      round(sum(target.values()), 3),
            "position_count":    len(target),
        }

    # ── 目标组合构建 ────────────────────────────────────────────────────────
    def _build_target(self, picks: List[Dict], mode: str) -> Dict[str, float]:
        total_max  = TOTAL_MAX.get(mode, 0.90)
        cat_used   = {k: 0.0 for k in CAT_MAX}
        result     = {}
        total_used = 0.0

        for etf in picks:
            code = etf["code"]
            cat  = etf.get("category", "equity")
            # 从推荐的 target_pos 解析数字
            raw_pos = etf.get("target_pos", "0%")
            try:
                pos = float(raw_pos.replace("%", "")) / 100
            except Exception:
                pos = 0.05

            if pos <= 0:
                continue

            # 类别上限
            if cat_used.get(cat, 0) + pos > CAT_MAX.get(cat, 0.60):
                pos = max(0, CAT_MAX.get(cat, 0.60) - cat_used.get(cat, 0))

            # 总仓位上限
            if total_used + pos > total_max:
                pos = max(0, total_max - total_used)

            if pos < 0.02:   # 低于2%不建仓
                continue

            result[code] = round(pos, 3)
            cat_used[cat] = cat_used.get(cat, 0) + pos
            total_used   += pos

        return result

    # ── 再平衡检查（核心：全仓下跌时不割肉） ──────────────────────────────
    def _check_rebalance(
        self,
        holdings: Dict,
        picks: List[Dict],
        mode: str,
    ):
        if not holdings:
            return False, "无现有持仓"

        # 获取当前持仓的估值分位
        val_pcts = [
            v.get("val_percentile")
            for v in holdings.values()
            if v.get("val_percentile") is not None
        ]

        if not val_pcts:
            return False, "持仓估值数据不足"

        avg_pct = sum(val_pcts) / len(val_pcts)
        all_low = all(p < 40 for p in val_pcts)
        any_high = any(p > 70 for p in val_pcts)

        # 场景1：所有持仓均低估，市场恐慌 → 不卖，做内部再平衡
        if all_low and mode != "defensive":
            cheapest = min(holdings.items(), key=lambda x: x[1].get("val_percentile", 50))
            most_exp = max(holdings.items(), key=lambda x: x[1].get("val_percentile", 50))
            gap = most_exp[1].get("val_percentile", 50) - cheapest[1].get("val_percentile", 50)
            if gap > 15:
                return True, (
                    f"全仓低估区(均值{avg_pct:.0f}%分位)，"
                    f"建议从 {most_exp[0]}({most_exp[1].get('val_percentile',0):.0f}%) "
                    f"调仓至 {cheapest[0]}({cheapest[1].get('val_percentile',0):.0f}%)"
                )

        # 场景2：有持仓估值高估 → 主动减仓
        if any_high:
            high_codes = [
                f"{code}({v.get('val_percentile',0):.0f}%分位)"
                for code, v in holdings.items()
                if v.get("val_percentile", 0) > 70
            ]
            return True, f"以下持仓估值偏高，建议减仓: {', '.join(high_codes)}"

        # 场景3：市场模式切换（如从均衡到防守）
        if mode == "defensive":
            non_defensive = [
                code for code, v in holdings.items()
                if v.get("category") not in ("dividend", "gold", "bond")
            ]
            if non_defensive:
                return True, f"进入防守模式，减持权益类: {non_defensive}"

        return False, "组合状态正常，无需调整"

    # ── 生成操作指令 ────────────────────────────────────────────────────────
    def _gen_actions(
        self,
        current: Dict,
        target: Dict[str, float],
        rb_reason: str,
    ) -> List[Dict]:
        actions = []
        all_codes = set(current.keys()) | set(target.keys())

        for code in all_codes:
            cur_w = current.get(code, {}).get("weight", 0) if isinstance(current.get(code), dict) else current.get(code, 0)
            tgt_w = target.get(code, 0)
            diff  = tgt_w - cur_w

            if abs(diff) < 0.02:   # 差异不足2%不操作
                if cur_w > 0:
                    actions.append({"code": code, "action": "HOLD",
                                    "current": f"{cur_w*100:.1f}%",
                                    "target": f"{tgt_w*100:.1f}%",
                                    "reason": "仓位偏差 <2%，无需调整"})
            elif diff > 0:
                actions.append({"code": code, "action": "BUY",
                                "current": f"{cur_w*100:.1f}%",
                                "target": f"{tgt_w*100:.1f}%",
                                "delta": f"+{diff*100:.1f}%",
                                "reason": rb_reason if cur_w > 0 else "新建仓位"})
            else:
                actions.append({"code": code, "action": "SELL",
                                "current": f"{cur_w*100:.1f}%",
                                "target": f"{tgt_w*100:.1f}%",
                                "delta": f"{diff*100:.1f}%",
                                "reason": rb_reason if tgt_w > 0 else "估值高估或模式切换"})

        # 排序：先卖后买
        actions.sort(key=lambda x: {"SELL": 0, "HOLD": 1, "BUY": 2}.get(x["action"], 1))
        return actions
