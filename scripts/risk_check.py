#!/usr/bin/env python3
"""
风控定时检查脚本
每5分钟检查持仓，风控触发自动执行止损
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import time
from datetime import datetime
from src.broker.position_manager import PositionManager

def run_risk_check():
    print("=" * 60)
    print(f"  风控检查 | {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 60)
    
    mgr = PositionManager()
    
    # 1. 显示账户状态
    acc = mgr.get_account_summary()
    pnl_val = acc['total_pnl']
    print(f"\n账户: 总资产{acc['total_assets']:,.0f} 盈亏{pnl_val:+,.0f}")
    print(f"持仓: {acc['position_count']}只 仓位{acc['position_ratio']:.1f}%")
    
    # 2. 检查风控
    risks = mgr.check_risk()
    print(f"\n风控信号: {len(risks)}个")
    
    # 3. 执行止损
    if risks:
        dry_run = "--dry-run" not in sys.argv
        results = mgr.execute_risk(dry_run=dry_run)
        return len([r for r in results if r['success']])
    else:
        print("无风控动作")
        return 0

if __name__ == '__main__':
    dry = "--dry-run" in sys.argv
    mode = "预演模式" if dry else "执行模式"
    print(f"=== 风控检查 | {mode} ===")
    
    count = run_risk_check()
    print(f"\n完成: 执行{count}笔")