"""
ETFUniverseAgent — 候选池筛选 Agent

职责：
  1. 拉取全市场场内 ETF 列表
  2. 按流动性/规模过滤
  3. 将每只 ETF 分类 (equity/dividend/gold/commodity/bond)
  4. 为有底层指数的 ETF 附上指数代码（用于后续估值）

ETF 分类体系：
  equity    — 宽基/行业指数 ETF，可用 PE/PB 分位估值
  dividend  — 红利/低波 ETF，用股息率分位估值
  gold      — 黄金 ETF，用实际利率 + 金价分位估值
  commodity — 有色/能源/农产品 ETF，用商品价格分位
  bond      — 利率债/信用债 ETF，用收益率分位（反向）
"""
from __future__ import annotations

import re
from typing import Dict, Any, List

import pandas as pd

from .base_agent import BaseAgent

# ── ETF → 底层指数映射表（可持续扩展） ─────────────────────────────────────
# key: ETF代码前缀（去掉交易所前缀），value: dict
ETF_INDEX_MAP: Dict[str, Dict] = {
    # ── 宽基 ────────────────────────────────────────────────────────────────
    "510300": {"index": "000300.SH", "cat": "equity",   "name": "沪深300ETF"},
    "510500": {"index": "000905.SH", "cat": "equity",   "name": "中证500ETF"},
    "512100": {"index": "000852.SH", "cat": "equity",   "name": "中证1000ETF"},
    "510050": {"index": "000016.SH", "cat": "equity",   "name": "上证50ETF"},
    "159915": {"index": "399006.SZ", "cat": "equity",   "name": "创业板ETF"},
    "588000": {"index": "000688.SH", "cat": "equity",   "name": "科创50ETF"},
    "588080": {"index": "000688.SH", "cat": "equity",   "name": "科创50ETF2"},
    "159902": {"index": "000852.SH", "cat": "equity",   "name": "中证1000ETF2"},
    # ── 行业 ────────────────────────────────────────────────────────────────
    "512800": {"index": "399986.SZ", "cat": "equity",   "name": "银行ETF"},
    "512880": {"index": "399975.SZ", "cat": "equity",   "name": "证券ETF"},
    "512010": {"index": "000933.CSI","cat": "equity",   "name": "医药ETF"},
    "159869": {"index": "399364.SZ", "cat": "equity",   "name": "科技ETF"},
    "159781": {"index": "931865.CSI","cat": "equity",   "name": "半导体ETF"},
    "159766": {"index": "000941.CSI","cat": "equity",   "name": "新能源ETF"},
    "516160": {"index": "000932.CSI","cat": "equity",   "name": "消费ETF"},
    "515030": {"index": "000942.CSI","cat": "equity",   "name": "新能源车ETF"},
    "512660": {"index": "000776.SH", "cat": "equity",   "name": "军工ETF"},
    "159992": {"index": "399673.SZ", "cat": "equity",   "name": "创新药ETF"},
    "513050": {"index": "HSTECH",    "cat": "equity",   "name": "中概互联ETF"},
    # ── 红利 ────────────────────────────────────────────────────────────────
    "510880": {"index": "000015.SH", "cat": "dividend", "name": "红利ETF"},
    "159905": {"index": "000922.CSI","cat": "dividend", "name": "中证红利ETF"},
    "512890": {"index": "H20269.CSI","cat": "dividend", "name": "红利低波ETF"},
    "159307": {"index": "000015.SH", "cat": "dividend", "name": "红利ETF2"},
    "515180": {"index": "000015.SH", "cat": "dividend", "name": "红利ETF华夏"},
    # ── 黄金 ────────────────────────────────────────────────────────────────
    "518880": {"index": None,        "cat": "gold",      "name": "黄金ETF华安"},
    "159934": {"index": None,        "cat": "gold",      "name": "黄金ETF易方达"},
    "518800": {"index": None,        "cat": "gold",      "name": "黄金ETF国泰"},
    "159937": {"index": None,        "cat": "gold",      "name": "黄金ETF招商"},
    # ── 商品（有色/能源） ───────────────────────────────────────────────────
    "159980": {"index": None,        "cat": "commodity", "name": "有色ETF",  "commodity": "metals"},
    "159981": {"index": None,        "cat": "commodity", "name": "能源化工ETF", "commodity": "energy"},
    "161129": {"index": None,        "cat": "commodity", "name": "原油LOF",   "commodity": "crude_oil"},
    "159939": {"index": None,        "cat": "commodity", "name": "有色金属ETF","commodity": "metals"},
    # ── 债券 ────────────────────────────────────────────────────────────────
    "511010": {"index": None,        "cat": "bond",      "name": "国债ETF"},
    "511020": {"index": None,        "cat": "bond",      "name": "国债ETF2"},
    "511260": {"index": None,        "cat": "bond",      "name": "十年国债ETF"},
    "511090": {"index": None,        "cat": "bond",      "name": "城投债ETF"},
    "159649": {"index": None,        "cat": "bond",      "name": "政金债ETF"},
}

# 基于名称关键词的分类规则（处理映射表之外的ETF）
_CAT_KEYWORDS = {
    "gold":      ["黄金", "贵金属"],
    "dividend":  ["红利", "低波红利", "高股息"],
    "bond":      ["国债", "政金债", "城投债", "信用债", "债券"],
    "commodity": ["有色", "能源", "原油", "石油", "农产品", "大宗"],
}


class ETFUniverseAgent(BaseAgent):
    """
    返回 dict：
      etf_list: List[dict]  每只 ETF 包含 code/name/category/index_code/amount_wan/mv_yi
    """

    MIN_AMOUNT_WAN = 1000   # 日成交额最低 1000 万
    MIN_MV_YI     = 5.0    # 规模最低 5 亿

    def run(self, mode: str = "balanced", **kwargs) -> Dict[str, Any]:
        # 防守模式：只允许红利/黄金/债券
        allowed_cats = (
            {"dividend", "gold", "bond"}
            if mode == "defensive"
            else {"equity", "dividend", "gold", "commodity", "bond"}
        )

        raw = self._fetch_etf_list()
        if raw is None or raw.empty:
            return {"ok": False, "error": "无法获取ETF列表", "etf_list": []}

        results: List[Dict] = []
        for _, row in raw.iterrows():
            code    = str(row.get("代码", row.get("code", ""))).strip()
            name    = str(row.get("名称", row.get("name", ""))).strip()
            amount  = float(row.get("成交额", row.get("amount", 0)) or 0)  # 元
            mv      = float(row.get("总市值", row.get("mv", 0)) or 0)       # 元

            # 排除杠杆/反向/跨境
            if self._should_exclude(name):
                continue

            # 流动性过滤
            amount_wan = amount / 1e4
            if amount_wan < self.MIN_AMOUNT_WAN:
                continue

            # 规模过滤（有数据时才检查）
            mv_yi = mv / 1e8
            if mv > 0 and mv_yi < self.MIN_MV_YI:
                continue

            # 分类
            pure_code = re.sub(r"^(sh|sz|SH|SZ)", "", code)
            cat_info = ETF_INDEX_MAP.get(pure_code) or self._classify_by_name(name)
            cat = cat_info.get("cat", "equity")

            if cat not in allowed_cats:
                continue

            results.append({
                "code":        code,
                "pure_code":   pure_code,
                "name":        name,
                "category":    cat,
                "index_code":  cat_info.get("index"),
                "commodity":   cat_info.get("commodity"),
                "amount_wan":  round(amount_wan, 1),
                "mv_yi":       round(mv_yi, 2),
                "current_price": float(row.get("最新价", row.get("price", 0)) or 0),
            })

        self.logger.info(f"候选ETF: {len(results)} 只 (模式={mode})")
        return {"etf_list": results, "mode": mode, "count": len(results)}

    def _fetch_etf_list(self) -> pd.DataFrame | None:
        """优先东方财富，降级新浪"""
        try:
            import akshare as ak
            df = ak.fund_etf_spot_em()
            if df is not None and not df.empty:
                # 统一列名
                rename = {"基金代码": "代码", "基金简称": "名称",
                          "成交额": "成交额", "总市值": "总市值", "最新价": "最新价"}
                df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
                return df
        except Exception as e:
            self.logger.warning(f"东方财富ETF失败: {e}")

        try:
            import akshare as ak
            df = ak.fund_etf_category_sina(symbol="ETF基金")
            if df is not None and not df.empty:
                rename = {"symbol": "代码", "name": "名称",
                          "tradeVolume": "成交额", "price": "最新价"}
                df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
                df["总市值"] = 0
                return df
        except Exception as e:
            self.logger.warning(f"新浪ETF失败: {e}")

        return None

    @staticmethod
    def _should_exclude(name: str) -> bool:
        kws = ["杠杆", "反向", "做空", "纳指", "标普", "恒生", "日经",
               "德国", "法国", "美国", "港股通", "沪港深", "QDII"]
        # 港股通ETF也可考虑，但暂时排除简化处理
        return any(k in name for k in kws)

    @staticmethod
    def _classify_by_name(name: str) -> Dict:
        for cat, kws in _CAT_KEYWORDS.items():
            if any(k in name for k in kws):
                return {"cat": cat, "index": None}
        return {"cat": "equity", "index": None}
