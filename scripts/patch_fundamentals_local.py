import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import duckdb
from loguru import logger
from src.factors.alpha_engine import AlphaEngine

logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")

def patch_fundamentals_from_local():
    """
    从本地stock_daily表中提取最新的基本面数据来补全stock_info表
    """
    logger.info("=" * 80)
    logger.info("开始补全基本面数据（从本地数据源）")
    logger.info("=" * 80)
    
    try:
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'quant.db')
        
        logger.info("正在连接数据库...")
        conn = duckdb.connect(db_path)
        
        # 检查stock_info表是否存在
        table_exists = conn.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_name = 'stock_info'
        """).fetchdf()
        
        if len(table_exists) == 0:
            logger.error("stock_info表不存在，请先初始化数据库")
            return
        
        # 从stock_daily表中提取每只股票最新的基本面数据
        logger.info("正在从stock_daily表提取最新的基本面数据...")
        
        query = '''
        SELECT 
            sd.ts_code,
            si.name,
            sd.pe_ttm,
            sd.total_mv
        FROM (
            SELECT 
                ts_code,
                pe_ttm,
                total_mv,
                ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) as rn
            FROM stock_daily
        ) sd
        LEFT JOIN stock_info si ON sd.ts_code = si.ts_code
        WHERE sd.rn = 1
        AND sd.pe_ttm IS NOT NULL
        AND sd.total_mv IS NOT NULL
        AND sd.pe_ttm > 0
        AND sd.total_mv > 0
        '''
        
        df_fundamentals = conn.execute(query).fetchdf()
        
        logger.success(f"✅ 成功提取 {len(df_fundamentals)} 只股票的基本面数据")
        
        if len(df_fundamentals) == 0:
            logger.warning("⚠️ 没有找到有效的基本面数据")
            return
        
        # 显示一些示例数据
        logger.info("\n基本面数据示例（市值前10）:")
        top_10 = df_fundamentals.nlargest(10, 'total_mv')
        logger.info(top_10.to_string(index=False))
        
        # 更新stock_info表
        logger.info("\n正在更新stock_info表...")
        
        # 创建临时表
        conn.execute('''
            CREATE OR REPLACE TEMP TABLE temp_fundamentals AS
            SELECT 
                ts_code,
                pe_ttm,
                pb,
                total_mv
            FROM df_fundamentals
        ''')
        
        # 更新stock_info表
        update_query = '''
            UPDATE stock_info
            SET 
                pe_ttm = tf.pe_ttm,
                total_mv = tf.total_mv
            FROM temp_fundamentals tf
            WHERE stock_info.ts_code = tf.ts_code
        '''
        
        result = conn.execute(update_query)
        updated_count = result.rowcount
        
        logger.success(f"✅ 成功更新 {updated_count} 只股票的基本面数据")
        
        # 验证更新结果
        logger.info("\n" + "=" * 80)
        logger.info("验证结果")
        logger.info("=" * 80)
        
        result = conn.execute('''
            SELECT COUNT(*) as count
            FROM stock_info
            WHERE ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ'
            AND pe_ttm IS NOT NULL
            AND total_mv IS NOT NULL
            AND pe_ttm > 0
            AND total_mv > 0
        ''').fetchone()
        
        logger.info(f"有有效PE和市值数据的A股股票: {result[0]} 只")
        
        result = conn.execute('''
            SELECT 
                COUNT(*) as total_count,
                COUNT(CASE WHEN pe_ttm IS NOT NULL AND pe_ttm > 0 THEN 1 END) as pe_count,
                COUNT(CASE WHEN total_mv IS NOT NULL AND total_mv > 0 THEN 1 END) as mv_count
            FROM stock_info
            WHERE ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ'
        ''').fetchone()
        
        logger.info(f"A股股票总数: {result[0]} 只")
        logger.info(f"有有效PE数据的股票: {result[1]} 只 ({result[1]/result[0]*100:.1f}%)")
        logger.info(f"有有效市值数据的股票: {result[2]} 只 ({result[2]/result[0]*100:.1f}%)")
        
        # 关闭数据库连接
        conn.close()
        
        # 自动触发因子刷新
        logger.info("\n" + "=" * 80)
        logger.info("正在重新计算因子...")
        logger.info("=" * 80)
        
        engine = AlphaEngine()
        engine.update_factors()
        engine.close()
        
        logger.success("\n" + "=" * 80)
        logger.success("✅ 基本面修复完成！")
        logger.success("=" * 80)
        
    except Exception as e:
        logger.error(f"补全基本面数据过程中发生错误: {e}")
        import traceback
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    patch_fundamentals_from_local()
