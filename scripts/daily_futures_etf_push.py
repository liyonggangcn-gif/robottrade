#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日期货ETF信号推送脚本
整合期货数据同步、信号生成和钉钉推送
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
from src.collector.futures_collector import FuturesCollector
from src.analysis.futures_etf_signal import FuturesETFSignalGenerator
from src.utils.config_loader import Config
from src.utils.notifier import NotifierFactory

logger = init_logger("daily_futures_etf_push")


def main():
    """主函数：同步数据、生成信号、推送通知"""
    print("=" * 80)
    print("  每日期货ETF信号推送")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()
    
    results = {}
    
    # 步骤1: 同步期货数据
    print("[步骤1] 同步期货数据...")
    try:
        collector = FuturesCollector()
        sync_results = collector.sync_futures_data()
        results['sync'] = sync_results['success_count'] > 0
        print(f"[OK] 同步完成: 成功{sync_results['success_count']}个, 失败{sync_results['fail_count']}个")
    except Exception as e:
        logger.error(f"期货数据同步失败: {e}", exc_info=True)
        results['sync'] = False
        print(f"[ERROR] 期货数据同步失败: {e}")
    
    print()
    
    # 步骤2: 生成交易信号
    print("[步骤2] 生成交易信号...")
    try:
        generator = FuturesETFSignalGenerator()
        signals = generator.get_all_sector_signals()
        results['signals'] = signals
        print(f"[OK] 信号生成完成: {len(signals)} 个板块")
    except Exception as e:
        logger.error(f"信号生成失败: {e}", exc_info=True)
        results['signals'] = {}
        print(f"[ERROR] 信号生成失败: {e}")
    
    print()
    
    # 步骤3: 推送钉钉通知
    print("[步骤3] 推送钉钉通知...")
    try:
        from scripts.push_futures_etf_signal import format_signal_message
        
        if results.get('signals'):
            title, content = format_signal_message(results['signals'])
            
            # 检查通知配置
            notification_config = Config.get('notification', {})
            if not notification_config.get('enabled', False):
                print("[WARN] 通知功能未启用")
                results['push'] = False
            else:
                provider = notification_config.get('provider', 'dingtalk')
                dingtalk_config = notification_config.get('dingtalk', {})
                webhook_url = dingtalk_config.get('webhook')
                secret_word = dingtalk_config.get('secret_word', '提醒')
                
                if not webhook_url:
                    print("[ERROR] 钉钉webhook未配置")
                    results['push'] = False
                else:
                    notifier = NotifierFactory.create_notifier(
                        provider,
                        webhook_url=webhook_url,
                        secret_word=secret_word
                    )
                    
                    success = notifier.send_message(title, content)
                    results['push'] = success
                    
                    if success:
                        print("[OK] 钉钉通知发送成功")
                    else:
                        print("[ERROR] 钉钉通知发送失败")
        else:
            print("[WARN] 无信号数据，跳过推送")
            results['push'] = False
    except Exception as e:
        logger.error(f"推送失败: {e}", exc_info=True)
        results['push'] = False
        print(f"[ERROR] 推送失败: {e}")
    
    print()
    print("=" * 80)
    print("  执行总结")
    print("=" * 80)
    print(f"数据同步: {'[OK] 成功' if results.get('sync') else '[ERROR] 失败'}")
    print(f"信号生成: {'[OK] 成功' if results.get('signals') else '[ERROR] 失败'}")
    print(f"钉钉推送: {'[OK] 成功' if results.get('push') else '[ERROR] 失败'}")
    print("=" * 80)
    print(f"  结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    return results.get('push', False)


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
