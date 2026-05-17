#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运行期货ETF信号生成器
生成基于期货价格的ETF交易信号
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

logger = init_logger("futures_etf_signal")


def main():
    """主函数：生成期货ETF信号"""
    print("=" * 80)
    print("  期货ETF交易信号生成")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()
    
    try:
        generator = FuturesETFSignalGenerator()
        
        # 获取所有板块信号
        signals = generator.get_all_sector_signals()
        
        print("\n" + "=" * 80)
        print("  板块交易信号")
        print("=" * 80)
        
        for sector, signal_info in signals.items():
            print(f"\n【{sector}】")
            print(f"  信号: {signal_info['signal']}")
            print(f"  强度: {signal_info['strength']:.2f}")
            print(f"  得分: {signal_info['score']:.2f}")
            print(f"  原因: {signal_info['reason']}")
            
            if signal_info['futures_scores']:
                print(f"  期货详情:")
                for futures_name, futures_data in signal_info['futures_scores'].items():
                    print(f"    {futures_name}: {futures_data['change_pct']:+.2f}% (得分: {futures_data['score']:.2f})")
        
        print("\n" + "=" * 80)
        print(f"  [OK] 信号生成完成")
        print(f"  结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)
        
        return True
        
    except Exception as e:
        logger.error(f"信号生成失败: {e}", exc_info=True)
        print(f"\n[ERROR] 信号生成失败: {e}")
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
