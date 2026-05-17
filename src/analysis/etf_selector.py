"""
A股ETF挑选：与行业择机联动，按流动性、规模、主题匹配推荐主题ETF。

建议持有周期：1 周～3 个月（可在 config etf_selector.holding_period_hint 配置）。

思路：
- 流动性：日成交额不低于阈值，避免难成交。
- 规模：总市值/份额不低于阈值，避免迷你ETF清盘或跟踪偏差大。
- 主题匹配：行业择机给出的行业（如电子、银行）→ 用名称关键词匹配ETF（如名称含「电子」「半导体」）。
- 排除：杠杆、反向、跨境等品种，仅保留场内A股主题/行业ETF。
"""

import os
import re
import time
from typing import Dict, List, Any, Optional

import pandas as pd

# 模块加载时清代理，避免 akshare/requests 走代理
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

from src.utils.config_loader import Config

try:
    import akshare as ak
except ImportError:
    ak = None


def _get_etf_spot_df_em(max_retries: int = 2) -> Optional[pd.DataFrame]:
    """拉取东方财富 ETF 实时行情（全量），失败时重试。"""
    if ak is None:
        return None
    for attempt in range(max_retries):
        if attempt > 0:
            time.sleep(3)
        try:
            df = ak.fund_etf_spot_em()
            if df is None or df.empty:
                return None
            return df
        except Exception as e:
            print(f"[ETFSelector] 东方财富获取失败(尝试 {attempt + 1}/{max_retries}): {e}")
    return None


def _get_etf_spot_df_sina() -> Optional[pd.DataFrame]:
    """拉取新浪财经 ETF 列表（代码、名称、涨跌幅、成交额等，无总市值）。"""
    if ak is None:
        return None
    try:
        df = ak.fund_etf_category_sina(symbol="ETF基金")
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        print(f"[ETFSelector] 新浪财经获取失败: {e}")
        return None


def _get_etf_spot_df_ths() -> Optional[pd.DataFrame]:
    """拉取同花顺 ETF 列表（基金代码、基金名称、增长率），无成交额/总市值，仅做名称匹配。"""
    if ak is None:
        return None
    try:
        df = ak.fund_etf_spot_ths(date="")
        if df is None or df.empty:
            return None
        # 统一列名：同花顺为 基金代码/基金名称/增长率
        df = df.rename(columns={"基金代码": "代码", "基金名称": "名称", "增长率": "涨跌幅"})
        df["成交额"] = 0  # 无成交额，后续流动性过滤会放宽
        return df
    except Exception as e:
        print(f"[ETFSelector] 同花顺获取失败: {e}")
        return None


def _get_etf_spot_df() -> Optional[pd.DataFrame]:
    """按 config 数据源拉取 ETF 行情：eastmoney | sina | ths | auto（依次尝试）。"""
    cfg = Config.get("etf_selector") or {}
    source = (cfg.get("data_source") or "auto").strip().lower()
    if source == "sina":
        df = _get_etf_spot_df_sina()
        if df is not None and not df.empty:
            return df
        print("[ETFSelector] 新浪失败，尝试东方财富...")
        df = _get_etf_spot_df_em()
        if df is not None and not df.empty:
            return df
        print("[ETFSelector] 东方财富失败，尝试同花顺...")
        return _get_etf_spot_df_ths()
    if source == "ths":
        df = _get_etf_spot_df_ths()
        if df is not None and not df.empty:
            return df
        print("[ETFSelector] 同花顺失败，尝试新浪...")
        df = _get_etf_spot_df_sina()
        if df is not None and not df.empty:
            return df
        return _get_etf_spot_df_em()
    if source == "eastmoney":
        return _get_etf_spot_df_em()
    # auto: 东财 -> 新浪 -> 同花顺
    df = _get_etf_spot_df_em()
    if df is not None and not df.empty:
        return df
    print("[ETFSelector] 东方财富不可用，尝试新浪财经...")
    df = _get_etf_spot_df_sina()
    if df is not None and not df.empty:
        return df
    print("[ETFSelector] 新浪不可用，尝试同花顺...")
    return _get_etf_spot_df_ths()


def _numeric_series(s: pd.Series, default: float = 0.0) -> pd.Series:
    """转为数值，非法填 default。"""
    return pd.to_numeric(s, errors="coerce").fillna(default)


def run(
    industry_names: Optional[List[str]] = None,
    top_per_industry: int = 2,
) -> Dict[str, Any]:
    """
    根据行业名单挑选主题ETF，并与流动性、规模过滤结合。

    Args:
        industry_names: 行业名列表（如行业择机输出的 emerging + mature 行业）。
                       若为 None，则从 config industry_timing.emerging_industries 等取默认。
        top_per_industry: 每个行业最多保留几只ETF（按成交额或规模排序）。

    Returns:
        {
            "by_industry": { "电子": [{"code","name","涨跌幅","成交额","总市值", ...}], ... },
            "etf_all_count": int,
            "etf_filtered_count": int,
            "error": str or None,
        }
    """
    cfg = Config.get("etf_selector") or {}
    min_amount_wan = float(cfg.get("min_daily_amount_wan", 500))
    min_mv_yi = float(cfg.get("min_total_mv_yi", 2.0))
    industry_keywords = cfg.get("industry_keywords") or {}
    exclude_keywords = cfg.get("exclude_keywords") or []

    out = {
        "by_industry": {},
        "etf_all_count": 0,
        "etf_filtered_count": 0,
        "error": None,
    }

    df = _get_etf_spot_df()
    if df is None or df.empty:
        out["error"] = "无法获取ETF行情"
        return out

    out["etf_all_count"] = len(df)

    # 列名兼容（东方财富接口字段）
    name_col = "名称" if "名称" in df.columns else "name"
    code_col = "代码" if "代码" in df.columns else "code"
    if name_col not in df.columns or code_col not in df.columns:
        out["error"] = "ETF行情缺少名称/代码列"
        return out

    # 成交额：可能是 "成交额"（万元或元），总市值可能是 "总市值"（元）
    amount_col = "成交额" if "成交额" in df.columns else None
    mv_col = "总市值" if "总市值" in df.columns else None
    pct_col = "涨跌幅" if "涨跌幅" in df.columns else None

    # 成交额：东财/新浪一般为元，转为万元
    df["_amount_wan"] = (_numeric_series(df[amount_col]) / 1e4) if amount_col else 0
    # 总市值：东财有、新浪无；有则为元转亿元
    if mv_col:
        df["_mv_yi"] = _numeric_series(df[mv_col]) / 1e8
    else:
        df["_mv_yi"] = 0.0

    # 排除关键词
    def _excluded(n: str) -> bool:
        if pd.isna(n):
            return True
        n = str(n)
        for kw in exclude_keywords:
            if kw in n:
                return True
        return False

    df = df[~df[name_col].apply(_excluded)].copy()
    # 流动性过滤：有成交额数据时才按 min_amount_wan 过滤，否则保留（如同花顺无成交额）
    if df["_amount_wan"].max() > 0:
        df = df[df["_amount_wan"] >= min_amount_wan].copy()
    # 规模过滤：有总市值数据时才按 min_mv_yi 过滤
    if df["_mv_yi"].max() > 0:
        df = df[df["_mv_yi"] >= min_mv_yi].copy()
    out["etf_filtered_count"] = len(df)

    if df.empty:
        return out

    # 确定要匹配的行业列表
    if industry_names is None:
        it_cfg = Config.get("industry_timing") or {}
        emerging = it_cfg.get("emerging_industries") or []
        industry_names = list(emerging)
        # 成熟行业也可加入，避免重复可从 industry_keywords 的 key 取
        for k in industry_keywords:
            if k not in industry_names:
                industry_names.append(k)
    industry_names = [str(x).strip() for x in industry_names if x]

    # 按行业匹配
    for ind in industry_names:
        keywords = industry_keywords.get(ind)
        if not keywords:
            keywords = [ind]
        pattern = "|".join(re.escape(k) for k in keywords)
        mask = df[name_col].astype(str).str.contains(pattern, na=False, regex=True)
        sub = df.loc[mask].copy()
        if sub.empty:
            continue
        # 按成交额降序，取前 top_per_industry
        sub = sub.sort_values("_amount_wan", ascending=False).head(top_per_industry)
        rows = []
        for _, r in sub.iterrows():
            rows.append({
                "code": str(r[code_col]),
                "name": str(r[name_col]),
                "涨跌幅": r.get(pct_col),
                "成交额_万": round(r["_amount_wan"], 0),
                "总市值_亿": round(r["_mv_yi"], 2),
            })
        out["by_industry"][ind] = rows

    return out


def _save_etf_csv(result: Dict[str, Any], output_dir: str = None):
    """将本次 ETF 挑选结果保存到 output/etf_picks_YYYYMMDD.csv"""
    from datetime import datetime as _dt
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(__file__), "..", "..", "output")
    os.makedirs(output_dir, exist_ok=True)
    rows = []
    for ind, etf_list in (result.get("by_industry") or {}).items():
        for x in etf_list:
            rows.append({
                "date": _dt.now().strftime("%Y-%m-%d"),
                "industry": ind,
                "code": x.get("code", ""),
                "name": x.get("name", ""),
                "涨跌幅": x.get("涨跌幅"),
                "成交额_万": x.get("成交额_万", 0),
                "总市值_亿": x.get("总市值_亿", 0),
                "strategy": x.get("strategy", ""),
                "score": x.get("score", 0),
                "signal": x.get("signal", ""),
                "futures_signal": x.get("futures_signal", ""),
                "futures_strength": x.get("futures_strength", 0),
                "futures_score": x.get("futures_score", 0),
                "futures_reason": x.get("futures_reason", ""),
                "futures_sector": x.get("futures_sector", ""),
                "futures_holding_period": x.get("futures_holding_period", ""),
                "futures_position_suggestion": x.get("futures_position_suggestion", ""),
                "futures_operation_advice": x.get("futures_operation_advice", ""),
                "futures_risk_level": x.get("futures_risk_level", ""),
            })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    csv_path = os.path.join(output_dir, f"etf_picks_{_dt.now().strftime('%Y%m%d')}.csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"[OK] ETF 结果已保存: {csv_path} ({len(df)} 条)")
    return csv_path


def _score_etf_by_strategy(
    by_industry: Dict[str, List[Dict]],
    industry_timing_data: Optional[Dict],
) -> Dict[str, List[Dict]]:
    """
    基于行业择机的周期+渗透率理论，对每只 ETF 打分并标注策略信号。

    数据来源（优先级）：
      1. industry_timing_data 中 emerging/mature DataFrame（含相对强度 RS 加分）
      2. config industry_timing.penetration_phase + cycle_type（渗透率+周期理论，中长期持有核心依据）

    新兴行业（渗透率）：
      - early_growth（破壁）→ +3 分，信号「重点关注」
      - mid_growth（高速）→ +2 分，信号「积极配置」
      - mature / late / decline → +0 或负分

    成熟行业（周期匹配）：
      - cycle_match 匹配当前经济周期 → +2 分
      - quarter_rs > 0（季度相对强度为正）→ +1 分
      - year_rs > 0（年度相对强度为正）→ +1 分
      - 不匹配则 +0，信号「观望」

    最终附带 strategy 说明和 signal（重点关注/积极配置/适度配置/观望/回避）。
    """
    # ---- 解析实时行业择机数据 ----
    emerging_info = {}  # {行业名: {penetration_phase, relative_strength}}
    mature_info = {}    # {行业名: {cycle_type, cycle_match, quarter_rs, year_rs}}
    has_live_data = False

    if industry_timing_data:
        edf = industry_timing_data.get("emerging")
        if edf is not None and not edf.empty and "industry" in edf.columns:
            for _, row in edf.iterrows():
                emerging_info[str(row["industry"])] = {
                    "penetration_phase": row.get("penetration_phase", ""),
                    "relative_strength": row.get("relative_strength", 0),
                }
            has_live_data = True

        mdf = industry_timing_data.get("mature")
        if mdf is not None and not mdf.empty and "industry" in mdf.columns:
            for _, row in mdf.iterrows():
                mature_info[str(row["industry"])] = {
                    "cycle_type": row.get("cycle_type", ""),
                    "cycle_match": bool(row.get("cycle_match", False)),
                    "quarter_rs": row.get("quarter_rs"),
                    "year_rs": row.get("year_rs"),
                }
            has_live_data = True

    # ---- 始终加载 config 渗透率+周期配置，作为中长期策略核心依据 ----
    it_cfg = Config.get("industry_timing") or {}
    cfg_pen = it_cfg.get("penetration_phase") or {}
    cfg_cyc = it_cfg.get("cycle_type") or {}
    cfg_emerging_list = it_cfg.get("emerging_industries") or []
    current_cycle = "mid"  # 默认中周期
    if industry_timing_data:
        current_cycle = industry_timing_data.get("current_cycle", "mid") or "mid"

    # ---- 打分字典 ----
    pen_score = {"early_growth": 3, "mid_growth": 2, "mature": 0, "late": -1, "decline": -2}
    pen_signal = {"early_growth": "重点关注", "mid_growth": "积极配置", "mature": "适度配置", "late": "观望", "decline": "回避"}
    pen_cn = {"early_growth": "破壁期", "mid_growth": "高速期", "mature": "饱和期", "late": "晚期", "decline": "衰退期"}
    cyc_cn = {"early": "早周期", "mid": "中周期", "late": "晚周期", "defensive": "防御"}

    scored = {}
    for ind, etf_list in by_industry.items():
        for x in etf_list:
            score = 0
            strategy_parts = []
            signal = "适度配置"

            # ---- 第一层：渗透率阶段（中长期核心依据，来自 config） ----
            phase = cfg_pen.get(ind, "")
            is_emerging = ind in cfg_emerging_list
            if phase:
                score += pen_score.get(phase, 0)
                strategy_parts.append(f"渗透率·{pen_cn.get(phase, phase)}")
                if is_emerging or phase in ("early_growth", "mid_growth"):
                    signal = pen_signal.get(phase, "适度配置")

            # ---- 第二层：经济周期匹配（中长期依据，来自 config） ----
            cyc_type = cfg_cyc.get(ind, "")
            if cyc_type:
                cycle_match = (current_cycle == cyc_type)
                ct = cyc_cn.get(cyc_type, cyc_type)
                if cycle_match:
                    score += 2
                    strategy_parts.append(f"周期·{ct}匹配")
                    # 周期匹配可升级信号（非衰退/晚期行业）
                    if phase not in ("late", "decline") and signal in ("适度配置", "观望"):
                        signal = "适度配置"
                else:
                    strategy_parts.append(f"周期·{ct}不匹配")
                    # 周期不匹配且渗透率非成长 → 降级
                    if phase not in ("early_growth", "mid_growth") and signal != "回避":
                        signal = "观望"

            # ---- 第三层：实时相对强度加分（有则锦上添花） ----
            if ind in emerging_info:
                rs = emerging_info[ind].get("relative_strength", 0) or 0
                if rs > 0:
                    score += 1
                    strategy_parts.append(f"RS{rs:+.1f}%")
            elif ind in mature_info:
                info = mature_info[ind]
                qrs = info.get("quarter_rs")
                yrs = info.get("year_rs")
                if qrs is not None and qrs > 0:
                    score += 1
                if yrs is not None and yrs > 0:
                    score += 1
                parts = []
                if qrs is not None:
                    parts.append(f"季{qrs:+.1f}%")
                if yrs is not None:
                    parts.append(f"年{yrs:+.1f}%")
                if parts:
                    strategy_parts.append(" ".join(parts))
                # 实时周期匹配 + 季度正收益 → 升级
                if info.get("cycle_match") and (qrs or 0) > 0:
                    if signal == "观望":
                        signal = "适度配置"
                    elif signal == "适度配置":
                        signal = "积极配置"

            # ---- 兜底：都没命中 ----
            if not phase and not cyc_type:
                strategy_parts = ["未入配置"]
                signal = "观望"

            # 衰退/晚期强制降级
            if phase == "decline":
                signal = "回避"
            elif phase == "late" and signal not in ("回避",):
                signal = "观望"

            x["score"] = score
            x["strategy"] = " | ".join(strategy_parts)
            x["signal"] = signal

        scored[ind] = etf_list
    return scored


def run_with_industry_timing(industry_timing_data: Optional[Dict] = None, top_per_industry: int = 2, save_csv: bool = True, use_futures_signal: bool = True) -> Dict[str, Any]:
    """
    与行业择机结果联动：基于渗透率+周期理论挑选并打分 ETF，保存结果。

    Args:
        industry_timing_data: IndustryTiming.run_split() 的返回值；若为 None 则仅用 config 默认行业。
        top_per_industry: 每行业最多几只ETF。
        save_csv: 是否保存到 output/etf_picks_YYYYMMDD.csv
        use_futures_signal: 是否使用期货价格信号（默认True）
    """
    industry_names = []
    if industry_timing_data:
        for df_key in ("emerging", "mature"):
            df = industry_timing_data.get(df_key)
            if df is not None and not df.empty and "industry" in df.columns:
                industry_names.extend(df["industry"].dropna().astype(str).unique().tolist())
    industry_names = list(dict.fromkeys(industry_names))
    result = run(industry_names=industry_names or None, top_per_industry=top_per_industry)

    # 基于周期+渗透率理论打分（始终执行，config 配置为中长期策略核心）
    if result.get("by_industry"):
        result["by_industry"] = _score_etf_by_strategy(result["by_industry"], industry_timing_data)
        
        # 添加期货价格信号（如果启用）
        if use_futures_signal:
            try:
                from src.analysis.futures_etf_signal import FuturesETFSignalGenerator
                signal_generator = FuturesETFSignalGenerator()
                
                # 为每个ETF添加期货信号
                for industry, etf_list in result["by_industry"].items():
                    updated_list = signal_generator.generate_etf_signals(etf_list)
                    result["by_industry"][industry] = updated_list
                    
                    # 为每个ETF添加持仓周期和操作建议
                    for etf in updated_list:
                        if etf.get('futures_signal') and etf.get('futures_signal') != 'N/A':
                            # 获取板块信号详情
                            sector = etf.get('futures_sector')
                            if sector:
                                sector_signal = signal_generator.generate_signal(sector)
                                etf['futures_holding_period'] = sector_signal.get('holding_period', '')
                                etf['futures_position_suggestion'] = sector_signal.get('position_suggestion', '')
                                etf['futures_operation_advice'] = sector_signal.get('operation_advice', '')
                                etf['futures_risk_level'] = sector_signal.get('risk_level', '')
                
                print("[ETFSelector] 已添加期货价格信号和操作建议")
            except Exception as e:
                print(f"[WARN] 添加期货价格信号失败: {e}")

    # 保存 CSV
    if save_csv and result.get("by_industry"):
        try:
            _save_etf_csv(result)
        except Exception as e:
            print(f"[WARN] ETF CSV 保存失败: {e}")

    return result
