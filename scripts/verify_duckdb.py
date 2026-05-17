import duckdb
conn = duckdb.connect('/home/li/robottrade/data/quant_backtest_5y.duckdb', read_only=True)
print('Tables:', conn.execute('SHOW TABLES').fetchall())
result = conn.execute('SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT ts_code) FROM stock_daily').fetchone()
print(f'Date range: {result[0]} to {result[1]}, Stocks: {result[2]}')
conn.close()
