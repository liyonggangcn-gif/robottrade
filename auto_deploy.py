#!/usr/bin/env python3
"""
自动化部署脚本
功能：将本地代码自动部署到Linux服务器
"""

import os
import sys
import time
import subprocess
import argparse

# 配置参数
DEFAULT_CONFIG = {
    'local_project_path': os.path.dirname(os.path.abspath(__file__)),
    'remote_host': '192.168.3.22',
    'remote_user': 'li',
    'remote_project_path': '/home/li/robottrade',
    'ssh_port': 22,
    'python_version': 'python3'
}

def run_command(cmd, cwd=None, capture_output=True):
    """运行命令并返回结果"""
    print(f"执行命令: {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=capture_output, text=True)
    if result.returncode != 0:
        print(f"命令执行失败: {result.stderr}")
        return False, result
    return True, result

def deploy_code(local_path, remote_host, remote_user, remote_path, ssh_port=22):
    """部署代码到远程服务器"""
    print(f"\n=== 部署代码到 {remote_host} ===")
    
    # 构建scp命令（使用免密登录）
    scp_cmd = f"scp -i C:/Users/{os.environ.get('USERNAME')}/.ssh/stock_deploy -r -P {ssh_port} {local_path}/src {local_path}/config {local_path}/sync_concepts_with_resume.py {remote_user}@{remote_host}:{remote_path}/"
    
    # 执行部署
    success, result = run_command(scp_cmd)
    if not success:
        print("❌ 代码部署失败")
        return False
    
    print("✅ 代码部署成功")
    return True

def install_dependencies(remote_host, remote_user, remote_path, ssh_port=22):
    """在远程服务器安装依赖"""
    print(f"\n=== 安装依赖 ===")
    
    # 构建SSH命令（使用免密登录）- 创建虚拟环境并安装依赖
    ssh_cmd = f'ssh -i C:/Users/{os.environ.get("USERNAME")}/.ssh/stock_deploy -p {ssh_port} {remote_user}@{remote_host} "cd {remote_path} && {DEFAULT_CONFIG["python_version"]} -m venv venv && source venv/bin/activate && pip install pandas pymysql tushare tqdm"'
    
    # 执行命令
    success, result = run_command(ssh_cmd)
    if not success:
        print("❌ 依赖安装失败")
        return False
    
    print("✅ 依赖安装成功")
    return True

def setup_cron(remote_host, remote_user, remote_path, ssh_port=22):
    """设置Cron任务"""
    print(f"\n=== 设置Cron任务 ===")
    
    # 创建板块数据同步脚本
    sync_script = f"""
#!/bin/bash

# 运行板块数据同步脚本
echo "=== 运行板块数据同步 ==="
echo "开始时间: $(date)"

# 进入项目目录
cd {remote_path}

# 激活虚拟环境并运行同步脚本
source venv/bin/activate
python sync_concepts_with_resume.py

echo "结束时间: $(date)"
echo "同步完成"
"""
    
    # 创建选股脚本
    stock_selection_script = f"""
#!/bin/bash

# 运行选股程序
echo "=== 运行选股程序 ==="
echo "开始时间: $(date)"

# 进入项目目录
cd {remote_path}

# 激活虚拟环境并运行选股脚本
source venv/bin/activate
python scripts/run_stock_selection.py

echo "结束时间: $(date)"
echo "选股完成"
"""
    
    # 创建收盘推送脚本
    evening_push_script = f"""
#!/bin/bash

# 运行收盘推送
echo "=== 运行收盘推送 ==="
echo "开始时间: $(date)"

# 进入项目目录
cd {remote_path}

# 激活虚拟环境并运行推送脚本
source venv/bin/activate
python scripts/evening_push.py

echo "结束时间: $(date)"
echo "推送完成"
"""
    
    # 构建SSH命令（使用免密登录）
    ssh_cmds = [
        f'ssh -i C:/Users/{os.environ.get("USERNAME")}/.ssh/stock_deploy -p {ssh_port} {remote_user}@{remote_host} "echo \"{sync_script}\" > ~/run_sync_concepts.sh && chmod +x ~/run_sync_concepts.sh"',
        f'ssh -i C:/Users/{os.environ.get("USERNAME")}/.ssh/stock_deploy -p {ssh_port} {remote_user}@{remote_host} "echo \"{stock_selection_script}\" > ~/run_stock_selection.sh && chmod +x ~/run_stock_selection.sh"',
        f'ssh -i C:/Users/{os.environ.get("USERNAME")}/.ssh/stock_deploy -p {ssh_port} {remote_user}@{remote_host} "echo \"{evening_push_script}\" > ~/run_evening_push.sh && chmod +x ~/run_evening_push.sh"'
    ]
    
    # 执行命令
    for cmd in ssh_cmds:
        success, result = run_command(cmd)
        if not success:
            print("❌ 脚本创建失败")
            return False
    
    # 设置Cron任务（使用免密登录）
    cron_cmd = f'ssh -i "C:/Users/{os.environ.get("USERNAME")}/.ssh/stock_deploy" -p {ssh_port} {remote_user}@{remote_host} "(crontab -l 2>/dev/null; echo \"0 2 * * 0 ~/run_sync_concepts.sh >> ~/sync_concepts.log 2>&1\") | crontab -"'
    cron_cmd_8am = f'ssh -i "C:/Users/{os.environ.get("USERNAME")}/.ssh/stock_deploy" -p {ssh_port} {remote_user}@{remote_host} "(crontab -l 2>/dev/null; echo \"0 8 * * 1-5 ~/run_stock_selection.sh >> ~/stock_selection.log 2>&1\") | crontab -"'
    cron_cmd_4pm = f'ssh -i "C:/Users/{os.environ.get("USERNAME")}/.ssh/stock_deploy" -p {ssh_port} {remote_user}@{remote_host} "(crontab -l 2>/dev/null; echo \"0 16 * * 1-5 ~/run_evening_push.sh >> ~/evening_push.log 2>&1\") | crontab -"'
    
    for cmd in [cron_cmd, cron_cmd_8am, cron_cmd_4pm]:
        success, result = run_command(cmd)
        if not success:
            print("❌ Cron任务设置失败")
            return False
    
    print("✅ Cron任务设置成功")
    return True

def verify_deployment(remote_host, remote_user, ssh_port=22):
    """验证部署结果"""
    print(f"\n=== 验证部署结果 ===")
    
    # 检查Cron任务（使用免密登录）
    cron_cmd = f"ssh -i C:/Users/{os.environ.get('USERNAME')}/.ssh/stock_deploy -p {ssh_port} {remote_user}@{remote_host} crontab -l"
    
    success, result = run_command(cron_cmd)
    if not success:
        print("❌ 无法检查Cron任务")
        return False
    
    print("当前Cron任务:")
    print(result.stdout)
    
    # 检查文件是否存在（使用免密登录）
    check_cmd = f"ssh -i C:/Users/{os.environ.get('USERNAME')}/.ssh/stock_deploy -p {ssh_port} {remote_user}@{remote_host} ls -la ~/run_sync_concepts.sh"
    
    success, result = run_command(check_cmd)
    if not success:
        print("❌ 启动脚本不存在")
        return False
    
    print("✅ 部署验证成功")
    return True

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='自动化部署脚本')
    parser.add_argument('--host', help='远程服务器IP地址', default=DEFAULT_CONFIG['remote_host'])
    parser.add_argument('--user', help='远程服务器用户名', default=DEFAULT_CONFIG['remote_user'])
    parser.add_argument('--port', type=int, help='SSH端口', default=DEFAULT_CONFIG['ssh_port'])
    parser.add_argument('--remote-path', help='远程项目路径', default=DEFAULT_CONFIG['remote_project_path'])
    parser.add_argument('--local-path', help='本地项目路径', default=DEFAULT_CONFIG['local_project_path'])
    parser.add_argument('--sync-only', action='store_true', help='仅同步代码，不安装依赖和设置Cron')
    
    args = parser.parse_args()
    
    print("=== 自动化部署开始 ===")
    print(f"本地路径: {args.local_path}")
    print(f"远程服务器: {args.user}@{args.host}:{args.port}")
    print(f"远程路径: {args.remote_path}")
    
    # 1. 部署代码
    if not deploy_code(args.local_path, args.host, args.user, args.remote_path, args.port):
        return False
    
    # 2. 安装依赖（如果不是仅同步）
    if not args.sync_only:
        if not install_dependencies(args.host, args.user, args.remote_path, args.port):
            return False
        
        # 3. 设置Cron任务
        if not setup_cron(args.host, args.user, args.remote_path, args.port):
            return False
    
    # 4. 验证部署
    if not verify_deployment(args.host, args.user, args.port):
        return False
    
    print("\n=== 自动化部署完成 ===")
    print("部署成功！代码已同步到远程服务器")
    if not args.sync_only:
        print("依赖已安装，Cron任务已设置")
    print("使用方法:")
    print(f"1. 手动运行: ssh {args.user}@{args.host} 'bash ~/run_sync_concepts.sh'")
    print(f"2. 查看日志: ssh {args.user}@{args.host} 'tail -f ~/sync_concepts.log'")
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
