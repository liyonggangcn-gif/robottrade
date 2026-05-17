#!/bin/bash
# Stop uvicorn, sync DuckDB, restart uvicorn

echo "=== Stopping uvicorn ==="
pkill -9 -f "uvicorn src.web.app" 2>/dev/null
sleep 3

echo "=== Running DuckDB sync ==="
cd /home/li/robottrade
./venv/bin/python scripts/sync_mysql_to_duckdb.py 2>&1

echo ""
echo "=== Restarting uvicorn ==="
nohup ./venv/bin/python -m uvicorn src.web.app:app --host 0.0.0.0 --port 8080 --workers 2 > /tmp/uvicorn.log 2>&1 &
sleep 5

echo "=== Verifying ==="
curl -s --connect-timeout 5 --max-time 10 http://localhost:8080/api/status 2>&1 | head -c 200
