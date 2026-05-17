#!/bin/bash
# Force kill old uvicorn and restart
kill -9 $(ps aux | grep uvicorn | grep -v grep | awk '{print $2}') 2>/dev/null
sleep 2
cd /home/li/robottrade
nohup ./venv/bin/python -m uvicorn src.web.app:app --host 0.0.0.0 --port 8080 --workers 2 > /tmp/uvicorn.log 2>&1 &
echo "Started new uvicorn"
sleep 5
ps aux | grep uvicorn | grep -v grep
curl -s --connect-timeout 5 --max-time 10 http://localhost:8080/api/status 2>&1 | head -c 300
