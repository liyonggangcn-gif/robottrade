#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易记忆模块
保存和检索历史交易经验，为 LLM 决策提供上下文
"""
from datetime import datetime, timedelta
from typing import List

from loguru import logger

from src.utils.db_utils import DBUtils


class TradeMemory:
    """
    交易记忆存储
    memory_type: win_pattern | loss_pattern | market_insight | strategy_note
    """

    # 合法的记忆类型
    VALID_TYPES = {'win_pattern', 'loss_pattern', 'market_insight', 'strategy_note'}

    def __init__(self):
        self._ensure_table()

    # ------------------------------------------------------------------ #
    #  表结构
    # ------------------------------------------------------------------ #
    def _ensure_table(self):
        """自动建表"""
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS agent_trade_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_type VARCHAR(20) NOT NULL,
                title VARCHAR(200),
                content TEXT,
                ts_code VARCHAR(20),
                trade_date VARCHAR(10),
                importance INT DEFAULT 1,
                created_at VARCHAR(20)
            )
        """)
        logger.debug("[Memory] 数据库表检查完毕")

    # ------------------------------------------------------------------ #
    #  写入
    # ------------------------------------------------------------------ #
    def save(self, memory_type: str, title: str, content: str,
             ts_code: str = '', trade_date: str = '', importance: int = 1):
        """保存一条记忆
        Args:
            memory_type: 类型，见 VALID_TYPES
            title: 简短标题（200字以内）
            content: 详细内容
            ts_code: 相关股票代码（可空）
            trade_date: 关联交易日期（可空）
            importance: 重要性 1-5，越大越重要
        """
        if memory_type not in self.VALID_TYPES:
            logger.warning(f"[Memory] 无效的 memory_type: {memory_type}，使用 strategy_note 代替")
            memory_type = 'strategy_note'

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            DBUtils.execute(
                """INSERT INTO agent_trade_memory
                   (memory_type, title, content, ts_code, trade_date, importance, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (memory_type, title, content, ts_code, trade_date, importance, now)
            )
            logger.info(f"[Memory] 保存记忆: [{memory_type}] {title}  importance={importance}")
        except Exception as e:
            logger.error(f"[Memory] 保存记忆失败: {e}")

    # ------------------------------------------------------------------ #
    #  读取
    # ------------------------------------------------------------------ #
    def get_recent(self, days: int = 30, limit: int = 20) -> List[dict]:
        """获取最近 N 天的记忆，按重要性倒序排列
        Returns:
            list of dict with keys: id, memory_type, title, content, ts_code,
                                    trade_date, importance, created_at
        """
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        try:
            df = DBUtils.query_df(
                """SELECT id, memory_type, title, content, ts_code, trade_date,
                          importance, created_at
                   FROM agent_trade_memory
                   WHERE created_at >= ?
                   ORDER BY importance DESC, created_at DESC
                   LIMIT ?""",
                (cutoff, limit)
            )
            return df.to_dict('records')
        except Exception as e:
            logger.error(f"[Memory] 读取记忆失败: {e}")
            return []

    def get_context_prompt(self, trade_date: str = '') -> str:
        """
        生成供 LLM 注入的记忆上下文字符串
        优先展示高重要性记忆，总长度不超过 1000 字符
        """
        memories = self.get_recent(days=60, limit=30)
        if not memories:
            return ''

        lines = ['【历史交易记忆】']
        total_chars = len(lines[0])
        max_chars = 1000

        for i, m in enumerate(memories, 1):
            date_str = m.get('trade_date') or m.get('created_at', '')[:10]
            title = m.get('title', '')
            content = m.get('content', '')
            # 内容截短
            short_content = content[:100] + '...' if len(content) > 100 else content
            line = f"{i}. [{date_str}] {title}: {short_content}"

            if total_chars + len(line) + 1 > max_chars:
                break
            lines.append(line)
            total_chars += len(line) + 1

        return '\n'.join(lines)
