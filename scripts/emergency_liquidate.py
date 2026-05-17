#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应急清仓脚本 —— 重大事件触发

用法:
  # 分析事件（不执行操作，仅看报告）
  python scripts/emergency_liquidate.py --event "美国对伊朗发动军事打击"

  # 分析 + 钉钉推送
  python scripts/emergency_liquidate.py --event "美国对伊朗发动军事打击" --notify

  # 分析 + 自动清仓（风险=极高时自动执行，其他等级仍询问）
  python scripts/emergency_liquidate.py --event "美国对伊朗发动军事打击" --auto

  # 强制清仓（跳过确认和风险等级判断）
  python scripts/emergency_liquidate.py --event "紧急" --force

  # 定时模式（可由 cron 调用，定期从事件文件读取待分析事件）
  python scripts/emergency_liquidate.py --from-file /tmp/event.txt --auto --notify
"""

import sys
import os
import argparse
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from loguru import logger
from src.utils.log_utils import init_logger

init_logger("emergency_liquidate")

from src.risk.event_risk_monitor import EventRiskMonitor, RISK_LEVELS
from src.portfolio.position_manager import PositionManager
from src.utils.notifier import NotifierFactory
from src.utils.config_loader import Config


# 执行清仓操作的风险等级门槛（极高 + 高）
AUTO_LIQUIDATE_LEVELS = {"极高"}      # --auto 模式下自动执行的等级
ASK_CONFIRM_LEVELS = {"高", "中"}     # 需人工确认的等级


def parse_args():
    parser = argparse.ArgumentParser(description="重大事件应急清仓工具")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--event", type=str, help="事件描述文字")
    group.add_argument("--from-file", type=str, metavar="FILE",
                       help="从文件读取事件描述（utf-8 纯文本）")

    parser.add_argument("--notify", action="store_true",
                        help="将分析结果推送到钉钉")
    parser.add_argument("--auto", action="store_true",
                        help="风险=极高时自动执行清仓（其他等级仍询问）")
    parser.add_argument("--force", action="store_true",
                        help="强制清仓，跳过分析和确认")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅展示分析，不做任何写库操作")
    return parser.parse_args()


def confirm(prompt: str) -> bool:
    """交互式确认"""
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def do_liquidate(pm: PositionManager, reason: str, dry_run: bool) -> dict:
    """执行清仓操作"""
    if dry_run:
        positions = pm.get_current_positions()
        print(f"\n[DRY-RUN] 模拟清仓 {len(positions)} 只持仓，不写入数据库")
        return {"liquidated": len(positions), "dry_run": True}
    return pm.emergency_liquidate_all(reason=reason)


def send_notification(result: dict, analysis: dict, dry_run: bool):
    """发送钉钉通知"""
    try:
        ding_cfg = Config.get('notification.dingtalk') or {}
        webhook_url = ding_cfg.get('webhook', '')
        secret_word = ding_cfg.get('secret_word', '提醒')
        if not webhook_url:
            print("[WARN] 钉钉推送未配置，跳过通知")
            return
        notifier = NotifierFactory.create_notifier(
            'dingtalk', webhook_url=webhook_url, secret_word=secret_word
        )

        monitor = EventRiskMonitor()
        title, content = monitor.format_alert_message(analysis)

        # 追加清仓执行结果
        if not dry_run and result.get("liquidated", 0) > 0:
            pos_lines = ""
            for p in result.get("positions", [])[:10]:
                pct = p.get("profit_loss_pct", 0) * 100
                sign = "+" if pct >= 0 else ""
                pos_lines += f"• {p['name']}({p['ts_code']}) {p['shares']:.0f}股 @ {p['price']:.2f} → {sign}{pct:.1f}%\n"

            total_pl = result.get("total_profit_loss", 0)
            pl_sign = "+" if total_pl >= 0 else ""

            content += f"""

---
### ✅ 清仓执行完毕（提醒）

- 清仓股票数：**{result['liquidated']} 只**
- 变现金额：**{result.get('total_amount', 0):,.0f} 元**
- 合计盈亏：**{pl_sign}{total_pl:,.0f} 元**

**明细（前10只）：**
{pos_lines}"""
        elif dry_run:
            content += "\n\n> ⚠️ 当前为 DRY-RUN 模式，未实际执行清仓"

        notifier.send_message(title, content)
        print("[INFO] 钉钉通知已发送")
    except Exception as e:
        print(f"[WARN] 钉钉通知发送失败: {e}")


def print_analysis(analysis: dict):
    """打印分析报告到终端"""
    level = analysis.get("risk_level", "低")
    emoji = analysis.get("emoji", "⚠️")
    action_name = analysis.get("action_name", "")
    confidence = analysis.get("confidence", 0)
    impact = analysis.get("impact_analysis", "")
    recommendation = analysis.get("recommendation", "")
    sectors = analysis.get("affected_sectors", [])
    risks = analysis.get("key_risks", [])

    print("\n" + "=" * 60)
    print(f"  {emoji} 重大事件风险分析报告")
    print("=" * 60)
    print(f"  事件  : {analysis.get('event', '')}")
    print(f"  时间  : {analysis.get('analyzed_at', '')[:16].replace('T', ' ')}")
    print(f"  风险  : {emoji} {level}  (置信度 {confidence*100:.0f}%)")
    print(f"  建议  : 🎯 {action_name}")
    print("-" * 60)
    print(f"  影响分析:\n  {impact}")
    print(f"\n  操作建议:\n  {recommendation}")
    if sectors:
        print("\n  受影响板块:")
        for s in sectors[:6]:
            icon = "📉" if s.get("impact") == "利空" else ("📈" if s.get("impact") == "利好" else "➡️")
            print(f"    {icon} {s.get('sector')}: {s.get('reason')}")
    if risks:
        print("\n  关键风险:")
        for r in risks[:4]:
            print(f"    ⚠️  {r}")
    print("=" * 60)


def main():
    args = parse_args()

    # 读取事件描述
    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8") as f:
            event = f.read().strip()
    else:
        event = args.event.strip()

    if not event:
        print("[ERROR] 事件描述不能为空")
        sys.exit(1)

    pm = PositionManager()

    # ----------------------------------------------------------------
    # --force：强制清仓，跳过分析
    # ----------------------------------------------------------------
    if args.force:
        print(f"\n⚡ 强制清仓模式（原因: {event}）")
        if not args.dry_run and not confirm("确认要强制清仓所有持仓？"):
            print("已取消")
            sys.exit(0)
        result = do_liquidate(pm, event, args.dry_run)
        # 发送一条简单的通知
        fake_analysis = {
            "event": event,
            "risk_level": "极高",
            "emoji": "🚨",
            "action_name": "强制清仓",
            "action": "full_liquidate",
            "confidence": 1.0,
            "summary": "用户手动触发强制清仓",
            "impact_analysis": "用户手动触发，跳过 LLM 分析",
            "recommendation": "已强制清仓所有持仓",
            "affected_sectors": [],
            "key_risks": [],
            "monitoring_indicators": [],
            "analyzed_at": datetime.now().isoformat(),
        }
        if args.notify:
            send_notification(result, fake_analysis, args.dry_run)
        print(f"\n✅ 清仓完成: {result.get('liquidated', 0)} 只股票")
        return

    # ----------------------------------------------------------------
    # 正常流程：LLM 分析 → 决策 → （可选）执行
    # ----------------------------------------------------------------
    print(f"\n🔍 正在分析事件...\n  {event}\n")
    monitor = EventRiskMonitor()
    analysis = monitor.analyze_event(event)

    # 打印分析报告
    print_analysis(analysis)

    level = analysis.get("risk_level", "低")
    action = analysis.get("action", "hold")
    positions = pm.get_current_positions()
    n_positions = len(positions)

    if n_positions == 0:
        print("\n[INFO] 当前无持仓，无需操作")
        if args.notify:
            send_notification({}, analysis, args.dry_run)
        return

    # ----------------------------------------------------------------
    # 决策逻辑
    # ----------------------------------------------------------------
    should_execute = False

    if action == "hold":
        print(f"\n💡 风险等级【{level}】，建议持仓观望，无需操作")
        if args.notify:
            send_notification({}, analysis, args.dry_run)
        return

    if args.auto and level in AUTO_LIQUIDATE_LEVELS:
        print(f"\n🚨 风险等级【{level}】，--auto 模式自动执行清仓（{n_positions} 只持仓）")
        should_execute = True

    elif level in AUTO_LIQUIDATE_LEVELS | ASK_CONFIRM_LEVELS:
        action_name = analysis.get("action_name", "操作")
        print(f"\n⚠️  风险等级【{level}】，建议：{action_name}（当前 {n_positions} 只持仓）")
        if not args.dry_run:
            should_execute = confirm(f"是否执行【{action_name}】？")
        else:
            should_execute = True

    # ----------------------------------------------------------------
    # 执行
    # ----------------------------------------------------------------
    result = {}
    if should_execute:
        result = do_liquidate(pm, event, args.dry_run)
        if not args.dry_run:
            pl_sign = "+" if result.get("total_profit_loss", 0) >= 0 else ""
            print(f"\n✅ 操作完成: 清仓 {result['liquidated']} 只股票")
            print(f"   变现金额: {result.get('total_amount', 0):,.0f} 元")
            print(f"   合计盈亏: {pl_sign}{result.get('total_profit_loss', 0):,.0f} 元")
    else:
        print("\n已取消操作，持仓保持不变")

    # 推送钉钉
    if args.notify:
        send_notification(result, analysis, args.dry_run)


if __name__ == "__main__":
    main()
