"""
RiskAgent: 风险控制Agent

负责：
1. 持仓风险评估（止损、止盈、熔断）
2. 卖出信号生成
3. 仓位调整建议
4. 风险预警
"""
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from src.utils.db_utils import DBUtils


STOP_LOSS_PCT = -0.08
STOP_GAIN_PCT = 0.15
CIRCUIT_BREAKER_PCT = -0.03


class RiskAgent:
    """风险控制Agent"""

    def __init__(
        self,
        stop_loss_pct: float = STOP_LOSS_PCT,
        stop_gain_pct: float = STOP_GAIN_PCT,
        circuit_breaker_pct: float = CIRCUIT_BREAKER_PCT
    ):
        self.stop_loss_pct = stop_loss_pct
        self.stop_gain_pct = stop_gain_pct
        self.circuit_breaker_pct = circuit_breaker_pct
        print(f"[RiskAgent] 初始化完成 | 止损:{stop_loss_pct*100}% 止盈:{stop_gain_pct*100}% 熔断:{circuit_breaker_pct*100}%")

    def get_positions(self) -> pd.DataFrame:
        """获取当前持仓"""
        try:
            df = DBUtils.query_df("""
                SELECT ts_code, name, shares, avg_cost, current_price,
                       profit_loss_pct as profit_pct, 
                       DATEDIFF(CURDATE(), buy_date) as holding_days, 
                       stop_loss_price
                FROM positions
                WHERE shares > 0
                ORDER BY profit_loss_pct ASC
            """)
            return df
        except Exception as e:
            print(f"[RiskAgent] 获取持仓失败: {e}")
            return pd.DataFrame()

    def get_market_status(self, trade_date: str) -> Dict[str, Any]:
        """获取市场状态（用于熔断判断）"""
        try:
            df = DBUtils.query_df("""
                SELECT AVG((sd.close - prev.close) / prev.close * 100) as market_avg
                FROM stock_daily sd
                LEFT JOIN (
                    SELECT ts_code, close
                    FROM stock_daily
                    WHERE trade_date = (
                        SELECT MAX(trade_date) FROM stock_daily WHERE trade_date < ?
                    )
                ) prev ON sd.ts_code = prev.ts_code
                WHERE sd.trade_date = ?
            """, (trade_date, trade_date))
            if df.empty:
                return {"market_avg": 0.0, "triggered": False}
            market_avg = float(df.iloc[0]['market_avg'] or 0)
            return {
                "market_avg": market_avg,
                "triggered": market_avg < -2.0
            }
        except Exception as e:
            print(f"[RiskAgent] 市场状态查询失败: {e}")
            return {"market_avg": 0.0, "triggered": False}

    def assess_position_risk(self, row: pd.Series, market_avg: float = 0) -> Dict[str, Any]:
        """评估单只持仓风险"""
        ts_code = str(row.get('ts_code', ''))
        name = str(row.get('name', ''))
        profit_pct = float(row.get('profit_pct', 0) or 0)
        holding_days = int(row.get('holding_days', 0) or 0)
        current_price = float(row.get('current_price', 0) or 0)
        avg_cost = float(row.get('avg_cost', 0) or 0)
        stop_loss_price = float(row.get('stop_loss_price', 0) or 0)

        risk_level = "normal"
        signal = None
        reason = ""

        if profit_pct <= self.stop_loss_pct:
            risk_level = "critical"
            signal = "STOP_LOSS"
            reason = f"亏损超过{abs(self.stop_loss_pct)*100:.0f}%，触发止损"
        elif profit_pct >= self.stop_gain_pct:
            risk_level = "warning"
            signal = "TAKE_PROFIT"
            reason = f"盈利超过{self.stop_gain_pct*100:.0f}%，建议止盈"
        elif profit_pct <= self.circuit_breaker_pct:
            risk_level = "high"
            signal = "CIRCUIT_BREAKER"
            reason = f"亏损达{abs(self.circuit_breaker_pct)*100:.0f}%，触发熔断"

        if stop_loss_price > 0 and current_price <= stop_loss_price:
            risk_level = "critical"
            signal = "STOP_LOSS"
            reason = f"价格触及止损价{stop_loss_price}"

        if market_avg < -2.0 and profit_pct < 0:
            risk_level = max(risk_level, "high")
            reason += "（市场大跌，注意风险）"

        if holding_days > 10 and profit_pct < -0.05:
            risk_level = "warning"
            reason += "（持仓超过10日且亏损）"

        return {
            "ts_code": ts_code,
            "name": name,
            "profit_pct": profit_pct,
            "holding_days": holding_days,
            "risk_level": risk_level,
            "signal": signal,
            "reason": reason
        }

    def generate_sell_signals(self, trade_date: str) -> List[Dict[str, Any]]:
        """生成卖出信号"""
        positions = self.get_positions()
        if positions.empty:
            print("[RiskAgent] 当前无持仓")
            return []

        market_status = self.get_market_status(trade_date)
        market_avg = market_status.get("market_avg", 0)

        sell_signals = []
        for _, row in positions.iterrows():
            risk = self.assess_position_risk(row, market_avg)
            if risk["signal"]:
                sell_signals.append(risk)

        if sell_signals:
            print(f"[RiskAgent] 生成 {len(sell_signals)} 个卖出信号")
            for sig in sell_signals:
                print(f"  {sig['ts_code']} {sig['name']}: {sig['signal']} - {sig['reason']}")

        return sell_signals

    def generate_position_adjustments(self, trade_date: str) -> List[Dict[str, Any]]:
        """生成仓位调整建议"""
        positions = self.get_positions()
        if positions.empty:
            return []

        market_status = self.get_market_status(trade_date)
        adjustments = []

        if market_status.get("triggered"):
            for _, row in positions.iterrows():
                if float(row.get('profit_pct', 0) or 0) < 0:
                    adjustments.append({
                        "ts_code": str(row.get('ts_code', '')),
                        "name": str(row.get('name', '')),
                        "action": "REDUCE",
                        "pct": 0.5,
                        "reason": "市场大跌且持仓亏损，减仓50%"
                    })

        for _, row in positions.iterrows():
            holding_days = int(row.get('holding_days', 0) or 0)
            profit_pct = float(row.get('profit_pct', 0) or 0)
            if holding_days > 15 and profit_pct < 0:
                adjustments.append({
                    "ts_code": str(row.get('ts_code', '')),
                    "name": str(row.get('name', '')),
                    "action": "REVIEW",
                    "pct": 0,
                    "reason": f"持仓{holding_days}日仍亏损，建议重新评估"
                })

        return adjustments

    def assess_portfolio_risk(self, trade_date: str) -> str:
        """评估整体组合风险"""
        positions = self.get_positions()
        if positions.empty:
            return "无持仓，市场敞口为0"

        total_loss = (positions['profit_pct'] < 0).sum()
        total_profit = (positions['profit_pct'] > 0).sum()
        avg_profit = positions['profit_pct'].mean()

        critical_count = 0
        for _, row in positions.iterrows():
            if float(row.get('profit_pct', 0) or 0) <= self.stop_loss_pct:
                critical_count += 1

        if critical_count > 0:
            level = "高风险"
            desc = f"{critical_count}只股票触发止损线，需立即处理"
        elif avg_profit < -0.05:
            level = "中高风险"
            desc = f"平均亏损{abs(avg_profit)*100:.1f}%，需关注"
        elif avg_profit < 0:
            level = "中等风险"
            desc = f"平均小幅亏损{abs(avg_profit)*100:.1f}%，继续观察"
        else:
            level = "低风险"
            desc = f"平均盈利{avg_profit*100:.1f}%，持仓健康"

        return f"[{level}] 盈{total_profit}只/亏{total_loss}只 {desc}"

    def run(self, trade_date: str) -> Dict[str, Any]:
        """完整执行风险分析"""
        print(f"\n{'='*60}")
        print(f"[RiskAgent] 开始执行 | 日期: {trade_date}")
        print(f"{'='*60}")

        sell_signals = self.generate_sell_signals(trade_date)
        adjustments = self.generate_position_adjustments(trade_date)
        portfolio_risk = self.assess_portfolio_risk(trade_date)

        result = {
            "risk_assessment": portfolio_risk,
            "sell_signals": sell_signals,
            "position_adjustments": adjustments,
            "critical_count": len([s for s in sell_signals if s.get('risk_level') == 'critical']),
            "warning_count": len([s for s in sell_signals if s.get('risk_level') == 'warning'])
        }

        print(f"[RiskAgent] 执行完成 | {portfolio_risk}")
        return result
