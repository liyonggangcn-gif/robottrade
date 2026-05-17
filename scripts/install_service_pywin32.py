#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
使用 pywin32 安装 Windows 服务
需要管理员权限运行
"""

import os
import sys
import win32serviceutil
import win32service
import servicemanager

# 添加项目根目录到路径
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

from scripts.service_runner import ServiceRunner


class QuantAgentService(win32serviceutil.ServiceFramework):
    """量化选股Windows服务"""
    
    _svc_name_ = "QuantAgent选股服务"
    _svc_display_name_ = "量化选股系统服务"
    _svc_description_ = "自动执行数据同步、选股和推送任务"
    
    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32service.CreateEvent(None, 0, 0, None)
        self.runner = None
    
    def SvcStop(self):
        """停止服务"""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.runner:
            self.runner.stop()
        win32service.SetEvent(self.stop_event)
    
    def SvcDoRun(self):
        """运行服务"""
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, '')
        )
        
        try:
            self.runner = ServiceRunner()
            self.runner.run()
        except Exception as e:
            servicemanager.LogErrorMsg(f"服务运行错误: {e}")
        
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, '')
        )


def main():
    """主函数"""
    if len(sys.argv) == 1:
        # 安装服务
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(QuantAgentService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        # 使用 win32serviceutil 处理命令行参数
        win32serviceutil.HandleCommandLine(QuantAgentService)


if __name__ == '__main__':
    main()
