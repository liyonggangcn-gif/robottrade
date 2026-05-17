#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows服务运行器 - 用于NSSM等工具包装
持续运行并定期执行任务
"""

import sys
import os
import time
import schedule
from datetime import datetime

# 添加项目根目录到路径
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

# 强制清除代理
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

# 设置Windows控制台UTF-8编码
if sys.platform == 'win32':
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except:
        pass

from src.utils.log_utils import init_logger

logger = init_logger("service_runner")


def run_daily_alpha():
    """执行每日数据同步和选股"""
    try:
        logger.info("=" * 60)
        logger.info("执行每日数据同步和选股任务")
        logger.info("=" * 60)
        
        from scripts.daily_alpha_run import run_pipeline
        import argparse
        args = argparse.Namespace(
            skip_sync=False,
            skip_qlib=True,
            skip_concepts=False,
            top_k=20,
            skip_notification=False
        )
        success = run_pipeline(args)
        logger.info(f"每日数据同步完成: {'成功' if success else '失败'}")
    except Exception as e:
        logger.error(f"每日数据同步失败: {e}", exc_info=True)


def run_morning_push():
    """执行早盘推送"""
    try:
        logger.info("=" * 60)
        logger.info("执行早盘推送任务")
        logger.info("=" * 60)
        
        from scripts.morning_push import send_morning_push
        success = send_morning_push()
        logger.info(f"早盘推送完成: {'成功' if success else '失败'}")
    except Exception as e:
        logger.error(f"早盘推送失败: {e}", exc_info=True)


def run_evening_push():
    """执行收盘推送"""
    try:
        logger.info("=" * 60)
        logger.info("执行收盘推送任务")
        logger.info("=" * 60)
        
        from scripts.evening_push import send_evening_push
        success = send_evening_push()
        logger.info(f"收盘推送完成: {'成功' if success else '失败'}")
    except Exception as e:
        logger.error(f"收盘推送失败: {e}", exc_info=True)


def main():
    """主循环"""
    logger.info("=" * 60)
    logger.info("量化选股系统服务运行器已启动")
    logger.info("=" * 60)
    
    # 设置定时任务
    schedule.every().day.at("08:00").do(run_daily_alpha)
    schedule.every().day.at("08:30").do(run_morning_push)
    schedule.every().day.at("16:00").do(run_evening_push)
    
    logger.info("定时任务已设置:")
    logger.info("  - 08:00: 数据同步和选股")
    logger.info("  - 08:30: 早盘推送")
    logger.info("  - 16:00: 收盘推送")
    
    # 主循环
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次
        except KeyboardInterrupt:
            logger.info("服务运行器收到停止信号")
            break
        except Exception as e:
            logger.error(f"服务运行器错误: {e}", exc_info=True)
            time.sleep(60)


if __name__ == '__main__':
    main()
