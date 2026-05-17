"""
Orchestrator: LangGraph 编排层

DeerFlow Multi-Agent 架构核心：
1. DataAgent — 数据收集（已在 tools/data_tools.py）
2. StrategyAgent — 选股策略
3. RiskAgent — 风险控制
4. ExecutionAgent — 交易执行

工作流：
  START → DataCollection → StrategySelection → RiskAssessment → Execution → END
                       ↓                    ↓
                   [异常处理] ←──────────────┘

支持工具调用（Tool Calling）：
- LLM 可以调用数据查询工具获取实时信息
- 支持人工确认环节（human-in-the-loop）
"""
import os
import sys
from datetime import datetime
from typing import Literal, Optional

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.agent.multi_agent.state import QuantAgentState
from src.agent.multi_agent.strategy_agent import StrategyAgent
from src.agent.multi_agent.risk_agent import RiskAgent
from src.agent.multi_agent.execution_agent import ExecutionAgent
from src.agent.multi_agent.tools.data_tools import get_data_tools
from src.agent.multi_agent.memory_service import get_memory_service


class QuantOrchestrator:
    """量化系统 Multi-Agent 编排器"""

    def __init__(self):
        self.strategy_agent = StrategyAgent()
        self.risk_agent = RiskAgent()
        self.execution_agent = ExecutionAgent()
        self.data_tools = get_data_tools()
        self.memory = get_memory_service()

        self.graph = self._build_graph()
        print("[QuantOrchestrator] 初始化完成，LangGraph 工作流已就绪")
        print(f"[QuantOrchestrator] 记忆服务已加载: {self.memory.get_context_summary()[:100]}...")

    def _build_graph(self) -> StateGraph:
        """构建 LangGraph 工作流"""
        workflow = StateGraph(QuantAgentState)

        workflow.add_node("strategy_selection", self._node_strategy_selection)
        workflow.add_node("risk_node", self._node_risk_assessment)
        workflow.add_node("execution", self._node_execution)

        workflow.set_entry_point("strategy_selection")

        workflow.add_edge("strategy_selection", "risk_node")
        workflow.add_edge("risk_node", "execution")
        workflow.add_edge("execution", END)

        checkpointer = MemorySaver()
        return workflow.compile(checkpointer=checkpointer)

    def _node_strategy_selection(self, state: QuantAgentState) -> QuantAgentState:
        """StrategyAgent: 多策略选股（股票 + ETF + 可转债）"""
        trade_date = state.get("trade_date", datetime.now().strftime("%Y-%m-%d"))
        top_k = state.get("top_k", 20)
        print(f"\n[StrategySelection] 多策略选股 {trade_date}，top_k={top_k}...")

        try:
            result = self.strategy_agent.run_multi_strategy(trade_date=trade_date, top_k=top_k)

            state["hot_sectors"] = result.get("hot_sectors", [])
            state["hot_concepts"] = result.get("hot_concepts", [])
            state["candidates"] = result.get("candidates", [])
            state["top_picks"] = result.get("top_picks", [])
            state["sector_analysis"] = result.get("sector_analysis", "")
            state["market_regime"] = result.get("market_regime", {}).get("regime", "unknown")
            state["etf_picks"] = result.get("etf_picks", [])
            state["cb_picks"] = result.get("cb_picks", [])
            state["stock_count"] = result.get("stock_count", 0)
            state["etf_count"] = result.get("etf_count", 0)
            state["cb_count"] = result.get("cb_count", 0)
            state["next_agent"] = "risk_assessment"
            state["reasoning"] = (
                f"策略选股完成：股票 {result.get('stock_count', 0)} 只，"
                f"ETF {result.get('etf_count', 0)} 只，"
                f"可转债 {result.get('cb_count', 0)} 只"
            )
        except Exception as e:
            print(f"[StrategySelection] 失败: {e}")
            import traceback
            traceback.print_exc()
            state["error"] = str(e)
            state["top_picks"] = []
            state["candidates"] = []
            state["etf_picks"] = []
            state["cb_picks"] = []

        return state

    def _node_risk_assessment(self, state: QuantAgentState) -> QuantAgentState:
        """RiskAgent: 风险评估"""
        trade_date = state.get("trade_date", datetime.now().strftime("%Y-%m-%d"))
        print(f"\n[RiskAssessment] 评估 {trade_date} 持仓风险...")

        try:
            result = self.risk_agent.run(trade_date=trade_date)

            state["risk_assessment"] = result.get("risk_assessment", "")
            state["sell_signals"] = result.get("sell_signals", [])
            state["position_adjustments"] = result.get("position_adjustments", [])

            critical = result.get("critical_count", 0)
            if critical > 0:
                state["reasoning"] += f" | 风险警告: {critical}只触发止损"
            else:
                state["reasoning"] += " | 风险评估通过"
        except Exception as e:
            print(f"[RiskAssessment] 失败: {e}")
            state["error"] = str(e)
            state["sell_signals"] = []
            state["position_adjustments"] = []

        return state

    def _node_execution(self, state: QuantAgentState) -> QuantAgentState:
        """ExecutionAgent: 生成交易指令"""
        trade_date = state.get("trade_date", datetime.now().strftime("%Y-%m-%d"))
        print(f"\n[Execution] 生成 {trade_date} 交易指令...")

        try:
            picks = state.get("top_picks", [])
            sell_signals = state.get("sell_signals", [])

            result = self.execution_agent.run(
                picks=picks,
                sell_signals=sell_signals,
                trade_date=trade_date
            )

            state["buy_orders"] = result.get("buy_orders", [])
            state["sell_orders"] = result.get("sell_orders", [])
            state["execution_summary"] = result.get("execution_summary", "")
            state["reasoning"] += f" | 买入{len(state['buy_orders'])}只/卖出{len(state['sell_orders'])}只"
        except Exception as e:
            print(f"[Execution] 失败: {e}")
            state["error"] = str(e)
            state["buy_orders"] = []
            state["sell_orders"] = []
            state["execution_summary"] = "执行失败"

        return state

    def run(
        self,
        trade_date: Optional[str] = None,
        top_k: int = 20,
        config: Optional[dict] = None
    ) -> QuantAgentState:
        """执行完整工作流

        Args:
            trade_date: 交易日期，默认今天
            top_k: 选股数量
            config: 可选配置（如 force_sell_all 等）

        Returns:
            最终状态（含所有 agent 输出）
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        print(f"\n{'='*70}")
        print(f"[QuantOrchestrator] 启动 Multi-Agent 工作流 | 日期: {trade_date} | top_k: {top_k}")
        print(f"{'='*70}")

        memory_context = self.memory.get_context_summary(days=30)
        previous_picks = self.memory.get_previous_picks(days=10)
        print(f"[记忆] 历史选股: {len(previous_picks)} 只")

        initial_state: QuantAgentState = {
            "trade_date": trade_date,
            "market_regime": "unknown",
            "task": "daily_selection",
            "market_summary": "",
            "northbound_flow": [],
            "hot_sectors": [],
            "hot_concepts": [],
            "candidates": [],
            "sector_analysis": "",
            "top_picks": [],
            "risk_assessment": "",
            "sell_signals": [],
            "position_adjustments": [],
            "buy_orders": [],
            "sell_orders": [],
            "execution_summary": "",
            "memory_context": memory_context,
            "recent_decisions": self.memory.get_recent_decisions(days=30),
            "next_agent": None,
            "reasoning": "",
            "error": None,
            "messages": [],
            "top_k": top_k,
            "previous_picks": previous_picks,
        }

        try:
            final_state = self.graph.invoke(initial_state, config={"configurable": {"thread_id": trade_date}})

            self.memory.save_execution_result(trade_date, final_state)
            self._save_picks_to_db(trade_date, final_state)
            self.memory.save_market_context(
                trade_date=trade_date,
                regime=final_state.get("market_regime", "unknown"),
                hot_sectors=final_state.get("hot_sectors", []),
                hot_concepts=final_state.get("hot_concepts", [])
            )

            if final_state.get("error"):
                print(f"\n[警告] 工作流包含错误: {final_state['error']}")

            print(f"\n{'='*70}")
            print("[QuantOrchestrator] 工作流执行完成")
            print(f"  选股: {len(final_state.get('top_picks', []))} 只")
            print(f"  风险: {final_state.get('risk_assessment', 'N/A')}")
            print(f"  买入: {len(final_state.get('buy_orders', []))} 单")
            print(f"  卖出: {len(final_state.get('sell_orders', []))} 单")
            print(f"{'='*70}\n")

            return final_state

        except Exception as e:
            print(f"[QuantOrchestrator] 工作流执行失败: {e}")
            initial_state["error"] = str(e)
            return initial_state

    def _save_picks_to_db(self, trade_date: str, state: QuantAgentState):
        """保存选股结果到 daily_picks 表"""
        try:
            from src.utils.db_utils import DBUtils
            picks = state.get("top_picks", [])
            if not picks:
                return
            trade_date_str = trade_date.replace('-', '')
            DBUtils.execute(
                "DELETE FROM daily_picks WHERE trade_date = ?",
                (trade_date_str,)
            )
            for pick in picks:
                track = str(pick.get("track", ""))
                if track in ('etf', 'cb'):
                    continue
                ts_code = str(pick.get("ts_code", ""))
                name = str(pick.get("name", ""))
                final_score = float(pick.get("final_score", 0)) if pick.get("final_score") is not None else None
                ai_score = float(pick.get("ai_score", 0)) if pick.get("ai_score") is not None else None
                event_score = float(pick.get("event_score", 0)) if pick.get("event_score") is not None else None
                fs = pick.get("fund_score")
                if fs is None:
                    fs = pick.get("fundamental_score")
                fund_score = float(fs) if fs is not None else None
                
                # 获取行业动量分数
                sector_momentum = pick.get("sector_momentum_score", 0) or 0
                fundamental_score = pick.get("fundamental_score", 0.5) or 0.5
                
                concept = str(pick.get("concepts", "")) if pick.get("concepts") else ""
                industry = str(pick.get("industry", "")) if pick.get("industry") else ""
                DBUtils.execute(
                    """INSERT INTO daily_picks
                    (trade_date, ts_code, name, final_score, ai_score, event_score, fund_score, 
                     sector_momentum_score, fundamental_score, track, concept, industry)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (trade_date_str, ts_code, name, final_score, ai_score, event_score, fund_score,
                     sector_momentum, fundamental_score, track, concept, industry)
                )
            print(f"[QuantOrchestrator] 保存 {len(picks)} 只选股到 daily_picks 表")
        except Exception as e:
            print(f"[QuantOrchestrator] 保存选股到数据库失败: {e}")

    def get_tools(self):
        """获取可用的 LangChain 工具（供 LLM 调用）"""
        return self.data_tools


def main():
    """测试入口"""
    orchestrator = QuantOrchestrator()
    result = orchestrator.run(top_k=10)
    print("\n最终摘要:")
    print(result.get("execution_summary", "无"))


if __name__ == "__main__":
    main()
