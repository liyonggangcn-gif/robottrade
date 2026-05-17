#!/usr/bin/env python3
"""
周报统计：每周盈亏汇总 + 策略优化建议
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timedelta
from src.broker.sim_broker import SimBroker
from src.utils.db_utils import DBUtils
from src.utils.notifier import send_alert
from loguru import logger

logger.add('/home/li/robottrade/logs/weekly.log', rotation='1 week', retention='12 weeks')

def run_weekly():
    now = datetime.now()
    weekly = now.strftime('%Y-W%W')
    logger.info(f"[Weekly] 周报 {weekly}")
    
    broker = SimBroker()
    acc = broker.get_account()
    pos = broker.get_positions()
    
    # 本周买入统计
    this_week = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    df = DBUtils.query_df("""
        SELECT ts_code, volume, cost, created_at 
        FROM agent_sim_positions 
        WHERE created_at >= ?
    """, (this_week,))
    
    total_buy = 0
    if not df.empty:
        df['amount'] = df['volume'] * df['cost']
        total_buy = df['amount'].sum()
    
    logger.info(f"[Weekly] 本周买入: {total_buy:,.0f}")
    
    # 持仓盈亏
    holdings = [
        f"{p.ts_code} {p.name[:6]} {p.profit_pct:+.1f}%"
        for p in pos
    ]
    
    msg = f"""### 📊 Agent周报 {weekly}

**账户**:
- 总资产: {acc.total_assets:,.0f}
- 持仓: {acc.total_market_value:,.0f} ({len(pos)}只)
- 现金: {acc.cash:,.0f}
- 盈亏: {acc.total_pnl:+,.0f}

**持仓**:
{chr(10).join(holdings) if holdings else '无'}

**操作**:
- 本周买入: {total_buy:,.0f}
"""
    
    logger.info(msg)
    send_alert(msg)
    
    return acc

if __name__ == '__main__':
    run_weekly()