import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 初始化日志（输出到 logs/batch_update_ashare_YYYYMMDD.log + 控制台）
from src.utils.log_utils import init_logger
_logger = init_logger("batch_update_ashare")

from src.collector.hybrid_loader import HybridLoader
from loguru import logger
import pandas as pd
from datetime import datetime
from tqdm import tqdm
import time

logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")
logger.add(os.path.join(os.path.dirname(__file__), '..', 'logs', f'batch_update_ashare_{datetime.now().strftime("%Y%m%d")}.log'),
           format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}", encoding="utf-8")


def batch_update_ashare_stocks():
    """批量更新A股数据"""
    logger.info("=" * 80)
    logger.info("开始批量更新A股数据")
    logger.info("=" * 80)
    
    loader = HybridLoader()
    
    try:
        logger.info("\n" + "=" * 80)
        logger.info("步骤1: 获取全市场A股股票列表")
        logger.info("=" * 80)
        
        stock_list = loader.get_stock_list(full_market=True)
        
        if stock_list.empty:
            logger.error("未能获取股票列表")
            return
        
        logger.info(f"获取到 {len(stock_list)} 只股票")
        
        a_share_stocks = stock_list[stock_list['market'] == 'A']
        logger.info(f"其中A股: {len(a_share_stocks)} 只")
        
        if len(a_share_stocks) == 0:
            logger.warning("没有A股股票，跳过")
            return
        
        logger.info(f"\n前10只A股股票:")
        logger.info(a_share_stocks.head(10).to_string())
        
        logger.info("\n" + "=" * 80)
        logger.info("步骤2: 批量获取股票历史数据")
        logger.info("=" * 80)
        
        start_date = '20200101'
        end_date = datetime.now().strftime('%Y%m%d')
        
        logger.info(f"时间范围: {start_date} ~ {end_date}")
        
        success_count = 0
        fail_count = 0
        total_count = len(a_share_stocks)
        
        for idx, row in tqdm(a_share_stocks.iterrows(), total=total_count, desc="更新A股数据"):
            ts_code = row['ts_code']
            name = row.get('name', ts_code)
            
            try:
                df = loader.fetch_data(ts_code, start_date=start_date, end_date=end_date)
                
                if not df.empty:
                    loader.save_to_database(df)
                    success_count += 1
                    
                    if success_count % 100 == 0:
                        logger.info(f"已成功更新 {success_count}/{total_count} 只股票")
                else:
                    fail_count += 1
                    logger.warning(f"未能获取 {ts_code} ({name}) 的数据")
                
                time.sleep(0.1)
                
            except Exception as e:
                fail_count += 1
                logger.error(f"处理 {ts_code} ({name}) 时出错: {e}")
        
        logger.info("\n" + "=" * 80)
        logger.info("步骤3: 保存股票信息")
        logger.info("=" * 80)
        
        loader.save_stock_info(stock_list)
        
        logger.info("\n" + "=" * 80)
        logger.info("步骤4: 验证数据更新结果")
        logger.info("=" * 80)
        
        result = loader.conn.execute('''
        SELECT COUNT(*) as count FROM stock_daily
        ''').fetchone()
        
        logger.info(f"数据库中共有 {result[0]} 条日线数据")
        
        result = loader.conn.execute('''
        SELECT COUNT(*) as count FROM stock_info
        ''').fetchone()
        
        logger.info(f"数据库中共有 {result[0]} 条股票信息")
        
        result = loader.conn.execute('''
        SELECT COUNT(DISTINCT ts_code) as count FROM stock_daily
        ''').fetchone()
        
        logger.info(f"数据库中共有 {result[0]} 只股票的日线数据")
        
        result = loader.conn.execute('''
        SELECT ts_code, COUNT(*) as count 
        FROM stock_daily 
        GROUP BY ts_code 
        ORDER BY count DESC 
        LIMIT 10
        ''').fetchdf()
        
        logger.info(f"\n数据最多的10只股票:")
        logger.info(result.to_string())
        
        logger.success("\n" + "=" * 80)
        logger.success("批量更新完成！")
        logger.success(f"成功: {success_count} 只股票")
        logger.success(f"失败: {fail_count} 只股票")
        logger.success(f"总计: {total_count} 只股票")
        logger.success("=" * 80)
        
    except Exception as e:
        logger.error(f"批量更新过程中发生错误: {e}")
        import traceback
        logger.error(traceback.format_exc())
    
    finally:
        loader.close()


if __name__ == "__main__":
    batch_update_ashare_stocks()
