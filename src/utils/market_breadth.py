import pandas as pd
from datetime import datetime
from src.utils.db_utils import DBUtils

class MarketBreadthCalculator:
    """行业市场宽度计算器
    
    用于计算特定日期的行业市场宽度，即行业内股票Close > MA20的比例
    """
    
    def __init__(self, db_path='data/quant.db'):
        """初始化市场宽度计算器
        
        Args:
            db_path: DuckDB数据库路径
        """
        self.db_path = db_path
    
    def calculate_industry_breadth(self, date):
        """计算指定日期的行业市场宽度
        
        Args:
            date: 指定日期，格式为'YYYY-MM-DD'
            
        Returns:
            dict: 行业名称到市场宽度的映射
        """
        try:
            # 计算每只股票的MA20
            print(f"Calculating industry breadth for date: {date}")
            
            # 1. 计算每只股票的MA20
            ma20_query = f'''
            WITH stock_ma20 AS (
                SELECT 
                    ts_code,
                    trade_date,
                    close,
                    AVG(close) OVER (
                        PARTITION BY ts_code 
                        ORDER BY trade_date 
                        ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                    ) as ma20
                FROM stock_daily
                WHERE trade_date <= '{date}'
            ),
            latest_data AS (
                SELECT 
                    sm.ts_code,
                    sm.trade_date,
                    sm.close,
                    sm.ma20,
                    si.industry
                FROM stock_ma20 sm
                LEFT JOIN stock_info si ON sm.ts_code = si.ts_code
                WHERE sm.trade_date = '{date}'
                AND (si.industry IS NOT NULL AND si.industry != '')
            ),
            industry_stats AS (
                SELECT 
                    industry,
                    COUNT(*) as total_stocks,
                    SUM(CASE WHEN close > ma20 THEN 1 ELSE 0 END) as above_ma20,
                    CASE 
                        WHEN COUNT(*) > 0 THEN 
                            SUM(CASE WHEN close > ma20 THEN 1 ELSE 0 END) * 1.0 / COUNT(*)
                        ELSE 0
                    END as breadth
                FROM latest_data
                GROUP BY industry
                HAVING COUNT(*) >= 5  -- 只考虑股票数量>=5的行业
            )
            SELECT 
                industry,
                total_stocks,
                above_ma20,
                breadth
            FROM industry_stats
            ORDER BY breadth DESC
            '''
            
            df_breadth = DBUtils.query_df(ma20_query)
            
            if df_breadth.empty:
                print(f"No industry data available for date: {date}")
                return {}
            
            # 转换为字典
            breadth_dict = {}
            for _, row in df_breadth.iterrows():
                breadth_dict[row['industry']] = row['breadth']
            
            print(f"Successfully calculated breadth for {len(breadth_dict)} industries")
            print("Top 5 industries by breadth:")
            for industry, breadth in sorted(breadth_dict.items(), key=lambda x: x[1], reverse=True)[:5]:
                print(f"  {industry}: {breadth:.4f}")
            
            return breadth_dict
            
        except Exception as e:
            print(f"Error calculating industry breadth: {e}")
            return {}
    
    def get_top_industry(self, date):
        """获取指定日期市场宽度最高的行业
        
        Args:
            date: 指定日期，格式为'YYYY-MM-DD'
            
        Returns:
            str: 市场宽度最高的行业名称
        """
        breadth_dict = self.calculate_industry_breadth(date)
        
        if not breadth_dict:
            return None
        
        # 按市场宽度排序，返回最高的
        top_industry = max(breadth_dict.items(), key=lambda x: x[1])[0]
        print(f"Top industry on {date}: {top_industry} (breadth: {breadth_dict[top_industry]:.4f})")
        
        return top_industry
    



if __name__ == "__main__":
    """测试行业宽度计算器"""
    import sys
    
    if len(sys.argv) > 1:
        test_date = sys.argv[1]
    else:
        # 默认使用当前日期
        test_date = datetime.now().strftime('%Y-%m-%d')
    
    print(f"Testing MarketBreadthCalculator for date: {test_date}")
    
    calculator = MarketBreadthCalculator()
    try:
        top_industry = calculator.get_top_industry(test_date)
        print(f"\nFinal result: Top industry on {test_date} is {top_industry}")
    finally:
        calculator.close()
