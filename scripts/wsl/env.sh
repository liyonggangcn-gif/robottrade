#!/bin/bash
# WSL Cron 环境配置
# 支持两种模式:
#   1) USE_WINDOWS_PYTHON=1: 通过 cmd.exe 调用 Windows 的 Python (推荐，复用已有依赖)
#   2) USE_WINDOWS_PYTHON=0: 使用 WSL 内的 python3 (需在 WSL 内 pip install -r requirements.txt)

# 添加 Windows 路径以防 cron 执行时找不到 cmd.exe
export PATH="$PATH:/mnt/c/Windows/System32"

# 默认使用 Windows Python，避免在 WSL 重复安装依赖
export USE_WINDOWS_PYTHON="${USE_WINDOWS_PYTHON:-1}"

# 项目路径 - 根据脚本位置自动推导
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PROJECT_DIR_WSL="$(cd "$SCRIPT_DIR/../.." && pwd)"

# 将 WSL 路径转为 Windows 路径 (如 /mnt/e/xxx -> E:/xxx，用正斜杠避免转义问题)
if [[ "$PROJECT_DIR_WSL" == /mnt/* ]]; then
  _drive=$(echo "$PROJECT_DIR_WSL" | cut -d'/' -f3)
  _rest="${PROJECT_DIR_WSL#/mnt/$_drive/}"
  export PROJECT_DIR_WIN="${_drive^^}:/${_rest}"
fi
