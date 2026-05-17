"""
市场研究数据库表定义与初始化
所有研究模块写入的信号表统一在此定义
"""
from src.utils.db_utils import DBUtils
from loguru import logger


def ensure_research_tables():
    """创建所有研究信号表"""
    tables = [
        """
        CREATE TABLE IF NOT EXISTS news_sector_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT,
            source_key TEXT,
            sector_name TEXT,
            sentiment TEXT,
            signal_strength REAL DEFAULT 0.0,
            llm_summary TEXT,
            fetched_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trade_date, source_key, sector_name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS futures_sector_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT,
            sector TEXT,
            signal TEXT,
            strength REAL DEFAULT 0.0,
            score REAL DEFAULT 0.0,
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trade_date, sector)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS market_sentiment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT UNIQUE,
            northbound_flow REAL DEFAULT 0.0,
            northbound_pct REAL DEFAULT 0.0,
            sentiment_level TEXT DEFAULT 'neutral',
            score REAL DEFAULT 0.5,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS institutional_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT,
            ts_code TEXT,
            name TEXT,
            reason TEXT,
            buy_amount REAL DEFAULT 0.0,
            sell_amount REAL DEFAULT 0.0,
            net_amount REAL DEFAULT 0.0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trade_date, ts_code, reason)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hot_topics_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT,
            topic TEXT,
            score REAL DEFAULT 0.0,
            source TEXT DEFAULT 'auto',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trade_date, topic)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sector_timing_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT,
            industry TEXT,
            return_pct REAL DEFAULT 0.0,
            relative_strength REAL DEFAULT 0.0,
            penetration_phase TEXT,
            cycle_type TEXT,
            suggestion TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trade_date, industry)
        )
        """,
    ]

    for sql in tables:
        try:
            DBUtils.execute(sql)
        except Exception as e:
            logger.warning(f"建表失败: {e}")

    indices = [
        "CREATE INDEX IF NOT EXISTS idx_nss_date ON news_sector_signals(trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_fss_date ON futures_sector_signals(trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_ms_date ON market_sentiment(trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_is_date ON institutional_signals(trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_htl_date ON hot_topics_log(trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_stl_date ON sector_timing_log(trade_date)",
    ]

    for sql in indices:
        try:
            DBUtils.execute(sql)
        except Exception:
            pass

    logger.info("研究信号表初始化完成")
