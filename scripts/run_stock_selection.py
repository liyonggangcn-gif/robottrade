import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 初始化日志
from src.utils.log_utils import init_logger
logger = init_logger("run_stock_selection")

from src.factors.alpha_engine import AlphaEngine
from src.strategy.topk_strategy import TopKStrategy
from src.utils.notifier import NotifierFactory
from src.utils.config_loader import Config

def run_stock_selection():
    """运行选股策略并发送钉钉通知"""
    print("开始运行选股策略...")
    
    # 1. 更新因子数据
    print("\n1. 更新因子数据...")
    alpha_engine = AlphaEngine()
    try:
        alpha_engine.update_factors()
        print("✓ 因子更新完成")
    finally:
        alpha_engine.close()
    
    # 2. 获取选股结果
    print("\n2. 获取选股结果...")
    strategy = TopKStrategy()
    try:
        latest_date = strategy.get_latest_trade_date()
        if not latest_date:
            print("✗ 无法获取最新交易日")
            return
        
        print(f"最新交易日: {latest_date}")
        
        top_stocks = strategy.get_top_stocks(latest_date, top_k=5)
        if top_stocks is None or len(top_stocks) == 0:
            print("✗ 未选出符合条件的股票")
            return
        
        print(f"✓ 成功选出 {len(top_stocks)} 只股票")
        
        # 3. 格式化选股结果
        content = f"# 每日选股结果 ({latest_date})\n\n"
        content += "## Top 5 精选股票\n\n"
        
        for i, (_, row) in enumerate(top_stocks.iterrows(), 1):
            ts_code = row['ts_code']
            name = row['name']
            close = row.get('close', 0)
            stop_loss = row.get('stop_loss_price', 0)
            score = row.get('score', 0)
            
            content += f"### Rank {i}: {ts_code} | {name}\n"
            content += f"- 现价: {close:.2f}元\n"
            content += f"- 止损价: {stop_loss:.2f}元\n"
            content += f"- 综合得分: {score:.4f}\n\n"
        
        print("\n" + "="*60)
        print(content)
        print("="*60)
        
        # 4. 发送钉钉通知
        print("\n4. 发送钉钉通知...")
        notification_config = Config.get('notification', {})
        enabled = notification_config.get('enabled', False)
        
        if enabled:
            provider = notification_config.get('provider', 'dingtalk')
            
            if provider == 'dingtalk':
                dingtalk_config = notification_config.get('dingtalk', {})
                webhook = dingtalk_config.get('webhook', '')
                secret_word = dingtalk_config.get('secret_word', '')
                
                if webhook and 'YOUR_TOKEN_HERE' not in webhook:
                    notifier = NotifierFactory.create_notifier(
                        'dingtalk',
                        webhook_url=webhook,
                        secret_word=secret_word
                    )
                    
                    success = notifier.send_message(
                        f"每日选股结果 ({latest_date})",
                        content
                    )
                    
                    if success:
                        print("✓ 钉钉通知发送成功")
                    else:
                        print("✗ 钉钉通知发送失败")
                else:
                    print("✗ 钉钉webhook未配置")
            else:
                print(f"✗ 不支持的通知提供商: {provider}")
        else:
            print("✗ 通知功能未启用")
            
    finally:
        strategy.close()
    
    print("\n选股策略执行完成！")

if __name__ == '__main__':
    run_stock_selection()
