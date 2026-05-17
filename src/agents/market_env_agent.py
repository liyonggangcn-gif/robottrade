"""
MarketEnvAgent — 市场环境判断 Agent
判断当前：
  - 市场模式 mode:  offensive / balanced / defensive
  - 经济周期 cycle: early / mid / late / defensive
  - 风险开关 risk_on: True/False

输出驱动后续所有 Agent 的配置策略。
目标：年化10%收益率下，防守模式保本，进攻模式超额。
"""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from typing import Dict, Any

import pandas as pd

from .base_agent import BaseAgent

warnings.filterwarnings("ignore")


class MarketEnvAgent(BaseAgent):
    """
    信号来源（优先级从高到低）：
      1. Tushare  cn_pmi       —— 制造业PMI
      2. AKShare  macro_china_pmi_monthly
      3. AKShare  bond_zh_us_rate —— 10年国债收益率
      4. AKShare  stock_zh_index_daily_em (000300) —— 市场趋势

    判断逻辑：
      mode:
        offensive  = PMI扩张 + 市场趋势向上 + 无信用风险
        defensive  = PMI连续收缩 OR 市场近20日跌幅 > 8%
        balanced   = 其他

      cycle:
        early   = PMI从收缩回升 + 利率下行
        mid     = PMI稳定扩张 + 利率平稳
        late    = PMI高位回落 + 利率上行
        defensive = PMI收缩 + 利率下行（宽松救市）
    """

    # ── 内部阈值 ────────────────────────────────────────────────────────────
    PMI_EXPAND   = 50.0
    PMI_STRONG   = 51.5
    MARKET_PANIC = -0.08   # 20日跌幅触发防守
    RATE_RISE    = 0.30    # bp，3个月内上行超过此值算利率上行

    def run(self, **kwargs) -> Dict[str, Any]:
        pmi_series  = self._get_pmi()
        rate_series = self._get_10y_yield()
        mkt_ret20   = self._get_market_trend()

        mode  = self._determine_mode(pmi_series, mkt_ret20)
        cycle = self._determine_cycle(pmi_series, rate_series)

        latest_pmi  = pmi_series.iloc[-1]  if pmi_series  is not None and len(pmi_series)  else None
        latest_rate = rate_series.iloc[-1] if rate_series is not None and len(rate_series) else None

        return {
            "mode":       mode,    # offensive / balanced / defensive
            "cycle":      cycle,   # early / mid / late / defensive
            "risk_on":    mode != "defensive",
            "pmi":        round(float(latest_pmi),  2) if latest_pmi  is not None else None,
            "rate_10y":   round(float(latest_rate), 3) if latest_rate is not None else None,
            "market_ret20d": round(float(mkt_ret20), 4) if mkt_ret20 is not None else None,
            "mode_reason": self._mode_reason,
            "cycle_reason": self._cycle_reason,
        }

    # ── 模式判断 ─────────────────────────────────────────────────────────────
    def _determine_mode(self, pmi, mkt_ret20) -> str:
        reasons = []

        # 防守条件（任一满足即进入防守）
        if mkt_ret20 is not None and mkt_ret20 < self.MARKET_PANIC:
            reasons.append(f"市场20日跌幅{mkt_ret20*100:.1f}% < -8%")
        if pmi is not None and len(pmi) >= 3:
            last3 = pmi.iloc[-3:].tolist()
            if all(v < self.PMI_EXPAND for v in last3):
                reasons.append(f"PMI连续3月收缩({last3})")

        if reasons:
            self._mode_reason = "防守: " + "; ".join(reasons)
            return "defensive"

        # 进攻条件（同时满足）
        off_reasons = []
        if pmi is not None and len(pmi) >= 2:
            if pmi.iloc[-1] > self.PMI_STRONG and pmi.iloc[-2] > self.PMI_EXPAND:
                off_reasons.append(f"PMI连续扩张({pmi.iloc[-1]:.1f})")
        if mkt_ret20 is not None and mkt_ret20 > 0.02:
            off_reasons.append(f"市场20日涨{mkt_ret20*100:.1f}%")

        if len(off_reasons) >= 2:
            self._mode_reason = "进攻: " + "; ".join(off_reasons)
            return "offensive"

        self._mode_reason = "均衡: 信号混杂"
        return "balanced"

    # ── 周期判断 ─────────────────────────────────────────────────────────────
    def _determine_cycle(self, pmi, rate) -> str:
        if pmi is None or len(pmi) < 6:
            self._cycle_reason = "数据不足，默认mid"
            return "mid"

        pmi_now  = pmi.iloc[-1]
        pmi_3m   = pmi.iloc[-4]   # 3个月前
        pmi_trend = pmi_now - pmi_3m   # 正=扩张，负=收缩

        rate_trend = 0.0
        if rate is not None and len(rate) >= 60:
            rate_trend = rate.iloc[-1] - rate.iloc[-60]   # 近3个月利率变化(%)

        if pmi_now < self.PMI_EXPAND and rate_trend < 0:
            self._cycle_reason = f"PMI收缩({pmi_now:.1f}) + 利率下行({rate_trend:+.2f}%)"
            return "defensive"
        elif pmi_trend > 0.5 and rate_trend <= self.RATE_RISE:
            self._cycle_reason = f"PMI回升趋势(+{pmi_trend:.1f}) + 利率平稳"
            return "early"
        elif pmi_now > self.PMI_EXPAND and abs(pmi_trend) < 0.5:
            self._cycle_reason = f"PMI稳定扩张({pmi_now:.1f})"
            return "mid"
        elif pmi_trend < -0.5 or rate_trend > self.RATE_RISE:
            self._cycle_reason = f"PMI回落趋势({pmi_trend:.1f}) + 利率{rate_trend:+.2f}%"
            return "late"
        else:
            self._cycle_reason = "信号模糊，默认mid"
            return "mid"

    # ── 数据获取 ─────────────────────────────────────────────────────────────
    def _get_pmi(self) -> pd.Series | None:
        """制造业PMI，返回近12个月Series"""
        try:
            import akshare as ak
            df = ak.macro_china_pmi_monthly()
            # 列名通常是 '月份', '制造业-指数'
            date_col  = df.columns[0]
            value_col = [c for c in df.columns if "制造业" in c and "指数" in c]
            if not value_col:
                value_col = [df.columns[1]]
            df = df[[date_col, value_col[0]]].copy()
            df.columns = ["date", "pmi"]
            df["pmi"] = pd.to_numeric(df["pmi"], errors="coerce")
            df = df.dropna().sort_values("date").tail(12)
            return df["pmi"].reset_index(drop=True)
        except Exception as e:
            self.logger.warning(f"PMI获取失败: {e}")
            return None

    def _get_10y_yield(self) -> pd.Series | None:
        """10年期国债收益率，返回近250个交易日"""
        try:
            import akshare as ak
            df = ak.bond_zh_us_rate(start_date="20230101")
            if df is None or df.empty:
                return None
            rate_col = [c for c in df.columns if "中国" in c and "10" in c]
            if not rate_col:
                rate_col = [df.columns[1]]
            df = df[["日期", rate_col[0]]].copy()
            df.columns = ["date", "rate"]
            df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
            df = df.dropna().sort_values("date").tail(250)
            return df["rate"].reset_index(drop=True)
        except Exception as e:
            self.logger.warning(f"国债收益率获取失败: {e}")
            return None

    def _get_market_trend(self) -> float | None:
        """沪深300近20日涨跌幅"""
        try:
            import akshare as ak
            end   = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=40)).strftime("%Y%m%d")
            df = ak.stock_zh_index_daily_em(symbol="sh000300")
            if df is None or df.empty:
                return None
            df = df.sort_values("date").tail(22)
            if len(df) < 2:
                return None
            ret = df["close"].iloc[-1] / df["close"].iloc[0] - 1
            return float(ret)
        except Exception as e:
            self.logger.warning(f"市场趋势获取失败: {e}")
            return None
