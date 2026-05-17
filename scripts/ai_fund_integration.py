#!/usr/bin/env python3
"""
AI分析集成脚本 - 并行调用ai-hedge-fund分析选股结果
方案B: 创建独立集成脚本
"""
import os
import sys
import subprocess
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

AI_FUND_DIR = '/home/li/ai_fund/ai-hedge-fund'

# 添加robottrade路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from src.utils.db_utils import DBUtils
from src.strategy.center import StrategyCenter
from src.utils.notifier import send_alert


def analyze_stock_concurrent(ts_code):
    """并行分析单只股票（调用服务器AI）"""
    try:
        # 通过SSH在服务器上运行AI分析
        cmd = f"cd /home/li/ai_fund/ai-hedge-fund && /home/li/robottrade/venv/bin/python run_cn.py {ts_code}"
        
        env = os.environ.copy()
        env['DEEPSEEK_API_KEY'] = 'sk-e4cd8339e40c42cb9275d6a16e0f56a1'
        
        # 使用subprocess通过ssh调用
        result = subprocess.run(
            ['ssh', 'li@192.168.3.22', cmd],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            timeout=300,
            env=env
        )
        
        if result.returncode == 0:
            return {'ts_code': ts_code, 'success': True, 'output': result.stdout}
        else:
            return {'ts_code': ts_code, 'success': False, 'error': result.stderr}
    except Exception as e:
        return {'ts_code': ts_code, 'success': False, 'error': str(e)}

def parse_ai_fund_output(output):
    """解析ai-hedge-fund输出，提取关键信息"""
    lines = output.split('\n')
    result = {
        'decision': 'hold',
        'reason': '',
        'agents': {}
    }
    
    # 查找决策结果
    for i, line in enumerate(lines):
        if 'Portfolio Manager' in line or '最终决策' in line:
            # 提取决策
            if 'buy' in line.lower() or '做多' in line:
                result['decision'] = 'buy'
            elif 'sell' in line.lower() or '做空' in line:
                result['decision'] = 'sell'
            else:
                result['decision'] = 'hold'
            
            # 提取后续几行作为理由
            result['reason'] = '\n'.join(lines[i:i+5])
            break
    
    return result

def run_ai_fund_integration():
    """主函数: 获取选股结果 → 并行AI分析 → 生成报告 → 推送"""
    print("=" * 60)
    print("AI分析集成 - 开始")
    print("=" * 60)
    
    # Step 1: 获取策略选股结果
    print("\n[Step 1] 获取策略选股结果...")
    try:
        sc = StrategyCenter(enable_macro=False, notify=False)
        picks = sc.run(['hybrid', 'dividend', 'value'], top_k=10)
        
        if picks is None or len(picks) == 0:
            print("  无选股结果，跳过AI分析")
            return
        
        stock_codes = picks['ts_code'].tolist()[:10]
        print(f"  获取到 {len(stock_codes)} 只股票")
    except Exception as e:
        print(f"  获取选股失败: {e}")
        return
    
    # Step 2: 并行调用ai-hedge-fund分析
    print("\n[Step 2] 并行调用AI分析...")
    results = []
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(analyze_stock_concurrent, code): code for code in stock_codes}
        
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            ts_code = result['ts_code']
            status = '✅' if result['success'] else '❌'
            print(f"  {status} {ts_code}")
    
    # Step 3: 解析结果并生成报告
    print("\n[Step 3] 生成分析报告...")
    buy_signals = []
    hold_signals = []
    
    for r in results:
        if r['success']:
            parsed = parse_ai_fund_output(r['output'])
            if parsed['decision'] == 'buy':
                buy_signals.append(r['ts_code'])
            else:
                hold_signals.append(r['ts_code'])
    
    # Step 4: 生成钉钉消息并推送
    print("\n[Step 4] 推送钉钉...")
    
    if buy_signals:
        report = "## 🤖 AI大师选股分析\n\n"
        report += f"**买入信号 ({len(buy_signals)}只)**\n"
        for code in buy_signals:
            report += f"- {code}\n"
        
        if hold_signals:
            report += f"\n**持有观察 ({len(hold_signals)}只)**\n"
            for code in hold_signals:
                report += f"- {code}\n"
        
        send_alert("AI选股分析报告", report, "ai_selection")
        print(f"  已推送买入信号: {len(buy_signals)} 只")
    
    print("\n完成!")

if __name__ == '__main__':
    run_ai_fund_integration()