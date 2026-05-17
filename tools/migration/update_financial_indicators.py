import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
import pandas as pd
from datetime import datetime, timedelta
from tqdm import tqdm

def update_financial_indicators():
    """更新财务指标数据（ROE, GPR, 净利润增长率）"""
    print("开始更新财务指标数据...")
    
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
    
    for code in tqdm(stock_codes, desc="Updating financial indicators"):
        try:
            # 转换为Tushare格式
            if code.startswith('6'):
                ts_code = f"{code}.SH"
            else:
                ts_code = f"{code}.SZ"
            
            # 获取财务指标数据（最近几个季度）
            df_fina = pro.fina_indicator(
                ts_code=ts_code,
                start_date='20240101',
                end_date='20250206',
                fields='ts_code,end_date,roe,grossprofit_margin,netprofit_yoy'
            )
            
            if df_fina is not None and not df_fina.empty:
                # 获取最新的财务指标
                latest = df_fina.iloc[0]
                roe = latest['roe'] if pd.notna(latest['roe']) else 0
                gpr = latest['grossprofit_margin'] if pd.notna(latest['grossprofit_margin']) else 0
                netprofit_yoy = latest['netprofit_yoy'] if pd.notna(latest['netprofit_yoy']) else 0
                
                # 更新数据库
                conn.execute('''
                UPDATE stock_daily
                SET roe = ?, gpr = ?, netprofit_yoy = ?
                WHERE ts_code = ? AND trade_date >= '2025-01-01'
                ''', [roe, gpr, netprofit_yoy, code])
                
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
           COUNT(CASE WHEN roe > 0 THEN 1 END) as roe_valid,
           COUNT(CASE WHEN gpr > 0 THEN 1 END) as gpr_valid,
           COUNT(CASE WHEN netprofit_yoy > 0 THEN 1 END) as growth_valid
    FROM stock_daily
    WHERE trade_date >= '2025-01-01'
    ''').fetchdf()
    
    print("\n验证结果:")
    print(result)
    
    conn.close()
    print("\n财务指标数据更新完成！")

if __name__ == '__main__':
    update_financial_indicators()
