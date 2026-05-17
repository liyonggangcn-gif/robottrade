#!/usr/bin/env python3
"""Fast 5-year data import using DuckDB mysql ATTACH"""
import duckdb
import time
import os
from datetime import datetime

MYSQL_HOST = '192.168.3.41'
MYSQL_PORT = 3306
MYSQL_USER = 'quant'
MYSQL_PASS = '!Abcd12345'
MYSQL_DB = 'quant_trade'
DUCKDB_PATH = '/home/li/robottrade/data/quant_backtest_5y.duckdb'

def main():
    conn = duckdb.connect(DUCKDB_PATH)
    conn.execute('INSTALL mysql; LOAD mysql;')
    
    attach_str = f"host={MYSQL_HOST} port={MYSQL_PORT} user={MYSQL_USER} passwd={MYSQL_PASS} db={MYSQL_DB}"
    print(f'Attaching MySQL: {attach_str}')
    conn.execute(f"ATTACH '{attach_str}' AS mysql_db (TYPE mysql)")
    
    # Check available tables
    tables = conn.execute("SELECT table_name FROM mysql_db.information_schema.tables WHERE table_schema = 'quant_trade'").fetchall()
    print(f'Available tables: {[t[0] for t in tables]}')
    
    target_tables = ['stock_daily', 'stock_info', 'stock_factors', 'stock_concepts']
    
    for table in target_tables:
        t0 = time.time()
        print(f'[{table}] importing...')
        conn.execute(f'DROP TABLE IF EXISTS {table}')
        
        if table == 'stock_daily':
            # Only last 5 years
            cutoff = (datetime.now().replace(year=datetime.now().year - 5)).strftime('%Y-%m-%d')
            conn.execute(f"""
                CREATE TABLE {table} AS 
                SELECT * FROM mysql_db.{table}
                WHERE trade_date >= '{cutoff}'
            """)
        else:
            conn.execute(f'CREATE TABLE {table} AS SELECT * FROM mysql_db.{table}')
        
        cnt = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        print(f'  Done: {cnt:,} rows in {time.time()-t0:.1f}s')
    
    conn.execute('DETACH mysql_db')
    
    print('Creating indexes...')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_daily_code_date ON stock_daily(ts_code, trade_date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_daily_date ON stock_daily(trade_date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_factors_code_date ON stock_factors(ts_code, trade_date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_concepts_code ON stock_concepts(ts_code)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_info_code ON stock_info(ts_code)')
    
    for table in target_tables:
        cnt = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        print(f'  {table}: {cnt:,} rows')
    
    conn.close()
    fsize = os.path.getsize(DUCKDB_PATH) / (1024*1024)
    print(f'\nData imported to {DUCKDB_PATH} ({fsize:.1f} MB)')

if __name__ == '__main__':
    main()
