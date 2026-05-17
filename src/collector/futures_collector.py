#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期货数据采集器
定期获取期货价格数据，用于ETF交易信号生成
"""

import os
import sys
import time
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# 清除代理设置
for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.utils.log_utils import init_logger

logger = init_logger("futures_collector")

try:
    import tushare as ts
    TUSHARE_AVAILABLE = True
except ImportError:
    TUSHARE_AVAILABLE = False
    logger.warning("tushare未安装，期货数据获取功能受限")

try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False


class FuturesCollector:
    """期货数据采集器"""
    
    # 主要期货品种映射（中文名 -> Tushare代码）
    # Tushare期货代码格式：交易所代码+品种代码+合约月份，如CU2403表示沪铜2024年3月合约
    # 主力合约通常使用当月或次月合约
    FUTURES_MAPPING = {
        # 有色金属（上海期货交易所 SHFE）
        "铜": {"symbol": "CU", "exchange": "SHFE", "name": "沪铜", "ts_code": "CU.SHF"},
        "铝": {"symbol": "AL", "exchange": "SHFE", "name": "沪铝", "ts_code": "AL.SHF"},
        "锌": {"symbol": "ZN", "exchange": "SHFE", "name": "沪锌", "ts_code": "ZN.SHF"},
        "铅": {"symbol": "PB", "exchange": "SHFE", "name": "沪铅", "ts_code": "PB.SHF"},
        "镍": {"symbol": "NI", "exchange": "SHFE", "name": "沪镍", "ts_code": "NI.SHF"},
        "锡": {"symbol": "SN", "exchange": "SHFE", "name": "沪锡", "ts_code": "SN.SHF"},
        
        # 贵金属（上海期货交易所 SHFE）
        "黄金": {"symbol": "AU", "exchange": "SHFE", "name": "沪金", "ts_code": "AU.SHF"},
        "白银": {"symbol": "AG", "exchange": "SHFE", "name": "沪银", "ts_code": "AG.SHF"},
        
        # 化工
        "原油": {"symbol": "SC", "exchange": "INE", "name": "原油", "ts_code": "SC.INE"},
        "PTA": {"symbol": "TA", "exchange": "CZCE", "name": "PTA", "ts_code": "TA.CZC"},
        "甲醇": {"symbol": "MA", "exchange": "CZCE", "name": "甲醇", "ts_code": "MA.CZC"},
        "PVC": {"symbol": "V", "exchange": "DCE", "name": "PVC", "ts_code": "V.DCE"},
        "塑料": {"symbol": "L", "exchange": "DCE", "name": "塑料", "ts_code": "L.DCE"},
        "PP": {"symbol": "PP", "exchange": "DCE", "name": "PP", "ts_code": "PP.DCE"},
        "橡胶": {"symbol": "RU", "exchange": "SHFE", "name": "橡胶", "ts_code": "RU.SHF"},
    }
    
    def __init__(self):
        """初始化期货采集器"""
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
        else:
            if not TUSHARE_AVAILABLE:
                logger.warning("tushare未安装，期货数据获取功能受限")
            if not self.tushare_token:
                logger.warning("未配置Tushare Token，期货数据获取功能受限")
        
        # 从配置读取期货品种列表
        futures_config = Config.get('futures_etf', {})
        self.tracked_futures = futures_config.get('tracked_futures', list(self.FUTURES_MAPPING.keys()))
        
        logger.info(f"期货采集器初始化完成，跟踪 {len(self.tracked_futures)} 个品种")
    
    def get_futures_spot_price(self, futures_name: str) -> Optional[float]:
        """
        获取期货实时价格
        
        Args:
            futures_name: 期货品种名称（如"铜"、"黄金"）
            
        Returns:
            当前价格，失败返回None
        """
        if not AKSHARE_AVAILABLE:
            return None
        
        if futures_name not in self.FUTURES_MAPPING:
            logger.warning(f"未找到期货品种: {futures_name}")
            return None
        
        try:
            # 先从数据库获取最新价格
            try:
                df_db = DBUtils.query_df("""
                    SELECT price FROM futures_prices 
                    WHERE futures_name = ? 
                    ORDER BY update_time DESC LIMIT 1
                """, [futures_name])
                if not df_db.empty:
                    return float(df_db.iloc[0]['price'])
            except Exception as e:
                logger.debug(f"从数据库获取{futures_name}价格失败: {e}")
            
            # 如果数据库没有，尝试从akshare获取
            futures_info = self.FUTURES_MAPPING[futures_name]
            symbol = futures_info['symbol']
            
            # 方法1: 尝试使用efinance（如果可用）
            try:
                import efinance as ef
                # efinance期货接口
                df = ef.futures.get_realtime_quotes()
                if df is not None and not df.empty:
                    # 查找对应的期货
                    name_cn = futures_info['name']
                    mask = df['期货名称'].str.contains(name_cn.replace('沪', '').replace('深', ''), na=False)
                    if mask.any():
                        matched = df[mask].iloc[0]
                        price = matched.get('最新价', matched.get('现价', 0))
                        if price:
                            return float(price)
            except Exception as e:
                logger.debug(f"efinance获取{futures_name}失败: {e}")
            
            # 方法2: 使用akshare的简化接口（如果可用）
            # 注意：由于akshare期货接口不稳定，这里先返回None
            # 实际使用时建议手动更新数据库或使用其他数据源
            
            return None
        except Exception as e:
            logger.error(f"获取{futures_name}价格失败: {e}")
            return None
    
    def get_futures_history(self, futures_name: str, days: int = 30) -> Optional[pd.DataFrame]:
        """
        获取期货历史价格数据
        
        Args:
            futures_name: 期货品种名称
            days: 获取最近N天的数据
            
        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """
        if futures_name not in self.FUTURES_MAPPING:
            logger.warning(f"未找到期货品种: {futures_name}")
            return None
        
        try:
            # 优先从数据库获取历史数据
            try:
                df_db = DBUtils.query_df("""
                    SELECT update_time as date, price as close, price as open, 
                           price as high, price as low, 0 as volume
                    FROM futures_prices 
                    WHERE futures_name = ? 
                    ORDER BY update_time DESC LIMIT ?
                """, [futures_name, days])
                
                if not df_db.empty and len(df_db) >= days:
                    return df_db
            except Exception as e:
                logger.debug(f"从数据库获取{futures_name}历史数据失败: {e}")
            
            # 使用Tushare获取历史数据
            if self.pro:
                try:
                    futures_info = self.FUTURES_MAPPING[futures_name]
                    symbol = futures_info['symbol']
                    exchange = futures_info['exchange']
                    
                    # 获取主力合约代码
                    df_basic = self.pro.fut_basic(exchange=exchange, fields='ts_code,symbol,name')
                    if df_basic is not None and not df_basic.empty:
                        df_symbol = df_basic[df_basic['symbol'] == symbol]
                        if not df_symbol.empty:
                            # 使用第一个合约（通常是主力合约）
                            ts_code = df_symbol.iloc[0]['ts_code']
                            
                            # 计算日期范围
                            end_date = datetime.now().strftime('%Y%m%d')
                            start_date = (datetime.now() - timedelta(days=days*2)).strftime('%Y%m%d')
                            
                            # 获取历史数据
                            df_daily = self.pro.fut_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
                            
                            if df_daily is not None and not df_daily.empty:
                                # 标准化列名
                                df_daily = df_daily.rename(columns={
                                    'trade_date': 'date',
                                    'open': 'open',
                                    'high': 'high',
                                    'low': 'low',
                                    'close': 'close',
                                    'vol': 'volume'
                                })
                                
                                # 只保留最近days天的数据
                                if len(df_daily) > days:
                                    df_daily = df_daily.tail(days)
                                
                                return df_daily
                except Exception as e:
                    logger.debug(f"Tushare获取{futures_name}历史数据失败: {e}")
            
            return None
        except Exception as e:
            logger.error(f"获取{futures_name}历史数据失败: {e}")
            return None
    
    def sync_futures_data(self) -> Dict[str, any]:
        """
        同步所有跟踪的期货品种数据到数据库
        
        Returns:
            同步结果字典
        """
        results = {
            'success_count': 0,
            'fail_count': 0,
            'updated_futures': [],
            'errors': []
        }
        
        logger.info(f"开始同步 {len(self.tracked_futures)} 个期货品种数据")
        
        for futures_name in self.tracked_futures:
            try:
                # 获取最新价格
                price = self.get_futures_spot_price(futures_name)
                
                if price is None:
                    # 尝试从历史数据获取
                    df = self.get_futures_history(futures_name, days=1)
                    if df is not None and not df.empty:
                        price = float(df.iloc[-1]['close'])
                
                if price is None:
                    results['fail_count'] += 1
                    results['errors'].append(f"{futures_name}: 无法获取价格")
                    logger.warning(f"无法获取{futures_name}价格")
                    continue
                
                # 保存到数据库
                self._save_futures_price(futures_name, price)
                
                results['success_count'] += 1
                results['updated_futures'].append({
                    'name': futures_name,
                    'price': price,
                    'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                })
                
                logger.info(f"✓ {futures_name}: {price:.2f}")
                
                # 避免请求过快
                time.sleep(0.5)
                
            except Exception as e:
                results['fail_count'] += 1
                results['errors'].append(f"{futures_name}: {str(e)}")
                logger.error(f"同步{futures_name}失败: {e}")
        
        logger.info(f"同步完成: 成功{results['success_count']}个, 失败{results['fail_count']}个")
        return results
    
    def _save_futures_price(self, futures_name: str, price: float):
        """
        保存期货价格到数据库
        
        Args:
            futures_name: 期货品种名称
            price: 价格
        """
        try:
            # 确保表存在
            DBUtils.execute("""
                CREATE TABLE IF NOT EXISTS futures_prices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    futures_name VARCHAR(50) NOT NULL,
                    price REAL NOT NULL,
                    update_time VARCHAR(30) NOT NULL,
                    UNIQUE KEY uk_name_time (futures_name, update_time)
                )
            """)
            
            # 插入数据
            update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            # MariaDB不支持OR REPLACE，使用INSERT IGNORE
            DBUtils.execute("""
                INSERT IGNORE INTO futures_prices (futures_name, price, update_time)
                VALUES (?, ?, ?)
            """, [futures_name, price, update_time])
            
        except Exception as e:
            logger.error(f"保存期货价格失败: {e}")
            raise
    
    def get_latest_futures_prices(self) -> Dict[str, float]:
        """
        从数据库获取最新的期货价格
        
        Returns:
            字典：{期货名称: 价格}
        """
        try:
            df = DBUtils.query_df("""
                SELECT futures_name, price, update_time
                FROM futures_prices
                WHERE update_time = (
                    SELECT MAX(update_time) 
                    FROM futures_prices f2 
                    WHERE f2.futures_name = futures_prices.futures_name
                )
            """)
            
            if df.empty:
                return {}
            
            return dict(zip(df['futures_name'], df['price']))
        except Exception as e:
            logger.error(f"获取最新期货价格失败: {e}")
            return {}
    
    def get_futures_price_change(self, futures_name: str, days: int = 5) -> Optional[float]:
        """
        计算期货价格变化率
        
        Args:
            futures_name: 期货品种名称
            days: 计算N天的变化率
            
        Returns:
            变化率（百分比），失败返回None
        """
        df = self.get_futures_history(futures_name, days=days+1)
        if df is None or df.empty or len(df) < 2:
            return None
        
        try:
            latest_price = float(df.iloc[-1]['close'])
            previous_price = float(df.iloc[0]['close'])
            
            if previous_price == 0:
                return None
            
            change_pct = (latest_price - previous_price) / previous_price * 100
            return change_pct
        except Exception as e:
            logger.error(f"计算{futures_name}价格变化率失败: {e}")
            return None


if __name__ == '__main__':
    collector = FuturesCollector()
    results = collector.sync_futures_data()
    print(f"\n同步结果: 成功{results['success_count']}个, 失败{results['fail_count']}个")
    if results['errors']:
        print("错误列表:")
        for err in results['errors']:
            print(f"  - {err}")
