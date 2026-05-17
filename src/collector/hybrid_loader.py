import os
import time
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from loguru import logger

# 在 import tushare 之前禁用代理，避免 127.0.0.1:7890 超时
from src.utils.network_utils import clear_proxy_env, patch_requests_no_proxy
clear_proxy_env()
patch_requests_no_proxy()

try:
    import tushare as ts
except ImportError:
    ts = None

try:
    import efinance as ef
except ImportError:
    ef = None

try:
    import yfinance as yf
except ImportError:
    yf = None

from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils


class HybridLoader:
    """高可用混合数据加载器
    
    支持多数据源自动降级：
    - Tushare (主力) → eFinance (A股备用) → YFinance (美股/港股)
    
    对上层策略透明，输出格式永远一致
    """
    
    def __init__(self, tushare_token: Optional[str] = None, db_path: Optional[str] = None):
        """初始化混合数据加载器
        
        Args:
            tushare_token: Tushare API Token
            db_path: SQLite数据库路径
        """
        self.tushare_token = tushare_token or Config.tushare_token
        self.db_path = db_path or Config.duckdb_path.replace('.duckdb', '.db')
        self.start_date = Config.start_date
        
        logger.info(f"Initializing HybridLoader...")
        logger.info(f"  SQLite path: {self.db_path}")
        logger.info(f"  Start date: {self.start_date}")
        
        self.pro = None
        self._init_tushare()
        self._init_database()
        
        logger.success("HybridLoader initialized successfully")
    
    def _init_tushare(self):
        """初始化Tushare连接"""
        if not self.tushare_token:
            logger.warning("No Tushare token provided, Tushare will be disabled")
            return
        
        if not ts:
            logger.warning("Tushare package not installed, Tushare will be disabled")
            return
        
        try:
            ts.set_token(self.tushare_token)
            self.pro = ts.pro_api()
            logger.success("Tushare API initialized successfully")
        except Exception as e:
            logger.warning(f"Failed to initialize Tushare API: {e}")
            self.pro = None
    
    def _init_database(self):
        """初始化数据库连接和表结构"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        logger.info(f"Initializing database: {self.db_path}")
        
        self._create_tables()
    
    def _create_tables(self):
        """创建数据库表（如果不存在）"""
        # 创建stock_daily表
        DBUtils.execute('''
        CREATE TABLE IF NOT EXISTS stock_daily (
            trade_date TEXT,
            ts_code TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            pre_close REAL,
            change REAL,
            pct_chg REAL,
            vol REAL,
            amount REAL,
            PRIMARY KEY (trade_date, ts_code)
        )
        ''')
        logger.info("stock_daily table created/verified")
        
        # 创建stock_info表
        DBUtils.execute('''
        CREATE TABLE IF NOT EXISTS stock_info (
            ts_code TEXT,
            name TEXT,
            market TEXT,
            pe_ttm REAL,
            pb REAL,
            total_mv REAL
        )
        ''')
        logger.info("stock_info table created/verified")
    
    def fetch_data(self, ts_code: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        """获取股票数据（自动降级）
        
        Args:
            ts_code: 股票代码
            start_date: 开始日期 (YYYYMMDD)，默认使用配置的开始日期
            end_date: 结束日期 (YYYYMMDD)，默认为今天
            
        Returns:
            标准化格式的DataFrame
        """
        if not start_date:
            start_date = self.start_date
        if not end_date:
            end_date = datetime.now().strftime('%Y%m%d')
        
        logger.info(f"Fetching data for {ts_code} from {start_date} to {end_date}")
        
        market = self._identify_market(ts_code)
        
        if market in ['US', 'HK']:
            return self._fetch_yfinance(ts_code, start_date, end_date)
        elif market == 'A':
            return self._fetch_ashare(ts_code, start_date, end_date)
        else:
            logger.warning(f"Unknown market for {ts_code}, trying all sources")
            return self._try_all_sources(ts_code, start_date, end_date)
    
    def _identify_market(self, code: str) -> str:
        """识别股票市场
        
        Args:
            code: 股票代码
            
        Returns:
            'A', 'US', 'HK', or 'UNKNOWN'
        """
        code_str = str(code)
        
        if code_str.isdigit() and len(code_str) == 6:
            return 'A'
        elif code_str.isdigit() and len(code_str) == 5:
            return 'HK'
        elif code_str.endswith('.HK') and code_str[:-3].isdigit():
            return 'HK'
        elif code_str.isalpha():
            return 'US'
        else:
            return 'UNKNOWN'
    
    def _fetch_ashare(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取A股数据（Tushare → eFinance）
        
        Args:
            ts_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            标准化格式的DataFrame
        """
        df = pd.DataFrame()
        
        if self.pro:
            try:
                logger.info(f"Fetching {ts_code} from Tushare...")
                df = self._fetch_tushare(ts_code, start_date, end_date)
                if not df.empty:
                    logger.success(f"Got {len(df)} records from Tushare for {ts_code}")
                    return df
            except Exception as e:
                logger.warning(f"Tushare failed for {ts_code}: {e}, switching to eFinance...")
        
        if ef:
            try:
                logger.info(f"Fetching {ts_code} from eFinance...")
                df = self._fetch_efinance(ts_code, start_date, end_date)
                if not df.empty:
                    logger.success(f"Got {len(df)} records from eFinance for {ts_code}")
                    return df
            except Exception as e:
                logger.warning(f"eFinance failed for {ts_code}: {e}")
        
        logger.error(f"All sources failed for {ts_code}")
        return pd.DataFrame()
    
    def _fetch_tushare(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """从Tushare获取数据
        
        Args:
            ts_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            DataFrame
        """
        df_daily = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        
        if df_daily.empty:
            return pd.DataFrame()
        
        df_basic = self.pro.daily_basic(ts_code=ts_code, start_date=start_date, end_date=end_date)
        
        if not df_basic.empty:
            df = pd.merge(df_daily, df_basic[['trade_date', 'pe_ttm', 'total_mv']], on='trade_date', how='left')
        else:
            df = df_daily
            df['pe_ttm'] = None
            df['total_mv'] = None
        
        return self._normalize_data(df)
    
    def _fetch_efinance(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """从eFinance获取数据
        
        Args:
            ts_code: 股票代码
            end_date: 结束日期
            
        Returns:
            DataFrame
        """
        df = ef.stock.get_quote_history(ts_code, beg=start_date, end=end_date)
        
        if df.empty:
            return pd.DataFrame()
        
        column_mapping = {
            "日期": "trade_date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "vol",
            "成交额": "amount"
        }
        
        df = df.rename(columns=column_mapping)
        
        df['pe_ttm'] = None
        df['total_mv'] = None
        
        try:
            base_info = ef.stock.get_base_info(ts_code)
            if not base_info.empty:
                pe_ttm = base_info.get('市盈率TTM', pd.Series([None])).iloc[0]
                total_mv = base_info.get('总市值', pd.Series([None])).iloc[0]
                
                df['pe_ttm'] = pe_ttm
                df['total_mv'] = total_mv
        except Exception as e:
            logger.warning(f"Failed to fetch fundamental data from eFinance: {e}")
        
        return self._normalize_data(df)
    
    def _fetch_yfinance(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """从YFinance获取数据（美股/港股）
        
        Args:
            ts_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            DataFrame
        """
        if not yf:
            logger.error("YFinance not installed")
            return pd.DataFrame()
        
        try:
            logger.info(f"Fetching {ts_code} from YFinance...")
            
            yf_code = self._convert_to_yfinance_code(ts_code)
            
            df = yf.download(
                yf_code,
                start=pd.to_datetime(start_date, format='%Y%m%d'),
                end=pd.to_datetime(end_date, format='%Y%m%d'),
                auto_adjust=False
            )
            
            if df.empty:
                return pd.DataFrame()
            
            df = df.reset_index()
            
            if df['Date'].dt.tz is not None:
                df['Date'] = df['Date'].dt.tz_localize(None)
            
            column_mapping = {
                "Date": "trade_date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "vol"
            }
            
            df = df.rename(columns=column_mapping)
            df['ts_code'] = ts_code
            df['amount'] = df['close'] * df['vol']
            
            try:
                ticker = yf.Ticker(yf_code)
                info = ticker.info
                pe_ttm = info.get('forwardPE') or info.get('trailingPE')
                total_mv = info.get('marketCap')
                
                if total_mv:
                    total_mv = total_mv / 100000000
                
                df['pe_ttm'] = pe_ttm
                df['total_mv'] = total_mv
            except Exception as e:
                logger.warning(f"Failed to fetch fundamental data from YFinance: {e}")
                df['pe_ttm'] = None
                df['total_mv'] = None
            
            return self._normalize_data(df)
            
        except Exception as e:
            logger.error(f"YFinance failed for {ts_code}: {e}")
            return pd.DataFrame()
    
    def _convert_to_yfinance_code(self, ts_code: str) -> str:
        """转换为YFinance代码格式
        
        Args:
            ts_code: 股票代码
            
        Returns:
            YFinance格式的代码
        """
        code_str = str(ts_code)
        
        if code_str.isdigit() and len(code_str) == 5:
            return f"{code_str}.HK"
        else:
            return code_str
    
    def _try_all_sources(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """尝试所有数据源
        
        Args:
            ts_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            DataFrame
        """
        sources = []
        
        if self.pro:
            sources.append(('Tushare', lambda: self._fetch_tushare(ts_code, start_date, end_date)))
        
        if ef:
            sources.append(('eFinance', lambda: self._fetch_efinance(ts_code, start_date, end_date)))
        
        if yf:
            sources.append(('YFinance', lambda: self._fetch_yfinance(ts_code, start_date, end_date)))
        
        for source_name, fetch_func in sources:
            try:
                logger.info(f"Trying {source_name} for {ts_code}...")
                df = fetch_func()
                if not df.empty:
                    logger.success(f"Got {len(df)} records from {source_name} for {ts_code}")
                    return df
            except Exception as e:
                logger.warning(f"{source_name} failed for {ts_code}: {e}")
        
        return pd.DataFrame()
    
    def _normalize_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """标准化数据格式
        
        无论数据来自哪里，最终输出的DataFrame必须包含以下标准列：
        ['trade_date', 'ts_code', 'open', 'high', 'low', 'close', 'vol', 'pe_ttm', 'total_mv']
        
        Args:
            df: 原始DataFrame
            
        Returns:
            标准化后的DataFrame
        """
        if df.empty:
            return df
        
        required_columns = ['trade_date', 'ts_code', 'open', 'high', 'low', 'close', 'vol', 'pe_ttm', 'total_mv']
        
        for col in required_columns:
            if col not in df.columns:
                if col in ['pe_ttm', 'total_mv']:
                    df[col] = None
                else:
                    logger.warning(f"Missing required column: {col}")
                    return pd.DataFrame()
        
        if 'amount' not in df.columns:
            df['amount'] = df['close'] * df['vol']
        
        df = df[required_columns + ['amount']]
        
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        
        numeric_columns = ['open', 'high', 'low', 'close', 'vol', 'amount', 'pe_ttm', 'total_mv']
        for col in numeric_columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        df = df.sort_values('trade_date').reset_index(drop=True)
        
        return df
    
    def get_stock_list(self, full_market: bool = False) -> pd.DataFrame:
        """获取股票列表
        
        Args:
            full_market: 是否获取全市场股票
            
        Returns:
            股票列表DataFrame
        """
        logger.info(f"Fetching stock list (full_market={full_market})...")
        
        if full_market:
            return self._get_full_market_list()
        else:
            return self._get_watchlist()
    
    def _get_full_market_list(self) -> pd.DataFrame:
        """获取全市场股票列表
        
        Returns:
            股票列表DataFrame
        """
        stocks = []
        
        if self.pro:
            try:
                logger.info("Fetching full market list from Tushare...")
                df = self.pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry,market,list_date')
                
                if not df.empty:
                    df['market'] = 'A'
                    stocks = df.to_dict('records')
                    logger.success(f"Got {len(stocks)} stocks from Tushare")
                    return pd.DataFrame(stocks)
            except Exception as e:
                logger.warning(f"Tushare failed to fetch stock list: {e}")
        
        if ef:
            try:
                logger.info("Fetching full market list from eFinance...")
                df = ef.stock.get_realtime_quotes()
                
                if not df.empty:
                    df = df.rename(columns={
                        '股票代码': 'ts_code',
                        '股票名称': 'name'
                    })
                    
                    df['industry'] = ''
                    df['market'] = 'A'
                    df['list_date'] = None
                    
                    stocks = df.to_dict('records')
                    logger.success(f"Got {len(stocks)} stocks from eFinance")
                    return pd.DataFrame(stocks)
            except Exception as e:
                logger.warning(f"eFinance failed to fetch stock list: {e}")
        
        logger.error("All sources failed to fetch stock list")
        return pd.DataFrame()
    
    def _get_watchlist(self) -> pd.DataFrame:
        """获取关注列表
        
        Returns:
            股票列表DataFrame
        """
        watchlist = [
            {'ts_code': '600519.SH', 'name': '贵州茅台', 'market': 'A', 'industry': '食品饮料', 'list_date': '2001-08-27'},
            {'ts_code': '300750.SZ', 'name': '宁德时代', 'market': 'A', 'industry': '电气设备', 'list_date': '2018-06-11'},
            {'ts_code': '300059.SZ', 'name': '东方财富', 'market': 'A', 'industry': '非银金融', 'list_date': '2010-03-19'},
            {'ts_code': '600036.SH', 'name': '招商银行', 'market': 'A', 'industry': '银行', 'list_date': '2002-04-09'},
            {'ts_code': '002060.SZ', 'name': '粤水电', 'market': 'A', 'industry': '建筑装饰', 'list_date': '2006-08-10'},
            {'ts_code': 'NVDA', 'name': 'NVIDIA', 'market': 'US', 'industry': 'Technology', 'list_date': None},
            {'ts_code': 'MSFT', 'name': 'Microsoft', 'market': 'US', 'industry': 'Technology', 'list_date': None},
            {'ts_code': 'AAPL', 'name': 'Apple', 'market': 'US', 'industry': 'Technology', 'list_date': None},
            {'ts_code': 'TSLA', 'name': 'Tesla', 'market': 'US', 'industry': 'Automotive', 'list_date': None},
            {'ts_code': '00700.HK', 'name': '腾讯控股', 'market': 'HK', 'industry': 'Technology', 'list_date': None}
        ]
        
        logger.info(f"Loaded watchlist with {len(watchlist)} stocks")
        return pd.DataFrame(watchlist)
    
    def save_to_database(self, df: pd.DataFrame):
        """保存数据到数据库
        
        Args:
            df: 标准化后的DataFrame
        """
        if df.empty:
            logger.warning("Empty DataFrame, nothing to save")
            return
        
        try:
            df_copy = df.copy()
            df_copy['trade_date'] = df_copy['trade_date'].dt.strftime('%Y-%m-%d')
            
            if 'pre_close' not in df_copy.columns:
                df_copy['pre_close'] = df_copy['close'].shift(1)
            if 'change' not in df_copy.columns:
                df_copy['change'] = df_copy['close'] - df_copy['pre_close']
            if 'pct_chg' not in df_copy.columns:
                df_copy['pct_chg'] = (df_copy['change'] / df_copy['pre_close'] * 100).round(2)
            
            df_copy = df_copy[['trade_date', 'ts_code', 'open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_chg', 'vol', 'amount']]
            
            # 使用短连接逐行插入数据
            inserted_count = 0
            for _, row in df_copy.iterrows():
                try:
                    DBUtils.execute('''
                    INSERT INTO stock_daily (trade_date, ts_code, open, high, low, close, pre_close, change, pct_chg, vol, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (trade_date, ts_code) DO UPDATE SET
                        open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        pre_close = EXCLUDED.pre_close,
                        change = EXCLUDED.change,
                        pct_chg = EXCLUDED.pct_chg,
                        vol = EXCLUDED.vol,
                        amount = EXCLUDED.amount
                    ''', [
                        row['trade_date'],
                        row['ts_code'],
                        row['open'],
                        row['high'],
                        row['low'],
                        row['close'],
                        row['pre_close'],
                        row['change'],
                        row['pct_chg'],
                        row['vol'],
                        row['amount']
                    ])
                    inserted_count += 1
                except Exception as e:
                    # 忽略重复键错误
                    if "UNIQUE constraint failed" not in str(e):
                        logger.warning(f"Error inserting record: {e}")
            
            logger.success(f"Saved {inserted_count} records to database")
        except Exception as e:
            logger.error(f"Failed to save to database: {e}")
    
    def save_stock_info(self, df: pd.DataFrame):
        """保存股票信息到数据库
        
        Args:
            df: 股票信息DataFrame
        """
        if df.empty:
            logger.warning("Empty DataFrame, nothing to save")
            return
        
        try:
            df_copy = df.copy()
            
            required_columns = ['ts_code', 'name', 'market', 'pe_ttm', 'pb', 'total_mv']
            for col in required_columns:
                if col not in df_copy.columns:
                    df_copy[col] = None
            
            df_copy = df_copy[required_columns]
            
            # 使用短连接逐行插入数据
            inserted_count = 0
            for _, row in df_copy.iterrows():
                try:
                    # 先删除旧记录
                    DBUtils.execute('DELETE FROM stock_info WHERE ts_code = ?', [row['ts_code']])
                    
                    # 插入新记录
                    DBUtils.execute('''
                    INSERT INTO stock_info (ts_code, name, market, pe_ttm, pb, total_mv)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ''', [
                        row['ts_code'],
                        row['name'],
                        row['market'],
                        row['pe_ttm'],
                        row['pb'],
                        row['total_mv']
                    ])
                    inserted_count += 1
                except Exception as e:
                    logger.warning(f"Error saving stock info for {row['ts_code']}: {e}")
            
            logger.success(f"Saved {inserted_count} stock info records to database")
        except Exception as e:
            logger.error(f"Failed to save stock info to database: {e}")
    
    def close(self):
        """关闭资源"""
        logger.info("HybridLoader resources closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __del__(self):
        """析构函数，确保连接被关闭"""
        self.close()
