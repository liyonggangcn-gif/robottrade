#!/usr/bin/env python3
"""
持仓个股公告和研报采集
使用Tushare Pro API: anns_d(公告) + research_report(研报)
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import tushare as ts
from datetime import datetime, timedelta
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils

Token = Config.get('tushare_token')
ts.set_token(Token)
pro = ts.pro_api()

def fetch_announcements(ts_codes, days_back=7):
    """获取个股公告"""
    table_name = 'stock_announcements'
    
    DBUtils.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(20),
            ann_date VARCHAR(10),
            name VARCHAR(50),
            title VARCHAR(500),
            url VARCHAR(500),
            fetched_at DATETIME,
            INDEX idx_ts_code (ts_code),
            INDEX idx_ann_date (ann_date)
        )
    """)
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    count = 0
    for code in ts_codes:
        try:
            df = pro.anns_d(ts_code=code, start_date=start_date.strftime('%Y%m%d'), end_date=end_date.strftime('%Y%m%d'))
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    try:
                        DBUtils.execute(f"""
                            INSERT IGNORE INTO {table_name} (ts_code, ann_date, name, title, url, fetched_at)
                            VALUES (%s, %s, %s, %s, %s, NOW())
                        """, (row.get('ts_code'), row.get('ann_date'), row.get('name'), row.get('title'), row.get('url')))
                        count += 1
                    except:
                        pass
            print(f"  {code}: {len(df) if df is not None else 0}条公告")
        except Exception as e:
            print(f"  {code}: 公告获取失败 - {e}")
    
    print(f"公告采集完成: {count}条")
    return count

def fetch_research_reports(ts_codes, days_back=7):
    """获取券商研报"""
    table_name = 'stock_research_reports'
    
    DBUtils.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(20),
            trade_date VARCHAR(10),
            title VARCHAR(500),
            abstr VARCHAR(1000),
            report_type VARCHAR(20),
            inst_csname VARCHAR(50),
            url VARCHAR(500),
            fetched_at DATETIME,
            INDEX idx_ts_code (ts_code),
            INDEX idx_trade_date (trade_date)
        )
    """)
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    count = 0
    for code in ts_codes:
        try:
            df = pro.research_report(ts_code=code, start_date=start_date.strftime('%Y%m%d'), end_date=end_date.strftime('%Y%m%d'))
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    try:
                        DBUtils.execute(f"""
                            INSERT IGNORE INTO {table_name} (ts_code, trade_date, title, abstr, report_type, inst_csname, url, fetched_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                        """, (
                            row.get('ts_code'), row.get('trade_date'), row.get('title'),
                            row.get('abstr', '')[:1000] if row.get('abstr') else '',
                            row.get('report_type'), row.get('inst_csname'), row.get('url')
                        ))
                        count += 1
                    except:
                        pass
            print(f"  {code}: {len(df) if df is not None else 0}条研报")
        except Exception as e:
            print(f"  {code}: 研报获取失败 - {e}")
    
    print(f"研报采集完成: {count}条")
    return count

def fetch_for_positions(days_back=7):
    """获取持仓个股的公告和研报"""
    print("=" * 50)
    print("  持仓个股公告和研报采集")
    print("=" * 50)
    
    positions = DBUtils.query_df("SELECT ts_code FROM positions")
    ts_codes = list(positions['ts_code'])
    
    print(f"持仓数量: {len(ts_codes)}")
    print(f"\n>>> 采集公告...")
    fetch_announcements(ts_codes, days_back)
    
    print(f"\n>>> 采集研报...")
    fetch_research_reports(ts_codes, days_back)
    
    print("\n完成!")

if __name__ == '__main__':
    fetch_for_positions(days_back=7)