#!/bin/bash
# 小市值周选股推送 - 每周一 8:30 执行

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

LOG_DIR="$PROJECT_DIR_WSL/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/cron_weekly_small_cap_$(date +%Y%m%d).log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始小市值周选股推送" >> "$LOG_FILE"

if [[ "${USE_WINDOWS_PYTHON:-1}" == "1" && -n "$PROJECT_DIR_WIN" ]]; then
  cmd.exe /c "cd /d $PROJECT_DIR_WIN && python scripts\weekly_small_cap_push.py" >> "$LOG_FILE" 2>&1
else
  cd "$PROJECT_DIR_WSL" || exit 1
  python3 scripts/weekly_small_cap_push.py >> "$LOG_FILE" 2>&1
fi
EXIT_CODE=$?

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 结束, exit=$EXIT_CODE" >> "$LOG_FILE"
exit $EXIT_CODE
