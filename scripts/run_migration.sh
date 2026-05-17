#!/bin/bash
cd /home/li/robottrade
rm -f /home/li/robottrade/data/quant_backtest.duckdb
python3 scripts/mysql_to_duckdb.py > /tmp/duckdb_import.log 2>&1 &
echo "Started PID: $!"
