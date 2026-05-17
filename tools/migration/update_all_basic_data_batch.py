import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
import pandas as pd
from datetime import datetime
from tqdm import tqdm
import time

def update_fundamental_data_batch():
    """批量更新所有A股的基本面数据"""
    print("开始批量更新所有A股的基本面数据...")
    
    # 初始化Tushare Pro
    import tushare as ts
    ts.set_token("0093d31f4758df12b01f312a922a49e837d07c18dba2ae5c3ac6d67f")
    pro = ts.pro_api()
    
    # 连接数据库
    conn = duckdb.connect('data/quant.db')
    
    # 获取所有A股代码（包括Tushare格式）
    result = conn.execute('''
    SELECT DISTINCT ts_code FROM stock_daily
    WHERE ts_code LIKE '%.SH%' OR ts_code LIKE '%.SZ%' OR ts_code LIKE '%.BJ%'
    ORDER BY ts_code
    ''').fetchdf()
    
    stock_codes = result['ts_code'].tolist()
    print(f"找到 {len(stock_codes)} 只A股（Tushare格式）")
    
    # 批量更新（每次100只）
    batch_size = 100
    total_updated = 0
    
    for i in tqdm(range(0, len(stock_codes), batch_size), desc="Updating basic data"):
        batch_codes = stock_codes[i:i+batch_size]
        
        try:
            # 批量获取基本面数据
            df_basic = pro.daily_basic(
                ts_code=','.join(batch_codes),
                trade_date='20250205',
                fields='ts_code,trade_date,pe,pb,total_mv'
            )
            
            if df_basic is not None and not df_basic.empty:
                # 批量更新数据库
                for _, row in df_basic.iterrows():
                    code = row['ts_code']
                    pe_ttm = row['pe'] if pd.notna(row['pe']) else 0
                    pb = row['pb'] if pd.notna(row['pb']) else 0
                    total_mv = row['total_mv'] if pd.notna(row['total_mv']) else 0
                    
                    conn.execute('''
                    UPDATE stock_daily
                    SET pe_ttm = ?, pb = ?, total_mv = ?
                    WHERE ts_code = ? AND trade_date >= '2025-01-01'
                    ''', [pe_ttm, pb, total_mv, code])
                    
                    total_updated += 1
                
                # 每批打印一次进度
                print(f"\n✓ 已更新 {total_updated}/{len(stock_codes)} 只股票")
                
            # 速率限制：每批之间等待0.5秒
            time.sleep(0.5)
                
        except Exception as e:
            print(f"\n✗ Error updating batch {i}-{i+batch_size}: {e}")
    
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
    update_fundamental_data_batch()
