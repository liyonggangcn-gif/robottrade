"""
Memory Service: 持久化记忆系统 (DeerFlow Style)

提供 DeerFlow 风格的记忆系统：
1. Fact 提取与置信度评分
2. Debounced 批量更新
3. Category 分类存储
4. 系统提示词注入
5. 持久化存储

参考 DeerFlow Memory Architecture:
- Confidence-based fact storage (threshold: 0.7)
- Debounced updates (30s batching)
- JSON storage with metadata
- System prompt injection
"""
import json
import threading
import queue
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
import pandas as pd

from src.utils.db_utils import DBUtils


class FactCategory(Enum):
    PREFERENCE = "preference"
    KNOWLEDGE = "knowledge"
    CONTEXT = "context"
    BEHAVIOR = "behavior"
    GOAL = "goal"


@dataclass
class MemoryFact:
    id: str
    content: str
    confidence: float
    category: str
    source: str = "execution"
    tags: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class MemoryConfig:
    enabled: bool = True
    debounce_seconds: int = 30
    max_facts: int = 100
    fact_confidence_threshold: float = 0.7
    injection_enabled: bool = True
    max_injection_tokens: int = 2000


class MemoryService:
    """DeerFlow 风格记忆服务"""

    TABLE_NAME = "agent_memory"
    FACTS_TABLE = "agent_facts"
    CONFIG = MemoryConfig()

    def __init__(self):
        self._ensure_tables()
        self._update_queue: queue.Queue = queue.Queue()
        self._debounce_timer: Optional[threading.Timer] = None
        print("[MemoryService] 初始化完成 (DeerFlow Style)")

    def _ensure_tables(self):
        """确保记忆表存在"""
        try:
            DBUtils.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    memory_type VARCHAR(50) NOT NULL,
                    key_name VARCHAR(200) NOT NULL,
                    value TEXT,
                    tags JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_type (memory_type),
                    INDEX idx_key (key_name),
                    INDEX idx_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            DBUtils.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.FACTS_TABLE} (
                    id VARCHAR(100) PRIMARY KEY,
                    content TEXT NOT NULL,
                    confidence FLOAT NOT NULL,
                    category VARCHAR(50) NOT NULL,
                    source VARCHAR(100) DEFAULT 'execution',
                    tags JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_category (category),
                    INDEX idx_confidence (confidence),
                    INDEX idx_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        except Exception as e:
            print(f"[MemoryService] 建表失败: {e}")

    def save_fact(
        self,
        content: str,
        confidence: float,
        category: str = "context",
        source: str = "execution",
        tags: Optional[List[str]] = None
    ) -> bool:
        """保存单个 Fact（带置信度）"""
        if confidence < self.CONFIG.fact_confidence_threshold:
            print(f"[MemoryService] Fact 置信度 {confidence:.2f} < {self.CONFIG.fact_confidence_threshold}，跳过")
            return False

        fact_id = f"fact-{datetime.now().strftime('%Y%m%d%H%M%S')}-{hash(content) % 10000}"
        try:
            DBUtils.execute(f"""
                INSERT INTO {self.FACTS_TABLE} (id, content, confidence, category, source, tags)
                VALUES (?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE 
                    content = VALUES(content), 
                    confidence = VALUES(confidence),
                    updated_at = NOW()
            """, (
                fact_id,
                content,
                confidence,
                category,
                source,
                json.dumps(tags or [], ensure_ascii=False)
            ))
            print(f"[MemoryService] 保存 Fact: {content[:50]}... (conf={confidence:.2f})")
            return True
        except Exception as e:
            print(f"[MemoryService] 保存 Fact 失败: {e}")
            return False

    def extract_and_save_facts(
        self,
        execution_result: Dict[str, Any],
        market_context: Dict[str, Any]
    ) -> List[str]:
        """从执行结果和市场上下文中提取 Facts"""
        saved_facts = []

        picks = execution_result.get("top_picks", [])
        if picks:
            top_pick = picks[0] if picks else {}
            sector = top_pick.get("industry", "未知")
            self.save_fact(
                content=f"近期选股偏好: {sector}行业",
                confidence=0.8,
                category=FactCategory.PREFERENCE.value,
                tags=["sector", "strategy"]
            )
            saved_facts.append(sector)

        regime = market_context.get("regime", "unknown")
        self.save_fact(
            content=f"当前市场状态: {regime}",
            confidence=0.95,
            category=FactCategory.CONTEXT.value,
            tags=["market", "regime"]
        )

        hot_sectors = market_context.get("hot_sectors", [])
        if hot_sectors:
            self.save_fact(
                content=f"热点行业: {', '.join(hot_sectors[:3])}",
                confidence=0.85,
                category=FactCategory.KNOWLEDGE.value,
                tags=["hot", "sector"]
            )

        risk = execution_result.get("risk_assessment", "")
        if risk:
            risk_level = "高风险" if "高风险" in risk else ("中等风险" if "中等" in risk else "低风险")
            self.save_fact(
                content=f"组合风险等级: {risk_level}",
                confidence=0.9,
                category=FactCategory.CONTEXT.value,
                tags=["risk"]
            )

        return saved_facts

    def get_top_facts(
        self,
        limit: int = 15,
        category: Optional[str] = None
    ) -> List[MemoryFact]:
        """获取 Top Facts（按置信度排序）"""
        try:
            sql = f"SELECT * FROM {self.FACTS_TABLE}"
            params = []
            if category:
                sql += " WHERE category = ?"
                params.append(category)
            sql += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
            params.append(limit)

            df = DBUtils.query_df(sql, params)
            facts = []
            for _, row in df.iterrows():
                try:
                    facts.append(MemoryFact(
                        id=row["id"],
                        content=row["content"],
                        confidence=float(row["confidence"]),
                        category=row["category"],
                        source=row.get("source", "execution"),
                        tags=json.loads(row["tags"]) if row.get("tags") else [],
                        created_at=str(row["created_at"])
                    ))
                except Exception:
                    pass
            return facts
        except Exception as e:
            print(f"[MemoryService] 获取 Facts 失败: {e}")
            return []

    def generate_memory_injection(self) -> str:
        """生成记忆注入文本（用于系统提示词）"""
        if not self.CONFIG.injection_enabled:
            return ""

        facts = self.get_top_facts(limit=15)
        if not facts:
            return ""

        lines = ["\n## 用户记忆 (来自历史交互)\n"]

        categories = {}
        for fact in facts:
            cat = fact.category
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(fact)

        for cat, cat_facts in categories.items():
            cat_names = {
                "preference": "偏好",
                "knowledge": "知识",
                "context": "上下文",
                "behavior": "行为",
                "goal": "目标"
            }
            lines.append(f"### {cat_names.get(cat, cat)}\n")
            for fact in cat_facts[:5]:
                lines.append(f"- {fact.content} (置信度: {fact.confidence:.0%})")
            lines.append("")

        return "\n".join(lines)

    def save_decision(
        self,
        trade_date: str,
        decision_type: str,
        content: Dict[str, Any],
        tags: Optional[List[str]] = None
    ):
        """保存决策记录"""
        key = f"{decision_type}_{trade_date}"
        try:
            DBUtils.execute(f"""
                INSERT INTO {self.TABLE_NAME} (memory_type, key_name, value, tags)
                VALUES (?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE value = VALUES(value), tags = VALUES(tags), updated_at = NOW()
            """, (
                "decision",
                key,
                json.dumps(content, ensure_ascii=False, default=str),
                json.dumps(tags or [], ensure_ascii=False)
            ))
        except Exception as e:
            print(f"[MemoryService] 保存决策失败: {e}")

    def save_execution_result(
        self,
        trade_date: str,
        result: Dict[str, Any]
    ):
        """保存执行结果（同时提取 Facts）"""
        self.save_decision(
            trade_date=trade_date,
            decision_type="execution",
            content=result,
            tags=["execution", "daily_run"]
        )

        market_context = {
            "regime": result.get("market_regime", "unknown"),
            "hot_sectors": result.get("hot_sectors", []),
            "hot_concepts": result.get("hot_concepts", [])
        }
        self.extract_and_save_facts(result, market_context)

        picks = result.get("top_picks", [])
        buy_orders = result.get("buy_orders", [])
        sell_orders = result.get("sell_orders", [])
        
        summary = {
            "picks_count": len(picks) if isinstance(picks, list) else int(picks or 0),
            "buy_orders": len(buy_orders) if isinstance(buy_orders, list) else int(buy_orders or 0),
            "sell_orders": len(sell_orders) if isinstance(sell_orders, list) else int(sell_orders or 0),
            "risk_assessment": result.get("risk_assessment", "")
        }
        print(f"[MemoryService] 执行结果已保存: {summary}")

    def save_market_context(
        self,
        trade_date: str,
        regime: str,
        hot_sectors: List[str],
        hot_concepts: List[str],
        north_flow: Optional[float] = None
    ):
        """保存市场上下文"""
        content = {
            "regime": regime,
            "hot_sectors": hot_sectors,
            "hot_concepts": hot_concepts,
            "north_flow": north_flow
        }
        self.save_decision(
            trade_date=trade_date,
            decision_type="market_context",
            content=content,
            tags=["market", regime]
        )

    def get_recent_decisions(
        self,
        days: int = 30,
        decision_type: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """获取近期决策记录"""
        try:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            sql = f"""
                SELECT * FROM {self.TABLE_NAME}
                WHERE memory_type = 'decision' AND created_at >= ?
            """
            params = [cutoff]

            if decision_type:
                sql += " AND key_name LIKE ?"
                params.append(f"{decision_type}_%")

            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            df = DBUtils.query_df(sql, params)
            results = []
            for _, row in df.iterrows():
                try:
                    results.append({
                        "key": row["key_name"],
                        "content": json.loads(row["value"]),
                        "tags": json.loads(row["tags"]) if row["tags"] else [],
                        "created_at": str(row["created_at"])
                    })
                except Exception:
                    pass
            return results
        except Exception as e:
            print(f"[MemoryService] 获取决策失败: {e}")
            return []

    def get_context_summary(self, days: int = 30) -> str:
        """生成记忆上下文摘要"""
        recent = self.get_recent_decisions(days=days, limit=100)

        if not recent:
            return "无历史记忆，首次运行"

        lines = [f"[历史记忆 - 近{days}天]"]

        execution_history = [d for d in recent if d["key"].startswith("execution_")]
        if execution_history:
            lines.append(f"执行次数: {len(execution_history)}")
            recent_exec = execution_history[0]
            lines.append(f"最近执行: {recent_exec['key'].replace('execution_', '')}")
            recent_content = recent_exec.get("content", {})
            lines.append(f"  - 选股: {len(recent_content.get('top_picks', []))} 只")
            lines.append(f"  - 风险: {recent_content.get('risk_assessment', 'N/A')}")

        market_contexts = [d for d in recent if d["key"].startswith("market_context_")]
        if market_contexts:
            latest = market_contexts[0]
            content = latest.get("content", {})
            lines.append(f"最近市场状态: {content.get('regime', 'unknown')}")
            hot_sectors = content.get("hot_sectors", [])
            if hot_sectors:
                lines.append(f"  - 热点行业: {', '.join(hot_sectors[:3])}")

        lines.append(f"总记忆条目: {len(recent)}")

        facts = self.get_top_facts(limit=5)
        if facts:
            lines.append(f"\nTop Facts:")
            for f in facts:
                lines.append(f"  [{f.category}] {f.content[:40]}... (conf={f.confidence:.0%})")

        return "\n".join(lines)

    def get_previous_picks(self, days: int = 10) -> List[str]:
        """获取近期推荐的股票代码"""
        try:
            decisions = self.get_recent_decisions(days=days, decision_type="execution")
            all_codes = []
            for d in decisions:
                picks = d.get("content", {}).get("top_picks", [])
                for p in picks:
                    code = p.get("ts_code", "")
                    if code:
                        all_codes.append(code)
            return list(set(all_codes))
        except Exception as e:
            print(f"[MemoryService] 获取历史选股失败: {e}")
            return []

    def get_execution_history(self, days: int = 30) -> pd.DataFrame:
        """获取执行历史"""
        decisions = self.get_recent_decisions(days=days, decision_type="execution")
        if not decisions:
            return pd.DataFrame()

        records = []
        for d in decisions:
            content = d.get("content", {})
            picks = content.get("top_picks", [])
            buy_orders = content.get("buy_orders", [])
            sell_orders = content.get("sell_orders", [])
            records.append({
                "date": d["key"].replace("execution_", ""),
                "picks_count": len(picks) if isinstance(picks, list) else int(picks or 0),
                "buy_orders": len(buy_orders) if isinstance(buy_orders, list) else int(buy_orders or 0),
                "sell_orders": len(sell_orders) if isinstance(sell_orders, list) else int(sell_orders or 0),
                "regime": content.get("market_regime", ""),
                "error": content.get("error")
            })
        return pd.DataFrame(records)

    def clear_old_memories(self, days: int = 90):
        """清理旧记忆"""
        try:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            DBUtils.execute(f"DELETE FROM {self.TABLE_NAME} WHERE created_at < ?", (cutoff,))
            DBUtils.execute(f"DELETE FROM {self.FACTS_TABLE} WHERE created_at < ?", (cutoff,))
            print(f"[MemoryService] 已清理 {days} 天前的记忆")
        except Exception as e:
            print(f"[MemoryService] 清理记忆失败: {e}")

    def search_memories(
        self,
        keyword: str,
        memory_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """关键词搜索记忆"""
        try:
            sql = f"SELECT * FROM {self.TABLE_NAME} WHERE value LIKE ?"
            params = [f"%{keyword}%"]

            if memory_type:
                sql += " AND memory_type = ?"
                params.append(memory_type)

            sql += " ORDER BY created_at DESC LIMIT 20"

            df = DBUtils.query_df(sql, params)
            results = []
            for _, row in df.iterrows():
                try:
                    results.append({
                        "key": row["key_name"],
                        "type": row["memory_type"],
                        "content": json.loads(row["value"]),
                        "created_at": str(row["created_at"])
                    })
                except Exception:
                    pass
            return results
        except Exception as e:
            print(f"[MemoryService] 搜索失败: {e}")
            return []


_memory_service: Optional[MemoryService] = None


def get_memory_service() -> MemoryService:
    """获取记忆服务单例"""
    global _memory_service
    if _memory_service is None:
        _memory_service = MemoryService()
    return _memory_service
