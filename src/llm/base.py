"""
LLM 推理节点基类
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Any, Optional
import json

from loguru import logger
from src.utils.llm_client import LLMClient
from src.utils.db_utils import DBUtils


SYSTEM_PROMPT = """你是【宽客猎手】，一位拥有10年A股量化交易经验的资深交易员。

你的核心能力：
1. 策略设计：根据市场特征设计量化策略
2. 统计分析：解读回溯结果，识别有效/无效因子
3. 风险意识：始终把风险控制放在第一位
4. 进化学习：从失败中学习，持续迭代策略

你的思维方式：
- 证据驱动：任何结论必须有数据支撑
- 概率思维：追求期望值为正的重复下注
- 风险第一：宁可错过机会，不可亏损本金
- 简洁有效：复杂的策略往往过拟合，简洁的策略更有生命力

你的输出格式：
- 先给结论，再给分析
- 用数据说话，避免空泛表述
- 决策建议要明确，给出具体行动
"""


class BaseLLMNode(ABC):
    """LLM 推理节点基类"""

    NODE_NAME: str = ""

    def __init__(self, node_name: str = None):
        self.node_name = node_name or self.NODE_NAME or self.__class__.__name__
        self.llm = LLMClient()
        self._ensure_table()

    def _ensure_table(self):
        """确保 llm_evaluations 表存在"""
        try:
            DBUtils.execute("""
                CREATE TABLE IF NOT EXISTS llm_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node TEXT,
                    trade_date TEXT,
                    input_summary TEXT,
                    reasoning TEXT,
                    decisions TEXT,
                    confidence REAL,
                    improvement_hints TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(trade_date, node)
                )
            """)
        except Exception as e:
            logger.debug(f"[{self.node_name}] 建表跳过: {e}")

    @abstractmethod
    def build_prompt(self, context: Dict[str, Any]) -> str:
        """从 context 构建用户提示词"""
        raise NotImplementedError

    def reason(self, trade_date: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行 LLM 推理

        Args:
            trade_date: 交易日期 (YYYY-MM-DD)
            context: 包含所有输入数据的字典

        Returns:
            {
                'reasoning': str,       # LLM 推理过程
                'decisions': str,       # 具体决策
                'confidence': float,    # 置信度 0~1
                'improvement_hints': str # 改进建议
            }
        """
        user_prompt = self.build_prompt(context)

        if not self.llm.is_available():
            logger.warning(f"[{self.node_name}] LLM 不可用，跳过推理")
            return {
                'reasoning': '[LLM unavailable]',
                'decisions': '',
                'confidence': 0.0,
                'improvement_hints': '',
            }

        response = self.llm._call_llm(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=1500,
        )

        reasoning = response or '[no response]'
        decisions = self._parse_decisions(reasoning)
        confidence = self._estimate_confidence(reasoning)
        hints = self._parse_hints(reasoning)

        self._save_to_db(trade_date, user_prompt, reasoning, decisions, confidence, hints)

        return {
            'reasoning': reasoning,
            'decisions': decisions,
            'confidence': confidence,
            'improvement_hints': hints,
        }

    def _parse_decisions(self, text: str) -> str:
        """从 LLM 输出中提取决策"""
        marker = '决策'
        for line in text.splitlines():
            if marker in line:
                return line.split(marker, 1)[-1].strip()
        return text[:300]

    def _parse_hints(self, text: str) -> str:
        """从 LLM 输出中提取改进建议"""
        hints = []
        for line in text.splitlines():
            low = line.lower()
            if any(k in low for k in ['建议', '改进', '优化', '问题', '风险提示']):
                hints.append(line.strip())
        return '\n'.join(hints[:5])

    def _estimate_confidence(self, text: str) -> float:
        """估计置信度"""
        text_lower = text.lower()
        high_confidence_words = ['确认', '强烈', '必然', '一定', '明确', '推荐']
        low_confidence_words = ['不确定', '可能', '也许', '观望', '建议']

        score = 0.5
        for w in high_confidence_words:
            if w in text_lower:
                score += 0.05
        for w in low_confidence_words:
            if w in text_lower:
                score -= 0.05

        return max(0.1, min(0.95, score))

    def _save_to_db(
        self, trade_date: str, input_summary: str,
        reasoning: str, decisions: str, confidence: float, hints: str,
    ):
        """保存推理结果到数据库"""
        try:
            date_fmt = trade_date.replace('-', '')
            DBUtils.execute("""
                REPLACE INTO llm_evaluations
                (node, trade_date, input_summary, reasoning, decisions,
                 confidence, improvement_hints)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                self.node_name,
                date_fmt,
                input_summary[:500],
                reasoning[:2000],
                decisions[:500],
                confidence,
                hints[:300],
            ))
        except Exception as e:
            logger.debug(f"[{self.node_name}] 保存失败: {e}")

    def get_history(self, trade_date: str = None, limit: int = 30) -> list:
        """获取历史推理记录"""
        date_clause = f"AND trade_date = '{trade_date.replace('-', '')}'" if trade_date else ""
        df = DBUtils.query_df(f"""
            SELECT node, trade_date, reasoning, decisions, confidence,
                   improvement_hints, created_at
            FROM llm_evaluations
            WHERE node = '{self.node_name}' {date_clause}
            ORDER BY created_at DESC
            LIMIT {limit}
        """)
        if df is None or df.empty:
            return []
        return df.to_dict('records')
