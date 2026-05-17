#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows服务包装器 - 将量化选股系统注册为Windows服务

使用方法:
    安装服务: python scripts/windows_service.py install
    启动服务: python scripts/windows_service.py start
    停止服务: python scripts/windows_service.py stop
    卸载服务: python scripts/windows_service.py remove
    查看状态: python scripts/windows_service.py status
"""

import sys
import os
import time
import logging
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

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False
    print("[WARN] pywin32 not installed. Install with: pip install pywin32")
    print("        Alternatively, use NSSM (Non-Sucking Service Manager)")

from src.utils.log_utils import init_logger

logger = init_logger("windows_service")


class QuantAgentService(win32serviceutil.ServiceFramework):
    """量化选股系统Windows服务"""
    
    _svc_name_ = "QuantAgentStockSelection"
    _svc_display_name_ = "量化选股系统服务"
    _svc_description_ = "自动执行数据同步、选股和推送的量化选股系统"
    
    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.is_alive = True
        
    def SvcStop(self):
        """停止服务"""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        self.is_alive = False
        
    def SvcDoRun(self):
        """运行服务主循环"""
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, '')
        )
        
        logger.info("量化选股系统服务已启动")
        
        # 主循环：定期执行任务
        while self.is_alive:
            try:
                # 检查当前时间，决定执行哪个任务
                now = datetime.now()
                hour = now.hour
                minute = now.minute
                
                # 8:00 - 数据同步和选股
                if hour == 8 and minute == 0:
                    logger.info("执行每日数据同步和选股任务...")
                    self._run_daily_alpha()
                
                # 8:30 - 早盘推送
                elif hour == 8 and minute == 30:
                    logger.info("执行早盘推送任务...")
                    self._run_morning_push()
                
                # 16:00 - 收盘推送
                elif hour == 16 and minute == 0:
                    logger.info("执行收盘推送任务...")
                    self._run_evening_push()
                
                # 等待1分钟后再次检查
                result = win32event.WaitForSingleObject(self.hWaitStop, 60000)  # 60秒
                if result == win32event.WAIT_OBJECT_0:
                    break
                    
            except Exception as e:
                logger.error(f"服务执行错误: {e}", exc_info=True)
                time.sleep(60)  # 出错后等待1分钟再继续
        
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, '')
        )
        logger.info("量化选股系统服务已停止")
    
    def _run_daily_alpha(self):
        """执行每日数据同步和选股"""
        try:
            from scripts.daily_alpha_run import run_pipeline
            import argparse
            args = argparse.Namespace(
                skip_sync=False,
                skip_qlib=True,
                skip_concepts=False,
                top_k=20,
                skip_notification=False
            )
            run_pipeline(args)
        except Exception as e:
            logger.error(f"每日数据同步失败: {e}", exc_info=True)
    
    def _run_morning_push(self):
        """执行早盘推送"""
        try:
            from scripts.morning_push import send_morning_push
            send_morning_push()
        except Exception as e:
            logger.error(f"早盘推送失败: {e}", exc_info=True)
    
    def _run_evening_push(self):
        """执行收盘推送"""
        try:
            from scripts.evening_push import send_evening_push
            send_evening_push()
        except Exception as e:
            logger.error(f"收盘推送失败: {e}", exc_info=True)


def main():
    """主函数"""
    if not WIN32_AVAILABLE:
        print("[ERROR] pywin32 not available. Please install: pip install pywin32")
        print("\nAlternatively, use NSSM to create Windows service:")
        print("  1. Download NSSM from https://nssm.cc/download")
        print("  2. Run: nssm install QuantAgentStockSelection")
        print("  3. Set Application path to: python.exe")
        print("  4. Set Arguments to: scripts/windows_service_runner.py")
        sys.exit(1)
    
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(QuantAgentService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(QuantAgentService)


if __name__ == '__main__':
    main()
