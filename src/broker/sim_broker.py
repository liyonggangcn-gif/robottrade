#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟经纪商（纸面交易）
持仓/订单持久化到 MySQL/SQLite，成交价取 stock_daily 最新收盘价
"""
from datetime import datetime
from typing import List

from loguru import logger

from src.broker.base_broker import BaseBroker, OrderResult, Position, AccountInfo
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class SimBroker(BaseBroker):
    """模拟经纪商 — 纸面交易，数据存数据库"""

    def __init__(self):
        # 初始资金从配置读取，默认 100 万
        self._initial_capital: float = float(
            Config.get('trading_agent.sim_capital', 1_000_000)
        )
        logger.info(f"[SimBroker] 初始化，初始资金={self._initial_capital:,.0f}")
        self._ensure_tables()
        self._ensure_account()

    # ------------------------------------------------------------------ #
    #  表结构初始化
    # ------------------------------------------------------------------ #
    def _ensure_tables(self):
        """自动建表"""
        # 账户表（只有一条记录，id=1）
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS agent_sim_account (
                id INT PRIMARY KEY,
                cash FLOAT NOT NULL,
                initial_capital FLOAT NOT NULL,
                updated_at VARCHAR(20)
            )
        """)
        # 持仓表
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS agent_sim_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code VARCHAR(20) NOT NULL,
                name VARCHAR(50),
                volume INT NOT NULL DEFAULT 0,
                cost FLOAT NOT NULL DEFAULT 0,
                buy_date VARCHAR(10)
            )
        """)
        # 订单表
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS agent_sim_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code VARCHAR(20) NOT NULL,
                side VARCHAR(10),
                price FLOAT,
                volume INT,
                amount FLOAT,
                status VARCHAR(20),
                created_at VARCHAR(20)
            )
        """)
        logger.debug("[SimBroker] 数据库表检查完毕")

    def _ensure_account(self):
        """如果账户不存在，则初始化"""
        df = DBUtils.query_df(
            "SELECT id FROM agent_sim_account WHERE id=1"
        )
        if df.empty:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            DBUtils.execute(
                "INSERT INTO agent_sim_account (id, cash, initial_capital, updated_at) VALUES (?, ?, ?, ?)",
                (1, self._initial_capital, self._initial_capital, now)
            )
            logger.info(f"[SimBroker] 账户初始化完成，初始现金={self._initial_capital:,.0f}")

    # ------------------------------------------------------------------ #
    #  辅助方法
    # ------------------------------------------------------------------ #
    def _get_fill_price(self, ts_code: str, given_price: float) -> float:
        """成交价：优先实时行情，fallback 给定价格，最后 stock_daily 收盘价"""
        # 1. 实时行情（最准确）
        try:
            from src.feeds.realtime_quote import get_realtime_quotes
            quotes = get_realtime_quotes([ts_code])
            if ts_code in quotes:
                price = float(quotes[ts_code].get('last_price', 0))
                if price > 0:
                    logger.debug(f"[SimBroker] 实时价 {ts_code}={price:.2f} (source={quotes[ts_code].get('source','')})")
                    return price
        except Exception as e:
            logger.debug(f"[SimBroker] 实时行情失败，fallback: {e}")
        # 2. 给定价格（调用方传入的目标价）
        if given_price and given_price > 0:
            return given_price
        # 3. stock_daily 最新收盘价（最后兜底）
        try:
            df = DBUtils.query_df(
                "SELECT close FROM stock_daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                (ts_code,)
            )
            if not df.empty and df['close'].iloc[0] > 0:
                return float(df['close'].iloc[0])
        except Exception as e:
            logger.warning(f"[SimBroker] 获取 {ts_code} 收盘价失败: {e}")
        return 0.0

    def _get_stock_name(self, ts_code: str) -> str:
        """从 stock_info 获取股票名称"""
        try:
            df = DBUtils.query_df(
                "SELECT name FROM stock_info WHERE ts_code=? LIMIT 1",
                (ts_code,)
            )
            if not df.empty:
                return str(df['name'].iloc[0])
        except Exception:
            pass
        return ts_code

    def _get_cash(self) -> float:
        """读取当前现金"""
        df = DBUtils.query_df(
            "SELECT cash FROM agent_sim_account WHERE id=1"
        )
        if df.empty:
            return 0.0
        return float(df['cash'].iloc[0])

    def _update_cash(self, new_cash: float):
        """更新现金余额"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        DBUtils.execute(
            "UPDATE agent_sim_account SET cash=?, updated_at=? WHERE id=1",
            (new_cash, now)
        )

    def _record_order(self, ts_code: str, side: str, price: float,
                      volume: int, amount: float, status: str = 'filled'):
        """记录订单到 agent_sim_orders"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        DBUtils.execute(
            """INSERT INTO agent_sim_orders
               (ts_code, side, price, volume, amount, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ts_code, side, price, volume, amount, status, now)
        )

    # ------------------------------------------------------------------ #
    #  BaseBroker 接口实现
    # ------------------------------------------------------------------ #
    def buy(self, ts_code: str, price: float, amount_yuan: float) -> OrderResult:
        """买入：根据金额自动计算手数，扣减现金，增加持仓"""
        fill_price = self._get_fill_price(ts_code, price)
        volume = self.calc_volume(fill_price, amount_yuan)

        if volume <= 0:
            msg = f"买入数量为0，price={fill_price:.2f} amount={amount_yuan:.0f}"
            logger.warning(f"[SimBroker] {ts_code} {msg}")
            return OrderResult(success=False, ts_code=ts_code, side='buy', msg=msg)

        actual_amount = fill_price * volume
        # 交易成本：万三佣金 + 5bp滑点（买入无印花税）
        commission   = actual_amount * 0.0003
        slippage_fee = actual_amount * 0.0005
        total_cost   = actual_amount + commission + slippage_fee
        cash = self._get_cash()

        if cash < total_cost:
            msg = f"现金不足，需要{total_cost:,.0f}（含费用），可用{cash:,.0f}"
            logger.warning(f"[SimBroker] {ts_code} {msg}")
            return OrderResult(success=False, ts_code=ts_code, side='buy', msg=msg)

        name = self._get_stock_name(ts_code)
        today = datetime.now().strftime('%Y-%m-%d')

        try:
            # 检查是否已持仓（加仓逻辑：加权平均成本）
            df = DBUtils.query_df(
                "SELECT id, volume, cost FROM agent_sim_positions WHERE ts_code=?",
                (ts_code,)
            )
            if not df.empty:
                exist_volume = int(df['volume'].iloc[0])
                exist_cost = float(df['cost'].iloc[0])
                new_volume = exist_volume + volume
                new_cost = (exist_cost * exist_volume + fill_price * volume) / new_volume
                DBUtils.execute(
                    "UPDATE agent_sim_positions SET volume=?, cost=? WHERE ts_code=?",
                    (new_volume, new_cost, ts_code)
                )
                logger.info(f"[SimBroker] 加仓 {ts_code} {name}  +{volume}股 @{fill_price:.2f}  "
                            f"累计{new_volume}股 均价{new_cost:.2f}  费用{commission+slippage_fee:.0f}")
            else:
                DBUtils.execute(
                    """INSERT INTO agent_sim_positions (ts_code, name, volume, cost, buy_date)
                       VALUES (?, ?, ?, ?, ?)""",
                    (ts_code, name, volume, fill_price, today)
                )
                logger.info(f"[SimBroker] 新建仓 {ts_code} {name}  {volume}股 @{fill_price:.2f}  "
                            f"费用{commission+slippage_fee:.0f}（佣金{commission:.0f}+滑点{slippage_fee:.0f}）")

            # 扣减现金（含手续费和滑点）
            self._update_cash(cash - total_cost)
            # 记录订单
            self._record_order(ts_code, 'buy', fill_price, volume, total_cost)

            return OrderResult(
                success=True,
                ts_code=ts_code,
                side='buy',
                price=fill_price,
                volume=volume,
                amount=total_cost,
                msg=f'成功（含费用{commission+slippage_fee:.0f}元）'
            )
        except Exception as e:
            logger.error(f"[SimBroker] 买入 {ts_code} 异常: {e}")
            return OrderResult(success=False, ts_code=ts_code, side='buy', msg=str(e))

    def sell(self, ts_code: str, price: float = 0) -> OrderResult:
        """卖出全部持仓"""
        df = DBUtils.query_df(
            "SELECT volume FROM agent_sim_positions WHERE ts_code=?",
            (ts_code,)
        )
        if df.empty or int(df['volume'].iloc[0]) <= 0:
            msg = f"无持仓可卖: {ts_code}"
            logger.warning(f"[SimBroker] {msg}")
            return OrderResult(success=False, ts_code=ts_code, side='sell', msg=msg)

        volume = int(df['volume'].iloc[0])
        return self.sell_volume(ts_code, volume, price)

    def sell_volume(self, ts_code: str, volume: int, price: float = 0) -> OrderResult:
        """卖出指定数量"""
        df = DBUtils.query_df(
            "SELECT id, volume, cost, name FROM agent_sim_positions WHERE ts_code=?",
            (ts_code,)
        )
        if df.empty:
            msg = f"无持仓: {ts_code}"
            logger.warning(f"[SimBroker] {msg}")
            return OrderResult(success=False, ts_code=ts_code, side='sell', msg=msg)

        exist_volume = int(df['volume'].iloc[0])
        name = str(df['name'].iloc[0]) if 'name' in df.columns else ts_code

        if volume > exist_volume:
            volume = exist_volume  # 最多卖持仓数量

        fill_price = self._get_fill_price(ts_code, price)
        if fill_price <= 0:
            msg = f"无法获取 {ts_code} 价格，卖出中止（防止以0元成交）"
            logger.error(f"[SimBroker] {msg}")
            return OrderResult(success=False, ts_code=ts_code, side='sell', msg=msg)
        actual_amount = fill_price * volume
        # 卖出费用：万三佣金 + 0.1%印花税（仅卖出收取）+ 5bp滑点
        commission  = actual_amount * 0.0003
        stamp_duty  = actual_amount * 0.001
        slippage_fee = actual_amount * 0.0005
        total_fee   = commission + stamp_duty + slippage_fee
        net_proceeds = actual_amount - total_fee  # 实际到手金额
        cash = self._get_cash()

        try:
            remain = exist_volume - volume
            if remain <= 0:
                # 清仓
                DBUtils.execute(
                    "DELETE FROM agent_sim_positions WHERE ts_code=?",
                    (ts_code,)
                )
                logger.info(f"[SimBroker] 清仓 {ts_code} {name}  {volume}股 @{fill_price:.2f}  "
                            f"毛款{actual_amount:,.0f}  费用{total_fee:.0f}  到手{net_proceeds:,.0f}")
            else:
                # 部分减仓
                DBUtils.execute(
                    "UPDATE agent_sim_positions SET volume=? WHERE ts_code=?",
                    (remain, ts_code)
                )
                logger.info(f"[SimBroker] 减仓 {ts_code} {name}  -{volume}股 @{fill_price:.2f}  "
                            f"剩余{remain}股  到手{net_proceeds:,.0f}")

            # 归还现金（扣除手续费+印花税+滑点）
            self._update_cash(cash + net_proceeds)
            # 记录订单（记录实际到手金额）
            self._record_order(ts_code, 'sell', fill_price, volume, net_proceeds)

            return OrderResult(
                success=True,
                ts_code=ts_code,
                side='sell',
                price=fill_price,
                volume=volume,
                amount=net_proceeds,
                msg=f'成功（含费用{total_fee:.0f}元：佣金{commission:.0f}+印花税{stamp_duty:.0f}+滑点{slippage_fee:.0f}）'
            )
        except Exception as e:
            logger.error(f"[SimBroker] 卖出 {ts_code} 异常: {e}")
            return OrderResult(success=False, ts_code=ts_code, side='sell', msg=str(e))

    def get_positions(self) -> List[Position]:
        """读取所有持仓，并补充当前价格"""
        try:
            df = DBUtils.query_df(
                "SELECT ts_code, name, volume, cost, buy_date FROM agent_sim_positions WHERE volume > 0"
            )
        except Exception as e:
            logger.error(f"[SimBroker] 读取持仓失败: {e}")
            return []

        # 批量获取所有持仓实时价，避免逐只串行HTTP请求
        all_codes = [str(r['ts_code']) for _, r in df.iterrows()]
        batch_quotes: dict = {}
        if all_codes:
            try:
                from src.feeds.realtime_quote import get_realtime_quotes
                batch_quotes = get_realtime_quotes(all_codes)
            except Exception as e:
                logger.debug(f"[SimBroker] 批量行情失败，将逐只fallback: {e}")

        positions = []
        for _, row in df.iterrows():
            ts_code = str(row['ts_code'])
            cost = float(row['cost'])
            volume = int(row['volume'])
            # 优先使用批量结果，fallback 逐只查
            # fallback 顺序：实时行情 → stock_daily 最新收盘价 → 成本价（避免盘前实时=0时误用成本价）
            if ts_code in batch_quotes:
                p = float(batch_quotes[ts_code].get('last_price', 0))
                if p > 0:
                    current_price = p
                else:
                    # 盘前/盘后实时行情不可用，回落到 stock_daily 最新收盘
                    try:
                        db_df = DBUtils.query_df(
                            "SELECT close FROM stock_daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                            (ts_code,)
                        )
                        db_close = float(db_df['close'].iloc[0]) if not db_df.empty else 0
                        current_price = db_close if db_close > 0 else cost
                    except Exception:
                        current_price = cost
            else:
                current_price = self._get_fill_price(ts_code, cost)
            market_value = current_price * volume
            profit_pct = (current_price - cost) / cost * 100 if cost > 0 else 0.0

            positions.append(Position(
                ts_code=ts_code,
                name=str(row.get('name', '')),
                volume=volume,
                cost=cost,
                current_price=current_price,
                market_value=market_value,
                profit_pct=profit_pct,
                buy_date=str(row.get('buy_date', ''))
            ))
        return positions

    def get_account(self) -> AccountInfo:
        """读取账户资金信息"""
        try:
            df = DBUtils.query_df(
                "SELECT cash, initial_capital FROM agent_sim_account WHERE id=1"
            )
        except Exception as e:
            logger.error(f"[SimBroker] 读取账户失败: {e}")
            return AccountInfo()

        if df.empty:
            return AccountInfo()

        cash = float(df['cash'].iloc[0])
        initial_capital = float(df['initial_capital'].iloc[0])

        # 计算持仓市值
        positions = self.get_positions()
        market_value = sum(p.market_value for p in positions)
        total_assets = cash + market_value
        profit_pct = (total_assets - initial_capital) / initial_capital * 100 if initial_capital > 0 else 0.0

        return AccountInfo(
            total_assets=total_assets,
            cash=cash,
            market_value=market_value,
            profit_pct=profit_pct
        )

    def cancel_order(self, order_id: str) -> bool:
        """模拟环境下订单已成交，撤单无意义，直接返回 False"""
        logger.warning(f"[SimBroker] 模拟账户订单立即成交，无法撤单: {order_id}")
        return False

    def reset(self):
        """重置模拟账户：清空持仓，恢复初始资金"""
        logger.info(f"[SimBroker] 重置模拟账户，初始资金={self._initial_capital:,.0f}")
        DBUtils.execute("DELETE FROM agent_sim_positions")
        DBUtils.execute("DELETE FROM agent_sim_orders")
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        DBUtils.execute(
            "UPDATE agent_sim_account SET cash=?, updated_at=? WHERE id=1",
            (self._initial_capital, now)
        )
        logger.info("[SimBroker] 重置完成")

    def _read_qmt_holdings(self):
        """从 qmt 库的 holdings 表读取真实持仓（含 ETF）"""
        try:
            import pymysql
            from src.utils.config_loader import Config
            mc = Config.mysql
            if not mc:
                return None
            conn = pymysql.connect(
                host=mc.get('host', 'localhost'),
                port=mc.get('port', 3306),
                user=mc.get('user', 'root'),
                password=mc.get('password', ''),
                database='qmt',        # 固定连 qmt 库
                charset='utf8mb4',
                connect_timeout=8,
            )
            import pandas as pd
            df = pd.read_sql(
                "SELECT code as ts_code, name, cost as avg_cost, price as current_price, "
                "pnl as profit_loss_pct, is_etf, updated_at FROM holdings",
                conn
            )
            conn.close()
            logger.info(f"[SimBroker] 从 qmt.holdings 读取 {len(df)} 条持仓")
            return df
        except Exception as e:
            logger.warning(f"[SimBroker] 读取 qmt.holdings 失败: {e}")
            return None

    def sync_from_real_positions(self) -> int:
        """
        从 qmt.holdings（真实持仓）同步到 agent_sim_positions。
        - 持股数量从 robottrade.positions 表补充（qmt.holdings 无 shares 字段）
        - ETF 持仓也纳入（is_etf=1）
        - 现金 = real_capital 配置 - 持仓市值
        Returns:
            同步的持仓数量
        """
        # 1. 读 qmt.holdings（真实成本/现价/盈亏）
        qmt_df = self._read_qmt_holdings()
        if qmt_df is None or qmt_df.empty:
            logger.warning("[SimBroker] qmt.holdings 为空，回退到 positions 表")
            qmt_df = None

        # 2. 读 robottrade.positions（含 shares 字段，以及市值反推持股数 + 买入日期）
        try:
            pos_df = DBUtils.query_df(
                "SELECT ts_code, shares, avg_cost, current_price, market_value, buy_date FROM positions"
            )
        except Exception:
            try:
                pos_df = DBUtils.query_df(
                    "SELECT ts_code, shares, avg_cost, current_price, market_value FROM positions"
                )
            except Exception:
                pos_df = None

        # 构建 shares 映射 + buy_date 映射（保留真实买入日期，避免每次重置为 today）
        shares_map = {}
        buy_date_map = {}
        if pos_df is not None and not pos_df.empty:
            for _, r in pos_df.iterrows():
                code = str(r['ts_code'])
                sh = int(float(r['shares'])) if r.get('shares') and float(r['shares']) > 0 else 0
                if sh == 0:
                    # 从市值反推（取整到100手）
                    mv = float(r['market_value']) if r.get('market_value') else 0.0
                    price = float(r['current_price']) if r.get('current_price') else float(r.get('avg_cost') or 1)
                    if mv > 0 and price > 0:
                        sh = max(100, int(mv / price / 100) * 100)
                shares_map[code] = sh
                # 保存真实买入日期（如果 positions 表有此字段）
                bd = r.get('buy_date')
                if bd and str(bd) not in ('', 'None', 'nan', 'NaT'):
                    buy_date_map[code] = str(bd)[:10]

        # 3. 确定最终持仓列表
        real_capital = float(Config.get('trading_agent.real_capital') or 1_600_000)
        if qmt_df is not None and not qmt_df.empty:
            # 以 qmt.holdings 为主，补 shares
            rows = []
            for _, r in qmt_df.iterrows():
                ts_code = str(r['ts_code'])
                name = str(r.get('name', ts_code))
                cost = float(r['avg_cost']) if r['avg_cost'] else 0.0
                cur = float(r['current_price']) if r['current_price'] else cost
                volume = shares_map.get(ts_code, 0)
                # ETF 成本/现价可能为 None，用 stock_daily 补
                if cost <= 0 or cur <= 0:
                    prc = self._get_fill_price(ts_code, 0)
                    cost = cost or prc
                    cur = cur or prc
                if cost > 0:
                    # volume 仍为 0：用持仓估算金额 / 成本价 反推（默认按 5 万/只估算，取整到 100）
                    if volume == 0:
                        estimated_value = real_capital / max(len(qmt_df), 1)
                        volume = max(100, int(estimated_value / cost / 100) * 100)
                    rows.append({'ts_code': ts_code, 'name': name,
                                 'cost': cost, 'current_price': cur, 'volume': volume,
                                 'buy_date': buy_date_map.get(ts_code, '')})
            source = "qmt.holdings"
        else:
            # 回退到 positions 表
            if pos_df is None or pos_df.empty:
                logger.warning("[SimBroker] 无持仓数据，跳过同步")
                return 0
            rows = []
            for _, r in pos_df.iterrows():
                ts_code = str(r['ts_code'])
                if ts_code.endswith('.HK'):
                    continue
                cost = float(r['avg_cost'])
                cur = self._get_fill_price(ts_code, cost)
                volume = int(float(r['shares']))
                bd = r.get('buy_date')
                buy_date_val = str(bd)[:10] if bd and str(bd) not in ('', 'None', 'nan', 'NaT') else ''
                rows.append({'ts_code': ts_code, 'name': ts_code,
                             'cost': cost, 'current_price': cur, 'volume': volume,
                             'buy_date': buy_date_val})
            source = "positions"

        # 4. 计算总市值和现金
        total_market_value = sum(
            r['current_price'] * r['volume'] for r in rows if r['volume'] > 0
        )
        cash = max(0.0, real_capital - total_market_value)

        # 5. 获取 Agent 今日已卖出的股票，同步时跳过，防止止损后重复触发
        agent_sold_today: set = set()
        try:
            today_str = datetime.now().strftime('%Y-%m-%d')
            sold_df = DBUtils.query_df(
                "SELECT DISTINCT ts_code FROM agent_sim_orders WHERE side='sell' AND created_at >= ?",
                (today_str,)
            )
            if not sold_df.empty:
                agent_sold_today = set(str(x) for x in sold_df['ts_code'].tolist())
                if agent_sold_today:
                    logger.info(f"[SimBroker] Agent今日已卖出 {len(agent_sold_today)} 只，同步跳过: {agent_sold_today}")
        except Exception as e:
            logger.warning(f"[SimBroker] 查询今日卖出记录失败: {e}")

        # 清空并重建 agent_sim_positions（保留 Agent 已持有但来源不在 real 列表里的仓位）
        DBUtils.execute("DELETE FROM agent_sim_positions")
        today = datetime.now().strftime('%Y-%m-%d')
        count = 0
        for r in rows:
            if r['cost'] <= 0:
                continue
            if r['ts_code'] in agent_sold_today:
                logger.info(f"[SimBroker] 跳过同步 {r['ts_code']}（Agent今日已止损/卖出）")
                continue
            # 优先使用真实买入日期；无则回落到今天（至少不会误报超长持仓）
            effective_buy_date = r.get('buy_date') or today
            DBUtils.execute(
                "INSERT INTO agent_sim_positions (ts_code, name, volume, cost, buy_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (r['ts_code'], r['name'], r['volume'], r['cost'], effective_buy_date)
            )
            count += 1

        # 6. 更新账户
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        DBUtils.execute(
            "UPDATE agent_sim_account SET cash=?, initial_capital=?, updated_at=? WHERE id=1",
            (cash, real_capital, now)
        )

        logger.info(
            f"[SimBroker] 持仓同步完成 (来源:{source})  "
            f"数量={count}  总资本={real_capital:,.0f}  "
            f"市值={total_market_value:,.0f}  现金={cash:,.0f}"
        )
        return count
