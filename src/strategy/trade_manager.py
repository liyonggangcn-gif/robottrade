import pandas as pd
from datetime import datetime
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils
from src.utils.notifier import DingTalkNotifier
from src.portfolio.portfolio_manager import PortfolioManager
from src.strategy.topk_strategy import TopKStrategy
from src.strategy.small_cap_jinx import SmallCapJinxStrategy

class TradeManager:
    """交易管家
    
    负责协调策略执行、信号生成和消息推送
    """
    
    def __init__(self, strategy_type='topk', read_only=False):
        """初始化交易管家
        
        Args:
            strategy_type: 策略类型，可选 'topk' 或 'smallcap'
            read_only: 是否使用只读连接
        """
        self.strategy_type = strategy_type
        self.portfolio_manager = PortfolioManager()
        
        if strategy_type == 'topk':
            self.strategy = TopKStrategy(read_only=read_only)
        elif strategy_type == 'smallcap':
            self.strategy = SmallCapJinxStrategy(read_only=read_only)
        else:
            raise ValueError(f"Unsupported strategy type: {strategy_type}")
        
        print(f"TradeManager initialized with {strategy_type} strategy")
    
    def generate_signals(self, top_k=10):
        """生成交易信号
        
        Args:
            top_k: 选取数量
            
        Returns:
            list: 交易信号列表
        """
        signals = []
        
        try:
            # 获取最新交易日
            latest_date = self.strategy.get_latest_trade_date()
            
            if not latest_date:
                print("无法获取最新交易日")
                return signals
            
            print(f"生成交易信号，日期: {latest_date}")
            
            # 获取当前持仓
            open_positions = self.portfolio_manager.get_open_positions()
            open_codes = set(open_positions['ts_code'].tolist()) if not open_positions.empty else set()
            
            # 检查止损信号
            self.portfolio_manager.check_stop_loss(latest_date)
            
            # 获取最新止损信号
            stop_loss_signals = DBUtils.query_df(f'''
            SELECT * FROM trade_signals 
            WHERE signal_date = '{latest_date}' AND signal_type = '止损'
            ORDER BY created_at DESC
            ''')
            
            # 添加止损信号到结果
            for _, row in stop_loss_signals.iterrows():
                signals.append({
                    'type': 'SELL',
                    'code': row['ts_code'],
                    'name': row['name'],
                    'price': row['price'],
                    'reason': row['reason'],
                    'date': row['signal_date']
                })
            
            # 获取选股结果
            selected_stocks = self.strategy.get_top_stocks(latest_date, top_k)
            
            if selected_stocks is None or selected_stocks.empty:
                print("未选出股票")
            else:
                # 生成买入信号
                for _, stock in selected_stocks.iterrows():
                    ts_code = stock['ts_code']
                    name = stock['name']
                    close_price = stock.get('close', 0)
                    score = stock.get('score', 0)
                    stop_loss_price = stock.get('stop_loss_price', 0)
                    
                    # 检查是否已在持仓中
                    if ts_code in open_codes:
                        signals.append({
                            'type': 'HOLD',
                            'code': ts_code,
                            'name': name,
                            'price': close_price,
                            'reason': f"继续持有，综合得分: {score:.4f}",
                            'score': score,
                            'date': latest_date
                        })
                    else:
                        signals.append({
                            'type': 'BUY',
                            'code': ts_code,
                            'name': name,
                            'price': close_price,
                            'reason': f"综合得分: {score:.4f}",
                            'score': score,
                            'date': latest_date
                        })
            
            # 检查轮动信号（持仓但不在新选股列表中）
            if not open_positions.empty and selected_stocks is not None and not selected_stocks.empty:
                selected_codes = set(selected_stocks['ts_code'].tolist())
                
                for _, position in open_positions.iterrows():
                    ts_code = position['ts_code']
                    name = position['name']
                    
                    if ts_code not in selected_codes and ts_code not in open_codes:
                        signals.append({
                            'type': 'SELL',
                            'code': ts_code,
                            'name': name,
                            'price': position['buy_price'],
                            'reason': '轮动卖出',
                            'date': latest_date
                        })
            
            print(f"共生成 {len(signals)} 条交易信号")
            
        except Exception as e:
            print(f"生成交易信号失败: {e}")
        
        return signals
    
    def send_notification(self, signals):
        """发送钉钉通知
        
        Args:
            signals: 交易信号列表
        """
        if not signals:
            print("今日无操作信号，跳过推送")
            return
        
        try:
            current_date = datetime.now().strftime('%Y-%m-%d')
            msg_title = f"🚀 量化交易指引 ({current_date})"
            lines = [f"## {msg_title}"]
            
            # 1. 优先展示卖出信号 (风险控制)
            sell_signals = [s for s in signals if s['type'] == 'SELL']
            if sell_signals:
                lines.append("\n### 🛑 卖出/止损操作")
                for s in sell_signals:
                    icon = "💔" if "止损" in s['reason'] else "🔄"
                    lines.append(f"- {icon} **{s['name']}** ({s['code']}): {s['reason']} @ {s['price']:.2f}")
            
            # 2. 展示买入信号 (机会)
            buy_signals = [s for s in signals if s['type'] == 'BUY']
            if buy_signals:
                lines.append("\n### 🟢 买入/建仓操作")
                for s in buy_signals:
                    lines.append(f"- 💰 **{s['name']}** ({s['code']}): 综合得分 {s.get('score', 0):.2f} @ {s['price']:.2f}")
            
            # 3. 展示继续持有信号
            hold_signals = [s for s in signals if s['type'] == 'HOLD']
            if hold_signals:
                lines.append("\n### 📊 继续持有")
                for s in hold_signals:
                    lines.append(f"- ✅ **{s['name']}** ({s['code']}): {s['reason']} @ {s['price']:.2f}")
            
            # 4. 添加持仓摘要
            summary = self.portfolio_manager.get_portfolio_summary()
            lines.append(f"\n---\n📊 当前持仓市值: {summary['total_market_value']:.2f}")
            lines.append(f"📈 当前盈亏: {summary['current_pnl']:.2f} ({summary['current_pnl_pct']:.2f}%)")
            lines.append(f"💰 总盈亏: {summary['total_pnl']:.2f}")
            
            message_content = "\n".join(lines)
            
            # 获取钉钉配置
            webhook_url = Config.get('notification.dingtalk.webhook')
            secret_word = Config.get('notification.dingtalk.secret_word')
            
            if not webhook_url:
                print("钉钉webhook未配置，跳过推送")
                return
            
            # 发送推送
            notifier = DingTalkNotifier(webhook_url=webhook_url, secret_word=secret_word)
            success = notifier.send_message(title=msg_title, content=message_content)
            
            if success:
                print(f"✅ 钉钉通知已发送: {len(signals)} 条信号")
            else:
                print(f"❌ 钉钉发送失败")
                
        except Exception as e:
            print(f"❌ 钉钉发送失败: {e}")
    
    def run_daily_check(self, top_k=10):
        """执行每日检查
        
        Args:
            top_k: 选取数量
            
        Returns:
            list: 交易信号列表
        """
        print("\n" + "="*80)
        print("🚀 开始每日交易检查")
        print("="*80)
        
        # 生成交易信号
        signals = self.generate_signals(top_k)
        
        # 发送通知
        self.send_notification(signals)
        
        print("="*80)
        print("✅ 每日交易检查完成")
        print("="*80)
        
        return signals
    
    def get_portfolio_summary(self):
        """获取持仓摘要
        
        Returns:
            dict: 持仓摘要
        """
        return self.portfolio_manager.get_portfolio_summary()
    
    def print_summary(self):
        """打印持仓摘要"""
        self.portfolio_manager.print_summary()
    
    def close(self):
        """关闭资源"""
        if hasattr(self.strategy, 'close'):
            self.strategy.close()
        self.portfolio_manager.close()
        print("TradeManager closed successfully")
    
    def __del__(self):
        """析构函数"""
        try:
            self.close()
        except:
            pass

if __name__ == '__main__':
    # 测试代码
    print("Testing TradeManager")
    
    # 创建交易管家
    trade_manager = TradeManager(strategy_type='topk')
    
    try:
        # 执行每日检查
        signals = trade_manager.run_daily_check(top_k=10)
        
        # 打印持仓摘要
        trade_manager.print_summary()
        
    finally:
        trade_manager.close()
