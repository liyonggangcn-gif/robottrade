#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国信iQuant (xtquant) 实盘经纪商
通过 XtQuantTrader 连接国信iQuant客户端，实现真实下单/查持仓/查账户。

使用前提：
  1. 国信iQuant 客户端已登录并保持运行
  2. 已开通量化交易权限

连接方式：XtQuantTrader(path=userdata路径, session=会话ID)
"""
import sys
import os
import time
import random
from typing import List, Optional
from datetime import datetime

from loguru import logger

from src.broker.base_broker import BaseBroker, OrderResult, Position, AccountInfo
from src.utils.config_loader import Config

# ── xtquant SDK 路径注入 ────────────────────────────────────────────────────
_IQUANT_ROOT = Config.get('iquant.install_path') or r'D:\国信iQuant策略交易平台'
_XT_SITE_PKG = os.path.join(_IQUANT_ROOT, 'bin.x64', 'Lib', 'site-packages')
if _XT_SITE_PKG not in sys.path:
    sys.path.insert(0, _XT_SITE_PKG)

try:
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount
    from xtquant import xtconstant
    _XT_AVAILABLE = True
except ImportError as e:
    logger.warning(f"[XtBroker] xtquant 导入失败: {e}，实盘下单不可用")
    _XT_AVAILABLE = False


# ── 默认参数 ────────────────────────────────────────────────────────────────
_USERDATA_PATH = os.path.join(_IQUANT_ROOT, 'userdata_mini')
_ACCOUNT_ID    = Config.get('iquant.account_id') or '18501905396'
_STRATEGY_NAME = 'QuantAgent-Alpha'


class _Callback(XtQuantTraderCallback if _XT_AVAILABLE else object):
    """最小化回调，仅记录日志"""

    def on_disconnected(self):
        logger.warning("[XtBroker] 与iQuant客户端连接断开")

    def on_stock_order(self, order):
        logger.info(f"[XtBroker] 委托回报: {order.stock_code} "
                    f"{'买入' if order.order_type == 23 else '卖出'} "
                    f"{order.order_volume}股 状态={order.status_msg}")

    def on_stock_trade(self, trade):
        logger.info(f"[XtBroker] 成交: {trade.stock_code} "
                    f"{trade.traded_volume}股@{trade.traded_price:.2f}")

    def on_order_error(self, order_error):
        logger.error(f"[XtBroker] 委托失败: {order_error.order_id} "
                     f"错误={order_error.error_msg}")

    def on_cancel_error(self, cancel_error):
        logger.error(f"[XtBroker] 撤单失败: {cancel_error.order_id} "
                     f"错误={cancel_error.error_msg}")


class XtBroker(BaseBroker):
    """
    国信iQuant 实盘经纪商

    config/settings.yaml 中配置：
        iquant:
          install_path: 'D:\\国信iQuant策略交易平台'
          account_id: '18501905396'
    """

    def __init__(self):
        if not _XT_AVAILABLE:
            raise RuntimeError("xtquant SDK 不可用，请检查 iQuant 安装路径")

        self._account_id = _ACCOUNT_ID
        self._account    = StockAccount(_ACCOUNT_ID)
        self._session    = random.randint(100000, 999999)
        self._trader     = XtQuantTrader(_USERDATA_PATH, self._session)
        self._callback   = _Callback()
        self._trader.register_callback(self._callback)
        self._connected  = False

        logger.info(f"[XtBroker] 初始化 account={_ACCOUNT_ID} "
                    f"userdata={_USERDATA_PATH}")

    # ------------------------------------------------------------------ #
    #  连接管理
    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        """连接 iQuant 客户端（需客户端已运行）"""
        try:
            # 必须先 start() 启动交易线程，再 connect()
            self._trader.start()
            connect_result = self._trader.connect()
            if connect_result != 0:
                logger.error(f"[XtBroker] 连接失败，错误码={connect_result}")
                return False
            subscribe_result = self._trader.subscribe(self._account)
            if subscribe_result != 0:
                logger.error(f"[XtBroker] 账户订阅失败，错误码={subscribe_result}")
                return False
            self._connected = True
            logger.info("[XtBroker] 连接成功，已订阅账户推送")
            return True
        except Exception as e:
            logger.error(f"[XtBroker] 连接异常: {e}")
            return False

    def disconnect(self):
        try:
            self._trader.unsubscribe(self._account)
            self._trader.stop()
        except Exception:
            pass
        self._connected = False

    def _ensure_connected(self):
        if not self._connected:
            ok = self.connect()
            if not ok:
                raise ConnectionError("iQuant 客户端未连接，请先启动iQuant并登录")

    # ------------------------------------------------------------------ #
    #  下单接口
    # ------------------------------------------------------------------ #
    def buy(self, ts_code: str, price: float, amount_yuan: float) -> OrderResult:
        """
        买入
        price=0  → 对手价（最优五档即时成交）
        price>0  → 限价委托
        """
        self._ensure_connected()
        if price <= 0:
            # 先查最新价
            price = self._get_latest_price(ts_code)
            if price <= 0:
                return OrderResult(success=False, ts_code=ts_code, side='buy',
                                   msg='无法获取最新价，买入取消')

        volume = self.calc_volume(price, amount_yuan)
        if volume <= 0:
            return OrderResult(success=False, ts_code=ts_code, side='buy',
                               msg=f'金额{amount_yuan:.0f}元不足购买1手(价格={price:.2f})')

        price_type = xtconstant.FIX_PRICE
        try:
            order_id = self._trader.order_stock(
                self._account, ts_code,
                xtconstant.STOCK_BUY, volume,
                price_type, price,
                strategy_name=_STRATEGY_NAME,
                order_remark='QuantAgent买入'
            )
            success = isinstance(order_id, int) and order_id > 0
            msg = f'order_id={order_id}' if success else f'下单失败(返回={order_id})'
            logger.info(f"[XtBroker] 买入 {ts_code} {volume}股@{price:.2f} → {msg}")
            return OrderResult(
                success=success,
                order_id=str(order_id),
                ts_code=ts_code, side='buy',
                price=price, volume=volume,
                amount=price * volume,
                msg=msg
            )
        except Exception as e:
            logger.error(f"[XtBroker] 买入异常: {e}")
            return OrderResult(success=False, ts_code=ts_code, side='buy', msg=str(e))

    def sell(self, ts_code: str, price: float = 0) -> OrderResult:
        """卖出全部持仓"""
        pos = self.get_position(ts_code)
        if not pos or pos.volume <= 0:
            return OrderResult(success=False, ts_code=ts_code, side='sell',
                               msg='无持仓，无法卖出')
        return self.sell_volume(ts_code, pos.volume, price)

    def sell_volume(self, ts_code: str, volume: int, price: float = 0) -> OrderResult:
        """卖出指定数量"""
        self._ensure_connected()
        if price <= 0:
            price = self._get_latest_price(ts_code)
            if price <= 0:
                return OrderResult(success=False, ts_code=ts_code, side='sell',
                                   msg='无法获取最新价，卖出取消')

        price_type = xtconstant.FIX_PRICE
        try:
            order_id = self._trader.order_stock(
                self._account, ts_code,
                xtconstant.STOCK_SELL, volume,
                price_type, price,
                strategy_name=_STRATEGY_NAME,
                order_remark='QuantAgent卖出'
            )
            success = isinstance(order_id, int) and order_id > 0
            msg = f'order_id={order_id}' if success else f'下单失败(返回={order_id})'
            logger.info(f"[XtBroker] 卖出 {ts_code} {volume}股@{price:.2f} → {msg}")
            return OrderResult(
                success=success,
                order_id=str(order_id),
                ts_code=ts_code, side='sell',
                price=price, volume=volume,
                amount=price * volume,
                msg=msg
            )
        except Exception as e:
            logger.error(f"[XtBroker] 卖出异常: {e}")
            return OrderResult(success=False, ts_code=ts_code, side='sell', msg=str(e))

    def cancel_order(self, order_id: str) -> bool:
        """撤单"""
        self._ensure_connected()
        try:
            result = self._trader.cancel_order_stock(self._account, int(order_id))
            logger.info(f"[XtBroker] 撤单 order_id={order_id} 结果={result}")
            return result == 0
        except Exception as e:
            logger.error(f"[XtBroker] 撤单异常: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  查询接口
    # ------------------------------------------------------------------ #
    def get_positions(self) -> List[Position]:
        """从 iQuant 实时查询持仓"""
        self._ensure_connected()
        try:
            xt_positions = self._trader.query_stock_positions(self._account)
            result = []
            for p in (xt_positions or []):
                if p.volume <= 0:
                    continue
                cur_price = p.market_value / p.volume if p.volume > 0 else p.open_price
                profit_pct = (cur_price - p.open_price) / p.open_price * 100 if p.open_price > 0 else 0.0
                result.append(Position(
                    ts_code=p.stock_code,
                    name=p.stock_code,   # xtquant 不返回股票名称
                    volume=p.volume,
                    cost=p.open_price,
                    current_price=cur_price,
                    market_value=p.market_value,
                    profit_pct=profit_pct,
                ))
            return result
        except Exception as e:
            logger.error(f"[XtBroker] 查询持仓失败: {e}")
            return []

    def get_account(self) -> AccountInfo:
        """从 iQuant 实时查询账户资金"""
        self._ensure_connected()
        try:
            asset = self._trader.query_stock_asset(self._account)
            if asset is None:
                return AccountInfo()
            positions = self.get_positions()
            mv = sum(p.market_value for p in positions)
            initial = float(Config.get('trading_agent.real_capital') or 1_600_000)
            profit_pct = (asset.total_asset - initial) / initial * 100 if initial > 0 else 0.0
            return AccountInfo(
                total_assets=asset.total_asset,
                cash=asset.cash,
                market_value=asset.market_value or mv,
                profit_pct=profit_pct,
            )
        except Exception as e:
            logger.error(f"[XtBroker] 查询账户失败: {e}")
            return AccountInfo()

    def query_orders(self) -> list:
        """查询当日委托"""
        self._ensure_connected()
        try:
            return self._trader.query_stock_orders(self._account) or []
        except Exception as e:
            logger.error(f"[XtBroker] 查询委托失败: {e}")
            return []

    # ------------------------------------------------------------------ #
    #  内部工具
    # ------------------------------------------------------------------ #
    def _get_latest_price(self, ts_code: str) -> float:
        """从数据库取最新收盘价作为委托价"""
        try:
            from src.utils.db_utils import DBUtils
            df = DBUtils.query_df(
                "SELECT close FROM stock_daily WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1",
                (ts_code,)
            )
            if not df.empty:
                return float(df.iloc[0]['close'])
        except Exception:
            pass
        return 0.0
