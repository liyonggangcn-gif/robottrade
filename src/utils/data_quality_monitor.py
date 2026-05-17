#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据质量监控模块
在策略执行前自动检查数据质量
支持MySQL + ClickHouse双数据源检查和自动修复
"""

import pandas as pd
from datetime import datetime, timedelta
import subprocess
import os
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config

class DataQualityMonitor:
    """数据质量监控器"""
    
    def __init__(self):
        """初始化数据质量监控器"""
        self._ch_client = None
        self._use_clickhouse = Config.get('use_clickhouse', False)
    
    def _get_ch_client(self):
        """获取ClickHouse客户端"""
        if self._ch_client is not None:
            return self._ch_client
        
        try:
            import clickhouse_connect
            self._ch_client = clickhouse_connect.get_client(
                host='192.168.3.51', port=8123,
                username='default', password='clickhouse123'
            )
            return self._ch_client
        except Exception as e:
            print(f"[DataQualityMonitor] ClickHouse连接失败: {e}")
            return None
    
    def check_clickhouse_data(self):
        """检查ClickHouse数据的最新日期"""
        try:
            ch = self._get_ch_client()
            if not ch:
                return None
            
            r = ch.query("SELECT MAX(trade_date) as max_date FROM stock_daily")
            if r.result_rows:
                ch_date = str(r.result_rows[0][0])
                return ch_date
            return None
        except Exception as e:
            print(f"[DataQualityMonitor] ClickHouse检查失败: {e}")
            return None
    
    def check_data_lag(self):
        """检查MySQL和ClickHouse的数据延迟"""
        mysql_date = self.get_latest_trade_date()
        ch_date = self.check_clickhouse_data() if self._use_clickhouse else None
        
        result = {
            'mysql_date': mysql_date,
            'clickhouse_date': ch_date,
            'mysql_ok': mysql_date is not None,
            'clickhouse_ok': ch_date == mysql_date if ch_date and mysql_date else False,
            'lag_days': 0
        }
        
        if mysql_date and ch_date:
            # 计算延迟天数
            mysql_dt = pd.Timestamp(mysql_date)
            ch_dt = pd.Timestamp(ch_date)
            result['lag_days'] = (mysql_dt - ch_dt).days
        
        return result
    
    def auto_sync_clickhouse(self):
        """自动同步ClickHouse（当数据落后时）"""
        lag_info = self.check_data_lag()
        
        if lag_info['clickhouse_ok']:
            print("[DataQualityMonitor] ClickHouse数据已是最新，无需同步")
            return {'synced': False, 'reason': 'already_up_to_date'}
        
        if lag_info['lag_days'] > 3:
            print(f"[DataQualityMonitor] ClickHouse数据落后{lag_info['lag_days']}天，触发同步...")
            try:
                # 调用sync脚本
                script_dir = os.path.dirname(os.path.abspath(__file__))
                project_root = os.path.dirname(os.path.dirname(script_dir))
                result = subprocess.run(
                    ['python3', 'scripts/sync_ch_fast.py'],
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                if result.returncode == 0:
                    print("[DataQualityMonitor] ClickHouse同步成功")
                    return {'synced': True, 'success': True}
                else:
                    print(f"[DataQualityMonitor] ClickHouse同步失败: {result.stderr[:200]}")
                    return {'synced': True, 'success': False, 'error': result.stderr[:200]}
            except Exception as e:
                print(f"[DataQualityMonitor] ClickHouse同步异常: {e}")
                return {'synced': True, 'success': False, 'error': str(e)}
        
        return {'synced': False, 'reason': f'lag={lag_info["lag_days"]}days (<3天阈值)'}
    
    def get_latest_trade_date(self):
        """获取最新交易日（MySQL）"""
        try:
            result = DBUtils.query_df('SELECT MAX(trade_date) as max_date FROM stock_daily')
            if result.empty or pd.isna(result.iloc[0]['max_date']):
                return None
            latest_date = result.iloc[0]['max_date']
            if isinstance(latest_date, pd.Timestamp):
                return latest_date.strftime('%Y-%m-%d')
            return str(latest_date)
        except Exception as e:
            print(f"Error getting latest trade date: {e}")
            return None
    
    def check_date_data_quality(self, date):
        """检查指定日期的数据质量
        
        Args:
            date: 日期，格式为'YYYY-MM-DD'
            
        Returns:
            dict: 数据质量报告
        """
        try:
            formatted_date = pd.Timestamp(date).strftime('%Y-%m-%d')
            
            # 查询数据质量指标
            query = f"""
            SELECT 
                COUNT(*) as total_stocks,
                SUM(CASE WHEN total_mv IS NOT NULL AND total_mv > 0 THEN 1 ELSE 0 END) as stocks_with_mv,
                SUM(CASE WHEN pe_ttm IS NOT NULL AND pe_ttm > 0 THEN 1 ELSE 0 END) as stocks_with_pe,
                SUM(CASE WHEN total_mv > 500000000 AND pe_ttm IS NOT NULL AND pe_ttm > 0 THEN 1 ELSE 0 END) as eligible_stocks,
                AVG(total_mv) as avg_mv,
                MIN(total_mv) as min_mv,
                MAX(total_mv) as max_mv
            FROM stock_daily
            WHERE trade_date = '{formatted_date}'
            """
            
            result = DBUtils.query_df(query)
            
            if result.empty:
                return {
                    'date': date,
                    'has_data': False,
                    'total_stocks': 0,
                    'quality_score': 0.0,
                    'issues': ['No data found for this date']
                }
            
            row = result.iloc[0]
            total = row['total_stocks']
            
            if total == 0:
                return {
                    'date': date,
                    'has_data': False,
                    'total_stocks': 0,
                    'quality_score': 0.0,
                    'issues': ['No stocks found for this date']
                }
            
            # 计算质量指标
            mv_coverage = row['stocks_with_mv'] / total if total > 0 else 0
            pe_coverage = row['stocks_with_pe'] / total if total > 0 else 0
            eligible_ratio = row['eligible_stocks'] / total if total > 0 else 0
            
            # 计算质量分数 (0-100)
            quality_score = (
                (mv_coverage * 0.3 + pe_coverage * 0.3 + eligible_ratio * 0.4) * 100
            )
            
            # 识别问题
            issues = []
            if total < 100:
                issues.append(f'Low stock count: {total} (expected >= 100)')
            if mv_coverage < 0.5:
                issues.append(f'Low market cap coverage: {mv_coverage*100:.1f}% (expected >= 50%)')
            if pe_coverage < 0.5:
                issues.append(f'Low PE coverage: {pe_coverage*100:.1f}% (expected >= 50%)')
            if eligible_ratio < 0.1:
                issues.append(f'Low eligible stocks ratio: {eligible_ratio*100:.1f}% (expected >= 10%)')
            
            return {
                'date': date,
                'has_data': True,
                'total_stocks': int(total),
                'stocks_with_mv': int(row['stocks_with_mv']),
                'stocks_with_pe': int(row['stocks_with_pe']),
                'eligible_stocks': int(row['eligible_stocks']),
                'mv_coverage': mv_coverage,
                'pe_coverage': pe_coverage,
                'eligible_ratio': eligible_ratio,
                'avg_mv': row['avg_mv'],
                'min_mv': row['min_mv'],
                'max_mv': row['max_mv'],
                'quality_score': quality_score,
                'issues': issues,
                'is_acceptable': len(issues) == 0 and quality_score >= 60
            }
        except Exception as e:
            return {
                'date': date,
                'has_data': False,
                'total_stocks': 0,
                'quality_score': 0.0,
                'issues': [f'Error checking data quality: {e}']
            }
    
    def get_latest_trade_date(self):
        """获取最新交易日"""
        try:
            result = DBUtils.query_df('SELECT MAX(trade_date) as max_date FROM stock_daily')
            if result.empty or pd.isna(result.iloc[0]['max_date']):
                return None
            latest_date = result.iloc[0]['max_date']
            if isinstance(latest_date, pd.Timestamp):
                return latest_date.strftime('%Y-%m-%d')
            return str(latest_date)
        except Exception as e:
            print(f"Error getting latest trade date: {e}")
            return None
    
    def check_latest_data_quality(self, auto_fix=True):
        """检查最新交易日的数据质量（包含ClickHouse）
        
        Args:
            auto_fix: 是否自动修复（同步ClickHouse）
        """
        # 1. 先检查数据延迟
        lag_info = self.check_data_lag()
        print(f"[DataQualityMonitor] MySQL最新: {lag_info['mysql_date']}, ClickHouse最新: {lag_info['clickhouse_date']}, 延迟: {lag_info['lag_days']}天")
        
        # 2. 如果需要自动修复
        if auto_fix and lag_info['lag_days'] > 0:
            sync_result = self.auto_sync_clickhouse()
            if sync_result.get('synced'):
                # 重新检查延迟
                lag_info = self.check_data_lag()
                print(f"[DataQualityMonitor] 同步后延迟: {lag_info['lag_days']}天")
        
        # 3. 检查MySQL数据质量
        latest_date = self.get_latest_trade_date()
        if not latest_date:
            return {'has_data': False, 'issues': ['MySQL无数据']}
        
        report = self.check_date_data_quality(latest_date)
        
        # 4. 添加ClickHouse信息到报告
        report['clickhouse_date'] = lag_info['clickhouse_date']
        report['data_lag_days'] = lag_info['lag_days']
        
        if lag_info['lag_days'] > 0:
            report['issues'].append(f'ClickHouse数据落后{lag_info["lag_days"]}天')
            report['is_acceptable'] = False
            report['quality_score'] = max(0, report['quality_score'] - lag_info['lag_days'] * 10)
        
        return report
    
    def print_quality_report(self, report):
        """打印数据质量报告"""
        if not report:
            print("[ERROR] No quality report available")
            return
        
        print("\n" + "="*80)
        print("  数据质量报告")
        print("="*80)
        print(f"日期: {report['date']}")
        print(f"是否有数据: {'是' if report['has_data'] else '否'}")
        
        if not report['has_data']:
            print("问题:")
            for issue in report['issues']:
                print(f"  - {issue}")
            return
        
        print(f"\n数据统计:")
        print(f"  股票总数: {report['total_stocks']}")
        print(f"  有市值数据的股票: {report['stocks_with_mv']} ({report['mv_coverage']*100:.1f}%)")
        print(f"  有PE数据的股票: {report['stocks_with_pe']} ({report['pe_coverage']*100:.1f}%)")
        print(f"  符合策略条件的股票: {report['eligible_stocks']} ({report['eligible_ratio']*100:.1f}%)")
        
        if report.get('avg_mv') and pd.notna(report['avg_mv']):
            print(f"\n市值分布:")
            print(f"  平均市值: {report['avg_mv']/100000000:.2f} 亿")
            if report.get('min_mv') and pd.notna(report['min_mv']):
                print(f"  最小市值: {report['min_mv']/100000000:.2f} 亿")
            if report.get('max_mv') and pd.notna(report['max_mv']):
                print(f"  最大市值: {report['max_mv']/100000000:.2f} 亿")
        
        print(f"\n质量评分: {report['quality_score']:.1f}/100")
        print(f"数据质量: {'[OK] 可接受' if report['is_acceptable'] else '[WARN] 存在问题'}")
        
        if report['issues']:
            print("\n问题列表:")
            for issue in report['issues']:
                print(f"  - {issue}")
        
        print("="*80 + "\n")
