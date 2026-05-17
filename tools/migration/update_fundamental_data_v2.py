import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
import pandas as pd
from datetime import datetime
from tqdm import tqdm

def update_fundamental_data():
    """更新现有数据的基本面信息"""
    print("开始更新基本面数据...")
    
    # 初始化Tushare Pro
    import tushare as ts
    ts.set_token("0093d31f4758df12b01f312a922a49e837d07c18dba2ae5c3ac6d67f")
    pro = ts.pro_api()
    
    # 连接数据库
    conn = duckdb.connect('data/quant.db')
    
    # 获取所有A股代码
    result = conn.execute('''
    SELECT DISTINCT ts_code FROM stock_daily
    WHERE LENGTH(ts_code) = 6 AND ts_code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'
    ORDER BY ts_code
    ''').fetchdf()
    
    stock_codes = result['ts_code'].tolist()
    print(f"找到 {len(stock_codes)} 只A股")
    
    # 逐个更新
    total_updated = 0
    
    for code in tqdm(stock_codes, desc="Updating fundamental data"):
        try:
            # 转换为Tushare格式
            if code.startswith('6'):
                ts_code = f"{code}.SH"
            else:
                ts_code = f"{code}.SZ"
            
            # 获取基本面数据
            df_basic = pro.daily_basic(
                ts_code=ts_code,
                trade_date='20250205',
                fields='ts_code,trade_date,pe,pb,total_mv'
            )
            
            if df_basic is not None and not df_basic.empty:
                row = df_basic.iloc[0]
                pe_ttm = row['pe'] if pd.notna(row['pe']) else 0
                pb = row['pb'] if pd.notna(row['pb']) else 0
                total_mv = row['total_mv'] if pd.notna(row['total_mv']) else 0
                
                # 更新数据库
                conn.execute('''
                UPDATE stock_daily
                SET pe_ttm = ?, pb = ?, total_mv = ?
                WHERE ts_code = ? AND trade_date >= '2025-01-01'
                ''', [pe_ttm, pb, total_mv, code])
                
                total_updated += 1
                
                # 每100只股票打印一次进度
                if total_updated % 100 == 0:
                    print(f"\n✓ 已更新 {total_updated}/{len(stock_codes)} 只股票")
                    
        except Exception as e:
            print(f"\n✗ Error updating {code}: {e}")
    
    print(f"\n=== Update Summary ===")
    print(f"Total stocks processed: {len(stock_codes)}")
    print(f"Total updated: {total_updated}")
    
    # 验证结果
    result = conn.execute('''
    SELECT COUNT(*) as total, 
           COUNT(CASE WHEN pe_ttm > 0 THEN 1 END) as pe_valid,
           COUNT(CASE WHEN pb > 0 THEN 1 END) as pb_valid,
           COUNT(CASE WHEN total_mv > 0 THEN 1 END) as mv_valid
    FROM stock_daily
    WHERE trade_date >= '2025-01-01'
    ''').fetchdf()
    
    print("\n验证结果:")
    print(result)
    
    conn.close()
    print("\n基本面数据更新完成！")

if __name__ == '__main__':
    update_fundamental_data()
