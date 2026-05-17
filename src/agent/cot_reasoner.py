"""
CoT (Chain of Thought) - 推理链展示
让Agent展示思考过程，增加透明度
"""
from typing import List, Dict
from loguru import logger

from src.utils.llm_router import LLMRouter


class CoTReasoner:
    """CoT推理链"""

    SYSTEM_PROMPT = """你是一个专业的股票量化分析师。请按以下步骤思考并展示：

步骤：
1. 理解问题 - 用户问的是什么？
2. 信息收集 - 需要哪些数据？
3. 分析推理 - 基于数据如何分析？
4. 生成结论 - 最终建议是什么？

格式要求：
按[步骤1][步骤2][步骤3][步骤4]分段展示
每步用"📝"开头
最后给出结论和建议"""

    def __init__(self, llm_router: LLMRouter = None):
        self.llm = llm_router or LLMRouter()

    def reason(self, query: str, context: str = "") -> Dict[str, str]:
        """执行CoT推理
        
        Returns:
            {'step1': '...', 'step2': '...', 'step3': '...', 'step4': '...', 'full': '...'}
        """
        full_prompt = f"""{context}

{self.SYSTEM_PROMPT}

用户问题: {query}

请按格式展示推理过程："""

        response = self.llm.analyze(full_prompt)
        
        # 解析各步骤
        steps = self._parse_steps(response)
        
        return {
            **steps,
            'full': response,
            'reasoning': response  # 兼容
        }

    def _parse_steps(self, response: str) -> Dict[str, str]:
        """解析推理步骤"""
        result = {
            'step1': '', 'step2': '', 'step3': '', 'step4': ''
        }
        
        current_step = 'step1'
        for line in response.split('\n'):
            line = line.strip()
            if '📝' in line or '步骤' in line or 'Step' in line:
                if '1' in line and '2' not in line:
                    current_step = 'step1'
                elif '2' in line:
                    current_step = 'step2'
                elif '3' in line:
                    current_step = 'step3'
                elif '4' in line:
                    current_step = 'step4'
            
            if current_step in result:
                result[current_step] += line + '\n'
        
        # 清理空步骤
        for k in result:
            if not result[k] or len(result[k]) < 5:
                result[k] = result.get('step1', '')  # 回退
        
        return result

    def get_display(self, reasoning: Dict) -> str:
        """获取可显示的推理链
        
        Args:
            reasoning: reason()返回的字典
            
        Returns:
            格式化的字符串
        """
        lines = ["【推理过程】"]
        
        for i, (key, val) in enumerate(reasoning.items(), 1):
            if key == 'full' or key == 'reasoning' or not val:
                continue
            lines.append(f"\n{i}. {val.strip()}")
        
        if 'step4' in reasoning and reasoning['step4']:
            lines.append("\n【结论】")
            lines.append(reasoning['step4'])
        
        return "\n".join(lines)


# 测试
if __name__ == "__main__":
    cot = CoTReasoner()
    
    print("=== Test CoT ===")
    result = cot.reason("002947.SZ恒铭达可以买入吗？")
    
    print("Full response:")
    print(result['full'][:500])
    
    print("\n=== Done ===")