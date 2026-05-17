#!/usr/bin/env python3
import pymysql
import duckdb
import pandas as pd
import time
import os

print("Connecting to MySQL...")
conn = pymysql.connect(host='192.168.3.41', port=3306, user='quant', password='!Abcd12345', database='quant_trade', charset='utf8mb4')
print("MySQL connected")

# Check range
cur = conn.cursor()
cur.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM stock_daily")
r = cur.fetchone()
print("stock_daily: %s to %s, %d rows" % (r[0], r[1], r[2]))

cur.execute("SELECT YEAR(trade_date) as yr, COUNT(DISTINCT trade_date) as days, COUNT(*) as cnt FROM stock_daily GROUP BY YEAR(trade_date) ORDER BY yr")
for row in cur.fetchall():
    print("  Year %d: %d days, %d rows" % (row[0], row[1], row[2]))
cur.close()

# Import to DuckDB
print("\nImporting to DuckDB...")
dc = duckdb.connect('/tmp/quant_backtest_5y.duckdb')

cutoff = '2021-01-01'
t1 = time.time()
print("stock_daily (since %s)..." % cutoff)
total = 0
first = True
for chunk in pd.read_sql("SELECT trade_date, ts_code, close, high, low, vol, amount, pe_ttm, roe, gpr, netprofit_yoy, total_mv FROM stock_daily WHERE trade_date >= '%s'" % cutoff, conn, chunksize=500000):
    if first:
        dc.execute('CREATE TABLE stock_daily AS SELECT * FROM chunk')
        first = False
    else:
        dc.execute('INSERT INTO stock_daily SELECT * FROM chunk')
    total += len(chunk)
    print("  chunk: %d rows" % total)
print("  Done: %d rows in %.1fs" % (total, time.time()-t1))

t1 = time.time()
print("stock_info...")
info = pd.read_sql("SELECT ts_code, name, industry FROM stock_info", conn)
dc.execute('CREATE TABLE stock_info AS SELECT * FROM info')
print("  Done: %d rows in %.1fs" % (len(info), time.time()-t1))

t1 = time.time()
print("stock_factors (since %s)..." % cutoff)
total = 0
first = True
for chunk in pd.read_sql("SELECT trade_date, ts_code, mom_20, rsi_14, macd_hist, bb_width, vol_ratio, atr_14, kdj_k, kdj_d FROM stock_factors WHERE trade_date >= '%s'" % cutoff, conn, chunksize=500000):
    if first:
        dc.execute('CREATE TABLE stock_factors AS SELECT * FROM chunk')
        first = False
    else:
        dc.execute('INSERT INTO stock_factors SELECT * FROM chunk')
    total += len(chunk)
    print("  chunk: %d rows" % total)
print("  Done: %d rows in %.1fs" % (total, time.time()-t1))

conn.close()

print("Creating indexes...")
dc.execute('CREATE INDEX IF NOT EXISTS idx_daily_code_date ON stock_daily(ts_code, trade_date)')
dc.execute('CREATE INDEX IF NOT EXISTS idx_daily_date ON stock_daily(trade_date)')
dc.execute('CREATE INDEX IF NOT EXISTS idx_info_code ON stock_info(ts_code)')
dc.execute('CREATE INDEX IF NOT EXISTS idx_factors_code_date ON stock_factors(ts_code, trade_date)')

for t in ['stock_daily', 'stock_info', 'stock_factors']:
    cnt = dc.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
    print("  %s: %d rows" % (t, cnt))

dc.close()
fsize = os.path.getsize('/tmp/quant_backtest_5y.duckdb') / (1024*1024)
print("DuckDB size: %.1f MB" % fsize)
print("DONE")
