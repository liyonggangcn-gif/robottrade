"""
行业估值模型 — 直接移植自 QMT core/valuation.py

classify() / valuate() / verdict() / _get_metal_cycle()

行业类型说明
-----------
BANK / INSUR / BROKER / GOLD / CYCLICAL / COAL / STEEL
CHEM / CHEM_MDI / CHEM_TIO2 / CHEM_FLUORO
SOLAR / WIND / UTILITY / SEMICON / AERO_SPACE / DEFENSE
SOFTWARE / INTERNET / CONSUMER / AUTO / MFG / INFRA / PHARMA / GENERAL
"""
from __future__ import annotations
from typing import Dict, Optional, Tuple

OVERSEAS_PE_BONUS   = {1: 1, 2: 2, 3: 4}
OVERSEAS_GROW_BONUS = {1: 0.01, 2: 0.02, 3: 0.04}


def classify(name: str, industry: str) -> str:
    c = name + (industry or "")
    if any(k in c for k in ["银行", "商行", "农商", "城商"]):              return "BANK"
    if any(k in c for k in ["保险", "人寿", "财险", "寿险"]):             return "INSUR"
    if any(k in c for k in ["证券", "基金", "期货公司", "投资管理"]):       return "BROKER"
    if any(k in name for k in ["黄金"]) or any(k in c for k in ["黄金矿", "金矿"]):
                                                                            return "GOLD"
    _MFG_NAMES      = ["铜冠矿建", "林州重机", "招标股份"]
    _INFRA_NAMES    = ["中国化学", "中国建筑", "中国交建", "中国中铁", "中国铁建"]
    _UTILITY_NAMES  = ["珠海港", "青岛港", "招商港口"]
    _CONSUMER_NAMES = ["中国中免", "中免", "王府井", "百联"]
    if any(k in name for k in _MFG_NAMES):      return "MFG"
    if any(k in name for k in _INFRA_NAMES):    return "INFRA"
    if any(k in name for k in _UTILITY_NAMES):  return "UTILITY"
    if any(k in name for k in _CONSUMER_NAMES): return "CONSUMER"

    if any(k in c for k in ["煤化工", "煤制烯烃", "煤制油", "煤制氢"]):    return "CHEM"
    if any(k in c for k in ["有色", "锡业", "豫光",
                              "锡", "铅", "锌", "铝", "镍", "钴", "锰",
                              "铂金", "钯", "稀土"]) and "化工" not in c:  return "CYCLICAL"
    if any(k in c for k in ["煤炭", "焦煤", "动力煤", "炼焦"]):           return "COAL"
    if any(k in c for k in ["钢铁", "特钢", "线材", "钢管"]):             return "STEEL"
    if any(k in c for k in ["MDI", "聚合MDI", "万华"]):                   return "CHEM_MDI"
    if any(k in c for k in ["钛白粉", "龙佰", "钛业"]):                   return "CHEM_TIO2"
    if any(k in c for k in ["氟化工", "制冷剂", "巨化", "氟化"]):         return "CHEM_FLUORO"
    if any(k in c for k in ["医药", "生物", "药业", "医疗", "器械",
                              "CRO", "CDMO", "基因", "疫苗",
                              "化学药", "中药", "原料药", "制药",
                              "医疗服务", "医疗设备"]):                    return "PHARMA"
    if any(k in c for k in ["化工", "化学", "石化", "氯碱", "农化",
                              "纯碱", "尿素", "甲醇", "乙烯",
                              "石油加工", "油气加工", "化纤"]):            return "CHEM"
    if any(k in c for k in ["光伏", "太阳能", "晶澳", "晶科",
                              "组件", "硅片", "钙钛矿"]):                  return "SOLAR"
    if any(k in c for k in ["风电", "海风", "风机"]):                     return "WIND"
    if any(k in c for k in ["电力", "水电", "核电", "热电",
                              "发电", "火电"]):                            return "UTILITY"
    if any(k in c for k in ["港口", "航运", "物流", "仓储",
                              "港", "码头"]):                              return "UTILITY"
    if any(k in c for k in ["燃气", "管道气", "水务", "供热",
                              "煤层气", "天然气"]):                        return "UTILITY"
    if any(k in c for k in ["元器件", "半导体", "芯片", "集成电路", "晶圆",
                              "封测", "设计", "EDA", "光刻",
                              "电子信息", "光电子", "被动元件"]):          return "SEMICON"
    if any(k in c for k in ["商业航天", "火箭", "卫星", "航天宏图",
                              "空天"]):                                     return "AERO_SPACE"
    if any(k in c for k in ["军工", "航空", "航发", "雷达", "导弹",
                              "舰船", "兵器", "军用"]):                    return "DEFENSE"
    if any(k in c for k in ["软件", "云计算", "SaaS", "数字化",
                              "信息安全", "人工智能", "AI",
                              "IT服务", "IT设备"]):                        return "SOFTWARE"
    if any(k in c for k in ["互联网", "电商", "平台", "游戏",
                              "传媒", "影视", "广告"]):                    return "INTERNET"
    if any(k in c for k in ["白酒", "啤酒", "饮料", "食品", "乳品",
                              "调味", "连锁餐饮", "零售", "免税",
                              "百货", "超市", "便利店",
                              "家居用品", "家电", "日用品",
                              "纺织", "服装", "鞋帽"]):                    return "CONSUMER"
    if any(k in c for k in ["汽车整车", "新能源车", "乘用车",
                              "整车"]):                                     return "AUTO"
    if any(k in c for k in ["机械", "汽车零", "汽车配件", "电工", "工程机械",
                              "重机", "矿建", "装备", "铸件",
                              "专用设备", "通用设备", "仪器仪表",
                              "电气设备", "自动化", "机器人",
                              "农业机械", "工业控制"]):                    return "MFG"
    if any(k in c for k in ["农药化工", "化肥", "农化", "农业",
                              "种子", "种植", "畜牧", "水产",
                              "农林牧渔"]):                                 return "CHEM"
    if any(k in c for k in ["地产", "房地产", "建设", "建筑",
                              "基建", "工程",
                              "路桥", "隧道", "市政"]):                    return "INFRA"
    if any(k in c for k in ["铜", "铁矿", "矿业", "资源",
                              "有色金属", "工业金属"]) and "化工" not in c: return "CYCLICAL"
    return "GENERAL"


def _get_metal_cycle(name: str) -> float:
    _metal_map = {
        "赤峰黄金": "黄金", "锡业股份": "锡", "豫光金铅": "铅",
        "铜冠矿建": "铜",  "龙佰集团": "钛白粉", "巨化股份": "氟化工",
        "云天化":   "尿素", "万华化学": "MDI",
        "宝丰能源": "聚丙烯", "陕西煤业": "动力煤",
    }
    product = _metal_map.get(name)
    if not product:
        return 0.0
    try:
        import pymysql
        from src.utils.config_loader import Config
        mysql = Config.mysql if hasattr(Config, 'mysql') else {}
        conn = pymysql.connect(
            host=mysql.get('host', '192.168.3.41'),
            port=int(mysql.get('port', 3306)),
            user=mysql.get('user', 'root'),
            password=mysql.get('password', ''),
            database='qmt',
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT AVG(price) as avg_p, MAX(price) as max_p, MIN(price) as min_p,
                   (SELECT price FROM commodity_prices
                    WHERE product=%s ORDER BY date DESC LIMIT 1) as cur_p
            FROM commodity_prices
            WHERE product=%s AND date >= DATE_SUB(CURDATE(), INTERVAL 3 YEAR)
        """, (product, product))
        row = cur.fetchone()
        conn.close()
        if row and row["avg_p"] and row["cur_p"]:
            avg, cur_p = float(row["avg_p"]), float(row["cur_p"])
            spread = (row["max_p"] or avg) - (row["min_p"] or avg)
            if spread > 0:
                return max(-1.0, min(1.0, (cur_p - avg) / (spread / 2)))
    except Exception:
        pass
    return 0.0


def valuate(name: str, fin: Dict, growth: float, mv_yi: float,
            overseas_level: int = 0) -> Tuple[str, str, Optional[float], str, float]:
    itype    = classify(name, fin.get("industry", ""))
    pe_ttm   = fin.get("pe_ttm")
    pb       = fin.get("pb")
    roe      = fin.get("roe", 0.10) or 0.10
    dv_ratio = fin.get("dv_ratio", 0) or 0
    np_yi    = (fin.get("net_profit") or 0) / 1e8
    rev_yi   = (fin.get("revenue") or 0) / 1e8
    ebitda_yi   = (fin.get("ebitda") or 0) / 1e8
    net_debt_yi = (fin.get("net_debt") or 0) / 1e8
    rd_exp_yi   = (fin.get("rd_exp") or 0) / 1e8
    rd_intensity = rd_exp_yi / rev_yi if rev_yi > 0 else 0
    gm = fin.get("gross_margin", 0.20) or 0.20

    if fin.get("forecast_profit"):
        fwd_np  = fin["forecast_profit"] / 1e8
        fwd_tag = "预期"
    else:
        yoy_raw = fin.get("netprofit_yoy")
        if yoy_raw is None:
            # 无数据，直接用 TTM
            fwd_np = np_yi
            fwd_tag = "TTM"
        elif yoy_raw < -0.30:
            # 真实负增长，截断在 -50%
            yoy = max(yoy_raw, -0.50)
            fwd_np = np_yi * (1 + yoy) if np_yi > 0 else np_yi
            fwd_tag = "外推"
        elif yoy_raw > 0.30:
            # 高增长，cap 到 30% 保守外推（避免泡沫化）
            fwd_np = np_yi * 1.30 if np_yi > 0 else np_yi
            fwd_tag = "外推"
        else:
            fwd_np = np_yi * (1 + yoy_raw) if np_yi > 0 else np_yi
            fwd_tag = "外推"

    g_bonus    = OVERSEAS_GROW_BONUS.get(overseas_level, 0)
    pe_bonus   = OVERSEAS_PE_BONUS.get(overseas_level, 0)
    adj_growth = growth + g_bonus

    method, upside, detail = "PE", None, ""

    if itype == "BANK":
        method = "PB-戈登"
        pb_eff = pb if pb else (pe_ttm * roe if pe_ttm and roe else None)
        if pb_eff and roe:
            pb     = pb_eff
            g_bank  = min(adj_growth, 0.07)
            fair_pb = max(min((roe - g_bank) / (0.11 - g_bank), 2.0), 0.3) * 0.80
            upside  = (fair_pb / pb - 1) * 100
            detail  = f"PB={pb:.2f}x 合理={fair_pb:.2f}x ROE={roe*100:.1f}%"

    elif itype == "INSUR":
        method = "P/EV"
        if pb:
            upside = (1.0 / pb - 1) * 100
            detail = f"PB={pb:.2f}x 目标≈1.0x"

    elif itype == "UTILITY":
        method = "DDM"
        if dv_ratio > 0:
            upside = (dv_ratio / 4.0 - 1) * 100
            detail = f"股息率={dv_ratio:.2f}% 目标=4.0%"
        elif np_yi > 0:
            fair_mv = fwd_np * (12 + pe_bonus)
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"降级PE 12x ({fwd_tag})"

    elif itype == "GOLD":
        method = "黄金PE"
        if np_yi > 0:
            cycle   = _get_metal_cycle(name)
            adj_pe  = (20 + pe_bonus) * (1 - cycle * 0.20)
            fair_mv = fwd_np * adj_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"周期={cycle:+.2f} PE={adj_pe:.1f}x ({fwd_tag})"

    elif itype == "CYCLICAL":
        cycle = _get_metal_cycle(name)
        if ebitda_yi > 0 and mv_yi > 0:
            method         = "EV/EBITDA"
            ev             = mv_yi + net_debt_yi
            fair_ev_ebitda = (8 + pe_bonus) * (1 - cycle * 0.25)
            fair_mv        = ebitda_yi * fair_ev_ebitda - net_debt_yi
            upside         = (fair_mv - mv_yi) / mv_yi * 100
            detail         = f"EV/EBITDA={ev/ebitda_yi:.1f}x 合理={fair_ev_ebitda:.1f}x 周期={cycle:+.2f}"
        elif np_yi > 0:
            method  = "中周期PE"
            mid_np  = np_yi / (1 + cycle * 0.35) if abs(cycle) > 0.1 else np_yi
            base_pe = (8 if cycle > 0.3 else (12 if cycle > -0.3 else 16)) + pe_bonus
            fair_mv = mid_np * base_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"中值利润={mid_np:.1f}亿 PE={base_pe}x 周期={cycle:+.2f}"

    elif itype in ("COAL", "STEEL"):
        cycle   = _get_metal_cycle(name)
        method  = "周期PE"
        base_pe = (6 if itype == "COAL" else 8) + pe_bonus
        if np_yi > 0:
            mid_np  = np_yi / (1 + cycle * 0.4) if abs(cycle) > 0.1 else np_yi
            fair_mv = mid_np * base_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"周期PE={base_pe}x 中值={mid_np:.1f}亿 周期={cycle:+.2f}"

    elif itype == "CHEM_MDI":
        method = "化工龙头PE"
        if np_yi > 0:
            cycle   = _get_metal_cycle(name)
            adj_pe  = max((18 + pe_bonus) / (1 + cycle * 0.15), 12)
            fair_mv = fwd_np * adj_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={adj_pe:.1f}x 毛利={gm*100:.1f}% ({fwd_tag})"

    elif itype == "CHEM_TIO2":
        method = "化工周期PE"
        if np_yi > 0:
            cycle   = _get_metal_cycle(name)
            adj_pe  = max((14 + pe_bonus) / (1 + cycle * 0.20), 8)
            fair_mv = fwd_np * adj_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={adj_pe:.1f}x 周期={cycle:+.2f} ({fwd_tag})"

    elif itype == "CHEM_FLUORO":
        method = "化工稀缺PE"
        if np_yi > 0:
            cycle   = _get_metal_cycle(name)
            adj_pe  = (18 + pe_bonus) / (1 + cycle * 0.10)
            fair_mv = fwd_np * adj_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={adj_pe:.1f}x 周期={cycle:+.2f} ({fwd_tag})"

    elif itype == "CHEM":
        method = "化工PE"
        if np_yi > 0:
            cycle         = _get_metal_cycle(name)
            spread_factor = gm / 0.25
            adj_pe        = max(min((14 + pe_bonus) / spread_factor, 22), 7)
            fair_mv = fwd_np * adj_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={adj_pe:.1f}x 毛利={gm*100:.1f}% 周期={cycle:+.2f}"

    elif itype == "SOLAR":
        if gm < 0.08:
            method = "PB兜底"
            if pb:
                upside = (0.9 / pb - 1) * 100
                detail = f"毛利={gm*100:.1f}%<8% PB目标0.9x"
        elif np_yi > 0:
            method  = "光伏PE"
            adj_pe  = 12 + pe_bonus
            fair_mv = fwd_np * adj_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={adj_pe}x 毛利={gm*100:.1f}% ({fwd_tag})"

    elif itype == "SEMICON":
        if np_yi <= 0:
            method = "PS"
            if rev_yi > 0:
                g_pct   = adj_growth * 100
                fair_ps = (4 if g_pct > 50 else (2.5 if g_pct > 30 else 1.5)) + pe_bonus * 0.2
                fair_mv = rev_yi * fair_ps
                upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
                detail  = f"PS={mv_yi/rev_yi:.1f}x 合理={fair_ps}x 增速={g_pct:.0f}%"
        elif adj_growth > 0:
            method  = "PEG+研发"
            implied_pe = mv_yi / np_yi if (not pe_ttm and np_yi > 0 and mv_yi > 0) else pe_ttm
            fair_pe = min(adj_growth * 100 * (1 + max(0, rd_intensity - 0.10) * 2), 80) + pe_bonus
            fair_mv = fwd_np * fair_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            if implied_pe:
                peg    = implied_pe / (adj_growth * 100)
                detail = f"PEG={peg:.2f} PE={implied_pe:.1f}x 研发={rd_intensity*100:.0f}%"
            else:
                detail = f"目标PE={fair_pe:.0f}x 研发={rd_intensity*100:.0f}%"

    elif itype == "AERO_SPACE":
        method = "航天EV/Rev"
        if rev_yi > 0:
            fair_ps = 5 if (np_yi > 0 or gm > 0.30) else 3
            fair_mv = rev_yi * fair_ps
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"EV/Rev合理={fair_ps}x"

    elif itype == "DEFENSE":
        method = "军工PE"
        if np_yi > 0:
            adj_pe  = 25 + pe_bonus
            fair_mv = fwd_np * adj_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={adj_pe}x ({fwd_tag})"

    elif itype == "PHARMA":
        if rd_intensity > 0.15 and np_yi <= 0:
            method = "研发型PS"
            if rev_yi > 0:
                fair_ps = 5 if rd_intensity > 0.30 else 3
                fair_mv = rev_yi * fair_ps
                upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
                detail  = f"研发={rd_intensity*100:.0f}% PS={fair_ps}x"
        elif np_yi > 0:
            method  = "医药PE"
            fair_pe = (22 if rd_intensity > 0.10 else 15) + pe_bonus
            fair_mv = fwd_np * fair_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={fair_pe}x 研发={rd_intensity*100:.0f}%"

    elif itype == "CONSUMER":
        method = "消费PE"
        if np_yi > 0:
            industry_str = fin.get("industry", "") or ""
            is_baijiu = any(k in (name + industry_str) for k in ["白酒", "酱酒", "酿酒"])
            if is_baijiu:
                # 白酒：行业无增长，给保守 PE
                fair_pe = 20 + pe_bonus
            else:
                fair_pe = (35 if gm > 0.50 else 22) + pe_bonus
            fair_mv = fwd_np * fair_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={fair_pe}x 毛利={gm*100:.1f}% ({fwd_tag})"

    elif itype == "MFG":
        method = "ROE修正PE"
        if np_yi > 0:
            fair_pe = min(12 + max(0, (roe - 0.10) * 150) + pe_bonus, 22)
            fair_mv = fwd_np * fair_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={fair_pe:.1f}x ROE={roe*100:.1f}%"

    elif itype == "INFRA":
        method = "建筑PE"
        if np_yi > 0:
            fair_pe = 12 + pe_bonus
            fair_mv = fwd_np * fair_pe
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={fair_pe}x"

    else:
        method = "PE"
        if np_yi > 0:
            bench   = 18 + pe_bonus
            fair_mv = fwd_np * bench
            upside  = (fair_mv - mv_yi) / mv_yi * 100 if mv_yi else None
            detail  = f"PE={bench}x ({fwd_tag})"

    return itype, method, upside, detail, adj_growth


def verdict(upside: Optional[float]) -> str:
    if upside is None: return "数据不足"
    if upside >= 30:   return "严重低估"
    if upside >= 15:   return "低估"
    if upside >= -15:  return "合理"
    if upside >= -30:  return "高估"
    return "严重高估"
