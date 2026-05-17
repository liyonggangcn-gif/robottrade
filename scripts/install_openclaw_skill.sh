#!/bin/bash
# 将 robottrade skill 安装到 openclaw
# 在服务器 192.168.3.22 上执行

set -e

SKILL_SRC="/home/li/robottrade/skills/robottrade"
SKILL_DST="$HOME/.openclaw/skills/robottrade"

echo "[1/3] 检查 skill 源文件..."
if [ ! -f "$SKILL_SRC/SKILL.md" ]; then
    echo "❌ SKILL.md 不存在: $SKILL_SRC/SKILL.md"
    echo "   请先确保 robottrade 已部署到 /home/li/robottrade"
    exit 1
fi

echo "[2/3] 安装 skill 到 ~/.openclaw/skills/robottrade..."
mkdir -p "$HOME/.openclaw/skills"

# 直接复制（openclaw 不允许指向其根目录之外的符号链接）
rm -rf "$SKILL_DST"
cp -r "$SKILL_SRC" "$SKILL_DST"
echo "   ✅ 已复制: $SKILL_DST"

echo "[3/3] 重载 openclaw gateway..."
if command -v openclaw &>/dev/null; then
    openclaw gateway restart 2>/dev/null && echo "   ✅ Gateway 已重启" || echo "   ⚠️  Gateway 重启失败，请手动执行: openclaw gateway restart"
else
    echo "   ⚠️  openclaw 命令不在 PATH，请手动重启 gateway"
fi

echo ""
echo "✅ 安装完成！在 openclaw 中可用技能: robottrade"
echo "   验证: openclaw skills list | grep robottrade"
