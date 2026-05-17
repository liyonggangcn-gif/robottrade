import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.factors.alpha_engine import AlphaEngine
from loguru import logger

logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")

def recalculate_all_factors():
    """重新计算所有因子"""
    logger.info("=" * 80)
    logger.info("开始重新计算所有因子")
    logger.info("=" * 80)
    
    try:
        engine = AlphaEngine()
        
        # 删除所有因子数据
        logger.info("正在删除旧因子数据...")
        engine.conn.execute('DELETE FROM stock_factors')
        logger.success("已删除所有因子数据")
        
        # 重新计算所有因子
        logger.info("正在计算所有因子...")
        df = engine.get_stock_daily_data()
        logger.info(f"加载了 {len(df)} 条日线数据")
        
        factor_df = engine.calculate_factors(df)
        logger.info(f"计算了 {len(factor_df)} 条因子数据")
        
        # 保存因子
        logger.info("正在保存因子数据...")
        engine.save_factors(factor_df)
        logger.success("因子数据保存成功")
        
        # 验证结果
        logger.info("\n" + "=" * 80)
        logger.info("验证结果")
        logger.info("=" * 80)
        
        result = engine.conn.execute('SELECT COUNT(*) as count FROM stock_factors').fetchone()
        logger.info(f"因子表中共有 {result[0]} 条记录")
        
        result = engine.conn.execute('SELECT COUNT(DISTINCT ts_code) as count FROM stock_factors').fetchone()
        logger.info(f"因子表中共有 {result[0]} 只股票的因子数据")
        
        result = engine.conn.execute('SELECT MIN(trade_date) as min_date, MAX(trade_date) as max_date FROM stock_factors').fetchone()
        logger.info(f"因子数据时间范围: {result[0]} ~ {result[1]}")
        
        engine.close()
        
        logger.success("\n" + "=" * 80)
        logger.success("因子重新计算完成！")
        logger.success("=" * 80)
        
    except Exception as e:
        logger.error(f"重新计算因子过程中发生错误: {e}")
        import traceback
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    recalculate_all_factors()
