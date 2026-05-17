#!/bin/bash
# 在 WSL 中安装 crontab
# 用法: 在 WSL 终端执行: ./scripts/wsl/install_cron.sh
# 或: bash scripts/wsl/install_cron.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
CRONTAB_FILE="$SCRIPT_DIR/crontab.txt"

# 将 crontab.txt 中的路径替换为实际项目路径
CRON_CONTENT=$(sed "s|/mnt/e/SynologyDrive/robottrade|$PROJECT_DIR|g" "$CRONTAB_FILE")

# 确保脚本可执行
chmod +x "$SCRIPT_DIR"/*.sh

echo "=========================================="
echo "  安装 WSL Cron 定时任务"
echo "=========================================="
echo "项目路径: $PROJECT_DIR"
echo ""
echo "将添加以下任务:"
echo "$CRON_CONTENT"
echo ""
echo "注意: 请先运行 'sudo service cron start' 启动 cron 服务"
echo "      或使用 Windows 开机脚本自动启动"
echo "=========================================="

# 合并到现有 crontab，避免覆盖用户其他任务（只移除本项目的旧任务）
(crontab -l 2>/dev/null | grep -v "run_data_sync.sh" | grep -v "run_morning_push.sh" | grep -v "run_evening_push.sh" || true; echo "$CRON_CONTENT" | grep -v "^#" | grep -v "^$") | crontab -

echo "已安装。当前 crontab:"
crontab -l
echo ""
echo "如需手动启动 cron: sudo service cron start"
