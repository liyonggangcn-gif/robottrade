import os

# 强制禁用系统代理，防止 requests 走 VPN 导致连接国内源失败
os.environ['http_proxy'] = ''
os.environ['https_proxy'] = ''
os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''
os.environ['all_proxy'] = ''
os.environ['ALL_PROXY'] = ''

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import efinance as ef
import pandas as pd
import duckdb
import time
from loguru import logger
from src.factors.alpha_engine import AlphaEngine

logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")

def fetch_fundamentals_batch(stock_codes, max_retries=3, delay=2):
    """
    批量获取股票基本面数据
    
    Args:
        stock_codes: 股票代码列表
        max_retries: 最大重试次数
        delay: 重试延迟（秒）
        
    Returns:
        包含基本面数据的DataFrame
    """
    for attempt in range(max_retries):
        try:
            # 使用efinance获取股票基本信息
            df = ef.stock.get_base_info(stock_codes)
            
            if df is None or len(df) == 0:
                raise Exception("efinance返回空数据")
                
            return df
            
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"⚠️ 获取基本面数据失败 (第 {attempt+1}/{max_retries} 次): {e}")
                logger.info(f"⚠️ {delay}秒后重试...")
                time.sleep(delay)
            else:
                logger.error(f"🚫 达到最大重试次数，获取失败")
                raise

def patch_fundamentals_efinance():
    """
    使用efinance补全基本面数据
    """
    logger.info("=" * 80)
    logger.info("开始补全基本面数据（使用efinance）")
    logger.info("=" * 80)
    
    try:
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'quant.db')
        
        logger.info("正在连接数据库...")
        conn = duckdb.connect(db_path)
        
        # 获取所有A股股票代码
        logger.info("正在获取A股股票列表...")
        stock_codes = conn.execute("""
            SELECT ts_code
            FROM stock_info
            WHERE ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ'
        """).fetchdf()
        
        logger.success(f"✅ 成功获取 {len(stock_codes)} 只A股股票")
        
        if len(stock_codes) == 0:
            logger.warning("⚠️ 没有找到A股股票")
            return
        
        # 转换股票代码格式（efinance需要去掉后缀）
        stock_codes_list = stock_codes['ts_code'].apply(
            lambda x: x.replace('.SH', '').replace('.SZ', '')
        ).tolist()
        
        logger.info(f"正在获取 {len(stock_codes_list)} 只股票的基本面数据...")
        
        # 分批获取数据（每次100只）
        batch_size = 100
        all_data = []
        
        for i in range(0, len(stock_codes_list), batch_size):
            batch_codes = stock_codes_list[i:i+batch_size]
            logger.info(f"正在处理第 {i//batch_size + 1} 批，股票数量: {len(batch_codes)}")
            
            try:
                df_batch = fetch_fundamentals_batch(batch_codes, max_retries=2, delay=2)
                all_data.append(df_batch)
                
                # 避免请求过快
                time.sleep(1)
                
            except Exception as e:
                logger.warning(f"⚠️ 批次 {i//batch_size + 1} 获取失败: {e}")
                continue
        
        if len(all_data) == 0:
            logger.error("🚫 没有获取到任何基本面数据")
            return
        
        # 合并所有批次的数据
        df_fundamentals = pd.concat(all_data, ignore_index=True)
        logger.success(f"✅ 成功获取 {len(df_fundamentals)} 只股票的基本面数据")
        
        # 显示列名
        logger.info(f"数据列: {df_fundamentals.columns.tolist()}")
        
        # 数据清洗和映射
        logger.info("正在清洗和映射数据...")
        
        # 检查可用的列
        available_columns = df_fundamentals.columns.tolist()
        logger.info(f"可用列: {available_columns}")
        
        # 尝试映射列名
        column_mapping = {}
        
        # 检查是否有股票代码列
        if '股票代码' in available_columns:
            column_mapping['股票代码'] = 'symbol'
        elif '代码' in available_columns:
            column_mapping['代码'] = 'symbol'
        
        # 检查是否有股票名称列
        if '股票名称' in available_columns:
            column_mapping['股票名称'] = 'name'
        elif '名称' in available_columns:
            column_mapping['名称'] = 'name'
        
        # 检查是否有PE列
        if '市盈率-动态' in available_columns:
            column_mapping['市盈率-动态'] = 'pe_ttm'
        elif '市盈率' in available_columns:
            column_mapping['市盈率'] = 'pe_ttm'
        elif 'PE' in available_columns:
            column_mapping['PE'] = 'pe_ttm'
        
        # 检查是否有PB列
        if '市净率' in available_columns:
            column_mapping['市净率'] = 'pb'
        elif 'PB' in available_columns:
            column_mapping['PB'] = 'pb'
        
        # 检查是否有市值列
        if '总市值' in available_columns:
            column_mapping['总市值'] = 'total_mv'
        elif '市值' in available_columns:
            column_mapping['市值'] = 'total_mv'
        
        logger.info(f"列名映射: {column_mapping}")
        
        # 重命名列
        df_clean = df_fundamentals.rename(columns=column_mapping)
        
        # 只保留需要的列
        required_columns = ['symbol', 'name', 'pe_ttm', 'pb', 'total_mv']
        df_clean = df_clean[[col for col in required_columns if col in df_clean.columns]]
        
        # 标准化股票代码（添加后缀）
        if 'symbol' in df_clean.columns:
            df_clean['ts_code'] = df_clean['symbol'].apply(
                lambda x: f"{x}.SH" if str(x).startswith('6') else f"{x}.SZ" if str(x).startswith(('0', '3')) else x
            )
        
        # 清洗基本面数据
        if 'pe_ttm' in df_clean.columns:
            df_clean['pe_ttm'] = pd.to_numeric(df_clean['pe_ttm'], errors='coerce')
        
        if 'pb' in df_clean.columns:
            df_clean['pb'] = pd.to_numeric(df_clean['pb'], errors='coerce')
        
        if 'total_mv' in df_clean.columns:
            df_clean['total_mv'] = pd.to_numeric(df_clean['total_mv'], errors='coerce')
        
        # 过滤掉没有有效数据的股票
        df_clean = df_clean.dropna(subset=['pe_ttm', 'total_mv'])
        df_clean = df_clean[(df_clean['pe_ttm'] > 0) & (df_clean['total_mv'] > 0)]
        
        logger.success(f"✅ 清洗后剩余 {len(df_clean)} 只有效股票")
        
        # 更新stock_info表
        logger.info("正在更新stock_info表...")
        
        # 创建临时表
        conn.execute('''
            CREATE OR REPLACE TEMP TABLE temp_fundamentals AS
            SELECT 
                ts_code,
                pe_ttm,
                pb,
                total_mv
            FROM df_clean
        ''')
        
        # 更新stock_info表
        update_query = '''
            UPDATE stock_info
            SET 
                pe_ttm = tf.pe_ttm,
                pb = tf.pb,
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
        
        # 显示一些示例数据
        result = conn.execute('''
            SELECT ts_code, name, pe_ttm, pb, total_mv
            FROM stock_info
            WHERE ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ'
            AND pe_ttm IS NOT NULL
            AND total_mv IS NOT NULL
            AND pe_ttm > 0
            AND total_mv > 0
            ORDER BY total_mv DESC
            LIMIT 10
        ''').fetchdf()
        
        logger.info("\n市值前10的股票基本面数据:")
        logger.info(result.to_string(index=False))
        
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
    patch_fundamentals_efinance()
