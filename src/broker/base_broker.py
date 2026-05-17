#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
经纪商抽象接口
所有broker实现必须继承此类
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    ts_code: str = ""
    side: str = ""          # buy / sell
    price: float = 0.0
    volume: int = 0         # 股数（手×100）
    amount: float = 0.0     # 成交金额
    msg: str = ""


@dataclass
class Position:
    ts_code: str
    name: str = ""
    volume: int = 0         # 持仓股数
    cost: float = 0.0       # 持仓均价
    current_price: float = 0.0
    market_value: float = 0.0
    profit_pct: float = 0.0
    buy_date: str = ""


@dataclass
class AccountInfo:
    total_assets: float = 0.0
    cash: float = 0.0
    market_value: float = 0.0
    profit_pct: float = 0.0


class BaseBroker(ABC):
    """经纪商抽象接口"""

    @abstractmethod
    def buy(self, ts_code: str, price: float, amount_yuan: float) -> OrderResult:
        """买入
        Args:
            ts_code: 股票代码（如 600519.SH）
            price: 委托价（0 或负数表示市价）
            amount_yuan: 委托金额（元），broker自动换算手数
        """

    @abstractmethod
    def sell(self, ts_code: str, price: float = 0) -> OrderResult:
        """卖出全部持仓
        Args:
            price: 委托价（0 表示市价）
        """

    @abstractmethod
    def sell_volume(self, ts_code: str, volume: int, price: float = 0) -> OrderResult:
        """卖出指定数量"""

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """获取当前持仓列表"""

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """获取账户资金信息"""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """撤单"""

    def get_position(self, ts_code: str) -> Optional[Position]:
        """获取单只股票持仓"""
        for p in self.get_positions():
            if p.ts_code == ts_code:
                return p
        return None

    @staticmethod
    def calc_volume(price: float, amount_yuan: float) -> int:
        """根据金额和价格计算可买手数（1手=100股），向下取整"""
        if price <= 0:
            return 0
        hands = int(amount_yuan / (price * 100))
        return hands * 100  # 返回股数
