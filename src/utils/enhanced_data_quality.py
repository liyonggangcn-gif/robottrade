#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
增强数据质量检查器
检查新增数据源的数据质量
"""

import pandas as pd
from datetime import datetime, timedelta
from src.utils.db_utils import DBUtils
from src.utils.log_utils import init_logger

logger = init_logger("enhanced_data_quality")


class EnhancedDataQualityChecker:
    """增强数据质量检查器"""
    
    def __init__(self):
        """初始化增强数据质量检查器"""
        self.issues = []
        self.warnings = []
    
    def check_all(self) -> dict:
        """检查所有增强数据源的数据质量"""
        print("=" * 80)
        print("增强数据源质量检查报告")
        print("=" * 80)
        
        results = {}
        
        # 1. 检查资金流向数据
        results['money_flow'] = self.check_money_flow()
        
        # 2. 检查龙虎榜数据
        results['dragon_tiger'] = self.check_dragon_tiger()
        
        # 3. 检查宏观经济数据
        results['macro_economic'] = self.check_macro_economic()
        
        # 4. 检查概念股票数据
        results['concept_stocks'] = self.check_concept_stocks()
        
        # 5. 检查财务摘要数据
        results['financial_summary'] = self.check_financial_summary()
        
        # 6. 检查数据时效性
        results['timeliness'] = self.check_timeliness()
        
        return results
    
    def check_money_flow(self) -> dict:
        """检查资金流向数据质量"""
        print("\n[1/6] 检查资金流向数据...")
        issues = []
        
        try:
            # 检查表是否存在
            try:
                df = DBUtils.query_df('SELECT COUNT(*) as cnt FROM money_flow')
                count = df.iloc[0]['cnt']
                print(f"  总记录数: {count}")
                
                if count == 0:
                    issues.append("资金流向表为空")
                    return {'status': 'WARN', 'issues': issues, 'count': 0}
                
                # 检查最新数据日期
                df_latest = DBUtils.query_df('''
                    SELECT MAX(trade_date) as latest_date, COUNT(*) as cnt 
                    FROM money_flow
                ''')
                if not df_latest.empty:
                    latest_date = df_latest.iloc[0]['latest_date']
                    latest_count = df_latest.iloc[0]['cnt']
                    
                    # 检查是否为今日数据
                    today = datetime.now().strftime('%Y%m%d')
                    if latest_date != today:
                        issues.append(f"最新数据日期为 {latest_date}，非今日数据")
                    
                    print(f"  最新数据日期: {latest_date} ({latest_count} 条)")
                
                # 检查缺失值
                df_null = DBUtils.query_df('''
                    SELECT 
                        COUNT(*) - COUNT(code) as null_code,
                        COUNT(*) - COUNT(net_amount) as null_net_amount
                    FROM money_flow
                ''')
                if not df_null.empty:
                    null_code = df_null.iloc[0]['null_code']
                    null_net_amount = df_null.iloc[0]['null_net_amount']
                    
                    if null_code > 0:
                        issues.append(f"发现 {null_code} 条记录的code字段为空")
                    if null_net_amount > 0:
                        issues.append(f"发现 {null_net_amount} 条记录的net_amount字段为空")
                
                # 检查异常值
                df_abnormal = DBUtils.query_df('''
                    SELECT COUNT(*) as cnt FROM money_flow
                    WHERE ABS(net_amount) > 10000000000
                ''')
                if not df_abnormal.empty and df_abnormal.iloc[0]['cnt'] > 0:
                    issues.append("发现异常大的资金流向数值（可能超过1000亿）")
                
            except Exception as e:
                if "no such table" in str(e).lower():
                    issues.append("资金流向表不存在")
                else:
                    issues.append(f"检查资金流向数据时出错: {e}")
        
        except Exception as e:
            issues.append(f"检查资金流向数据失败: {e}")
        
        status = 'OK' if not issues else 'ERROR'
        if issues:
            print(f"  ❌ 发现问题: {len(issues)} 个")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("  ✅ 数据质量良好")
        
        return {'status': status, 'issues': issues, 'count': count if 'count' in locals() else 0}
    
    def check_dragon_tiger(self) -> dict:
        """检查龙虎榜数据质量"""
        print("\n[2/6] 检查龙虎榜数据...")
        issues = []
        
        try:
            try:
                df = DBUtils.query_df('SELECT COUNT(*) as cnt FROM dragon_tiger')
                count = df.iloc[0]['cnt']
                print(f"  总记录数: {count}")
                
                if count == 0:
                    issues.append("龙虎榜表为空（可能是非交易日或无龙虎榜数据）")
                    return {'status': 'WARN', 'issues': issues, 'count': 0}
                
                # 检查最新数据日期
                df_latest = DBUtils.query_df('''
                    SELECT MAX(trade_date) as latest_date, COUNT(*) as cnt 
                    FROM dragon_tiger
                ''')
                if not df_latest.empty:
                    latest_date = df_latest.iloc[0]['latest_date']
                    latest_count = df_latest.iloc[0]['cnt']
                    
                    # 检查是否为今日数据
                    today = datetime.now().strftime('%Y%m%d')
                    if latest_date != today:
                        issues.append(f"最新数据日期为 {latest_date}，非今日数据")
                    
                    print(f"  最新数据日期: {latest_date} ({latest_count} 条)")
                
                # 检查缺失值
                df_null = DBUtils.query_df('''
                    SELECT 
                        COUNT(*) - COUNT(code) as null_code,
                        COUNT(*) - COUNT(net_amount) as null_net_amount
                    FROM dragon_tiger
                ''')
                if not df_null.empty:
                    null_code = df_null.iloc[0]['null_code']
                    null_net_amount = df_null.iloc[0]['null_net_amount']
                    
                    if null_code > 0:
                        issues.append(f"发现 {null_code} 条记录的code字段为空")
                    if null_net_amount > 0:
                        issues.append(f"发现 {null_net_amount} 条记录的net_amount字段为空")
                
            except Exception as e:
                if "no such table" in str(e).lower():
                    issues.append("龙虎榜表不存在")
                else:
                    issues.append(f"检查龙虎榜数据时出错: {e}")
        
        except Exception as e:
            issues.append(f"检查龙虎榜数据失败: {e}")
        
        status = 'OK' if not issues else ('WARN' if '非交易日' in str(issues) else 'ERROR')
        if issues:
            print(f"  {'⚠️' if status == 'WARN' else '❌'} 发现问题: {len(issues)} 个")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("  ✅ 数据质量良好")
        
        return {'status': status, 'issues': issues, 'count': count if 'count' in locals() else 0}
    
    def check_macro_economic(self) -> dict:
        """检查宏观经济数据质量"""
        print("\n[3/6] 检查宏观经济数据...")
        issues = []
        
        try:
            try:
                df = DBUtils.query_df('SELECT COUNT(*) as cnt FROM macro_economic')
                count = df.iloc[0]['cnt']
                print(f"  总记录数: {count}")
                
                if count == 0:
                    issues.append("宏观经济表为空")
                    return {'status': 'ERROR', 'issues': issues, 'count': 0}
                
                # 检查各指标数据
                df_indicators = DBUtils.query_df('''
                    SELECT indicator, COUNT(*) as cnt, MAX(period) as latest_period
                    FROM macro_economic
                    GROUP BY indicator
                ''')
                
                if not df_indicators.empty:
                    print("  各指标数据:")
                    for _, row in df_indicators.iterrows():
                        indicator = row['indicator']
                        cnt = row['cnt']
                        latest = row['latest_period']
                        print(f"    {indicator}: {cnt} 条记录，最新: {latest}")
                        
                        if cnt == 0:
                            issues.append(f"{indicator} 指标无数据")
                
                # 检查数据时效性（检查最新数据是否在6个月内）
                df_recent = DBUtils.query_df('''
                    SELECT indicator, MAX(period) as latest_period
                    FROM macro_economic
                    GROUP BY indicator
                ''')
                
                for _, row in df_recent.iterrows():
                    latest_period = row['latest_period']
                    if latest_period:
                        # 尝试解析日期
                        try:
                            if len(latest_period) >= 4:
                                year = int(latest_period[:4])
                                if year < datetime.now().year - 1:
                                    issues.append(f"{row['indicator']} 数据过旧（最新: {latest_period}）")
                        except:
                            pass
                
            except Exception as e:
                if "no such table" in str(e).lower():
                    issues.append("宏观经济表不存在")
                else:
                    issues.append(f"检查宏观经济数据时出错: {e}")
        
        except Exception as e:
            issues.append(f"检查宏观经济数据失败: {e}")
        
        status = 'OK' if not issues else 'ERROR'
        if issues:
            print(f"  ❌ 发现问题: {len(issues)} 个")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("  ✅ 数据质量良好")
        
        return {'status': status, 'issues': issues, 'count': count if 'count' in locals() else 0}
    
    def check_concept_stocks(self) -> dict:
        """检查概念股票数据质量"""
        print("\n[4/6] 检查概念股票数据...")
        issues = []
        
        try:
            try:
                df = DBUtils.query_df('SELECT COUNT(*) as cnt FROM concept_stocks')
                count = df.iloc[0]['cnt']
                print(f"  总记录数: {count}")
                
                if count == 0:
                    issues.append("概念股票表为空")
                    return {'status': 'WARN', 'issues': issues, 'count': 0}
                
                # 检查各概念数据
                df_concepts = DBUtils.query_df('''
                    SELECT concept_name, COUNT(*) as cnt
                    FROM concept_stocks
                    GROUP BY concept_name
                ''')
                
                if not df_concepts.empty:
                    print("  各概念股票数:")
                    for _, row in df_concepts.iterrows():
                        concept = row['concept_name']
                        cnt = row['cnt']
                        print(f"    {concept}: {cnt} 只股票")
                        
                        if cnt == 0:
                            issues.append(f"{concept} 概念无股票")
                
                # 检查缺失值
                df_null = DBUtils.query_df('''
                    SELECT 
                        COUNT(*) - COUNT(ts_code) as null_code,
                        COUNT(*) - COUNT(concept_name) as null_concept
                    FROM concept_stocks
                ''')
                if not df_null.empty:
                    null_code = df_null.iloc[0]['null_code']
                    null_concept = df_null.iloc[0]['null_concept']
                    
                    if null_code > 0:
                        issues.append(f"发现 {null_code} 条记录的ts_code字段为空")
                    if null_concept > 0:
                        issues.append(f"发现 {null_concept} 条记录的concept_name字段为空")
                
            except Exception as e:
                if "no such table" in str(e).lower():
                    issues.append("概念股票表不存在")
                else:
                    issues.append(f"检查概念股票数据时出错: {e}")
        
        except Exception as e:
            issues.append(f"检查概念股票数据失败: {e}")
        
        status = 'OK' if not issues else 'WARN'
        if issues:
            print(f"  ⚠️ 发现问题: {len(issues)} 个")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("  ✅ 数据质量良好")
        
        return {'status': status, 'issues': issues, 'count': count if 'count' in locals() else 0}
    
    def check_financial_summary(self) -> dict:
        """检查财务摘要数据质量"""
        print("\n[5/6] 检查财务摘要数据...")
        issues = []
        
        try:
            try:
                df = DBUtils.query_df('SELECT COUNT(*) as cnt FROM financial_summary')
                count = df.iloc[0]['cnt']
                print(f"  总记录数: {count}")
                
                if count == 0:
                    issues.append("财务摘要表为空")
                    return {'status': 'WARN', 'issues': issues, 'count': 0}
                
                # 检查缺失值
                df_null = DBUtils.query_df('''
                    SELECT 
                        COUNT(*) - COUNT(ts_code) as null_code,
                        COUNT(*) - COUNT(roe) as null_roe,
                        COUNT(*) - COUNT(update_date) as null_date
                    FROM financial_summary
                ''')
                if not df_null.empty:
                    null_code = df_null.iloc[0]['null_code']
                    null_roe = df_null.iloc[0]['null_roe']
                    null_date = df_null.iloc[0]['null_date']
                    
                    if null_code > 0:
                        issues.append(f"发现 {null_code} 条记录的ts_code字段为空")
                    if null_roe > count * 0.5:  # 如果超过50%的ROE为空
                        issues.append(f"发现 {null_roe} 条记录的ROE字段为空（超过50%）")
                    if null_date > 0:
                        issues.append(f"发现 {null_date} 条记录的update_date字段为空")
                
                # 检查异常值
                df_abnormal = DBUtils.query_df('''
                    SELECT COUNT(*) as cnt FROM financial_summary
                    WHERE roe > 100 OR roe < -100 OR 
                          net_profit_margin > 100 OR net_profit_margin < -100
                ''')
                if not df_abnormal.empty and df_abnormal.iloc[0]['cnt'] > 0:
                    issues.append("发现异常的财务指标数值（ROE或净利率超出合理范围）")
                
            except Exception as e:
                if "no such table" in str(e).lower():
                    issues.append("财务摘要表不存在")
                else:
                    issues.append(f"检查财务摘要数据时出错: {e}")
        
        except Exception as e:
            issues.append(f"检查财务摘要数据失败: {e}")
        
        status = 'OK' if not issues else 'WARN'
        if issues:
            print(f"  ⚠️ 发现问题: {len(issues)} 个")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("  ✅ 数据质量良好")
        
        return {'status': status, 'issues': issues, 'count': count if 'count' in locals() else 0}
    
    def check_timeliness(self) -> dict:
        """检查数据时效性"""
        print("\n[6/6] 检查数据时效性...")
        issues = []
        
        try:
            today = datetime.now().strftime('%Y%m%d')
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
            
            # 检查各表的最后更新时间
            tables = {
                'money_flow': 'trade_date',
                'dragon_tiger': 'trade_date',
                'financial_summary': 'update_date'
            }
            
            for table, date_col in tables.items():
                try:
                    df = DBUtils.query_df(f'''
                        SELECT MAX({date_col}) as latest_date, COUNT(*) as cnt
                        FROM {table}
                    ''')
                    
                    if not df.empty and df.iloc[0]['cnt'] > 0:
                        latest_date = df.iloc[0]['latest_date']
                        if latest_date:
                            # 检查是否为最近的数据
                            if latest_date < yesterday:
                                days_old = (datetime.now() - datetime.strptime(latest_date, '%Y%m%d')).days
                                issues.append(f"{table} 数据过旧（最新: {latest_date}，已过 {days_old} 天）")
                except Exception as e:
                    if "no such table" not in str(e).lower():
                        issues.append(f"检查{table}时效性失败: {e}")
        
        except Exception as e:
            issues.append(f"检查数据时效性失败: {e}")
        
        status = 'OK' if not issues else 'WARN'
        if issues:
            print(f"  ⚠️ 发现问题: {len(issues)} 个")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("  ✅ 数据时效性良好")
        
        return {'status': status, 'issues': issues}
    
    def print_summary(self, results: dict):
        """打印检查摘要"""
        print("\n" + "=" * 80)
        print("检查摘要")
        print("=" * 80)
        
        total_issues = 0
        for name, result in results.items():
            if 'issues' in result:
                issues = result['issues']
                total_issues += len(issues)
                status_icon = {
                    'OK': '✅',
                    'WARN': '⚠️',
                    'ERROR': '❌'
                }.get(result.get('status', 'OK'), '❓')
                print(f"{name}: {status_icon} {result.get('status', 'UNKNOWN')} ({len(issues)} 个问题)")
        
        print(f"\n总计: {total_issues} 个问题")
        
        if total_issues == 0:
            print("🎉 所有数据源质量良好，未发现问题！")
        else:
            print("⚠️  发现数据质量问题，建议检查并修复。")


if __name__ == '__main__':
    checker = EnhancedDataQualityChecker()
    results = checker.check_all()
    checker.print_summary(results)
