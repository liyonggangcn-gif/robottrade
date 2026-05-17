#!/usr/bin/env python3
"""
5年因子IC分析 + 12策略5年回测
直接在 MySQL 上做因子分析，避免大数据导入
"""
import pymysql
import pandas as pd
import numpy as np
import time
import json

MYSQL_HOST = '192.168.3.41'
MYSQL_PORT = 3306
MYSQL_USER = 'quant'
MYSQL_PASS = '!Abcd12345'
MYSQL_DB = 'quant_trade'

def main():
    t0 = time.time()
    conn = pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                          password=MYSQL_PASS, database=MYSQL_DB, charset='utf8mb4')
    
    # Check data range
    cur = conn.cursor()
    cur.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM stock_daily")
    r = cur.fetchone()
    print("Data range: %s to %s, %d rows" % (r[0], r[1], r[2]))
    
    cur.execute("SELECT YEAR(trade_date) as yr, COUNT(DISTINCT trade_date) as days, COUNT(*) as cnt FROM stock_daily GROUP BY YEAR(trade_date) ORDER BY yr")
    for row in cur.fetchall():
        print("  Year %d: %d days, %d rows" % (row[0], row[1], row[2]))
    cur.close()
    
    # ================================================================
    # Step 1: Factor IC Analysis (directly on MySQL, aggregated)
    # ================================================================
    print("\n" + "="*70)
    print("Step 1: 因子IC分析 (5年)")
    print("="*70)
    
    start_date = '2021-04-01'
    end_date = '2026-04-03'
    
    # Get all trade dates
    trade_dates_df = pd.read_sql(
        "SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date >= '%s' AND trade_date <= '%s' ORDER BY trade_date" % (start_date, end_date),
        conn
    )
    trade_dates = [str(d).strip() for d in trade_dates_df['trade_date'].tolist()]
    print("Trading days: %d" % len(trade_dates))
    
    # For each date, compute factor values and forward returns
    # We'll do this in batches to avoid memory issues
    factor_cols = ['pe_ttm', 'roe', 'gpr', 'netprofit_yoy', 'total_mv']
    factor_names = {
        'pe_ttm': '市盈率TTM', 'roe': 'ROE', 'gpr': '毛利率',
        'netprofit_yoy': '净利润增速', 'total_mv': '总市值'
    }
    
    ic_results = {}
    for col in factor_cols:
        ics = []
        for i, date in enumerate(trade_dates):
            # Find exit date (5 trading days later)
            exit_idx = i + 5
            if exit_idx >= len(trade_dates):
                break
            exit_date = trade_dates[exit_idx]
            
            # Get factor values and forward returns in one query
            query = """
                SELECT sd1.ts_code, sd1.%s as factor_val,
                       (sd2.close - sd1.close) / sd1.close as fwd_ret
                FROM stock_daily sd1
                JOIN stock_daily sd2 ON sd1.ts_code = sd2.ts_code
                WHERE sd1.trade_date = '%s' AND sd2.trade_date = '%s'
                  AND sd1.close > 0 AND sd2.close > 0
                  AND sd1.%s IS NOT NULL
            """ % (col, date, exit_date, col)
            
            df = pd.read_sql(query, conn)
            if len(df) < 50:
                continue
            
            ic = df['factor_val'].rank().corr(df['fwd_ret'].rank(), method='pearson')
            if not np.isnan(ic):
                ics.append(ic)
            
            if (i+1) % 50 == 0:
                print("  %s: processed %d/%d dates" % (col, i+1, len(trade_dates)))
        
        if len(ics) > 10:
            ic_arr = np.array(ics)
            ic_results[col] = {
                'name': factor_names.get(col, col),
                'mean_ic': float(np.mean(ic_arr)),
                'abs_mean_ic': float(np.mean(np.abs(ic_arr))),
                'ic_std': float(np.std(ic_arr)),
                'icir': float(np.mean(ic_arr) / np.std(ic_arr)) if np.std(ic_arr) > 0 else 0,
                'ic_positive_rate': float(np.mean(ic_arr > 0)),
                'n_periods': len(ics),
            }
            print("  %s: mean_ic=%.4f, |IC|=%.4f, ICIR=%.3f, IC>0=%.1f%%, periods=%d" % (
                factor_names.get(col, col), np.mean(ic_arr), np.mean(np.abs(ic_arr)),
                np.mean(ic_arr)/np.std(ic_arr) if np.std(ic_arr) > 0 else 0,
                np.mean(ic_arr > 0)*100, len(ics)))
    
    sorted_ic = sorted(ic_results.items(), key=lambda x: abs(x[1]['mean_ic']), reverse=True)
    
    print("\n%s | %s | %s | %s | %s | %s" % (
        '因子'.ljust(15), '均值IC'.rjust(7), '|IC|均值'.rjust(7),
        'ICIR'.rjust(6), 'IC>0率'.rjust(6), '期数'.rjust(4)))
    print("-"*65)
    for col, data in sorted_ic:
        print("%s | %+7.4f | %7.4f | %6.3f | %5.1f%% | %4d" % (
            data['name'].ljust(15), data['mean_ic'], data['abs_mean_ic'],
            data['icir'], data['ic_positive_rate']*100, data['n_periods']))
    
    conn.close()
    print("\nTotal time: %.1fs" % (time.time()-t0))

if __name__ == '__main__':
    main()
