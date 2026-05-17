#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据增强脚本 - 使用AkShare扩展数据源

新增数据：
1. 北向资金流向
2. 主力资金流向
3. 龙虎榜数据
4. 新闻舆情
5. 研报数据
"""

import sys
import os
import io
from datetime import datetime, timedelta
import pandas as pd
import akshare as ak

# Windows编码修复
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.utils.db_utils import DBUtils


def sync_northbound_flow():
    """同步北向资金流向（沪股通+深股通）"""
    print("\n[1/5] 同步北向资金流向...")
    
    try:
        # 沪股通
        df_hu = ak.stock_em_hsgt_north_net_flow_in(symbol="沪股通")
        df_hu['market'] = 'SH'
        
        # 深股通
        df_sz = ak.stock_em_hsgt_north_net_flow_in(symbol="深股通")
        df_sz['market'] = 'SZ'
        
        # 合并
        df = pd.concat([df_hu, df_sz], ignore_index=True)
        
        # 存储
        with DBUtils.get_conn() as conn:
            df.to_sql('northbound_flow', conn, if_exists='replace', index=False)
        
        print(f"  ✅ 北向资金流向同步完成: {len(df)} 条记录")
        return True
    
    except Exception as e:
        print(f"  ❌ 北向资金流向同步失败: {e}")
        return False


def sync_main_fund_flow(limit=50):
    """
    同步主力资金流向
    
    Args:
        limit: 同步前N只股票（避免API限制）
    """
    print(f"\n[2/5] 同步主力资金流向 (Top {limit} 只股票)...")
    
    try:
        # 获取股票列表
        sql = """
        SELECT DISTINCT ts_code 
        FROM stock_daily 
        WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)
        ORDER BY total_mv DESC
        LIMIT ?
        """
        stock_list = DBUtils.query_df(sql, params=[limit])
        
        all_data = []
        success_count = 0
        
        for idx, row in stock_list.iterrows():
            ts_code = row['ts_code']
            stock_code = ts_code[:6]
            market = 'sz' if ts_code.endswith('.SZ') else 'sh'
            
            try:
                # 获取资金流向
                df = ak.stock_individual_fund_flow(stock=stock_code, market=market)
                df['ts_code'] = ts_code
                all_data.append(df)
                success_count += 1
                
                if (idx + 1) % 10 == 0:
                    print(f"  进度: {idx + 1}/{limit}")
            
            except Exception as e:
                continue
        
        if all_data:
            df_all = pd.concat(all_data, ignore_index=True)
            
            # 存储
            with DBUtils.get_conn() as conn:
                df_all.to_sql('main_fund_flow', conn, if_exists='replace', index=False)
            
            print(f"  ✅ 主力资金流向同步完成: {success_count}/{limit} 只股票, {len(df_all)} 条记录")
            return True
        else:
            print(f"  ⚠️ 主力资金流向同步失败：无数据")
            return False
    
    except Exception as e:
        print(f"  ❌ 主力资金流向同步失败: {e}")
        return False


def sync_dragon_tiger(days=30):
    """
    同步龙虎榜数据
    
    Args:
        days: 同步最近N天的龙虎榜
    """
    print(f"\n[3/5] 同步龙虎榜数据 (最近{days}天)...")
    
    try:
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        
        # 获取龙虎榜明细
        df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
        
        if df.empty:
            print(f"  ⚠️ 龙虎榜数据为空")
            return False
        
        # 存储
        with DBUtils.get_conn() as conn:
            df.to_sql('dragon_tiger', conn, if_exists='replace', index=False)
        
        print(f"  ✅ 龙虎榜数据同步完成: {len(df)} 条记录")
        return True
    
    except Exception as e:
        print(f"  ❌ 龙虎榜数据同步失败: {e}")
        return False


def sync_stock_news(limit=30):
    """
    同步股票新闻
    
    Args:
        limit: 同步前N只股票的新闻
    """
    print(f"\n[4/5] 同步股票新闻 (Top {limit} 只股票)...")
    
    try:
        # 获取股票列表
        sql = """
        SELECT DISTINCT ts_code 
        FROM stock_daily 
        WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)
        ORDER BY total_mv DESC
        LIMIT ?
        """
        stock_list = DBUtils.query_df(sql, params=[limit])
        
        all_news = []
        success_count = 0
        
        for idx, row in stock_list.iterrows():
            ts_code = row['ts_code']
            stock_code = ts_code[:6]
            
            try:
                # 获取新闻
                df = ak.stock_news_em(symbol=stock_code)
                df['ts_code'] = ts_code
                all_news.append(df)
                success_count += 1
                
                if (idx + 1) % 10 == 0:
                    print(f"  进度: {idx + 1}/{limit}")
            
            except Exception as e:
                continue
        
        if all_news:
            df_all = pd.concat(all_news, ignore_index=True)
            
            # 存储
            with DBUtils.get_conn() as conn:
                df_all.to_sql('stock_news', conn, if_exists='replace', index=False)
            
            print(f"  ✅ 股票新闻同步完成: {success_count}/{limit} 只股票, {len(df_all)} 条新闻")
            return True
        else:
            print(f"  ⚠️ 股票新闻同步失败：无数据")
            return False
    
    except Exception as e:
        print(f"  ❌ 股票新闻同步失败: {e}")
        return False


def sync_research_reports(limit=30):
    """
    同步研报数据
    
    Args:
        limit: 同步前N只股票的研报
    """
    print(f"\n[5/5] 同步研报数据 (Top {limit} 只股票)...")
    
    try:
        # 获取股票列表
        sql = """
        SELECT DISTINCT ts_code 
        FROM stock_daily 
        WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)
        ORDER BY total_mv DESC
        LIMIT ?
        """
        stock_list = DBUtils.query_df(sql, params=[limit])
        
        all_reports = []
        success_count = 0
        
        for idx, row in stock_list.iterrows():
            ts_code = row['ts_code']
            stock_code = ts_code[:6]
            
            try:
                # 获取研报
                df = ak.stock_research_report_em(symbol=stock_code)
                df['ts_code'] = ts_code
                all_reports.append(df)
                success_count += 1
                
                if (idx + 1) % 10 == 0:
                    print(f"  进度: {idx + 1}/{limit}")
            
            except Exception as e:
                continue
        
        if all_reports:
            df_all = pd.concat(all_reports, ignore_index=True)
            
            # 存储
            with DBUtils.get_conn() as conn:
                df_all.to_sql('research_reports', conn, if_exists='replace', index=False)
            
            print(f"  ✅ 研报数据同步完成: {success_count}/{limit} 只股票, {len(df_all)} 篇研报")
            return True
        else:
            print(f"  ⚠️ 研报数据同步失败：无数据")
            return False
    
    except Exception as e:
        print(f"  ❌ 研报数据同步失败: {e}")
        return False


def main():
    """执行数据增强"""
    print("=" * 60)
    print("  数据增强脚本 - AkShare扩展")
    print("=" * 60)
    print(f"  执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = {}
    
    # 1. 北向资金
    results['northbound'] = sync_northbound_flow()
    
    # 2. 主力资金（限制50只避免API限制）
    results['main_fund'] = sync_main_fund_flow(limit=50)
    
    # 3. 龙虎榜
    results['dragon_tiger'] = sync_dragon_tiger(days=30)
    
    # 4. 股票新闻（限制30只）
    results['news'] = sync_stock_news(limit=30)
    
    # 5. 研报数据（限制30只）
    results['reports'] = sync_research_reports(limit=30)
    
    # 总结
    print("\n" + "=" * 60)
    print("  数据增强完成")
    print("=" * 60)
    
    success_count = sum(results.values())
    total_count = len(results)
    
    print(f"\n✅ 成功: {success_count}/{total_count} 个数据源")
    
    for name, success in results.items():
        status = "✅" if success else "❌"
        print(f"  {status} {name}")
    
    print("\n💡 提示:")
    print("  - 新增数据表已存储在数据库中")
    print("  - 可在Dashboard或分析脚本中使用这些数据构建因子")
    print("  - 建议定期运行此脚本以保持数据最新")


if __name__ == '__main__':
    main()
