"""
Tool Calling Agent - LLM自动调用数据工具
基于ReAct框架，让LLM决定调用哪个工具
"""
import json
from typing import List, Dict, Any, Optional
from loguru import logger

from src.utils.llm_router import LLMRouter
from src.agent.multi_agent.tools.data_tools import get_data_tools


class ToolAgent:
    """工具调用Agent - 基于ReAct"""

    SYSTEM_PROMPT = """你是一个专业的股票量化分析师助手。

可用的工具：
- query_market_summary: 查询市场概况（大盘涨跌）
- query_northbound_flow: 查询北向资金流向
- query_stock_daily: 查询个股行情
- query_stock_factors: 查询技术因子
- query_hot_concepts: 查询热点概念
- query_daily_picks: 查询今日选股结果
- query_portfolio: 查询持仓

规则：
1. 先理解用户问题
2. 选择合适的工具调用
3. 根据返回结果回答问题
4. 如果需要多个工具，可以连续调用

回复格式：
- 思考：你的分析
- 动作：工具名(参数)
- 观察：工具返回结果
- 回答：最终回复"""

    def __init__(self, llm_router: LLMRouter = None):
        self.llm = llm_router or LLMRouter()
        self.tools = get_data_tools()
        self.tool_map = {t.name: t for t in self.tools}

        # 构建工具描述用于LLM
        self.tool_descriptions = self._build_tool_desc()

    def _build_tool_desc(self) -> str:
        """构建工具描述"""
        lines = ["可用的工具："]
        for t in self.tools:
            desc = t.description or "无描述"
            lines.append(f"- {t.name}: {desc}")
        return "\n".join(lines)

    def run(self, user_query: str) -> Dict[str, Any]:
        """执行Tool Calling - ReAct循环"""
        system = self.SYSTEM_PROMPT + "\n\n" + self.tool_descriptions
        
        # 调用LLM
        content = self.llm.analyze(user_query, system=system)

        # 解析工具调用
        result = self._parse_and_call(content)
        
        return {
            "thought": content[:500],
            "tool_calls": result.get("calls", []),
            "observations": result.get("observations", []),
            "answer": result.get("answer", content)
        }

    def _parse_and_call(self, content: str) -> Dict[str, Any]:
        """解析LLM响应，提取并调用工具"""
        import re

        calls = []
        observations = []

        # 匹配 tool_name(args) 格式
        pattern = r'[\u4e00-\u9fa5a-zA-Z]+_[\w]+\([^)]*\)'
        matches = re.findall(pattern, content)

        for match in matches:
            try:
                # 解析工具名和参数
                if '(' in match and ')' in match:
                    tool_name = match[:match.index('(')]
                    args_str = match[match.index('(')+1:match.index(')')]
                    
                    if tool_name in self.tool_map:
                        tool = self.tool_map[tool_name]
                        # 简单参数解析
                        args = {}
                        if args_str:
                            # 尝试解析常见格式
                            if '=' in args_str:
                                for pair in args_str.split(','):
                                    k, v = pair.split('=')
                                    args[k.strip()] = v.strip().strip('"\'')
                            
                        result = tool.invoke(args)
                        calls.append({"tool": tool_name, "args": args, "result": str(result)[:200]})
                        observations.append(str(result)[:200])
            except Exception as e:
                logger.warning(f"Tool call failed: {e}")

        # 如果没有工具调用，返回原文
        if not calls:
            return {"answer": content}

        return {
            "calls": calls,
            "observations": observations,
            "answer": observations[-1] if observations else content
        }


# 测试
if __name__ == "__main__":
    agent = ToolAgent()
    
    print("=== Test Tool Calling ===")
    result = agent.run("查询今天市场怎么样？")
    print(f"Answer: {result['answer'][:300]}")