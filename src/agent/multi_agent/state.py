"""
多Agent状态定义

LangGraph 共享状态，所有Agent都读写这个状态。
"""
from typing import TypedDict, Optional, List, Dict, Any
from datetime import datetime


class QuantAgentState(TypedDict, total=False):
    """量化Agent系统的共享状态"""

    # 上下文信息
    trade_date: str
    market_regime: str
    task: str

    # DataAgent 输出
    market_summary: str
    northbound_flow: List[Dict[str, Any]]
    hot_sectors: List[str]
    hot_concepts: List[str]

    # StrategyAgent 输出
    candidates: List[Dict[str, Any]]
    sector_analysis: str
    top_picks: List[Dict[str, Any]]
    etf_picks: List[Dict[str, Any]]
    cb_picks: List[Dict[str, Any]]
    stock_count: int
    etf_count: int
    cb_count: int

    # RiskAgent 输出
    risk_assessment: str
    sell_signals: List[Dict[str, Any]]
    position_adjustments: List[Dict[str, Any]]

    # ExecutionAgent 输出
    buy_orders: List[Dict[str, Any]]
    sell_orders: List[Dict[str, Any]]
    execution_summary: str

    # Memory
    memory_context: str
    recent_decisions: List[Dict[str, Any]]

    # 流程控制
    next_agent: Optional[str]
    reasoning: str
    error: Optional[str]

    # Agent消息历史
    messages: List[Dict[str, Any]]
