#!/bin/bash
cd /home/li/robottrade
nohup python3 scripts/backtest_5y_full.py > /tmp/backtest_5y.log 2>&1 &
echo "Started PID: $!"
