"""
回测与绩效跟踪相关数据库表
"""
from src.utils.db_utils import DBUtils
from loguru import logger


def ensure_backtest_tables():
    """创建回测和绩效跟踪相关表（MySQL兼容）"""
    tables = [
        """
        CREATE TABLE IF NOT EXISTS pick_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date VARCHAR(12),
            ts_code VARCHAR(20),
            name VARCHAR(100),
            entry_price REAL,
            entry_score REAL,
            ai_score REAL,
            event_score REAL,
            fundamental_score REAL,
            sector_momentum_score REAL,
            track VARCHAR(50),
            ret_1d REAL,
            ret_5d REAL,
            ret_10d REAL,
            ret_20d REAL,
            ret_max REAL,
            ret_min REAL,
            holding_days INTEGER DEFAULT 0,
            exit_price REAL,
            exit_reason VARCHAR(200),
            created_at VARCHAR(30) DEFAULT CURRENT_TIMESTAMP,
            updated_at VARCHAR(30) DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trade_date, ts_code)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS signal_effectiveness (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period VARCHAR(20),
            signal_type VARCHAR(50),
            pick_count INTEGER DEFAULT 0,
            win_count INTEGER DEFAULT 0,
            avg_ret REAL DEFAULT 0.0,
            sharpe REAL DEFAULT 0.0,
            top_k_ret REAL DEFAULT 0.0,
            max_ret REAL DEFAULT 0.0,
            min_ret REAL DEFAULT 0.0,
            corr_to_actual REAL DEFAULT 0.0,
            updated_at VARCHAR(30) DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(period, signal_type)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ab_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id VARCHAR(50) UNIQUE,
            name VARCHAR(200),
            description TEXT,
            hypothesis TEXT,
            status VARCHAR(20) DEFAULT 'running',
            winner VARCHAR(10),
            p_value REAL,
            t_stat REAL,
            sample_size INTEGER DEFAULT 0,
            created_at VARCHAR(30) DEFAULT CURRENT_TIMESTAMP,
            completed_at VARCHAR(30)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ab_arms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id VARCHAR(50),
            arm_id VARCHAR(10),
            name VARCHAR(100),
            description TEXT,
            params TEXT,
            ai_weight REAL,
            event_weight REAL,
            fundamental_weight REAL,
            sector_weight REAL,
            futures_bonus REAL,
            institutional_bonus REAL,
            news_bonus REAL,
            northbound_bonus REAL,
            top_k INTEGER,
            stop_loss_pct REAL,
            UNIQUE(experiment_id, arm_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ab_daily_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id VARCHAR(50),
            arm_id VARCHAR(10),
            trade_date VARCHAR(12),
            picks_count INTEGER DEFAULT 0,
            ret_1d REAL,
            ret_5d REAL,
            ret_10d REAL,
            ret_20d REAL,
            avg_score REAL,
            win_rate_5d REAL,
            created_at VARCHAR(30) DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(experiment_id, arm_id, trade_date)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS llm_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node VARCHAR(50),
            trade_date VARCHAR(12),
            input_summary TEXT,
            reasoning TEXT,
            decisions TEXT,
            confidence REAL,
            improvement_hints TEXT,
            created_at VARCHAR(30) DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trade_date, node)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS llm_trader_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node VARCHAR(50),
            trade_date VARCHAR(12),
            input_summary TEXT,
            reasoning TEXT,
            decision TEXT,
            confidence REAL,
            human_confirmed INTEGER DEFAULT 0,
            human_override TEXT,
            actual_outcome TEXT,
            feedback TEXT,
            created_at VARCHAR(30) DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id VARCHAR(50) UNIQUE NOT NULL,
            strategy VARCHAR(30) NOT NULL,
            start_date VARCHAR(12) NOT NULL,
            end_date VARCHAR(12) NOT NULL,
            top_k INT DEFAULT 10,
            rebalance_days INT DEFAULT 10,
            cost_rate REAL DEFAULT 0.0003,
            
            total_return REAL DEFAULT 0.0,
            annualized_return REAL DEFAULT 0.0,
            sharpe_ratio REAL DEFAULT 0.0,
            max_drawdown REAL DEFAULT 0.0,
            win_rate REAL DEFAULT 0.0,
            total_trades INT DEFAULT 0,
            winning_trades INT DEFAULT 0,
            losing_trades INT DEFAULT 0,
            
            daily_returns TEXT,
            equity_curve TEXT,
            metrics_json TEXT,
            
            status VARCHAR(20) DEFAULT 'running',
            error_msg TEXT,
            created_at VARCHAR(30) DEFAULT CURRENT_TIMESTAMP,
            completed_at VARCHAR(30),
            
            INDEX idx_strategy (strategy),
            INDEX idx_dates (start_date, end_date),
            INDEX idx_created (created_at)
        )
        """,
    ]

    for sql in tables:
        try:
            DBUtils.execute(sql)
        except Exception as e:
            logger.warning(f"建表失败: {e}")

    indices = [
        "CREATE INDEX IF NOT EXISTS idx_pp_date ON pick_performance(trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_pp_code ON pick_performance(ts_code)",
        "CREATE INDEX IF NOT EXISTS idx_se_period ON signal_effectiveness(period)",
        "CREATE INDEX IF NOT EXISTS idx_ab_exp ON ab_experiments(experiment_id)",
        "CREATE INDEX IF NOT EXISTS idx_ab_arm_exp ON ab_arms(experiment_id)",
        "CREATE INDEX IF NOT EXISTS idx_ab_dr_exp ON ab_daily_results(experiment_id)",
        "CREATE INDEX IF NOT EXISTS idx_le_date ON llm_evaluations(trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_ltd_date ON llm_trader_decisions(trade_date)",
    ]
    for sql in indices:
        try:
            DBUtils.execute(sql)
        except Exception:
            pass

    logger.info("回测与绩效跟踪表初始化完成")
