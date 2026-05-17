import pandas as pd
from datetime import datetime
from src.utils.db_utils import DBUtils

class DataQualityChecker:
    """数据质量检查器"""
    
    def __init__(self):
        """初始化数据质量检查器"""
        pass
    
    def check_stock_info_quality(self):
        """检查stock_info表质量
        
        Returns:
            dict: 质量检查结果
        """
        result = {
            'table': 'stock_info',
            'total_records': 0,
            'non_empty_ts_code': 0,
            'non_empty_name': 0,
            'non_empty_market': 0,
            'non_empty_industry': 0,
            'non_empty_pe_ttm': 0,
            'non_empty_pb': 0,
            'non_empty_total_mv': 0,
            'valid_ts_code_format': 0,
            'issues': []
        }
        
        try:
            # 获取所有数据
            df = DBUtils.query_df("SELECT * FROM stock_info")
            result['total_records'] = len(df)
            
            if not df.empty:
                # 检查关键字段非空
                result['non_empty_ts_code'] = df['ts_code'].notna().sum()
                result['non_empty_name'] = df['name'].notna().sum()
                result['non_empty_market'] = df['market'].notna().sum()
                result['non_empty_industry'] = df['industry'].notna().sum()
                result['non_empty_pe_ttm'] = df['pe_ttm'].notna().sum()
                result['non_empty_pb'] = df['pb'].notna().sum()
                result['non_empty_total_mv'] = df['total_mv'].notna().sum()
                
                # 检查ts_code格式
                def is_valid_ts_code(code):
                    if pd.isna(code):
                        return False
                    parts = str(code).split('.')
                    return len(parts) == 2 and parts[0].isdigit() and parts[1] in ['SH', 'SZ', 'BJ']
                
                result['valid_ts_code_format'] = df['ts_code'].apply(is_valid_ts_code).sum()
                
                # 检查异常值
                if 'pe_ttm' in df.columns:
                    pe_outliers = df[(df['pe_ttm'] < 0) | (df['pe_ttm'] > 1000)]
                    if len(pe_outliers) > 0:
                        result['issues'].append(f'PE异常值: {len(pe_outliers)}条记录')
                
                if 'pb' in df.columns:
                    pb_outliers = df[(df['pb'] < 0) | (df['pb'] > 100)]
                    if len(pb_outliers) > 0:
                        result['issues'].append(f'PB异常值: {len(pb_outliers)}条记录')
                
                if 'total_mv' in df.columns:
                    mv_outliers = df[(df['total_mv'] < 0) | (df['total_mv'] > 1e9)]
                    if len(mv_outliers) > 0:
                        result['issues'].append(f'市值异常值: {len(mv_outliers)}条记录')
            
        except Exception as e:
            result['issues'].append(f'检查失败: {str(e)}')
        
        return result
    
    def check_stock_daily_quality(self, date=None):
        """检查stock_daily表质量
        
        Args:
            date: 检查特定日期的数据，默认检查最新交易日
            
        Returns:
            dict: 质量检查结果
        """
        result = {
            'table': 'stock_daily',
            'total_records': 0,
            'non_empty_trade_date': 0,
            'non_empty_ts_code': 0,
            'non_empty_close': 0,
            'non_empty_amount': 0,
            'non_empty_pe_ttm': 0,
            'non_empty_total_mv': 0,
            'valid_date_format': 0,
            'valid_ts_code_format': 0,
            'amount_greater_than_zero': 0,
            'issues': []
        }
        
        try:
            # 确定检查日期
            if date:
                query_date = date
            else:
                # 获取最新交易日
                latest_date_df = DBUtils.query_df("SELECT MAX(trade_date) as latest_date FROM stock_daily")
                if not latest_date_df.empty and pd.notna(latest_date_df.iloc[0]['latest_date']):
                    query_date = latest_date_df.iloc[0]['latest_date']
                else:
                    result['issues'].append('没有找到交易日数据')
                    return result
            
            # 获取指定日期的数据
            df = DBUtils.query_df(f"SELECT * FROM stock_daily WHERE trade_date = '{query_date}'")
            result['total_records'] = len(df)
            result['check_date'] = query_date
            
            if not df.empty:
                # 检查关键字段非空
                result['non_empty_trade_date'] = df['trade_date'].notna().sum()
                result['non_empty_ts_code'] = df['ts_code'].notna().sum()
                result['non_empty_close'] = df['close'].notna().sum()
                result['non_empty_amount'] = df['amount'].notna().sum()
                result['non_empty_pe_ttm'] = df['pe_ttm'].notna().sum()
                result['non_empty_total_mv'] = df['total_mv'].notna().sum()
                
                # 检查日期格式
                def is_valid_date(date_str):
                    if pd.isna(date_str):
                        return False
                    try:
                        datetime.strptime(str(date_str), '%Y-%m-%d')
                        return True
                    except:
                        return False
                
                result['valid_date_format'] = df['trade_date'].apply(is_valid_date).sum()
                
                # 检查ts_code格式
                def is_valid_ts_code(code):
                    if pd.isna(code):
                        return False
                    parts = str(code).split('.')
                    return len(parts) == 2 and parts[0].isdigit() and parts[1] in ['SH', 'SZ', 'BJ']
                
                result['valid_ts_code_format'] = df['ts_code'].apply(is_valid_ts_code).sum()
                
                # 检查成交额大于0
                if 'amount' in df.columns:
                    result['amount_greater_than_zero'] = (df['amount'] > 0).sum()
                    low_amount = df[df['amount'] < 1000000]  # 成交额小于100万
                    if len(low_amount) > len(df) * 0.5:
                        result['issues'].append('超过50%的股票成交额小于100万，可能数据异常')
                
                # 检查市值和PE数据缺失
                pe_missing_rate = 1 - result['non_empty_pe_ttm'] / len(df)
                mv_missing_rate = 1 - result['non_empty_total_mv'] / len(df)
                
                if pe_missing_rate > 0.5:
                    result['issues'].append(f'PE数据缺失率: {pe_missing_rate:.2f}，可能数据异常')
                if mv_missing_rate > 0.5:
                    result['issues'].append(f'市值数据缺失率: {mv_missing_rate:.2f}，可能数据异常')
            else:
                result['issues'].append(f'日期 {query_date} 没有数据')
            
        except Exception as e:
            result['issues'].append(f'检查失败: {str(e)}')
        
        return result
    
    def check_stock_concepts_quality(self):
        """检查stock_concepts表质量
        
        Returns:
            dict: 质量检查结果
        """
        result = {
            'table': 'stock_concepts',
            'total_records': 0,
            'non_empty_ts_code': 0,
            'non_empty_concept_name': 0,
            'valid_ts_code_format': 0,
            'unique_stocks': 0,
            'unique_concepts': 0,
            'issues': []
        }
        
        try:
            # 获取所有数据
            df = DBUtils.query_df("SELECT * FROM stock_concepts")
            result['total_records'] = len(df)
            
            if not df.empty:
                # 检查关键字段非空
                result['non_empty_ts_code'] = df['ts_code'].notna().sum()
                result['non_empty_concept_name'] = df['concept_name'].notna().sum()
                
                # 检查ts_code格式
                def is_valid_ts_code(code):
                    if pd.isna(code):
                        return False
                    parts = str(code).split('.')
                    return len(parts) == 2 and parts[0].isdigit() and parts[1] in ['SH', 'SZ', 'BJ']
                
                result['valid_ts_code_format'] = df['ts_code'].apply(is_valid_ts_code).sum()
                
                # 统计唯一股票和概念
                result['unique_stocks'] = df['ts_code'].nunique()
                result['unique_concepts'] = df['concept_name'].nunique()
                
                # 检查异常值
                if len(df) < 1000:
                    result['issues'].append('概念数据可能不完整')
            
        except Exception as e:
            result['issues'].append(f'检查失败: {str(e)}')
        
        return result
    
    def check_ai_predictions_quality(self, date=None):
        """检查ai_predictions表质量
        
        Args:
            date: 检查特定日期的数据，默认检查最新交易日
            
        Returns:
            dict: 质量检查结果
        """
        result = {
            'table': 'ai_predictions',
            'total_records': 0,
            'non_empty_ts_code': 0,
            'non_empty_ai_score': 0,
            'valid_ts_code_format': 0,
            'valid_ai_score_range': 0,
            'issues': []
        }
        
        try:
            # 获取所有数据
            df = DBUtils.query_df("SELECT * FROM ai_predictions")
            result['total_records'] = len(df)
            
            if not df.empty:
                # 检查关键字段非空
                result['non_empty_ts_code'] = df['ts_code'].notna().sum()
                result['non_empty_ai_score'] = df['ai_score'].notna().sum()
                
                # 检查ts_code格式
                def is_valid_ts_code(code):
                    if pd.isna(code):
                        return False
                    parts = str(code).split('.')
                    return len(parts) == 2 and parts[0].isdigit() and parts[1] in ['SH', 'SZ', 'BJ']
                
                result['valid_ts_code_format'] = df['ts_code'].apply(is_valid_ts_code).sum()
                
                # 检查ai_score范围
                if 'ai_score' in df.columns:
                    result['valid_ai_score_range'] = ((df['ai_score'] >= 0) & (df['ai_score'] <= 100)).sum()
                    score_outliers = df[(df['ai_score'] < 0) | (df['ai_score'] > 100)]
                    if len(score_outliers) > 0:
                        result['issues'].append(f'AI评分异常值: {len(score_outliers)}条记录')
            else:
                result['issues'].append('没有找到预测数据')
            
        except Exception as e:
            result['issues'].append(f'检查失败: {str(e)}')
        
        return result
    
    def run_all_checks(self, date=None):
        """运行所有表的质量检查
        
        Args:
            date: 检查特定日期的数据，默认检查最新交易日
            
        Returns:
            dict: 所有表的质量检查结果
        """
        results = {
            'stock_info': self.check_stock_info_quality(),
            'stock_daily': self.check_stock_daily_quality(date),
            'stock_concepts': self.check_stock_concepts_quality(),
            'ai_predictions': self.check_ai_predictions_quality(date)
        }
        return results
    
    def generate_quality_report(self, date=None):
        """生成质量检查报告
        
        Args:
            date: 检查特定日期的数据，默认检查最新交易日
            
        Returns:
            str: 质量检查报告
        """
        results = self.run_all_checks(date)
        
        report = f"""# 数据质量检查报告

## 检查日期
{date or datetime.now().strftime('%Y-%m-%d')}

"""
        
        for table_name, result in results.items():
            report += f"\n## {table_name} 表质量检查\n"
            report += f"- 总记录数: {result.get('total_records', 0)}"
            
            if 'check_date' in result:
                report += f" (检查日期: {result['check_date']})"
            
            report += "\n"
            
            # 检查完整性
            report += "- 完整性:\n"
            for key, value in result.items():
                if key.startswith('non_empty_'):
                    field_name = key.replace('non_empty_', '')
                    rate = (value / result['total_records'] * 100) if result['total_records'] > 0 else 0
                    report += f"  - {field_name}: {value}/{result['total_records']} ({rate:.1f}%)\n"
            
            # 检查一致性
            report += "- 一致性:\n"
            if 'valid_date_format' in result:
                rate = (result['valid_date_format'] / result['total_records'] * 100) if result['total_records'] > 0 else 0
                report += f"  - 日期格式: {result['valid_date_format']}/{result['total_records']} ({rate:.1f}%)\n"
            if 'valid_ts_code_format' in result:
                rate = (result['valid_ts_code_format'] / result['total_records'] * 100) if result['total_records'] > 0 else 0
                report += f"  - TS代码格式: {result['valid_ts_code_format']}/{result['total_records']} ({rate:.1f}%)\n"
            
            # 检查准确性
            report += "- 准确性:\n"
            if 'amount_greater_than_zero' in result:
                rate = (result['amount_greater_than_zero'] / result['total_records'] * 100) if result['total_records'] > 0 else 0
                report += f"  - 成交额大于0: {result['amount_greater_than_zero']}/{result['total_records']} ({rate:.1f}%)\n"
            if 'valid_ai_score_range' in result:
                rate = (result['valid_ai_score_range'] / result['total_records'] * 100) if result['total_records'] > 0 else 0
                report += f"  - AI评分范围有效: {result['valid_ai_score_range']}/{result['total_records']} ({rate:.1f}%)\n"
            
            # 其他统计信息
            if 'unique_stocks' in result:
                report += f"- 唯一股票数: {result['unique_stocks']}\n"
            if 'unique_concepts' in result:
                report += f"- 唯一概念数: {result['unique_concepts']}\n"
            
            # 问题列表
            if result.get('issues', []):
                report += "- 问题:\n"
                for issue in result['issues']:
                    report += f"  - {issue}\n"
            else:
                report += "- 问题: 无\n"
        
        return report
