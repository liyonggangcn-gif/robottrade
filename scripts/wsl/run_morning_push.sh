#!/bin/bash
# 早盘推送 - 每日 8:30 执行
# 在 WSL 中通过 cron 调用

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

LOG_DIR="$PROJECT_DIR_WSL/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/cron_morning_push_$(date +%Y%m%d).log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始早盘推送" >> "$LOG_FILE"

if [[ "${USE_WINDOWS_PYTHON:-1}" == "1" && -n "$PROJECT_DIR_WIN" ]]; then
  cmd.exe /c "cd /d $PROJECT_DIR_WIN && python scripts\morning_push.py" >> "$LOG_FILE" 2>&1
else
  cd "$PROJECT_DIR_WSL" || exit 1
  python3 scripts/morning_push.py >> "$LOG_FILE" 2>&1
fi
EXIT_CODE=$?

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 结束, exit=$EXIT_CODE" >> "$LOG_FILE"
exit $EXIT_CODE
