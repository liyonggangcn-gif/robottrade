#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 服务主程序
定期执行数据同步和选股任务
"""

import os
import sys
import time
import schedule
from datetime import datetime, time as dt_time
from threading import Event

# 添加项目根目录到路径
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

# 强制清除代理
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

# 初始化日志
from src.utils.log_utils import init_logger
logger = init_logger("service_runner")

# 导入任务模块
from scripts.daily_alpha_run import run_pipeline
from scripts.morning_push import send_morning_push
from scripts.evening_push import send_evening_push
import argparse


class ServiceRunner:
    """Windows 服务运行器"""
    
    def __init__(self):
        self.running = Event()
        self.running.set()
        logger.info("ServiceRunner initialized")
    
    def run_daily_alpha(self):
        """执行每日选股流程"""
        try:
            logger.info("=" * 60)
            logger.info("开始执行每日选股流程")
            logger.info("=" * 60)
            
            # 解析参数（跳过AI训练以加快速度）
            args = argparse.Namespace(
                skip_sync=False,
                skip_qlib=True,  # 跳过AI训练
                skip_concepts=False,
                top_k=20,
                watch_list_only=False,
                skip_notification=False
            )
            
            success = run_pipeline(args)
            
            if success:
                logger.info("每日选股流程执行成功")
            else:
                logger.warning("每日选股流程执行失败")
                
        except Exception as e:
            logger.error(f"执行每日选股流程时发生错误: {e}", exc_info=True)
    
    def run_morning_push(self):
        """执行早盘推送"""
        try:
            logger.info("=" * 60)
            logger.info("开始执行早盘推送")
            logger.info("=" * 60)
            
            success = send_morning_push()
            
            if success:
                logger.info("早盘推送执行成功")
            else:
                logger.warning("早盘推送执行失败")
                
        except Exception as e:
            logger.error(f"执行早盘推送时发生错误: {e}", exc_info=True)
    
    def run_evening_push(self):
        """执行收盘推送"""
        try:
            logger.info("=" * 60)
            logger.info("开始执行收盘推送")
            logger.info("=" * 60)
            
            success = send_evening_push()
            
            if success:
                logger.info("收盘推送执行成功")
            else:
                logger.warning("收盘推送执行失败")
                
        except Exception as e:
            logger.error(f"执行收盘推送时发生错误: {e}", exc_info=True)
    
    def setup_schedule(self):
        """设置定时任务"""
        # 每日8:00执行数据同步和选股
        schedule.every().day.at("08:00").do(self.run_daily_alpha)
        
        # 每日8:30执行早盘推送
        schedule.every().day.at("08:30").do(self.run_morning_push)
        
        # 每日16:00执行收盘推送
        schedule.every().day.at("16:00").do(self.run_evening_push)
        
        logger.info("定时任务已设置:")
        logger.info("  - 08:00: 数据同步和选股")
        logger.info("  - 08:30: 早盘推送")
        logger.info("  - 16:00: 收盘推送")
    
    def run(self):
        """运行服务主循环"""
        logger.info("=" * 60)
        logger.info("量化选股服务启动")
        logger.info(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
        # 设置定时任务
        self.setup_schedule()
        
        # 如果是启动时已经过了8:00，立即执行一次
        current_time = datetime.now().time()
        if current_time >= dt_time(8, 0) and current_time < dt_time(8, 30):
            logger.info("当前时间已过8:00，立即执行数据同步和选股")
            self.run_daily_alpha()
        
        # 主循环
        logger.info("服务主循环开始运行...")
        while self.running.is_set():
            try:
                schedule.run_pending()
                time.sleep(60)  # 每分钟检查一次
            except KeyboardInterrupt:
                logger.info("收到停止信号，正在关闭服务...")
                self.stop()
                break
            except Exception as e:
                logger.error(f"服务主循环错误: {e}", exc_info=True)
                time.sleep(60)  # 出错后等待1分钟再继续
    
    def stop(self):
        """停止服务"""
        logger.info("正在停止服务...")
        self.running.clear()
        schedule.clear()
        logger.info("服务已停止")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='量化选股 Windows 服务')
    parser.add_argument('--test', action='store_true', help='测试模式：立即执行一次所有任务')
    args = parser.parse_args()
    
    service = ServiceRunner()
    
    if args.test:
        # 测试模式：立即执行所有任务
        logger.info("测试模式：立即执行所有任务")
        service.run_daily_alpha()
        time.sleep(5)
        service.run_morning_push()
        time.sleep(5)
        service.run_evening_push()
        logger.info("测试完成")
    else:
        # 正常模式：运行服务
        try:
            service.run()
        except Exception as e:
            logger.error(f"服务运行错误: {e}", exc_info=True)
            sys.exit(1)


if __name__ == '__main__':
    main()
