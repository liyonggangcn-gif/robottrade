"""
股票池健康度检查——绿灯/黄灯/红灯评级 + 风险提示

评级标准
--------
红灯 (red)   ：存在重大风险，建议移除或暂停关注
黄灯 (yellow)：存在隐患，需持续跟踪，酌情处理
绿灯 (green) ：基本面健康，无重大风险信号

特殊处理
--------
- 科创板(688)和科技类成长型公司：若经营现金流为负但研发占比高，单独标注，可放宽

数据来源
--------
- fina_indicator 批量拉取（50只/次，~30次API），覆盖 debt_to_assets
- 本地 stock_info：ST名称判断、PE/PB
- 本地 stock_daily：ROE、netprofit_yoy（最新交易日）
- 结果当日缓存，次日失效
"""

import json
import time
from datetime import date, datetime
from typing import Optional

import pandas as pd
import tushare as ts

from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils

LIGHT_RED    = "red"
LIGHT_YELLOW = "yellow"
LIGHT_GREEN  = "green"

STAR_PREFIX    = "688"
ANNUAL_PERIOD  = "20241231"
FINA_BATCH_SZ  = 50   # fina_indicator 每批数量


class StockHealthChecker:
    """股票池健康度检查器"""

    def __init__(self):
        self.pro = ts.pro_api(Config.get("tushare_token"))
        self._ensure_cache_table()

    # ─────────────────────────────────────────────────────────────────────────
    # 公开接口
    # ─────────────────────────────────────────────────────────────────────────

    def check_pool(self, force_refresh: bool = False) -> pd.DataFrame:
        """检查全股票池，返回带评级的 DataFrame。"""
        if not force_refresh:
            cached = self._load_summary_cache()
            if cached is not None:
                return cached

        pool_df = DBUtils.query_df(
            "SELECT ts_code, company_name, company_type, tier FROM stock_pool WHERE is_active = 1"
        )
        if pool_df.empty:
            return pd.DataFrame()

        codes = pool_df["ts_code"].tolist()
        print(f"[健康检查] 共 {len(codes)} 只，开始拉取数据...")

        # ── 1. 本地数据（ST名称 / PE / PB / ROE / YoY）────────────────────
        local = self._fetch_local(codes)
        # ── 2. Tushare 批量拉财务指标（债务/资产负债率）─────────────────────
        fina  = self._batch_fina(codes)

        print("[健康检查] 数据获取完成，开始评级...")

        results = []
        for _, row in pool_df.iterrows():
            code = row["ts_code"]
            loc  = local.get(code, {})
            fin  = fina.get(code, {})
            res  = self._evaluate(
                ts_code      = code,
                company_name = row["company_name"],
                company_type = row["company_type"],
                tier         = row["tier"],
                local        = loc,
                fina         = fin,
            )
            results.append(res)

        df = pd.DataFrame(results)
        order = {LIGHT_RED: 0, LIGHT_YELLOW: 1, LIGHT_GREEN: 2}
        df["_o"] = df["light"].map(order)
        df = df.sort_values(["_o", "company_name"]).drop(columns=["_o"]).reset_index(drop=True)

        self._save_summary_cache(df)
        print(f"[健康检查] 完成: 红灯{(df['light']=='red').sum()} / 黄灯{(df['light']=='yellow').sum()} / 绿灯{(df['light']=='green').sum()}")
        return df

    # ─────────────────────────────────────────────────────────────────────────
    # 评级逻辑
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate(self, ts_code, company_name, company_type, tier, local, fina) -> dict:
        red_reasons    = []
        yellow_reasons = []
        risk_tags      = []
        notes          = []

        is_star = ts_code.startswith(STAR_PREFIX)
        is_tech = company_type == "growth" or is_star
        is_fin  = company_type == "rate_sensitive"   # 银行/保险：忽略负债率阈值

        # ── 辅助值 ─────────────────────────────────────────────────────────
        is_st       = local.get("is_st", False)
        pb          = self._f(local.get("pb"))
        pe          = self._f(local.get("pe_ttm"))
        # 优先用 fina_indicator（年报），其次用 stock_daily（最新日数据）
        roe         = self._f(fina.get("roe"))         or self._f(local.get("roe"))
        profit_yoy  = self._f(fina.get("netprofit_yoy")) or self._f(local.get("netprofit_yoy"))
        rev_yoy     = self._f(fina.get("or_yoy"))
        debt_ratio  = self._f(fina.get("debt_to_assets"))

        # ── 红灯 ────────────────────────────────────────────────────────────

        if is_st:
            red_reasons.append("ST/*ST 风险警示")
            risk_tags.append("ST")

        if pb is not None and pb < 0:
            red_reasons.append("净资产为负（PB<0）")
            risk_tags.append("资不抵债")

        if debt_ratio is not None and not is_fin:
            if debt_ratio > 85:
                red_reasons.append(f"资产负债率极高（{debt_ratio:.1f}%）")
                risk_tags.append("高负债")
            elif debt_ratio > 70:
                yellow_reasons.append(f"资产负债率偏高（{debt_ratio:.1f}%）")

        if roe is not None:
            if roe < -5:
                red_reasons.append(f"ROE 严重亏损（{roe:.1f}%）")
                risk_tags.append("持续亏损")
            elif roe < 0:
                yellow_reasons.append(f"ROE 为负（{roe:.1f}%）")
            elif roe < 5:
                yellow_reasons.append(f"ROE 偏低（{roe:.1f}%）")

        # ── 黄灯 ────────────────────────────────────────────────────────────

        if profit_yoy is not None:
            if profit_yoy < -50:
                yellow_reasons.append(f"净利润大幅下滑（{profit_yoy:.1f}%）")
            elif profit_yoy < -20:
                yellow_reasons.append(f"净利润下滑（{profit_yoy:.1f}%）")

        if rev_yoy is not None:
            if rev_yoy < -20:
                yellow_reasons.append(f"营收大幅下滑（{rev_yoy:.1f}%）")
            elif rev_yoy < -5:
                yellow_reasons.append(f"营收小幅下滑（{rev_yoy:.1f}%）")

        if pe is not None and pe > 0:
            if pe >= 200:
                yellow_reasons.append(f"估值极高（PE={pe:.1f}）")
            elif pe > 80:
                yellow_reasons.append(f"估值偏高（PE={pe:.1f}）")

        # 科创板/科技公司备注
        if is_tech:
            notes.append("科创/科技类：现金流指标如为负需结合研发投入判断，可适当放宽")

        # ── 综合评级 ────────────────────────────────────────────────────────
        if red_reasons:
            light       = LIGHT_RED
            main_reason = "；".join(red_reasons)
        elif yellow_reasons:
            light       = LIGHT_YELLOW
            main_reason = "；".join(yellow_reasons)
        else:
            light       = LIGHT_GREEN
            main_reason = "基本面健康"

        return {
            "ts_code":       ts_code,
            "company_name":  company_name,
            "company_type":  company_type,
            "tier":          tier,
            "light":         light,
            "main_reason":   main_reason,
            "risk_tags":     "，".join(risk_tags) if risk_tags else "",
            "notes":         "；".join(notes) if notes else "",
            "roe":           self._pct(roe),
            "debt_ratio":    self._pct(debt_ratio),
            "netprofit_yoy": self._pct(profit_yoy),
            "or_yoy":        self._pct(rev_yoy),
            "pe_ttm":        round(pe, 1) if pe is not None else None,
            "pb":            round(pb, 2) if pb is not None else None,
            "is_st":         is_st,
            "data_period":   ANNUAL_PERIOD,
            "checked_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # 数据获取
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_local(self, codes: list) -> dict:
        """
        从本地 DB 获取：
          - stock_info: name（判断ST）、pe_ttm、pb
          - stock_daily: roe、netprofit_yoy（最新交易日）
        """
        result = {c: {} for c in codes}

        # stock_info
        try:
            df = DBUtils.query_df(
                "SELECT ts_code, name, pe_ttm, pb FROM stock_info"
            )
            for _, r in df.iterrows():
                c = r["ts_code"]
                if c in result:
                    result[c]["pe_ttm"] = r["pe_ttm"]
                    result[c]["pb"]     = r["pb"]
                    result[c]["is_st"]  = bool("ST" in str(r["name"]))
        except Exception as e:
            print(f"[健康检查] stock_info 查询失败: {e}")

        # stock_daily（最新交易日）
        try:
            df = DBUtils.query_df(
                """
                SELECT sd.ts_code, sd.roe, sd.netprofit_yoy
                FROM stock_daily sd
                INNER JOIN (
                    SELECT ts_code, MAX(trade_date) AS max_date
                    FROM stock_daily GROUP BY ts_code
                ) t ON sd.ts_code = t.ts_code AND sd.trade_date = t.max_date
                """
            )
            for _, r in df.iterrows():
                c = r["ts_code"]
                if c in result:
                    result[c]["roe"]          = r["roe"]
                    result[c]["netprofit_yoy"] = r["netprofit_yoy"]
        except Exception as e:
            print(f"[健康检查] stock_daily 查询失败: {e}")

        return result

    def _batch_fina(self, codes: list) -> dict:
        """
        批量拉取 fina_indicator（50只/批），获取年报债务/资产负债率/ROE/YoY。
        """
        fields = "ts_code,roe,debt_to_assets,netprofit_yoy,or_yoy"
        result = {}
        batches = [codes[i:i+FINA_BATCH_SZ] for i in range(0, len(codes), FINA_BATCH_SZ)]
        print(f"[健康检查] fina_indicator: {len(batches)} 批次...")
        for i, batch in enumerate(batches):
            ts_str = ",".join(batch)
            try:
                df = self.pro.fina_indicator(ts_code=ts_str, period=ANNUAL_PERIOD, fields=fields)
                for _, r in df.iterrows():
                    result[r["ts_code"]] = r.to_dict()
            except Exception as e:
                print(f"[健康检查] 批次 {i+1} 失败: {e}")
            time.sleep(0.15)
            if (i + 1) % 10 == 0:
                print(f"[健康检查]   进度 {i+1}/{len(batches)}")
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 缓存（当日结果存 DB）
    # ─────────────────────────────────────────────────────────────────────────

    def _ensure_cache_table(self):
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS health_check_cache (
                cache_date DATE NOT NULL,
                result_json MEDIUMTEXT,
                PRIMARY KEY (cache_date)
            )
        """)
        # 若列类型仍为 TEXT（旧版建表），升级为 MEDIUMTEXT
        try:
            DBUtils.execute(
                "ALTER TABLE health_check_cache MODIFY COLUMN result_json MEDIUMTEXT"
            )
        except Exception:
            pass  # SQLite 不支持 ALTER MODIFY，忽略

    def _load_summary_cache(self) -> Optional[pd.DataFrame]:
        today = date.today().isoformat()
        try:
            df = DBUtils.query_df(
                "SELECT result_json FROM health_check_cache WHERE cache_date = ?",
                params=[today],
            )
            if df.empty:
                return None
            data = json.loads(df.iloc[0]["result_json"])
            return pd.DataFrame(data)
        except Exception:
            return None

    def _save_summary_cache(self, df: pd.DataFrame):
        today = date.today().isoformat()
        # 序列化时用 ascii-safe，避免 MySQL 字符集问题
        payload = json.dumps(df.to_dict("records"), ensure_ascii=True)
        try:
            DBUtils.execute(
                "DELETE FROM health_check_cache WHERE cache_date = ?", params=[today]
            )
            DBUtils.execute(
                "INSERT INTO health_check_cache (cache_date, result_json) VALUES (?, ?)",
                params=[today, payload],
            )
            print(f"[健康检查] 结果已缓存（{today}）")
        except Exception as e:
            print(f"[健康检查] 缓存保存失败: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # 工具
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _f(val) -> Optional[float]:
        if val is None or (val != val):
            return None
        try:
            f = float(val)
            return None if f != f else f
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _pct(val: Optional[float]) -> str:
        return f"{val:.1f}%" if val is not None else ""
