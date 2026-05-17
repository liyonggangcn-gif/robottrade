#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务监控脚本 - 持续监控服务状态，确保任务按时执行
可以作为Windows服务运行，也可以作为独立进程运行
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

logger = init_logger("service_monitor")


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
        return success
    except Exception as e:
        logger.error(f"每日数据同步失败: {e}", exc_info=True)
        return False


def run_morning_push():
    """执行早盘推送"""
    try:
        logger.info("=" * 60)
        logger.info("执行早盘推送任务")
        logger.info("=" * 60)
        
        from scripts.morning_push import send_morning_push
        success = send_morning_push()
        logger.info(f"早盘推送完成: {'成功' if success else '失败'}")
        return success
    except Exception as e:
        logger.error(f"早盘推送失败: {e}", exc_info=True)
        return False


def run_evening_push():
    """执行收盘推送"""
    try:
        logger.info("=" * 60)
        logger.info("执行收盘推送任务")
        logger.info("=" * 60)
        
        from scripts.evening_push import send_evening_push
        success = send_evening_push()
        logger.info(f"收盘推送完成: {'成功' if success else '失败'}")
        return success
    except Exception as e:
        logger.error(f"收盘推送失败: {e}", exc_info=True)
        return False


def check_health():
    """健康检查：每分钟执行一次"""
    try:
        # 检查数据库连接
        from src.utils.db_utils import DBUtils
        DBUtils.query_df("SELECT 1")
        
        # 检查最新数据日期
        result = DBUtils.query_df("SELECT MAX(trade_date) as max_date FROM stock_daily")
        if not result.empty and result.iloc[0]['max_date']:
            latest_date = result.iloc[0]['max_date']
            logger.debug(f"数据库最新交易日: {latest_date}")
        
        return True
    except Exception as e:
        logger.warning(f"健康检查失败: {e}")
        return False


def main():
    """主循环"""
    logger.info("=" * 80)
    logger.info("量化选股系统服务监控器已启动")
    logger.info(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 80)
    
    # 设置定时任务
    schedule.every().day.at("08:00").do(run_daily_alpha)
    schedule.every().day.at("08:30").do(run_morning_push)
    schedule.every().day.at("16:00").do(run_evening_push)
    
    # 每分钟执行健康检查
    schedule.every(1).minutes.do(check_health)
    
    logger.info("定时任务已设置:")
    logger.info("  - 08:00: 数据同步和选股")
    logger.info("  - 08:30: 早盘推送")
    logger.info("  - 16:00: 收盘推送")
    logger.info("  - 每分钟: 健康检查")
    logger.info("")
    logger.info("服务监控器运行中，按 Ctrl+C 停止...")
    
    # 主循环
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次
    except KeyboardInterrupt:
        logger.info("服务监控器收到停止信号")
    except Exception as e:
        logger.error(f"服务监控器错误: {e}", exc_info=True)
        time.sleep(60)


if __name__ == '__main__':
    main()
