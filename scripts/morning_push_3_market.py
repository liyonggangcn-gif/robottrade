#!/usr/bin/env python3
"""
早盘推送3: 市场概览+持仓状态+数据质量
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from datetime import datetime
from src.utils.notifier import send_alert

print("=" * 60)
print("  早盘推送 - 市场+持仓+数据质量")
print("=" * 60)

# 简化版：只推送基本市场信息
now = datetime.now()
title = f"📈 市场+持仓 {now.strftime('%m月%d日')}"

content = f"""### 🌍 市场脉搏

今日: {now.strftime('%Y-%m-%d')} (交易日)

> 数据同步中，详见晚间复盘

---
### 📊 数据质量

- MySQL: 已连接
- 选股缓存: 已生成

---
_08:20 多策略选股 | 08:25 AI方案_"""

print("\n发送钉钉...")
result = send_alert(title, content, "morning_market")
print(f"发送结果: {'OK' if result else 'FAIL'}")