#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据质量稽核脚本

用于定期检查数据库中的数据完整性和异常情况
包括：
1. 数据完整性检查
2. 异常值检测
3. 数据一致性验证
4. 数据更新状态检查
"""

import pandas as pd
from datetime import datetime, timedelta
from src.utils.db_utils import DBUtils

class DataQualityAudit:
    """数据质量稽核类"""
    
    def __init__(self):
        """初始化数据质量稽核"""
        print("初始化数据质量稽核...")
    
    def check_data_integrity(self):
        """检查数据完整性"""
        print("\n" + "="*80)
        print("🔍 数据完整性检查")
        print("="*80)
        
        checks = {
            "stock_info表记录数": "SELECT COUNT(*) FROM stock_info",
            "stock_daily表记录数": "SELECT COUNT(*) FROM stock_daily",
            "stock_factors表记录数": "SELECT COUNT(*) FROM stock_factors",
            "stock_daily唯一股票数": "SELECT COUNT(DISTINCT ts_code) FROM stock_daily",
            "stock_factors唯一股票数": "SELECT COUNT(DISTINCT ts_code) FROM stock_factors",
            "stock_info表非空名称数": "SELECT COUNT(*) FROM stock_info WHERE name IS NOT NULL",
            "stock_info表有效PE数": "SELECT COUNT(*) FROM stock_info WHERE pe_ttm > 0",
            "stock_info表有效市值数": "SELECT COUNT(*) FROM stock_info WHERE total_mv > 0",
        }
        
        results = {}
        for check_name, query in checks.items():
            try:
                df = DBUtils.query_df(query)
                results[check_name] = df.iloc[0, 0] if not df.empty else 0
                print(f"✅ {check_name}: {results[check_name]}")
            except Exception as e:
                results[check_name] = f"错误: {e}"
                print(f"❌ {check_name}: 错误 - {e}")
        
        return results
    
    def detect_anomalies(self):
        """检测异常值"""
        print("\n" + "="*80)
        print("🚨 异常值检测")
        print("="*80)
        
        anomalies = {
            "stock_info表PE异常值": "SELECT COUNT(*) FROM stock_info WHERE pe_ttm < 0 OR pe_ttm > 1000",
            "stock_info表市值异常值": "SELECT COUNT(*) FROM stock_info WHERE total_mv > 1000000000000",  # 超过1万亿
            "stock_daily表价格异常值": "SELECT COUNT(*) FROM stock_daily WHERE close < 0.01 OR close > 10000",
        }
        
        results = {}
        for anomaly_name, query in anomalies.items():
            try:
                df = DBUtils.query_df(query)
                count = df.iloc[0, 0] if not df.empty else 0
                results[anomaly_name] = count
                if count > 0:
                    print(f"⚠️  {anomaly_name}: {count} 条异常")
                else:
                    print(f"✅ {anomaly_name}: 无异常")
            except Exception as e:
                results[anomaly_name] = f"错误: {e}"
                print(f"❌ {anomaly_name}: 错误 - {e}")
        
        return results
    
    def verify_data_consistency(self):
        """验证数据一致性"""
        print("\n" + "="*80)
        print("🔄 数据一致性验证")
        print("="*80)
        
        consistency_checks = {
            "stock_info与stock_factors股票代码匹配度": "SELECT COUNT(DISTINCT sf.ts_code) FROM stock_factors sf JOIN stock_info si ON sf.ts_code = si.ts_code",
            "stock_daily与stock_factors日期匹配度": "SELECT COUNT(DISTINCT sd.trade_date) FROM stock_daily sd JOIN stock_factors sf ON sd.trade_date = sf.trade_date AND sd.ts_code = sf.ts_code",
        }
        
        results = {}
        for check_name, query in consistency_checks.items():
            try:
                df = DBUtils.query_df(query)
                results[check_name] = df.iloc[0, 0] if not df.empty else 0
                print(f"✅ {check_name}: {results[check_name]}")
            except Exception as e:
                results[check_name] = f"错误: {e}"
                print(f"❌ {check_name}: 错误 - {e}")
        
        return results
    
    def check_update_status(self):
        """检查数据更新状态"""
        print("\n" + "="*80)
        print("📅 数据更新状态检查")
        print("="*80)
        
        update_checks = {
            "stock_daily最新交易日": "SELECT MAX(trade_date) FROM stock_daily",
            "stock_factors最新交易日": "SELECT MAX(trade_date) FROM stock_factors",
        }
        
        results = {}
        for check_name, query in update_checks.items():
            try:
                df = DBUtils.query_df(query)
                latest_date = df.iloc[0, 0] if not df.empty else None
                results[check_name] = latest_date
                
                if latest_date:
                    # 检查是否为最近7天内的数据
                    latest_date_dt = pd.to_datetime(latest_date)
                    days_diff = (datetime.now() - latest_date_dt).days
                    
                    if days_diff <= 7:
                        print(f"✅ {check_name}: {latest_date} (更新及时)")
                    else:
                        print(f"⚠️  {check_name}: {latest_date} (已过期 {days_diff} 天)")
                else:
                    print(f"❌ {check_name}: 无数据")
            except Exception as e:
                results[check_name] = f"错误: {e}"
                print(f"❌ {check_name}: 错误 - {e}")
        
        return results
    
    def run_full_audit(self):
        """运行完整的稽核"""
        print("\n" + "="*100)
        print("📊 数据质量完整稽核报告")
        print("="*100)
        
        # 运行所有检查
        integrity_results = self.check_data_integrity()
        anomaly_results = self.detect_anomalies()
        consistency_results = self.verify_data_consistency()
        update_results = self.check_update_status()
        
        # 生成摘要
        print("\n" + "="*100)
        print("📋 稽核摘要")
        print("="*100)
        
        # 检查关键指标 - 使用有实际数据的股票数
        total_stocks = integrity_results.get("stock_daily唯一股票数", 0)
        if total_stocks == 0:
            total_stocks = integrity_results.get("stock_factors唯一股票数", 0)
        if total_stocks == 0:
            total_stocks = integrity_results.get("stock_info表记录数", 0)
        
        valid_pe_stocks = integrity_results.get("stock_info表有效PE数", 0)
        valid_mv_stocks = integrity_results.get("stock_info表有效市值数", 0)
        
        pe_coverage = (valid_pe_stocks / total_stocks * 100) if total_stocks > 0 else 0
        mv_coverage = (valid_mv_stocks / total_stocks * 100) if total_stocks > 0 else 0
        
        print(f"stock_info表总股票数: {integrity_results.get('stock_info表记录数', 0)}")
        print(f"stock_daily表唯一股票数: {integrity_results.get('stock_daily唯一股票数', 0)}")
        print(f"stock_factors表唯一股票数: {integrity_results.get('stock_factors唯一股票数', 0)}")
        print(f"有效PE覆盖率: {pe_coverage:.2f}%")
        print(f"有效市值覆盖率: {mv_coverage:.2f}%")
        
        # 检查更新状态
        latest_daily = update_results.get("stock_daily最新交易日", None)
        latest_factors = update_results.get("stock_factors最新交易日", None)
        
        print(f"stock_daily最新数据: {latest_daily}")
        print(f"stock_factors最新数据: {latest_factors}")
        
        # 检查异常值
        total_anomalies = sum(v for v in anomaly_results.values() if isinstance(v, int))
        print(f"检测到的异常值总数: {total_anomalies}")
        
        # 评估数据质量
        if pe_coverage > 80 and mv_coverage > 80 and total_anomalies < 100:
            print("\n✅ 数据质量评估: 良好")
        elif pe_coverage > 50 and mv_coverage > 50 and total_anomalies < 500:
            print("\n⚠️  数据质量评估: 一般")
        else:
            print("\n❌ 数据质量评估: 较差")
        
        # 返回完整结果
        return {
            "integrity": integrity_results,
            "anomalies": anomaly_results,
            "consistency": consistency_results,
            "update_status": update_results,
            "summary": {
                "total_stocks": total_stocks,
                "pe_coverage": pe_coverage,
                "mv_coverage": mv_coverage,
                "total_anomalies": total_anomalies
            }
        }

if __name__ == "__main__":
    # 运行数据质量稽核
    auditor = DataQualityAudit()
    results = auditor.run_full_audit()
    
    print("\n" + "="*100)
    print("✅ 数据质量稽核完成")
    print("="*100)
