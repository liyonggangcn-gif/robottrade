"""
ETF 多策略套件

除了抄底反弹（etf_bottom_fish_strategy.py），还有以下套路：

┌──────────────────────────────────────────────────────────────┐
│  策略          核心逻辑                    适用市场环境       │
│  ─────────    ─────────────────────────  ──────────────────  │
│  抄底反弹     超卖回升 + 放量             震荡/底部反转       │
│  趋势动量     强者恒强，追涨前 N 板块     单边上涨趋势        │
│  双重动量     绝对动量(跑赢现金) +        趋势确认后入场      │
│               相对动量(行业比较)                              │
│  资金净流入   量价背离：价稳量增          主力建仓阶段        │
│  波动率择时   高波动时防御，低时进攻      危机/反转节点       │
│  AH溢价套利   A/H 折溢价均值回归         A股与港股价差修复   │
└──────────────────────────────────────────────────────────────┘
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

from src.strategy.etf_bottom_fish_strategy import (
    _fetch_etf_list, _filter_etf, _fetch_hist,
)


# ══════════════════════════════════════════════
# 策略 2：趋势动量（强者恒强）
# ══════════════════════════════════════════════

class ETFMomentumStrategy:
    """
    趋势动量策略：买近期表现最强的行业/主题 ETF。

    信号：
      - 近 20 日收益 > 0（正动量）
      - 近 5 日收益 > 0（短期持续）
      - 相对大盘（沪深 300）超额收益最大
      - 成交额持续放大（量价配合趋势）

    适合市场：单边上涨、行业轮动初期
    """

    WEIGHTS = {
        "ret_20d":     0.35,   # 中期动量
        "ret_5d":      0.25,   # 短期动量
        "excess_ret":  0.25,   # 相对大盘超额
        "vol_trend":   0.15,   # 量价趋势
    }

    def run(self, top_n: int = 6, hist_days: int = 60,
            sleep_sec: float = 0.3) -> Optional[pd.DataFrame]:
        print("[ETFMomentum] 步骤1：获取 ETF 列表...")
        spot = _fetch_etf_list()
        if spot is None:
            return None
        spot = _filter_etf(spot)

        # 获取沪深 300 近期收益作为基准
        hs300_ret_20d = self._get_benchmark_ret(20)
        hs300_ret_5d  = self._get_benchmark_ret(5)

        print(f"[ETFMomentum] 步骤2：计算动量指标（{len(spot)} 只 ETF）...")
        records = []
        for _, row in spot.iterrows():
            hist = _fetch_hist(row["code"], days=hist_days)
            if hist is None or len(hist) < 25:
                continue
            close  = hist["close"]
            volume = hist["volume"]

            ret_5d  = float((close.iloc[-1] - close.iloc[-6])  / close.iloc[-6])  if len(close) >= 6  else 0.0
            ret_20d = float((close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]) if len(close) >= 21 else 0.0

            # 过滤：必须正动量
            if ret_20d <= 0 or ret_5d <= 0:
                continue

            excess_ret = ret_20d - hs300_ret_20d

            # 量价趋势：近 5 日均量 vs 近 20 日均量
            vol5   = volume.iloc[-5:].mean()  if len(volume) >= 5  else np.nan
            vol20  = volume.iloc[-20:].mean() if len(volume) >= 20 else np.nan
            vol_ratio = float(vol5 / vol20) if (vol20 and vol20 > 0) else 1.0

            # 打分（均归一化到 0-1 后加权，此处用相对排名，先收集原始值）
            records.append({
                "code":       row["code"],
                "name":       row["name"],
                "price":      row.get("price", np.nan),
                "pct_chg":    row.get("pct_chg", np.nan),
                "amount_wan": row.get("amount_wan", np.nan),
                "ret_5d":     round(ret_5d * 100, 2),
                "ret_20d":    round(ret_20d * 100, 2),
                "excess_ret": round(excess_ret * 100, 2),
                "vol_ratio":  round(vol_ratio, 2),
            })
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        if not records:
            return None

        df = pd.DataFrame(records)

        # 排名归一化（越高越好）
        for col in ["ret_20d", "ret_5d", "excess_ret", "vol_ratio"]:
            df[f"rank_{col}"] = df[col].rank(pct=True)

        df["score"] = (
            self.WEIGHTS["ret_20d"]    * df["rank_ret_20d"] +
            self.WEIGHTS["ret_5d"]     * df["rank_ret_5d"] +
            self.WEIGHTS["excess_ret"] * df["rank_excess_ret"] +
            self.WEIGHTS["vol_trend"]  * df["rank_vol_ratio"]
        )

        result = df.sort_values("score", ascending=False).head(top_n).reset_index(drop=True)
        result["advice"] = result.apply(
            lambda r: f"追涨仓位20-30%，止损设近5日最低价下方2%，目标+10%", axis=1
        )

        print(f"\n[ETFMomentum] ===== 趋势动量 ETF Top {top_n} =====")
        for _, r in result.iterrows():
            print(f"  {r['code']} {r['name']:<12} score={r['score']:.3f}  "
                  f"5d={r['ret_5d']:+.1f}%  20d={r['ret_20d']:+.1f}%  "
                  f"超额={r['excess_ret']:+.1f}%  量比={r['vol_ratio']:.2f}")
        return result

    def _get_benchmark_ret(self, days: int) -> float:
        """获取沪深 300 N 日收益率。"""
        try:
            df = ak.index_zh_a_hist(symbol="000300", period="daily",
                                    start_date=(datetime.date.today() -
                                                datetime.timedelta(days=days + 10)).strftime("%Y%m%d"),
                                    end_date=datetime.date.today().strftime("%Y%m%d"))
            close = pd.to_numeric(df["收盘"], errors="coerce").dropna()
            if len(close) >= days + 1:
                return float((close.iloc[-1] - close.iloc[-(days+1)]) / close.iloc[-(days+1)])
        except Exception:
            pass
        return 0.0


# ══════════════════════════════════════════════
# 策略 3：双重动量（Dual Momentum）
# ══════════════════════════════════════════════

class ETFDualMomentumStrategy:
    """
    双重动量策略（Gary Antonacci 改编版）：

    第一关：绝对动量 — ETF 近 3 个月收益 > 货币基金收益（约 1.5%）
             不通过 → 持有货币基金（保本）
    第二关：相对动量 — 在通过绝对动量的 ETF 中，按 3 月收益排名，选 Top K

    优势：趋势跟踪 + 防崩溃（绝对动量保护）
    适合市场：有明显趋势的行情，回撤控制好
    """

    CASH_RETURN_3M = 0.015   # 货币基金 3 月参考收益（约 1.5%）

    def run(self, top_n: int = 5, hist_days: int = 90,
            sleep_sec: float = 0.3) -> Optional[pd.DataFrame]:
        print("[DualMomentum] 获取 ETF 列表...")
        spot = _fetch_etf_list()
        if spot is None:
            return None
        spot = _filter_etf(spot)
        # 取成交额 Top 150 提高运算效率
        spot = spot.sort_values("amount_wan", ascending=False).head(150)

        records = []
        for _, row in spot.iterrows():
            hist = _fetch_hist(row["code"], days=hist_days + 10)
            if hist is None or len(hist) < hist_days:
                continue
            close  = hist["close"]

            ret_3m = float((close.iloc[-1] - close.iloc[0]) / close.iloc[0])
            ret_1m = float((close.iloc[-1] - close.iloc[-22]) / close.iloc[-22]) if len(close) >= 22 else 0.0
            ret_5d = float((close.iloc[-1] - close.iloc[-6])  / close.iloc[-6])  if len(close) >= 6  else 0.0

            # 第一关：绝对动量
            if ret_3m <= self.CASH_RETURN_3M:
                continue

            records.append({
                "code":     row["code"],
                "name":     row["name"],
                "price":    row.get("price", np.nan),
                "pct_chg":  row.get("pct_chg", np.nan),
                "amount_wan": row.get("amount_wan", np.nan),
                "ret_3m":   round(ret_3m * 100, 2),
                "ret_1m":   round(ret_1m * 100, 2),
                "ret_5d":   round(ret_5d * 100, 2),
                "score":    ret_3m * 0.6 + ret_1m * 0.3 + ret_5d * 0.1,
            })
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        if not records:
            print("[DualMomentum] 无 ETF 通过绝对动量过滤，建议持有货币基金")
            return pd.DataFrame()

        df = pd.DataFrame(records).sort_values("score", ascending=False).head(top_n)
        df["advice"] = "等权配置，每月末换仓一次"
        df = df.reset_index(drop=True)

        print(f"\n[DualMomentum] ===== 双重动量 ETF Top {top_n} =====")
        for _, r in df.iterrows():
            print(f"  {r['code']} {r['name']:<12}  "
                  f"3m={r['ret_3m']:+.1f}%  1m={r['ret_1m']:+.1f}%  5d={r['ret_5d']:+.1f}%")
        return df


# ══════════════════════════════════════════════
# 策略 4：资金净流入（量价背离型）
# ══════════════════════════════════════════════

class ETFSmartMoneyStrategy:
    """
    资金净流入策略：价格横盘/微跌但成交量放大 → 主力建仓信号。

    核心指标：
      - 量价背离度：近 5 日价格变化 < 0 但成交量 > 均量（主力吸筹）
      - OBV 趋势：累积的能量柱方向向上
      - 换手率：适中（过低=无人关注，过高=短期炒作）
    """

    def run(self, top_n: int = 6, hist_days: int = 60,
            sleep_sec: float = 0.3) -> Optional[pd.DataFrame]:
        print("[SmartMoney] 获取 ETF 列表...")
        spot = _fetch_etf_list()
        if spot is None:
            return None
        spot = _filter_etf(spot)
        spot = spot.sort_values("amount_wan", ascending=False).head(200)

        records = []
        for _, row in spot.iterrows():
            hist = _fetch_hist(row["code"], days=hist_days)
            if hist is None or len(hist) < 25:
                continue
            close  = hist["close"]
            volume = hist["volume"]

            ret_5d  = float((close.iloc[-1] - close.iloc[-6])  / close.iloc[-6])  if len(close) >= 6  else 0.0
            vol5    = volume.iloc[-5:].mean()
            vol20   = volume.iloc[-20:].mean() if len(volume) >= 20 else vol5
            vol_ratio = float(vol5 / vol20) if vol20 > 0 else 1.0

            # OBV：量 × 涨跌方向累积
            price_diff = close.diff()
            obv = (volume * np.sign(price_diff)).cumsum()
            obv_trend_5d  = float(obv.iloc[-1] - obv.iloc[-6])  if len(obv) >= 6  else 0.0
            obv_trend_20d = float(obv.iloc[-1] - obv.iloc[-21]) if len(obv) >= 21 else 0.0

            # 信号：价横/跌 + 量增 + OBV 向上（主力进场）
            price_stable = -0.03 < ret_5d < 0.02
            volume_surge = vol_ratio >= 1.15
            obv_rising   = obv_trend_5d > 0 and obv_trend_20d > 0

            if not (price_stable and volume_surge and obv_rising):
                continue

            # 打分：量价背离程度
            score = vol_ratio * 0.4 + (obv_trend_5d / (abs(obv_trend_20d) + 1e-9)) * 0.3 + \
                    max(0, -ret_5d) * 10 * 0.3  # 微跌但量增得分更高

            records.append({
                "code":        row["code"],
                "name":        row["name"],
                "price":       row.get("price", np.nan),
                "pct_chg":     row.get("pct_chg", np.nan),
                "amount_wan":  row.get("amount_wan", np.nan),
                "vol_ratio":   round(vol_ratio, 2),
                "ret_5d":      round(ret_5d * 100, 2),
                "obv_trend":   "上升" if obv_rising else "下降",
                "score":       round(score, 4),
                "advice":      f"分批建仓，首仓20%，量能持续放大后加仓",
            })
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        if not records:
            return None

        df = pd.DataFrame(records).sort_values("score", ascending=False).head(top_n).reset_index(drop=True)
        print(f"\n[SmartMoney] ===== 资金净流入 ETF Top {top_n} =====")
        for _, r in df.iterrows():
            print(f"  {r['code']} {r['name']:<12} score={r['score']:.3f}  "
                  f"量比={r['vol_ratio']:.2f}  5d={r['ret_5d']:+.1f}%  OBV={r['obv_trend']}")
        return df


# ══════════════════════════════════════════════
# 策略 5：波动率择时（防御/进攻切换）
# ══════════════════════════════════════════════

class ETFVolatilityTimingStrategy:
    """
    波动率择时策略：
    - 市场波动率高（恐慌期）→ 持有防御型 ETF（国债、消费、医疗）
    - 市场波动率低（平静期）→ 持有进攻型 ETF（科技、新能源、周期）

    波动率信号：用沪深 300 近 20 日收益的标准差（年化）
    阈值：年化波动率 > 25% → 防御；< 15% → 进攻；中间 → 均衡
    """

    DEFENSIVE_KEYWORDS  = ["国债", "债券", "消费", "医疗", "医药", "食品", "公用", "银行"]
    AGGRESSIVE_KEYWORDS = ["科技", "芯片", "半导体", "新能源", "人工智能", "机器人", "成长",
                           "创业", "军工", "周期", "有色", "钢铁"]
    BALANCED_KEYWORDS   = ["沪深300", "中证500", "全市场", "宽基"]

    HIGH_VOL_THRESHOLD = 0.25   # 高波动阈值（年化）
    LOW_VOL_THRESHOLD  = 0.15   # 低波动阈值（年化）

    def run(self, top_n: int = 5, sleep_sec: float = 0.3) -> dict:
        # 计算市场波动率
        vol_annualized = self._calc_market_vol()
        if vol_annualized > self.HIGH_VOL_THRESHOLD:
            mode = "defensive"
            mode_cn = f"⚠️ 防御模式（市场波动率={vol_annualized:.1%} > 25%）"
            target_kws = self.DEFENSIVE_KEYWORDS
        elif vol_annualized < self.LOW_VOL_THRESHOLD:
            mode = "aggressive"
            mode_cn = f"🚀 进攻模式（市场波动率={vol_annualized:.1%} < 15%）"
            target_kws = self.AGGRESSIVE_KEYWORDS
        else:
            mode = "balanced"
            mode_cn = f"⚖️ 均衡模式（市场波动率={vol_annualized:.1%}，15-25%）"
            target_kws = self.BALANCED_KEYWORDS

        print(f"[VolTiming] 当前市场状态: {mode_cn}")

        # 按当前模式过滤 ETF
        spot = _fetch_etf_list()
        if spot is None:
            return {"mode": mode, "vol": vol_annualized, "picks": None}
        spot = _filter_etf(spot)

        mask = spot["name"].apply(lambda n: any(kw in str(n) for kw in target_kws))
        filtered = spot[mask].sort_values("amount_wan", ascending=False).head(top_n * 3)

        records = []
        for _, row in filtered.iterrows():
            hist = _fetch_hist(row["code"], days=30)
            if hist is None or len(hist) < 10:
                continue
            close = hist["close"]
            vol   = hist["volume"]
            ret_5d = float((close.iloc[-1] - close.iloc[-6]) / close.iloc[-6]) if len(close) >= 6 else 0.0
            vol5  = vol.iloc[-5:].mean() if len(vol) >= 5 else 1
            vol20 = vol.iloc[-20:].mean() if len(vol) >= 20 else vol5
            records.append({
                "code":       row["code"],
                "name":       row["name"],
                "amount_wan": row.get("amount_wan", 0),
                "ret_5d":     round(ret_5d * 100, 2),
                "vol_ratio":  round(vol5 / vol20 if vol20 > 0 else 1.0, 2),
            })
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        if not records:
            return {"mode": mode, "vol": vol_annualized, "picks": None}

        df = (pd.DataFrame(records)
              .sort_values("amount_wan", ascending=False)
              .head(top_n).reset_index(drop=True))

        print(f"[VolTiming] ===== {mode_cn} Top {top_n} =====")
        for _, r in df.iterrows():
            print(f"  {r['code']} {r['name']:<12}  5d={r['ret_5d']:+.1f}%  量比={r['vol_ratio']:.2f}")

        return {"mode": mode, "vol": vol_annualized, "picks": df, "mode_cn": mode_cn}

    def _calc_market_vol(self) -> float:
        """计算沪深 300 近 20 日年化波动率。"""
        try:
            end   = datetime.date.today().strftime("%Y%m%d")
            start = (datetime.date.today() - datetime.timedelta(days=40)).strftime("%Y%m%d")
            df = ak.index_zh_a_hist(symbol="000300", period="daily",
                                    start_date=start, end_date=end)
            close = pd.to_numeric(df["收盘"], errors="coerce").dropna()
            daily_ret = close.pct_change().dropna().iloc[-20:]
            return float(daily_ret.std() * np.sqrt(252))
        except Exception:
            return 0.18   # 默认均衡


# ══════════════════════════════════════════════
# 综合选策略 Runner
# ══════════════════════════════════════════════

def run_all_strategies(top_n: int = 5) -> dict:
    """
    一键运行全部策略并汇总。
    返回：{'bottom_fish': df, 'momentum': df, 'dual_momentum': df,
           'smart_money': df, 'vol_timing': {...}}
    """
    from src.strategy.etf_bottom_fish_strategy import ETFBottomFishStrategy
    results = {}

    print("\n" + "="*60)
    print("  ETF 多策略扫描")
    print("="*60)

    strategies = [
        ("bottom_fish",   "抄底反弹",   ETFBottomFishStrategy(),    {}),
        ("momentum",      "趋势动量",   ETFMomentumStrategy(),      {}),
        ("dual_momentum", "双重动量",   ETFDualMomentumStrategy(),  {}),
        ("smart_money",   "资金净流入", ETFSmartMoneyStrategy(),    {}),
    ]

    for key, name, strat, kwargs in strategies:
        print(f"\n▶ 运行策略：{name}")
        try:
            results[key] = strat.run(top_n=top_n, **kwargs)
        except Exception as e:
            print(f"  [WARN] {name} 运行失败: {e}")
            results[key] = None

    print("\n▶ 运行策略：波动率择时")
    try:
        results["vol_timing"] = ETFVolatilityTimingStrategy().run(top_n=top_n)
    except Exception as e:
        print(f"  [WARN] 波动率择时运行失败: {e}")
        results["vol_timing"] = None

    return results
