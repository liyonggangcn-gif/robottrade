"""
统一股票过滤规则：供策略、选股、数据加载复用。

规则（符合 .cursorrules）:
- 市值 < 500M 剔除
- PE <= 0 剔除
- 可选的自定义门槛
"""

import pandas as pd
from typing import Optional

# 默认门槛（与 .cursorrules 一致）
DEFAULT_MIN_MARKET_CAP = 500_000_000  # 5亿
DEFAULT_MIN_PE = 0.01  # PE 必须为正（亏损股 PE<0 或 NaN）


def filter_tradable_stocks(
    df: pd.DataFrame,
    min_market_cap: float = DEFAULT_MIN_MARKET_CAP,
    min_pe: float = DEFAULT_MIN_PE,
    market_cap_col: str = "total_mv",
    pe_col: str = "pe_ttm",
) -> pd.DataFrame:
    """
    剔除不符合交易条件的股票（垃圾数据、异常值）。

    Args:
        df: 股票 DataFrame
        min_market_cap: 最小市值（元），默认 5 亿
        min_pe: 最小 PE，默认 0.01（剔除 PE<=0 亏损股）
        market_cap_col: 市值列名
        pe_col: PE 列名

    Returns:
        过滤后的 DataFrame
    """
    if df.empty:
        return df
    out = df.copy()
    n_before = len(out)

    # 市值过滤
    if market_cap_col in out.columns:
        out = out[
            (out[market_cap_col].isna()) |
            (out[market_cap_col] >= min_market_cap)
        ]
    # PE 过滤（剔除亏损股）
    if pe_col in out.columns:
        out = out[
            (out[pe_col].isna()) |
            (out[pe_col] >= min_pe)
        ]

    n_after = len(out)
    if n_before != n_after:
        removed = n_before - n_after
        # 可选：print(f"[StockFilter] 剔除 {removed} 只不符合条件股票")
    return out.reset_index(drop=True)


def apply_min_filters(df: pd.DataFrame) -> pd.DataFrame:
    """
    应用最小过滤（市值>0、PE>0），用于快速清洗。
    等价于 filter_tradable_stocks(df, min_market_cap=0, min_pe=0.01)
    但市值>0 仍会剔除。
    """
    if df.empty:
        return df
    out = df.copy()
    if "total_mv" in out.columns:
        out = out[(out["total_mv"].isna()) | (out["total_mv"] > 0)]
    if "pe_ttm" in out.columns:
        out = out[(out["pe_ttm"].isna()) | (out["pe_ttm"] > 0)]
    return out.reset_index(drop=True)
