import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
import pandas as pd
from loguru import logger

logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")

def update_stock_info_from_daily():
    """从stock_daily表中提取A股的基本面数据并更新到stock_info表"""
    logger.info("=" * 80)
    logger.info("开始从stock_daily表更新stock_info表")
    logger.info("=" * 80)
    
    conn = duckdb.connect('data/quant.db')
    
    try:
        # 从stock_daily表中提取A股的基本面数据
        logger.info("正在从stock_daily表提取A股基本面数据...")
        
        query = '''
        SELECT DISTINCT
            sd.ts_code,
            SUBSTRING(sd.ts_code FROM 1 FOR 6) as code,
            CASE 
                WHEN sd.ts_code LIKE '%.SH' THEN '主板'
                WHEN sd.ts_code LIKE '%.SZ' THEN '主板'
                ELSE '其他'
            END as market,
            latest_pe.pe_ttm,
            latest_mv.total_mv
        FROM (
            SELECT DISTINCT ts_code
            FROM stock_daily
            WHERE ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ'
        ) sd
        LEFT JOIN (
            SELECT 
                ts_code, 
                pe_ttm
            FROM stock_daily
            WHERE pe_ttm IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) = 1
        ) latest_pe ON sd.ts_code = latest_pe.ts_code
        LEFT JOIN (
            SELECT 
                ts_code, 
                total_mv
            FROM stock_daily
            WHERE total_mv IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) = 1
        ) latest_mv ON sd.ts_code = latest_mv.ts_code
        '''
        
        df_a_share = conn.execute(query).fetchdf()
        logger.success(f"提取到 {len(df_a_share)} 只A股的基本面数据")
        
        # 添加名称列（暂时用股票代码代替）
        df_a_share['name'] = df_a_share['code']
        df_a_share['pb'] = 0.0
        
        # 只保留需要的列
        df_a_share = df_a_share[['ts_code', 'name', 'market', 'pe_ttm', 'pb', 'total_mv']]
        
        # 使用DuckDB Native Dataframe Insert
        logger.info("正在更新stock_info表...")
        try:
            # 先删除所有A股数据
            conn.execute('DELETE FROM stock_info WHERE ts_code LIKE \'%.SH\' OR ts_code LIKE \'%.SZ\'')
            logger.info("已删除旧的A股数据")
            
            # 插入新的A股数据
            conn.register('temp_a_share_df', df_a_share)
            
            conn.execute('''
                INSERT INTO stock_info 
                SELECT ts_code, name, market, pe_ttm, pb, total_mv
                FROM temp_a_share_df
            ''')
            
            logger.success(f"✅ 成功更新: {len(df_a_share)} 条A股股票信息")
            
        except Exception as e:
            logger.warning(f"⚠️ 更新股票信息失败: {e}")
        finally:
            # 清理临时视图
            try:
                conn.unregister('temp_a_share_df')
            except:
                pass
        
        # 验证结果
        logger.info("\n" + "=" * 80)
        logger.info("验证结果")
        logger.info("=" * 80)
        
        result = conn.execute('SELECT COUNT(*) as count FROM stock_info').fetchone()
        logger.info(f"stock_info表中共有 {result[0]} 条记录")
        
        result = conn.execute('SELECT COUNT(*) as count FROM stock_info WHERE ts_code LIKE \'%.SH\' OR ts_code LIKE \'%.SZ\'').fetchone()
        logger.info(f"其中A股: {result[0]} 条")
        
        result = conn.execute('SELECT COUNT(*) as count FROM stock_info WHERE ts_code NOT LIKE \'%.SH\' AND ts_code NOT LIKE \'%.SZ\'').fetchone()
        logger.info(f"其他股票: {result[0]} 条")
        
        logger.success("\n" + "=" * 80)
        logger.success("stock_info表更新完成！")
        logger.success("=" * 80)
        
    except Exception as e:
        logger.error(f"更新过程中发生错误: {e}")
        import traceback
        logger.error(traceback.format_exc())
    
    finally:
        conn.close()
        logger.info("数据库连接已关闭")

if __name__ == "__main__":
    update_stock_info_from_daily()
