"""
财务数据拉取器
从 Tushare 获取公司财务三表核心指标，缓存到 financial_data 表。

字段说明（fina_indicator 精简版）：
  roe            - 净资产收益率（%）
  grossprofit_margin - 毛利率（%）
  netprofit_yoy  - 净利润同比增长率（%）
  debt_to_assets - 资产负债率（%）
  op_to_profit   - 经营现金流/净利润（现金质量，>1 优秀，<0.5 警惕）
  revenue_yoy    - 营收同比增长率（%）
  n_income_attr_p- 归母净利润（元）
  ann_date       - 公告日期
"""

import time
import pandas as pd
from datetime import date, timedelta
from typing import Optional

from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class FinancialFetcher:
    """财务数据拉取器（Tushare + 本地缓存）"""

    # 缓存有效期：7天（季报更新频率低，无需每天重拉）
    CACHE_DAYS = 7

    def __init__(self):
        self._pro = None
        self._ensure_table()

    def get(self, ts_code: str, periods: int = 8, force_refresh: bool = False) -> pd.DataFrame:
        """
        获取最近 N 个报告期的财务指标。
        优先用本地缓存，超过 CACHE_DAYS 天则重新从 Tushare 拉取。

        Returns:
            DataFrame，列：end_date, roe, gross_margin, net_profit_yoy,
                           revenue_yoy, debt_ratio, cashflow_quality,
                           net_profit（元），ann_date
            按 end_date 降序排列（最新在前）
        """
        if not force_refresh:
            cached = self._get_cache(ts_code, periods)
            if cached is not None:
                return cached

        # 从 Tushare 拉取
        fresh = self._fetch_tushare(ts_code, periods)
        if fresh is not None and not fresh.empty:
            self._save_cache(ts_code, fresh)
            return fresh.head(periods)

        # 兜底：从现有 stock_daily / stock_info 构造简化版
        return self._fallback(ts_code)

    def get_summary(self, ts_code: str) -> dict:
        """
        返回财务摘要字典，供 LLM 提示词使用。
        包括：最新期数据 + 趋势描述
        """
        df = self.get(ts_code)
        if df.empty:
            return {"available": False, "note": "暂无财务数据"}

        latest = df.iloc[0].to_dict()
        # 计算 ROE/净利润增速的趋势
        roe_trend = self._calc_trend(df, "roe", 4)
        profit_trend = self._calc_trend(df, "net_profit_yoy", 4)
        margin_trend = self._calc_trend(df, "gross_margin", 4)

        return {
            "available": True,
            "report_date": str(latest.get("end_date", "")),
            # 最新期核心指标
            "roe": self._fmt(latest.get("roe"), "%"),
            "gross_margin": self._fmt(latest.get("gross_margin"), "%"),
            "net_profit_yoy": self._fmt(latest.get("net_profit_yoy"), "%"),
            "revenue_yoy": self._fmt(latest.get("revenue_yoy"), "%"),
            "debt_ratio": self._fmt(latest.get("debt_ratio"), "%"),
            "cashflow_quality": self._fmt(latest.get("cashflow_quality")),
            # 趋势
            "roe_trend": roe_trend,
            "profit_trend": profit_trend,
            "margin_trend": margin_trend,
            # 原始近4期数据（供LLM参考）
            "recent_roe": self._series_str(df, "roe", 4),
            "recent_profit_growth": self._series_str(df, "net_profit_yoy", 4),
            "recent_revenue_growth": self._series_str(df, "revenue_yoy", 4),
        }

    # ──────────────────────────────────────────────
    # Tushare 拉取
    # ──────────────────────────────────────────────

    def _fetch_tushare(self, ts_code: str, periods: int) -> Optional[pd.DataFrame]:
        try:
            pro = self._get_pro()
            if pro is None:
                return None

            # fina_indicator 核心财务指标
            df = pro.fina_indicator(
                ts_code=ts_code,
                fields=(
                    "ts_code,ann_date,end_date,"
                    "roe,grossprofit_margin,netprofit_yoy,debt_to_assets,"
                    "op_to_profit,revenue_yoy,n_income_attr_p"
                ),
            )
            if df is None or df.empty:
                return None

            # 字段重命名
            df = df.rename(columns={
                "grossprofit_margin": "gross_margin",
                "netprofit_yoy":      "net_profit_yoy",
                "debt_to_assets":     "debt_ratio",
                "op_to_profit":       "cashflow_quality",
                "n_income_attr_p":    "net_profit",
            })

            # 转数值
            for col in ["roe", "gross_margin", "net_profit_yoy", "revenue_yoy",
                        "debt_ratio", "cashflow_quality", "net_profit"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.sort_values("end_date", ascending=False).head(periods * 2)
            return df

        except Exception as e:
            print(f"[FinancialFetcher] Tushare 拉取失败 {ts_code}: {e}")
            return None

    def _get_pro(self):
        if self._pro is not None:
            return self._pro
        try:
            import tushare as ts
            token = Config.tushare_token
            if not token:
                return None
            ts.set_token(token)
            self._pro = ts.pro_api()
            return self._pro
        except Exception:
            return None

    # ──────────────────────────────────────────────
    # 缓存管理
    # ──────────────────────────────────────────────

    def _get_cache(self, ts_code: str, periods: int) -> Optional[pd.DataFrame]:
        threshold = (date.today() - timedelta(days=self.CACHE_DAYS)).isoformat()
        df = DBUtils.query_df(
            """SELECT * FROM financial_data
               WHERE ts_code = ? AND fetched_date >= ?
               ORDER BY end_date DESC LIMIT ?""",
            params=[ts_code, threshold, periods],
        )
        return df if not df.empty else None

    def _save_cache(self, ts_code: str, df: pd.DataFrame):
        today = date.today().isoformat()
        # 删除旧缓存后批量插入
        DBUtils.execute(
            "DELETE FROM financial_data WHERE ts_code = ?", params=[ts_code]
        )
        for _, row in df.iterrows():
            try:
                DBUtils.execute(
                    """INSERT INTO financial_data
                (ts_code, end_date, ann_date, roe, gross_margin, net_profit_yoy,
                         revenue_yoy, debt_ratio, cashflow_quality, net_profit, fetched_date)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON DUPLICATE KEY UPDATE
                       ann_date=VALUES(ann_date), roe=VALUES(roe),
                       gross_margin=VALUES(gross_margin), net_profit_yoy=VALUES(net_profit_yoy),
                       revenue_yoy=VALUES(revenue_yoy), debt_ratio=VALUES(debt_ratio),
                       cashflow_quality=VALUES(cashflow_quality),
                       net_profit=VALUES(net_profit), fetched_date=VALUES(fetched_date)""",
                    params=[
                        ts_code,
                        str(row.get("end_date", "")),
                        str(row.get("ann_date", "")),
                        self._safe_float(row.get("roe")),
                        self._safe_float(row.get("gross_margin")),
                        self._safe_float(row.get("net_profit_yoy")),
                        self._safe_float(row.get("revenue_yoy")),
                        self._safe_float(row.get("debt_ratio")),
                        self._safe_float(row.get("cashflow_quality")),
                        self._safe_float(row.get("net_profit")),
                        today,
                    ],
                )
            except Exception:
                pass

    def _fallback(self, ts_code: str) -> pd.DataFrame:
        """从 stock_daily 拿最新 PE/PB 构造一行简化数据（无财报时的保底）"""
        # stock_daily 无 pb 列，需 JOIN stock_info 补充
        df = DBUtils.query_df(
            """SELECT sd.trade_date as end_date, sd.pe_ttm, si.pb
               FROM stock_daily sd
               LEFT JOIN stock_info si ON sd.ts_code = si.ts_code
               WHERE sd.ts_code = ?
               ORDER BY sd.trade_date DESC LIMIT 1""",
            params=[ts_code],
        )
        if df.empty:
            return pd.DataFrame()
        # 补充空列
        for col in ["roe", "gross_margin", "net_profit_yoy", "revenue_yoy",
                    "debt_ratio", "cashflow_quality", "net_profit", "ann_date"]:
            df[col] = None
        return df

    # ──────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────

    def _calc_trend(self, df: pd.DataFrame, col: str, n: int) -> str:
        if col not in df.columns or len(df) < 2:
            return "数据不足"
        series = pd.to_numeric(df[col].head(n), errors="coerce").dropna()
        if len(series) < 2:
            return "数据不足"
        # 简单线性趋势：后半均值 vs 前半均值
        half = len(series) // 2
        recent_avg = series.iloc[:half].mean()
        older_avg = series.iloc[half:].mean()
        delta = recent_avg - older_avg
        if delta > 2:   return f"上升（近{n}期均值{recent_avg:.1f}%）"
        if delta < -2:  return f"下降（近{n}期均值{recent_avg:.1f}%）"
        return f"平稳（近{n}期均值{recent_avg:.1f}%）"

    def _series_str(self, df: pd.DataFrame, col: str, n: int) -> str:
        if col not in df.columns:
            return "N/A"
        vals = pd.to_numeric(df[col].head(n), errors="coerce")
        dates = df["end_date"].head(n).astype(str).str[:7]  # YYYY-MM
        parts = [f"{d}:{v:.1f}%" if pd.notna(v) else f"{d}:N/A"
                 for d, v in zip(dates, vals)]
        return "  ".join(parts)

    @staticmethod
    def _fmt(val, suffix="") -> str:
        if val is None or pd.isna(val):
            return "N/A"
        return f"{val:.1f}{suffix}"

    @staticmethod
    def _safe_float(val):
        try:
            v = float(val)
            return None if pd.isna(v) else v
        except Exception:
            return None

    def _ensure_table(self):
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS financial_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code VARCHAR(20) NOT NULL,
                end_date VARCHAR(10),
                ann_date VARCHAR(10),
                roe FLOAT,
                gross_margin FLOAT,
                net_profit_yoy FLOAT,
                revenue_yoy FLOAT,
                debt_ratio FLOAT,
                cashflow_quality FLOAT,
                net_profit FLOAT,
                fetched_date DATE,
                UNIQUE KEY uniq_code_date (ts_code, end_date)
            )
        """)
