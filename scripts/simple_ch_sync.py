#!/usr/bin/env python3
"""
Simple full sync to ClickHouse - one shot
"""
import sys
import os
import clickhouse_connect
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.utils.db_utils import DBUtils

print("=== Simple Full Sync ===")

# 连接ClickHouse
ch = clickhouse_connect.get_client(
    host='192.168.3.51', port=8123,
    username='default', password='clickhouse123'
)
print("Connected")

# 删除旧表重建
ch.command("DROP TABLE IF EXISTS stock_daily")
ch.command("""
CREATE TABLE stock_daily (
    trade_date Date,
    ts_code String,
    name String,
    close Nullable(Float32),
    open Nullable(Float32),
    high Nullable(Float32),
    low Nullable(Float32),
    vol Nullable(Int64),
    amount Nullable(Float64),
    pe_ttm Nullable(Float32),
    roe Nullable(Float32),
    gpr Nullable(Float32),
    netprofit_yoy Nullable(Float32),
    total_mv Nullable(Float64),
    turnover_rate Nullable(Float32)
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(trade_date)
ORDER BY (ts_code, trade_date)
""")
print("Created stock_daily table")

# 获取MySQL数据 - 只选择存在的列
print("\nFetching data from MySQL...")
df = DBUtils.query_df("""
    SELECT trade_date, ts_code, close, open, high, low, vol, amount,
           pe_ttm, roe, gpr, netprofit_yoy, total_mv
    FROM stock_daily
    WHERE trade_date >= '20250101'
    ORDER BY trade_date
    LIMIT 500000
""")
print(f"Got {len(df)} rows")

# 转换日期
df['trade_date'] = pd.to_datetime(df['trade_date'], errors='coerce').dt.date

# 插入
print("\nInserting to ClickHouse...")
ch.insert_df('stock_daily', df)
print(f"Inserted {len(df)} rows")

# 验证
result = ch.query("SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM stock_daily")
print(f"Result: {result.result_rows[0]}")

ch.close()
print("\nDone!")
