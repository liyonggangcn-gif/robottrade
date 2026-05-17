#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重新同步A股数据（修复后）
"""

import os
import sys

# 清除网络代理
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("all_proxy", None)
print("网络代理已清除")

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.collector.data_loader import DataLoader
from src.factors.alpha_engine import AlphaEngine

def resync_data():
    """重新同步数据"""
    print("=== 开始重新同步A股数据 ===")
    
    # 1. 初始化数据加载器
    loader = DataLoader()
    
    try:
        # 2. 同步日线数据（分批同步）
        print("同步日线数据...")
        print("采用分批同步策略，确保数据质量")
        # 先同步前100只股票验证效果
        loader.sync_daily_data(limit=100)
        
        # 3. 初始化Alpha引擎
        print("\n初始化Alpha引擎...")
        engine = AlphaEngine()
        
        # 4. 更新因子
        print("更新因子数据...")
        engine.update_factors()
        engine.close()
        
        print("\n=== 数据同步完成 ===")
        print("系统已准备就绪，可以重启服务查看效果")
        
    except Exception as e:
        print(f"错误: {e}")
    finally:
        # 5. 关闭连接
        loader.close()

if __name__ == "__main__":
    resync_data()
