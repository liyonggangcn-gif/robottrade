import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
import pandas as pd
from datetime import datetime

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
    
    # 分批更新
    batch_size = 50
    total_updated = 0
    
    for i in range(0, len(stock_codes), batch_size):
        batch = stock_codes[i:i+batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(stock_codes) + batch_size - 1) // batch_size
        
        print(f"\n=== Processing batch {batch_num}/{total_batches} ({len(batch)} stocks) ===")
        
        # 转换为Tushare格式
        ts_codes = []
        for code in batch:
            if code.startswith('6'):
                ts_codes.append(f"{code}.SH")
            else:
                ts_codes.append(f"{code}.SZ")
        
        try:
            # 获取基本面数据
            print(f"Fetching basic data for batch {batch_num}...")
            df_basic = pro.daily_basic(
                ts_code=','.join(ts_codes),
                trade_date='20250205',
                fields='ts_code,trade_date,pe,pb,total_mv'
            )
            
            if df_basic is not None and not df_basic.empty:
                # 重命名列
                df_basic = df_basic.rename(columns={'pe': 'pe_ttm'})
                
                # 转换代码格式
                df_basic['ts_code'] = df_basic['ts_code'].str.replace('.SH', '').str.replace('.SZ', '')
                
                # 更新数据库
                for _, row in df_basic.iterrows():
                    ts_code = row['ts_code']
                    pe_ttm = row['pe_ttm'] if pd.notna(row['pe_ttm']) else 0
                    pb = row['pb'] if pd.notna(row['pb']) else 0
                    total_mv = row['total_mv'] if pd.notna(row['total_mv']) else 0
                    
                    conn.execute('''
                    UPDATE stock_daily
                    SET pe_ttm = ?, pb = ?, total_mv = ?
                    WHERE ts_code = ? AND trade_date >= '2025-01-01'
                    ''', [pe_ttm, pb, total_mv, ts_code])
                
                updated = len(df_basic)
                total_updated += updated
                print(f"✓ Updated {updated} stocks in batch {batch_num}")
            else:
                print(f"✗ No basic data for batch {batch_num}")
                
        except Exception as e:
            print(f"✗ Error processing batch {batch_num}: {e}")
        
        # 短暂休息，避免API限流
        if i + batch_size < len(stock_codes):
            print("Taking a short break to avoid API rate limiting...")
            import time
            time.sleep(1)
    
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
