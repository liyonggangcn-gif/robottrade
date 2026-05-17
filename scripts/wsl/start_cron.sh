#!/bin/bash
# 启动 WSL 内的 cron 服务
# 供 Windows 开机脚本调用

sudo service cron start 2>/dev/null || sudo service crond start 2>/dev/null || true
