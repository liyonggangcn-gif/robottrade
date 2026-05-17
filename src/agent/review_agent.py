#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘后复盘Agent
自动分析当日交易表现，提取经验教训，存入记忆
"""
from datetime import datetime, timedelta
from typing import List

from loguru import logger

from src.broker.base_broker import BaseBroker
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.utils.llm_router import LLMRouter
from src.agent.trade_memory import TradeMemory


class ReviewAgent:
    """
    盘后复盘 Agent
    1. 汇总当日交易和持仓盈亏
    2. 调用 LLM 进行归因分析
    3. 保存复盘结论到记忆和数据库
    4. 推送钉钉
    """

    def __init__(self, broker: BaseBroker, llm_router: LLMRouter, memory: TradeMemory):
        self._broker = broker
        self._router = llm_router
        self._memory = memory
        self._ensure_table()

    # ------------------------------------------------------------------ #
    #  表结构
    # ------------------------------------------------------------------ #
    def _ensure_table(self):
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS agent_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date VARCHAR(10),
                total_pnl FLOAT,
                pnl_pct FLOAT,
                review_text TEXT,
                created_at VARCHAR(20)
            )
        """)

    # ------------------------------------------------------------------ #
    #  决策对比分析
    # ------------------------------------------------------------------ #
    def _get_today_plan(self, trade_date: str) -> dict:
        """获取当日决策计划（agent_decisions 表）"""
        try:
            df = DBUtils.query_df(
                "SELECT plan_json FROM agent_decisions WHERE trade_date=? ORDER BY id DESC LIMIT 1",
                (trade_date,)
            )
            if not df.empty:
                import json
                return json.loads(df['plan_json'].iloc[0] or '{}')
        except Exception:
            pass
        return {}

    def _calc_trade_stats(self, trade_date: str) -> dict:
        """计算历史交易统计（胜率/盈亏比/平均持仓天数）"""
        try:
            # 近30天已平仓交易
            cutoff = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            df = DBUtils.query_df(
                """SELECT profit_loss, hold_days FROM trade_history
                   WHERE close_date >= ? AND profit_loss IS NOT NULL""",
                (cutoff,)
            )
            if df.empty:
                return {}
            wins = df[df['profit_loss'] > 0]
            losses = df[df['profit_loss'] <= 0]
            total = len(df)
            return {
                'total_trades': total,
                'win_rate': len(wins) / total if total > 0 else 0,
                'avg_win': float(wins['profit_loss'].mean()) if not wins.empty else 0,
                'avg_loss': float(losses['profit_loss'].mean()) if not losses.empty else 0,
                'avg_hold_days': float(df['hold_days'].mean()) if 'hold_days' in df.columns else 0,
                'profit_factor': abs(float(wins['profit_loss'].sum()) / float(losses['profit_loss'].sum()))
                    if not losses.empty and losses['profit_loss'].sum() != 0 else 0,
            }
        except Exception:
            return {}

    def _format_plan_vs_actual(self, plan: dict, orders: List[dict]) -> str:
        """对比决策计划 vs 实际执行"""
        if not plan:
            return "（无决策计划记录）"
        lines = []
        trades = plan.get('trades', [])
        if trades:
            lines.append(f"计划交易 {len(trades)} 笔:")
            executed_codes = {o.get('ts_code', '') for o in orders}
            for t in trades:
                code = t.get('ts_code', '')
                action = t.get('action', '')
                planned = f"{action} {code} {t.get('name', '')}"
                executed = '已执行' if code in executed_codes else '未执行'
                lines.append(f"  {planned} → {executed}")
        else:
            lines.append("计划: 无交易指令（持仓不动）")
        return '\n'.join(lines)
    def _get_today_orders(self, trade_date: str) -> List[dict]:
        """读取当日所有订单"""
        try:
            df = DBUtils.query_df(
                """SELECT ts_code, side, price, volume, amount, status, created_at
                   FROM agent_sim_orders
                   WHERE created_at LIKE ?
                   ORDER BY created_at""",
                (f"{trade_date}%",)
            )
            return df.to_dict('records') if not df.empty else []
        except Exception as e:
            logger.warning(f"[Review] 查询订单失败: {e}")
            return []

    def _calc_day_pnl(self, orders: List[dict], positions: list = None) -> tuple:
        """计算当日盈亏
        Returns:
            (unrealized_pnl, cash_flow)
            unrealized_pnl: 持仓浮盈合计 = sum((现价-成本)*持仓量)
            cash_flow: 净现金流 = 卖出额 - 买入额（负表示净买入）
        """
        buy_amount = sum(float(o.get('amount', 0)) for o in orders if o.get('side') == 'buy')
        sell_amount = sum(float(o.get('amount', 0)) for o in orders if o.get('side') == 'sell')
        cash_flow = sell_amount - buy_amount

        unrealized_pnl = 0.0
        if positions:
            for p in positions:
                unrealized_pnl += (p.current_price - p.cost) * p.volume

        return unrealized_pnl, cash_flow

    def _format_orders(self, orders: List[dict]) -> str:
        """格式化订单列表为文本"""
        if not orders:
            return "（今日无交易）"
        lines = ["代码 | 方向 | 价格 | 数量 | 金额 | 时间"]
        for o in orders:
            lines.append(
                f"{o.get('ts_code','')} | {o.get('side','')} | "
                f"{float(o.get('price',0)):.2f} | {o.get('volume','')} | "
                f"{float(o.get('amount',0)):,.0f} | {str(o.get('created_at',''))[:16]}"
            )
        return '\n'.join(lines)

    def _format_positions(self, positions: list) -> str:
        """格式化持仓列表为文本"""
        if not positions:
            return "（当前无持仓）"
        lines = ["代码 | 名称 | 持仓量 | 成本 | 现价 | 盈亏%"]
        total_pnl_val = 0.0
        for p in positions:
            pnl = (p.current_price - p.cost) * p.volume
            total_pnl_val += pnl
            lines.append(
                f"{p.ts_code} | {p.name} | {p.volume} | "
                f"{p.cost:.2f} | {p.current_price:.2f} | {p.profit_pct:+.1f}% "
                f"({pnl:+,.0f}元)"
            )
        lines.append(f"\n持仓浮盈合计: {total_pnl_val:+,.0f}元")
        return '\n'.join(lines)

    # ------------------------------------------------------------------ #
    #  LLM 分析
    # ------------------------------------------------------------------ #
    def _get_market_context(self, trade_date: str) -> str:
        """调用 V3 获取当日市场简述"""
        prompt = (
            f"今日（{trade_date}）A股市场简述，请用2-3句话概括大盘走势和主要板块表现。"
            "若不知道具体数据，请给出合理的市场状态描述。"
        )
        result = self._router.fast_query(prompt, max_tokens=200)
        return result or "（市场数据不可用）"

    def _analyze_performance(self, trade_date: str, orders_text: str,
                             positions_text: str, day_pnl: float,
                             account_info, market_context: str,
                             plan_vs_actual: str = '', trade_stats: dict = None) -> str:
        """调用 R1 进行绩效归因分析（增强版 ★）"""
        total_assets = account_info.total_assets if account_info else 0
        pnl_pct = day_pnl / total_assets * 100 if total_assets > 0 else 0

        # 历史统计
        stats_text = ''
        if trade_stats:
            stats_text = (
                f"近30天交易统计：{trade_stats.get('total_trades',0)}笔 | "
                f"胜率{trade_stats.get('win_rate',0)*100:.0f}% | "
                f"盈亏比{trade_stats.get('profit_factor',0):.2f} | "
                f"平均持仓{trade_stats.get('avg_hold_days',0):.0f}天"
            )

        prompt = f"""
交易日期：{trade_date}
市场背景：{market_context}

{plan_vs_actual}

当日实际交易：
{orders_text}

当前持仓情况：
{positions_text}

持仓浮盈：{day_pnl:+,.0f}元（约{pnl_pct:+.2f}%总资产）
账户总资产：{total_assets:,.0f}元
{stats_text}

---
请分析今日交易表现，给出以下五点：

a) **盈亏归因**：哪些操作贡献了盈亏，原因是什么？
b) **计划 vs 实际**：决策计划是否被完全执行？哪些偏离了？为什么？
c) **个股判断**：今日哪只/哪些判断正确？哪些判断失误？为什么？
d) **明日关注点**：基于今日情况，明日需要重点关注什么？（具体到股票或板块）
e) **策略改进建议**：从今日交易中，有哪些可以改进量化策略的点？结合历史统计判断趋势。

请结构化回复，逻辑清晰。
"""
        result = self._router.reason(prompt, max_tokens=2500)
        return result or "（LLM分析不可用）"

    # ------------------------------------------------------------------ #
    #  主入口
    # ------------------------------------------------------------------ #
    def run(self, trade_date: str = None) -> str:
        """
        执行盘后复盘
        Returns:
            格式化复盘文本
        """
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')

        logger.info(f"[Review] 开始盘后复盘  trade_date={trade_date}")

        # 1. 获取当日订单
        orders = self._get_today_orders(trade_date)
        logger.info(f"[Review] 当日订单 {len(orders)} 笔")

        # 2. 获取持仓 P&L
        try:
            positions = self._broker.get_positions()
        except Exception as e:
            logger.error(f"[Review] 获取持仓失败: {e}")
            positions = []

        # 3. 计算当日盈亏（浮盈 + 净现金流）
        unrealized_pnl, cash_flow = self._calc_day_pnl(orders, positions)
        day_pnl = unrealized_pnl  # 用持仓浮盈代表当日 P&L

        # 4. 账户信息
        try:
            account_info = self._broker.get_account()
        except Exception:
            account_info = None

        pnl_pct = 0.0
        if account_info and account_info.total_assets > 0:
            pnl_pct = day_pnl / account_info.total_assets * 100

        # 5. 格式化文本
        orders_text = self._format_orders(orders)
        positions_text = self._format_positions(positions)

        # 6. 获取决策计划 ★
        plan = self._get_today_plan(trade_date)
        plan_vs_actual = self._format_plan_vs_actual(plan, orders)

        # 7. 历史交易统计 ★
        trade_stats = self._calc_trade_stats(trade_date)

        # 8. 获取市场背景（V3）
        market_context = ''
        if self._router.is_available():
            market_context = self._get_market_context(trade_date)

        # 9. R1 绩效归因（增强版）
        analysis = ''
        if self._router.is_available():
            analysis = self._analyze_performance(
                trade_date, orders_text, positions_text,
                day_pnl, account_info, market_context,
                plan_vs_actual=plan_vs_actual,
                trade_stats=trade_stats
            )

        # 8. 组装复盘文本
        review_text = self._build_review_text(
            trade_date, day_pnl, pnl_pct, market_context,
            orders_text, positions_text, analysis, account_info,
            trade_stats=trade_stats
        )

        # 9. 从分析中提取记忆并保存
        if analysis:
            self._save_insights(trade_date, analysis, day_pnl)

        # 10. 保存复盘到数据库
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            DBUtils.execute(
                """INSERT INTO agent_reviews
                   (trade_date, total_pnl, pnl_pct, review_text, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (trade_date, day_pnl, pnl_pct, review_text, now)
            )
            logger.info(f"[Review] 复盘已保存  pnl={day_pnl:+,.0f}  pnl_pct={pnl_pct:+.2f}%")
        except Exception as e:
            logger.error(f"[Review] 保存复盘失败: {e}")

        # 11. 发送钉钉
        self._send_review(trade_date, review_text, day_pnl, pnl_pct)

        return review_text

    # ------------------------------------------------------------------ #
    #  辅助方法
    # ------------------------------------------------------------------ #
    def _build_review_text(self, trade_date: str, day_pnl: float, pnl_pct: float,
                           market_context: str, orders_text: str, positions_text: str,
                           analysis: str, account_info, trade_stats: dict = None) -> str:
        """组装完整复盘文本（增强版 ★）"""
        total_assets = account_info.total_assets if account_info else 0
        cash = account_info.cash if account_info else 0
        market_value = account_info.market_value if account_info else 0

        stats_line = ''
        if trade_stats and trade_stats.get('total_trades', 0) > 0:
            stats_line = (
                f"- 近30天：{trade_stats['total_trades']}笔 | "
                f"胜率{trade_stats['win_rate']*100:.0f}% | "
                f"盈亏比{trade_stats['profit_factor']:.2f} | "
                f"均持{trade_stats['avg_hold_days']:.0f}天"
            )

        lines = [
            f"# 盘后复盘 {trade_date}",
            f"",
            f"## 账户概况",
            f"- 总资产: {total_assets:,.0f}元",
            f"- 现金: {cash:,.0f}元  持仓市值: {market_value:,.0f}元",
            f"- 持仓浮盈: {day_pnl:+,.0f}元 ({pnl_pct:+.2f}%)",
            stats_line,
            f"",
            f"## 市场背景",
            market_context,
            f"",
            f"## 今日交易",
            orders_text,
            f"",
            f"## 当前持仓",
            positions_text,
            f"",
            f"## AI复盘分析",
            analysis if analysis else "（LLM不可用，跳过AI分析）",
        ]
        return '\n'.join(lines)

    def _save_insights(self, trade_date: str, analysis: str, day_pnl: float):
        """从分析文本中提取关键经验保存到记忆"""
        try:
            # 根据当日盈亏决定记忆类型
            if day_pnl > 0:
                memory_type = 'win_pattern'
                title = f"{trade_date} 盈利操作总结"
                importance = 3
            elif day_pnl < -100:  # 避免把浮盈≈0的建仓日误判为亏损
                memory_type = 'loss_pattern'
                title = f"{trade_date} 亏损操作复盘"
                importance = 4
            else:
                memory_type = 'strategy_note'
                title = f"{trade_date} 操作复盘"
                importance = 2

            # 截取分析的核心部分（前500字）
            content = analysis[:500] if len(analysis) > 500 else analysis

            self._memory.save(
                memory_type=memory_type,
                title=title,
                content=content,
                trade_date=trade_date,
                importance=importance
            )
            logger.info(f"[Review] 已保存交易记忆: {title}")
        except Exception as e:
            logger.error(f"[Review] 保存记忆失败: {e}")

    def _send_review(self, trade_date: str, review_text: str,
                     day_pnl: float, pnl_pct: float):
        """推送钉钉"""
        try:
            from src.utils.notifier import send_alert
            title = f"【盘后复盘】{trade_date}  持仓浮盈 {day_pnl:+,.0f}元 ({pnl_pct:+.2f}%)"
            # 钉钉消息限制长度，截取前1500字符
            content = review_text[:1500] if len(review_text) > 1500 else review_text
            send_alert(title, content, message_type='review')
            logger.info("[Review] 复盘已推送钉钉")
        except Exception as e:
            logger.debug(f"[Review] 钉钉推送失败（非关键错误）: {e}")
