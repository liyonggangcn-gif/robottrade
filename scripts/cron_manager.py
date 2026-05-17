#!/usr/bin/env python3
"""
Cron管理器 - 安全管理定时任务
避免覆盖/丢失问题
"""
import subprocess
import os
import sys

CRON_FILE = "/home/li/robottrade/cron_tasks.txt"
BACKUP_FILE = "/home/li/robottrade/cron_backup.txt"


def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout, result.returncode


def get_current_cron():
    """获取当前cron"""
    out, code = run_cmd("crontab -l")
    if code != 0 or "no crontab" in out:
        return ""
    return out


def save_backup(cron_content):
    """保存备份"""
    with open(BACKUP_FILE, 'w') as f:
        f.write(cron_content)
    print(f"[OK] Backup saved to {BACKUP_FILE}")


def load_cron_file():
    """从文件加载cron定义"""
    if os.path.exists(CRON_FILE):
        with open(CRON_FILE, 'r') as f:
            return f.read()
    return ""


def init_cron_file():
    """初始化cron定义文件"""
    current = get_current_cron()
    if current:
        save_backup(current)
    
    default_cron = """# robottrade 定时任务

# === 数据同步 ===
0 8 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/sync_ch_fast.py >> logs/sync_ch.log 2>&1
0 8 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/fast_sync_today.py >> logs/fast_sync.log 2>&1
5 8 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/sync_roe_tushare.py >> logs/sync_roe.log 2>&1
0 17 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/sync_enhanced_data.py >> logs/sync_enhanced.log 2>&1
30 3 * * 0 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/sync_financial_data.py >> logs/sync_financial.log 2>&1

# === 开盘前准备 ===
# 09:00 数据质量检查
0 9 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/daily_quality_check.py >> logs/daily_quality.log 2>&1

# 08:30 A股选股+推送
30 8 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/daily_alpha_run.py >> logs/daily_alpha.log 2>&1
30 8 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/push_all_strategies.py >> logs/all_strategies.log 2>&1

# === 盘中监测 (9-14点每30分钟) ===
0,30 9-14 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/sector_hot_push.py >> logs/sector_hot.log 2>&1

# === 收盘后 ===
0 16 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/evening_push.py >> logs/evening_push.log 2>&1
5 16 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/collect_ai_stocks.py >> logs/ai_stocks.log 2>&1
30 17 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/check_data_quality.py --save --alert >> logs/data_quality.log 2>&1

# === 新闻采集 ===
# 每小时RSS采集
30 * * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/fetch_rsshub.py >> logs/fetch_rsshub.log 2>&1
# 交易时段新闻
0,30 8-15 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/fetch_news.py >> logs/fetch_news.log 2>&1
0 8-21 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/hourly_news.py >> logs/hourly_news.log 2>&1
# 美股新闻
0 9-17 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/hourly_us_news.py >> logs/hourly_us_news.log 2>&1

# === 晚间任务 ===
# 20:00 数据校验
0 20 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/validate_data.py >> logs/validate_data.log 2>&1
# 20:30 美股动量+推送
30 20 * * 1-5 cd /home/li/stock_us && /usr/bin/python3 /home/li/stock_us/momentum_v2.py --feishu >> /home/li/stock_us/logs/momentum.log 2>&1

# === 周任务 ===
30 8 * * 1 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/weekly_small_cap_push.py >> logs/weekly_small_cap.log 2>&1
30 9 * * 1 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/push_etf_strategies.py >> logs/push_etf.log 2>&1

# === LLM Agent (在选股完成后的09:00) ===
0 9 * * 1-5 cd /home/li/robottrade && . venv/bin/activate && python3 scripts/run_trading_agent.py --phase decision >> logs/agent_decision.log 2>&1
30 17 * * 1-5 cd /home/li/stock_us && python3 scripts/sync_us_all.py >> logs/sync_us.log 2>&1
"""
    
    with open(CRON_FILE, 'w') as f:
        f.write(default_cron)
    print(f"[OK] Cron file created: {CRON_FILE}")
    return default_cron


def apply_cron():
    """应用cron到系统"""
    cron_content = load_cron_file()
    if not cron_content:
        print("[ERROR] No cron file to apply")
        return False
    
    # 保存到临时文件
    tmp_file = "/tmp/crontab_tmp.txt"
    with open(tmp_file, 'w') as f:
        f.write(cron_content)
    
    # 应用
    out, code = run_cmd(f"crontab {tmp_file}")
    if code == 0:
        print("[OK] Cron applied successfully")
        return True
    else:
        print(f"[ERROR] {out}")
        return False


def add_task(name, schedule, command):
    """添加任务 - 智能追加"""
    cron = load_cron_file()
    
    # 检查是否已存在
    if command[:50] in cron:
        print(f"[WARN] Task already exists: {name}")
        return False
    
    # 添加新任务
    new_task = f"\n# {name}\n{schedule} {command}\n"
    
    with open(CRON_FILE, 'a') as f:
        f.write(new_task)
    
    print(f"[OK] Added task: {name}")
    return True


def remove_task(keyword):
    """删除任务 - 按关键字"""
    cron = load_cron_file()
    lines = cron.split('\n')
    
    new_lines = []
    removed = 0
    skip = False
    
    for line in lines:
        if keyword in line and not line.strip().startswith('#'):
            skip = True
            removed += 1
            continue
        if skip and line.strip().startswith('#'):
            skip = False
        if not skip:
            new_lines.append(line)
    
    with open(CRON_FILE, 'w') as f:
        f.write('\n'.join(new_lines))
    
    print(f"[OK] Removed {removed} tasks containing '{keyword}'")
    return True


def list_tasks():
    """列出所有任务"""
    cron = load_cron_file()
    print("\n" + "="*60)
    print("当前定时任务")
    print("="*60)
    print(cron)


def main():
    if len(sys.argv) < 2:
        print("""
Cron Manager - 定时任务管理

用法:
  python cron_manager.py init         - 初始化cron文件
  python cron_manager.py apply        - 应用cron到系统
  python cron_manager.py list         - 列出所有任务
  python cron_manager.py add <name> <schedule> <command>  - 添加任务
  python cron_manager.py remove <keyword>  - 删除任务
  python cron_manager.py backup       - 备份当前cron

例子:
  python cron_manager.py init
  python cron_manager.py apply
  python cron_manager.py add "测试任务" "0 * * * *" "echo test"
  python cron_manager.py remove "test"
""")
        return
    
    cmd = sys.argv[1]
    
    if cmd == "init":
        init_cron_file()
    elif cmd == "apply":
        apply_cron()
    elif cmd == "list":
        list_tasks()
    elif cmd == "backup":
        current = get_current_cron()
        save_backup(current)
    elif cmd == "add" and len(sys.argv) >= 5:
        name = sys.argv[2]
        schedule = sys.argv[3]
        command = ' '.join(sys.argv[4:])
        if add_task(name, schedule, command):
            apply_cron()
    elif cmd == "remove" and len(sys.argv) >= 3:
        keyword = sys.argv[2]
        if remove_task(keyword):
            apply_cron()
    else:
        print(f"Unknown command: {cmd}")


if __name__ == '__main__':
    main()