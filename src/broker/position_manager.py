"""
重构持仓管理：实时数据模型 + 盈亏计算 + 风控执行
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.broker.sim_broker import SimBroker

class PositionManager:
    """持仓管理器 - 组合风控策略"""
    
    def __init__(self):
        cfg = Config.get('trading_agent.risk') or {}
        self.stop_loss = float(cfg.get('stop_loss', 0.08))
        self.trailing_stop = float(cfg.get('trailing_stop', 0.05))
        self.atr_multiplier = float(cfg.get('atr_multiplier', 2.5))
        self._broker = SimBroker()
    
    def get_holdings(self) -> list:
        """获取所有持仓，实时价格+盈亏"""
        return self._broker.get_positions()
    
    def get_account_summary(self) -> dict:
        """账户汇总"""
        info = self._broker.get_account_info()
        return {
            'total_assets': info.total_assets,
            'cash': info.cash,
            'total_market_value': info.total_market_value,
            'total_pnl': info.total_pnl,
            'position_ratio': info.total_market_value / info.total_assets * 100 if info.total_assets > 0 else 0
        }
    
    def check_risk(self) -> list:
        """风控检查：ATR止损 + 移动止损 + 均线止损 + 固定止损"""
        holdings = self.get_holdings()
        actions = []
        
        for h in holdings:
            ts = h.ts_code
            pnl_pct = h.profit_pct
            name = h.name
            cost = h.cost
            current = h.current_price
            volume = h.volume
            
            # 跳过异常数据
            if pnl_pct < -50:
                print(f"⚠️ 跳过异常 {ts}: 亏{pnl_pct:.1f}%")
                continue
            
            reasons = []
            
            # 1. ATR止损
            atr_stop = self._get_atr_stop(ts, cost, current)
            if atr_stop > 0 and current <= atr_stop:
                reasons.append(f'ATR{self.atr_multiplier}x')
            
            # 2. 移动止盈 (从峰值回落)
            peak = self._get_peak_price(ts, cost)
            if peak > 0:
                drawdown = (current - peak) / peak * 100
                if drawdown <= -self.trailing_stop * 100:
                    reasons.append(f'回落{drawdown:.0f}%')
            
            # 3. 均线止损 (跌破MA20)
            ma20 = self._get_ma20(ts)
            if ma20 > 0 and current < ma20:
                reasons.append(f'跌破MA20')
            
            # 4. 固定止损
            if pnl_pct <= -self.stop_loss * 100:
                reasons.append(f'亏损{pnl_pct:.1f}%')
            
            if reasons:
                actions.append({
                    'ts_code': ts,
                    'name': name,
                    'volume': volume,
                    'action': 'stop_loss',
                    'reason': '+'.join(reasons),
                    'price': current
                })
                print(f"🚨 止损 {ts} {name}: {' | '.join(reasons)}")
        
        return actions
    
    def _get_atr_stop(self, ts_code: str, entry_price: float, current_price: float) -> float:
        """ATR止损价 = 当前价 - N*ATR"""
        try:
            df = DBUtils.query_df("""
                SELECT high, low, close FROM stock_daily 
                WHERE ts_code=? ORDER BY trade_date DESC LIMIT 21
            """, (ts_code,))
            if df.empty or len(df) < 14:
                return 0
            
            # 计算ATR (14日)
            highs = df['high'].astype(float).values
            lows = df['low'].astype(float).values
            closes = df['close'].astype(float).values
            
            trs = []
            for i in range(1, len(closes)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1])
                )
                trs.append(tr)
            
            if len(trs) < 14:
                return 0
            
            atr = sum(trs[-14:]) / 14
            stop_price = current_price - (self.atr_multiplier * atr)
            
            fixed_stop = entry_price * (1 - self.stop_loss)
            return min(stop_price, fixed_stop) if stop_price > 0 else fixed_stop
            
        except Exception as e:
            return 0
    
    def _get_ma20(self, ts_code: str) -> float:
        """获取20日均线"""
        try:
            df = DBUtils.query_df("""
                SELECT close FROM stock_daily 
                WHERE ts_code=? ORDER BY trade_date DESC LIMIT 20
            """, (ts_code,))
            if df.empty or len(df) < 20:
                return 0
            return df['close'].astype(float).mean()
        except:
            return 0
    
    def _get_peak_price(self, ts_code: str, base_cost: float) -> float:
        """获取持仓期间最高价"""
        try:
            # 读取近20日最高价
            df = DBUtils.query_df("""
                SELECT MAX(high) as peak FROM stock_daily 
                WHERE ts_code=? AND trade_date >= DATE_SUB(CURDATE(), INTERVAL 20 DAY)
            """, (ts_code,))
            if not df.empty:
                peak = float(df.iloc[0]['peak']) if df.iloc[0]['peak'] else 0
                if peak > 0:
                    return peak
        except:
            pass
        return base_cost * 1.2  # 默认20%盈利作为峰值
    
    def execute_risk(self, dry_run: bool = True) -> list:
        """执行风控动作
        dry_run=True: 只检查不执行
        """
        risks = self.check_risk()
        results = []
        
        if not risks:
            print("无风控动作")
            return results
        
        print(f"\n发现 {len(risks)} 个风控动作:")
        
        if dry_run:
            for r in risks:
                print(f"  [预演] {r['action']} {r['ts_code']}: {r['reason']}")
            return risks
        
        # 执行卖出
        from src.broker.sim_broker import SimBroker
        broker = SimBroker()
        
        for r in risks:
            ts = r['ts_code']
            vol = r['volume']
            
            result = broker.sell(ts, price=0)
            status = "✅" if result.success else "❌"
            print(f"  {status} 卖出 {ts} {vol}股: {result.msg}")
            
            results.append({
                'ts_code': ts,
                'success': result.success,
                'msg': result.msg,
                'reason': r['reason']
            })
        
        # 推送通知
        if results:
            self._notify_riskExecution(results)
        
        return results
    
    def _notify_riskExecution(self, results: list):
        """推送风控执行通知"""
        try:
            from src.utils.notifier import send_alert
            lines = [f"### 🚨 Agent风控执行\n"]
            for r in results:
                emoji = "✅" if r['success'] else "❌"
                lines.append(f"{emoji} {r['ts_code']}: {r['reason'][:30]}")
            
            send_alert(f"🚨 风控执行{len(results)}笔", "\n".join(lines), message_type='risk_control')
        except Exception as e:
            print(f"通知失败: {e}")
    
    def get_account_summary(self) -> dict:
        """账户汇总"""
        holdings = self.get_holdings()
        total_mv = sum(h['market_value'] for h in holdings)
        total_pnl = sum(h['pnl'] for h in holdings)
        
        df = DBUtils.query_df("SELECT cash FROM agent_sim_account WHERE id=1")
        cash = float(df.iloc[0]['cash']) if not df.empty else 0
        total_assets = total_mv + cash
        
        return {
            'total_market_value': total_mv,
            'total_pnl': total_pnl,
            'cash': cash,
            'total_assets': total_assets,
            'position_count': len(holdings),
            'position_ratio': total_mv / total_assets * 100 if total_assets > 0 else 0
        }


# 测试
if __name__ == '__main__':
    mgr = PositionManager()
    
    print("=" * 60)
    print("  持仓管理测试")
    print("=" * 60)
    
    # 1. 账户汇总
    acc = mgr.get_account_summary()
    print(f"\n账户汇总:")
    print(f"  总市值: {acc['total_market_value']:,.0f}")
    print(f"  总盈亏: {acc['total_pnl']:,.0f}")
    print(f"  现金:   {acc['cash']:,.0f}")
    print(f"  总资产: {acc['total_assets']:,.0f}")
    print(f"  持仓:  {acc['position_count']}只 ({acc['position_ratio']:.1f}%)")
    
    # 2. 持仓明细
    print(f"\n持仓明细:")
    for h in mgr.get_holdings()[:10]:
        pnl_emoji = "🟢" if h['pnl_pct'] > 0 else "🔴" if h['pnl_pct'] < 0 else "⚪"
        print(f"  {h['ts_code']} {h['name'][:6]}: 成本{h['cost']:.2f} 现{h['current_price']:.2f} {pnl_emoji}{h['pnl_pct']:+.1f}%")
    
    # 3. 风控检查
    print(f"\n风控检查:")
    risks = mgr.check_risk()
    if risks:
        for r in risks:
            print(f"  {r['action']} {r['ts_code']}: {r['reason']}")
    else:
        print("  无风控信号")