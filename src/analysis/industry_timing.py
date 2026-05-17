"""
行业择机：按渗透率阶段 + 经济周期做行业超配/低配建议

- 渗透率：破壁(early_growth) / 高速(mid_growth) / 饱和(mature) / 衰退(decline)
- 周期属性：早周期(early) / 中周期(mid) / 晚周期(late) / 防御(defensive)
- 输出：行业相对强度、当前经济周期阶段、超配/标配/低配建议
"""

import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple

import pandas as pd

from src.utils.config_loader import Config


# 默认渗透率/周期映射（当配置缺失时使用）
DEFAULT_PENETRATION = {
    "电力设备": "mid_growth", "汽车": "mid_growth", "电子": "mid_growth",
    "计算机": "mature", "通信": "early_growth", "医药生物": "mature",
    "食品饮料": "mature", "银行": "mature", "房地产": "decline",
    "煤炭": "late", "石油石化": "late", "有色金属": "late", "钢铁": "late",
}
DEFAULT_CYCLE = {
    "银行": "early", "非银金融": "early", "房地产": "early", "汽车": "early",
    "计算机": "mid", "电子": "mid", "电力设备": "mid", "医药生物": "defensive",
    "食品饮料": "defensive", "煤炭": "late", "有色金属": "late", "钢铁": "late",
}


class IndustryTiming:
    """行业择机：渗透率 + 周期 → 超配/标配/低配建议"""

    def __init__(
        self,
        benchmark: str = None,
        lookback_days: int = None,
        penetration_phase: dict = None,
        cycle_type: dict = None,
    ):
        cfg = Config.get("industry_timing") or {}
        self.benchmark = benchmark or cfg.get("benchmark") or "000300"
        self.lookback_days = lookback_days or cfg.get("lookback_days") or 60
        self.penetration_phase = penetration_phase or cfg.get("penetration_phase") or DEFAULT_PENETRATION
        self.cycle_type = cycle_type or cfg.get("cycle_type") or DEFAULT_CYCLE

    def _industry_list_em(self) -> List[str]:
        """东方财富行业板块名称列表"""
        try:
            import akshare as ak
            df = ak.stock_board_industry_name_em()
            if df is None or df.empty:
                return []
            return df["板块名称"].dropna().astype(str).tolist()
        except Exception as e:
            print(f"[IndustryTiming] 获取行业列表失败: {e}")
            return list(set(self.penetration_phase) | set(self.cycle_type))

    def _benchmark_return(self, end_date: datetime, days: int) -> Optional[float]:
        """沪深300 在 [end_date - days, end_date] 区间涨跌幅（复利）"""
        try:
            import akshare as ak
            end_str = end_date.strftime("%Y%m%d")
            start_date = end_date - timedelta(days=days + 30)
            start_str = start_date.strftime("%Y%m%d")
            df = ak.index_zh_a_hist(symbol=self.benchmark, start_date=start_str, end_date=end_str, period="daily")
            if df is None or len(df) < 2:
                return None
            df = df.sort_values("日期")
            df = df.tail(days + 5)
            if len(df) < 2:
                return None
            first_close = float(df.iloc[0]["收盘"])
            last_close = float(df.iloc[-1]["收盘"])
            if first_close <= 0:
                return None
            return (last_close / first_close) - 1.0
        except Exception as e:
            print(f"[IndustryTiming] 基准收益计算失败: {e}")
            return None

    def _industry_hist_return_em(self, name: str, end_date: datetime, days: int) -> Optional[float]:
        """单行业在 [end_date-days, end_date] 区间涨跌幅"""
        try:
            import akshare as ak
            end_str = end_date.strftime("%Y%m%d")
            start_date = end_date - timedelta(days=days + 30)
            start_str = start_date.strftime("%Y%m%d")
            df = ak.stock_board_industry_hist_em(symbol=name, start_date=start_str, end_date=end_str, period="日k")
            if df is None or len(df) < 2:
                return None
            df = df.sort_values("日期")
            df = df.tail(days + 5)
            if len(df) < 2:
                return None
            first_close = float(df.iloc[0]["收盘"])
            last_close = float(df.iloc[-1]["收盘"])
            if first_close <= 0:
                return None
            return (last_close / first_close) - 1.0
        except Exception:
            return None

    def current_cycle_phase(self, end_date: datetime = None) -> str:
        """
        简单判断当前经济周期阶段：用沪深300的20日/60日趋势。
        - 20日线 > 60日线 且 近期上涨 -> early
        - 20日线 > 60日线 且 近期震荡 -> mid
        - 20日线 < 60日线 -> late 或 defensive（偏弱取 defensive）
        """
        try:
            import akshare as ak
            end_date = end_date or datetime.now()
            end_str = end_date.strftime("%Y%m%d")
            start_date = end_date - timedelta(days=120)
            start_str = start_date.strftime("%Y%m%d")
            df = ak.index_zh_a_hist(symbol=self.benchmark, start_date=start_str, end_date=end_str, period="daily")
            if df is None or len(df) < 60:
                return "mid"
            df = df.sort_values("日期").tail(65)
            df["ma20"] = df["收盘"].astype(float).rolling(20).mean()
            df["ma60"] = df["收盘"].astype(float).rolling(60).mean()
            last = df.iloc[-1]
            ma20 = float(last["ma20"])
            ma60 = float(last["ma60"])
            close = float(last["收盘"])
            ret5 = (close / float(df.iloc[-6]["收盘"]) - 1.0) if len(df) >= 6 else 0
            if ma20 >= ma60 and ret5 > 0.01:
                return "early"
            if ma20 >= ma60:
                return "mid"
            if ma20 < ma60 and ret5 < -0.02:
                return "defensive"
            return "late"
        except Exception as e:
            print(f"[IndustryTiming] 周期阶段判断失败: {e}")
            return "mid"

    def run(
        self,
        end_date: datetime = None,
        max_industries: int = 80,
    ) -> pd.DataFrame:
        """
        执行行业择机：计算各行业相对强度、渗透率阶段、周期属性，并给出建议。

        Returns:
            DataFrame 列: industry, return_pct, benchmark_return_pct, relative_strength,
                         penetration_phase, cycle_type, cycle_match, suggest
        """
        end_date = end_date or datetime.now()
        industries = self._industry_list_em()
        if not industries:
            return pd.DataFrame()
        industries = industries[:max_industries]

        bench_ret = self._benchmark_return(end_date, self.lookback_days)
        if bench_ret is None:
            bench_ret = 0.0

        current_cycle = self.current_cycle_phase(end_date)
        print(f"[IndustryTiming] 当前经济周期阶段: {current_cycle} | 基准{self.lookback_days}日收益: {bench_ret*100:.2f}%")

        rows = []
        for i, name in enumerate(industries):
            ret = self._industry_hist_return_em(name, end_date, self.lookback_days)
            if (i + 1) % 10 == 0:
                time.sleep(0.2)
            if ret is None:
                continue
            rel = ret - bench_ret
            pen = self.penetration_phase.get(name, "mature")
            cyc = self.cycle_type.get(name, "mid")
            # 周期匹配：当前阶段下该行业是否属于占优属性
            cycle_match = (current_cycle == "early" and cyc == "early") or \
                          (current_cycle == "mid" and cyc == "mid") or \
                          (current_cycle == "late" and cyc == "late") or \
                          (current_cycle == "defensive" and cyc == "defensive")
            # 建议：相对强度 + 周期匹配 + 渗透率（成长加分）
            score = rel * 100
            if cycle_match:
                score += 2.0
            if pen in ("early_growth", "mid_growth"):
                score += 0.5
            if pen == "decline":
                score -= 1.0
            if score >= 3:
                suggest = "超配"
            elif score >= 0:
                suggest = "标配"
            else:
                suggest = "低配"
            rows.append({
                "industry": name,
                "return_pct": round(ret * 100, 2),
                "benchmark_return_pct": round(bench_ret * 100, 2),
                "relative_strength": round(rel * 100, 2),
                "penetration_phase": pen,
                "cycle_type": cyc,
                "cycle_match": cycle_match,
                "suggest": suggest,
            })

        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df = df.sort_values("relative_strength", ascending=False).reset_index(drop=True)
        return df

    def run_split(
        self,
        end_date: datetime = None,
        max_industries: int = 80,
        emerging_top: int = 10,
        mature_top: int = 10,
    ) -> Dict[str, Any]:
        """
        新兴行业按渗透率选、成熟行业按生命周期(季/年)选，供日报/看板展示。

        Returns:
            {
                'current_cycle': str,
                'benchmark_return_pct': float,
                'lookback_days' / 'lookback_quarter' / 'lookback_year': int,
                'emerging': DataFrame [industry, penetration_phase, relative_strength, return_pct, ...],
                'mature': DataFrame [industry, cycle_type, cycle_match, quarter_rs, year_rs, ...],
            }
        """
        end_date = end_date or datetime.now()
        cfg = Config.get("industry_timing") or {}
        emerging_list = cfg.get("emerging_industries") or cfg.get("tech_industries") or [
            "电子", "通信", "电力设备", "传媒", "国防军工"
        ]
        lookback_q = cfg.get("lookback_quarter", 65)
        lookback_y = cfg.get("lookback_year", 250)

        full = self.run(end_date=end_date, max_industries=max_industries)
        if full.empty:
            return {
                "current_cycle": self.current_cycle_phase(end_date),
                "benchmark_return_pct": 0.0,
                "lookback_days": self.lookback_days,
                "lookback_quarter": lookback_q,
                "lookback_year": lookback_y,
                "emerging": pd.DataFrame(),
                "mature": pd.DataFrame(),
            }
        bench_pct = full["benchmark_return_pct"].iloc[0] if "benchmark_return_pct" in full.columns else 0.0
        current_cycle = self.current_cycle_phase(end_date)

        # 新兴行业：按渗透率阶段排序（破壁/高速优先），再按相对强度
        emerging_df = full[full["industry"].isin(emerging_list)].copy()
        if not emerging_df.empty:
            pen_order = {"early_growth": 0, "mid_growth": 1, "mature": 2, "late": 3, "decline": 4}
            emerging_df["_pen_order"] = emerging_df["penetration_phase"].map(lambda x: pen_order.get(x, 2))
            emerging_df = emerging_df.sort_values(["_pen_order", "relative_strength"], ascending=[True, False]).drop(columns=["_pen_order"])
            emerging_df = emerging_df.head(emerging_top).reset_index(drop=True)

        # 成熟行业：按生命周期(季/年)算相对强度，再按周期匹配与季度强度排序
        mature_list = full[~full["industry"].isin(emerging_list)]["industry"].unique().tolist()
        mature_rows = []
        bench_q = self._benchmark_return(end_date, lookback_q) or 0.0
        bench_y = self._benchmark_return(end_date, lookback_y) or 0.0
        for i, name in enumerate(mature_list[: max_industries]):
            if (i + 1) % 8 == 0:
                time.sleep(0.15)
            ret_q = self._industry_hist_return_em(name, end_date, lookback_q)
            ret_y = self._industry_hist_return_em(name, end_date, lookback_y)
            if ret_q is None and ret_y is None:
                continue
            row_60 = full[full["industry"] == name].iloc[0] if not full[full["industry"] == name].empty else None
            quarter_rs = (ret_q - bench_q) * 100 if ret_q is not None else None
            year_rs = (ret_y - bench_y) * 100 if ret_y is not None else None
            mature_rows.append({
                "industry": name,
                "cycle_type": row_60.get("cycle_type", "mid") if row_60 is not None else "mid",
                "cycle_match": row_60.get("cycle_match", False) if row_60 is not None else False,
                "quarter_rs": round(quarter_rs, 1) if quarter_rs is not None else None,
                "year_rs": round(year_rs, 1) if year_rs is not None else None,
                "relative_strength": row_60.get("relative_strength") if row_60 is not None else None,
            })
        mature_df = pd.DataFrame(mature_rows)
        if not mature_df.empty:
            mature_df["_q"] = mature_df["quarter_rs"].fillna(-999)
            mature_df = mature_df.sort_values(["cycle_match", "_q"], ascending=[False, False]).drop(columns=["_q"])
            mature_df = mature_df.head(mature_top).reset_index(drop=True)

        return {
            "current_cycle": current_cycle,
            "benchmark_return_pct": float(bench_pct),
            "lookback_days": self.lookback_days,
            "lookback_quarter": lookback_q,
            "lookback_year": lookback_y,
            "emerging": emerging_df,
            "mature": mature_df,
        }
