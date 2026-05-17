import os

# 强制禁用系统代理，防止 requests 走 VPN 导致连接国内源失败
for _k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_k, None)

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak
import efinance as ef
import pandas as pd
import duckdb
import time
from loguru import logger
from src.factors.alpha_engine import AlphaEngine

logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")

def fetch_with_retry(func, max_retries=3, delay=5, *args, **kwargs):
    """
    带重试机制的数据获取函数
    
    Args:
        func: 要执行的函数
        max_retries: 最大重试次数
        delay: 重试延迟（秒）
        *args, **kwargs: 函数参数
        
    Returns:
        函数执行结果
    """
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"⚠️ 数据获取失败 (第 {attempt+1}/{max_retries} 次): {e}")
                logger.info(f"⚠️ {delay}秒后重试...")
                time.sleep(delay)
            else:
                logger.error(f"🚫 达到最大重试次数，获取失败")
                raise

def fetch_fundamentals_from_efinance():
    """
    使用efinance获取基本面数据
    
    Returns:
        包含基本面数据的DataFrame
    """
    logger.info("正在使用efinance获取A股实时数据...")
    
    try:
        # 获取A股实时行情数据
        df = ef.stock.get_realtime_quotes()
        
        if df is None or len(df) == 0:
            raise Exception("efinance返回空数据")
            
        logger.success(f"✅ 成功获取 {len(df)} 只股票的实时数据")
        return df
        
    except Exception as e:
        logger.error(f"efinance获取数据失败: {e}")
        raise

def fetch_fundamentals_from_akshare():
    """
    使用akshare获取基本面数据
    
    Returns:
        包含基本面数据的DataFrame
    """
    logger.info("正在使用akshare获取A股实时数据...")
    
    try:
        df = ak.stock_zh_a_spot_em()
        
        if df is None or len(df) == 0:
            raise Exception("akshare返回空数据")
            
        logger.success(f"✅ 成功获取 {len(df)} 只股票的实时数据")
        return df
        
    except Exception as e:
        logger.error(f"akshare获取数据失败: {e}")
        raise

def standardize_ts_code(symbol):
    """
    将股票代码转换为标准格式（添加后缀）
    
    Args:
        symbol: 股票代码（如 600519）
        
    Returns:
        标准化的股票代码（如 600519.SH）
    """
    symbol = str(symbol).strip()
    
    if symbol.startswith('6'):
        return f"{symbol}.SH"
    elif symbol.startswith(('0', '3')):
        return f"{symbol}.SZ"
    else:
        return symbol

def clean_fundamental_value(value):
    """
    清洗基本面数据值
    
    Args:
        value: 原始值
        
    Returns:
        清洗后的float值，如果是无效值则返回None
    """
    if pd.isna(value):
        return None
    
    value_str = str(value).strip()
    
    if value_str in ['-', '--', 'None', 'nan', 'NaN', '']:
        return None
    
    try:
        return float(value_str)
    except (ValueError, TypeError):
        return None

def patch_fundamentals():
    """
    使用AkShare或efinance补全基本面数据
    """
    logger.info("=" * 80)
    logger.info("开始补全基本面数据")
    logger.info("=" * 80)
    
    source = None  # 用于记录数据源
    
    try:
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'quant.db')
        
        # 尝试使用efinance获取数据
        try:
            df_spot = fetch_with_retry(fetch_fundamentals_from_efinance, max_retries=2, delay=3)
            source = 'efinance'
        except Exception as e:
            logger.warning(f"⚠️ efinance获取失败，尝试使用akshare: {e}")
            df_spot = fetch_with_retry(fetch_fundamentals_from_akshare, max_retries=2, delay=3)
            source = 'akshare'
        
        logger.success(f"✅ 成功从 {source} 获取 {len(df_spot)} 只股票的实时数据")
        
        logger.info("正在清洗数据...")
        
        # 根据数据源进行列名映射
        if source == 'efinance':
            # efinance的列名
            column_mapping = {
                '股票代码': 'symbol',
                '股票名称': 'name',
                '市盈率-动态': 'pe_ttm',
                '市净率': 'pb',
                '总市值': 'total_mv'
            }
        else:
            # akshare的列名
            column_mapping = {
                '代码': 'symbol',
                '名称': 'name',
                '市盈率-动态': 'pe_ttm',
                '市净率': 'pb',
                '总市值': 'total_mv'
            }
        
        # 重命名列
        df_clean = df_spot.rename(columns=column_mapping)
        
        # 只保留需要的列
        required_columns = ['symbol', 'name', 'pe_ttm', 'pb', 'total_mv']
        df_clean = df_clean[[col for col in required_columns if col in df_clean.columns]]
        
        # 标准化股票代码（添加后缀）
        df_clean['ts_code'] = df_clean['symbol'].apply(standardize_ts_code)
        
        # 清洗基本面数据
        df_clean['pe_ttm'] = df_clean['pe_ttm'].apply(clean_fundamental_value)
        df_clean['pb'] = df_clean['pb'].apply(clean_fundamental_value)
        df_clean['total_mv'] = df_clean['total_mv'].apply(clean_fundamental_value)
        
        # 过滤掉没有有效数据的股票
        df_clean = df_clean.dropna(subset=['pe_ttm', 'total_mv'])
        
        logger.success(f"✅ 清洗后剩余 {len(df_clean)} 只有效股票")
        
        # 连接数据库
        logger.info("正在连接数据库...")
        conn = duckdb.connect(db_path)
        
        # 检查stock_info表是否存在
        table_exists = conn.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'stock_info'
            )
        """).fetchone()[0]
        
        if not table_exists:
            logger.error("stock_info表不存在，请先初始化数据库")
            return
        
        # 获取当前stock_info表中的A股股票
        logger.info("正在获取当前数据库中的A股股票...")
        existing_stocks = conn.execute("""
            SELECT ts_code, name, market
            FROM stock_info
            WHERE ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ'
        """).fetchdf()
        
        logger.info(f"数据库中现有 {len(existing_stocks)} 只A股股票")
        
        # 合并数据：优先使用AkShare的数据
        logger.info("正在更新基本面数据...")
        
        # 创建临时表
        conn.execute('''
            CREATE OR REPLACE TEMP TABLE temp_fundamentals AS
            SELECT 
                ts_code,
                name,
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
        ''').fetchone()
        
        logger.info(f"有有效PE和市值数据的A股股票: {result[0]} 只")
        
        # 显示一些示例数据
        result = conn.execute('''
            SELECT ts_code, name, pe_ttm, pb, total_mv
            FROM stock_info
            WHERE ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ'
            AND pe_ttm IS NOT NULL
            AND total_mv IS NOT NULL
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
    patch_fundamentals()
