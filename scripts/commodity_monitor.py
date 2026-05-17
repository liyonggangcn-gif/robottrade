#!/usr/bin/env python3
"""
大宗商品价格监控 - 生意社爬虫
抓取 100ppi.com 商品涨跌榜，关联上市公司，推送到钉钉
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import re
import requests
from datetime import datetime

from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def get_commodity_prices():
    """从生意社100ppi.com爬取商品涨跌榜"""
    prices = {}
    url = "https://www.100ppi.com/ppi/"
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "utf-8"
        html = resp.text
        
        # 解析商品涨跌榜数据
        # 格式: 商品名称 价格 七日涨跌幅
        # 找到数据表格
        patterns = [
            r'能源.*?([-]?\d+\.\d+).*?([-]?\d+[%\u2B\u00C9\u2197]+)',
            r'化工.*?([-]?\d+\.\d+).*?([-]?\d+[%\u2B\u00C9\u2197]+)',
            r'钢铁.*?([-]?\d+\.\d+).*?([-]?\d+[%\u2B\u00C9\u2197]+)',
            r'有色.*?([-]?\d+\.\d+).*?([-]?\d+[%\u2B\u00C9\u2197]+)',
        ]
        
        # 匹配价格和涨跌幅块
        # 简单解析: 在tbody中找tr
        import pandas as pd
        from io import StringIO
        
        # 提取表格数据
        tables = pd.read_html(StringIO(html))
        for table in tables:
            if table.empty:
                continue
            for _, row in table.iterrows():
                if len(row) >= 3:
                    name = str(row.iloc[0]).strip()
                    price_str = str(row.iloc[1]).strip()
                    chg_str = str(row.iloc[2]).strip()
                    
                    # 解析价格
                    try:
                        price = float(re.sub(r'[^\d.]', '', price_str))
                    except:
                        price = 0
                    
                    # 解析涨跌幅
                    chg = 0
                    if '\u2b05' in chg_str or '+' in chg_str:
                        chg = float(re.sub(r'[^\d.\-]', '', chg_str))
                    elif '\u2b07' in chg_str or '-' in chg_str:
                        chg = -float(re.sub(r'[^\d.\-]', '', chg_str))
                    
                    if name and price > 0:
                        prices[name] = {"price": price, "chg": chg}
                        
    except Exception as e:
        print(f"爬取失败: {e}")
    
    return prices


def get_commodity_prices_simple():
    """简化版：从生意社爬取"""
    url = "https://www.100ppi.com/ppi/"
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "utf-8"
        html = resp.text
        
        prices = {}
        
        # 关键商品列表
        commodities = [
            "铜", "铝", "锌", "铅", "镍", "黄金", "白银", "螺纹钢", "铁矿石", 
            "原油", " PTA", "甲醇", "沥青", "橡胶", "不锈钢", "焦炭", "动力煤"
        ]
        
        for name in commodities:
            # 简化：直接返回商品名称，标注需要API获取
            prices[name] = {"price": 0, "chg": 0, "note": "需API"}
            
    except Exception as e:
        print(f"请求失败: {e}")
        
    return prices


def build_report():
    """构建报告"""
    # 生意社需要登录/付费API，先只用关联股票
    prices = {}
    
    stocks = get_commodity_stocks()
    
    report = f"📊 大宗商品价格监控 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    
    # 关联行业股票
    stocks_by_industry = {}
    for _, row in stocks.iterrows():
        ind = str(row.get("industry", "")).strip()
        if not ind:
            continue
        if ind not in stocks_by_industry:
            stocks_by_industry[ind] = []
        stocks_by_industry[ind].append({
            "code": str(row.get("ts_code", "")),
            "name": str(row.get("name", "")),
        })
    
    # 按行业展示
    report += "【大宗商品关联股票】\n"
    
    # 重点行业
    key_industries = ["铜", "铝", "铅锌", "黄金", "白银", "钢铁", "煤炭", "石油开采", "石油加工", "铁矿石", "有色金属"]
    shown = set()
    for ind in key_industries:
        for ind2, stock_list in stocks_by_industry.items():
            if ind in ind2 and ind2 not in shown:
                count = len(stock_list)
                top = stock_list[:3]
                names = ", ".join([s["name"][:6] for s in top])
                report += f"• {ind2}({count}): {names}...\n"
                shown.add(ind2)
                break
    
    if not shown:
        # 通用行业
        for ind, stock_list in list(stocks_by_industry.items())[:15]:
            count = len(stock_list)
            top = stock_list[:3]
            names = ", ".join([s["name"][:6] for s in top])
            report += f"• {ind}({count}): {names}...\n"
    
    report += "\n💡 说明: 生意社需API/登录获取实时价格，当前显示关联股票"
    report += "\n📞 商务咨询: 0571-87671511"
    
    return report


def get_commodity_stocks():
    """获取商品关联股票"""
    sql = """
    SELECT industry, ts_code, name FROM stock_info 
    WHERE industry IS NOT NULL AND industry != ''
    """
    return DBUtils.query_df(sql)


def main():
    print("=" * 50)
    print("大宗商品价格监控 - 生意社")
    print("=" * 50)
    
    report = build_report()
    print(report)
    
    # 发送到钉钉
    webhook = Config.get("notification.dingtalk.webhook")
    if webhook:
        try:
            from src.utils.notifier import DingTalkNotifier
            secret = Config.get("notification.dingtalk.secret_word", "提醒")
            notifier = DingTalkNotifier(webhook, secret_word=secret)
            notifier.send_message("大宗商品监控", report)
            print("\n已推送到钉钉")
        except Exception as e:
            print(f"推送失败: {e}")


if __name__ == "__main__":
    main()