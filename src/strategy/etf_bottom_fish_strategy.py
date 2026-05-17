"""
ETF 板块轮动抄底反弹策略

核心逻辑：
  1. 获取所有场内 A 股行业/主题 ETF 行情（东方财富）
  2. 过滤流动性（成交额 ≥ 500 万）、规模（市值 ≥ 2 亿）
  3. 拉取近 60 日 K 线，计算五维指标：
       - 回调深度：从近期高点的跌幅（理想区间 10-35%）
       - RSI 超卖：RSI(14) < 35 且回升
       - 量能放大：近 5 日均量 / 近 20 日均量 > 1.2
       - 价格企稳：近 5 日涨跌幅转正
       - 板块轮动：近 5 日涨 && 近 20 日跌（典型抄底信号）
  4. 五维加权打分，选出 Top N
  5. 附加建仓建议（首仓比例、参考止损）

使用：
    from src.strategy.etf_bottom_fish_strategy import ETFBottomFishStrategy
    picks = ETFBottomFishStrategy().run(top_n=6)
"""

import os
import time
import datetime
from typing import Optional

import numpy as np
import pandas as pd

for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

try:
    import akshare as ak
except ImportError:
    ak = None


# ──────────────────────────────────────────────
# 配置常量（可通过 Config 覆盖）
# ──────────────────────────────────────────────
MIN_AMOUNT_WAN   = 500      # 最小日成交额（万元）
MIN_MV_YI        = 2.0      # 最小市值（亿元）
HIST_DAYS        = 60       # 拉取历史K线天数
IDEAL_DROP_LOW   = 0.10     # 理想回调区间下限（10%）
IDEAL_DROP_HIGH  = 0.35     # 理想回调区间上限（35%）
VOLUME_RATIO_TH  = 1.2      # 量比阈值（近5日/近20日均量）

# 所有品类都排除（杠杆、反向、货币市场）
EXCLUDE_ALWAYS = ["杠杆", "反向", "做空", "货币", "黄金债", "国债", "利率债"]

# A 股行业 ETF 额外排除（海外指数）
EXCLUDE_ASTOCK_EXTRA = ["跨境", "美元", "港股", "纳斯达克",
                         "标普", "日经", "德国", "法国", "恒生"]

# 商品 ETF 关键词白名单（命中则标记为 commodity，不受 ASTOCK_EXTRA 过滤）
COMMODITY_KEYWORDS = ["黄金", "白银", "原油", "石油", "天然气", "铜", "铝", "锌", "镍",
                       "豆粕", "玉米", "棉花", "白糖", "橡胶", "煤炭", "钢铁", "有色",
                       "贵金属", "商品", "能源", "农产品"]

# QDII 关键词（命中则标记为 qdii，允许海外指数名称）
QDII_KEYWORDS     = ["QDII", "纳斯达克", "标普", "日经", "德国", "法国", "恒生",
                      "港股", "美股", "亚太", "全球", "新兴市场", "欧洲", "东南亚",
                      "印度", "越南", "中概互联", "互联网ETF", "香港科技"]


def _rsi(close: pd.Series, period: int = 14) -> float:
    """计算最新 RSI 值。"""
    if len(close) < period + 1:
        return 50.0
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    val = rsi.iloc[-1]
    return float(val) if not np.isnan(val) else 50.0


def _normalize_etf_df(df: pd.DataFrame) -> pd.DataFrame:
    """标准化东方财富 ETF 行情字段。"""
    rename = {}
    for col in df.columns:
        lc = col.lower().replace(" ", "")
        if "代码" in col or "code" in lc:          rename[col] = "code"
        elif "名称" in col or "name" in lc:         rename[col] = "name"
        elif "涨跌幅" in col or "pct" in lc:        rename[col] = "pct_chg"
        elif "成交额" in col or "amount" in lc:     rename[col] = "amount"
        elif "总市值" in col or "market" in lc:     rename[col] = "total_mv"
        elif "最新价" in col or "现价" in col or "price" in lc: rename[col] = "price"
    df = df.rename(columns=rename)
    for col in ["code", "name", "pct_chg", "amount", "total_mv", "price"]:
        if col not in df.columns:
            df[col] = np.nan
    df["code"]     = df["code"].astype(str).str.strip()
    df["name"]     = df["name"].astype(str).str.strip()
    df["pct_chg"]  = pd.to_numeric(df["pct_chg"],  errors="coerce")
    df["amount"]   = pd.to_numeric(df["amount"],   errors="coerce")
    df["total_mv"] = pd.to_numeric(df["total_mv"], errors="coerce")
    df["price"]    = pd.to_numeric(df["price"],    errors="coerce")
    df["amount_wan"] = df["amount"]   / 1e4
    df["mv_yi"]      = df["total_mv"] / 1e8
    return df


def _classify_etf(name: str) -> str:
    """按名称判断 ETF 品类：astock / commodity / qdii。"""
    if any(kw in name for kw in COMMODITY_KEYWORDS):
        return "commodity"
    if any(kw in name for kw in QDII_KEYWORDS):
        return "qdii"
    return "astock"


def _patch_no_proxy():
    """强制 requests.Session 不走代理（修复 VPN/proxy 环境变量干扰）。"""
    try:
        import requests
        _orig = requests.Session.merge_environment_settings
        def _no_proxy(self, url, proxies, stream, verify, cert):
            result = _orig(self, url, proxies, stream, verify, cert)
            result["proxies"] = {}
            return result
        requests.Session.merge_environment_settings = _no_proxy
    except Exception:
        pass


def _fetch_etf_list() -> Optional[pd.DataFrame]:
    """拉取东方财富全量 ETF 行情（含场内商品 ETF 与 QDII ETF/LOF）。
    主源：东方财富 fund_etf_spot_em；失败时降级至新浪多分类汇总。
    """
    if ak is None:
        print("[ETFBottomFish] akshare 未安装")
        return None

    # 强制禁用代理（防止 VPN 环境变量污染 requests.Session）
    for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ.pop(_k, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    _patch_no_proxy()

    frames = []

    # ── 主源：东方财富全量场内 ETF ──
    em_ok = False
    try:
        df = ak.fund_etf_spot_em()
        if df is not None and not df.empty:
            df = _normalize_etf_df(df)
            frames.append(df)
            em_ok = True
    except Exception as e:
        print(f"[ETFBottomFish] 东方财富全量 ETF 获取失败: {e}")

    # ── 降级备源：新浪各分类 ETF（东方财富失败时启用）──
    if not em_ok:
        print("[ETFBottomFish] 降级使用新浪数据源...")
        for category in ["股票型基金", "混合型基金", "ETF基金", "QDII基金"]:
            try:
                df_s = ak.fund_etf_category_sina(symbol=category)
                if df_s is not None and not df_s.empty:
                    df_s = _normalize_etf_df(df_s)
                    frames.append(df_s)
            except Exception:
                pass

    # ── 补充：新浪 QDII 基金（东方财富成功时也追加）──
    if em_ok:
        try:
            df_qdii = ak.fund_etf_category_sina(symbol="QDII基金")
            if df_qdii is not None and not df_qdii.empty:
                df_qdii = _normalize_etf_df(df_qdii)
                df_qdii["_from_qdii_api"] = True
                frames.append(df_qdii)
        except Exception:
            pass

    if not frames:
        return None

    combined = pd.concat(frames, ignore_index=True)
    # 去重（同一 code 保留第一条，主池优先）
    combined = combined.drop_duplicates(subset=["code"], keep="first")

    # 标记品类
    combined["etf_type"] = combined["name"].apply(_classify_etf)

    return combined


def _filter_etf(df: pd.DataFrame) -> pd.DataFrame:
    """过滤：流动性 + 规模 + 按品类排除不同关键词。"""

    def _should_exclude(row) -> bool:
        name     = str(row.get("name", ""))
        etype    = row.get("etf_type", "astock")
        # 所有品类都排除杠杆/反向/货币
        if any(kw in name for kw in EXCLUDE_ALWAYS):
            return True
        # A 股行业 ETF 额外排除海外指数名称
        if etype == "astock" and any(kw in name for kw in EXCLUDE_ASTOCK_EXTRA):
            return True
        return False

    mask_excl = df.apply(_should_exclude, axis=1)
    df = df[~mask_excl].copy()

    # 流动性过滤（QDII API 补充数据可能无成交额，放宽）
    from_qdii = df.get("_from_qdii_api", False)
    liq_ok = (df["amount_wan"].fillna(0) >= MIN_AMOUNT_WAN) | df.get("_from_qdii_api", pd.Series(False, index=df.index)).fillna(False)
    df = df[liq_ok]

    # 规模过滤
    df = df[(df["mv_yi"].isna()) | (df["mv_yi"] >= MIN_MV_YI)]

    return df.reset_index(drop=True)


def _fetch_hist(code: str, days: int = HIST_DAYS) -> Optional[pd.DataFrame]:
    """拉取单只 ETF 历史 K 线（东方财富），返回含 close/volume 的 DataFrame。"""
    if ak is None:
        return None
    _patch_no_proxy()
    end   = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=days + 20)).strftime("%Y%m%d")
    # fund_etf_hist_em 只接受6位纯数字，去掉 sz/sh 前缀
    bare_code = code[-6:] if len(code) > 6 else code
    try:
        df = ak.fund_etf_hist_em(
            symbol=bare_code,
            period="daily",
            start_date=start,
            end_date=end,
            adjust="qfq",          # 前复权
        )
    except Exception:
        return None
    if df is None or df.empty:
        return None

    # 标准化列名
    rename = {}
    for col in df.columns:
        lc = col.lower()
        if "收盘" in col or "close" in lc:
            rename[col] = "close"
        elif "成交量" in col or "volume" in lc:
            rename[col] = "volume"
        elif "日期" in col or "date" in lc:
            rename[col] = "date"
    df = df.rename(columns=rename)
    if "close" not in df.columns or "volume" not in df.columns:
        return None

    df["close"]  = pd.to_numeric(df["close"],  errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.dropna(subset=["close", "volume"]).tail(days)
    return df.reset_index(drop=True)


# ──────────────────────────────────────────────
# 打分函数
# ──────────────────────────────────────────────

def _score_drawdown(drawdown: float) -> float:
    """回调深度评分：理想区间 10-35%，太浅或太深都减分。"""
    d = abs(drawdown)   # 正数，代表跌幅
    if d < 0.05:
        return 0.0
    elif d < 0.10:
        return 0.3 + (d - 0.05) / 0.05 * 0.4
    elif d <= 0.20:
        return 0.7 + (d - 0.10) / 0.10 * 0.3     # 0.7 → 1.0
    elif d <= 0.35:
        return 1.0
    elif d <= 0.50:
        return 1.0 - (d - 0.35) / 0.15 * 0.4     # 1.0 → 0.6
    else:
        return 0.3    # 跌超 50%，可能是基本面问题


def _score_rsi(rsi: float) -> float:
    """RSI 超卖回升评分：越接近 30 且向上得分越高。"""
    if rsi < 20:
        return 0.85   # 极度超卖，但可能加速下跌，略降分
    elif rsi < 30:
        return 1.0
    elif rsi < 40:
        return 0.8
    elif rsi < 50:
        return 0.5
    elif rsi < 55:
        return 0.2
    else:
        return 0.0


def _score_volume(volume_ratio: float) -> float:
    """量比评分：反弹放量是关键信号。"""
    if volume_ratio >= 2.0:
        return 1.0
    elif volume_ratio >= 1.5:
        return 0.85
    elif volume_ratio >= 1.2:
        return 0.65
    elif volume_ratio >= 1.0:
        return 0.35
    else:
        return 0.1


def _score_price_stabilize(ret_5d: float) -> float:
    """价格企稳评分：近 5 日涨跌幅。"""
    if ret_5d >= 0.04:
        return 1.0
    elif ret_5d >= 0.02:
        return 0.85
    elif ret_5d >= 0.005:
        return 0.65
    elif ret_5d >= 0:
        return 0.45
    elif ret_5d >= -0.02:
        return 0.2
    else:
        return 0.0


def _score_rotation(ret_5d: float, ret_20d: float) -> float:
    """板块轮动确认：近 5 日涨 + 近 20 日跌 = 典型抄底信号。"""
    if ret_5d > 0.01 and ret_20d < -0.05:
        return 1.0
    elif ret_5d > 0 and ret_20d < -0.03:
        return 0.8
    elif ret_5d > 0 and ret_20d < 0:
        return 0.5
    elif ret_5d > 0:
        return 0.3
    else:
        return 0.0


def _calc_indicators(hist: pd.DataFrame) -> dict:
    """给定历史 K 线，计算五维指标。"""
    close  = hist["close"]
    volume = hist["volume"]

    # 1. 回调深度（从近 60 日高点算起）
    peak       = close.max()
    latest     = close.iloc[-1]
    drawdown   = (latest - peak) / peak          # 负值

    # 2. RSI(14)
    rsi_val    = _rsi(close, 14)

    # 3. 量比：近 5 日均量 / 近 20 日均量
    vol5       = volume.iloc[-5:].mean()  if len(volume) >= 5  else np.nan
    vol20      = volume.iloc[-20:].mean() if len(volume) >= 20 else np.nan
    vol_ratio  = float(vol5 / vol20) if (vol20 and vol20 > 0) else 1.0

    # 4. 近 5 日 & 近 20 日价格收益率
    ret_5d     = float((close.iloc[-1] - close.iloc[-6])  / close.iloc[-6])  if len(close) >= 6  else 0.0
    ret_20d    = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if len(close) >= 21 else 0.0

    # 5. RSI 方向（近 3 日 RSI 均值 vs 前 3 日）——判断 RSI 是否回升
    rsi_series = pd.Series([_rsi(close.iloc[:i+1], 14) for i in range(len(close))])
    rsi_trend  = (rsi_series.iloc[-3:].mean() - rsi_series.iloc[-6:-3].mean()) if len(rsi_series) >= 6 else 0.0

    return {
        "drawdown":   drawdown,
        "rsi":        rsi_val,
        "rsi_trend":  rsi_trend,
        "vol_ratio":  vol_ratio,
        "ret_5d":     ret_5d,
        "ret_20d":    ret_20d,
        "peak":       peak,
        "latest":     latest,
    }


def _build_advice(row: dict) -> str:
    """生成建仓建议文字。"""
    score     = row["score"]
    drawdown  = row["drawdown"]
    rsi       = row["rsi"]
    ret_5d    = row["ret_5d"]

    # 首仓比例
    if score >= 0.75:
        position = "30%"
    elif score >= 0.60:
        position = "20%"
    else:
        position = "10%"

    # 止损位（从当前价 -5% 或跌回低点）
    stop_pct = "-5%"

    # 加仓条件
    add_cond = "突破5日均线后加仓至50%" if ret_5d > 0 else "确认企稳（收盘连续2日阳线）后加仓"

    return f"首仓{position}，{add_cond}，止损{stop_pct}"


# ──────────────────────────────────────────────
# 主策略类
# ──────────────────────────────────────────────

class ETFBottomFishStrategy:
    """板块轮动抄底反弹 ETF 选择策略。"""

    # 五维权重
    WEIGHTS = {
        "drawdown":  0.30,
        "rsi":       0.25,
        "volume":    0.20,
        "price":     0.15,
        "rotation":  0.10,
    }

    def run(self, top_n: int = 6, hist_days: int = HIST_DAYS,
            sleep_sec: float = 0.3, max_etf: int = 0) -> Optional[pd.DataFrame]:
        """
        执行抄底反弹 ETF 筛选。

        参数：
            top_n      : 最终输出数量
            hist_days  : 拉取历史 K 线天数（用于计算指标）
            sleep_sec  : 每只 ETF 拉取历史时的睡眠间隔（防限流）
            max_etf    : 最多处理几只 ETF（0=不限，用于快速模式）

        返回：
            DataFrame，含 code/name/score/drawdown/rsi/vol_ratio/ret_5d/ret_20d/advice
        """
        print("[ETFBottomFish] 步骤1：获取 ETF 列表...")
        spot_df = _fetch_etf_list()
        if spot_df is None:
            print("[ETFBottomFish] 获取 ETF 列表失败")
            return None

        print(f"[ETFBottomFish] 步骤2：过滤（原始 {len(spot_df)} 只）...")
        spot_df = _filter_etf(spot_df)
        if max_etf and max_etf > 0:
            spot_df = spot_df.head(max_etf)
        print(f"[ETFBottomFish] 过滤后 {len(spot_df)} 只")

        print(f"[ETFBottomFish] 步骤3：拉取历史 K 线并计算指标（{hist_days}日）...")
        records = []
        for i, row in spot_df.iterrows():
            code = row["code"]
            name = row["name"]
            hist = _fetch_hist(code, days=hist_days)
            if hist is None or len(hist) < 25:
                continue

            try:
                ind = _calc_indicators(hist)
            except Exception as e:
                print(f"  [WARN] {code} {name} 指标计算失败: {e}")
                continue

            # 打分
            s_drawdown = _score_drawdown(ind["drawdown"])
            s_rsi      = _score_rsi(ind["rsi"])
            # RSI 方向加成：RSI 回升则 +0.1
            if ind["rsi_trend"] > 2:
                s_rsi = min(1.0, s_rsi + 0.1)
            s_volume   = _score_volume(ind["vol_ratio"])
            s_price    = _score_price_stabilize(ind["ret_5d"])
            s_rotation = _score_rotation(ind["ret_5d"], ind["ret_20d"])

            score = (
                self.WEIGHTS["drawdown"]  * s_drawdown +
                self.WEIGHTS["rsi"]       * s_rsi      +
                self.WEIGHTS["volume"]    * s_volume    +
                self.WEIGHTS["price"]     * s_price     +
                self.WEIGHTS["rotation"]  * s_rotation
            )

            type_icon = {"commodity": "🛢️", "qdii": "🌐", "astock": "🏭"}.get(
                row.get("etf_type", "astock"), "🏭"
            )
            # RSI 硬性门槛：RSI > 45 时不是真正超卖，跳过
            if ind["rsi"] > 45:
                continue

            records.append({
                "code":        code,
                "name":        name,
                "etf_type":    row.get("etf_type", "astock"),
                "type_icon":   type_icon,
                "price":       row.get("price", np.nan),
                "pct_chg":     row.get("pct_chg", np.nan),
                "amount_wan":  row.get("amount_wan", np.nan),
                "mv_yi":       row.get("mv_yi", np.nan),
                "score":       round(score, 4),
                "drawdown":    round(ind["drawdown"] * 100, 2),
                "rsi":         round(ind["rsi"], 1),
                "rsi_trend":   round(ind["rsi_trend"], 2),
                "vol_ratio":   round(ind["vol_ratio"], 2),
                "ret_5d":      round(ind["ret_5d"] * 100, 2),
                "ret_20d":     round(ind["ret_20d"] * 100, 2),
                "s_drawdown":  round(s_drawdown, 3),
                "s_rsi":       round(s_rsi, 3),
                "s_volume":    round(s_volume, 3),
                "s_price":     round(s_price, 3),
                "s_rotation":  round(s_rotation, 3),
            })

            if sleep_sec > 0:
                time.sleep(sleep_sec)

        if not records:
            print("[ETFBottomFish] 无有效数据")
            return None

        result = pd.DataFrame(records).sort_values("score", ascending=False).head(top_n)
        result = result.reset_index(drop=True)

        # 追加建仓建议
        result["advice"] = result.apply(lambda r: _build_advice(r.to_dict()), axis=1)

        # 按品类统计
        type_counts = result["etf_type"].value_counts().to_dict()
        print(f"\n[ETFBottomFish] ===== 抄底反弹 ETF Top {top_n} =====")
        print(f"  品类分布: A股行业={type_counts.get('astock',0)}  商品={type_counts.get('commodity',0)}  QDII={type_counts.get('qdii',0)}")
        for _, r in result.iterrows():
            print(
                f"  {r['type_icon']} {r['code']} {r['name']:<12} "
                f"score={r['score']:.3f}  "
                f"回调={r['drawdown']:.1f}%  RSI={r['rsi']:.0f}  "
                f"量比={r['vol_ratio']:.2f}  5d={r['ret_5d']:+.1f}%  20d={r['ret_20d']:+.1f}%"
            )

        return result


def format_dingtalk_message(result: pd.DataFrame) -> str:
    """将选股结果格式化为钉钉 Markdown 消息。"""
    today = datetime.date.today().strftime("%m月%d日")
    type_counts = result["etf_type"].value_counts().to_dict() if "etf_type" in result.columns else {}
    a_cnt  = type_counts.get("astock", 0)
    c_cnt  = type_counts.get("commodity", 0)
    q_cnt  = type_counts.get("qdii", 0)

    lines = [f"### 📉 ETF抄底反弹雷达 {today}（提醒）\n"]
    lines.append(f"**策略**：超卖回升+放量 | 🏭A股{a_cnt}只 🛢️商品{c_cnt}只 🌐QDII{q_cnt}只\n")
    lines.append("---")

    for i, r in result.iterrows():
        rank  = i + 1
        icon  = r.get("type_icon", "📊")
        lines.append(
            f"\n**{rank}. {icon}{r['name']}** `{r['code']}`  "
            f"评分 **{r['score']:.2f}**\n"
            f"> 📉 回调 {r['drawdown']:.1f}% | "
            f"RSI {r['rsi']:.0f} | "
            f"量比 {r['vol_ratio']:.2f} | "
            f"5日 {r['ret_5d']:+.1f}% | "
            f"20日 {r['ret_20d']:+.1f}%\n"
            f"> 💡 {r['advice']}"
        )

    lines.append("\n---")
    lines.append("⚠️ 抄底有风险，建议分批建仓，严格止损")
    return "\n".join(lines)
