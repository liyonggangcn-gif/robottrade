#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
推送消息记录模块
保存所有推送消息到数据库，方便回溯
"""

from datetime import datetime
from src.utils.db_utils import DBUtils
from src.utils.log_utils import init_logger

logger = init_logger("message_logger")


class MessageLogger:
    """推送消息记录器"""
    
    def __init__(self):
        """初始化消息记录器"""
        self._init_table()
    
    def _init_table(self):
        """初始化消息记录表"""
        with DBUtils.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS push_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_type VARCHAR(255) NOT NULL,  -- 消息类型: morning_push, evening_push, futures_etf, etf_strategy等
                title VARCHAR(255) NOT NULL,          -- 消息标题
                content TEXT NOT NULL,                -- 消息内容
                send_time VARCHAR(255) NOT NULL,      -- 发送时间
                send_status VARCHAR(50),              -- 发送状态: success, failed
                error_message TEXT,                   -- 错误信息（如果失败）
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            
            # 创建索引
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_type ON push_messages(message_type)
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_send_time ON push_messages(send_time)
            """)
        
        logger.info("推送消息记录表初始化完成")
    
    def log_message(self, message_type: str, title: str, content: str, 
                   send_status: str = 'success', error_message: str = None):
        """
        记录推送消息
        
        Args:
            message_type: 消息类型（morning_push, evening_push, futures_etf等）
            title: 消息标题
            content: 消息内容
            send_status: 发送状态（success/failed）
            error_message: 错误信息（如果失败）
        """
        try:
            send_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            with DBUtils.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                INSERT INTO push_messages 
                (message_type, title, content, send_time, send_status, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
                """, (message_type, title, content, send_time, send_status, error_message))
            
            logger.info(f"消息已记录: {message_type} - {title}")
            
        except Exception as e:
            logger.error(f"记录消息失败: {e}", exc_info=True)
    
    def get_messages(self, message_type: str = None, limit: int = 100, 
                    start_date: str = None, end_date: str = None):
        """
        获取推送消息历史
        
        Args:
            message_type: 消息类型过滤（可选）
            limit: 返回数量限制
            start_date: 开始日期（YYYY-MM-DD）
            end_date: 结束日期（YYYY-MM-DD）
            
        Returns:
            DataFrame: 消息列表
        """
        try:
            sql = "SELECT * FROM push_messages WHERE 1=1"
            params = []
            
            if message_type:
                sql += " AND message_type = ?"
                params.append(message_type)
            
            if start_date:
                sql += " AND send_time >= ?"
                params.append(start_date)
            
            if end_date:
                sql += " AND send_time <= ?"
                params.append(end_date)
            
            sql += " ORDER BY send_time DESC LIMIT ?"
            params.append(limit)
            
            df = DBUtils.query_df(sql, params=params)
            return df
            
        except Exception as e:
            logger.error(f"获取消息历史失败: {e}", exc_info=True)
            return None
    
    def get_message_statistics(self):
        """获取消息统计信息"""
        try:
            stats = {}
            
            # 总消息数
            total_df = DBUtils.query_df("SELECT COUNT(*) as cnt FROM push_messages")
            stats['total'] = total_df.iloc[0]['cnt'] if not total_df.empty else 0
            
            # 按类型统计
            type_df = DBUtils.query_df("""
                SELECT message_type, COUNT(*) as cnt 
                FROM push_messages 
                GROUP BY message_type
            """)
            stats['by_type'] = type_df.to_dict('records') if not type_df.empty else []
            
            # 成功率
            success_df = DBUtils.query_df("""
                SELECT COUNT(*) as cnt 
                FROM push_messages 
                WHERE send_status = 'success'
            """)
            success_count = success_df.iloc[0]['cnt'] if not success_df.empty else 0
            stats['success_rate'] = (success_count / stats['total'] * 100) if stats['total'] > 0 else 0
            
            # 最近7天消息数
            week_df = DBUtils.query_df("""
                SELECT COUNT(*) as cnt 
                FROM push_messages 
                WHERE send_time >= datetime('now', '-7 days')
            """)
            stats['last_7_days'] = week_df.iloc[0]['cnt'] if not week_df.empty else 0
            
            return stats
            
        except Exception as e:
            logger.error(f"获取消息统计失败: {e}", exc_info=True)
            return {}
