#!/usr/bin/env python3
"""
数据质量Agent - 自动监控，LLM决策是否修复
"""
import sys
import os
sys.path.insert(0, '.')

import subprocess
import time
from datetime import datetime

THRESHOLDS = {
    'stock_daily': {'max_delay': 3, 'min_stocks': 4000},
    'stock_factors': {'max_delay': 7, 'min_rows': 1000000},
    'ai_predictions': {'max_delay': 3, 'min_rows': 100000},
    'news_cache': {'min_rows': 5000},
    'financial_data': {'max_delay': 120, 'min_stocks': 500},
}


FIX_COMMANDS = {
    'stock_daily': 'python scripts/fast_sync_today.py',
    'stock_factors': 'python scripts/run_ai_model.py',
    'news_cache': 'python scripts/fetch_rsshub.py',
    'financial_data': 'python scripts/sync_financial_data.py',
}


def run_sql(sql):
    """运行SQL"""
    os.environ['PYTHONPATH'] = '/home/li/robottrade'
    from src.utils.db_utils import DBUtils
    return DBUtils.query_df(sql)


def check_table(table):
    """检查表"""
    status = {'table': table, 'ok': True, 'delay': 0, 'issues': [], 'fixable': False}
    
    try:
        if table == 'stock_daily':
            r = run_sql("SELECT MAX(trade_date) d FROM stock_daily")
            latest = str(r.iloc[0]['d'])[:10]
            delay = (datetime.now().date() - datetime.strptime(latest, '%Y-%m-%d').date()).days
            status['delay'] = delay
            
            r2 = run_sql("SELECT COUNT(DISTINCT ts_code) cnt FROM stock_daily WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)")
            stocks = int(r2.iloc[0]['cnt'])
            
            if delay > 3 or stocks < 4000:
                status['ok'] = False
                status['issues'].append(f"延迟{delay}天/仅{stocks}只")
                status['fixable'] = True
                status['fix_cmd'] = 'fast_sync_today'
        
        elif table == 'stock_factors':
            try:
                r = run_sql("SELECT MAX(trade_date) d FROM stock_factors")
                latest = str(r.iloc[0]['d'])[:10]
                if latest and len(latest) == 8:
                    latest = f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}"
                delay = (datetime.now().date() - datetime.strptime(latest, '%Y-%m-%d').date()).days
                status['delay'] = delay
                
                r2 = run_sql("SELECT COUNT(*) cnt FROM stock_factors")
                rows = int(r2.iloc[0]['cnt'])
                
                if delay > 7 or rows < 1000000:
                    status['ok'] = False
                    status['issues'].append(f"延迟{delay}天/仅{rows}行")
                    status['fixable'] = True
                    status['fix_cmd'] = 'run_ai_model'
            except Exception as e:
                status['ok'] = False
                status['issues'].append(f"检查失败")
        
        elif table == 'news_cache':
            try:
                r = run_sql("SELECT COUNT(*) cnt FROM news_cache WHERE fetched_at > datetime('now', '-2 days')")
                rows = int(r.iloc[0]['cnt'])
                
                if rows < 5000:
                    status['ok'] = False
                    status['issues'].append(f"仅{rows}条")
                    status['fixable'] = True
                    status['fix_cmd'] = 'fetch_rsshub'
            except:
                pass
        
        elif table == 'financial_data':
            try:
                r = run_sql("SELECT MAX(ann_date) d FROM financial_data")
                latest = str(r.iloc[0]['d'])[:10]
                if latest and len(latest) == 8:
                    latest = f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}"
                delay = (datetime.now().date() - datetime.strptime(latest, '%Y-%m-%d').date()).days
                status['delay'] = delay
                
                r2 = run_sql("SELECT COUNT(DISTINCT ts_code) cnt FROM financial_data")
                stocks = int(r2.iloc[0]['cnt'])
                
                if delay > 120 or stocks < 500:
                    status['ok'] = False
                    status['issues'].append(f"延迟{delay}天/仅{stocks}只")
            except Exception as e:
                status['ok'] = False
                status['issues'].append(f"检查失败")

    except Exception as e:
        status['ok'] = False
        status['issues'].append(str(e)[:30])
    
    return status


def auto_fix(fix_key):
    """自动修复"""
    print(f"[Fix] {fix_key}...")
    
    cmds = {
        'fast_sync_today': 'python scripts/fast_sync_today.py',
        'run_ai_model': 'python scripts/run_ai_model.py --factors-only',
        'fetch_rsshub': 'python scripts/fetch_rsshub.py',
    }
    
    cmd = cmds.get(fix_key, '')
    if not cmd:
        return False
    
    try:
        result = subprocess.run(
            f"cd /home/li/robottrade && . venv/bin/activate && {cmd}",
            shell=True, capture_output=True, timeout=180, text=True
        )
        return result.returncode == 0
    except:
        return False


def ask_llm_fix(results):
    """让LLM决定是否修复"""
    from src.utils.llm_client import LLMClient
    
    # 收集问题
    issues = [r for r in results if not r['ok']]
    if not issues:
        return results
    
    # 构建问题描述
    issue_text = "\n".join([
        f"- {r['table']}: {r['issues'][0] if r['issues'] else '未知问题'}"
        for r in issues
    ])
    
    # 让LLM决策
    prompt = f"""你是一个数据运维专家。以下数据表有问题，请决定是否需要立即修复：

{issue_text}

可修复命令：
- stock_daily: fast_sync_today.py (快速同步今日数据，2分钟)
- stock_factors: run_ai_model.py (运行AI模型计算因子，30分钟)
- news_cache: fetch_rsshub.py (抓取财经新闻，1分钟)

请回答：
1. 需要修复哪些表？（列出表名）
2. 修复优先级？（高/中/低）

直接回答，不需要解释。"""

    try:
        llm = LLMClient()
        if llm.is_available():
            decision = llm._call_llm(
                system_prompt="你是数据运维专家",
                user_prompt=prompt,
                temperature=0.2,
                max_tokens=100
            )
            print(f"[LLM决策] {decision[:200]}")
            
            # 根据LLM决策执行修复
            for r in issues:
                if r['table'] in decision and r.get('fixable'):
                    fix_key = r.get('fix_cmd', r['table'])
                    cmd = FIX_COMMANDS.get(fix_key, '')
                    if cmd:
                        print(f"[执行修复] {r['table']}...")
                        ok = auto_fix(cmd)
                        if ok:
                            r['fixed'] = True
                            print(f"  ✓ 修复成功")
    except Exception as e:
        print(f"[LLM错误] {e}")
    
    return results


def main():
    print("="*40)
    print("数据质量Agent")
    print("="*40)
    
    tables = ['stock_daily', 'stock_factors', 'news_cache', 'financial_data']
    results = []
    
    for t in tables:
        print(f"[Check] {t}...")
        s = check_table(t)
        results.append(s)
        
        if s['ok']:
            print(f"  OK")
        else:
            print(f"  异常: {s['issues']}")
    
    # LLM决策是否修复
    print("\n[LLM决策] 询问是否修复...")
    results = ask_llm_fix(results)
    
    # 统计
    ok_cnt = sum(1 for r in results if r['ok'])
    fail_cnt = len(results) - ok_cnt
    fixed_cnt = sum(1 for r in results if r.get('fixed'))
    
    # 发送钉钉
    lines = [f"## 数据质量Agent ({datetime.now().strftime('%m-%d %H:%M')})", ""]
    lines.append(f"正常: {ok_cnt} | 异常: {fail_cnt} | 已修复: {fixed_cnt}")
    lines.append("")
    
    for r in results:
        icon = "✅" if r['ok'] else ("🔧" if r.get('fixed') else "❌")
        line = f"{icon} {r['table']}"
        if r['delay'] > 0:
            line += f" 延迟{r['delay']}天"
        if r['issues']:
            line += f" {r['issues'][0]}"
        if r.get('fixed'):
            line += " [已修复]"
        lines.append(line)
    
    content = "\n".join(lines)
    
    if fail_cnt > 0 or fixed_cnt > 0:
        from src.utils.notifier import send_alert
        send_alert("数据质量Agent", content, message_type='morning')
        print("[PUSH] 已发送")
    
    return results


if __name__ == '__main__':
    main()