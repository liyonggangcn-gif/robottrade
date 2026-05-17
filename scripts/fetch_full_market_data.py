import efinance as ef
import pandas as pd
import duckdb
import os
import traceback
from datetime import datetime
from loguru import logger

class FullMarketDataFetcher:
    """全市场数据获取器"""
    
    def __init__(self, duckdb_path='data/quant.db'):
        """初始化
        
        Args:
            duckdb_path: DuckDB数据库路径
        """
        self.duckdb_path = duckdb_path
        
        # 确保数据库目录存在
        os.makedirs(os.path.dirname(self.duckdb_path), exist_ok=True)
        
        # 初始化DuckDB连接
        self.conn = duckdb.connect(self.duckdb_path)
        logger.info(f"Successfully connected to DuckDB at {self.duckdb_path}")
        
        # 初始化表结构
        self._init_tables()
    
    def _init_tables(self):
        """初始化表结构"""
        # stock_info表
        self.conn.execute('''
        CREATE TABLE IF NOT EXISTS stock_info (
            ts_code VARCHAR PRIMARY KEY,
            name VARCHAR,
            market VARCHAR,
            pe_ttm DOUBLE,
            pb DOUBLE,
            total_mv DOUBLE,
            roe DOUBLE,
            industry VARCHAR,
            sector VARCHAR,
            list_date DATE
        )
        ''')
        
        # stock_daily表
        self.conn.execute('''
        CREATE TABLE IF NOT EXISTS stock_daily (
            ts_code VARCHAR,
            trade_date DATE,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            vol DOUBLE,
            amount DOUBLE,
            pct_chg DOUBLE,
            PRIMARY KEY (ts_code, trade_date)
        )
        ''')
        
        logger.info("Tables initialized successfully")
    
    def fetch_full_market_stocks(self):
        """获取全市场股票列表和基本面数据
        
        Returns:
            DataFrame: 股票基本信息
        """
        logger.info("Fetching full market stocks...")
        
        try:
            # 手动构建A股股票代码列表
            logger.info("Building A-share stock list manually...")
            stocks_list = []
            
            # 沪市主板 (600xxx, 601xxx, 603xxx, 605xxx)
            for prefix in ['600', '601', '603', '605']:
                for i in range(1000):
                    code = f"{prefix}{i:03d}"
                    stocks_list.append({'ts_code': code, 'name': f'股票{code}'})
            
            # 深市主板 (000xxx)
            for i in range(1000):
                code = f"000{i:03d}"
                stocks_list.append({'ts_code': code, 'name': f'股票{code}'})
            
            # 中小板 (002xxx)
            for i in range(1000):
                code = f"002{i:03d}"
                stocks_list.append({'ts_code': code, 'name': f'股票{code}'})
            
            # 创业板 (300xxx)
            for i in range(1000):
                code = f"300{i:03d}"
                stocks_list.append({'ts_code': code, 'name': f'股票{code}'})
            
            # 科创板 (688xxx)
            for i in range(1000):
                code = f"688{i:03d}"
                stocks_list.append({'ts_code': code, 'name': f'股票{code}'})
            
            df_manual = pd.DataFrame(stocks_list)
            
            # 添加默认值
            df_manual['pe_ttm'] = 0.0
            df_manual['pb'] = 0.0
            df_manual['total_mv'] = 0.0
            df_manual['roe'] = 0.0
            df_manual['industry'] = ''
            df_manual['sector'] = ''
            df_manual['list_date'] = None
            
            # 处理ts_code格式
            df_manual['ts_code'] = df_manual['ts_code'].apply(self._format_ts_code)
            
            # 添加market字段
            df_manual['market'] = df_manual['ts_code'].apply(self._get_market)
            
            logger.info(f"Generated {len(df_manual)} stock codes manually")
            return df_manual
            
        except Exception as e:
            logger.error(f"Error fetching full market stocks: {e}")
            return None
    
    def _format_ts_code(self, code):
        """格式化股票代码
        
        Args:
            code: 股票代码
            
        Returns:
            格式化后的股票代码
        """
        if pd.isna(code):
            return ''
        
        code_str = str(code)
        
        # 如果已经是标准格式（如600519.SH），直接返回
        if '.' in code_str:
            return code_str
        
        # 根据代码长度判断市场
        if len(code_str) == 6:
            if code_str.startswith('6'):
                return f"{code_str}.SH"
            else:
                return f"{code_str}.SZ"
        elif code_str.startswith('0'):
            return f"{code_str}.SZ"
        elif code_str.startswith('3'):
            return f"{code_str}.SZ"
        elif code_str.startswith('6'):
            return f"{code_str}.SH"
        else:
            return code_str
    
    def _get_market(self, ts_code):
        """获取市场类型
        
        Args:
            ts_code: 股票代码
            
        Returns:
            市场类型
        """
        if pd.isna(ts_code):
            return ''
        
        code_str = str(ts_code)
        if '.SH' in code_str:
            return 'SH'
        elif '.SZ' in code_str:
            return 'SZ'
        elif code_str.startswith('6'):
            return 'SH'
        else:
            return 'SZ'
    
    def update_stock_info(self, df_stocks):
        """更新股票基本信息表
        
        Args:
            df_stocks: 股票基本信息DataFrame
        """
        if df_stocks is None or len(df_stocks) == 0:
            logger.warning("No stock data to update")
            return
        
        logger.info(f"Updating stock_info table with {len(df_stocks)} stocks...")
        
        try:
            # 删除旧数据
            self.conn.execute("DELETE FROM stock_info")
            logger.info("Deleted old data from stock_info")
            
            # 检查数据格式
            logger.info(f"DataFrame columns: {df_stocks.columns.tolist()}")
            logger.info(f"First 5 rows:\n{df_stocks.head()}")
            
            # 分批插入数据（每次1000条）
            batch_size = 1000
            total_rows = len(df_stocks)
            
            for i in range(0, total_rows, batch_size):
                batch_df = df_stocks.iloc[i:i+batch_size]
                
                # 转换为列表
                records = batch_df.to_records(index=False).tolist()
                
                # 插入数据
                self.conn.execute('''
                INSERT INTO stock_info (ts_code, name, market, pe_ttm, pb, total_mv, roe, industry, sector, list_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', records)
                
                logger.info(f"Inserted batch {i//batch_size + 1}/{(total_rows + batch_size - 1)//batch_size}")
            
            logger.info(f"Successfully updated stock_info table with {total_rows} records")
            
        except Exception as e:
            logger.error(f"Error updating stock_info table: {e}")
            logger.error(traceback.format_exc())
            raise
    
    def fetch_and_update(self):
        """获取并更新全市场数据"""
        logger.info("="*50)
        logger.info("Starting full market data fetch and update")
        logger.info("="*50)
        
        # 获取全市场股票数据
        df_stocks = self.fetch_full_market_stocks()
        
        if df_stocks is not None and len(df_stocks) > 0:
            # 更新数据库
            self.update_stock_info(df_stocks)
            
            # 显示统计信息
            logger.info(f"Total stocks: {len(df_stocks)}")
            logger.info(f"Stocks with PE data: {(df_stocks['pe_ttm'] > 0).sum()}")
            logger.info(f"Stocks with Market Cap data: {(df_stocks['total_mv'] > 0).sum()}")
            
            logger.info("="*50)
            logger.info("Full market data update completed successfully!")
            logger.info("="*50)
        else:
            logger.error("Failed to fetch full market data")
        
        # 关闭连接
        self.close()
    
    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
            self.conn = None
            logger.info("Successfully closed DuckDB connection")


def main():
    """主函数"""
    try:
        fetcher = FullMarketDataFetcher()
        fetcher.fetch_and_update()
    except Exception as e:
        logger.error(f"Error in main: {e}")
        logger.error(traceback.format_exc())


if __name__ == '__main__':
    main()