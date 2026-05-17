#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QuantAgent-Alpha 主入口文件
"""

import os
import sys
import click
import yaml
import logging
from datetime import datetime

# 添加项目根目录到 Python 路径
_project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _project_root)

from src.collector.data_loader import DataLoader
from src.strategy.topk_strategy import TopKStrategy

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/quant_agent.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

class QuantAgent:
    def __init__(self, config_path='config/settings.yaml'):
        """初始化QuantAgent"""
        # 加载配置
        self.config_path = config_path
        self.config = self._load_config()
        
        # 初始化数据加载器
        self.data_loader = None
        
        # 初始化策略
        self.strategy = None
        
        # 确保日志目录存在
        os.makedirs('logs', exist_ok=True)
    
    def _load_config(self):
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            logger.info(f"Successfully loaded config from {self.config_path}")
            return config
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            raise
    
    def init_data_loader(self):
        """初始化数据加载器（使用 Config 单例，无需传入 config）"""
        try:
            self.data_loader = DataLoader()
            logger.info("Successfully initialized DataLoader")
        except Exception as e:
            logger.error(f"Failed to initialize DataLoader: {e}")
            raise
    
    def init_strategy(self):
        """初始化策略（TopKStrategy 使用 Config 单例）"""
        try:
            self.strategy = TopKStrategy()
            logger.info("Successfully initialized TopKStrategy")
        except Exception as e:
            logger.error(f"Failed to initialize TopKStrategy: {e}")
            raise
    
    def update_data(self, market=None):
        """更新数据（使用 sync_daily_data 统一接口）"""
        if self.data_loader is None:
            self.init_data_loader()
        
        logger.info("Starting data update...")
        
        try:
            if market is None or market == 'A股':
                logger.info("Syncing A股 daily data...")
                result = self.data_loader.sync_daily_data(full_market=True)
                if isinstance(result, dict) and result.get('success'):
                    logger.info(f"Sync OK: {result.get('total_inserted', 0)} records")
                else:
                    logger.warning(f"Sync result: {result}")
            if market is None or market == '港股':
                logger.info("港股同步暂未实现，跳过")
            # 同步概念
            if market is None or market == 'A股':
                logger.info("Syncing concepts...")
                self.data_loader.sync_concepts()
            self.data_loader.close()
            logger.info("Data update completed successfully!")
        except Exception as e:
            logger.error(f"Data update failed: {e}")
            raise
    
    def run_backtest(self):
        """运行回测"""
        if self.strategy is None:
            self.init_strategy()
        
        backtest_cfg = self.config.get('backtest', {})
        start_date = backtest_cfg.get('start_date') or self.config.get('start_date', '20200101')
        end_date = backtest_cfg.get('end_date', datetime.now().strftime('%Y%m%d'))
        
        logger.info(f"Starting backtest...")
        logger.info(f"Backtest period: {start_date} to {end_date}")
        
        try:
            report = self.strategy.run_backtest(start_date, end_date, top_k=self.config.get('strategy', {}).get('topk', 10))
            metrics = {'stocks_count': len(report)} if report is not None and not report.empty else {}
            
            logger.info("Backtest completed successfully!")
            logger.info("Backtest metrics:")
            for key, value in metrics.items():
                logger.info(f"{key}: {value}")
            
            return metrics
        except Exception as e:
            logger.error(f"Failed to run backtest: {e}")
            raise
    
    def run_strategy(self, date=None):
        """运行实盘策略"""
        if self.strategy is None:
            self.init_strategy()
        
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        
        logger.info(f"Running strategy for date: {date}")
        
        try:
            portfolio = self.strategy.execute_trade(date)
            
            logger.info("Strategy execution completed successfully!")
            logger.info(f"Selected portfolio: {portfolio}")
            
            return portfolio
        except Exception as e:
            logger.error(f"Failed to execute strategy: {e}")
            raise
    
    def start_dashboard(self):
        """启动Streamlit仪表盘"""
        import subprocess
        
        streamlit_config = self.config.get('streamlit', {})
        port = streamlit_config.get('port', 8501)
        host = streamlit_config.get('host', 'localhost')
        
        logger.info(f"Starting Streamlit dashboard on {host}:{port}...")
        
        # 启动 Streamlit（使用 src/app/dashboard.py）
        dashboard_path = os.path.join(_project_root, 'src', 'app', 'dashboard.py')
        cmd = [
            sys.executable, '-m', 'streamlit', 'run', dashboard_path,
            '--server.port', str(port),
            '--server.address', host
        ]
        
        try:
            subprocess.run(cmd, check=True)
        except Exception as e:
            logger.error(f"Failed to start Streamlit dashboard: {e}")
            raise

@click.command()
@click.option('--config', default='config/settings.yaml', help='配置文件路径')
@click.option('--market', type=click.Choice(['A股', '港股', 'all']), default='all', help='市场类型')
def update_data(config, market):
    """更新数据"""
    agent = QuantAgent(config)
    agent.update_data(market if market != 'all' else None)

@click.command()
def backtest():
    """运行回测"""
    agent = QuantAgent()
    agent.run_backtest()

@click.command()
@click.option('--date', default=None, help='交易日期 (YYYY-MM-DD)')
def run_strategy(date):
    """运行实盘策略"""
    agent = QuantAgent()
    agent.run_strategy(date)

@click.command()
def start_dashboard():
    """启动Streamlit仪表盘"""
    agent = QuantAgent()
    agent.start_dashboard()

@click.group()
def cli():
    """QuantAgent-Alpha 命令行工具"""
    pass

# 添加命令
cli.add_command(update_data)
cli.add_command(backtest)
cli.add_command(run_strategy)
cli.add_command(start_dashboard)

if __name__ == '__main__':
    # 测试代码
    try:
        agent = QuantAgent()
        
        # 测试数据更新
        # agent.update_data()
        
        # 测试回测
        # agent.run_backtest()
        
        # 测试策略执行
        # agent.run_strategy()
        
        logger.info("QuantAgent-Alpha initialized successfully!")
    except Exception as e:
        logger.error(f"Error: {e}")
        sys.exit(1)
