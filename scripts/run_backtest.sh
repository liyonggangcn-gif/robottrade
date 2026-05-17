#!/bin/bash
cd /home/li/robottrade
nohup python3 scripts/duckdb_backtest_fast.py > /tmp/backtest.log 2>&1 &
echo "Started PID: $!"
