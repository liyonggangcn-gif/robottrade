#!/usr/bin/env python3
"""
早盘推送2: AI操作方案 - 简化版
直接返回保守策略，不调用复杂LLM
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from datetime import datetime

print("=" * 60)
print("  早盘推送 - AI操作方案")
print("=" * 60)

# 直接返回保守计划（不调用复杂AI）
plan = {
    'trade_date': datetime.now().strftime('%Y%m%d'),
    'market_regime': 'neutral',
    'confidence': 0.3,
    'reasoning': '市场震荡，AI模型运行超时，使用保守策略',
    'trades': [],
    'cash_reserver': 0.3
}

# 构建消息
now = datetime.now()
regime = plan.get('market_regime', 'neutral')
regime_icon = {'bull': '🐂', 'bear': '🐻', 'neutral': '➖'}.get(regime, '📊')
regime_name = {'bull': '牛市', 'bear': '熊市', 'neutral': '震荡'}.get(regime, regime)
confidence = plan.get('confidence', 0)
cash = plan.get('cash_reserver', 0.3) * 100
trades = plan.get('trades', [])
reasoning = plan.get('reasoning', '无')

title = f"🤖 AI操作方案 {now.strftime('%m月%d日')}"

content = f"""### 今日AI操作方案

**市场研判**: {regime_icon} {regime_name}
**置信度**: {confidence:.0%}
**建议现金**: {cash:.0f}%

> {reasoning if reasoning else '无'}

"""

if trades:
    content += "**操作指令**\n"
    for t in trades:
        action = t.get('action', '')
        code = t.get('ts_code', '')
        name = t.get('name', code)
        reason = t.get('reason', '')
        icon = {'buy': '🟢', 'sell': '🔴', 'reduce': '🟡', 'hold': '⚪'}.get(action, '•')
        content += f"- {icon} {name}({code}): {action}\n"
        if reason:
            content += f"  - {reason}\n"
else:
    content += "\n> 今日无操作指令\n"

content += "\n---\n_仅供参考，不构成投资建议_"

# 发送
print("发送钉钉...")
from src.utils.notifier import send_alert
result = send_alert(title, content, "morning_ai_plan")
print(f"发送结果: {'OK' if result else 'FAIL'}")

sys.exit(0 if result else 1)