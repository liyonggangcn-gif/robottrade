#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期货ETF信号钉钉推送脚本
定期推送基于期货价格的ETF交易信号
"""

import sys
import os
from datetime import datetime

# 添加项目根目录到路径
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

# 强制清除代理
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

# 设置Windows控制台UTF-8编码
if sys.platform == 'win32':
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except:
        pass

from src.utils.log_utils import init_logger
from src.analysis.futures_etf_signal import FuturesETFSignalGenerator
from src.utils.config_loader import Config
from src.utils.notifier import NotifierFactory

logger = init_logger("push_futures_etf_signal")


def format_signal_message(signals: dict) -> tuple:
    """
    格式化信号消息
    
    Args:
        signals: 板块信号字典
        
    Returns:
        (title, content) 元组
    """
    today = datetime.now().strftime('%Y-%m-%d')
    
    title = f"📊 期货ETF交易信号 {today}"
    
    content = f"**期货ETF交易信号报告**\n\n"
    content += f"📅 日期：{today}\n"
    content += f"⏰ 时间：{datetime.now().strftime('%H:%M:%S')}\n\n"
    content += "---\n\n"
    
    # 信号图标
    signal_icons = {
        'BUY': '🟢',
        'SELL': '🔴',
        'HOLD': '🟡'
    }
    
    # 按信号类型分组
    buy_signals = []
    sell_signals = []
    hold_signals = []
    
    for sector, signal_info in signals.items():
        signal = signal_info['signal']
        if signal == 'BUY':
            buy_signals.append((sector, signal_info))
        elif signal == 'SELL':
            sell_signals.append((sector, signal_info))
        else:
            hold_signals.append((sector, signal_info))
    
    # 买入信号
    if buy_signals:
        content += "## 🟢 买入信号\n\n"
        for sector, signal_info in buy_signals:
            icon = signal_icons.get(signal_info['signal'], '●')
            content += f"### {icon} {sector}\n\n"
            content += f"- **信号强度**：{signal_info['strength']:.1%}\n"
            content += f"- **综合得分**：{signal_info['score']:.2f}\n"
            content += f"- **建议持仓周期**：{signal_info['holding_period']}\n"
            content += f"- **仓位建议**：{signal_info['position_suggestion']}\n"
            content += f"- **风险等级**：{signal_info['risk_level']}\n"
            content += f"- **操作建议**：{signal_info['operation_advice']}\n"
            
            # 期货详情
            if signal_info['futures_scores']:
                content += f"\n**主要期货品种表现：**\n"
                top_futures = sorted(
                    signal_info['futures_scores'].items(),
                    key=lambda x: abs(x[1].get('change_pct', 0)),
                    reverse=True
                )[:3]
                
                for futures_name, futures_data in top_futures:
                    change_pct = futures_data.get('change_pct', 0)
                    if change_pct is not None:
                        change_icon = '📈' if change_pct > 0 else '📉'
                        content += f"- {change_icon} {futures_name}：{change_pct:+.2f}%\n"
            
            content += "\n"
    
    # 卖出信号
    if sell_signals:
        content += "## 🔴 卖出信号\n\n"
        for sector, signal_info in sell_signals:
            icon = signal_icons.get(signal_info['signal'], '●')
            content += f"### {icon} {sector}\n\n"
            content += f"- **信号强度**：{signal_info['strength']:.1%}\n"
            content += f"- **综合得分**：{signal_info['score']:.2f}\n"
            content += f"- **操作建议**：{signal_info['operation_advice']}\n"
            content += "\n"
    
    # 持有信号
    if hold_signals:
        content += "## 🟡 持有/观望\n\n"
        for sector, signal_info in hold_signals:
            icon = signal_icons.get(signal_info['signal'], '●')
            content += f"### {icon} {sector}\n\n"
            content += f"- **综合得分**：{signal_info['score']:.2f}\n"
            content += f"- **操作建议**：{signal_info['operation_advice']}\n"
            content += "\n"
    
    content += "---\n\n"
    content += "**说明：**\n"
    content += "- 信号基于期货价格变化（5天/20天/60天多时间框架分析）\n"
    content += "- 持仓周期建议：1-2周（强信号）、2-4周（中等信号）、4-8周（弱信号）\n"
    content += "- 仓位建议：强信号20-30%，中等信号10-20%，弱信号5-10%\n"
    content += "- 仅供参考，理性决策，注意风险\n"
    
    return title, content


def main():
    """主函数：生成并推送期货ETF信号"""
    print("=" * 80)
    print("  期货ETF信号钉钉推送")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()
    
    try:
        # 生成信号
        generator = FuturesETFSignalGenerator()
        signals = generator.get_all_sector_signals()
        
        # 格式化消息
        title, content = format_signal_message(signals)
        
        # 打印预览
        print("=" * 80)
        print("  消息预览")
        print("=" * 80)
        print(f"\n标题：{title}\n")
        print(content)
        print("=" * 80)
        print()
        
        # 检查通知配置
        notification_config = Config.get('notification', {})
        if not notification_config.get('enabled', False):
            print("[WARN] 通知功能未启用")
            return False
        
        provider = notification_config.get('provider', 'dingtalk')
        dingtalk_config = notification_config.get('dingtalk', {})
        webhook_url = dingtalk_config.get('webhook')
        secret_word = dingtalk_config.get('secret_word', '提醒')
        
        if not webhook_url:
            print("[ERROR] 钉钉webhook未配置")
            return False
        
        # 发送推送
        notifier = NotifierFactory.create_notifier(
            provider,
            webhook_url=webhook_url,
            secret_word=secret_word
        )
        
        print("[INFO] 正在发送钉钉通知...")
        success = notifier.send_message(title, content, message_type='futures_etf')
        
        if success:
            print("[OK] 钉钉通知发送成功！")
            return True
        else:
            print("[ERROR] 钉钉通知发送失败")
            return False
        
    except Exception as e:
        logger.error(f"推送失败: {e}", exc_info=True)
        print(f"\n[ERROR] 推送失败: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n[WARN] 用户中断执行")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n[ERROR] 执行过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
