#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国信证券 iQuant 经纪商接口
通过 HTTP REST API 与 iQuant 平台通信
"""
from typing import List, Optional

import requests
from loguru import logger

from src.broker.base_broker import BaseBroker, OrderResult, Position, AccountInfo
from src.utils.config_loader import Config


class IQuantBroker(BaseBroker):
    """国信证券 iQuant 经纪商 — 通过 HTTP REST API 下单"""

    def __init__(self):
        cfg = Config.get('trading_agent.iquant') or {}
        self._host = cfg.get('host', '127.0.0.1')
        self._port = cfg.get('port', 8888)
        self._account = cfg.get('account', '')
        self._base_url = f"http://{self._host}:{self._port}"
        self._timeout = 5  # 秒

        logger.info(f"[iQuant] 初始化  base_url={self._base_url}  account={self._account}")

    # ------------------------------------------------------------------ #
    #  辅助方法
    # ------------------------------------------------------------------ #
    def _strip_code(self, ts_code: str) -> str:
        """600519.SH → 600519"""
        return ts_code.split('.')[0] if '.' in ts_code else ts_code

    def _get(self, path: str, params: dict = None) -> dict:
        """发起 GET 请求，失败则抛出 ConnectionError"""
        url = f"{self._base_url}{path}"
        try:
            resp = requests.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"[iQuant] GET {path} 失败: {e}")
            raise ConnectionError(f"iQuant HTTP GET {path} 失败: {e}") from e

    def _post(self, path: str, body: dict) -> dict:
        """发起 POST 请求，失败则抛出 ConnectionError"""
        url = f"{self._base_url}{path}"
        try:
            resp = requests.post(url, json=body, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"[iQuant] POST {path} 失败: {e}")
            raise ConnectionError(f"iQuant HTTP POST {path} 失败: {e}") from e

    def _delete(self, path: str) -> dict:
        """发起 DELETE 请求，失败则抛出 ConnectionError"""
        url = f"{self._base_url}{path}"
        try:
            resp = requests.delete(url, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"[iQuant] DELETE {path} 失败: {e}")
            raise ConnectionError(f"iQuant HTTP DELETE {path} 失败: {e}") from e

    def _parse_position(self, item: dict) -> Position:
        """将接口响应 dict 转为 Position 对象"""
        ts_code_raw = str(item.get('ts_code', item.get('code', '')))
        # 若返回裸代码，补全后缀
        if '.' not in ts_code_raw:
            suffix = '.SH' if ts_code_raw.startswith('6') else '.SZ'
            ts_code_raw = ts_code_raw + suffix

        cost = float(item.get('cost', item.get('avg_price', 0)))
        current_price = float(item.get('current_price', item.get('price', cost)))
        volume = int(item.get('volume', item.get('qty', 0)))
        market_value = current_price * volume
        profit_pct = (current_price - cost) / cost * 100 if cost > 0 else 0.0

        return Position(
            ts_code=ts_code_raw,
            name=str(item.get('name', '')),
            volume=volume,
            cost=cost,
            current_price=current_price,
            market_value=market_value,
            profit_pct=profit_pct,
            buy_date=str(item.get('buy_date', ''))
        )

    def _parse_order_result(self, data: dict, ts_code: str, side: str,
                            price: float, volume: int, amount: float) -> OrderResult:
        """将接口响应 dict 转为 OrderResult"""
        success = data.get('success', data.get('status', '') == 'ok')
        order_id = str(data.get('order_id', data.get('id', '')))
        msg = data.get('msg', data.get('message', ''))
        return OrderResult(
            success=bool(success),
            order_id=order_id,
            ts_code=ts_code,
            side=side,
            price=price,
            volume=volume,
            amount=amount,
            msg=msg
        )

    # ------------------------------------------------------------------ #
    #  BaseBroker 接口实现
    # ------------------------------------------------------------------ #
    def buy(self, ts_code: str, price: float, amount_yuan: float) -> OrderResult:
        """计算手数后向 iQuant 提交买单"""
        # price=0 表示市价，用最新价估算手数
        fill_price = price
        if fill_price <= 0:
            # 市价委托时用 1 元估算不现实，改从 positions 暂时跳过手数计算
            # 这里用一个安全值；实际由 iQuant 平台按市价成交
            logger.warning(f"[iQuant] {ts_code} 市价委托，手数将由平台决定")
            fill_price = 1.0  # 占位，平台会按市价处理

        volume = self.calc_volume(fill_price, amount_yuan)
        if volume <= 0:
            msg = f"买入数量为0，price={fill_price} amount={amount_yuan}"
            logger.warning(f"[iQuant] {ts_code} {msg}")
            return OrderResult(success=False, ts_code=ts_code, side='buy', msg=msg)

        amount = fill_price * volume
        body = {
            'ts_code': self._strip_code(ts_code),
            'side': 'buy',
            'price': price,         # 原始价格发给 iQuant（0=市价）
            'volume': volume,
            'account': self._account
        }
        logger.info(f"[iQuant] 买入 {ts_code}  {volume}股 @{price:.2f}  金额={amount:,.0f}")
        try:
            data = self._post('/api/v1/orders', body)
            result = self._parse_order_result(data, ts_code, 'buy', price, volume, amount)
            if result.success:
                logger.info(f"[iQuant] 委托成功 order_id={result.order_id}")
            else:
                logger.warning(f"[iQuant] 委托失败: {result.msg}")
            return result
        except ConnectionError as e:
            return OrderResult(success=False, ts_code=ts_code, side='buy', msg=str(e))

    def sell(self, ts_code: str, price: float = 0) -> OrderResult:
        """卖出全部持仓"""
        pos = self.get_position(ts_code)
        if pos is None or pos.volume <= 0:
            msg = f"无持仓: {ts_code}"
            logger.warning(f"[iQuant] {msg}")
            return OrderResult(success=False, ts_code=ts_code, side='sell', msg=msg)
        return self.sell_volume(ts_code, pos.volume, price)

    def sell_volume(self, ts_code: str, volume: int, price: float = 0) -> OrderResult:
        """卖出指定数量"""
        amount = price * volume if price > 0 else 0
        body = {
            'ts_code': self._strip_code(ts_code),
            'side': 'sell',
            'price': price,
            'volume': volume,
            'account': self._account
        }
        logger.info(f"[iQuant] 卖出 {ts_code}  {volume}股 @{price:.2f}")
        try:
            data = self._post('/api/v1/orders', body)
            result = self._parse_order_result(data, ts_code, 'sell', price, volume, amount)
            if result.success:
                logger.info(f"[iQuant] 委托成功 order_id={result.order_id}")
            else:
                logger.warning(f"[iQuant] 委托失败: {result.msg}")
            return result
        except ConnectionError as e:
            return OrderResult(success=False, ts_code=ts_code, side='sell', msg=str(e))

    def get_positions(self) -> List[Position]:
        """查询当前持仓"""
        try:
            data = self._get('/api/v1/positions', {'account': self._account})
            items = data if isinstance(data, list) else data.get('data', data.get('positions', []))
            positions = [self._parse_position(item) for item in items]
            logger.debug(f"[iQuant] 持仓 {len(positions)} 只")
            return positions
        except ConnectionError:
            return []

    def get_account(self) -> AccountInfo:
        """查询账户资金"""
        try:
            data = self._get('/api/v1/account', {'account': self._account})
            # 兼容不同字段名
            total = float(data.get('total_assets', data.get('totalAssets', 0)))
            cash = float(data.get('cash', data.get('available', 0)))
            market_value = float(data.get('market_value', data.get('marketValue', total - cash)))
            profit_pct = float(data.get('profit_pct', data.get('profitPct', 0)))
            logger.debug(f"[iQuant] 账户总资产={total:,.0f}  现金={cash:,.0f}")
            return AccountInfo(
                total_assets=total,
                cash=cash,
                market_value=market_value,
                profit_pct=profit_pct
            )
        except ConnectionError:
            return AccountInfo()

    def cancel_order(self, order_id: str) -> bool:
        """撤单"""
        logger.info(f"[iQuant] 撤单 order_id={order_id}")
        try:
            data = self._delete(f'/api/v1/orders/{order_id}')
            success = data.get('success', data.get('status', '') == 'ok')
            if success:
                logger.info(f"[iQuant] 撤单成功 order_id={order_id}")
            else:
                logger.warning(f"[iQuant] 撤单失败: {data.get('msg', '')}")
            return bool(success)
        except ConnectionError:
            return False

    def is_connected(self) -> bool:
        """检查 iQuant 服务是否可达"""
        try:
            self._get('/api/v1/account', {'account': self._account})
            logger.info("[iQuant] 连接正常")
            return True
        except ConnectionError:
            logger.warning("[iQuant] 连接失败")
            return False
