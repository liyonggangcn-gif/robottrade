"""
ETFValuationAgent — 四类资产估值评分 Agent

估值方法：
  equity    → 底层指数 PE/PB 历史10年分位（Tushare index_dailybasic）
  dividend  → 股息率历史分位 + 股债利差分位
  gold      → 实际利率方向 + 金价历史3年分位
  commodity → 商品价格历史3年分位
  bond      → 10年国债收益率历史分位（反向：收益率高=价格便宜）

评分 0~1，越高越便宜（越值得买）
估值分位 > 75% 直接给 0 分（高估排除）
"""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import numpy as np
import pandas as pd

from .base_agent import BaseAgent

warnings.filterwarnings("ignore")

TUSHARE_TOKEN = "0093d31f4758df12b01f312a922a49e837d07c18dba2ae5c3ac6d67f"
LOOKBACK_YEARS_EQUITY = 10
LOOKBACK_YEARS_COMMODITY = 3


def _pct_to_score(percentile: float) -> float:
    """估值分位 → 评分（线性映射，分位越低评分越高）"""
    if percentile > 75:
        return 0.0
    if percentile < 10:
        return 1.0
    # 10~75% → 1.0~0.05 线性
    return round(1.0 - (percentile - 10) / (75 - 10) * 0.95, 3)


class ETFValuationAgent(BaseAgent):
    """
    输入: etf_list (来自 ETFUniverseAgent)
    输出: valuation_scores {code: {score, percentile, method, detail}}
    """

    def __init__(self):
        super().__init__()
        self._ts_pro = None
        self._index_cache: Dict[str, pd.DataFrame] = {}  # 指数历史缓存
        self._gold_price_cache: Optional[pd.Series] = None
        self._rate_cache: Optional[pd.Series] = None

    def run(self, etf_list: List[Dict], **kwargs) -> Dict[str, Any]:
        scores: Dict[str, Dict] = {}
        for etf in etf_list:
            code = etf["code"]
            cat  = etf["category"]
            try:
                if cat == "equity":
                    r = self._score_equity(etf)
                elif cat == "dividend":
                    r = self._score_dividend(etf)
                elif cat == "gold":
                    r = self._score_gold()
                elif cat == "commodity":
                    r = self._score_commodity(etf)
                elif cat == "bond":
                    r = self._score_bond()
                else:
                    r = {"score": 0.5, "percentile": 50, "method": "default", "detail": "未知类型"}
            except Exception as e:
                self.logger.warning(f"{code} 估值失败: {e}")
                r = {"score": None, "percentile": None, "method": "error", "detail": str(e)}

            scores[code] = r

        valid = [(c, v["score"]) for c, v in scores.items() if v.get("score") is not None]
        self.logger.info(f"估值评分完成: {len(valid)}/{len(etf_list)} 只有效")
        return {"valuation_scores": scores}

    # ── equity: PE/PB历史分位 ──────────────────────────────────────────────
    def _score_equity(self, etf: Dict) -> Dict:
        idx = etf.get("index_code")
        if not idx:
            return {"score": 0.5, "percentile": 50, "method": "no_index", "detail": "无底层指数映射"}

        hist = self._get_index_valuation(idx)
        if hist is None or hist.empty:
            return {"score": 0.5, "percentile": 50, "method": "no_data", "detail": "无历史估值数据"}

        cur_pe = hist["pe_ttm"].iloc[-1]
        cur_pb = hist["pb"].iloc[-1]

        pe_pct = float(np.percentile(hist["pe_ttm"].dropna(), 0))  # init
        pe_pct = _safe_percentile(hist["pe_ttm"].dropna().values, cur_pe)
        pb_pct = _safe_percentile(hist["pb"].dropna().values, cur_pb)

        # PE 权重 60%，PB 权重 40%
        combined_pct = pe_pct * 0.6 + pb_pct * 0.4
        score = _pct_to_score(combined_pct)

        return {
            "score":      score,
            "percentile": round(combined_pct, 1),
            "method":     "PE/PB分位",
            "detail":     f"PE={cur_pe:.1f}x({pe_pct:.0f}%) PB={cur_pb:.2f}x({pb_pct:.0f}%)",
        }

    # ── dividend: 股息率分位 + 股债利差 ────────────────────────────────────
    def _score_dividend(self, etf: Dict) -> Dict:
        idx = etf.get("index_code")
        hist = self._get_index_valuation(idx) if idx else None

        if hist is None or hist.empty or "dv_ratio" not in hist.columns:
            return {"score": 0.5, "percentile": 50, "method": "dv_ratio缺失", "detail": ""}

        cur_dv = hist["dv_ratio"].iloc[-1]
        if not cur_dv or cur_dv <= 0:
            return {"score": 0.5, "percentile": 50, "method": "股息率为0", "detail": ""}

        # 股息率分位（越高越便宜，所以反向）
        dv_pct = _safe_percentile(hist["dv_ratio"].dropna().values, cur_dv)
        dv_score = _pct_to_score(100 - dv_pct)  # 反向

        # 股债利差：股息率 - 10年国债收益率
        rate = self._get_latest_10y_rate()
        if rate:
            spread = cur_dv - rate
            # 利差历史分位（Tushare历史数据有限，用固定参考值）
            # 利差 > 2% 极度有利，利差 < 0 不利
            spread_score = min(1.0, max(0.0, (spread + 1) / 3))  # [-1%, +2%] → [0, 1]
        else:
            spread_score = 0.5

        score = dv_score * 0.6 + spread_score * 0.4
        return {
            "score":      round(score, 3),
            "percentile": round(100 - dv_pct, 1),
            "method":     "股息率分位+股债利差",
            "detail":     f"股息率={cur_dv:.2f}%({dv_pct:.0f}分位) 国债={rate:.2f}% 利差={cur_dv-(rate or 0):.2f}%",
        }

    # ── gold: 实际利率方向 + 金价历史分位 ──────────────────────────────────
    def _score_gold(self) -> Dict:
        gold_pct  = self._get_gold_price_percentile()
        rate_signal = self._get_real_rate_signal()  # +1 利好黄金, -1 利空, 0 中性

        # 金价历史分位（反向：价格越低越便宜）
        price_score = _pct_to_score(gold_pct) if gold_pct is not None else 0.5
        # 实际利率信号
        rate_score  = {1: 0.8, 0: 0.5, -1: 0.2}.get(rate_signal, 0.5)

        score = price_score * 0.4 + rate_score * 0.6
        return {
            "score":      round(score, 3),
            "percentile": round(gold_pct, 1) if gold_pct is not None else None,
            "method":     "金价分位+实际利率",
            "detail":     f"金价{gold_pct:.0f}%分位 利率信号={rate_signal:+d}",
        }

    # ── commodity: 商品价格历史分位 ────────────────────────────────────────
    def _score_commodity(self, etf: Dict) -> Dict:
        commodity = etf.get("commodity", "metals")
        pct = self._get_commodity_percentile(commodity)
        if pct is None:
            return {"score": 0.5, "percentile": None, "method": "商品分位", "detail": "无数据"}
        score = _pct_to_score(pct)
        return {
            "score":      round(score, 3),
            "percentile": round(pct, 1),
            "method":     "商品价格分位",
            "detail":     f"{commodity}价格{pct:.0f}%历史分位",
        }

    # ── bond: 收益率分位（反向） ────────────────────────────────────────────
    def _score_bond(self) -> Dict:
        rate_series = self._get_rate_history()
        if rate_series is None or rate_series.empty:
            return {"score": 0.5, "percentile": 50, "method": "收益率分位", "detail": "无数据"}
        cur_rate = rate_series.iloc[-1]
        rate_pct = _safe_percentile(rate_series.values, cur_rate)
        # 收益率越高 = 债券越便宜（反向）
        score = _pct_to_score(100 - rate_pct)
        return {
            "score":      round(score, 3),
            "percentile": round(100 - rate_pct, 1),
            "method":     "10Y国债收益率分位",
            "detail":     f"10Y={cur_rate:.2f}%({rate_pct:.0f}%分位→价格{100-rate_pct:.0f}%分位)",
        }

    # ── 数据获取辅助 ───────────────────────────────────────────────────────
    def _get_index_valuation(self, idx_code: str) -> pd.DataFrame | None:
        if idx_code in self._index_cache:
            return self._index_cache[idx_code]
        try:
            import tushare as ts
            pro = ts.pro_api(TUSHARE_TOKEN)
            start = (datetime.now() - timedelta(days=365 * LOOKBACK_YEARS_EQUITY)).strftime("%Y%m%d")
            df = pro.index_dailybasic(
                ts_code=idx_code, start_date=start,
                fields="trade_date,pe_ttm,pb,dv_ratio"
            )
            if df is None or df.empty:
                return None
            df = df.sort_values("trade_date")
            df["pe_ttm"]   = pd.to_numeric(df["pe_ttm"],   errors="coerce")
            df["pb"]       = pd.to_numeric(df["pb"],       errors="coerce")
            df["dv_ratio"] = pd.to_numeric(df.get("dv_ratio", pd.Series()), errors="coerce")
            df = df.dropna(subset=["pe_ttm", "pb"])
            self._index_cache[idx_code] = df
            return df
        except Exception as e:
            self.logger.warning(f"指数{idx_code}估值获取失败: {e}")
            return None

    def _get_latest_10y_rate(self) -> float | None:
        try:
            import akshare as ak
            df = ak.bond_zh_us_rate(start_date="20240101")
            col = [c for c in df.columns if "中国" in c and "10" in c]
            if col:
                return float(df[col[0]].dropna().iloc[-1])
        except Exception:
            pass
        return None

    def _get_gold_price_percentile(self) -> float | None:
        try:
            import akshare as ak
            df = ak.spot_hist_sge(symbol="Au99.99")
            if df is None or df.empty:
                return None
            df = df.sort_values(df.columns[0]).tail(365 * LOOKBACK_YEARS_COMMODITY)
            price_col = [c for c in df.columns if "价" in c or "price" in c.lower()]
            if not price_col:
                price_col = [df.columns[1]]
            prices = pd.to_numeric(df[price_col[0]], errors="coerce").dropna()
            cur = prices.iloc[-1]
            return _safe_percentile(prices.values, cur)
        except Exception as e:
            self.logger.warning(f"金价分位失败: {e}")
            return None

    def _get_real_rate_signal(self) -> int:
        """实际利率信号：+1利好黄金(利率下行/为负)，-1利空，0中性"""
        try:
            rate = self._get_latest_10y_rate()
            import akshare as ak
            cpi_df = ak.macro_china_cpi_monthly()
            cpi_col = [c for c in cpi_df.columns if "同比" in c]
            if not cpi_col:
                return 0
            cpi = float(pd.to_numeric(cpi_df[cpi_col[0]], errors="coerce").dropna().iloc[-1])
            if rate is None:
                return 0
            real_rate = rate - cpi
            if real_rate < 0:   return  1   # 负实际利率 → 利好黄金
            if real_rate > 2.5: return -1   # 高实际利率 → 利空黄金
            return 0
        except Exception:
            return 0

    def _get_commodity_percentile(self, commodity: str) -> float | None:
        try:
            import akshare as ak
            symbol_map = {
                "metals":    ("铜", ak.futures_main_sina),
                "energy":    ("原油", ak.futures_main_sina),
                "crude_oil": ("原油", ak.futures_main_sina),
            }
            if commodity not in symbol_map:
                return None
            sym, func = symbol_map[commodity]
            df = func(symbol=sym)
            if df is None or df.empty:
                return None
            price_col = [c for c in df.columns if "收盘" in c or "close" in c.lower()]
            if not price_col:
                price_col = [df.columns[1]]
            prices = pd.to_numeric(df[price_col[0]], errors="coerce").dropna().tail(
                250 * LOOKBACK_YEARS_COMMODITY
            )
            return _safe_percentile(prices.values, prices.iloc[-1])
        except Exception as e:
            self.logger.warning(f"商品{commodity}分位失败: {e}")
            return None

    def _get_rate_history(self) -> pd.Series | None:
        if self._rate_cache is not None:
            return self._rate_cache
        try:
            import akshare as ak
            df = ak.bond_zh_us_rate(start_date="20150101")
            col = [c for c in df.columns if "中国" in c and "10" in c]
            if col:
                s = pd.to_numeric(df[col[0]], errors="coerce").dropna()
                self._rate_cache = s
                return s
        except Exception:
            pass
        return None


def _safe_percentile(arr: np.ndarray, value: float) -> float:
    """计算 value 在 arr 中的百分位（0-100）"""
    arr = arr[~np.isnan(arr)]
    if len(arr) == 0:
        return 50.0
    return float(np.mean(arr <= value) * 100)
