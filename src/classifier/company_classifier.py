"""
公司类型分类器
将股票按盈利驱动结构分为6种类型，不同类型适用不同分析框架和估值方法。

类型定义：
  resource       - 资源价格型：盈利主要由大宗商品价格驱动（石化、煤炭、有色）
  brand          - 品牌定价型：依赖品牌溢价和定价权（白酒、消费品）
  growth         - 成长渗透型：依赖行业渗透率提升和市占率扩张（科技、新能源）
  rate_sensitive - 利率敏感型：盈利随利率/信用周期波动（银行、保险）
  policy         - 政策驱动型：强依赖政策补贴和监管导向（军工、环保）
  cashflow       - 稳定现金流型：收入稳定可预期，适合按股息/现金流估值（公用事业）
"""

from typing import Optional
from src.utils.db_utils import DBUtils


# 行业 → 公司类型映射
# 同时覆盖申万一级行业 和 Tushare 实际存储的细分行业名称
_INDUSTRY_TYPE_MAP = {
    # ── 资源价格型 ──
    "石油石化": "resource", "石油":     "resource", "油气":     "resource",
    "煤炭":     "resource",
    "有色金属": "resource", "有色":     "resource", "铜":       "resource",
    "铝":       "resource", "黄金":     "resource", "稀土":     "resource",
    "钢铁":     "resource",
    "基础化工": "resource", "化学原料": "resource", "化学制品": "resource",
    "油气化工": "resource",

    # ── 品牌定价型 ──
    "食品饮料": "brand",    "食品":     "brand",    "白酒":     "brand",
    "饮料":     "brand",    "调味品":   "brand",    "乳制品":   "brand",
    "啤酒":     "brand",    "软饮料":   "brand",
    "家用电器": "brand",    "小家电":   "brand",    "家电":     "brand",
    "商贸零售": "brand",    "零售":     "brand",    "综合商贸": "brand",
    "百货":     "brand",    "超市":     "brand",
    "纺织服装": "brand",    "纺织":     "brand",    "服装":     "brand",
    "轻工制造": "brand",    "家居用品": "brand",    "家具":     "brand",
    "美容护理": "brand",    "化妆品":   "brand",    "个护":     "brand",
    "餐饮":     "brand",    "酒店":     "brand",

    # ── 成长渗透型 ──
    "电子":     "growth",   "元器件":   "growth",   "半导体":   "growth",
    "芯片":     "growth",   "消费电子": "growth",   "面板":     "growth",
    "PCB":      "growth",
    "计算机":   "growth",   "软件":     "growth",   "IT设备":   "growth",
    "互联网":   "growth",   "人工智能": "growth",   "大数据":   "growth",
    "通信":     "growth",   "通信设备": "growth",
    "电力设备": "growth",   "电气设备": "growth",   "新能源":   "growth",
    "光伏":     "growth",   "风电":     "growth",   "储能":     "growth",
    "锂电池":   "growth",   "动力电池": "growth",
    "医药生物": "growth",   "化学制药": "growth",   "中药":     "growth",
    "医疗器械": "growth",   "医疗保健": "growth",   "生物制品": "growth",
    "CRO":      "growth",
    "汽车":     "growth",   "汽车整车": "growth",   "汽车零部件":"growth",
    "机械设备": "growth",   "专用机械": "growth",   "通用机械": "growth",
    "精密":     "growth",   "仪器":     "growth",   "机器人":   "growth",

    # ── 政策驱动型 ──
    "国防军工": "policy",   "军工":     "policy",   "航空":     "policy",
    "航天":     "policy",   "船舶":     "policy",
    "农林牧渔": "policy",   "农业":     "policy",   "农药":     "policy",
    "农牧":     "policy",   "农业综合": "policy",
    "建筑材料": "policy",   "建材":     "policy",
    "建筑装饰": "policy",   "建筑":     "policy",
    "环保":     "policy",
    "社会服务": "policy",

    # ── 利率敏感型 ──
    "银行":     "rate_sensitive",
    "非银金融": "rate_sensitive", "证券":     "rate_sensitive",
    "保险":     "rate_sensitive", "多元金融": "rate_sensitive",
    "房地产":   "rate_sensitive",

    # ── 稳定现金流型 ──
    "公用事业": "cashflow",  "电力":     "cashflow",  "水务":     "cashflow",
    "燃气":     "cashflow",
    "交通运输": "cashflow",  "物流":     "cashflow",  "快递":     "cashflow",
    "港口":     "cashflow",  "机场":     "cashflow",  "高速":     "cashflow",
    "传媒":     "cashflow",
}

# 各类型的中文名和核心描述
TYPE_META = {
    "resource": {
        "name": "资源价格型",
        "drivers": ["大宗商品价格", "产销量", "加工价差"],
        "valuation": "PB历史分位 + 商品价格分位",
        "buy_signal": "商品价格历史分位 < 25% AND PB < 历史均值",
        "wrong_metric": "当期PE（周期底部PE最高反而是买点）",
    },
    "brand": {
        "name": "品牌定价型",
        "drivers": ["定价权", "渠道库存健康度", "消费者复购率"],
        "valuation": "PE历史分位（5年）+ FCF yield",
        "buy_signal": "PE历史分位 < 25% AND 渠道库存正常",
        "wrong_metric": "绝对PE（需横向比较历史分位）",
    },
    "growth": {
        "name": "成长渗透型",
        "drivers": ["行业渗透率", "市场份额", "单位成本下降曲线"],
        "valuation": "PEG（成长期） → PE分位（成熟期）",
        "buy_signal": "PEG < 1.0 AND 市占率稳定或提升",
        "wrong_metric": "静态PE（早期可能亏损，PE无意义）",
    },
    "rate_sensitive": {
        "name": "利率敏感型",
        "drivers": ["净息差NIM", "资产质量", "信贷规模"],
        "valuation": "PB/ROE + NIM趋势",
        "buy_signal": "PB < 1 AND NIM企稳 AND 不良率 < 2%",
        "wrong_metric": "PE（利润受拨备影响大，PE失真）",
    },
    "policy": {
        "name": "政策驱动型",
        "drivers": ["政策支持力度", "行业景气度", "竞争格局"],
        "valuation": "政策周期位置 + PEG",
        "buy_signal": "政策加码信号 AND 行业排产上升",
        "wrong_metric": "单独看估值（政策可快速改变基本面）",
    },
    "cashflow": {
        "name": "稳定现金流型",
        "drivers": ["流量/电量", "受监管费率", "折旧后现金释放"],
        "valuation": "股息率 + EV/EBITDA",
        "buy_signal": "股息率 > 无风险利率 × 1.5 AND 负债率可控",
        "wrong_metric": "PE（折旧等会导致净利润失真）",
    },
}


class CompanyClassifier:
    """公司类型分类器"""

    def classify(self, ts_code: str, industry: Optional[str] = None) -> str:
        """
        识别公司类型。
        优先使用传入的 industry，否则从数据库查询。
        未能识别时返回 "growth"（最通用的兜底类型）。
        """
        if not industry:
            industry = self._get_industry(ts_code)

        if industry:
            # 精确匹配
            company_type = _INDUSTRY_TYPE_MAP.get(industry)
            if company_type:
                return company_type

            # 模糊匹配（处理行业名称变体）
            for key, ctype in _INDUSTRY_TYPE_MAP.items():
                if key in industry or industry in key:
                    return ctype

        return "growth"  # 兜底

    def classify_batch(self, ts_codes: list) -> dict:
        """批量分类，返回 {ts_code: company_type}"""
        # 一次性从 DB 拉取所有行业信息
        if not ts_codes:
            return {}

        placeholders = ",".join(["?" for _ in ts_codes])
        df = DBUtils.query_df(
            f"SELECT ts_code, industry FROM stock_info WHERE ts_code IN ({placeholders})",
            params=ts_codes,
        )

        industry_map = dict(zip(df["ts_code"], df["industry"])) if not df.empty else {}

        return {
            code: self.classify(code, industry_map.get(code))
            for code in ts_codes
        }

    def get_type_meta(self, company_type: str) -> dict:
        """返回类型的元数据（名称、驱动因子、估值方法等）"""
        return TYPE_META.get(company_type, TYPE_META["growth"])

    def get_type_name(self, company_type: str) -> str:
        return TYPE_META.get(company_type, {}).get("name", company_type)

    def _get_industry(self, ts_code: str) -> Optional[str]:
        try:
            df = DBUtils.query_df(
                "SELECT industry FROM stock_info WHERE ts_code = ?",
                params=[ts_code],
            )
            if not df.empty and df["industry"].iloc[0]:
                return df["industry"].iloc[0]
        except Exception:
            pass
        return None
