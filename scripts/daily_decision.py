#!/usr/bin/env python3
"""
盘前决策：每日自动选股买入
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime
from src.broker.sim_broker import SimBroker
from src.agent.decision_engine import DecisionEngine
from src.utils.llm_router import LLMRouter
from src.agent.trade_memory import TradeMemory
from loguru import logger

logger.add('/home/li/robottrade/logs/agents.log', rotation='1 day', retention='30 days')

def run_daily():
    now = datetime.now()
    trade_date = now.strftime('%Y-%m-%d')
    logger.info(f"[Agent] 盘前决策 {trade_date}")
    
    broker = SimBroker()
    router = LLMRouter()
    memory = TradeMemory()
    engine = DecisionEngine(broker, router, memory)
    
    try:
        result = engine.run(trade_date)
        trades = result.get('trades', [])
        buys = [t for t in trades if t.get('action') == 'buy']
        
        logger.info(f"[Agent] 买入信号: {len(buys)}只")
        
        # 执行买入
        for t in buys[:5]:
            ts = t['ts_code']
            acc = broker.get_account()
            amount = acc.cash / 5 if buys else acc.cash
            
            r = broker.buy(ts, price=0, amount_yuan=amount)
            logger.info(f"  {'✅' if r.success else '❌'} 买入 {ts}: {r.msg}")
        
        # 输出账户
        acc = broker.get_account()
        logger.info(f"[Agent] 总资产: {acc.total_assets:,.0f} 持仓: {acc.market_value:,.0f} 现金: {acc.cash:,.0f}")
        
        return result
        
    except Exception as e:
        logger.error(f"[Agent] 异常: {e}")
        return None

if __name__ == '__main__':
    run_daily()