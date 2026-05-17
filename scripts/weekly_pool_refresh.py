"""
每周股票池刷新脚本

V5 逻辑 —— 两类入池策略 + 强制入池规则，目标 1200-1400 只
  股票池 = 市场上值得长期关注的公司集合（不考虑股价高低）

两类板块策略：
  ┌──────────────┬────────────────────────────────────────────────────┐
  │ 策略          │ 板块                      │ 入池逻辑              │
  ├──────────────┼───────────────────────────┼──────────────────────┤
  │ 质量优先      │ 消费 / 金融 / 公用事业    │ ROE+毛利率门槛，选最好 │
  │ 覆盖优先      │ 科技 / 医药 / 新能源      │ 不设盈利门槛，按市值   │
  │              │ 高端制造 / 资源能源        │ 全覆盖重要公司         │
  └──────────────┴───────────────────────────┴──────────────────────┘

硬性淘汰（所有板块）：
  - ST / 退市风险
  - 总市值 < 30亿（微盘股）
  - 日均成交额 < 1000万（流动性极差）

覆盖优先板块额外排除：
  - 连续3年净利润为负（确实不是好公司）
  - PE > 500（严重透支，炒作股）

质量优先板块门槛（不看PE/PB）：
  消费（品牌型）     近3年均ROE ≥ 10%，毛利率 ≥ 20%
  金融（银行/保险）  近3年均ROE ≥ 8%
  公用事业          近3年均ROE ≥ 6%

目标规模（每板块上限）：
  科技:    200    医药医疗: 180    新能源:   150
  高端制造: 150    资源能源: 120    消费:     150
  金融:     60    公用事业:  80
  其他:     50
  合计 ≈ 1140，去掉已在池中的后约 800-1000 只

用法：
  python scripts/weekly_pool_refresh.py --dry-run          # 只看结果不写库
  python scripts/weekly_pool_refresh.py --rebuild          # 清空旧池重建
  python scripts/weekly_pool_refresh.py --auto-add         # 增量追加
"""

import sys, os, argparse, json
from datetime import date, timedelta

BIGCAP_THRESHOLD_WAN = 10_000_000   # 1000亿（万元单位）
ROCKET_THRESHOLD = 3.0              # 年内3倍涨幅

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from src.utils.db_utils import DBUtils
from src.classifier.company_classifier import CompanyClassifier
from src.universe.stock_pool import StockPool


# ────────────────────────────────────────────────────────────
# 板块定义 & 入池策略
# ────────────────────────────────────────────────────────────

SECTOR_MAP = {
    "消费": [
        "白酒", "食品", "饮料", "调味品", "家电", "零售", "商贸", "餐饮", "服装",
        "纺织", "家居", "化妆品", "个护", "乳制品", "啤酒", "软饮料", "百货",
        "超市", "农牧", "烟草", "家用电器", "小家电",
    ],
    "医药医疗": [
        "医药", "生物", "医疗", "制药", "器械", "CRO", "CMO", "诊断", "疫苗",
        "中药", "西药", "医院", "健康", "化学制药", "中成药", "血液制品",
    ],
    "科技": [
        "半导体", "芯片", "电子", "软件", "计算机", "通信", "互联网", "游戏",
        "人工智能", "云计算", "大数据", "信息技术", "网络", "IT", "数字",
        "面板", "PCB", "消费电子", "元器件", "光学", "传感器", "航天信息",
    ],
    "金融": [
        "银行", "保险", "券商", "证券", "信托", "多元金融", "期货",
    ],
    "新能源": [
        "光伏", "风电", "储能", "新能源", "锂电池", "动力电池", "充电桩",
        "氢能", "特高压", "绿电", "电力设备", "电气设备", "逆变器",
    ],
    "高端制造": [
        "军工", "航空", "航天", "船舶", "精密", "仪器", "机器人", "自动化",
        "工业", "装备", "重工", "机械", "轨道交通", "工程机械", "专用机械",
        "通用机械", "商业航天", "低空经济", "无人机",
        "汽车", "新能源车", "整车", "零部件",
    ],
    "资源能源": [
        "煤炭", "有色", "铜", "铝", "黄金", "石油", "天然气", "石化",
        "化工", "钢铁", "铁矿", "锂", "稀土", "矿产", "化学原料",
        "油气", "化学制品",
    ],
    "公用事业": [
        "电力", "水务", "燃气", "环保", "交通", "高速", "港口", "机场",
        "物流", "快递", "水电", "核电", "热力",
    ],
}

def map_to_sector(industry: str) -> str:
    if not industry or pd.isna(industry):
        return "其他"
    for sector, keywords in SECTOR_MAP.items():
        for kw in keywords:
            if kw in str(industry):
                return sector
    return "其他"


# 每板块最大入池数量
SECTOR_LIMITS = {
    "科技":    200,
    "医药医疗": 180,
    "新能源":   150,
    "高端制造": 150,
    "资源能源": 120,
    "消费":    150,
    "金融":     60,
    "公用事业":  80,
    "其他":     50,
}

# 覆盖优先板块：不设盈利门槛，按市值/质量排序全覆盖
COVERAGE_SECTORS = {"科技", "医药医疗", "新能源", "高端制造", "资源能源", "其他"}

# 质量优先板块：保留基本质量门槛
QUALITY_SECTORS = {"消费", "金融", "公用事业"}

# 质量门槛（仅对质量优先板块生效）
QUALITY_THRESHOLDS = {
    "消费":    {"avg_roe": 10.0, "avg_gpr": 20.0},
    "金融":    {"avg_roe":  8.0, "avg_gpr":   0.0},
    "公用事业": {"avg_roe":  6.0, "avg_gpr":   0.0},
}


# ────────────────────────────────────────────────────────────
# 硬性淘汰
# ────────────────────────────────────────────────────────────

def apply_hard_filter(df: pd.DataFrame) -> pd.DataFrame:
    n0 = len(df)
    counts = {}

    if "name" in df.columns:
        before = len(df)
        df = df[~df["name"].str.contains(r"ST|退市|B股|C股", na=False, regex=True)]
        counts["ST/退市"] = before - len(df)

    if "total_mv" in df.columns:
        before = len(df)
        # 市值 < 30亿（万元单位：30亿 = 300000万）
        df = df[df["total_mv"].isna() | (df["total_mv"] >= 30 * 10000)]
        counts["市值<30亿"] = before - len(df)

    if "avg_amount" in df.columns:
        before = len(df)
        # 日均成交额 < 1000万（千元单位：1000万 = 10000千元）
        df = df[df["avg_amount"].isna() | (df["avg_amount"] >= 10000)]
        counts["成交<1000万"] = before - len(df)

    n1 = len(df)
    detail = "  ".join(f"{k}:{v}" for k, v in counts.items() if v > 0)
    print(f"[硬性淘汰] {n0} → {n1}（过滤 {n0 - n1} 只  {detail}）")
    return df.reset_index(drop=True)


# ────────────────────────────────────────────────────────────
# 近3年质量指标（批量）
# ────────────────────────────────────────────────────────────

def compute_quality_metrics(ts_codes: list) -> pd.DataFrame:
    three_years_ago = (date.today() - timedelta(days=3 * 366)).strftime("%Y-%m-%d")
    BATCH = 500
    all_results = []
    for i in range(0, len(ts_codes), BATCH):
        batch = ts_codes[i: i + BATCH]
        codes_str = ",".join(f"'{c}'" for c in batch)
        sql = f"""
            SELECT ts_code, trade_date, roe, netprofit_yoy, gpr
            FROM stock_daily
            WHERE ts_code IN ({codes_str})
              AND trade_date >= '{three_years_ago}'
              AND (roe IS NOT NULL OR netprofit_yoy IS NOT NULL OR gpr IS NOT NULL)
        """
        df = DBUtils.query_df(sql)
        if not df.empty:
            all_results.append(df)

    if not all_results:
        return pd.DataFrame(columns=[
            "ts_code", "avg_roe_3y", "roe_min_3y", "roe_std_3y",
            "avg_netprofit_yoy_3y", "pos_growth_years", "neg_growth_years",
            "avg_gpr_3y",
        ])

    df = pd.concat(all_results, ignore_index=True)
    for col in ["roe", "netprofit_yoy", "gpr"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["year"] = df["trade_date"].astype(str).str[:4]
    year_end = (
        df.sort_values("trade_date")
        .groupby(["ts_code", "year"])
        .last()
        .reset_index()
    )

    def agg_metrics(g):
        roe_v = g["roe"].dropna()
        yoy_v = g["netprofit_yoy"].dropna()
        gpr_v = g["gpr"].dropna()
        return pd.Series({
            "avg_roe_3y":           roe_v.mean()  if len(roe_v) >= 1 else np.nan,
            "roe_min_3y":           roe_v.min()   if len(roe_v) >= 2 else (roe_v.mean() if len(roe_v) == 1 else np.nan),
            "roe_std_3y":           roe_v.std()   if len(roe_v) >= 2 else 0.0,
            "avg_netprofit_yoy_3y": yoy_v.mean()  if len(yoy_v) >= 1 else np.nan,
            "pos_growth_years":     int((yoy_v > 0).sum())  if len(yoy_v) >= 1 else 0,
            "neg_growth_years":     int((yoy_v < 0).sum())  if len(yoy_v) >= 1 else 0,
            "avg_gpr_3y":           gpr_v.mean()  if len(gpr_v) >= 1 else np.nan,
        })

    return year_end.groupby("ts_code").apply(agg_metrics).reset_index()


# ────────────────────────────────────────────────────────────
# 覆盖优先：基本排除 + 质量评分
# ────────────────────────────────────────────────────────────

def coverage_filter(df: pd.DataFrame) -> pd.DataFrame:
    """覆盖优先板块：只排除明显垃圾股，按市值+质量打分排序。"""
    # 排除连续3年净利润为负（确实不值得关注）
    if "neg_growth_years" in df.columns and "pos_growth_years" in df.columns:
        # 近3年全负增长 且 avg_roe < -5%
        bad_mask = (
            (df["neg_growth_years"] >= 3) &
            (df["avg_roe_3y"].notna()) & (df["avg_roe_3y"] < -5)
        )
        df = df[~bad_mask]

    # 排除极端高PE（>500，严重泡沫）
    if "pe_ttm" in df.columns:
        df = df[df["pe_ttm"].isna() | (df["pe_ttm"] <= 500) | (df["pe_ttm"] <= 0)]

    return df


def coverage_score(row: pd.Series) -> float:
    """覆盖优先评分：市值(50%) + 盈利质量(25%) + 增速(25%)

    对于无财务数据的股票，给予中性分（而非0），避免大市值公司被小市值有ROE数据的公司挤出。
    """
    log_mv  = np.log(max(row.get("total_mv") or 1, 1))
    roe_raw = row.get("avg_roe_3y")     # None/NaN → 无数据
    yoy_raw = row.get("avg_netprofit_yoy_3y")
    pos_yrs = row.get("pos_growth_years") or 0
    gpr_raw = row.get("avg_gpr_3y")

    mv_score = min(log_mv / 18 * 50, 50)   # 市值权重提升到50%

    # 有数据时正常计分；无数据时给中性分（15/25/10/15 各半满）
    if roe_raw is not None and not np.isnan(float(roe_raw)):
        roe_score = min(max(float(roe_raw), -10) / 30 * 25 + 8, 25)
    else:
        roe_score = 12  # 中性：相当于ROE≈5%水平

    if gpr_raw is not None and not np.isnan(float(gpr_raw)):
        gpr_score = min(float(gpr_raw) / 80 * 10, 10)
    else:
        gpr_score = 5   # 中性

    if yoy_raw is not None and not np.isnan(float(yoy_raw)):
        yoy_score = min(max(float(yoy_raw), -20) / 100 * 15 + 5, 25)
        yoy_score += pos_yrs * 1.5
    else:
        yoy_score = 8   # 中性

    return min(mv_score + roe_score + gpr_score + yoy_score, 100)


# ────────────────────────────────────────────────────────────
# 质量优先：严格门槛 + 质量评分
# ────────────────────────────────────────────────────────────

def quality_filter(df: pd.DataFrame, sector: str) -> pd.DataFrame:
    """质量优先板块：需要通过 ROE/毛利率 门槛。

    对于无财务数据的股票（ROE=NaN），使用 stock_info 的 pe_ttm 和 pb 作为替代筛选：
    PE > 0 且 PB > 0 表示公司盈利，视为通过 ROE 门槛。
    """
    thresholds = QUALITY_THRESHOLDS.get(sector, {})
    min_roe = thresholds.get("avg_roe", 6.0)
    min_gpr = thresholds.get("avg_gpr", 0.0)

    mask = pd.Series(True, index=df.index)

    if "avg_roe_3y" in df.columns:
        # 有ROE数据的按门槛过滤；无ROE数据的按PE>0(即盈利)或大市值来判断
        has_roe = df["avg_roe_3y"].notna()
        roe_pass = has_roe & (df["avg_roe_3y"] >= min_roe)
        no_roe_pass = ~has_roe & (
            (df.get("pe_ttm", pd.Series(0.0, index=df.index)) > 0) |
            (df.get("total_mv", pd.Series(0.0, index=df.index)) >= 30_000_000)  # 3000亿以上免审
        )
        mask &= (roe_pass | no_roe_pass)

    if min_gpr > 0 and "avg_gpr_3y" in df.columns:
        mask &= df["avg_gpr_3y"].isna() | (df["avg_gpr_3y"] >= min_gpr)

    return df[mask]


def quality_score(row: pd.Series, sector: str) -> float:
    """质量优先评分：ROE稳定性(50%) + 毛利率(30%) + 市值(20%)"""
    avg_roe = row.get("avg_roe_3y")  or 0
    roe_min = row.get("roe_min_3y")  or 0
    roe_std = row.get("roe_std_3y")  or 10
    avg_gpr = row.get("avg_gpr_3y")  or 0
    log_mv  = np.log(max(row.get("total_mv") or 1, 1))

    if sector == "消费":
        roe_s  = min(avg_roe / 30 * 50, 50)
        roe_s += 10 if roe_min >= 12 else (5 if roe_min >= 8 else 0)
        gpr_s  = min(avg_gpr / 60 * 30, 30)
        mv_s   = min(log_mv / 18 * 20, 20)
        return min(roe_s + gpr_s + mv_s, 100)

    elif sector == "金融":
        roe_s  = min(avg_roe / 20 * 60, 60)
        stab   = 20 / (1 + roe_std / 5)
        mv_s   = min(log_mv / 18 * 20, 20)
        return min(roe_s + stab + mv_s, 100)

    elif sector == "公用事业":
        roe_s  = min(avg_roe / 15 * 50, 50)
        mv_s   = min(log_mv / 18 * 30, 30)
        stab   = 20 / (1 + roe_std / 5)
        return min(roe_s + mv_s + stab, 100)

    return 50.0


# ────────────────────────────────────────────────────────────
# 退化检测
# ────────────────────────────────────────────────────────────

def detect_degraded(pool_df: pd.DataFrame, quality_df: pd.DataFrame) -> list:
    if pool_df.empty or quality_df.empty:
        return []
    merged = pool_df[["ts_code", "company_name", "company_type"]].merge(
        quality_df, on="ts_code", how="left"
    )
    degraded = []
    for _, row in merged.iterrows():
        reasons = []
        avg_roe = row.get("avg_roe_3y")
        neg_yrs = row.get("neg_growth_years", 0)
        ctype   = row.get("company_type", "growth")

        # 品牌/银行/现金流型 ROE 大幅下滑
        if ctype in ("brand", "rate_sensitive", "cashflow"):
            threshold = {"brand": 6, "cashflow": 4, "rate_sensitive": 5}.get(ctype, 4)
            if avg_roe is not None and avg_roe < threshold:
                reasons.append(f"ROE滑落至{avg_roe:.1f}%")

        # 任何类型连续3年净利下滑
        if neg_yrs and neg_yrs >= 3:
            reasons.append("近3年净利润持续下滑")

        if reasons:
            degraded.append({
                "ts_code": row["ts_code"],
                "company_name": row.get("company_name", ""),
                "reasons": "、".join(reasons),
            })
    return degraded


# ────────────────────────────────────────────────────────────
# 强制入池辅助
# ────────────────────────────────────────────────────────────

def _find_rocket_stocks(ts_codes: list, threshold: float = 3.0) -> set:
    """找出历史上任意单一自然年内涨幅达到 threshold 倍的股票。

    按自然年计算，取全年最高/最低收盘价之比。
    覆盖 2020 至今。返回 ts_codes 的子集。
    """
    if not ts_codes:
        return set()
    try:
        BATCH = 500
        rocket = set()
        for i in range(0, len(ts_codes), BATCH):
            batch = ts_codes[i: i + BATCH]
            codes_str = ",".join(f"'{c}'" for c in batch)
            sql = f"""
                SELECT ts_code,
                       YEAR(trade_date) AS yr,
                       MAX(close) AS max_close,
                       MIN(close) AS min_close
                FROM stock_daily
                WHERE ts_code IN ({codes_str})
                  AND trade_date >= '2020-01-01'
                  AND close > 0
                GROUP BY ts_code, YEAR(trade_date)
                HAVING min_close > 0 AND max_close >= min_close * {threshold}
            """
            df = DBUtils.query_df(sql)
            if not df.empty:
                rocket.update(df["ts_code"].unique().tolist())
        return rocket
    except Exception as e:
        print(f"  [WARN] 飙升股查询失败: {e}")
        return set()


# ────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────

def run_weekly_refresh(dry_run=False, auto_add=False, rebuild=False):
    print(f"\n{'='*60}")
    print(f"每周股票池刷新（V5 两类策略+强制入池 目标1200-1400只）  {date.today()}")
    print(f"{'='*60}\n")

    pool = StockPool()
    classifier = CompanyClassifier()

    if rebuild and not dry_run:
        print("[rebuild] 清空旧股票池...")
        # 保留 core_holding 层 和 手工入池记录（is_manual=1）
        DBUtils.execute("DELETE FROM stock_pool WHERE tier != 'core_holding' AND (is_manual = 0 OR is_manual IS NULL)")
        print("[rebuild] 旧池已清空（保留 core_holding 和手工入池记录），开始重建\n")

    # 1. 全市场基础数据
    print("[1/5] 拉取全市场股票数据...")
    df = DBUtils.query_df("""
        SELECT si.ts_code, si.name, si.industry, si.pe_ttm, si.pb, si.total_mv,
               amt.avg_amount
        FROM stock_info si
        LEFT JOIN (
            SELECT ts_code, AVG(amount) as avg_amount
            FROM stock_daily
            WHERE trade_date >= (
                SELECT DATE_FORMAT(DATE_SUB(MAX(trade_date), INTERVAL 20 DAY), '%Y-%m-%d')
                FROM stock_daily
            )
            GROUP BY ts_code
        ) amt ON si.ts_code = amt.ts_code
        WHERE si.ts_code LIKE '%.SH' OR si.ts_code LIKE '%.SZ'
    """)
    if df.empty:
        print("[ERROR] stock_info 为空"); return
    print(f"  全市场共 {len(df)} 只股票")

    # 2. 硬性淘汰
    print("\n[2/5] 硬性淘汰...")
    df = apply_hard_filter(df)

    # 3. 板块映射 + 公司类型
    print("\n[3/5] 板块映射 + 公司类型分类...")
    df["sector"] = df["industry"].apply(map_to_sector)
    df["company_type"] = df["industry"].apply(
        lambda ind: classifier.classify("", industry=ind)
    )

    for sec in list(SECTOR_MAP.keys()) + ["其他"]:
        cnt = (df["sector"] == sec).sum()
        if cnt > 0:
            print(f"  {sec}: {cnt}只")

    # 4. 近3年质量指标
    ts_codes = df["ts_code"].tolist()
    print(f"\n[4/5] 计算近3年质量指标（{len(ts_codes)}只，分批处理）...")
    quality_df = compute_quality_metrics(ts_codes)
    df = df.merge(quality_df, on="ts_code", how="left")

    # 5. 分板块筛选入池
    print("\n[5/5] 分板块筛选...")
    pool_df = pool.get_pool()
    existing_codes = set(pool_df["ts_code"].tolist()) if not pool_df.empty else set()

    selected_parts = []

    for sector in list(SECTOR_MAP.keys()) + ["其他"]:
        sub = df[df["sector"] == sector].copy()
        if sub.empty:
            continue

        limit = SECTOR_LIMITS.get(sector, 50)

        if sector in COVERAGE_SECTORS:
            # 覆盖优先：宽松过滤 + 市值/质量排序
            sub = coverage_filter(sub)
            sub["_score"] = sub.apply(coverage_score, axis=1)
        else:
            # 质量优先：ROE/毛利率门槛 + 质量排序
            sub = quality_filter(sub, sector)
            sub["_score"] = sub.apply(lambda r: quality_score(r, sector), axis=1)

        sub = sub.sort_values("_score", ascending=False).head(limit)
        print(f"  [{sector}] {'覆盖' if sector in COVERAGE_SECTORS else '质量'}优先 "
              f"→ 通过: {len(df[df['sector']==sector])}只 → 选: {len(sub)}只")
        selected_parts.append(sub)

    if not selected_parts:
        print("[ERROR] 无候选股票"); return

    all_selected = pd.concat(selected_parts, ignore_index=True)

    # ── 强制入池：大市值 & 历史飙升股 ──────────────────────
    print("\n[强制入池规则]")

    # 规则1：总市值 >= 1000亿 强制入池
    bigcap_df = df[df["total_mv"] >= BIGCAP_THRESHOLD_WAN].copy()
    bigcap_codes = set(bigcap_df["ts_code"].tolist())
    already_selected = set(all_selected["ts_code"].tolist())
    bigcap_new = bigcap_df[~bigcap_df["ts_code"].isin(already_selected)]
    print(f"  大市值(≥1000亿): 共{len(bigcap_codes)}只，新增{len(bigcap_new)}只未入选")

    # 规则2：历史飙升股（任意年内3倍+涨幅）
    rocket_codes = _find_rocket_stocks(df["ts_code"].tolist(), ROCKET_THRESHOLD)
    rocket_df = df[df["ts_code"].isin(rocket_codes) & ~df["ts_code"].isin(already_selected) & ~df["ts_code"].isin(bigcap_codes)].copy()
    print(f"  历史飙升股(年内≥3倍): 共{len(rocket_codes)}只，新增{len(rocket_df)}只未入选")

    if not bigcap_new.empty or not rocket_df.empty:
        bypass = pd.concat([bigcap_new, rocket_df], ignore_index=True).drop_duplicates("ts_code")
        bypass["_score"] = bypass.apply(coverage_score, axis=1)
        bypass["_bypass_reason"] = bypass["ts_code"].apply(
            lambda c: "大市值" if c in bigcap_codes else "飙升股"
        )
        all_selected = pd.concat([all_selected, bypass], ignore_index=True).drop_duplicates("ts_code")
        print(f"  合计强制追加: {len(bypass)}只 → 总选股: {len(all_selected)}只")

    new_candidates = all_selected[~all_selected["ts_code"].isin(existing_codes)]

    # 退化检测
    degraded = []
    if not pool_df.empty:
        pool_q = quality_df[quality_df["ts_code"].isin(existing_codes)]
        degraded = detect_degraded(pool_df, pool_q)

    # 报告
    print(f"\n{'='*60}")
    print(f"刷新结果  {date.today()}")
    print(f"{'='*60}")
    print(f"通过硬性淘汰: {len(df)}只 → 板块选股: {len(all_selected)}只")
    print(f"新候选（不含已在池中）: {len(new_candidates)}只")
    print(f"退化待复审: {len(degraded)}只")

    # 展示各板块 Top5
    print("\n── 各板块 Top5 预览 ──")
    for sector in list(SECTOR_MAP.keys()) + ["其他"]:
        sub = new_candidates[new_candidates["sector"] == sector]
        if sub.empty:
            continue
        print(f"\n【{sector}】({len(sub)}只)")
        for _, row in sub.head(5).iterrows():
            roe_s  = f"ROE={row['avg_roe_3y']:.1f}%" if pd.notna(row.get("avg_roe_3y")) else "ROE=N/A"
            yoy_s  = f"增速={row['avg_netprofit_yoy_3y']:.1f}%" if pd.notna(row.get("avg_netprofit_yoy_3y")) else ""
            gpr_s  = f"毛利={row['avg_gpr_3y']:.1f}%" if pd.notna(row.get("avg_gpr_3y")) else ""
            mv_s   = f"市值{row['total_mv']/10000:.0f}亿" if row.get("total_mv") else ""
            ctype  = {"brand":"品牌","growth":"成长","cashflow":"现金流",
                      "resource":"资源","rate_sensitive":"银行","policy":"政策"}.get(row["company_type"], row["company_type"])
            print(f"  {row['ts_code']}  {str(row['name']):<8}  [{ctype}]  {roe_s}  {yoy_s}  {gpr_s}  {mv_s}")

    if degraded:
        print(f"\n── 建议复审（质量退化）──")
        for item in degraded:
            print(f"  [!] {item['ts_code']}  {item['company_name']}  → {item['reasons']}")

    # 入池
    if auto_add and not dry_run and not new_candidates.empty:
        print(f"\n[入池] 将 {len(new_candidates)} 只新候选加入 reserve 层...")
        add_df = new_candidates.rename(columns={"name": "company_name"})
        added = pool.batch_add(add_df, tier="reserve")
        print(f"[入池] 实际新增 {added} 只")

    if not dry_run and (auto_add or rebuild):
        _save_log(new_candidates, degraded)
        print("\n刷新日志已保存")
    else:
        print("\n[dry-run] 跳过写库")

    print(f"\n{'='*60}\n")
    return new_candidates, degraded


# ────────────────────────────────────────────────────────────
# 日志
# ────────────────────────────────────────────────────────────

def _save_log(candidates: pd.DataFrame, degraded: list):
    today = date.today().isoformat()
    c_json = json.dumps(
        candidates[["ts_code", "company_type", "sector"]].to_dict("records"),
        ensure_ascii=False,
    )
    d_json = json.dumps([d["ts_code"] for d in degraded], ensure_ascii=False)
    DBUtils.execute(
        "INSERT INTO pool_refresh_log (refresh_date, added_count, removed_count, candidates, degraded) VALUES (?, ?, ?, ?, ?)",
        params=[today, len(candidates), len(degraded), c_json, d_json],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="每周股票池刷新（V5）")
    parser.add_argument("--dry-run",  action="store_true", help="只看结果，不写库")
    parser.add_argument("--rebuild",  action="store_true", help="清空旧池重建")
    parser.add_argument("--auto-add", action="store_true", help="自动入池")
    args = parser.parse_args()
    run_weekly_refresh(dry_run=args.dry_run, auto_add=args.auto_add, rebuild=args.rebuild)
