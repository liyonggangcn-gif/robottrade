"""
股票池管理模块
维护三层股票池：核心持仓(core_holding) / 重点观察(watch) / 储备研究(reserve)

每只公司有完整的档案（类型、驱动因子配置、研究日志），
系统对池内公司持续跟踪，不追热点不频繁换手。
"""

import json
from datetime import date
from typing import Optional

import pandas as pd

from src.utils.db_utils import DBUtils
from src.classifier.company_classifier import CompanyClassifier

# 池子分层定义
TIERS = {
    "core_holding": "核心持仓",   # 已买入
    "watch":        "重点观察",   # 等待买入时机
    "reserve":      "储备研究",   # 待深入研究
}


class StockPool:
    """股票池管理"""

    def __init__(self):
        self.classifier = CompanyClassifier()
        self._ensure_tables()

    # ──────────────────────────────────────────────
    # 入池 / 出池 / 升降层
    # ──────────────────────────────────────────────

    def add(self, ts_code: str, tier: str = "reserve",
            company_type: Optional[str] = None,
            notes: str = "") -> bool:
        """
        将股票加入池子。
        company_type 不传时自动识别。
        """
        if tier not in TIERS:
            raise ValueError(f"tier 必须是 {list(TIERS.keys())} 之一")

        if self.is_in_pool(ts_code):
            # 已在池中：升级为手工固定（确保不被周刷新删除），并更新备注
            DBUtils.execute(
                "UPDATE stock_pool SET is_manual = 1, notes = ?, updated_at = CURRENT_TIMESTAMP WHERE ts_code = ? AND is_active = 1",
                params=[notes or "手工固定", ts_code],
            )
            print(f"[StockPool] {ts_code} 已在池中，已标记为手工固定")
            return True

        if not company_type:
            company_type = self.classifier.classify(ts_code)

        # 从 stock_info 取公司名
        df = DBUtils.query_df(
            "SELECT name FROM stock_info WHERE ts_code = ?", params=[ts_code]
        )
        company_name = df["name"].iloc[0] if not df.empty else ""

        DBUtils.execute(
            """INSERT INTO stock_pool
               (ts_code, company_name, company_type, tier, enter_date, notes, is_active, is_manual)
               VALUES (?, ?, ?, ?, ?, ?, 1, 1)""",
            params=[ts_code, company_name, company_type, tier,
                    date.today().isoformat(), notes],
        )

        # 初始化公司档案（如果没有的话）
        self._init_profile(ts_code, company_type)

        type_name = self.classifier.get_type_name(company_type)
        print(f"[StockPool] 已加入 {ts_code} {company_name} ({type_name}) → {TIERS[tier]}")
        return True

    def batch_add(self, df: pd.DataFrame, tier: str = "reserve") -> int:
        """
        批量入池，df 需含列：ts_code, name(或company_name), company_type。
        可选列：sector（板块名称）。
        跳过已在池中的股票，返回实际新增数量。
        """
        if tier not in TIERS:
            raise ValueError(f"tier 必须是 {list(TIERS.keys())} 之一")

        # 统一列名
        if "name" in df.columns and "company_name" not in df.columns:
            df = df.rename(columns={"name": "company_name"})

        # 已在池中的 code
        existing = DBUtils.query_df("SELECT ts_code FROM stock_pool WHERE is_active = 1")
        existing_codes = set(existing["ts_code"].tolist()) if not existing.empty else set()

        today = date.today().isoformat()
        added = 0
        for _, row in df.iterrows():
            ts_code = row["ts_code"]
            if ts_code in existing_codes:
                continue
            company_name = str(row.get("company_name", ""))
            company_type = str(row.get("company_type", "growth"))
            sector = row.get("sector", None)
            sector = str(sector) if sector is not None and sector == sector else None  # NaN → None
            DBUtils.execute(
                """INSERT INTO stock_pool
                   (ts_code, company_name, company_type, tier, enter_date, notes, is_active, sector)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                params=[ts_code, company_name, company_type, tier, today,
                        f"weekly_refresh自动入池 评分{row.get('quick_score', 0):.0f}",
                        sector],
            )
            self._init_profile(ts_code, company_type)
            existing_codes.add(ts_code)
            added += 1

        type_name = TIERS[tier]
        print(f"[StockPool] 批量入池完成: 新增 {added} 只 → {type_name}")
        return added

    def remove(self, ts_code: str, reason: str = ""):
        """将股票移出池子（软删除，保留历史记录）"""
        DBUtils.execute(
            "UPDATE stock_pool SET is_active = 0, notes = ? WHERE ts_code = ?",
            params=[f"[已移除] {reason}", ts_code],
        )
        print(f"[StockPool] 已移除 {ts_code}，原因：{reason}")

    def update_tier(self, ts_code: str, new_tier: str, reason: str = ""):
        """调整股票所在层级（如从观察池升入持仓池）"""
        if new_tier not in TIERS:
            raise ValueError(f"tier 必须是 {list(TIERS.keys())} 之一")

        old = self.get_stock(ts_code)
        old_tier = old["tier"] if old else "unknown"

        DBUtils.execute(
            "UPDATE stock_pool SET tier = ? WHERE ts_code = ? AND is_active = 1",
            params=[new_tier, ts_code],
        )
        # 记录到研究日志
        self.add_research_log(
            ts_code,
            trigger_type="tier_change",
            summary=f"层级变更：{TIERS.get(old_tier, old_tier)} → {TIERS[new_tier]}",
            action_suggestion=reason,
        )
        print(f"[StockPool] {ts_code} 层级变更：{old_tier} → {new_tier}")

    # ──────────────────────────────────────────────
    # 查询
    # ──────────────────────────────────────────────

    def get_pool(self, tier: Optional[str] = None) -> pd.DataFrame:
        """返回股票池（可按层级过滤）"""
        sql = "SELECT * FROM stock_pool WHERE is_active = 1"
        params = []
        if tier:
            sql += " AND tier = ?"
            params.append(tier)
        sql += " ORDER BY tier, company_type, ts_code"
        return DBUtils.query_df(sql, params=params if params else None)

    def get_stock(self, ts_code: str) -> Optional[dict]:
        """返回单只股票的池子记录"""
        df = DBUtils.query_df(
            "SELECT * FROM stock_pool WHERE ts_code = ? AND is_active = 1",
            params=[ts_code],
        )
        return df.iloc[0].to_dict() if not df.empty else None

    def is_in_pool(self, ts_code: str) -> bool:
        df = DBUtils.query_df(
            "SELECT 1 FROM stock_pool WHERE ts_code = ? AND is_active = 1",
            params=[ts_code],
        )
        return not df.empty

    def get_profile(self, ts_code: str) -> Optional[dict]:
        """返回公司档案"""
        df = DBUtils.query_df(
            "SELECT * FROM company_profile WHERE ts_code = ?", params=[ts_code]
        )
        if df.empty:
            return None
        row = df.iloc[0].to_dict()
        # 解析 JSON 字段
        for field in ("profit_drivers", "buy_conditions", "sell_conditions"):
            if row.get(field):
                try:
                    row[field] = json.loads(row[field])
                except Exception:
                    pass
        return row

    def update_profile(self, ts_code: str, **kwargs):
        """更新公司档案字段"""
        # JSON 字段序列化
        for field in ("profit_drivers", "buy_conditions", "sell_conditions"):
            if field in kwargs and isinstance(kwargs[field], (dict, list)):
                kwargs[field] = json.dumps(kwargs[field], ensure_ascii=False)

        set_clause = ", ".join([f"{k} = ?" for k in kwargs])
        values = list(kwargs.values()) + [ts_code]
        DBUtils.execute(
            f"UPDATE company_profile SET {set_clause} WHERE ts_code = ?",
            params=values,
        )

    # ──────────────────────────────────────────────
    # 研究日志
    # ──────────────────────────────────────────────

    def add_research_log(self, ts_code: str, trigger_type: str,
                         summary: str, action_suggestion: str = ""):
        """追加研究日志（不覆盖历史）"""
        DBUtils.execute(
            """INSERT INTO research_log
               (ts_code, log_date, trigger_type, summary, action_suggestion)
               VALUES (?, ?, ?, ?, ?)""",
            params=[ts_code, date.today().isoformat(),
                    trigger_type, summary, action_suggestion],
        )

    def get_research_logs(self, ts_code: str, limit: int = 10) -> pd.DataFrame:
        """返回最近N条研究日志"""
        return DBUtils.query_df(
            """SELECT log_date, trigger_type, summary, action_suggestion
               FROM research_log WHERE ts_code = ?
               ORDER BY log_date DESC, id DESC LIMIT ?""",
            params=[ts_code, limit],
        )

    def sync_positions_to_pool(self) -> dict:
        """
        将 positions 表的实盘持仓同步到 stock_pool：
        - 有持仓（shares > 0）→ tier=core_holding, is_manual=1
        - 已清仓（shares = 0 或已不在 positions）且原来是 core_holding → 降级为 watch

        Returns:
            dict: {upgraded, added, downgraded}
        """
        pos_df = DBUtils.query_df(
            "SELECT ts_code, name, company_type FROM positions WHERE shares > 0"
        )
        active_codes = set(pos_df["ts_code"].tolist()) if not pos_df.empty else set()

        upgraded = added = downgraded = 0

        # 1. 当前持仓 → 升级/入池为 core_holding
        for _, row in pos_df.iterrows():
            code = row["ts_code"]
            existing = self.get_stock(code)
            if existing:
                if existing.get("tier") != "core_holding" or not existing.get("is_manual"):
                    DBUtils.execute(
                        "UPDATE stock_pool SET tier='core_holding', is_manual=1, "
                        "updated_at=CURRENT_TIMESTAMP WHERE ts_code=? AND is_active=1",
                        params=[code],
                    )
                    print(f"[持仓同步] {code} 升级为 core_holding")
                    upgraded += 1
            else:
                # 不在池中，新增
                company_type = str(row.get("company_type") or "") or None
                if not company_type:
                    company_type = self.classifier.classify(code)
                name = str(row.get("name") or "")
                sector_df = DBUtils.query_df(
                    "SELECT industry FROM stock_info WHERE ts_code=?", params=[code]
                )
                sector = None
                if not sector_df.empty:
                    sector = sector_df.iloc[0].get("industry")
                DBUtils.execute(
                    """INSERT INTO stock_pool
                       (ts_code, company_name, company_type, tier, enter_date,
                        notes, is_active, is_manual, sector)
                       VALUES (?, ?, ?, 'core_holding', ?, '持仓同步自动入池', 1, 1, ?)""",
                    params=[code, name, company_type,
                            date.today().isoformat(), sector],
                )
                self._init_profile(code, company_type)
                print(f"[持仓同步] {code} 新增入池 core_holding")
                added += 1

        # 2. 原 core_holding 但已清仓 → 降级为 watch
        ch_df = DBUtils.query_df(
            "SELECT ts_code FROM stock_pool WHERE tier='core_holding' AND is_active=1"
        )
        for _, row in ch_df.iterrows():
            code = row["ts_code"]
            if code not in active_codes:
                DBUtils.execute(
                    "UPDATE stock_pool SET tier='watch', updated_at=CURRENT_TIMESTAMP "
                    "WHERE ts_code=? AND is_active=1",
                    params=[code],
                )
                print(f"[持仓同步] {code} 已清仓，降级为 watch")
                downgraded += 1

        print(f"[持仓同步] 完成 upgraded={upgraded} added={added} downgraded={downgraded}")
        return {"upgraded": upgraded, "added": added, "downgraded": downgraded}

    def print_pool_summary(self):
        """打印股票池概况"""
        df = self.get_pool()
        if df.empty:
            print("[StockPool] 股票池为空")
            return

        print(f"\n{'='*60}")
        print(f"股票池概况  共 {len(df)} 只  ({date.today()})")
        print(f"{'='*60}")

        for tier, label in TIERS.items():
            sub = df[df["tier"] == tier]
            if sub.empty:
                continue
            print(f"\n>> {label}({len(sub)}只)")
            for _, row in sub.iterrows():
                type_name = self.classifier.get_type_name(row["company_type"])
                print(f"  {row['ts_code']}  {row['company_name']:<8}  [{type_name}]  {row['notes'] or ''}")
        print()

    # ──────────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────────

    def _init_profile(self, ts_code: str, company_type: str):
        """初始化公司档案（仅在不存在时插入）"""
        exists = DBUtils.query_df(
            "SELECT 1 FROM company_profile WHERE ts_code = ?", params=[ts_code]
        )
        if not exists.empty:
            return

        meta = self.classifier.get_type_meta(company_type)
        default_drivers = {"drivers": meta["drivers"], "notes": ""}
        default_buy = {"conditions": meta["buy_signal"]}
        default_sell = {"conditions": "PE历史分位>80% OR 盈利下修>15% OR 持仓亏损>12%"}

        DBUtils.execute(
            """INSERT INTO company_profile
               (ts_code, company_type, profit_drivers, buy_conditions, sell_conditions, research_notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            params=[
                ts_code, company_type,
                json.dumps(default_drivers, ensure_ascii=False),
                json.dumps(default_buy, ensure_ascii=False),
                json.dumps(default_sell, ensure_ascii=False),
                "",
            ],
        )

    def _ensure_tables(self):
        """建表（首次运行时）"""
        # 股票池表
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS stock_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code VARCHAR(20) NOT NULL UNIQUE,
                company_name VARCHAR(50),
                company_type VARCHAR(30) NOT NULL DEFAULT 'growth',
                tier VARCHAR(20) NOT NULL DEFAULT 'reserve',
                enter_date DATE,
                notes TEXT DEFAULT '',
                is_active TINYINT DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 公司档案表（TEXT 列不带 DEFAULT，避免 MySQL 转 VARCHAR(255)）
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS company_profile (
                ts_code VARCHAR(20) PRIMARY KEY,
                company_type VARCHAR(30),
                profit_drivers TEXT,
                buy_conditions TEXT,
                sell_conditions TEXT,
                target_price_bear FLOAT,
                target_price_mid FLOAT,
                target_price_bull FLOAT,
                research_notes TEXT,
                last_analysis_date DATE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 研究日志表（summary/action_suggestion 不带 DEFAULT，避免 MySQL 转 VARCHAR(255)）
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS research_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code VARCHAR(20) NOT NULL,
                log_date DATE NOT NULL,
                trigger_type VARCHAR(30),
                summary TEXT,
                action_suggestion TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 池子刷新日志
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS pool_refresh_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                refresh_date DATE NOT NULL,
                added_count INT DEFAULT 0,
                removed_count INT DEFAULT 0,
                candidates TEXT DEFAULT '',
                degraded TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
