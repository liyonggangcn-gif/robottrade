import os
import time
import schedule
from loguru import logger

# 导入核心模块
from src.collector.data_loader import UniversalDataLoader
from src.factors.alpha_engine import AlphaEngine
from src.strategy.topk_strategy import TopKStrategy
from src.utils.notifier import NotifierFactory
from src.utils.config_loader import Config

# 配置日志
logger.add(
    "logs/bot_{time}.log",
    rotation="1 day",
    retention="7 days",
    compression="zip",
    level="INFO"
)

class TradingBot:
    """交易机器人"""
    
    def __init__(self):
        """初始化交易机器人"""
        logger.info("Initializing TradingBot...")
        
        # 加载配置（使用全局实例）
        self.config = Config
        
        # 初始化通知器
        self.notifier = self._init_notifier()
        
        logger.info("TradingBot initialized successfully")
    
    def _init_notifier(self):
        """初始化通知器"""
        try:
            notification_config = self.config.get('notification', {})
            enabled = notification_config.get('enabled', False)
            
            if not enabled:
                logger.info("Notification is disabled in config")
                return None
            
            provider = notification_config.get('provider', 'feishu')
            
            if provider == 'feishu':
                feishu_config = notification_config.get('feishu', {})
                webhook = feishu_config.get('webhook', '')
                secret_word = feishu_config.get('secret_word', '')
                
                if not webhook or 'YOUR_HOOK_HERE' in webhook:
                    logger.warning("Feishu webhook is not configured properly")
                    return None
                
                logger.info("Initializing Feishu notifier")
                return NotifierFactory.create_notifier(
                    'feishu',
                    webhook_url=webhook,
                    secret_word=secret_word
                )
            elif provider == 'dingtalk':
                dingtalk_config = notification_config.get('dingtalk', {})
                webhook = dingtalk_config.get('webhook', '')
                secret_word = dingtalk_config.get('secret_word', '')
                
                if not webhook or 'YOUR_TOKEN_HERE' in webhook:
                    logger.warning("DingTalk webhook is not configured properly")
                    return None
                
                logger.info("Initializing DingTalk notifier")
                return NotifierFactory.create_notifier(
                    'dingtalk',
                    webhook_url=webhook,
                    secret_word=secret_word
                )
            else:
                logger.warning(f"Unsupported notification provider: {provider}")
                return None
        except Exception as e:
            logger.error(f"Error initializing notifier: {e}")
            return None
    
    def run_daily_job(self):
        """执行每日任务"""
        logger.info("开始执行每日任务")
        
        try:
            # 1. 同步数据
            logger.info("同步每日数据...")
            data_loader = UniversalDataLoader()
            try:
                data_loader.sync_daily_data(full_market=True, batch_size=100)
                logger.success("数据同步完成")
            finally:
                data_loader.close()
            
            # 2. 更新因子
            logger.info("更新因子数据...")
            alpha_engine = AlphaEngine()
            try:
                alpha_engine.update_factors()
                logger.success("因子更新完成")
            finally:
                alpha_engine.close()
            
            # 3. 获取选股结果
            logger.info("获取选股结果...")
            strategy = TopKStrategy(read_only=True)
            try:
                latest_date = strategy.get_latest_trade_date()
                if not latest_date:
                    logger.error("无法获取最新交易日")
                    return
                
                top_stocks = strategy.get_top_stocks(latest_date, top_k=5)
                if top_stocks is None or len(top_stocks) == 0:
                    logger.error("未选出符合条件的股票")
                    return
                
                logger.success(f"成功选出 {len(top_stocks)} 只股票")
                
                # 4. 格式化选股结果
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
                
                # 5. 发送通知
                if self.notifier:
                    logger.info("发送选股结果通知...")
                    success = self.notifier.send_message(
                        f"每日选股结果 ({latest_date})",
                        content
                    )
                    if success:
                        logger.success("通知发送成功")
                    else:
                        logger.error("通知发送失败")
                else:
                    logger.warning("未配置通知器，跳过发送")
                    # 打印结果到控制台
                    logger.info(f"\n{content}")
            finally:
                strategy.close()
            
        except Exception as e:
            logger.error(f"执行每日任务时出错: {e}")
            # 发送错误通知
            if self.notifier:
                error_content = f"# 任务执行失败\n\n**错误信息:**\n{e}"
                self.notifier.send_message(
                    "任务执行失败",
                    error_content
                )
        finally:
            logger.info("每日任务执行完成")
    
    def start(self):
        """启动机器人"""
        logger.info("启动交易机器人...")
        
        # 设置定时任务
        schedule.every().day.at("09:00").do(self.run_daily_job)
        logger.info("已设置定时任务: 每天 09:00 执行")
        
        # 立即执行一次任务
        logger.info("立即执行一次任务以测试...")
        self.run_daily_job()
        
        # 主循环
        logger.info("进入主循环，等待定时任务执行...")
        while True:
            try:
                schedule.run_pending()
                time.sleep(60)
            except KeyboardInterrupt:
                logger.info("收到中断信号，正在退出...")
                break
            except Exception as e:
                logger.error(f"主循环出错: {e}")
                time.sleep(60)
    
    def close(self):
        """关闭资源"""
        logger.info("关闭资源...")
        logger.info("资源关闭完成")

if __name__ == '__main__':
    # 确保logs目录存在
    os.makedirs('logs', exist_ok=True)
    
    bot = TradingBot()
    
    try:
        bot.start()
    finally:
        bot.close()
        logger.info("TradingBot exited")
