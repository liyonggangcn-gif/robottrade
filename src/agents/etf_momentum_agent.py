"""
ETFMomentumAgent — 景气度 + 动量评分 Agent

两个子维度：
  1. 行业景气度 (15%)：当前经济周期对应行业是否占优
  2. 相对动量   (15%)：近期相对沪深300的超额收益

合并权重在 ETFSelectionAgent 里汇总，这里只输出 0-1 评分
"""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import pandas as pd
import numpy as np

from .base_agent import BaseAgent

warnings.filterwarnings("ignore")

# ── 行业周期配置（动态周期下的行业超配/低配）───────────────────────────────
CYCLE_OVERWEIGHT = {
    "early":     ["银行", "非银金融", "房地产", "汽车", "消费"],
    "mid":       ["电子", "计算机", "通信", "电力设备", "军工", "医药"],
    "late":      ["煤炭", "有色金属", "石油石化", "钢铁", "化工"],
    "defensive": ["医药生物", "食品饮料", "公用事业", "黄金", "国债"],
}

# ETF 名称 → 行业分类（用于与 CYCLE_OVERWEIGHT 对应）
ETF_INDUSTRY_MAP = {
    "银行": "银行", "证券": "非银金融", "医药": "医药", "消费": "消费",
    "新能源": "电力设备", "军工": "军工", "科技": "电子", "半导体": "电子",
    "芯片": "电子", "通信": "通信", "有色": "有色金属", "能源": "煤炭",
    "化工": "化工", "红利": "消费", "黄金": "黄金", "国债": "国债",
    "煤炭": "煤炭", "石化": "石油石化", "钢铁": "钢铁",
}


class ETFMomentumAgent(BaseAgent):
    """
    输入: etf_list, cycle (来自 MarketEnvAgent)
    输出: momentum_scores {code: {score, cycle_match, relative_strength, detail}}
    """

    BENCHMARK = "sh000300"   # 沪深300

    def run(self, etf_list: List[Dict], cycle: str = "mid", **kwargs) -> Dict[str, Any]:
        benchmark_rets = self._get_benchmark_returns()
        scores: Dict[str, Dict] = {}

        for etf in etf_list:
            code = etf["code"]
            name = etf["name"]
            cat  = etf["category"]

            try:
                # 周期景气度评分
                cycle_score, cycle_match = self._score_cycle(name, cat, cycle)

                # 相对强度评分
                rs_score, rs_detail = self._score_relative_strength(
                    code, name, benchmark_rets
                )

                # 综合
                momentum_score = cycle_score * 0.5 + rs_score * 0.5

                scores[code] = {
                    "score":           round(momentum_score, 3),
                    "cycle_score":     round(cycle_score, 3),
                    "rs_score":        round(rs_score, 3),
                    "cycle_match":     cycle_match,
                    "detail":          rs_detail,
                }
            except Exception as e:
                self.logger.warning(f"{code} 动量评分失败: {e}")
                scores[code] = {"score": 0.5, "cycle_score": 0.5, "rs_score": 0.5,
                                "cycle_match": False, "detail": str(e)}

        return {"momentum_scores": scores, "cycle": cycle}

    # ── 周期匹配评分 ────────────────────────────────────────────────────────
    def _score_cycle(self, name: str, cat: str, cycle: str):
        overweight = CYCLE_OVERWEIGHT.get(cycle, [])

        # 债券/黄金在防守周期超配
        if cat == "bond" and cycle == "defensive":
            return 1.0, True
        if cat == "gold" and cycle in ("defensive", "late"):
            return 0.9, True

        # 基于名称关键词匹配行业
        industry = None
        for kw, ind in ETF_INDUSTRY_MAP.items():
            if kw in name:
                industry = ind
                break

        if industry and industry in overweight:
            return 0.85, True
        elif industry:
            return 0.35, False
        else:
            return 0.5, False   # 未知行业，中性

    # ── 相对强度评分 ────────────────────────────────────────────────────────
    def _score_relative_strength(
        self, code: str, name: str, benchmark_rets: Optional[Dict]
    ):
        hist = self._get_etf_history(code, name)
        if hist is None or len(hist) < 25:
            return 0.5, "历史数据不足"

        ret_5d  = hist["close"].iloc[-1] / hist["close"].iloc[-6]  - 1 if len(hist) >= 6  else 0
        ret_20d = hist["close"].iloc[-1] / hist["close"].iloc[-21] - 1 if len(hist) >= 21 else 0
        ret_60d = hist["close"].iloc[-1] / hist["close"].iloc[-61] - 1 if len(hist) >= 61 else 0

        # 超额收益
        bm_5d  = benchmark_rets.get("ret_5d",  0) if benchmark_rets else 0
        bm_20d = benchmark_rets.get("ret_20d", 0) if benchmark_rets else 0
        bm_60d = benchmark_rets.get("ret_60d", 0) if benchmark_rets else 0

        excess_5d  = ret_5d  - bm_5d
        excess_20d = ret_20d - bm_20d
        excess_60d = ret_60d - bm_60d

        # 加权超额（短期20% + 中期50% + 长期30%）
        weighted_excess = excess_5d * 0.2 + excess_20d * 0.5 + excess_60d * 0.3

        # 归一化到 [0, 1]，[-5%, +5%] → [0, 1]
        rs_score = min(1.0, max(0.0, (weighted_excess + 0.05) / 0.10))

        detail = (f"超额: 5d={excess_5d*100:+.1f}% "
                  f"20d={excess_20d*100:+.1f}% 60d={excess_60d*100:+.1f}%")
        return round(rs_score, 3), detail

    # ── 数据获取 ────────────────────────────────────────────────────────────
    def _get_etf_history(self, code: str, name: str) -> pd.DataFrame | None:
        try:
            import akshare as ak
            pure = code.lower().replace("sh", "").replace("sz", "")
            end   = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
            df = ak.fund_etf_hist_em(
                symbol=pure, period="daily",
                start_date=start, end_date=end, adjust="qfq"
            )
            if df is None or df.empty:
                return None
            df = df.rename(columns={"日期": "date", "收盘": "close"})
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df = df.sort_values("date").dropna(subset=["close"])
            return df
        except Exception as e:
            self.logger.debug(f"{code} 历史数据失败: {e}")
            return None

    def _get_benchmark_returns(self) -> Dict | None:
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily_em(symbol=self.BENCHMARK)
            if df is None or df.empty:
                return None
            df = df.sort_values("date")
            c = df["close"]

            def ret(n):
                return float(c.iloc[-1] / c.iloc[-(n+1)] - 1) if len(c) > n else 0

            return {"ret_5d": ret(5), "ret_20d": ret(20), "ret_60d": ret(60)}
        except Exception:
            return None
