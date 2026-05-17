#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoTrader — 交易信号审计记录器

职责：将选股结果写入 trade_signals 表（审计追踪）。
实际交易由 TradingAgent (src/agent/trading_agent.py) 负责。
"""

import pandas as pd
from datetime import datetime
from typing import Dict, Optional

from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.utils.log_utils import init_logger

logger = init_logger("auto_trader")

# DDL
_SIGNALS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS trade_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code     VARCHAR(15) NOT NULL,
    name        VARCHAR(30),
    action      VARCHAR(20) NOT NULL,
    score       REAL,
    reason      TEXT,
    trade_date  VARCHAR(20),
    created_at  VARCHAR(30) DEFAULT (datetime('now', 'localtime'))
)"""

_SIGNALS_DDL_MYSQL = """
CREATE TABLE IF NOT EXISTS trade_signals (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    ts_code     VARCHAR(15) NOT NULL,
    name        VARCHAR(30),
    action      VARCHAR(20) NOT NULL,
    score       DOUBLE,
    reason      TEXT,
    trade_date  VARCHAR(20),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_date (trade_date)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"""

_INSERT_SQL = (
    "INSERT INTO trade_signals (ts_code, name, action, score, reason, trade_date) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


class AutoTrader:
    """交易信号审计记录器。

    ⚠️ 实际交易执行由 TradingAgent 负责，本类只负责将每日选股结果
    写入 trade_signals 表，作为可回查的审计追踪。
    """

    def __init__(self):
        self._is_mysql = Config.get('db_type', 'sqlite') == 'mysql'
        self._table_ready = False
        logger.info("[AutoTrader] 初始化完成（信号审计模式）")

    # ------------------------------------------------------------------ #
    #  内部辅助
    # ------------------------------------------------------------------ #
    def _ensure_table(self):
        if getattr(self, '_table_ready', False):
            return
        try:
            is_mysql = getattr(self, '_is_mysql', False)
            ddl = _SIGNALS_DDL_MYSQL if is_mysql else _SIGNALS_DDL_SQLITE
            DBUtils.execute(ddl)
            self._table_ready = True
        except Exception as e:
            logger.warning(f"[AutoTrader] trade_signals 建表失败（非致命）: {e}")

    # ------------------------------------------------------------------ #
    #  公开接口
    # ------------------------------------------------------------------ #
    def auto_trade_stocks(self, stock_picks: Optional[pd.DataFrame],
                          action: str = 'BUY_PROPOSED') -> Dict:
        """将选股结果写入 trade_signals 并返回摘要。

        Args:
            stock_picks: 含 ts_code / name / score|final_score 列的 DataFrame
            action:      信号类型标签，默认 'BUY_PROPOSED'

        Returns:
            {'buy_count': int, 'buy_list': list, 'sell_count': 0, 'hold_count': 0, ...}
        """
        results = {
            'buy_count': 0, 'sell_count': 0, 'hold_count': 0,
            'buy_list': [], 'sell_list': [], 'hold_list': [],
        }

        if stock_picks is None or stock_picks.empty:
            logger.info("[AutoTrader] 无选股结果，跳过信号记录")
            return results

        self._ensure_table()
        today = datetime.now().strftime('%Y-%m-%d')
        score_col = 'score' if 'score' in stock_picks.columns else 'final_score'

        logged = 0
        for _, r in stock_picks.iterrows():
            ts_code = str(r.get('ts_code', ''))
            name    = str(r.get('name', ''))
            score   = float(r.get(score_col, 0) or 0)
            reason  = str(r.get('reason', ''))
            if not ts_code:
                continue
            try:
                DBUtils.execute(_INSERT_SQL, (ts_code, name, action, score, reason, today))
                logged += 1
            except Exception as e:
                logger.debug(f"[AutoTrader] trade_signals 写入跳过 {ts_code}: {e}")

            results['buy_list'].append({
                'ts_code': ts_code,
                'name':    name,
                'score':   score,
            })

        results['buy_count'] = len(results['buy_list'])
        logger.info(f"[AutoTrader] 已记录 {logged} 条买入意向信号 → trade_signals")
        return results

    def get_trading_summary(self) -> Dict:
        """查询今日已记录的信号数量。"""
        today = datetime.now().strftime('%Y-%m-%d')
        try:
            df = DBUtils.query_df(
                "SELECT action, COUNT(*) AS cnt FROM trade_signals "
                "WHERE trade_date = ? GROUP BY action",
                params=(today,)
            )
            return df.set_index('action')['cnt'].to_dict() if not df.empty else {}
        except Exception:
            return {}
