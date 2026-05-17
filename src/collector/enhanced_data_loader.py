#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
增强数据加载器 - 充分利用AKShare和Tushare
提供更多数据源功能和优化
"""

import os
import sys
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, Dict, List

# 清除代理设置
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils
from src.utils.log_utils import init_logger

logger = init_logger("enhanced_data_loader")

try:
    import tushare as ts
    TUSHARE_AVAILABLE = True
except ImportError:
    TUSHARE_AVAILABLE = False

try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False

try:
    import efinance as ef
    EFINANCE_AVAILABLE = True
except ImportError:
    EFINANCE_AVAILABLE = False


class EnhancedDataLoader:
    """增强数据加载器 - 充分利用多个数据源"""
    
    def __init__(self):
        """初始化增强数据加载器"""
        # 初始化Tushare
        self.pro = None
        self.tushare_token = Config.tushare_token
        if TUSHARE_AVAILABLE and self.tushare_token:
            try:
                ts.set_token(self.tushare_token)
                self.pro = ts.pro_api()
                logger.info("Tushare API初始化成功")
            except Exception as e:
                logger.warning(f"Tushare API初始化失败: {e}")
        
        logger.info("增强数据加载器初始化完成")
    
    def get_stock_money_flow(self, ts_code: str, trade_date: str = None) -> Optional[pd.DataFrame]:
        """
        获取股票资金流向数据（使用AKShare）
        
        Args:
            ts_code: 股票代码（如"000001"）
            trade_date: 交易日期（YYYYMMDD），如果为None则获取最新
            
        Returns:
            资金流向DataFrame
        """
        if not AKSHARE_AVAILABLE:
            return None
        
        try:
            # AKShare获取资金流向 - 修正API调用
            # 使用正确的API：stock_individual_fund_flow_rank
            df = ak.stock_individual_fund_flow_rank(indicator="今日")
            
            # 过滤指定股票
            if ts_code:
                code_clean = ts_code.replace('.SH', '').replace('.SZ', '')
                # 尝试不同的列名
                if '代码' in df.columns:
                    df = df[df['代码'] == code_clean]
                elif '股票代码' in df.columns:
                    df = df[df['股票代码'] == code_clean]
            
            return df
        except Exception as e:
            logger.error(f"获取资金流向失败: {e}")
            return None
    
    def get_dragon_tiger_list(self, trade_date: str = None) -> Optional[pd.DataFrame]:
        """
        获取龙虎榜数据（使用AKShare）
        
        Args:
            trade_date: 交易日期（YYYYMMDD），如果为None则获取最新
            
        Returns:
            龙虎榜DataFrame
        """
        if not AKSHARE_AVAILABLE:
            return None
        
        try:
            # 尝试使用AKShare的龙虎榜接口
            # 注意：AKShare的龙虎榜API可能有变化，这里使用通用方法
            if trade_date:
                date_obj = datetime.strptime(trade_date, '%Y%m%d')
                date_str = date_obj.strftime('%Y%m%d')
            else:
                # 获取最新龙虎榜 - 使用当前日期
                date_str = datetime.now().strftime('%Y%m%d')
            
            # 尝试不同的AKShare龙虎榜接口
            try:
                # 方法1: stock_lhb_detail_em (可能需要不同的参数格式)
                df = ak.stock_lhb_detail_em(date=date_str)
            except:
                try:
                    # 方法2: stock_lhb_em
                    df = ak.stock_lhb_em(date=date_str)
                except:
                    # 方法3: stock_lhb_jgzw_em (机构专用)
                    df = ak.stock_lhb_jgzw_em(date=date_str)
            
            return df
        except Exception as e:
            logger.error(f"获取龙虎榜失败: {e}")
            return None
    
    def get_stock_news(self, ts_code: str, limit: int = 10) -> Optional[List[Dict]]:
        """
        获取股票新闻（使用AKShare）
        
        Args:
            ts_code: 股票代码
            limit: 返回数量限制
            
        Returns:
            新闻列表
        """
        if not AKSHARE_AVAILABLE:
            return None
        
        try:
            code_clean = ts_code.replace('.SH', '').replace('.SZ', '')
            df = ak.stock_news_em(symbol=code_clean)
            
            if df is not None and not df.empty:
                # 只返回最新的limit条
                df = df.head(limit)
                return df.to_dict('records')
            
            return None
        except Exception as e:
            logger.error(f"获取股票新闻失败: {e}")
            return None
    
    def get_macro_economic_data(self, indicator: str = "GDP") -> Optional[pd.DataFrame]:
        """
        获取宏观经济数据（使用AKShare）
        
        Args:
            indicator: 指标名称（GDP、CPI、PMI等）
            
        Returns:
            宏观经济数据DataFrame
        """
        if not AKSHARE_AVAILABLE:
            return None
        
        try:
            # 根据指标获取数据
            if indicator == "GDP":
                df = ak.macro_china_gdp()
            elif indicator == "CPI":
                df = ak.macro_china_cpi()
            elif indicator == "PMI":
                df = ak.macro_china_pmi()
            else:
                logger.warning(f"不支持的指标: {indicator}")
                return None
            
            return df
        except Exception as e:
            logger.error(f"获取宏观经济数据失败: {e}")
            return None
    
    def get_stock_financial_summary(self, ts_code: str) -> Optional[Dict]:
        """
        获取股票财务摘要（使用Tushare）
        
        Args:
            ts_code: 股票代码（如"000001.SZ"）
            
        Returns:
            财务摘要字典
        """
        if not self.pro:
            return None
        
        try:
            # 获取最新财务指标
            df = self.pro.fina_indicator(ts_code=ts_code, start_date='', end_date='', limit=1)
            
            if df is not None and not df.empty:
                latest = df.iloc[0]
                return {
                    'roe': latest.get('roe'),
                    'roa': latest.get('roa'),
                    'gross_profit_margin': latest.get('grossprofit_margin'),
                    'net_profit_margin': latest.get('netprofit_margin'),
                    'debt_to_assets': latest.get('debt_to_assets'),
                    'current_ratio': latest.get('current_ratio'),
                    'update_date': latest.get('end_date')
                }
            
            return None
        except Exception as e:
            logger.error(f"获取财务摘要失败: {e}")
            return None
    
    def get_concept_stocks(self, concept_name: str) -> Optional[pd.DataFrame]:
        """
        获取概念板块股票列表（使用Tushare）
        
        Args:
            concept_name: 概念名称（如"人工智能"）
            
        Returns:
            股票列表DataFrame
        """
        if not self.pro:
            return None
        
        try:
            # 先获取概念列表
            concept_list = self.pro.concept()
            if concept_list is None or concept_list.empty:
                return None
            
            # 查找匹配的概念
            matched = concept_list[concept_list['name'].str.contains(concept_name, na=False)]
            if matched.empty:
                return None
            
            # 获取第一个匹配概念下的股票
            concept_code = matched.iloc[0]['code']
            df = self.pro.concept_detail(id=concept_code, fields='ts_code,name')
            
            return df
        except Exception as e:
            logger.error(f"获取概念股票失败: {e}")
            return None
    
    def get_stock_realtime_quote(self, ts_code: str) -> Optional[Dict]:
        """
        获取股票实时行情（多数据源尝试）
        
        Args:
            ts_code: 股票代码
            
        Returns:
            实时行情字典
        """
        code_clean = ts_code.replace('.SH', '').replace('.SZ', '')
        
        # 方法1: 使用eFinance
        if EFINANCE_AVAILABLE:
            try:
                df = ef.stock.get_realtime_quotes()
                if df is not None and not df.empty:
                    stock_data = df[df['股票代码'] == code_clean]
                    if not stock_data.empty:
                        row = stock_data.iloc[0]
                        return {
                            'code': ts_code,
                            'name': row.get('股票名称', ''),
                            'price': row.get('最新价', 0),
                            'change_pct': row.get('涨跌幅', 0),
                            'volume': row.get('成交量', 0),
                            'amount': row.get('成交额', 0),
                            'source': 'efinance'
                        }
            except Exception as e:
                logger.debug(f"eFinance获取实时行情失败: {e}")
        
        # 方法2: 使用AKShare
        if AKSHARE_AVAILABLE:
            try:
                df = ak.stock_zh_a_spot_em()
                if df is not None and not df.empty:
                    stock_data = df[df['代码'] == code_clean]
                    if not stock_data.empty:
                        row = stock_data.iloc[0]
                        return {
                            'code': ts_code,
                            'name': row.get('名称', ''),
                            'price': row.get('最新价', 0),
                            'change_pct': row.get('涨跌幅', 0),
                            'volume': row.get('成交量', 0),
                            'amount': row.get('成交额', 0),
                            'source': 'akshare'
                        }
            except Exception as e:
                logger.debug(f"AKShare获取实时行情失败: {e}")
        
        return None
    
    def get_market_summary(self) -> Optional[Dict]:
        """
        获取市场概况（使用AKShare）
        
        Returns:
            市场概况字典
        """
        if not AKSHARE_AVAILABLE:
            return None
        
        try:
            # 获取A股市场概况
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return None
            
            total = len(df)
            rise = len(df[df['涨跌幅'] > 0])
            fall = len(df[df['涨跌幅'] < 0])
            flat = len(df[df['涨跌幅'] == 0])
            limit_up = len(df[df['涨跌幅'] >= 9.9])
            limit_down = len(df[df['涨跌幅'] <= -9.9])
            avg_change = df['涨跌幅'].mean()
            
            return {
                'total': total,
                'rise': rise,
                'fall': fall,
                'flat': flat,
                'limit_up': limit_up,
                'limit_down': limit_down,
                'avg_change': avg_change,
                'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            logger.error(f"获取市场概况失败: {e}")
            return None


if __name__ == '__main__':
    loader = EnhancedDataLoader()
    
    # 测试功能
    print("=" * 60)
    print("增强数据加载器测试")
    print("=" * 60)
    
    # 测试市场概况
    print("\n[测试] 获取市场概况...")
    summary = loader.get_market_summary()
    if summary:
        print(f"  总股票数: {summary['total']}")
        print(f"  上涨: {summary['rise']}, 下跌: {summary['fall']}, 平盘: {summary['flat']}")
        print(f"  涨停: {summary['limit_up']}, 跌停: {summary['limit_down']}")
        print(f"  平均涨跌幅: {summary['avg_change']:.2f}%")
    
    # 测试实时行情
    print("\n[测试] 获取实时行情（000001）...")
    quote = loader.get_stock_realtime_quote("000001.SZ")
    if quote:
        print(f"  {quote['name']}: {quote['price']:.2f} ({quote['change_pct']:+.2f}%)")
        print(f"  数据源: {quote['source']}")
