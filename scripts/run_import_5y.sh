#!/bin/bash
cd /home/li/robottrade
rm -f /home/li/robottrade/data/quant_backtest_5y.duckdb
nohup python3 scripts/mysql_to_duckdb_5y.py > /tmp/duckdb_5y_import.log 2>&1 &
echo "Started PID: $!"
