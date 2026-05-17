#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动所有服务并运行今日选股
整合数据同步、因子更新、选股策略、交易信号生成和Web界面启动
"""

import os
import sys
import subprocess
import time
import argparse
from datetime import datetime

# 添加项目根目录到路径
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

# 强制清除代理（必须在 import 数据加载器之前）
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

# 初始化日志
from src.utils.log_utils import init_logger
logger = init_logger("start_all_services")

from src.collector.data_loader import UniversalDataLoader
from src.factors.alpha_engine import AlphaEngine
from src.strategy.topk_strategy import TopKStrategy
from src.strategy.small_cap_jinx import SmallCapJinxStrategy
from src.strategy.trade_manager import TradeManager
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils
import pandas as pd

def print_section(title):
    """打印分隔线"""
    print("\n" + "="*80)
    print(f"  {title}")
    print("="*80 + "\n")

def check_data_freshness():
    """检查数据新鲜度"""
    try:
        result = DBUtils.query_df('SELECT MAX(trade_date) as max_date FROM stock_daily')
        if result.empty or pd.isna(result.iloc[0]['max_date']):
            return None, None
        
        latest_date = result.iloc[0]['max_date']
        latest_date_dt = pd.to_datetime(latest_date)
        today = pd.Timestamp.now().normalize()
        days_diff = (today - latest_date_dt).days
        
        return latest_date, days_diff
    except Exception as e:
        print(f"[WARN] 检查数据新鲜度失败: {e}")
        return None, None

def sync_latest_data():
    """步骤1: 同步最新数据（如果需要）"""
    print_section("步骤1: 检查并同步最新日线数据")
    
    # 先检查数据新鲜度
    latest_date, days_diff = check_data_freshness()
    
    if latest_date:
        print(f"数据库最新交易日: {latest_date}")
        if days_diff is not None:
            if days_diff <= 1:
                print(f"[INFO] 数据已是最新（{days_diff}天前），跳过同步")
                return True
            else:
                print(f"[INFO] 数据已过期 {days_diff} 天，需要同步")
    else:
        print("[INFO] 数据库中没有数据，需要同步")
    
    try:
        # 检查并同步交易日历（如果表为空）
        try:
            from src.utils.db_utils import DBUtils
            calendar_check = DBUtils.query_df("SELECT COUNT(*) as cnt FROM trade_calendar")
            if calendar_check.empty or calendar_check.iloc[0]['cnt'] == 0:
                print("[INFO] trade_calendar 表为空，正在同步交易日历...")
                try:
                    from scripts.sync_tushare_data import TushareDataSync
                    sync_tool = TushareDataSync()
                    # 同步最近2年的交易日历
                    from datetime import datetime, timedelta
                    start_date = (datetime.now() - timedelta(days=730)).strftime('%Y%m%d')
                    sync_tool.sync_trade_calendar(start_date=start_date)
                    print("[OK] 交易日历同步完成")
                except Exception as e:
                    print(f"[WARN] 交易日历同步失败: {e}，将使用简单规则判断")
        except Exception as e:
            print(f"[WARN] 检查交易日历表失败: {e}")
        
        loader = UniversalDataLoader()
        print("正在同步最新日线数据...")
        # 检查数据质量，决定是否需要全量同步
        from src.utils.data_quality_monitor import DataQualityMonitor
        monitor = DataQualityMonitor()
        quality_report = monitor.check_latest_data_quality()
        
        if quality_report and not quality_report['is_acceptable']:
            print(f"[WARN] 数据质量不佳（评分: {quality_report['quality_score']:.1f}/100），执行全量同步")
            loader.sync_daily_data(limit=None, batch_size=100, full_market=True)
        else:
            # 增量同步
            loader.sync_daily_data(limit=None, batch_size=100, full_market=False)
        loader.close()
        print("[OK] 数据同步完成")
        return True
    except Exception as e:
        print(f"[ERROR] 数据同步失败: {e}")
        return False

def update_factors():
    """步骤2: 更新因子数据"""
    print_section("步骤2: 更新Alpha因子数据")
    
    try:
        engine = AlphaEngine()
        print("正在计算并更新因子...")
        engine.update_factors()
        engine.close()
        print("[OK] 因子更新完成")
        return True
    except Exception as e:
        print(f"[ERROR] 因子更新失败: {e}")
        return False

def run_topk_selection():
    """步骤3: 运行TopK策略选股"""
    print_section("步骤3: 运行TopK策略选股")
    
    try:
        strategy = TopKStrategy(read_only=True)
        latest_date = strategy.get_latest_trade_date()
        
        if not latest_date:
            print("[ERROR] 无法获取最新交易日")
            strategy.close()
            return None
        
        print(f"最新交易日: {latest_date}")
        print("正在执行TopK策略选股...")
        
        top_stocks = strategy.get_top_stocks(latest_date, top_k=10)
        strategy.close()
        
        if top_stocks is None or top_stocks.empty:
            print("[ERROR] TopK策略未选出股票")
            return None
        
        print(f"[OK] TopK策略成功选出 {len(top_stocks)} 只股票")
        print("\nTopK策略选股结果:")
        print(top_stocks[['ts_code', 'name', 'score', 'close']].to_string(index=False))
        
        return top_stocks
    except Exception as e:
        print(f"[ERROR] TopK策略执行失败: {e}")
        return None

def run_smallcap_selection():
    """步骤4: 运行小市值策略选股"""
    print_section("步骤4: 运行小市值+行业冥灯策略选股")
    
    try:
        strategy = SmallCapJinxStrategy(read_only=True)
        latest_date = strategy.get_latest_trade_date()
        
        if not latest_date:
            print("[ERROR] 无法获取最新交易日")
            strategy.close()
            return None
        
        print(f"最新交易日: {latest_date}")
        print("正在执行小市值策略选股...")
        
        top_stocks = strategy.get_top_stocks(latest_date, top_k=10)
        strategy.close()
        
        if top_stocks.empty:
            print("[WARN] 小市值策略未选出股票（可能是过滤条件触发）")
            return None
        
        print(f"[OK] 小市值策略成功选出 {len(top_stocks)} 只股票")
        print("\n小市值策略选股结果:")
        display_cols = ['ts_code', 'name', 'industry', 'total_mv', 'close']
        available_cols = [col for col in display_cols if col in top_stocks.columns]
        print(top_stocks[available_cols].to_string(index=False))
        
        return top_stocks
    except Exception as e:
        print(f"[ERROR] 小市值策略执行失败: {e}")
        return None

def generate_trade_signals():
    """步骤5: 生成交易信号并发送通知"""
    print_section("步骤5: 生成交易信号并发送通知")
    
    try:
        # 使用TopK策略生成信号
        trade_manager = TradeManager(strategy_type='topk', read_only=False)
        
        print("正在生成交易信号...")
        signals = trade_manager.run_daily_check(top_k=10)
        
        print(f"\n[OK] 成功生成 {len(signals)} 条交易信号")
        
        # 打印信号摘要
        if signals:
            buy_signals = [s for s in signals if s['type'] == 'BUY']
            sell_signals = [s for s in signals if s['type'] == 'SELL']
            hold_signals = [s for s in signals if s['type'] == 'HOLD']
            
            print(f"\n信号统计:")
            print(f"  - 买入信号: {len(buy_signals)} 条")
            print(f"  - 卖出信号: {len(sell_signals)} 条")
            print(f"  - 持仓信号: {len(hold_signals)} 条")
        
        # 打印持仓摘要
        trade_manager.print_summary()
        
        trade_manager.close()
        return signals
    except Exception as e:
        print(f"[ERROR] 交易信号生成失败: {e}")
        import traceback
        traceback.print_exc()
        return None

def sync_futures_and_generate_signals():
    """步骤6: 同步期货数据并生成ETF信号"""
    print_section("步骤6: 同步期货数据并生成ETF信号")
    
    try:
        # 检查是否启用期货ETF功能
        futures_config = Config.get('futures_etf', {})
        if not futures_config.get('enabled', True):
            print("[INFO] 期货ETF功能未启用，跳过")
            return True
        
        # 同步期货数据
        print("[期货ETF] 同步期货数据...")
        from src.collector.futures_collector import FuturesCollector
        collector = FuturesCollector()
        sync_results = collector.sync_futures_data()
        
        if sync_results['success_count'] > 0:
            print(f"[OK] 期货数据同步完成: 成功{sync_results['success_count']}个")
        else:
            print(f"[WARN] 期货数据同步失败: 失败{sync_results['fail_count']}个")
            return False
        
        # 生成ETF信号
        print("[期货ETF] 生成交易信号...")
        from src.analysis.futures_etf_signal import FuturesETFSignalGenerator
        generator = FuturesETFSignalGenerator()
        signals = generator.get_all_sector_signals()
        
        # 打印信号摘要
        buy_count = sum(1 for s in signals.values() if s['signal'] == 'BUY')
        sell_count = sum(1 for s in signals.values() if s['signal'] == 'SELL')
        hold_count = sum(1 for s in signals.values() if s['signal'] == 'HOLD')
        
        print(f"[OK] ETF信号生成完成:")
        print(f"  - 买入信号: {buy_count} 个")
        print(f"  - 卖出信号: {sell_count} 个")
        print(f"  - 持有信号: {hold_count} 个")
        
        # 显示主要信号
        for sector, signal_info in signals.items():
            if signal_info['signal'] != 'HOLD':
                print(f"  - {sector}: {signal_info['signal']} (强度: {signal_info['strength']:.1%}, 持仓周期: {signal_info['holding_period']})")
        
        return True
    except Exception as e:
        print(f"[WARN] 期货ETF信号生成失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def start_dashboard():
    """步骤7: 启动Streamlit Dashboard"""
    print_section("步骤7: 启动Streamlit Dashboard Web界面")
    
    try:
        # 获取项目根目录（scripts的父目录）
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(script_dir)
        dashboard_path = os.path.join(project_root, 'src', 'app', 'dashboard.py')
        
        port = Config.get('streamlit.port', 8501)
        host = Config.get('streamlit.host', 'localhost')
        
        print(f"正在启动Streamlit Dashboard...")
        print(f"项目根目录: {project_root}")
        print(f"Dashboard路径: {dashboard_path}")
        print(f"访问地址: http://{host}:{port}")
        
        # 启动Streamlit（后台运行）
        cmd = [
            sys.executable, '-m', 'streamlit', 'run', dashboard_path,
            '--server.port', str(port),
            '--server.address', host,
            '--server.headless', 'true'
        ]
        
        # 在Windows上使用start命令在新窗口启动
        if sys.platform == 'win32':
            # 使用start命令在新窗口启动，更可靠
            start_cmd = f'start "Streamlit Dashboard" cmd /k "cd /d {project_root} && {" ".join(cmd)}"'
            os.system(start_cmd)
            # 等待一下确保进程启动
            time.sleep(2)
        else:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=project_root
            )
        
        print(f"[OK] Streamlit Dashboard 已在新窗口中启动")
        print(f"   请在浏览器中访问: http://{host}:{port}")
        print(f"   如果无法访问，请检查新打开的窗口是否有错误信息")
        return True
    except Exception as e:
        print(f"[ERROR] Streamlit Dashboard 启动失败: {e}")
        return False

def check_and_fix_services():
    """步骤0: 检查并修复服务（可选）"""
    print_section("步骤0: 检查并修复服务状态")
    
    try:
        # 检查定时任务是否存在
        def check_scheduled_task(task_name):
            """检查Windows计划任务状态"""
            try:
                result = subprocess.run(
                    ['schtasks', '/query', '/tn', task_name, '/fo', 'LIST'],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='ignore'
                )
                return result.returncode == 0
            except Exception:
                return False
        
        tasks = [
            ("量化选股-数据同步", "数据同步任务", "daily_alpha_run.py", "08:00"),
            ("量化选股-早盘推送", "早盘推送任务", "morning_push.py", "08:30"),
            ("量化选股-收盘推送", "收盘推送任务", "evening_push.py", "16:00"),
        ]
        
        missing_tasks = []
        for task_name, task_desc, script_name, time_str in tasks:
            exists = check_scheduled_task(task_name)
            if exists:
                print(f"[OK] {task_desc}: 存在")
            else:
                print(f"[WARN] {task_desc}: 不存在")
                missing_tasks.append((task_name, task_desc, script_name, time_str))
        
        # 如果有缺失的任务，自动创建
        if missing_tasks:
            print(f"\n[INFO] 发现 {len(missing_tasks)} 个缺失的定时任务")
            print("[INFO] 正在自动创建缺失的定时任务...")
            
            try:
                # 导入创建任务的函数
                from scripts.create_scheduled_tasks import create_task
                
                # 创建缺失的任务
                for task_name, task_desc, script_name, time_str in missing_tasks:
                    try:
                        if create_task(task_name, script_name, time_str):
                            print(f"[OK] {task_desc} 创建成功")
                        else:
                            print(f"[WARN] {task_desc} 创建失败")
                    except Exception as e:
                        print(f"[WARN] {task_desc} 创建失败: {e}")
                
                print("[OK] 定时任务创建完成")
            except Exception as e:
                print(f"[WARN] 自动创建定时任务失败: {e}")
                print("[INFO] 可以稍后手动运行: python scripts\\create_scheduled_tasks.py")
        else:
            print("[OK] 所有定时任务正常")
        
        return True
    except Exception as e:
        print(f"[WARN] 服务检查失败: {e}，继续执行...")
        import traceback
        traceback.print_exc()
        return False

def main():
    """主函数：执行所有步骤"""
    print("\n" + "="*80)
    print("  启动所有服务并运行今日选股")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)
    
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='启动所有服务并运行今日选股')
    parser.add_argument('--check-services', action='store_true', 
                       help='启动前检查并修复服务状态（定时任务等）')
    parser.add_argument('--skip-check', action='store_true',
                       help='跳过服务检查')
    args, unknown = parser.parse_known_args()
    
    results = {}
    
    # 步骤0: 检查并修复服务（可选，可通过参数控制）
    if args.check_services and not args.skip_check:
        results['service_check'] = check_and_fix_services()
    
    # 步骤1: 数据同步
    results['data_sync'] = sync_latest_data()
    if not results['data_sync']:
        print("\n[WARN] 数据同步失败，但继续执行后续步骤...")
    
    # 步骤2: 因子更新
    results['factor_update'] = update_factors()
    if not results['factor_update']:
        print("\n[WARN] 因子更新失败，但继续执行后续步骤...")
    
    # 步骤3: TopK策略选股
    results['topk_stocks'] = run_topk_selection()
    
    # 步骤4: 小市值策略选股
    results['smallcap_stocks'] = run_smallcap_selection()
    
    # 步骤5: 生成交易信号
    results['trade_signals'] = generate_trade_signals()
    
    # 步骤6: 同步期货数据并生成ETF信号（可选）
    results['futures_etf'] = sync_futures_and_generate_signals()
    
    # 步骤7: 启动Web界面
    results['dashboard'] = start_dashboard()
    
    # 总结
    print_section("执行总结")
    if 'service_check' in results:
        print(f"服务检查: {'[OK] 完成' if results['service_check'] else '[WARN] 部分失败'}")
    print(f"数据同步: {'[OK] 成功' if results['data_sync'] else '[ERROR] 失败'}")
    print(f"因子更新: {'[OK] 成功' if results['factor_update'] else '[ERROR] 失败'}")
    print(f"TopK选股: {'[OK] 成功' if results['topk_stocks'] is not None else '[ERROR] 失败'}")
    print(f"小市值选股: {'[OK] 成功' if results['smallcap_stocks'] is not None else '[WARN] 未选出'}")
    print(f"交易信号: {'[OK] 成功' if results['trade_signals'] is not None else '[ERROR] 失败'}")
    if 'futures_etf' in results:
        print(f"期货ETF: {'[OK] 成功' if results['futures_etf'] else '[WARN] 失败'}")
    print(f"Web界面: {'[OK] 已启动' if results['dashboard'] else '[ERROR] 失败'}")
    
    # 提示信息
    if 'service_check' not in results:
        print("\n[TIP] 提示: 使用 --check-services 参数可以在启动前检查并修复服务状态")
    
    print("\n" + "="*80)
    print(f"  [OK] 所有服务启动完成")
    print(f"  结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80 + "\n")
    
    if results['dashboard']:
        print("[TIP] 提示: Streamlit Dashboard 正在后台运行")
        print("   按 Ctrl+C 退出主程序（Dashboard将继续运行）\n")

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[WARN] 用户中断执行")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n[ERROR] 执行过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
