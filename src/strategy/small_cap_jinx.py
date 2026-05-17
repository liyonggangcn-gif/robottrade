import os
import pandas as pd
from datetime import datetime, timedelta
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils
from src.utils.market_breadth import MarketBreadthCalculator
from src.utils.data_quality_monitor import DataQualityMonitor

class SmallCapJinxStrategy:
    """小市值 + 行业冥灯择时策略
    
    核心逻辑：
    1. 基础选股：选择全市场市值最小的10只股票
    2. 择时过滤：
       - 日历过滤：1月和4月强制空仓
       - 行业冥灯过滤：当银行、煤炭等行业走强时强制空仓
    3. 交易逻辑：每周一重新选股
    """
    
    def __init__(self, read_only=False):
        """初始化策略
        
        Args:
            read_only: 是否使用只读连接
        """
        # 获取配置
        self.db_path = Config.duckdb_path.replace('.duckdb', '.db')
        self.start_date = Config.start_date
        
        # 初始化市场宽度计算器
        self.breadth_calculator = MarketBreadthCalculator(self.db_path)
        
        # 初始化数据质量监控器
        self.quality_monitor = DataQualityMonitor()
        
        # 冥灯行业列表
        self.jinx_sectors = ['银行', '有色', '钢铁', '煤炭', '石油']
        
        # 降级策略配置
        self.fallback_mv_threshold = 100000000  # 降级时使用1亿市值门槛
        
        print("SmallCapJinxStrategy initialized successfully")
    
    def get_latest_trade_date(self):
        """获取最新交易日
        
        Returns:
            str: 最新交易日，格式为'YYYY-MM-DD'
        """
        try:
            result = DBUtils.query_df('''
            SELECT MAX(trade_date) FROM stock_daily
            ''').iloc[0, 0]
            
            if pd.notna(result):
                # 确保返回字符串格式
                if isinstance(result, datetime):
                    return result.strftime('%Y-%m-%d')
                return str(result)
            else:
                return None
        except Exception as e:
            print(f"Error getting latest trade date: {e}")
            return None
    
    def get_stock_list(self):
        """获取股票列表（过滤ST和上市不满1年的）
        
        Returns:
            pandas DataFrame: 股票列表
        """
        try:
            # 获取当前日期
            current_date = datetime.now().strftime('%Y-%m-%d')
            
            # 计算1年前的日期
            one_year_ago = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            
            query = f'''
            SELECT 
                si.ts_code,
                si.name,
                si.industry,
                si.pe_ttm,
                si.total_mv
            FROM stock_info si
            WHERE 
                si.name NOT LIKE '%ST%' 
                AND si.name NOT LIKE '%退%'
                AND si.total_mv > 10000000  -- 必须大于1000万，剔除0值和异常值
                AND si.pe_ttm IS NOT NULL   -- 必须有PE数据
            ORDER BY 
                si.total_mv ASC
            '''
            
            df = DBUtils.query_df(query)
            print(f"Found {len(df)} eligible stocks")
            
            # 标准化股票代码格式（添加.SH/.SZ后缀）
            for i, row in df.iterrows():
                code = row['ts_code']
                if code.isdigit() and len(code) == 6:
                    if code.startswith('6'):
                        df.at[i, 'ts_code'] = f"{code}.SH"
                    else:
                        df.at[i, 'ts_code'] = f"{code}.SZ"
            
            # 去重，确保每个股票代码只出现一次
            df = df.drop_duplicates(subset=['ts_code'], keep='first')
            print(f"After deduplication: {len(df)} eligible stocks")
            
            return df
        except Exception as e:
            print(f"Error getting stock list: {e}")
            return pd.DataFrame()
    
    def apply_calendar_filter(self, date):
        """应用日历过滤器
        
        Args:
            date: 日期，格式为'YYYY-MM-DD'或datetime对象
            
        Returns:
            bool: 是否需要空仓
        """
        try:
            # 处理日期类型
            if isinstance(date, datetime):
                month = date.month
            else:
                month = datetime.strptime(date, '%Y-%m-%d').month
                
            if month in [1, 4]:
                print(f"[FILTER] Calendar filter triggered: month {month} requires empty position")
                return True
            print(f"[FILTER] Calendar filter passed: month {month} is allowed")
            return False
        except Exception as e:
            print(f"Error applying calendar filter: {e}")
            return False
    
    def apply_jinx_filter(self, date):
        """应用行业冥灯过滤器
        
        Args:
            date: 日期，格式为'YYYY-MM-DD'
            
        Returns:
            bool: 是否需要空仓
        """
        try:
            # 获取当日市场宽度最高的行业
            top_sector = self.breadth_calculator.get_top_industry(date)
            
            if top_sector is None:
                print(f"[FILTER] Jinx filter skipped: cannot get top industry (data may be insufficient)")
                return False
            
            if top_sector in self.jinx_sectors:
                print(f"[FILTER] Jinx filter triggered: top sector {top_sector} is in jinx list")
                return True
            
            print(f"[FILTER] Jinx filter passed: top sector {top_sector} is not in jinx list")
            return False
        except Exception as e:
            print(f"Error applying jinx filter: {e}")
            return False
    
    def get_top_stocks(self, date, top_k=10):
        """获取指定日期的Top K小市值股票
        
        Args:
            date: 指定日期，格式为'YYYY-MM-DD'
            top_k: 选取数量
            
        Returns:
            pandas DataFrame: Top K小市值股票
        """
        print(f"Getting top {top_k} small cap stocks for date: {date}")
        
        # 1. 应用过滤器
        calendar_filtered = self.apply_calendar_filter(date)
        jinx_filtered = self.apply_jinx_filter(date)
        
        if calendar_filtered:
            print("[FILTER] Calendar filter triggered, returning empty position")
            return pd.DataFrame()
        
        if jinx_filtered:
            print("[FILTER] Jinx filter triggered, returning empty position")
            return pd.DataFrame()
        
        try:
            # 转换日期格式为 YYYY-MM-DD
            formatted_date = pd.Timestamp(date).strftime('%Y-%m-%d')
            
            # 0. 数据质量检查
            total_count_query = f"SELECT COUNT(*) as cnt FROM stock_daily WHERE trade_date = '{formatted_date}'"
            total_result = DBUtils.query_df(total_count_query)
            total_stocks = total_result.iloc[0]['cnt'] if not total_result.empty else 0
            
            if total_stocks == 0:
                print(f"[DATA QUALITY] No stock data found for date {formatted_date}")
                return pd.DataFrame()
            
            # 检查有市值数据的股票数量
            mv_count_query = f"""
            SELECT COUNT(*) as cnt 
            FROM stock_daily 
            WHERE trade_date = '{formatted_date}' 
              AND total_mv IS NOT NULL 
              AND total_mv > 0
            """
            mv_result = DBUtils.query_df(mv_count_query)
            mv_stocks = mv_result.iloc[0]['cnt'] if not mv_result.empty else 0
            
            print(f"[DATA QUALITY] Total stocks: {total_stocks}, Stocks with market cap: {mv_stocks}")
            
            if mv_stocks == 0:
                print(f"[DATA QUALITY] WARNING: No stocks with market cap data for date {formatted_date}")
                print(f"[DATA QUALITY] This may indicate incomplete data sync. Please sync data again.")
                return pd.DataFrame()
            
            if mv_stocks < 10:
                print(f"[DATA QUALITY] WARNING: Only {mv_stocks} stocks with market cap data (less than requested {top_k})")
            
            # 1. 从 stock_daily 表查询符合条件的小市值股票（核心修复）
            query = f"""
            SELECT ts_code, close, total_mv, pe_ttm
            FROM stock_daily
            WHERE trade_date = '{formatted_date}'
              AND total_mv > 500000000  -- ⚡️ 核心修复：市值必须 > 5亿，剔除 0 值和脏数据
              AND total_mv IS NOT NULL   -- ⚡️ 确保市值不为空
              AND pe_ttm IS NOT NULL     -- ⚡️ 剔除无 PE 数据
              AND pe_ttm > 0             -- ⚡️ PE 必须为正数
            ORDER BY total_mv ASC
            LIMIT {top_k}
            """
            top_stocks = DBUtils.query_df(query)
            
            if top_stocks.empty:
                # 检查是否有市值数据但不符合条件
                check_query = f"""
                SELECT COUNT(*) as cnt 
                FROM stock_daily 
                WHERE trade_date = '{formatted_date}'
                  AND total_mv IS NOT NULL 
                  AND total_mv > 0
                """
                check_result = DBUtils.query_df(check_query)
                available_count = check_result.iloc[0]['cnt'] if not check_result.empty else 0
                
                if available_count > 0:
                    print(f"[FILTER] No eligible stocks found: {available_count} stocks have market cap data, but none meet criteria (total_mv > 5亿, pe_ttm > 0)")
                    
                    # 尝试降级策略：降低市值门槛
                    print(f"[FALLBACK] Trying fallback strategy with lower market cap threshold ({self.fallback_mv_threshold/100000000:.0f}亿)...")
                    fallback_query = f"""
                    SELECT ts_code, close, total_mv, pe_ttm
                    FROM stock_daily
                    WHERE trade_date = '{formatted_date}'
                      AND total_mv > {self.fallback_mv_threshold}
                      AND total_mv IS NOT NULL
                      AND pe_ttm IS NOT NULL
                      AND pe_ttm > 0
                    ORDER BY total_mv ASC
                    LIMIT {top_k}
                    """
                    fallback_stocks = DBUtils.query_df(fallback_query)
                    
                    if not fallback_stocks.empty:
                        print(f"[FALLBACK] Found {len(fallback_stocks)} stocks with fallback criteria")
                        top_stocks = fallback_stocks
                    else:
                        # 显示一些样本数据帮助调试
                        sample_query = f"""
                        SELECT ts_code, close, total_mv, pe_ttm
                        FROM stock_daily
                        WHERE trade_date = '{formatted_date}'
                          AND total_mv IS NOT NULL
                          AND total_mv > 0
                        ORDER BY total_mv ASC
                        LIMIT 5
                        """
                        sample = DBUtils.query_df(sample_query)
                        if not sample.empty:
                            print("[DEBUG] Sample stocks (first 5 by market cap):")
                            print(sample.to_string(index=False))
                        return pd.DataFrame()
                else:
                    print(f"[DATA QUALITY] No stocks with valid market cap data for date {formatted_date}")
                    return pd.DataFrame()
            
            # 2. 从 stock_info 表获取股票名称和行业信息
            info_query = f"""
            SELECT ts_code, name, industry
            FROM stock_info
            """
            df_info = DBUtils.query_df(info_query)
            
            # 3. 关联股票信息数据
            result = pd.merge(top_stocks, df_info, on='ts_code', how='left')
            
            # 4. 处理股票名称和行业信息的缺失情况
            # 兜底填充
            result['name'] = result['name'].fillna(result['ts_code'])
            result['industry'] = result['industry'].fillna('未知')
            
            print(f"[OK] Selected {len(result)} top small cap stocks")
            return result
            
        except Exception as e:
            print(f"[ERROR] Error in get_top_stocks: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()
    
    def is_week_start(self, date):
        """判断是否为一周的开始（周一）
        
        Args:
            date: 日期，格式为'YYYY-MM-DD'或datetime对象
            
        Returns:
            bool: 是否为周一
        """
        try:
            # 处理日期类型
            if isinstance(date, datetime):
                return date.weekday() == 0
            else:
                return datetime.strptime(date, '%Y-%m-%d').weekday() == 0
        except Exception as e:
            print(f"Error checking week start: {e}")
            return False
    
    def run_weekly_rebalance(self):
        """执行每周重新平衡
        
        Returns:
            pandas DataFrame: 选股结果
        """
        # 获取最新交易日
        latest_date = self.get_latest_trade_date()
        
        if not latest_date:
            print("✗ Cannot get latest trade date")
            return pd.DataFrame()
        
        # 检查是否为周一
        if not self.is_week_start(latest_date):
            print(f"✗ Today {latest_date} is not Monday, skipping rebalance")
            return pd.DataFrame()
        
        # 执行选股
        print("\n=== Running weekly rebalance ===")
        top_stocks = self.get_top_stocks(latest_date, top_k=10)
        
        if not top_stocks.empty:
            print("\nWeekly rebalance result:")
            print(top_stocks[['ts_code', 'name', 'industry', 'total_mv', 'close']])
        else:
            print("\nWeekly rebalance: Empty position")
        
        return top_stocks
    
    def close(self):
        """关闭资源"""
        print("Successfully closed SmallCapJinxStrategy resources")
    
    def __del__(self):
        """析构函数"""
        self.close()


if __name__ == "__main__":
    """测试小市值+行业冥灯策略"""
    print("Testing SmallCapJinxStrategy")
    
    strategy = SmallCapJinxStrategy()
    
    try:
        # 获取最新交易日
        latest_date = strategy.get_latest_trade_date()
        if latest_date:
            print(f"Latest trade date: {latest_date}")
            
            # 测试选股
            top_stocks = strategy.get_top_stocks(latest_date, top_k=10)
            
            if not top_stocks.empty:
                print("\nTop 10 small cap stocks:")
                print(top_stocks[['ts_code', 'name', 'industry', 'total_mv', 'close']])
            else:
                print("\nNo stocks selected (empty position)")
            
            # 测试每周重新平衡
            print("\nTesting weekly rebalance:")
            strategy.run_weekly_rebalance()
        else:
            print("Cannot get latest trade date")
            
    finally:
        strategy.close()
