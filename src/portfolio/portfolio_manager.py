import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils

class PortfolioManager:
    """持仓管理器"""
    
    def __init__(self):
        """初始化持仓管理器"""
        
        # 初始化持仓表
        self._init_portfolio_tables()
    
    def _init_portfolio_tables(self):
        """初始化持仓表"""
        # 创建持仓表
        DBUtils.execute('''
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY,
            ts_code TEXT,
            name TEXT,
            buy_date TEXT,
            buy_price REAL,
            quantity INTEGER,
            stop_loss_price REAL,
            status TEXT,
            sell_date TEXT,
            sell_price REAL,
            profit_loss REAL,
            profit_loss_pct REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 创建交易信号表
        DBUtils.execute('''
        CREATE TABLE IF NOT EXISTS trade_signals (
            id INTEGER PRIMARY KEY,
            signal_date TEXT,
            ts_code TEXT,
            name TEXT,
            signal_type TEXT,
            price REAL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        print("Successfully initialized portfolio tables")
    
    def add_position(self, ts_code, name, buy_date, buy_price, quantity, stop_loss_price):
        """添加持仓
        
        Args:
            ts_code: 股票代码
            name: 股票名称
            buy_date: 买入日期
            buy_price: 买入价格
            quantity: 持仓数量
            stop_loss_price: 止损价格
        """
        DBUtils.execute('''
        INSERT INTO portfolio (ts_code, name, buy_date, buy_price, quantity, stop_loss_price, status)
        VALUES (?, ?, ?, ?, ?, ?, '持仓中')
        ''', [ts_code, name, buy_date, buy_price, quantity, stop_loss_price])
        
        print(f"Added position: {ts_code} {name} at {buy_price}")
    
    def close_position(self, position_id, sell_date, sell_price):
        """平仓
        
        Args:
            position_id: 持仓ID
            sell_date: 卖出日期
            sell_price: 卖出价格
        """
        # 获取持仓信息
        df = DBUtils.query_df('''
        SELECT * FROM portfolio WHERE id = ?
        ''', [position_id])
        
        if df.empty:
            print(f"Position {position_id} not found")
            return
        
        position = df.iloc[0]
        
        # 计算盈亏
        buy_price = position['buy_price']
        quantity = position['quantity']
        profit_loss = (sell_price - buy_price) * quantity
        profit_loss_pct = (sell_price - buy_price) / buy_price * 100
        
        # 更新持仓
        DBUtils.execute('''
        UPDATE portfolio 
        SET status = '已平仓',
            sell_date = ?,
            sell_price = ?,
            profit_loss = ?,
            profit_loss_pct = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''', [sell_date, sell_price, profit_loss, profit_loss_pct, position_id])
        
        print(f"Closed position {position_id}: P/L = {profit_loss:.2f} ({profit_loss_pct:.2f}%)")
    
    def get_open_positions(self):
        """获取当前持仓"""
        result = DBUtils.query_df('''
        SELECT * FROM portfolio WHERE status = '持仓中' ORDER BY buy_date DESC
        ''')
        
        return result
    
    def get_closed_positions(self, limit=None):
        """获取已平仓记录"""
        query = '''
        SELECT * FROM portfolio WHERE status = '已平仓' ORDER BY sell_date DESC
        '''
        
        if limit:
            query += f' LIMIT {limit}'
        
        result = DBUtils.query_df(query)
        
        return result
    
    def add_signal(self, signal_date, ts_code, name, signal_type, price, reason):
        """添加交易信号
        
        Args:
            signal_date: 信号日期
            ts_code: 股票代码
            name: 股票名称
            signal_type: 信号类型（买入/卖出/止损）
            price: 价格
            reason: 信号原因
        """
        DBUtils.execute('''
        INSERT INTO trade_signals (signal_date, ts_code, name, signal_type, price, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', [signal_date, ts_code, name, signal_type, price, reason])
        
        print(f"Added signal: {signal_type} {ts_code} {name} at {price}")
    
    def get_latest_signals(self, limit=10):
        """获取最新交易信号"""
        result = DBUtils.query_df(f'''
        SELECT * FROM trade_signals 
        ORDER BY signal_date DESC, created_at DESC 
        LIMIT {limit}
        ''')
        
        return result
    
    def check_stop_loss(self, current_date):
        """检查止损
        
        Args:
            current_date: 当前日期
        """
        open_positions = self.get_open_positions()
        
        for _, position in open_positions.iterrows():
            ts_code = position['ts_code']
            position_id = position['id']
            stop_loss_price = position['stop_loss_price']
            
            # 获取最新价格
            df = DBUtils.query_df(f'''
            SELECT close FROM stock_daily 
            WHERE ts_code = '{ts_code}' AND trade_date <= '{current_date}'
            ORDER BY trade_date DESC LIMIT 1
            ''')
            latest_price = df.iloc[0]['close'] if not df.empty else None
            
            if latest_price:
                current_price = latest_price
                
                # 检查是否触发止损
                if current_price <= stop_loss_price:
                    # 平仓
                    self.close_position(position_id, current_date, current_price)
                    
                    # 添加止损信号
                    self.add_signal(
                        current_date, ts_code, position['name'], 
                        '止损', current_price, f"价格跌破止损价 {stop_loss_price}"
                    )
    
    def generate_buy_signals(self, selected_stocks, current_date):
        """生成买入信号
        
        Args:
            selected_stocks: 选中的股票
            current_date: 当前日期
        """
        if selected_stocks is None or len(selected_stocks) == 0:
            return
        
        for _, stock in selected_stocks.iterrows():
            ts_code = stock['ts_code']
            name = stock['name']
            close_price = stock.get('close', 0)
            stop_loss_price = stock.get('stop_loss_price', 0)
            
            # 添加买入信号
            self.add_signal(
                current_date, ts_code, name,
                '买入', close_price, f"综合得分: {stock.get('score', 0):.4f}"
            )
            
            # 添加持仓
            self.add_position(
                ts_code, name, current_date, close_price, 100, stop_loss_price
            )
    
    def get_portfolio_summary(self):
        """获取持仓摘要"""
        # 获取当前持仓
        open_positions = self.get_open_positions()
        
        # 获取已平仓记录
        closed_positions = self.get_closed_positions(limit=100)
        
        # 计算当前持仓市值
        total_market_value = 0
        total_cost = 0
        
        for _, position in open_positions.iterrows():
            ts_code = position['ts_code']
            quantity = position['quantity']
            buy_price = position['buy_price']
            
            # 获取最新价格
            df = DBUtils.query_df(f'''
            SELECT close FROM stock_daily 
            WHERE ts_code = '{ts_code}'
            ORDER BY trade_date DESC LIMIT 1
            ''')
            
            if not df.empty:
                current_price = df.iloc[0]['close']
                if current_price:
                    market_value = current_price * quantity
                    cost = buy_price * quantity
                    
                    total_market_value += market_value
                    total_cost += cost
        
        # 计算已平仓总盈亏
        total_closed_pnl = 0
        if len(closed_positions) > 0:
            total_closed_pnl = closed_positions['profit_loss'].sum()
        
        # 计算当前持仓盈亏
        current_pnl = total_market_value - total_cost
        
        summary = {
            'open_positions_count': len(open_positions),
            'closed_positions_count': len(closed_positions),
            'total_market_value': total_market_value,
            'total_cost': total_cost,
            'current_pnl': current_pnl,
            'current_pnl_pct': (current_pnl / total_cost * 100) if total_cost > 0 else 0,
            'total_closed_pnl': total_closed_pnl,
            'total_pnl': current_pnl + total_closed_pnl
        }
        
        return summary
    
    def print_summary(self):
        """打印持仓摘要"""
        summary = self.get_portfolio_summary()
        
        print("\n" + "="*50)
        print("持仓摘要")
        print("="*50)
        print(f"当前持仓数: {summary['open_positions_count']}")
        print(f"已平仓数: {summary['closed_positions_count']}")
        print(f"持仓市值: {summary['total_market_value']:.2f}")
        print(f"持仓成本: {summary['total_cost']:.2f}")
        print(f"当前盈亏: {summary['current_pnl']:.2f} ({summary['current_pnl_pct']:.2f}%)")
        print(f"已平仓盈亏: {summary['total_closed_pnl']:.2f}")
        print(f"总盈亏: {summary['total_pnl']:.2f}")
        print("="*50)
    
    def close(self):
        """关闭资源"""
        print("Successfully closed PortfolioManager resources")
    
    def __del__(self):
        """析构函数，确保连接被关闭"""
        self.close()

if __name__ == '__main__':
    # 测试代码
    manager = PortfolioManager()
    
    # 添加测试持仓
    manager.add_position(
        '600519.SH', '贵州茅台', 
        '2026-02-05', 1800.00, 100, 1764.00
    )
    
    # 打印摘要
    manager.print_summary()
    
    # 获取最新信号
    signals = manager.get_latest_signals(limit=5)
    print("\n最新交易信号:")
    print(signals)
    
    manager.close()
