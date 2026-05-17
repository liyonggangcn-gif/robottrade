#!/bin/bash
cd /home/li/robottrade
rm -f /tmp/quant_backtest_5y.duckdb /tmp/backtest_5y_results.json
nohup python3 scripts/backtest_5y_full.py > /tmp/backtest_5y.log 2>&1 &
echo "Started PID: $!"
