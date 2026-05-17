#!/bin/bash
# AI动量选股推送 (每日8:05)
cd /home/li/robottrade && source venv/bin/activate
python scripts/ai_morning_push.py >> logs/ai_morning_$(date +\%Y\%m\%d).log 2>&1