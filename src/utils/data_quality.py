import pandas as pd
import numpy as np
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils

class DataQualityChecker:
    """数据质量检查器"""
    
    def __init__(self):
        """初始化数据质量检查器"""
        self.db_path = Config.duckdb_path.replace('.duckdb', '.db')
        print(f"Successfully initialized DataQualityChecker with SQLite at {self.db_path}")
    
    def check_all(self):
        """检查所有数据质量"""
        print("="*50)
        print("数据质量检查报告")
        print("="*50)
        
        results = {}
        
        # 1. 检查stock_daily表
        results['stock_daily'] = self.check_stock_daily()
        
        # 2. 检查stock_factors表
        results['stock_factors'] = self.check_stock_factors()
        
        # 3. 检查stock_info表
        results['stock_info'] = self.check_stock_info()
        
        # 4. 检查数据一致性
        results['consistency'] = self.check_consistency()
        
        return results
    
    def check_stock_daily(self):
        """检查stock_daily表"""
        print("\n1. 检查 stock_daily 表...")
        
        issues = []
        
        # 检查记录数
        df = DBUtils.query_df('SELECT COUNT(*) FROM stock_daily')
        count = df.iloc[0, 0]
        print(f"   总记录数: {count}")
        
        # 检查缺失值
        df_null = DBUtils.query_df('''
        SELECT 
            COUNT(*) - COUNT(open) as null_open,
            COUNT(*) - COUNT(high) as null_high,
            COUNT(*) - COUNT(low) as null_low,
            COUNT(*) - COUNT(close) as null_close,
            COUNT(*) - COUNT(vol) as null_vol
        FROM stock_daily
        ''')
        null_counts = tuple(df_null.iloc[0])
        
        if null_counts[0] > 0:
            issues.append(f"open字段有 {null_counts[0]} 个缺失值")
        if null_counts[1] > 0:
            issues.append(f"high字段有 {null_counts[1]} 个缺失值")
        if null_counts[2] > 0:
            issues.append(f"low字段有 {null_counts[2]} 个缺失值")
        if null_counts[3] > 0:
            issues.append(f"close字段有 {null_counts[3]} 个缺失值")
        if null_counts[4] > 0:
            issues.append(f"vol字段有 {null_counts[4]} 个缺失值")
        
        # 检查异常值（价格<=0）
        df = DBUtils.query_df('''
        SELECT COUNT(*) FROM stock_daily 
        WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
        ''')
        invalid_prices = df.iloc[0, 0]
        
        if invalid_prices > 0:
            issues.append(f"发现 {invalid_prices} 条价格<=0的异常记录")
        
        # 检查价格逻辑（low > high 或 close不在[low, high]范围内）
        df = DBUtils.query_df('''
        SELECT COUNT(*) FROM stock_daily 
        WHERE low > high OR close < low OR close > high
        ''')
        invalid_logic = df.iloc[0, 0]
        
        if invalid_logic > 0:
            issues.append(f"发现 {invalid_logic} 条价格逻辑错误的记录")
        
        # 检查成交量异常（<0）
        df = DBUtils.query_df('''
        SELECT COUNT(*) FROM stock_daily 
        WHERE vol < 0
        ''')
        invalid_volume = df.iloc[0, 0]
        
        if invalid_volume > 0:
            issues.append(f"发现 {invalid_volume} 条成交量<0的异常记录")
        
        # 检查重复记录
        df = DBUtils.query_df('''
        SELECT COUNT(*) - (SELECT COUNT(*) FROM (
            SELECT DISTINCT trade_date, ts_code FROM stock_daily
        )) as duplicates
        FROM stock_daily
        ''')
        duplicates = df.iloc[0, 0]
        
        if duplicates > 0:
            issues.append(f"发现 {duplicates} 条重复记录")
        
        if issues:
            print("   ❌ 发现问题:")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("   ✅ 未发现明显问题")
        
        return {
            'total_records': count,
            'issues': issues,
            'status': 'OK' if not issues else 'ERROR'
        }
    
    def check_stock_factors(self):
        """检查stock_factors表"""
        print("\n2. 检查 stock_factors 表...")
        
        issues = []
        
        # 检查记录数
        df = DBUtils.query_df('SELECT COUNT(*) FROM stock_factors')
        count = df.iloc[0, 0]
        print(f"   总记录数: {count}")
        
        # 检查缺失值
        df_null = DBUtils.query_df('''
        SELECT 
            COUNT(*) - COUNT(mom_20) as null_mom_20,
            COUNT(*) - COUNT(vol_20) as null_vol_20,
            COUNT(*) - COUNT(rsi_14) as null_rsi_14,
            COUNT(*) - COUNT(atr_14) as null_atr_14
        FROM stock_factors
        ''')
        null_counts = tuple(df_null.iloc[0])
        
        if null_counts[0] > 0:
            issues.append(f"mom_20字段有 {null_counts[0]} 个缺失值")
        if null_counts[1] > 0:
            issues.append(f"vol_20字段有 {null_counts[1]} 个缺失值")
        if null_counts[2] > 0:
            issues.append(f"rsi_14字段有 {null_counts[2]} 个缺失值")
        if null_counts[3] > 0:
            issues.append(f"atr_14字段有 {null_counts[3]} 个缺失值")
        
        # 检查异常值（RSI应该在0-100之间）
        df = DBUtils.query_df('''
        SELECT COUNT(*) FROM stock_factors 
        WHERE rsi_14 < 0 OR rsi_14 > 100
        ''')
        invalid_rsi = df.iloc[0, 0]
        
        if invalid_rsi > 0:
            issues.append(f"发现 {invalid_rsi} 条RSI异常值（不在0-100之间）")
        
        # 检查波动率异常（应该>=0）
        df = DBUtils.query_df('''
        SELECT COUNT(*) FROM stock_factors 
        WHERE vol_20 < 0
        ''')
        invalid_vol = df.iloc[0, 0]
        
        if invalid_vol > 0:
            issues.append(f"发现 {invalid_vol} 条波动率异常值（<0）")
        
        # 检查ATR异常（应该>=0）
        df = DBUtils.query_df('''
        SELECT COUNT(*) FROM stock_factors 
        WHERE atr_14 < 0
        ''')
        invalid_atr = df.iloc[0, 0]
        
        if invalid_atr > 0:
            issues.append(f"发现 {invalid_atr} 条ATR异常值（<0）")
        
        if issues:
            print("   ❌ 发现问题:")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("   ✅ 未发现明显问题")
        
        return {
            'total_records': count,
            'issues': issues,
            'status': 'OK' if not issues else 'ERROR'
        }
    
    def check_stock_info(self):
        """检查stock_info表"""
        print("\n3. 检查 stock_info 表...")
        
        issues = []
        
        # 检查记录数
        df = DBUtils.query_df('SELECT COUNT(*) FROM stock_info')
        count = df.iloc[0, 0]
        print(f"   总记录数: {count}")
        
        # 检查缺失值
        df = DBUtils.query_df('''
        SELECT COUNT(*) FROM stock_info WHERE name IS NULL
        ''')
        null_names = df.iloc[0, 0]
        
        if null_names > 0:
            issues.append(f"发现 {null_names} 条股票名称缺失的记录")
        
        # 检查PE异常（应该>=0）
        df = DBUtils.query_df('''
        SELECT COUNT(*) FROM stock_info 
        WHERE pe_ttm < 0
        ''')
        invalid_pe = df.iloc[0, 0]
        
        if invalid_pe > 0:
            issues.append(f"发现 {invalid_pe} 条PE异常值（<0）")
        
        # 检查PB异常（应该>=0）
        df = DBUtils.query_df('''
        SELECT COUNT(*) FROM stock_info 
        WHERE pb < 0
        ''')
        invalid_pb = df.iloc[0, 0]
        
        if invalid_pb > 0:
            issues.append(f"发现 {invalid_pb} 条PB异常值（<0）")
        
        # 检查市值异常（应该>=0）
        df = DBUtils.query_df('''
        SELECT COUNT(*) FROM stock_info 
        WHERE total_mv < 0
        ''')
        invalid_mv = df.iloc[0, 0]
        
        if invalid_mv > 0:
            issues.append(f"发现 {invalid_mv} 条市值异常值（<0）")
        
        if issues:
            print("   ❌ 发现问题:")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("   ✅ 未发现明显问题")
        
        return {
            'total_records': count,
            'issues': issues,
            'status': 'OK' if not issues else 'ERROR'
        }
    
    def check_consistency(self):
        """检查数据一致性"""
        print("\n4. 检查数据一致性...")
        
        issues = []
        
        # 检查stock_factors中的股票是否在stock_info中
        df = DBUtils.query_df('''
        SELECT COUNT(DISTINCT sf.ts_code) 
        FROM stock_factors sf
        LEFT JOIN stock_info si ON sf.ts_code = si.ts_code
        WHERE si.ts_code IS NULL
        ''')
        orphan_stocks = df.iloc[0, 0]
        
        if orphan_stocks > 0:
            issues.append(f"发现 {orphan_stocks} 只股票在stock_factors中但不在stock_info中")
        
        # 检查stock_daily中的股票是否在stock_info中
        df = DBUtils.query_df('''
        SELECT COUNT(DISTINCT sd.ts_code) 
        FROM stock_daily sd
        LEFT JOIN stock_info si ON sd.ts_code = si.ts_code
        WHERE si.ts_code IS NULL
        ''')
        orphan_daily = df.iloc[0, 0]
        
        if orphan_daily > 0:
            issues.append(f"发现 {orphan_daily} 只股票在stock_daily中但不在stock_info中")
        
        # 检查stock_factors中的日期是否在stock_daily中
        df = DBUtils.query_df('''
        SELECT COUNT(DISTINCT sf.trade_date) 
        FROM stock_factors sf
        LEFT JOIN stock_daily sd ON sf.trade_date = sd.trade_date
        WHERE sd.trade_date IS NULL
        ''')
        orphan_dates = df.iloc[0, 0]
        
        if orphan_dates > 0:
            issues.append(f"发现 {orphan_dates} 个日期在stock_factors中但不在stock_daily中")
        
        if issues:
            print("   ❌ 发现问题:")
            for issue in issues:
                print(f"      - {issue}")
        else:
            print("   ✅ 未发现一致性问题")
        
        return {
            'issues': issues,
            'status': 'OK' if not issues else 'ERROR'
        }
    
    def print_summary(self, results):
        """打印检查摘要"""
        print("\n" + "="*50)
        print("检查摘要")
        print("="*50)
        
        total_issues = 0
        for table, result in results.items():
            if 'issues' in result:
                issues = result['issues']
                total_issues += len(issues)
                status = "✅ OK" if result['status'] == 'OK' else "❌ ERROR"
                print(f"{table}: {status} ({len(issues)} 个问题)")
        
        print(f"\n总计: {total_issues} 个问题")
        
        if total_issues == 0:
            print("🎉 数据质量良好，未发现问题！")
        else:
            print("⚠️  发现数据质量问题，建议进行修复。")
    
    def close(self):
        """关闭资源"""
        print("Successfully closed DataQualityChecker resources")
    
    def __del__(self):
        """析构函数，确保连接被关闭"""
        self.close()

if __name__ == '__main__':
    # 测试代码
    checker = DataQualityChecker()
    results = checker.check_all()
    checker.print_summary(results)
    checker.close()
