#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingAgent 主编排器
整合 DecisionEngine / RiskController / ReviewAgent，提供 run_decision / run_monitor / run_review 接口
"""
import time
import json
import os
from datetime import datetime
from typing import Optional

from loguru import logger

from src.utils.config_loader import Config
from src.utils.llm_router import LLMRouter
from src.agent.trade_memory import TradeMemory
from src.agent.decision_engine import DecisionEngine
from src.agent.risk_controller import RiskController
from src.agent.review_agent import ReviewAgent
from src.portfolio.holding_manager import HoldingManager

# 可选集成 QuantOrchestrator
try:
    from src.agent.multi_agent.orchestrator import QuantOrchestrator
    _QUANT_ORCHESTRATOR_AVAILABLE = True
except ImportError:
    _QUANT_ORCHESTRATOR_AVAILABLE = False


class TradingAgent:
    """
    交易Agent主类
    根据配置自动选择 broker（sim / iquant），
    然后驱动 Decision → Monitor → Review 三阶段流程。
    """

    def __init__(self):
        # ---- 选择并初始化 Broker ----
        broker_type = Config.get('trading_agent.broker', 'sim')
        self.broker = self._init_broker(broker_type)

        # ---- 初始化通用组件 ----
        self._router = LLMRouter()
        self._memory = TradeMemory()

        # ---- 子系统 ----
        self._decision_engine = DecisionEngine(self.broker, self._router, self._memory)
        self._risk_controller = RiskController(self.broker, llm_router=self._router)
        self._review_agent = ReviewAgent(self.broker, self._router, self._memory)
        self._holding_manager = HoldingManager()

        # ---- 可选：QuantOrchestrator 引擎 ----
        self._orchestrator = None
        if _QUANT_ORCHESTRATOR_AVAILABLE:
            try:
                self._orchestrator = QuantOrchestrator()
                logger.info("[Agent] QuantOrchestrator 引擎已加载（可选）")
            except Exception as e:
                logger.warning(f"[Agent] QuantOrchestrator 初始化失败: {e}")

        # 监控间隔（秒）
        cfg = Config.get('trading_agent') or {}
        self._monitor_interval: int = int(cfg.get('monitor_interval', 300))

        # 自动从真实持仓同步：让 Agent 看见真实仓位
        # self._sync_real_positions()  # 已清理旧持仓，不再需要同步

        logger.info(f"[Agent] 初始化完成  broker={broker_type}  "
                    f"llm_available={self._router.is_available()}  "
                    f"monitor_interval={self._monitor_interval}s")

    # ------------------------------------------------------------------ #
    #  待执行挂单（入场价监控）
    # ------------------------------------------------------------------ #
    def _save_pending_order(self, ts_code: str, name: str, weight: float,
                             entry_price: float, stop_loss_price: float,
                             reason: str, trade_date: str, buy_phase: int = 1):
        """
        将 buy 指令存入 agent_pending_orders 表，等盘中条件满足后执行。
        buy_phase=1：等价格回落到 entry_price 以下才买（传统限价等待）
        buy_phase=2：等价格上涨到 entry_price 以上才买（确认上涨动量后加仓）
        """
        try:
            from src.utils.db_utils import DBUtils
            # 建表（若不存在）—— buy_phase 字段区分第一批/第二批
            DBUtils.execute("""
                CREATE TABLE IF NOT EXISTS agent_pending_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_code VARCHAR(16) NOT NULL,
                    name VARCHAR(32),
                    weight FLOAT,
                    entry_price FLOAT,
                    stop_loss_price FLOAT,
                    reason TEXT,
                    trade_date VARCHAR(16),
                    status VARCHAR(16) DEFAULT 'pending',
                    buy_phase INT DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    executed_at DATETIME
                )
            """)
            # 兼容旧表：尝试添加 buy_phase 列（已存在时会报错，静默忽略）
            try:
                DBUtils.execute("ALTER TABLE agent_pending_orders ADD COLUMN buy_phase INT DEFAULT 1")
            except Exception:
                pass
            # 同一交易日同一股票同一批次只保留最新挂单
            DBUtils.execute(
                "DELETE FROM agent_pending_orders WHERE ts_code=? AND trade_date=? AND status='pending' AND buy_phase=?",
                (ts_code, trade_date, buy_phase)
            )
            DBUtils.execute(
                """INSERT INTO agent_pending_orders
                   (ts_code, name, weight, entry_price, stop_loss_price, reason, trade_date, buy_phase)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (ts_code, name, weight, entry_price, stop_loss_price, reason, trade_date, buy_phase)
            )
        except Exception as e:
            logger.warning(f"[Agent] 保存挂单失败: {e}")

    def _check_entry_orders(self, trade_date: str, total_assets: float):
        """
        检查待执行挂单，按批次类型触发：
        - buy_phase=1（默认）：当前价 ≤ entry_price 时买入（等回调入场）
        - buy_phase=2（动量确认）：当前价 ≥ entry_price 时买入（等上涨确认后加仓）
        超过5个自然日未触发的挂单自动过期。
        """
        try:
            from src.utils.db_utils import DBUtils
            from src.feeds.realtime_quote import get_realtime_quotes
            from datetime import datetime as dt, timedelta

            # 过期检查：超过5天未成交的挂单设为 expired
            expire_threshold = (dt.now() - timedelta(days=5)).strftime('%Y-%m-%d')
            DBUtils.execute(
                "UPDATE agent_pending_orders SET status='expired' "
                "WHERE status='pending' AND trade_date < ?",
                (expire_threshold,)
            )

            df = DBUtils.query_df(
                "SELECT * FROM agent_pending_orders WHERE status='pending' AND trade_date=?",
                (trade_date,)
            )
            if df.empty:
                return

            codes = df['ts_code'].tolist()
            rt_quotes = get_realtime_quotes(codes)
            if not rt_quotes:
                return

            for _, row in df.iterrows():
                ts_code = str(row['ts_code'])
                entry_price = float(row['entry_price'])
                weight = float(row['weight'])
                order_id = int(row['id'])
                buy_phase = int(row.get('buy_phase', 1))
                name = str(row.get('name', ts_code))

                rt = rt_quotes.get(ts_code, {})
                current_price = float(rt.get('last_price', 0))
                if current_price <= 0:
                    continue

                # 触发条件：phase-1 等回调，phase-2 等上涨确认
                if buy_phase == 2:
                    triggered = current_price >= entry_price
                    condition_desc = f"当前价{current_price:.2f}≥确认价{entry_price:.2f}"
                else:
                    triggered = current_price <= entry_price
                    condition_desc = f"当前价{current_price:.2f}≤入场价{entry_price:.2f}"

                if triggered:
                    amount_yuan = total_assets * weight
                    result = self.broker.buy(ts_code, price=0, amount_yuan=amount_yuan)
                    status = 'executed' if result.success else 'failed'
                    DBUtils.execute(
                        "UPDATE agent_pending_orders SET status=?, executed_at=CURRENT_TIMESTAMP WHERE id=?",
                        (status, order_id)
                    )
                    phase_label = '第二批加仓' if buy_phase == 2 else '入场'
                    if result.success:
                        logger.info(f"[EntryMonitor] {phase_label} {ts_code}: {condition_desc}  "
                                    f"{result.volume}股 @{result.price:.2f}  金额{result.amount:,.0f}")
                        try:
                            from src.utils.notifier import send_alert
                            send_alert(
                                f"Agent{phase_label}成交 {ts_code[:6]}",
                                f"**{name}** ({ts_code[:6]}) {phase_label}成交\n"
                                f"成交价 {result.price:.2f}  数量 {result.volume}股  "
                                f"金额 {result.amount:,.0f}元\n"
                                f"触发条件: {condition_desc}",
                                message_type='agent_trade'
                            )
                        except Exception:
                            pass
                    else:
                        logger.warning(f"[EntryMonitor] {phase_label}买入失败: {ts_code}  {result.msg}")
                else:
                    phase_label = '第二批等确认' if buy_phase == 2 else '等入场'
                    logger.debug(f"[EntryMonitor] {phase_label}: {ts_code}  {condition_desc.split('≥')[0].split('≤')[0].strip()}")
        except Exception as e:
            logger.warning(f"[EntryMonitor] 检查异常: {e}")

    # ------------------------------------------------------------------ #
    #  真实持仓同步
    # ------------------------------------------------------------------ #
    def _sync_real_positions(self):
        """将 positions 表（真实持仓）同步到 broker，使 Agent 决策基于真实仓位"""
        try:
            from src.broker.sim_broker import SimBroker
            if isinstance(self.broker, SimBroker):
                n = self.broker.sync_from_real_positions()
                if n > 0:
                    logger.info(f"[Agent] 已从真实持仓同步 {n} 只股票到模拟账户")
                else:
                    logger.info("[Agent] 真实持仓为空或同步跳过，使用现有模拟账户")
        except Exception as e:
            logger.warning(f"[Agent] 真实持仓同步失败（非致命）: {e}")

    # ------------------------------------------------------------------ #
    #  Broker 工厂
    # ------------------------------------------------------------------ #
    def _init_broker(self, broker_type: str):
        """根据配置类型创建 Broker 实例，连接失败自动降级到 sim"""
        if broker_type == 'xt':
            # 国信iQuant xtquant 实盘接口
            try:
                from src.broker.xt_broker import XtBroker
                xb = XtBroker()
                if xb.connect():
                    logger.info("[Agent] XtBroker 连接成功，使用国信iQuant实盘")
                    return xb
                else:
                    logger.warning("[Agent] XtBroker 连接失败（iQuant客户端未运行？），降级到 SimBroker")
            except Exception as e:
                logger.warning(f"[Agent] XtBroker 初始化异常 ({e})，降级到 SimBroker")

        if broker_type == 'iquant':
            try:
                from src.broker.iquant_broker import IQuantBroker
                ib = IQuantBroker()
                if ib.is_connected():
                    logger.info("[Agent] iQuant 连接成功，使用 IQuantBroker")
                    return ib
                else:
                    logger.warning("[Agent] iQuant 连接失败，降级到 SimBroker")
            except Exception as e:
                logger.warning(f"[Agent] iQuant 初始化异常 ({e})，降级到 SimBroker")

        from src.broker.sim_broker import SimBroker
        logger.info("[Agent] 使用 SimBroker（模拟账户）")
        return SimBroker()

    # ------------------------------------------------------------------ #
    #  盘前决策
    # ------------------------------------------------------------------ #
    def run_decision(self, trade_date: str = None) -> dict:
        """
        盘前决策阶段
        1. 调用 DecisionEngine 生成交易计划
        2. 按计划执行买卖
        Returns:
            交易计划 dict
        """
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')

        print(f"\n{'='*60}")
        print(f"  盘前决策  |  {trade_date}")
        print(f"{'='*60}")

        # 优先加载晨间计划（08:30 morning_push.py 保存），不再独立生成
        plan_path = f"output/agent_plan_{trade_date.replace('-', '')}.json"
        if os.path.exists(plan_path):
            try:
                with open(plan_path, 'r', encoding='utf-8') as f:
                    plan = json.load(f)
                logger.info(f"[Agent] ✅ 已加载晨间计划: {plan_path} (trades={len(plan.get('trades',[]))})")
                # 删除文件，避免次日误读
                os.remove(plan_path)
            except Exception as e:
                logger.warning(f"[Agent] 晨间计划加载失败，重新生成: {e}")
                plan = self._decision_engine.run(trade_date)
        else:
            plan = self._decision_engine.run(trade_date)

        # ── HoldingManager 持仓稳定性管理 ★ ───────────────────────────────
        # 将 DecisionEngine 的选股信号经过持仓稳定性管理器处理
        # 决策：最小持仓期、换仓阈值、止损止盈、换仓比例
        trades = plan.get('trades', [])
        if trades:
            try:
                # 构建信号 DataFrame 供 HoldingManager 使用
                signals = []
                for t in trades:
                    if t.get('action') == 'buy':
                        signals.append({
                            'ts_code': t.get('ts_code', ''),
                            'name': t.get('name', ''),
                            'score': t.get('score', 0),
                            'track': t.get('track', ''),
                        })
                if signals:
                    import pandas as pd
                    signals_df = pd.DataFrame(signals)
                    # 获取上期推荐（用于识别近期常客）
                    prev_picks = set(signals_df['ts_code'].astype(str))
                    # 调用 HoldingManager 决策
                    hm_decision = self._holding_manager.decide(signals_df, trade_date, prev_picks)
                    logger.info(f"[Agent] HoldingManager 决策: {hm_decision.summary}")
                    # 将 HoldingManager 决策结果转换为交易指令
                    new_trades = []
                    # 新买入
                    for buy in hm_decision.buy_list:
                        new_trades.append({
                            'ts_code': buy['ts_code'],
                            'name': buy.get('name', ''),
                            'action': 'buy',
                            'weight': buy.get('weight', 0.15),
                            'reason': buy.get('reason', 'HoldingManager推荐'),
                            'track': buy.get('track', ''),
                        })
                    # 卖出（含强制止损、止盈减仓、换仓卖出）
                    for sell in hm_decision.all_sell:
                        new_trades.append({
                            'ts_code': sell['ts_code'],
                            'name': sell.get('name', ''),
                            'action': 'sell',
                            'weight': 0,
                            'reason': sell.get('reason', 'HoldingManager决策'),
                            'track': sell.get('track', ''),
                        })
                    # 继续持有
                    for hold in hm_decision.hold_list:
                        new_trades.append({
                            'ts_code': hold['ts_code'],
                            'name': hold.get('name', ''),
                            'action': 'hold',
                            'weight': hold.get('weight', 0),
                            'reason': hold.get('reason', '持仓中'),
                            'track': hold.get('track', ''),
                        })
                    if new_trades:
                        logger.info(f"[Agent] HoldingManager 调整后: 买入{len(hm_decision.buy_list)}只 卖出{len(hm_decision.all_sell)}只 持有{len(hm_decision.hold_list)}只")
                        plan['trades'] = new_trades
                        plan['holding_manager_summary'] = hm_decision.summary
            except Exception as e:
                logger.warning(f"[Agent] HoldingManager 执行失败，使用原始计划: {e}")

        # 获取账户信息（无论是否有交易指令都需要）
        try:
            account = self.broker.get_account()
            total_assets = account.total_assets
        except Exception:
            account = None
            total_assets = 0

        trades = plan.get('trades', [])
        if not trades:
            logger.info("[Agent] 决策计划无交易指令，保持现有持仓")
            self._print_plan(plan)
            self._push_decision_to_dingtalk(plan, trade_date,
                                            {'total_assets': total_assets,
                                             'cash': getattr(account, 'cash', 0),
                                             'profit_pct': getattr(account, 'profit_pct', 0)})
            return plan

        # 硬拦截：获取 Agent 自己买入的日期，30天内不卖
        agent_buy_dates = self._decision_engine._get_agent_buy_dates()
        today_dt = datetime.strptime(trade_date, '%Y-%m-%d')

        # 读取单只最大仓位上限
        _max_w = float(
            (Config.get('trading_agent.decision') or {}).get('max_single_weight', 0.20)
        )

        # ── 置信度仓位调整 ★ ─────────────────────────────────────────────
        confidence = plan.get('confidence', 1.0)
        conf_weight_map = {
            (0.0, 0.4):  0.0,   # <40% 置信度：跳过
            (0.4, 0.6):  0.5,   # 40-60%：半仓试探
            (0.6, 0.8):  0.75,  # 60-80%：75% 仓位
            (0.8, 1.0):  1.0,   # >80%：全额执行
        }
        conf_multiplier = 1.0
        for (low, high), mult in conf_weight_map.items():
            if low <= confidence < high:
                conf_multiplier = mult
                break
        if conf_multiplier < 1.0:
            logger.warning(f"[Agent] 置信度 {confidence:.0%}，仓位折扣 {conf_multiplier:.0%}")

        # ── 冷却期检查 ★ ─────────────────────────────────────────────────
        # 获取近期卖出股票，30天内不得再买
        recent_sold = self._get_recent_sold_codes(days=30)

        # ── 现金管理 ★ ───────────────────────────────────────────────────
        # 检查是否需要买入货币基金（空仓超过3天）
        idle_days = self._get_cash_idle_days()
        if idle_days >= 3 and not any(t.get('action') == 'buy' for t in trades):
            logger.info(f"[Agent] 资金空闲 {idle_days} 天，建议买入货币基金（511880）")

        # 执行交易
        for trade in trades:
            ts_code = trade.get('ts_code', '')
            action = trade.get('action', '')
            weight = float(trade.get('weight', 0))
            # 防止 LLM 输出超限仓位（两批合计不得超过 max_single_weight）
            if action == 'buy' and weight > _max_w:
                logger.warning(f"[Agent] ⚠️ LLM weight={weight:.2%} 超过上限 {_max_w:.2%}，已截断")
                weight = _max_w
            reason = trade.get('reason', '')
            entry_price = float(trade.get('entry_price', 0) or 0)

            if not ts_code or not action:
                continue

            # 铁则硬拦截：Agent 自己买入不足最短持仓期的仓位，禁止 sell/reduce
            # A轨(sector_rotation/both)=5天，B轨(dividend/value)=15天
            if action in ('sell', 'reduce') and ts_code in agent_buy_dates:
                last_buy_dt = datetime.strptime(agent_buy_dates[ts_code], '%Y-%m-%d')
                hold_days = (today_dt - last_buy_dt).days
                # 查轨道
                _track = trade.get('track', '')
                _min_hold = 15 if _track in ('dividend', 'value') else 5
                if hold_days < _min_hold:
                    logger.warning(f"[Agent] ⛔ 最短持仓铁则拦截: {ts_code} {trade.get('name','')} "
                                   f"买入仅 {hold_days} 天（< {_min_hold}天/{_track or '?'}轨），强制 hold，拒绝 {action}")
                    trade['action'] = 'hold'
                    action = 'hold'

            logger.info(f"[Agent] 执行交易: {ts_code} {action}  weight={weight:.2%}  "
                        f"entry={entry_price or '市价'}  原因: {reason}")

            try:
                if action == 'buy':
                    if total_assets > 0 and weight > 0:
                        amount_yuan = total_assets * weight
                        result = self.broker.buy(ts_code, price=0, amount_yuan=amount_yuan)
                        if result.success:
                            logger.info(f"[Agent] 买入成功: {ts_code}  {result.volume}股 "
                                        f"@{result.price:.2f}  金额{result.amount:,.0f}  "
                                        f"（仓位 {weight*100:.0f}%）")
                        else:
                            logger.warning(f"[Agent] 买入失败: {ts_code}  {result.msg}")
                    else:
                        logger.warning(f"[Agent] 跳过买入 {ts_code}: total_assets={total_assets} weight={weight}")

                elif action == 'sell':
                    result = self.broker.sell(ts_code)
                    if result.success:
                        logger.info(f"[Agent] 卖出成功: {ts_code}  {result.volume}股 @{result.price:.2f}")
                    else:
                        logger.warning(f"[Agent] 卖出失败: {ts_code}  {result.msg}")

                elif action == 'reduce':
                    # 减仓：按目标 weight 计算保留量，卖出超出部分
                    # 安全检查：weight=0 意味着 LLM 遗漏了目标仓位，拒绝执行防止误清仓
                    if weight <= 0:
                        logger.warning(f"[Agent] ⚠️ reduce 动作 weight=0，拒绝执行以防误清仓 {ts_code}；"
                                       f"如需清仓请使用 sell 动作")
                    else:
                        pos = self.broker.get_position(ts_code)
                        if pos and total_assets > 0:
                            target_value = total_assets * weight
                            current_price = pos.current_price if pos.current_price > 0 else pos.cost
                            target_volume = int(target_value / current_price / 100) * 100
                            reduce_volume = pos.volume - target_volume
                            if reduce_volume > 0:
                                result = self.broker.sell_volume(ts_code, reduce_volume)
                                logger.info(f"[Agent] 减仓 {ts_code}  -{reduce_volume}股")

                elif action == 'hold':
                    logger.info(f"[Agent] 持有 {ts_code}，无操作")

                else:
                    logger.warning(f"[Agent] 未知交易动作: {action}")

            except Exception as e:
                logger.error(f"[Agent] 执行交易 {ts_code} {action} 异常: {e}")

        self._print_plan(plan)
        self._push_decision_to_dingtalk(plan, trade_date, {
            'total_assets': total_assets,
            'cash': getattr(account, 'cash', 0),
            'profit_pct': getattr(account, 'profit_pct', 0),
        })
        return plan

    def _push_decision_to_dingtalk(self, plan: dict, trade_date: str, account_info: dict = None):
        """将盘前决策计划推送到钉钉"""
        try:
            from src.utils.notifier import send_alert
            trades = plan.get('trades', [])
            summary = plan.get('summary', '')
            account_info = account_info or {}

            lines = [f"### 🤖 交易Agent盘前决策 {trade_date}\n"]

            # 账户概况
            if account_info:
                total = account_info.get('total_assets', 0)
                cash = account_info.get('cash', 0)
                pnl = account_info.get('profit_pct', 0)
                lines.append(f"**账户**: 总资产 {total:,.0f}元  现金 {cash:,.0f}元  盈亏 {pnl:+.2f}%\n")

            if summary:
                lines.append(f"**决策摘要**: {summary}\n")

            if trades:
                buy_list = [t for t in trades if t.get('action') == 'buy']
                sell_list = [t for t in trades if t.get('action') in ('sell', 'reduce')]
                hold_list = [t for t in trades if t.get('action') == 'hold']

                if buy_list:
                    lines.append(f"**📈 买入计划（{len(buy_list)}只）**")
                    for t in buy_list:
                        entry = t.get('entry_price', 0)
                        sl = t.get('stop_loss_price', 0)
                        w = t.get('weight', 0)
                        entry_str = f" 入场≤{entry:.2f}" if entry else " 市价"
                        sl_str = f" 止损{sl:.2f}" if sl else ""
                        lines.append(f"  · {t.get('name', t['ts_code'])} ({t['ts_code'][:6]})"
                                     f"  {w*100:.1f}%仓位{entry_str}{sl_str}")
                        lines.append(f"    {t.get('reason', '')[:60]}")
                    lines.append("")

                if sell_list:
                    lines.append(f"**📉 卖出/减仓（{len(sell_list)}只）**")
                    for t in sell_list:
                        action_cn = '全卖' if t.get('action') == 'sell' else f"减至{t.get('weight', 0)*100:.0f}%"
                        lines.append(f"  · {t.get('name', t['ts_code'])} ({t['ts_code'][:6]}) {action_cn}"
                                     f"  {t.get('reason', '')[:50]}")
                    lines.append("")

                if hold_list:
                    names = [f"{t.get('name', t['ts_code'][:6])}" for t in hold_list[:5]]
                    lines.append(f"**⏸ 持有不动（{len(hold_list)}只）**: {' '.join(names)}")
            else:
                lines.append("**今日无交易指令，保持现有持仓**")

            content = '\n'.join(lines)
            send_alert(f"🤖 Agent盘前计划 {trade_date[:10]}", content, message_type='agent_decision')
        except Exception as e:
            logger.warning(f"[Agent] 决策推送钉钉失败（非关键）: {e}")

    # ------------------------------------------------------------------ #
    #  盘中监控
    # ------------------------------------------------------------------ #
    @staticmethod
    def _heartbeat_sleep(seconds: float, label: str = "Monitor", heartbeat_interval: int = 600):
        """分段睡眠，每 heartbeat_interval 秒输出一条心跳日志，防止长时沉默掩盖崩溃"""
        remaining = seconds
        while remaining > 0:
            chunk = min(remaining, heartbeat_interval)
            time.sleep(chunk)
            remaining -= chunk
            if remaining > 0:
                logger.info(f"[{label}] 💓 心跳  还需等待 {remaining/60:.1f} 分钟")

    def run_monitor(self, duration_minutes: int = 330):
        """
        盘中监控
        Args:
            duration_minutes: 监控时长（分钟），A股交易时间约330分钟（09:30-15:00）
        """
        print(f"\n{'='*60}")
        print(f"  盘中监控开始  |  预计监控 {duration_minutes} 分钟  "
              f"|  检查间隔 {self._monitor_interval}s")
        print(f"{'='*60}")

        start_time = datetime.now()
        end_time_deadline = start_time.timestamp() + duration_minutes * 60

        iteration = 0
        try:
            while datetime.now().timestamp() < end_time_deadline:
                now = datetime.now()
                now_str = now.strftime('%H:%M:%S')
                t = now.hour * 60 + now.minute

                # 15:00 后收盘，退出
                if t >= 15 * 60:
                    logger.info(f"[Monitor] {now_str} 已过 15:00，市场收盘，退出监控")
                    break

                # 未到开盘（09:30前），等待
                if t < 9 * 60 + 30:
                    wait = (9 * 60 + 30 - t) * 60
                    logger.info(f"[Monitor] {now_str} 未到开盘，等待 {wait//60} 分钟")
                    self._heartbeat_sleep(min(wait, 3600))
                    continue

                # 午休（11:30-13:00），等待
                if 11 * 60 + 30 < t < 13 * 60:
                    wait = (13 * 60 - t) * 60
                    logger.info(f"[Monitor] {now_str} 午休时段，等待 {wait//60} 分钟")
                    self._heartbeat_sleep(min(wait, 5400))
                    continue

                iteration += 1
                logger.info(f"[Monitor] {now_str} 第{iteration}次检查（风控+入场价监控）")
                trade_date = now.strftime('%Y-%m-%d')

                try:
                    # 1. 风险检查（止损/减仓）
                    risk_actions = self._risk_controller.check()
                    if risk_actions:
                        logger.warning(f"[Monitor] 发现 {len(risk_actions)} 个风险信号，立即执行")
                        results = self._risk_controller.execute_actions(risk_actions)
                        executed = []
                        for r in results:
                            status = '成功' if r.get('success') else '失败'
                            logger.info(f"[Monitor] {r['ts_code']} {r['action']} {status}: {r.get('msg', '')}")
                            if r.get('success'):
                                executed.append(r)
                        # 推送已执行的风控动作
                        if executed:
                            try:
                                from src.utils.notifier import send_alert
                                lines = [f"### ⚠️ Agent风控执行 {now_str}\n"]
                                for r in executed:
                                    action_cn = {'stop_loss': '🚨止损', 'reduce': '✂️减仓'}.get(r.get('action'), r.get('action'))
                                    lines.append(f"- {action_cn} **{r.get('name', r['ts_code'])}** ({r['ts_code'][:6]})"
                                                 f"  {r.get('msg', '')[:60]}")
                                send_alert(f"⚠️ Agent风控 {len(executed)}笔", '\n'.join(lines), message_type='agent_risk')
                            except Exception:
                                pass
                    else:
                        logger.debug(f"[Monitor] {now_str} 无风险信号")

                    # 2. 入场价监控（检查待执行挂单）
                    try:
                        account = self.broker.get_account()
                        total_assets = account.total_assets
                    except Exception:
                        total_assets = 0
                    if total_assets > 0:
                        self._check_entry_orders(trade_date, total_assets)

                except Exception as e:
                    logger.error(f"[Monitor] 第{iteration}次检查异常: {e}", exc_info=True)

                # 等待下一次检查
                elapsed = datetime.now().timestamp() - start_time.timestamp()
                remaining = end_time_deadline - datetime.now().timestamp()
                if remaining <= 0:
                    break

                sleep_time = min(self._monitor_interval, remaining)
                logger.debug(f"[Monitor] 等待 {sleep_time:.0f}s 后下次检查  "
                             f"（已运行 {elapsed/60:.1f}min）")
                time.sleep(sleep_time)

        except Exception as fatal:
            # 捕获监控主循环的致命异常，发钉钉告警，防止无声崩溃
            logger.critical(f"[Monitor] 💀 监控主循环致命异常，已退出！exception={fatal}", exc_info=True)
            try:
                from src.utils.notifier import send_alert
                send_alert(
                    "💀 Agent盘中监控崩溃",
                    f"监控主循环在第 {iteration} 次迭代后发生致命异常，已退出！\n"
                    f"错误: {fatal}\n"
                    f"请立即检查服务器日志并手动重启。",
                    message_type='agent_risk'
                )
            except Exception:
                pass

        logger.info(f"[Monitor] 监控结束，共运行 {iteration} 次检查")
        print(f"\n[Agent] 盘中监控结束")

    # ------------------------------------------------------------------ #
    #  盘后复盘
    # ------------------------------------------------------------------ #
    def run_review(self, trade_date: str = None) -> str:
        """
        盘后复盘阶段
        Returns:
            复盘文本
        """
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')

        print(f"\n{'='*60}")
        print(f"  盘后复盘  |  {trade_date}")
        print(f"{'='*60}")

        review_text = self._review_agent.run(trade_date)
        print(review_text)
        return review_text

    # ------------------------------------------------------------------ #
    #  辅助方法
    # ------------------------------------------------------------------ #
    def _get_recent_sold_codes(self, days: int = 30) -> set:
        """获取最近 N 天内卖出过的股票代码，避免高频反复操作"""
        try:
            from datetime import timedelta as td
            from src.utils.db_utils import DBUtils
            cutoff = (datetime.now() - td(days=days)).strftime('%Y-%m-%d')
            df = DBUtils.query_df(
                """SELECT DISTINCT ts_code FROM agent_sim_orders
                   WHERE side='sell' AND created_at >= ?""",
                (cutoff,)
            )
            return set(df['ts_code'].astype(str).tolist()) if not df.empty else set()
        except Exception:
            return set()

    def _get_cash_idle_days(self) -> int:
        """
        计算账户现金空闲天数：上次建仓距今天数
        用于判断是否需要买入货币基金被动管理
        """
        try:
            from datetime import timedelta as td
            from src.utils.db_utils import DBUtils
            df = DBUtils.query_df(
                """SELECT MIN(created_at) as last_date FROM agent_sim_orders
                   WHERE side='buy' AND created_at >= date('now', '-90 days')"""
            )
            if df.empty:
                return 999
            last_str = df['last_date'].iloc[0]
            if not last_str:
                return 999
            last_buy = datetime.strptime(str(last_str)[:10], '%Y-%m-%d')
            return (datetime.now() - last_buy).days
        except Exception:
            return 0

    # ------------------------------------------------------------------ #
    #  全流程
    # ------------------------------------------------------------------ #
    def run_all(self, trade_date: str = None):
        """执行完整交易日流程：决策 → 监控 → 复盘"""
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')

        logger.info(f"[Agent] 启动全流程  trade_date={trade_date}")
        self.run_decision(trade_date)
        self.run_monitor()
        self.run_review(trade_date)

    # ------------------------------------------------------------------ #
    #  Multi-Agent 选股（可选引擎）
    # ------------------------------------------------------------------ #
    def run_multi_agent(self, trade_date: str = None, top_k: int = 20) -> dict:
        """
        使用 QuantOrchestrator 执行选股工作流（可选引擎）
        工作流：StrategyAgent → RiskAgent → ExecutionAgent
        返回：dict，包含选股结果、风控评估、执行摘要
        """
        if not self._orchestrator:
            logger.warning("[Agent] QuantOrchestrator 不可用，请检查安装")
            return {"success": False, "error": "QuantOrchestrator not available"}

        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        try:
            logger.info(f"[Agent] 启动 Multi-Agent 选股  trade_date={trade_date}  top_k={top_k}")
            state = self._orchestrator.run(trade_date=trade_date, top_k=top_k)
            return {
                "success": True,
                "trade_date": trade_date,
                "stock_count": state.get("stock_count", 0),
                "etf_count": state.get("etf_count", 0),
                "cb_count": state.get("cb_count", 0),
                "top_picks": state.get("top_picks", [])[:10],
                "risk_assessment": state.get("risk_assessment", ""),
                "sell_signals": state.get("sell_signals", []),
                "buy_orders": state.get("buy_orders", []),
                "sell_orders": state.get("sell_orders", []),
                "execution_summary": state.get("execution_summary", ""),
                "error": state.get("error"),
            }
        except Exception as e:
            logger.error(f"[Agent] Multi-Agent 执行失败: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------ #
    #  辅助打印
    # ------------------------------------------------------------------ #
    def _print_plan(self, plan: dict):
        """在终端打印决策计划摘要"""
        print(f"\n市场研判: {plan.get('market_regime', 'N/A')}  "
              f"置信度: {plan.get('confidence', 0):.0%}")
        reasoning = plan.get('reasoning', '')
        if reasoning:
            print(f"逻辑: {reasoning}")

        trades = plan.get('trades', [])
        if trades:
            print(f"\n交易指令 ({len(trades)} 条):")
            for t in trades:
                print(f"  [{t.get('action','').upper():6s}] {t.get('ts_code','')} {t.get('name','')}  "
                      f"weight={t.get('weight',0):.1%}  {t.get('reason','')}")
        else:
            print("  （无交易指令，保持现有持仓）")
