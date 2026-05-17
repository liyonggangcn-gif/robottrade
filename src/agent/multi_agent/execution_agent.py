"""
ExecutionAgent: 交易执行Agent

负责：
1. 根据选股结果生成买入订单
2. 根据风险信号生成卖出订单
3. 仓位管理（单只仓位上限、总仓位限制）
4. 执行摘要生成
"""
import pandas as pd
from datetime import datetime
from typing import Optional, List, Dict, Any
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


DEFAULT_POSITION_SIZE_PCT = 0.10
MAX_SINGLE_POSITION_PCT = 0.15
MAX_TOTAL_POSITION_PCT = 0.90


class ExecutionAgent:
    """交易执行Agent"""

    def __init__(
        self,
        position_size_pct: float = DEFAULT_POSITION_SIZE_PCT,
        max_single_pct: float = MAX_SINGLE_POSITION_PCT,
        max_total_pct: float = MAX_TOTAL_POSITION_PCT
    ):
        self.position_size_pct = position_size_pct
        self.max_single_pct = max_single_pct
        self.max_total_pct = max_total_pct
        print(f"[ExecutionAgent] 初始化完成 | 单仓上限:{max_single_pct*100}% 总仓上限:{max_total_pct*100}%")

    def get_available_cash(self) -> float:
        """获取可用资金（元）"""
        try:
            df = DBUtils.query_df("""
                SELECT total_capital FROM account_info
                ORDER BY update_time DESC LIMIT 1
            """)
            if not df.empty:
                return float(df.iloc[0]['total_capital'] or 0)
        except Exception:
            pass
        try:
            df = DBUtils.query_df("""
                SELECT SUM(volume * current_price) as position_value
                FROM positions WHERE volume > 0
            """)
            total_pos = float(df.iloc[0]['position_value'] or 0) if not df.empty else 0
            cfg = Config.get('portfolio') or {}
            total_capital = float(cfg.get('initial_capital', 1000000))
            return total_capital - total_pos
        except Exception:
            return 500000.0

    def get_current_positions(self) -> List[str]:
        """获取当前持仓代码列表"""
        try:
            df = DBUtils.query_df(
                "SELECT ts_code FROM positions WHERE shares > 0"
            )
            return df['ts_code'].tolist() if not df.empty else []
        except Exception:
            return []

    def get_latest_price(self, ts_code: str) -> Optional[float]:
        """获取最新价格"""
        try:
            df = DBUtils.query_df("""
                SELECT close FROM stock_daily
                WHERE ts_code = ?
                ORDER BY trade_date DESC LIMIT 1
            """, (ts_code,))
            if not df.empty:
                return float(df.iloc[0]['close'] or 0)
        except Exception:
            pass
        return None

    def calculate_position_size(
        self,
        ts_code: str,
        score: float,
        available_cash: float
    ) -> int:
        """计算买入数量"""
        price = self.get_latest_price(ts_code)
        if not price or price <= 0:
            return 0

        max_amount = available_cash * self.max_single_pct
        target_amount = available_cash * self.position_size_pct * score

        buy_amount = min(target_amount, max_amount)
        buy_amount = min(buy_amount, available_cash * 0.95)

        volume = int(buy_amount / price / 100) * 100

        return max(volume, 0)

    def generate_buy_orders(
        self,
        picks: List[Dict[str, Any]],
        avoid_codes: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """生成买入订单"""
        if not picks:
            print("[ExecutionAgent] 无选股结果，跳过买入")
            return []

        avoid_codes = avoid_codes or []
        current_positions = set(self.get_current_positions())
        available_cash = self.get_available_cash()

        buy_orders = []
        for pick in picks:
            ts_code = str(pick.get('ts_code', ''))
            name = pick.get('name', '')
            score = float(pick.get('final_score', 0.5))

            if ts_code in current_positions:
                continue
            if ts_code in avoid_codes:
                continue

            volume = self.calculate_position_size(ts_code, score, available_cash)
            price = self.get_latest_price(ts_code)

            if volume > 0 and price:
                available_cash -= volume * price
                buy_orders.append({
                    "ts_code": ts_code,
                    "name": name,
                    "action": "BUY",
                    "volume": volume,
                    "price": price,
                    "amount": volume * price,
                    "score": score,
                    "reason": f"策略评分{score:.3f}，买入{volume}股"
                })

        if buy_orders:
            total_amount = sum(o['amount'] for o in buy_orders)
            print(f"[ExecutionAgent] 生成 {len(buy_orders)} 个买入订单，总金额: {total_amount:.2f}元")
            for order in buy_orders[:3]:
                print(f"  {order['ts_code']} {order['name']}: 买入{order['volume']}股@{order['price']}")

        return buy_orders

    def generate_sell_orders(
        self,
        sell_signals: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """根据风险信号生成卖出订单"""
        if not sell_signals:
            return []

        sell_orders = []
        for signal in sell_signals:
            ts_code = signal.get('ts_code', '')
            name = signal.get('name', '')
            signal_type = signal.get('signal', '')

            try:
                df = DBUtils.query_df("""
                    SELECT shares FROM positions WHERE ts_code = ? AND shares > 0
                """, (ts_code,))
                if df.empty:
                    continue
                volume = int(df.iloc[0]['shares'])
                price = self.get_latest_price(ts_code)
                if volume > 0 and price:
                    sell_orders.append({
                        "ts_code": ts_code,
                        "name": name,
                        "action": "SELL",
                        "volume": volume,
                        "price": price,
                        "amount": volume * price,
                        "signal": signal_type,
                        "reason": signal.get('reason', '')
                    })
            except Exception as e:
                print(f"[ExecutionAgent] 处理卖出信号失败 {ts_code}: {e}")

        if sell_orders:
            print(f"[ExecutionAgent] 生成 {len(sell_orders)} 个卖出订单")
            for order in sell_orders:
                print(f"  {order['ts_code']} {order['name']}: {order['signal']} 卖出{order['volume']}股")

        return sell_orders

    def generate_execution_summary(
        self,
        buy_orders: List[Dict],
        sell_orders: List[Dict],
        trade_date: str
    ) -> str:
        """生成执行摘要"""
        buy_count = len(buy_orders)
        sell_count = len(sell_orders)
        buy_amount = sum(o['amount'] for o in buy_orders)
        sell_amount = sum(o['amount'] for o in sell_orders)

        summary_lines = [
            f"【执行摘要 {trade_date}】",
            f"买入: {buy_count} 只，金额 {buy_amount:,.2f} 元",
            f"卖出: {sell_count} 只，金额 {sell_amount:,.2f} 元",
        ]

        if buy_orders:
            summary_lines.append("买入清单:")
            for o in buy_orders[:5]:
                summary_lines.append(
                    f"  {o['ts_code']} {o['name']}: {o['volume']}股 × {o['price']:.2f}"
                )

        if sell_orders:
            summary_lines.append("卖出清单:")
            for o in sell_orders:
                summary_lines.append(
                    f"  {o['ts_code']} {o['name']}: {o['signal']} {o['volume']}股"
                )

        return "\n".join(summary_lines)

    def run(
        self,
        picks: List[Dict[str, Any]],
        sell_signals: List[Dict[str, Any]],
        trade_date: str
    ) -> Dict[str, Any]:
        """完整执行交易生成"""
        print(f"\n{'='*60}")
        print(f"[ExecutionAgent] 开始执行 | 日期: {trade_date}")
        print(f"{'='*60}")

        avoid_codes = [s['ts_code'] for s in sell_signals if s.get('signal') == 'STOP_LOSS']
        buy_orders = self.generate_buy_orders(picks, avoid_codes)
        sell_orders = self.generate_sell_orders(sell_signals)
        summary = self.generate_execution_summary(buy_orders, sell_orders, trade_date)

        result = {
            "buy_orders": buy_orders,
            "sell_orders": sell_orders,
            "execution_summary": summary,
            "buy_count": len(buy_orders),
            "sell_count": len(sell_orders),
            "total_buy_amount": sum(o['amount'] for o in buy_orders),
            "total_sell_amount": sum(o['amount'] for o in sell_orders)
        }

        print(f"[ExecutionAgent] 执行完成 | 买入{len(buy_orders)}只/卖出{len(sell_orders)}只")
        return result
