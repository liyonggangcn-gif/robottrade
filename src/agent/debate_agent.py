"""
Multi-Agent Debate - 策略互验
让多个Agent辩论，选出最优策略
"""
from typing import List, Dict, Any
from loguru import logger

from src.utils.llm_router import LLMRouter
from src.strategy.center import StrategyCenter


class DebateAgent:
    """辩论Agent"""

    ROLES = {
        "bull": "你是看涨分析师，擅长发现股票的上涨潜力",
        "bear": "你是看跌分析师，擅长识别风险和问题", 
        "neutral": "你是中性分析师，客观分析优缺点"
    }

    def __init__(self, llm_router: LLMRouter = None):
        self.llm = llm_router or LLMRouter()
        self.sc = StrategyCenter()

    def debate(self, ts_code: str, trade_date: str = None) -> Dict[str, Any]:
        """执行多Agent辩论
        
        Args:
            ts_code: 股票代码
            trade_date: 交易日期
            
        Returns:
            {'bull': '...', 'bear': '...', 'neutral': '...', 'verdict': '...'}
        """
        # 获取股票信息
        stock_info = self._get_stock_info(ts_code)
        
        # 三个Agent分别分析
        results = {}
        
        for role, prompt in self.ROLES.items():
            full_prompt = f"""{prompt}

股票信息：
{stock_info}

请分析这只股票是否值得买入，给出你的观点和理由（100字以内）"""
            
            response = self.llm.analyze(full_prompt, max_tokens=300)
            results[role] = response.strip()
        
        # 综合裁决
        results['verdict'] = self._make_verdict(results)
        
        return results

    def _get_stock_info(self, ts_code: str) -> str:
        """获取股票基本信息"""
        try:
            from src.utils.db_utils import DBUtils
            
            df = DBUtils.query_df("""
                SELECT sd.ts_code, sd.close, sd.pe_ttm, sd.roe, sd.total_mv,
                       sf.rsi_14, sf.macd_hist, sf.price_pos_52w
                FROM stock_daily sd
                LEFT JOIN stock_factors sf ON sd.ts_code = sf.ts_code AND sd.trade_date = sf.trade_date
                WHERE sd.ts_code = %s
                ORDER BY sd.trade_date DESC
                LIMIT 1
            """, (ts_code,))
            
            if df.empty:
                return f"股票: {ts_code}"
            
            r = df.iloc[0]
            return f"""股票: {ts_code}
价格: {r['close']}
PE: {r.get('pe_ttm', 'N/A')}
ROE: {r.get('roe', 'N/A')}%
市值: {r.get('total_mv', 'N/A')}
RSI: {r.get('rsi_14', 'N/A')}
MACD: {r.get('macd_hist', 'N/A')}
52周位置: {r.get('price_pos_52w', 'N/A')}%"""
        except Exception as e:
            logger.warning(f"Get stock info failed: {e}")
            return f"股票: {ts_code}"

    def _make_verdict(self, results: Dict) -> str:
        """综合裁决"""
        prompt = f"""以下是三个分析师的观点：

看涨：{results['bull']}
看跌：{results['bear']}
中性：{results['neutral']}

请综合分析，给出最终建议（买入/观望/卖出），50字以内"""
        
        return self.llm.analyze(prompt, max_tokens=100).strip()

    def debate_pick(self, candidates: List[Dict]) -> List[Dict]:
        """对候选股票进行辩论排序
        
        Args:
            candidates: [{'ts_code': '...', 'score': ...}, ...]
            
        Returns:
            [{'ts_code': '...', 'verdict': '...', 'score': ...}, ...]
        """
        ranked = []
        
        for c in candidates:
            result = self.debate(c['ts_code'])
            
            # 简单评分
            verdict = result.get('verdict', '')
            if '买入' in verdict:
                score = 2
            elif '观望' in verdict:
                score = 1
            else:
                score = 0
                
            ranked.append({
                'ts_code': c['ts_code'],
                'verdict': verdict,
                'score': score,
                'bull_view': result.get('bull', ''),
                'bear_view': result.get('bear', '')
            })
        
        # 按分数排序
        ranked.sort(key=lambda x: x['score'], reverse=True)
        
        return ranked


# 测试
if __name__ == "__main__":
    agent = DebateAgent()
    
    print("=== Test Debate ===")
    result = agent.debate("002947.SZ")
    print("Bull:", result['bull'][:100])
    print("Bear:", result['bear'][:100])
    print("Verdict:", result['verdict'])
    print("=== Done ===")