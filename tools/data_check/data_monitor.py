#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据监控脚本

每半小时自动检查数据更新和质量
包括：
1. 定期运行数据同步
2. 执行数据质量稽核
3. 发送通知（如有必要）
"""

import time
import schedule
from datetime import datetime
from sync_tushare_data import TushareDataSync
from data_quality_audit import DataQualityAudit

class DataMonitor:
    """数据监控类"""
    
    def __init__(self):
        """初始化数据监控"""
        self.sync = TushareDataSync()
        self.auditor = DataQualityAudit()
        self.last_sync_time = None
        self.last_audit_time = None
        print("初始化数据监控...")
    
    def sync_data(self):
        """同步数据"""
        print(f"\n" + "="*100)
        print(f"🚀 开始数据同步 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("="*100)
        
        try:
            # 同步股票基本信息
            self.sync.sync_stock_basic()
            
            # 同步stock_info基本面数据
            self.sync.sync_stock_info_fundamental()
            
            # 记录同步时间
            self.last_sync_time = datetime.now()
            print(f"✅ 数据同步完成 - {self.last_sync_time.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            print(f"❌ 数据同步失败: {e}")
    
    def audit_data_quality(self):
        """检查数据质量"""
        print(f"\n" + "="*100)
        print(f"🔍 开始数据质量检查 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("="*100)
        
        try:
            # 运行完整的质量稽核
            results = self.auditor.run_full_audit()
            
            # 记录稽核时间
            self.last_audit_time = datetime.now()
            print(f"✅ 数据质量检查完成 - {self.last_audit_time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # 检查关键指标
            summary = results.get('summary', {})
            pe_coverage = summary.get('pe_coverage', 0)
            mv_coverage = summary.get('mv_coverage', 0)
            total_anomalies = summary.get('total_anomalies', 0)
            
            # 如果数据质量较差，触发警告
            if pe_coverage < 50 or mv_coverage < 50 or total_anomalies > 500:
                print("\n🚨 数据质量警告: 数据质量较差，建议检查数据同步")
                # 这里可以添加通知逻辑
        except Exception as e:
            print(f"❌ 数据质量检查失败: {e}")
    
    def run_monitoring(self):
        """运行监控"""
        print("="*100)
        print("📊 数据监控系统启动")
        print("="*100)
        print("监控任务:")
        print("1. 每30分钟执行数据同步")
        print("2. 每30分钟执行数据质量检查")
        print("="*100)
        
        # 立即执行一次同步和检查
        self.sync_data()
        self.audit_data_quality()
        
        # 每30分钟执行一次同步
        schedule.every(30).minutes.do(self.sync_data)
        
        # 每30分钟执行一次质量检查
        schedule.every(30).minutes.do(self.audit_data_quality)
        
        # 持续运行
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)  # 每分钟检查一次
        except KeyboardInterrupt:
            print("\n" + "="*100)
            print("🛑 数据监控系统停止")
            print("="*100)

if __name__ == "__main__":
    # 运行数据监控
    monitor = DataMonitor()
    monitor.run_monitoring()
