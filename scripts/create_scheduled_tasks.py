#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
创建Windows计划任务
"""

import os
import subprocess
import sys

# 添加项目根目录到路径
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

def create_task(task_name, script_name, time_str, args=""):
    """创建计划任务"""
    # 创建包装批处理文件
    bat_file = os.path.join(project_root, f"task_wrapper_{script_name.replace('.py', '')}.bat")
    with open(bat_file, 'w', encoding='utf-8') as f:
        f.write(f"""@echo off
cd /d {project_root}
python scripts\\{script_name} {args}
""")
    
    # 创建任务
    cmd = [
        'schtasks', '/create',
        '/tn', task_name,
        '/tr', f'"{bat_file}"',
        '/sc', 'daily',
        '/st', time_str,
        '/f'
    ]
    
    print(f"创建任务: {task_name}")
    print(f"命令: {' '.join(cmd)}")
    
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
    
    if result.returncode == 0:
        print(f"[OK] {task_name} 创建成功")
        return True
    else:
        print(f"[ERROR] {task_name} 创建失败: {result.stderr}")
        return False

def create_minutely_task(task_name, script_name, interval_minutes, start_time="09:00"):
    """创建每N分钟循环执行的计划任务"""
    bat_file = os.path.join(project_root, f"task_wrapper_{script_name.replace('.py', '')}.bat")
    with open(bat_file, 'w', encoding='utf-8') as f:
        f.write(f"""@echo off
cd /d {project_root}
python scripts\\{script_name}
""")
    cmd = [
        'schtasks', '/create',
        '/tn', task_name,
        '/tr', f'"{bat_file}"',
        '/sc', 'minute',
        '/mo', str(interval_minutes),
        '/st', start_time,
        '/f'
    ]
    print(f"创建循环任务: {task_name} (每{interval_minutes}分钟)")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
    if result.returncode == 0:
        print(f"[OK] {task_name} 创建成功")
        return True
    else:
        print(f"[ERROR] {task_name} 创建失败: {result.stderr}")
        return False


def main():
    """主函数"""
    print("=" * 60)
    print("创建量化选股系统定时任务")
    print("=" * 60)
    print()

    # 删除旧任务
    print("[步骤1] 删除旧任务...")
    tasks = [
        "量化选股-数据同步",
        "量化选股-早盘推送",
        "量化选股-收盘推送",
        "量化选股-增强数据同步",
        "量化选股-数据质量检查",
        "量化选股-数据质量巡检",
        "量化选股-新闻抓取",
    ]
    for task in tasks:
        subprocess.run(['schtasks', '/delete', '/tn', task, '/f'],
                      capture_output=True, errors='ignore')
    print("[OK] 旧任务已清理")
    print()

    # 创建新任务
    print("[步骤2] 创建新任务...")
    print()

    success_count = 0

    # 原有任务
    if create_task("量化选股-数据同步", "daily_alpha_run.py", "08:00"):
        success_count += 1

    if create_task("量化选股-早盘推送", "morning_push.py", "08:30"):
        success_count += 1

    if create_task("量化选股-收盘推送", "evening_push.py", "16:00"):
        success_count += 1

    # 新增任务：增强数据源同步（收盘后17:00）
    if create_task("量化选股-增强数据同步", "sync_enhanced_data.py", "17:00"):
        success_count += 1

    # 新增任务：数据质量检查（收盘后17:30）
    if create_task("量化选股-数据质量检查", "check_enhanced_data_quality.py", "17:30"):
        success_count += 1

    # 数据质量巡检（每天 17:45，带存库+钉钉告警）
    if create_task("量化选股-数据质量巡检", "check_data_quality.py", "17:45", "--save --alert"):
        success_count += 1

    # 新闻抓取（每30分钟，从8:00开始）
    if create_minutely_task("量化选股-新闻抓取", "fetch_news.py", 30, "08:00"):
        success_count += 1

    print()
    print("=" * 60)
    print(f"任务创建完成: {success_count}/7 成功")
    print("=" * 60)

    # 验证任务
    print()
    print("[步骤3] 验证任务...")
    for task in tasks:
        result = subprocess.run(['schtasks', '/query', '/tn', task],
                              capture_output=True, errors='ignore')
        if result.returncode == 0:
            print(f"[OK] {task} 存在")
        else:
            print(f"[ERROR] {task} 不存在")

if __name__ == '__main__':
    main()
