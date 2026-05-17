"""
PoolStrategy - 股票池内每日买入信号扫描

只在 stock_pool 的 watch/reserve 层中扫描，
对每只股票按公司类型判断是否进入买入区，
输出今日有信号的候选列表（已按类型分组）。

用法：
    strategy = PoolStrategy()
    result = strategy.run()
    # result: {
    #   "signals": DataFrame（今日有买入信号的股票）,
    #   "by_type": {type_name: [rows...]},
    #   "all_pool": DataFrame（全池估值快照）
    # }
"""

import pandas as pd
from datetime import date, datetime
from typing import Optional

from src.utils.db_utils import DBUtils
from src.classifier.company_classifier import CompanyClassifier, TYPE_META
from src.valuation.valuation_engine import ValuationEngine


# 各类型的买入区条件（对应 valuation_history 字段）
# 说明：pb_percentile_5y 因历史数据暂缺，利率敏感型和资源型直接用绝对值判断
_BUY_CONDITIONS = {
    "resource":       lambda r: r.get("pb_percentile_5y") is not None and r["pb_percentile_5y"] <= 25
                                or (r.get("pb") is not None and r["pb"] < 1.0),
    "brand":          lambda r: r.get("pe_percentile_5y") is not None and r["pe_percentile_5y"] <= 25,
    "growth":         lambda r: r.get("peg") is not None and 0 < r["peg"] <= 1.0,
    "rate_sensitive": lambda r: r.get("pb") is not None and r["pb"] < 0.8,   # PB<0.8 对银行为明显低估
    "policy":         lambda r: r.get("pe_percentile_5y") is not None and r["pe_percentile_5y"] <= 30,
    "cashflow":       lambda r: r.get("dividend_yield") is not None and r["dividend_yield"] >= 4.0,
}

# 各类型"快进入买入区"的提示
_APPROACHING_CONDITIONS = {
    "resource":       lambda r: r.get("pb_percentile_5y") is not None and r["pb_percentile_5y"] <= 35
                                or (r.get("pb") is not None and 1.0 <= r["pb"] < 1.3),
    "brand":          lambda r: r.get("pe_percentile_5y") is not None and r["pe_percentile_5y"] <= 35,
    "growth":         lambda r: r.get("peg") is not None and 0 < r["peg"] <= 1.3,
    "rate_sensitive": lambda r: r.get("pb") is not None and 0.8 <= r["pb"] < 1.0,
    "policy":         lambda r: r.get("pe_percentile_5y") is not None and r["pe_percentile_5y"] <= 40,
    "cashflow":       lambda r: r.get("dividend_yield") is not None and r["dividend_yield"] >= 3.5,
}


class PoolStrategy:
    """股票池买入信号扫描策略"""

    def __init__(self):
        self.classifier = CompanyClassifier()
        self.valuation_engine = ValuationEngine()

    def run(self, update_valuation: bool = True) -> dict:
        """
        扫描股票池，返回今日信号。

        Args:
            update_valuation: 是否先更新估值（日常调用传 True；调试/测试传 False 节省时间）

        Returns:
            dict with keys:
              signals    - DataFrame，今日买入信号股票
              approaching- DataFrame，接近买入区股票
              all_pool   - DataFrame，全池最新估值快照
              summary    - str，供推送用的文字摘要
        """
        print(f"[PoolStrategy] 开始扫描股票池 {date.today()}")

        # 1. 先更新估值
        if update_valuation:
            print("[PoolStrategy] 更新池内估值...")
            self.valuation_engine.update_pool()

        # 2. 拉取 watch + reserve 层的股票 + 最新估值
        pool_df = self._get_pool_with_valuation()
        if pool_df.empty:
            print("[PoolStrategy] 股票池为空，跳过")
            return {"signals": pd.DataFrame(), "approaching": pd.DataFrame(),
                    "all_pool": pd.DataFrame(), "summary": "股票池为空"}

        print(f"[PoolStrategy] 扫描 {len(pool_df)} 只池内股票...")

        # 3. 判断买入信号
        signals = []
        approaching = []

        for _, row in pool_df.iterrows():
            r = row.to_dict()
            ctype = r.get("company_type", "growth")

            buy_fn = _BUY_CONDITIONS.get(ctype)
            approach_fn = _APPROACHING_CONDITIONS.get(ctype)

            if buy_fn and buy_fn(r):
                r["signal"] = "buy"
                r["signal_reason"] = self._build_reason(r, ctype)
                signals.append(r)
            elif approach_fn and approach_fn(r):
                r["signal"] = "approaching"
                r["signal_reason"] = self._build_reason(r, ctype)
                approaching.append(r)

        signals_df = pd.DataFrame(signals) if signals else pd.DataFrame()
        approaching_df = pd.DataFrame(approaching) if approaching else pd.DataFrame()

        # 过滤连续大涨股（山顶风险），收集过热详情用于告警
        surge_warning = []
        if not signals_df.empty:
            signals_df, surged = self._filter_surging_stocks(signals_df)
            surge_warning.extend(surged)
        if not approaching_df.empty:
            approaching_df, surged = self._filter_surging_stocks(approaching_df)
            surge_warning.extend(surged)

        # 如有过热股，推送钉钉告警
        if surge_warning:
            self._push_surge_warning(surge_warning)

        print(f"[PoolStrategy] 今日买入信号: {len(signals_df)} 只 | 接近买入区: {len(approaching_df)} 只")

        # 4. 按类型分组
        by_type = self._group_by_type(signals_df)
        by_type_approaching = self._group_by_type(approaching_df)

        # 5. 生成摘要
        summary = self._build_summary(signals_df, approaching_df)

        return {
            "signals": signals_df,
            "approaching": approaching_df,
            "by_type": by_type,
            "by_type_approaching": by_type_approaching,
            "all_pool": pool_df,
            "summary": summary,
            "surge_warning": surge_warning,  # 过热股列表，供 format_dingtalk 使用
        }

    def format_dingtalk(self, result: dict) -> str:
        """
        生成钉钉推送文本（Markdown 格式，按 PRD 定义格式）。
        始终返回字符串（无信号时也给摘要）。
        """
        signals = result.get("signals", pd.DataFrame())
        approaching = result.get("approaching", pd.DataFrame())
        all_pool = result.get("all_pool", pd.DataFrame())

        today = datetime.now().strftime("%m月%d日")
        total = len(all_pool) if not all_pool.empty else 0
        lines = [f"### 【股票池日报 {today}】  池内共{total}只\n"]

        # ── 买入信号（按类型分组，每类最多3只，按安全边际排序）──
        TOP_PER_TYPE = 3
        if not signals.empty:
            lines.append(f"**>> 今日买入区（{len(signals)}只，以下为各类型精选）**\n")
            by_type = result.get("by_type", {})
            for ctype in ["brand", "cashflow", "resource", "rate_sensitive", "growth", "policy"]:
                rows = by_type.get(ctype, [])
                if not rows:
                    continue
                # 按安全边际降序排（最被低估的优先）
                rows = sorted(rows, key=lambda r: r.get("safety_margin") or 0, reverse=True)
                type_name = self.classifier.get_type_name(ctype)
                show = rows[:TOP_PER_TYPE]
                lines.append(f"**[{type_name}]（共{len(rows)}只，精选{len(show)}只）**")
                for r in show:
                    name = str(r.get("company_name", ""))[:6]
                    ts = r.get("ts_code", "")
                    reason = r.get("signal_reason", "")
                    margin = r.get("safety_margin")
                    target = r.get("target_price_mid")
                    margin_str = f"  安全边际{margin*100:+.0f}%" if margin else ""
                    target_str = f"  目标价{target:.0f}" if target else ""
                    lines.append(f"  · {name}({ts[:6]})  {reason}{target_str}{margin_str}")
                lines.append("")
        else:
            lines.append("**今日池内无新增买入信号**\n")

        # ── 接近买入区（每类最多3只）──
        if not approaching.empty:
            by_type_a = result.get("by_type_approaching", {})
            approaching_lines = []
            for ctype in ["brand", "cashflow", "resource", "rate_sensitive", "growth", "policy"]:
                rows = by_type_a.get(ctype, [])
                if not rows:
                    continue
                type_name = self.classifier.get_type_name(ctype)
                names = [f"{str(r.get('company_name',''))[:4]}({r.get('ts_code','')[:6]})"
                         for r in rows[:3]]
                approaching_lines.append(f"{type_name}: {'  '.join(names)}")
            if approaching_lines:
                lines.append(f"**>> 接近买入区（{len(approaching)}只）**")
                lines.extend(approaching_lines)
                lines.append("")

        lines.append("> 以上为估值信号，需结合基本面、市场环境判断")

        # 过热预警段（如有）
        surge_warning = result.get("surge_warning", [])
        if surge_warning:
            lines.append("")
            lines.append(f"**🔥 过热预警（{len(surge_warning)}只已剔除）**")
            for d in surge_warning:
                lines.append(f"  ⚠ {d['name']}({d['ts_code'][:6]}) 连续{d['consec_days']}日涨 +{d['total_rise']:.1f}% → 暂不入场")

        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────────

    def _get_pool_with_valuation(self) -> pd.DataFrame:
        """获取 watch/reserve 层股票 + 最新估值数据"""
        # 池子基础信息
        pool_df = DBUtils.query_df("""
            SELECT ts_code, company_name, company_type, tier, notes
            FROM stock_pool
            WHERE is_active = 1 AND tier IN ('watch', 'reserve')
        """)
        if pool_df.empty:
            return pd.DataFrame()

        # 最新估值（从 valuation_history 取每只股票最新一条）
        ts_list = pool_df["ts_code"].tolist()
        placeholders = ",".join(["?" for _ in ts_list])
        val_df = DBUtils.query_df(f"""
            SELECT v.*
            FROM valuation_history v
            INNER JOIN (
                SELECT ts_code, MAX(trade_date) as max_date
                FROM valuation_history
                WHERE ts_code IN ({placeholders})
                GROUP BY ts_code
            ) latest ON v.ts_code = latest.ts_code AND v.trade_date = latest.max_date
        """, params=ts_list)

        if val_df.empty:
            # 没有估值数据，直接返回池子基础信息
            return pool_df

        # 合并
        merged = pool_df.merge(val_df, on="ts_code", how="left")

        # 补充当前价格（从 stock_daily 取最新收盘价）
        price_df = DBUtils.query_df(f"""
            SELECT sd.ts_code, sd.close, sd.trade_date
            FROM stock_daily sd
            INNER JOIN (
                SELECT ts_code, MAX(trade_date) as max_date
                FROM stock_daily
                WHERE ts_code IN ({placeholders})
                GROUP BY ts_code
            ) latest ON sd.ts_code = latest.ts_code AND sd.trade_date = latest.max_date
        """, params=ts_list)

        if not price_df.empty:
            merged = merged.merge(
                price_df[["ts_code", "close"]].rename(columns={"close": "current_price"}),
                on="ts_code", how="left"
            )

        return merged

    def _build_reason(self, row: dict, ctype: str) -> str:
        """生成信号原因一句话描述"""
        pe_pct = row.get("pe_percentile_5y")
        pb_pct = row.get("pb_percentile_5y")
        pb = row.get("pb")
        peg = row.get("peg")
        dy = row.get("dividend_yield")

        if ctype == "resource":
            return f"PB历史分位{pb_pct:.0f}% 处于低位" if pb_pct else "PB低位"
        elif ctype == "brand":
            return f"PE历史分位{pe_pct:.0f}% 处于历史便宜区" if pe_pct else "PE低位"
        elif ctype == "growth":
            return f"PEG={peg:.2f} 增速相对估值低" if peg else "PEG低"
        elif ctype == "rate_sensitive":
            return f"PB={pb:.2f} 低于净资产 历史分位{pb_pct:.0f}%" if pb and pb_pct else f"PB={pb:.2f}"
        elif ctype == "policy":
            return f"PE历史分位{pe_pct:.0f}% 估值合理偏低" if pe_pct else "PE低位"
        elif ctype == "cashflow":
            return f"股息率{dy:.1f}% 配置价值高" if dy else "股息率高"
        return "估值进入低位区间"

    def _group_by_type(self, df: pd.DataFrame) -> dict:
        """按类型分组，返回 {company_type: [row_dict, ...]}"""
        if df.empty:
            return {}
        result = {}
        for ctype in ["brand", "cashflow", "growth", "resource", "rate_sensitive", "policy"]:
            sub = df[df["company_type"] == ctype]
            if not sub.empty:
                result[ctype] = sub.to_dict("records")
        return result

    def _filter_surging_stocks(self, df: pd.DataFrame,
                               consec_days: int = 3,
                               total_rise_pct: float = 15.0):
        """
        过滤近期连续大涨股（山顶风险）。
        判断条件（两者同时满足才剔除）：
          1. 最近 consec_days 个交易日全部上涨（每日 close > 前日 close）
          2. 这段时间累计涨幅 >= total_rise_pct%

        Returns:
            (filtered_df, surge_details)
            surge_details: list of dict，每条含 ts_code/name/consec_days/total_rise
        """
        if df.empty:
            return df, []

        codes = df['ts_code'].tolist()
        # 建立 ts_code → company_name 映射
        name_map = {row['ts_code']: row.get('company_name', row['ts_code'])
                    for _, row in df.iterrows()}

        placeholders = ','.join(['?' for _ in codes])
        hist = DBUtils.query_df(f"""
            SELECT ts_code, trade_date, close
            FROM stock_daily
            WHERE ts_code IN ({placeholders})
            ORDER BY ts_code, trade_date DESC
        """, params=codes)

        if hist.empty:
            return df, []

        surge_codes = set()
        surge_details = []
        for code, grp in hist.groupby('ts_code'):
            grp = grp.sort_values('trade_date', ascending=False).reset_index(drop=True)
            if len(grp) < consec_days + 1:
                continue
            recent = grp.iloc[:consec_days]
            prev   = grp.iloc[1:consec_days + 1]
            is_consec = all(
                recent.iloc[i]['close'] > prev.iloc[i]['close']
                for i in range(consec_days)
            )
            base_close = grp.iloc[consec_days]['close']
            top_close  = grp.iloc[0]['close']
            total_rise = (top_close - base_close) / base_close * 100 if base_close > 0 else 0

            if is_consec and total_rise >= total_rise_pct:
                surge_codes.add(code)
                surge_details.append({
                    'ts_code':    code,
                    'name':       str(name_map.get(code, code))[:6],
                    'consec_days': consec_days,
                    'total_rise': total_rise,
                })

        if surge_codes:
            names = [f"{d['name']}(+{d['total_rise']:.1f}%)" for d in surge_details]
            print(f"[PoolStrategy] ⚠️ 过滤过热股 {len(surge_codes)} 只: {', '.join(names)}")
        return df[~df['ts_code'].isin(surge_codes)].reset_index(drop=True), surge_details

    def _push_surge_warning(self, surge_details: list):
        """将过热股告警推送到钉钉"""
        try:
            from src.utils.notifier import send_alert
            lines = [f"### 🔥 股票池过热预警（{len(surge_details)}只）\n",
                     "以下股票连续大涨已被**剔除买入信号**，当前处于过热区域，请注意风险：\n"]
            for d in surge_details:
                lines.append(f"- **{d['name']}** ({d['ts_code'][:6]})  "
                              f"连续{d['consec_days']}日上涨  "
                              f"区间涨幅 **+{d['total_rise']:.1f}%**")
            lines.append("\n⚠️ 如已持有，建议关注止盈；若未建仓，等待回调再介入")
            send_alert(
                f"🔥 过热预警 {len(surge_details)}只",
                '\n'.join(lines),
                message_type='surge_warning'
            )
        except Exception as e:
            print(f"[PoolStrategy] 过热告警推送失败（非关键）: {e}")

    def _build_summary(self, signals_df: pd.DataFrame, approaching_df: pd.DataFrame) -> str:
        if signals_df.empty and approaching_df.empty:
            return "今日池内无买入信号"
        parts = []
        if not signals_df.empty:
            parts.append(f"{len(signals_df)}只进入买入区")
        if not approaching_df.empty:
            parts.append(f"{len(approaching_df)}只接近买入区")
        return "、".join(parts)
