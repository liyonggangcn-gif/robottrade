"""
估值引擎
每日静默更新股票池内所有公司的估值信号，按公司类型选择对应估值方法。

核心逻辑：
  - 历史分位：当前PE/PB在过去N年中有多便宜（分位越低越便宜）
  - PEG：PE / 净利润增速（< 1 低估，> 1.5 高估）
  - 股息率：年化股息 / 当前股价（稳定现金流型核心指标）
  - 安全边际：(目标价 - 当前价) / 目标价
"""

import math
import pandas as pd
import numpy as np
from datetime import date, timedelta
from typing import Optional

from src.utils.db_utils import DBUtils
from src.classifier.company_classifier import CompanyClassifier


def _to_db(v):
    """将 nan/inf 转为 None，确保 MySQL FLOAT 列可接受"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


# 各类型的估值"便宜"阈值（分位低于此值 → cheap）
_CHEAP_THRESHOLDS = {
    "resource":       {"metric": "pb_percentile",  "cheap": 25, "expensive": 75},
    "brand":          {"metric": "pe_percentile",  "cheap": 25, "expensive": 75},
    "growth":         {"metric": "peg",            "cheap": 1.0, "expensive": 1.5},
    "rate_sensitive": {"metric": "pb_percentile",  "cheap": 30, "expensive": 70},
    "policy":         {"metric": "pe_percentile",  "cheap": 30, "expensive": 75},
    "cashflow":       {"metric": "dividend_yield", "cheap": 4.0, "expensive": 2.5},  # 股息率反向：高=便宜
}


class ValuationEngine:
    """估值引擎：按公司类型计算估值信号，存入 valuation_history 表"""

    def __init__(self):
        self.classifier = CompanyClassifier()
        self._ensure_table()

    def update_pool(self, ts_codes: Optional[list] = None):
        """
        更新估值历史。
        ts_codes=None 时更新 stock_pool 中所有活跃股票。
        """
        if ts_codes is None:
            df = DBUtils.query_df(
                "SELECT ts_code, company_type FROM stock_pool WHERE is_active = 1"
            )
            if df.empty:
                return
            ts_codes = df["ts_code"].tolist()
            type_map = dict(zip(df["ts_code"], df["company_type"]))
        else:
            type_map = self.classifier.classify_batch(ts_codes)

        today = date.today().isoformat()
        updated = 0
        for ts_code in ts_codes:
            try:
                company_type = type_map.get(ts_code, "growth")
                result = self.compute(ts_code, company_type)
                if result:
                    self._save(ts_code, today, result)
                    updated += 1
            except Exception as e:
                print(f"[ValuationEngine] {ts_code} 更新失败: {e}")

        print(f"[ValuationEngine] 已更新 {updated}/{len(ts_codes)} 只股票的估值")

    def compute(self, ts_code: str, company_type: str) -> Optional[dict]:
        """计算单只股票的估值指标"""
        # 拉取最近5年日线数据（stock_daily 无 pb 列）
        df = DBUtils.query_df(
            """SELECT trade_date, close, pe_ttm, total_mv
               FROM stock_daily
               WHERE ts_code = ?
               ORDER BY trade_date DESC
               LIMIT 1500""",
            params=[ts_code],
        )
        if df.empty or len(df) < 60:
            return None

        latest = df.iloc[0]
        pe_ttm = latest.get("pe_ttm")

        # pb 来自 stock_info（快照，无历史时间序列）
        info = DBUtils.query_df(
            "SELECT pb FROM stock_info WHERE ts_code = ?", params=[ts_code]
        )
        pb = float(info["pb"].iloc[0]) if not info.empty and info["pb"].iloc[0] is not None else None

        # PE 历史分位（过去5年，约1250个交易日）
        pe_series = pd.to_numeric(df["pe_ttm"], errors="coerce").dropna()
        pe_percentile = self._percentile(pe_ttm, pe_series) if pe_ttm and pe_ttm > 0 else None
        # PB 无历史时间序列，暂不计算分位
        pb_percentile = None

        # 净利润增速（最近4个季度同比，用于PEG）
        growth_rate = self._get_earnings_growth(ts_code)
        peg = None
        if pe_ttm and pe_ttm > 0 and growth_rate and growth_rate > 0:
            peg = round(pe_ttm / growth_rate, 2)

        # 股息率（稳定现金流型）
        dividend_yield = self._get_dividend_yield(ts_code, latest.get("total_mv"))

        # 估值信号
        signal = self._get_signal(company_type, pe_percentile, pb_percentile, peg, dividend_yield, pb)

        # 简单目标价：使用历史中位PE × 预期EPS（粗略估计）
        target_price_mid = self._estimate_target_price(ts_code, pe_series, latest.get("close"))
        current_price = latest.get("close")
        safety_margin = None
        if target_price_mid and current_price and current_price > 0:
            safety_margin = round((target_price_mid - current_price) / target_price_mid, 4)

        return {
            "pe_ttm": pe_ttm,
            "pb": pb,
            "pe_percentile_5y": pe_percentile,
            "pb_percentile_5y": pb_percentile,
            "peg": peg,
            "dividend_yield": dividend_yield,
            "safety_margin": safety_margin,
            "target_price_mid": target_price_mid,
            "valuation_signal": signal,   # cheap / fair / expensive / unknown
        }

    def get_latest(self, ts_code: str) -> Optional[dict]:
        """获取最新一条估值记录"""
        df = DBUtils.query_df(
            """SELECT * FROM valuation_history
               WHERE ts_code = ?
               ORDER BY trade_date DESC LIMIT 1""",
            params=[ts_code],
        )
        if df.empty:
            return None
        return df.iloc[0].to_dict()

    # ──────────────────────────────────────────────
    # 内部辅助方法
    # ──────────────────────────────────────────────

    def _percentile(self, current_value, series: pd.Series) -> Optional[float]:
        """计算 current_value 在 series 中的百分位（0-100）"""
        if current_value is None or series.empty:
            return None
        # 过滤极端值（PE > 300 或 < 0 视为无效）
        clean = series[(series > 0) & (series < 300)]
        if len(clean) < 30:
            return None
        pct = float((clean < current_value).sum()) / len(clean) * 100
        return round(pct, 1)

    def _get_earnings_growth(self, ts_code: str) -> Optional[float]:
        """从 stock_daily 拿最近4季净利润同比增速（近似用ROE_YOY或基本面增速）"""
        try:
            # 尝试从 stock_factors 取 roe_yoy（已有因子）
            df = DBUtils.query_df(
                """SELECT trade_date, roe_yoy FROM stock_factors
                   WHERE ts_code = ?
                   ORDER BY trade_date DESC LIMIT 1""",
                params=[ts_code],
            )
            if not df.empty and df["roe_yoy"].iloc[0] is not None:
                return float(df["roe_yoy"].iloc[0])
        except Exception:
            pass
        return None

    def _get_dividend_yield(self, ts_code: str, total_mv) -> Optional[float]:
        """估算股息率（%）：暂用 stock_info 中 PE 倒数 × 假设分红率 30%"""
        # TODO: 接入 Tushare dividend 接口后替换为真实数据
        try:
            df = DBUtils.query_df(
                "SELECT pe_ttm FROM stock_info WHERE ts_code = ?",
                params=[ts_code],
            )
            if not df.empty:
                pe = df["pe_ttm"].iloc[0]
                if pe and pe > 0:
                    # 粗略估算：股息率 ≈ 1/PE × 分红率(30%)
                    return round(100 / pe * 0.3, 2)
        except Exception:
            pass
        return None

    def _estimate_target_price(self, ts_code: str, pe_series: pd.Series, current_price) -> Optional[float]:
        """
        简单目标价：历史 PE 中位数 × 当前 EPS。
        后续接入分析师预期 EPS 后可替换。
        """
        if pe_series.empty or not current_price:
            return None
        clean = pe_series[(pe_series > 0) & (pe_series < 300)]
        if len(clean) < 60:
            return None
        median_pe = clean.median()
        # 当前EPS = 当前价 / 当前PE（逆推）
        latest_pe = pe_series.iloc[0] if pe_series.iloc[0] > 0 else None
        if not latest_pe:
            return None
        current_eps = current_price / latest_pe
        return round(median_pe * current_eps, 2)

    def _get_signal(self, company_type, pe_pct, pb_pct, peg, dividend_yield, pb=None) -> str:
        """根据公司类型和估值指标判断 cheap/fair/expensive/unknown"""
        cfg = _CHEAP_THRESHOLDS.get(company_type, _CHEAP_THRESHOLDS["growth"])
        metric = cfg["metric"]

        if metric == "pe_percentile" and pe_pct is not None:
            if pe_pct <= cfg["cheap"]:    return "cheap"
            if pe_pct >= cfg["expensive"]: return "expensive"
            return "fair"

        if metric == "pb_percentile":
            if pb_pct is not None:
                if pb_pct <= cfg["cheap"]:    return "cheap"
                if pb_pct >= cfg["expensive"]: return "expensive"
                return "fair"
            # pb_percentile 无历史序列时，用 PB 绝对值兜底
            if pb is not None and pb > 0:
                # rate_sensitive(银行): PB<0.8 cheap, PB>1.2 expensive
                # resource(资源): PB<1.0 cheap, PB>2.5 expensive
                if company_type == "rate_sensitive":
                    if pb < 0.8:  return "cheap"
                    if pb > 1.2:  return "expensive"
                    return "fair"
                else:  # resource
                    if pb < 1.0:  return "cheap"
                    if pb > 2.5:  return "expensive"
                    return "fair"

        if metric == "peg" and peg is not None:
            if peg <= cfg["cheap"]:    return "cheap"
            if peg >= cfg["expensive"]: return "expensive"
            return "fair"

        if metric == "dividend_yield" and dividend_yield is not None:
            if dividend_yield >= cfg["cheap"]:    return "cheap"
            if dividend_yield <= cfg["expensive"]: return "expensive"
            return "fair"

        return "unknown"

    def _save(self, ts_code: str, trade_date: str, data: dict):
        # 先删除当天旧记录（同时兼容 SQLite 和 MySQL）
        DBUtils.execute(
            "DELETE FROM valuation_history WHERE ts_code = ? AND trade_date = ?",
            params=[ts_code, trade_date],
        )
        DBUtils.execute(
            """INSERT INTO valuation_history
               (ts_code, trade_date, pe_ttm, pb, pe_percentile_5y, pb_percentile_5y,
                peg, dividend_yield, safety_margin, target_price_mid, valuation_signal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            params=[
                ts_code, trade_date,
                _to_db(data.get("pe_ttm")), _to_db(data.get("pb")),
                _to_db(data.get("pe_percentile_5y")), _to_db(data.get("pb_percentile_5y")),
                _to_db(data.get("peg")), _to_db(data.get("dividend_yield")),
                _to_db(data.get("safety_margin")), _to_db(data.get("target_price_mid")),
                data.get("valuation_signal"),
            ]
        )

    def _ensure_table(self):
        # SQLite 不支持 UNIQUE KEY 语法，需分两步
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS valuation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code VARCHAR(20) NOT NULL,
                trade_date DATE NOT NULL,
                pe_ttm FLOAT,
                pb FLOAT,
                pe_percentile_5y FLOAT,
                pb_percentile_5y FLOAT,
                peg FLOAT,
                dividend_yield FLOAT,
                safety_margin FLOAT,
                target_price_mid FLOAT,
                valuation_signal VARCHAR(20)
            )
        """)
        # 为 SQLite 添加唯一索引（MySQL 建表时通过 _convert_sql 已处理）
        try:
            DBUtils.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_val ON valuation_history (ts_code, trade_date)"
            )
        except Exception:
            pass  # MySQL 模式下此语句会失败，忽略即可
