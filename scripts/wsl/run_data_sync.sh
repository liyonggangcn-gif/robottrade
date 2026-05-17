#!/bin/bash
# 数据同步+选股 - 每日 8:00 执行
# 在 WSL 中通过 cron 调用

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/env.sh"

LOG_DIR="$PROJECT_DIR_WSL/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/cron_data_sync_$(date +%Y%m%d).log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始数据同步" >> "$LOG_FILE"

if [[ "${USE_WINDOWS_PYTHON:-1}" == "1" && -n "$PROJECT_DIR_WIN" ]]; then
  # 使用 Windows Python
  cmd.exe /c "cd /d $PROJECT_DIR_WIN && python scripts\daily_alpha_run.py --skip-qlib" >> "$LOG_FILE" 2>&1
else
  # 使用 WSL Python
  cd "$PROJECT_DIR_WSL" || exit 1
  python3 scripts/daily_alpha_run.py --skip-qlib >> "$LOG_FILE" 2>&1
fi
EXIT_CODE=$?

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 结束, exit=$EXIT_CODE" >> "$LOG_FILE"
exit $EXIT_CODE
