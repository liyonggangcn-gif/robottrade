#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PositionManager: 仓位管理模块

主要功能：
1. 仓位分配：根据股票评分自动分配仓位权重
2. 风险控制：单只股票最大仓位限制、总仓位控制
3. 止损止盈：自动计算止损价、止盈价
4. 持仓跟踪：记录买入/卖出交易，计算持仓盈亏
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class PositionManager:
    """仓位管理器"""
    
    # 默认风控参数
    DEFAULT_TOTAL_CAPITAL = 1000000  # 默认总资金 100万
    DEFAULT_MAX_POSITION_PCT = 0.15   # 单只股票最大仓位 15%
    DEFAULT_MAX_TOTAL_POSITION = 0.80 # 最大总仓位 80%
    DEFAULT_STOP_LOSS_PCT = 0.08      # 默认止损比例 -8%
    DEFAULT_TAKE_PROFIT_PCT = 0.20    # 默认止盈比例 +20%
    
    def __init__(self, total_capital: float = None):
        """
        初始化仓位管理器
        
        Args:
            total_capital: 总资金量，None则从配置文件读取
        """
        # 从配置文件读取或使用默认值
        config_capital = Config.get('portfolio.total_capital')
        self.total_capital = total_capital or config_capital or self.DEFAULT_TOTAL_CAPITAL
        
        self.max_position_pct = Config.get('portfolio.max_position_pct') or self.DEFAULT_MAX_POSITION_PCT
        self.max_total_position = Config.get('portfolio.max_total_position') or self.DEFAULT_MAX_TOTAL_POSITION
        self.stop_loss_pct = Config.get('portfolio.stop_loss_pct') or self.DEFAULT_STOP_LOSS_PCT
        self.take_profit_pct = Config.get('portfolio.take_profit_pct') or self.DEFAULT_TAKE_PROFIT_PCT
        
        print(f"[PositionManager] 初始化完成")
        print(f"  总资金: {self.total_capital:,.0f} 元")
        print(f"  单只最大仓位: {self.max_position_pct*100:.1f}%")
        print(f"  最大总仓位: {self.max_total_position*100:.1f}%")
        print(f"  止损比例: {self.stop_loss_pct*100:.1f}%")
        print(f"  止盈比例: {self.take_profit_pct*100:.1f}%")
        
        # 初始化数据库表
        self._init_tables()
    
    def _init_tables(self):
        """创建持仓和交易历史表"""
        with DBUtils.get_conn() as conn:
            cursor = conn.cursor()

            # 持仓表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                ts_code VARCHAR(20) PRIMARY KEY,
                name VARCHAR(100),
                shares REAL,
                avg_cost REAL,
                current_price REAL,
                market_value REAL,
                profit_loss REAL,
                profit_loss_pct REAL,
                position_pct REAL,
                stop_loss_price REAL,
                take_profit_price REAL,
                buy_date VARCHAR(20),
                update_date VARCHAR(20),
                company_type VARCHAR(30),
                buy_phase TINYINT DEFAULT 1
            )
            """)
            
            # 交易历史表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code VARCHAR(20),
                name VARCHAR(100),
                action VARCHAR(10),
                price REAL,
                shares REAL,
                amount REAL,
                commission REAL,
                trade_date VARCHAR(20),
                strategy VARCHAR(50),
                notes TEXT
            )
            """)
        
        print("[PositionManager] 数据库表初始化完成")
        self._migrate_positions_table()
    
    # ========================================================================
    # 仓位分配算法
    # ========================================================================
    
    def allocate_positions(self, stocks_df: pd.DataFrame, 
                          score_col: str = 'final_score',
                          method: str = 'proportional') -> pd.DataFrame:
        """
        根据评分分配仓位
        
        Args:
            stocks_df: 股票列表 (必须包含 ts_code, name, close, {score_col})
            score_col: 用于分配的评分列名
            method: 分配方法
                - 'proportional': 按评分比例分配
                - 'equal': 等权分配
                - 'tiered': 分层分配（高分多配，低分少配）
        
        Returns:
            DataFrame: 添加了 position_pct, shares, amount, stop_loss_price, take_profit_price 列
        """
        if stocks_df.empty:
            print("[PositionManager] 空股票列表，无法分配仓位")
            return stocks_df
        
        df = stocks_df.copy()
        
        # 确保必要的列存在
        required_cols = ['ts_code', 'name', 'close', score_col]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise ValueError(f"缺少必要列: {missing}")
        
        print(f"\n[仓位分配] 方法: {method}, 股票数: {len(df)}")
        
        # 计算可用资金（总资金 × 最大总仓位）
        available_capital = self.total_capital * self.max_total_position
        
        if method == 'equal':
            # 等权分配
            position_pct = min(self.max_position_pct, 1.0 / len(df))
            df['position_pct'] = position_pct
            
        elif method == 'tiered':
            # 分层分配：Top 30% -> 高仓位，Middle 40% -> 中仓位，Bottom 30% -> 低仓位
            n = len(df)
            top_n = max(1, int(n * 0.3))
            mid_n = max(1, int(n * 0.4))
            
            df['rank'] = df[score_col].rank(ascending=False, method='first')
            
            def assign_tier(rank):
                if rank <= top_n:
                    return min(self.max_position_pct, 0.12)  # 高层 12%
                elif rank <= top_n + mid_n:
                    return min(self.max_position_pct, 0.08)  # 中层 8%
                else:
                    return min(self.max_position_pct, 0.05)  # 低层 5%
            
            df['position_pct'] = df['rank'].apply(assign_tier)
            df.drop(columns=['rank'], inplace=True)
            
        else:  # proportional (默认)
            # 按评分比例分配
            total_score = df[score_col].sum()
            if total_score > 0:
                df['position_pct'] = (df[score_col] / total_score) * self.max_total_position
                # 限制单只股票最大仓位
                df['position_pct'] = df['position_pct'].clip(upper=self.max_position_pct)
                # 重新归一化，确保总仓位不超标
                if df['position_pct'].sum() > self.max_total_position:
                    df['position_pct'] = df['position_pct'] / df['position_pct'].sum() * self.max_total_position
            else:
                df['position_pct'] = 0
        
        # 计算金额和股数
        df['amount'] = df['position_pct'] * self.total_capital
        df['shares'] = (df['amount'] / df['close']).apply(lambda x: int(x / 100) * 100)  # 取整到100股
        df['amount'] = df['shares'] * df['close']  # 重新计算实际金额
        df['position_pct'] = df['amount'] / self.total_capital  # 重新计算实际仓位比例
        
        # 最终检查：确保总仓位不超标
        total_pct = df['position_pct'].sum()
        if total_pct > self.max_total_position:
            # 按比例缩减所有仓位
            scale_factor = self.max_total_position / total_pct
            df['position_pct'] = df['position_pct'] * scale_factor
            df['amount'] = df['position_pct'] * self.total_capital
            df['shares'] = (df['amount'] / df['close']).apply(lambda x: int(x / 100) * 100)
            df['amount'] = df['shares'] * df['close']
            df['position_pct'] = df['amount'] / self.total_capital
        
        # 计算止损止盈价格
        df['stop_loss_price'] = df['close'] * (1 - self.stop_loss_pct)
        df['take_profit_price'] = df['close'] * (1 + self.take_profit_pct)
        
        # 四舍五入
        df['stop_loss_price'] = df['stop_loss_price'].round(2)
        df['take_profit_price'] = df['take_profit_price'].round(2)
        
        # 统计信息
        total_allocated = df['amount'].sum()
        total_pct = df['position_pct'].sum()
        
        print(f"[仓位分配] 完成")
        print(f"  总分配金额: {total_allocated:,.0f} 元 ({total_pct*100:.1f}%)")
        print(f"  剩余现金: {self.total_capital - total_allocated:,.0f} 元 ({(1-total_pct)*100:.1f}%)")
        print(f"  平均单只仓位: {total_pct/len(df)*100:.1f}%")
        
        return df
    
    # ========================================================================
    # 持仓管理
    # ========================================================================
    
    def get_current_positions(self) -> pd.DataFrame:
        """获取当前持仓（优先 agent_sim_positions，回退到 positions 表）

        agent_sim_positions 由 TradingAgent/SimBroker 维护，是权威持仓来源。
        positions 表由 QMT 外部同步写入，作为回退来源。
        """
        # 1. 优先读 agent_sim_positions（TradingAgent 权威来源）
        try:
            agent_df = DBUtils.query_df(
                "SELECT ts_code, name, volume AS shares, cost AS avg_cost, buy_date "
                "FROM agent_sim_positions WHERE volume > 0"
            )
            if not agent_df.empty:
                # 补充当前价格（从 stock_daily 最新一日）
                try:
                    price_df = DBUtils.query_df(
                        "SELECT ts_code, close AS current_price FROM stock_daily "
                        "WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)"
                    )
                    if not price_df.empty:
                        agent_df = agent_df.merge(price_df, on='ts_code', how='left')
                except Exception:
                    pass
                if 'current_price' not in agent_df.columns:
                    agent_df['current_price'] = agent_df['avg_cost']
                agent_df['current_price'] = agent_df['current_price'].fillna(agent_df['avg_cost'])
                agent_df['market_value'] = agent_df['shares'] * agent_df['current_price']
                agent_df['profit_loss'] = (
                    (agent_df['current_price'] - agent_df['avg_cost']) * agent_df['shares']
                )
                agent_df['profit_loss_pct'] = (
                    (agent_df['current_price'] - agent_df['avg_cost']) /
                    agent_df['avg_cost'].replace(0, float('nan'))
                ).fillna(0)
                agent_df['position_pct'] = (
                    agent_df['market_value'] / self.total_capital if self.total_capital > 0 else 0
                )
                agent_df['stop_loss_price'] = agent_df['avg_cost'] * (1 - self.stop_loss_pct)
                agent_df['take_profit_price'] = agent_df['avg_cost'] * (1 + self.take_profit_pct)
                return agent_df.sort_values('position_pct', ascending=False)
        except Exception:
            pass

        # 2. 回退到 positions 表（QMT 外部同步的真实持仓）
        df = DBUtils.query_df("SELECT * FROM positions ORDER BY position_pct DESC")
        return df
    
    def update_position_prices(self, trade_date: str = None):
        """
        更新所有持仓的当前价格和盈亏
        
        Args:
            trade_date: 交易日期，None则使用最新日期
        """
        positions = self.get_current_positions()
        if positions.empty:
            print("[PositionManager] 无持仓，跳过更新")
            return
        
        # 获取最新交易日
        if trade_date is None:
            result = DBUtils.query_df("SELECT MAX(trade_date) as max_date FROM stock_daily")
            if result.empty:
                print("[PositionManager] 无法获取最新交易日")
                return
            trade_date = result.iloc[0]['max_date']
        
        print(f"[PositionManager] 更新持仓价格 (日期: {trade_date})")
        
        with DBUtils.get_conn() as conn:
            cursor = conn.cursor()
            
            for _, pos in positions.iterrows():
                ts_code = pos['ts_code']
                
                # 查询最新价格
                price_sql = f"""
                SELECT close FROM stock_daily 
                WHERE ts_code = ? AND trade_date = ?
                """
                price_df = DBUtils.query_df(price_sql, params=[ts_code, trade_date])
                
                if not price_df.empty:
                    current_price = price_df.iloc[0]['close']
                    shares = pos['shares']
                    avg_cost = pos['avg_cost']
                    
                    # 计算盈亏
                    market_value = shares * current_price
                    cost_value = shares * avg_cost
                    profit_loss = market_value - cost_value
                    profit_loss_pct = (current_price / avg_cost - 1) if avg_cost > 0 else 0
                    position_pct = market_value / self.total_capital
                    
                    # 更新数据库
                    cursor.execute("""
                    UPDATE positions 
                    SET current_price = ?, 
                        market_value = ?, 
                        profit_loss = ?, 
                        profit_loss_pct = ?,
                        position_pct = ?,
                        update_date = ?
                    WHERE ts_code = ?
                    """, (current_price, market_value, profit_loss, profit_loss_pct, 
                          position_pct, trade_date, ts_code))
        
        print(f"[PositionManager] 持仓价格更新完成")
    
    def get_position_summary(self) -> Dict:
        """
        获取持仓汇总信息
        
        Returns:
            字典包含: total_value, total_profit_loss, total_pct, cash, stock_count
        """
        positions = self.get_current_positions()
        
        if positions.empty:
            return {
                'total_value': 0,
                'total_profit_loss': 0,
                'total_profit_loss_pct': 0,
                'total_position_pct': 0,
                'cash': self.total_capital,
                'stock_count': 0,
                'positions': []
            }
        
        total_value = positions['market_value'].sum()
        total_cost = (positions['shares'] * positions['avg_cost']).sum()
        total_profit_loss = positions['profit_loss'].sum()
        total_profit_loss_pct = (total_value / total_cost - 1) if total_cost > 0 else 0
        total_position_pct = total_value / self.total_capital
        cash = self.total_capital - total_cost
        
        return {
            'total_value': total_value,
            'total_cost': total_cost,
            'total_profit_loss': total_profit_loss,
            'total_profit_loss_pct': total_profit_loss_pct,
            'total_position_pct': total_position_pct,
            'cash': cash,
            'stock_count': len(positions),
            'positions': positions.to_dict('records')
        }
    
    def check_stop_loss_take_profit(self) -> Tuple[List[Dict], List[Dict]]:
        """【仅用于盘后展示，不执行任何交易】

        盘中止损/止盈执行由 RiskController (src/agent/risk_controller.py) 负责。
        本方法仅在收盘推送中展示哪些持仓跌破止损线/达到止盈线，供人工参考。

        止损阈值与 RiskController 保持一致（trading_agent.risk.stop_loss）。

        Returns:
            (stop_loss_list, take_profit_list)
        """
        positions = self.get_current_positions()
        if positions.empty:
            return [], []

        # 读取与 RiskController 相同的止损/止盈阈值，保持展示与执行一致
        risk_cfg = Config.get('trading_agent.risk') or {}
        rc_stop_loss = abs(float(risk_cfg.get('stop_loss', self.stop_loss_pct)))
        rc_take_profit = abs(float(risk_cfg.get('trailing_stop', self.take_profit_pct)))

        stop_loss_list = []
        take_profit_list = []

        for _, pos in positions.iterrows():
            try:
                current_price = float(pos.get('current_price') or 0)
                avg_cost = float(pos.get('avg_cost') or 0)
                if current_price <= 0 or avg_cost <= 0:
                    continue
                profit_loss_pct = float(pos.get('profit_loss_pct') or 0)
                stop_loss_price = avg_cost * (1 - rc_stop_loss)
                take_profit_price = avg_cost * (1 + rc_take_profit)

                if current_price <= stop_loss_price:
                    stop_loss_list.append({
                        'ts_code': pos['ts_code'],
                        'name': pos.get('name', pos['ts_code']),
                        'current_price': current_price,
                        'stop_loss_price': round(stop_loss_price, 2),
                        'profit_loss_pct': profit_loss_pct,
                    })
                elif current_price >= take_profit_price:
                    take_profit_list.append({
                        'ts_code': pos['ts_code'],
                        'name': pos.get('name', pos['ts_code']),
                        'current_price': current_price,
                        'take_profit_price': round(take_profit_price, 2),
                        'profit_loss_pct': profit_loss_pct,
                    })
            except Exception:
                continue

        return stop_loss_list, take_profit_list
    
    def get_all_positions(self) -> List[Dict]:
        """获取所有持仓（返回字典列表）"""
        df = self.get_current_positions()
        return df.to_dict('records') if not df.empty else []

    def get_phase1_candidates(self) -> List[Dict]:
        """
        返回当前处于第1批（半仓）且可以考虑加仓的持仓。
        条件：buy_phase=1 且持仓未亏损（确认走势正常）
        """
        positions = self.get_current_positions()
        if positions.empty:
            return []
        candidates = []
        if 'buy_phase' not in positions.columns:
            return []
        for _, pos in positions.iterrows():
            if int(pos.get('buy_phase', 1)) == 1:
                pl_pct = pos.get('profit_loss_pct', 0)
                if pl_pct is not None and float(pl_pct) >= 0:  # 持仓盈利，说明方向正确
                    candidates.append(pos.to_dict())
        return candidates

    # ========================================================================
    # 三类卖出信号检查
    # ========================================================================

    def check_sell_signals(self) -> List[Dict]:
        """【仅用于盘后展示，不执行任何交易】

        盘中止损执行由 RiskController (src/agent/risk_controller.py) 负责。
        本方法检查3类卖出信号供收盘推送显示：
          1. stop_loss         — 价格跌破止损线（与 RiskController 同阈值）
          2. valuation_expensive — 估值历史高位（按公司类型判断）
          3. profit_driver_broken — 盈利逻辑破坏（净利润/ROE大幅恶化）

        Returns:
            list of dict: {ts_code, name, signal_type, reason, urgency,
                           current_price, profit_loss_pct}
        """
        positions = self.get_current_positions()
        if positions.empty:
            return []

        # 懒加载估值模块
        try:
            from src.valuation.valuation_engine import _CHEAP_THRESHOLDS
        except Exception:
            _CHEAP_THRESHOLDS = {}

        # ── 批量预加载估值数据（1条SQL，避免N+1）─────────────────────────────
        all_codes = positions['ts_code'].tolist()
        codes_str = "','".join(all_codes)
        val_map: dict = {}
        fin_map: dict = {}
        try:
            vdf = DBUtils.query_df(
                f"""SELECT v.* FROM valuation_history v
                    INNER JOIN (
                        SELECT ts_code, MAX(trade_date) as md
                        FROM valuation_history WHERE ts_code IN ('{codes_str}')
                        GROUP BY ts_code
                    ) m ON v.ts_code = m.ts_code AND v.trade_date = m.md"""
            )
            if not vdf.empty:
                val_map = {r['ts_code']: r.to_dict() for _, r in vdf.iterrows()}
        except Exception:
            pass
        try:
            from datetime import date, timedelta
            threshold = (date.today() - timedelta(days=30)).isoformat()
            fdf = DBUtils.query_df(
                f"""SELECT f.* FROM financial_data f
                    INNER JOIN (
                        SELECT ts_code, MAX(end_date) as md
                        FROM financial_data
                        WHERE ts_code IN ('{codes_str}') AND fetched_date >= '{threshold}'
                        GROUP BY ts_code
                    ) m ON f.ts_code = m.ts_code AND f.end_date = m.md"""
            )
            if not fdf.empty:
                fin_map = {r['ts_code']: r.to_dict() for _, r in fdf.iterrows()}
        except Exception:
            pass
        # ─────────────────────────────────────────────────────────────────────

        signals = []
        for _, pos in positions.iterrows():
            ts_code = pos['ts_code']
            name = pos.get('name', ts_code)
            current_price = float(pos.get('current_price') or 0)
            avg_cost = float(pos.get('avg_cost') or 0)
            company_type = pos.get('company_type') or 'growth'
            pl_pct = (current_price - avg_cost) / avg_cost if avg_cost > 0 else 0

            # ---- 信号1: 技术止损（与 RiskController 同阈值）----
            _rc_stop = abs(float((Config.get('trading_agent.risk') or {}).get('stop_loss', self.stop_loss_pct)))
            if avg_cost > 0 and current_price > 0 and pl_pct <= -_rc_stop:
                signals.append({
                    'ts_code': ts_code, 'name': name,
                    'signal_type': 'stop_loss',
                    'reason': f'亏损{pl_pct*100:.1f}%，触及止损线-{_rc_stop*100:.0f}%',
                    'urgency': 'high',
                    'current_price': current_price,
                    'profit_loss_pct': pl_pct,
                })
                continue  # 止损优先级最高，跳过其他检查

            # ---- 信号2: 估值到顶 ----
            val = val_map.get(ts_code)
            if val:
                try:
                    if val.get('valuation_signal') == 'expensive':
                        cfg = _CHEAP_THRESHOLDS.get(company_type,
                                                     _CHEAP_THRESHOLDS.get('growth', {}))
                        metric = cfg.get('metric', 'pe_percentile')
                        if metric == 'pe_percentile':
                            pct = val.get('pe_percentile_5y')
                            mstr = f"PE历史分位{pct:.0f}%" if pct else "PE偏高"
                        elif metric == 'pb_percentile':
                            pct = val.get('pb_percentile_5y')
                            mstr = f"PB历史分位{pct:.0f}%" if pct else "PB偏高"
                        elif metric == 'peg':
                            peg = val.get('peg')
                            mstr = f"PEG={peg:.2f}" if peg else "PEG偏高"
                        elif metric == 'dividend_yield':
                            dy = val.get('dividend_yield')
                            mstr = f"股息率{dy:.1f}%（偏低）" if dy else "股息率偏低"
                        else:
                            mstr = "估值偏高"
                        signals.append({
                            'ts_code': ts_code, 'name': name,
                            'signal_type': 'valuation_expensive',
                            'reason': f'估值到顶：{mstr}',
                            'urgency': 'medium',
                            'current_price': current_price,
                            'profit_loss_pct': pl_pct,
                        })
                        continue  # 已有估值信号，跳过盈利检查
                except Exception:
                    pass

            # ---- 信号3: 盈利逻辑破坏（从 fin_map 批量预加载的数据中读取）----
            fin_row = fin_map.get(ts_code)
            if fin_row:
                try:
                    broken_parts = []
                    net_yoy_raw = fin_row.get('net_profit_yoy')
                    if net_yoy_raw is not None:
                        try:
                            yoy_val = float(net_yoy_raw)
                            if yoy_val < -20:
                                broken_parts.append(f"净利润同比{yoy_val:.1f}%")
                        except Exception:
                            pass
                    # ROE+净利润双降：需要多期数据，跳过（批量查询只取最新1期）
                    if broken_parts:
                        signals.append({
                            'ts_code': ts_code, 'name': name,
                            'signal_type': 'profit_driver_broken',
                            'reason': f'盈利逻辑破坏：{", ".join(broken_parts)}',
                            'urgency': 'medium',
                            'current_price': current_price,
                            'profit_loss_pct': pl_pct,
                        })
                except Exception:
                    pass

            # ---- 信号4: 机会成本（持有 > 180天 且涨幅 < 5%）----
            buy_date_str = str(pos.get('buy_date') or '')
            if buy_date_str:
                try:
                    from datetime import date as _date
                    buy_dt = _date.fromisoformat(buy_date_str[:10]) if '-' in buy_date_str else \
                             _date(int(buy_date_str[:4]), int(buy_date_str[4:6]), int(buy_date_str[6:8]))
                    hold_days = (_date.today() - buy_dt).days
                    if hold_days >= 180 and 0 <= pl_pct < 0.05:
                        signals.append({
                            'ts_code': ts_code, 'name': name,
                            'signal_type': 'opportunity_cost',
                            'reason': f'持有{hold_days}天涨幅仅{pl_pct*100:.1f}%，建议评估是否换仓',
                            'urgency': 'low',
                            'current_price': current_price,
                            'profit_loss_pct': pl_pct,
                        })
                except Exception:
                    pass

        return signals

    # ========================================================================
    # 持仓看板（用于钉钉推送）
    # ========================================================================

    def format_holding_dashboard(self) -> str:
        """
        生成持仓看板文本，用于早盘推送。
        显示：每只持仓的成本、当前价、盈亏、估值状态、预警信号。
        """
        positions = self.get_current_positions()
        if positions.empty:
            return ""

        ve = None
        cheap_thresholds = {}
        try:
            from src.valuation.valuation_engine import ValuationEngine, _CHEAP_THRESHOLDS
            ve = ValuationEngine()
            cheap_thresholds = _CHEAP_THRESHOLDS
        except Exception:
            pass

        sell_signals = self.check_sell_signals()
        signal_map = {s['ts_code']: s for s in sell_signals}

        total_pl = float(positions['profit_loss'].sum()) if 'profit_loss' in positions.columns else 0
        total_cost = float((positions['shares'] * positions['avg_cost']).sum())
        total_pl_pct = total_pl / total_cost * 100 if total_cost > 0 else 0

        lines = [
            f"### 📊 持仓看板  {len(positions)}只 | 浮动盈亏 {total_pl:+.0f}元 ({total_pl_pct:+.1f}%)\n"
        ]

        signal_icons = {'cheap': '低估', 'fair': '合理', 'expensive': '高估[!]', 'unknown': '-'}

        for _, pos in positions.iterrows():
            ts_code = pos['ts_code']
            name = (pos.get('name') or ts_code)[:6]
            avg_cost = float(pos.get('avg_cost') or 0)
            current_price = float(pos.get('current_price') or avg_cost)
            pl_pct = float(pos.get('profit_loss_pct') or 0)
            buy_phase = int(pos['buy_phase']) if 'buy_phase' in positions.columns and pos.get('buy_phase') else 1
            company_type = pos.get('company_type') or 'growth' if 'company_type' in positions.columns else 'growth'

            # 估值状态
            val_str = ""
            if ve:
                try:
                    val = ve.get_latest(ts_code)
                    if val:
                        signal = val.get('valuation_signal', 'unknown')
                        cfg = cheap_thresholds.get(company_type, cheap_thresholds.get('growth', {}))
                        metric = cfg.get('metric', 'pe_percentile')
                        if metric == 'pe_percentile':
                            pct = val.get('pe_percentile_5y')
                            val_str = f"PE分位{pct:.0f}%({signal_icons[signal]})" if pct else signal_icons[signal]
                        elif metric == 'pb_percentile':
                            pct = val.get('pb_percentile_5y')
                            val_str = f"PB分位{pct:.0f}%({signal_icons[signal]})" if pct else signal_icons[signal]
                        elif metric == 'peg':
                            peg = val.get('peg')
                            val_str = f"PEG={peg:.1f}({signal_icons[signal]})" if peg else signal_icons[signal]
                        elif metric == 'dividend_yield':
                            dy = val.get('dividend_yield')
                            val_str = f"股息{dy:.1f}%({signal_icons[signal]})" if dy else signal_icons[signal]
                except Exception:
                    pass

            phase_str = f"第{buy_phase}批 " if buy_phase else ""
            pl_sign = "+" if pl_pct >= 0 else ""
            line = (f"- **{name}**({ts_code[:6]}) {phase_str}"
                    f"成本{avg_cost:.2f}→{current_price:.2f} | {pl_sign}{pl_pct*100:.1f}%")
            if val_str:
                line += f" | {val_str}"

            if ts_code in signal_map:
                sig = signal_map[ts_code]
                urgency_mark = "[!!]" if sig['urgency'] == 'high' else "[!]"
                line += f"\n  {urgency_mark} {sig['reason']}"

            lines.append(line)

        if sell_signals:
            lines.append(f"\n**[!] {len(sell_signals)}只股票触发卖出信号，请关注**")

        return "\n".join(lines)

    # ========================================================================
    # 内部辅助
    # ========================================================================

    def _migrate_positions_table(self):
        """向旧版 positions 表追加新字段（如果不存在）"""
        for col, definition in [
            ("company_type", "VARCHAR(30)"),
            ("buy_phase", "TINYINT DEFAULT 1"),
        ]:
            try:
                DBUtils.execute(f"ALTER TABLE positions ADD COLUMN {col} {definition}")
            except Exception:
                pass  # 字段已存在，忽略

    # ========================================================================
    # 应急清仓
    # ========================================================================

    def emergency_liquidate_all(self, reason: str, trade_date: str = None) -> Dict:
        """
        应急全部清仓（模拟交易）

        Args:
            reason: 清仓原因（如"重大事件：美国打击伊朗"）
            trade_date: 交易日期，None 则使用今日

        Returns:
            清仓报告字典
        """
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y%m%d')

        positions = self.get_current_positions()
        if positions.empty:
            print("[PositionManager] 当前无持仓，无需清仓")
            return {'liquidated': 0, 'total_amount': 0, 'positions': []}

        liquidated = []
        total_amount = 0.0
        total_profit_loss = 0.0

        with DBUtils.get_conn() as conn:
            cursor = conn.cursor()
            for _, pos in positions.iterrows():
                ts_code = pos['ts_code']
                name = pos.get('name', ts_code)
                shares = pos['shares']
                current_price = pos['current_price'] if pos['current_price'] > 0 else pos['avg_cost']
                amount = shares * current_price
                commission = amount * 0.0003
                profit_loss = pos.get('profit_loss', 0)
                profit_loss_pct = pos.get('profit_loss_pct', 0)

                # 记录卖出交易
                cursor.execute("""
                INSERT INTO transactions
                (ts_code, name, action, price, shares, amount, commission,
                 trade_date, strategy, notes)
                VALUES (?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?)
                """, (ts_code, name, current_price, shares, amount, commission,
                      trade_date, 'EmergencyLiquidate', f'应急清仓: {reason}'))

                liquidated.append({
                    'ts_code': ts_code,
                    'name': name,
                    'shares': shares,
                    'price': current_price,
                    'amount': amount,
                    'profit_loss': profit_loss,
                    'profit_loss_pct': profit_loss_pct,
                })
                total_amount += amount
                total_profit_loss += profit_loss

            # 清空持仓表
            cursor.execute("DELETE FROM positions")

        print(f"[PositionManager] 应急清仓完成: {len(liquidated)} 只股票, 合计 {total_amount:,.0f} 元")
        return {
            'liquidated': len(liquidated),
            'total_amount': total_amount,
            'total_profit_loss': total_profit_loss,
            'trade_date': trade_date,
            'reason': reason,
            'positions': liquidated,
        }


if __name__ == "__main__":
    # 测试代码
    print("\n" + "="*60)
    print("PositionManager 测试")
    print("="*60)
    
    # 创建测试数据
    test_stocks = pd.DataFrame({
        'ts_code': ['000001.SZ', '600519.SH', '300750.SZ'],
        'name': ['平安银行', '贵州茅台', '宁德时代'],
        'close': [10.5, 1650.0, 180.0],
        'final_score': [0.85, 0.92, 0.78]
    })
    
    pm = PositionManager(total_capital=1000000)
    result = pm.allocate_positions(test_stocks, method='proportional')
    
    print("\n分配结果:")
    print(result[['ts_code', 'name', 'close', 'final_score', 'position_pct', 'shares', 'amount']])
