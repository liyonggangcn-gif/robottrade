import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
import pandas as pd
import tushare as ts
from tqdm import tqdm
from datetime import datetime
from loguru import logger

from src.utils.config_loader import Config

logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")


def fetch_with_retry(pro, api_name, max_retries=3, timeout=5, **kwargs):
    """带超时和重试机制的数据获取函数
    
    Args:
        pro: Tushare Pro API 客户端
        api_name: API名称 ('daily' 或 'daily_basic')
        max_retries: 最大重试次数
        timeout: 超时时间（秒）
        **kwargs: API参数
        
    Returns:
        DataFrame 或 None
    """
    for attempt in range(max_retries):
        try:
            if api_name == 'daily':
                df = pro.daily(**kwargs)
            elif api_name == 'daily_basic':
                df = pro.daily_basic(**kwargs)
            else:
                raise ValueError(f"不支持的API: {api_name}")
            
            return df
            
        except Exception as e:
            logger.warning(f"⚠️ {api_name} 请求失败 (第 {attempt+1}/{max_retries} 次): {e}")
            
            if attempt < max_retries - 1:
                logger.info("⚠️ 网络卡顿，3秒后重试...")
                time.sleep(3)
            else:
                logger.error("🚫 达到最大重试次数，跳过该请求")
                return None
    
    return None


def get_existing_stocks(conn):
    """获取数据库中已存在的股票代码
    
    Args:
        conn: DuckDB 连接
        
    Returns:
        set: 已存在的股票代码集合
    """
    try:
        result = conn.execute('SELECT DISTINCT ts_code FROM stock_daily').fetchall()
        return set(row[0] for row in result)
    except:
        return set()


def init_full_market():
    """全市场数据初始化 - 无人值守稳定运行版本"""
    logger.info("=" * 80)
    logger.info("开始全市场数据初始化（无人值守稳定运行版本）")
    logger.info("=" * 80)
    
    tushare_token = Config.tushare_token
    duckdb_path = Config.duckdb_path
    
    if not tushare_token:
        logger.error("未配置Tushare Token，请在config/settings.yaml中设置tushare_token")
        return
    
    logger.info(f"Tushare Token: {tushare_token[:10]}...")
    logger.info(f"数据库路径: {duckdb_path}")
    
    pro = ts.pro_api(tushare_token)
    
    # 在循环外部建立持久化连接
    conn = duckdb.connect(duckdb_path)
    
    try:
        logger.info("\n" + "=" * 80)
        logger.info("步骤1: 获取全市场股票列表")
        logger.info("=" * 80)
        
        df_stocks = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry,market,list_date')
        
        logger.success(f"共获取到 {len(df_stocks)} 只上市股票（目标：5000+）")
        
        logger.info(f"市场分布:")
        logger.info(df_stocks['market'].value_counts().to_string())
        
        logger.info("\n" + "=" * 80)
        logger.info("步骤2: 检查已存在的数据（Checkpoint）")
        logger.info("=" * 80)
        
        existing_stocks = get_existing_stocks(conn)
        logger.info(f"数据库中已存在 {len(existing_stocks)} 只股票的数据")
        
        # 过滤出需要处理的股票
        df_stocks_to_process = df_stocks[~df_stocks['ts_code'].isin(existing_stocks)]
        
        if len(df_stocks_to_process) == 0:
            logger.success("所有股票数据已存在，无需处理")
            return
        
        logger.success(f"需要处理 {len(df_stocks_to_process)} 只新股票")
        
        logger.info("\n" + "=" * 80)
        logger.info("步骤3: 分批次拉取数据（优化参数）")
        logger.info("=" * 80)
        
        start_date = '20240101'
        end_date = datetime.now().strftime('%Y%m%d')
        
        logger.info(f"时间范围: {start_date} ~ {end_date}")
        logger.info(f"批次大小: 30只股票/批次（优化）")
        logger.info(f"请求间隔: 1.2秒/批次（严格流控）")
        logger.info(f"超时设置: 5秒/请求（防止卡死）")
        
        batch_size = 30
        total_batches = (len(df_stocks_to_process) + batch_size - 1) // batch_size
        
        success_count = 0
        fail_count = 0
        total_records = 0
        commit_interval = 10
        
        for batch_idx in tqdm(range(total_batches), desc="批量拉取数据"):
            start_idx = batch_idx * batch_size
            end_idx = min((batch_idx + 1) * batch_size, len(df_stocks_to_process))
            
            batch_codes = df_stocks_to_process['ts_code'].iloc[start_idx:end_idx].tolist()
            
            try:
                # 使用重试机制获取日线数据（强制超时5秒）
                df_daily = fetch_with_retry(
                    pro, 
                    'daily',
                    max_retries=3,
                    timeout=5,
                    ts_code=','.join(batch_codes), 
                    start_date=start_date, 
                    end_date=end_date
                )
                
                if df_daily is None:
                    fail_count += len(batch_codes)
                    logger.warning(f"批次 {batch_idx + 1}/{total_batches}: 获取日线数据失败，跳过")
                    continue
                
                logger.info(f"批次 {batch_idx + 1}/{total_batches}: daily={len(df_daily)}条")
                
                if not df_daily.empty:
                    # 先保存日线数据，基本面数据后续补充
                    df_merged = df_daily.copy()
                    df_merged['pe_ttm'] = None
                    df_merged['pb'] = None
                    df_merged['total_mv'] = None
                    
                    # Convert trade_date from YYYYMMDD to YYYY-MM-DD format
                    df_merged['trade_date'] = pd.to_datetime(df_merged['trade_date'], format='%Y%m%d').dt.strftime('%Y-%m-%d')
                    
                    # 使用 DuckDB Native Dataframe Insert（零拷贝，极快）
                    try:
                        conn.register('temp_daily_df', df_merged)
                        
                        # 使用 INSERT OR IGNORE（如果主键冲突则跳过，提高速度）
                        conn.execute('''
                            INSERT OR IGNORE INTO stock_daily 
                            SELECT 
                                trade_date,
                                ts_code,
                                open,
                                high,
                                low,
                                close,
                                pre_close,
                                change,
                                pct_chg,
                                vol,
                                amount,
                                pe_ttm,
                                total_mv
                            FROM temp_daily_df
                        ''')
                        
                        logger.info(f"✅ 成功入库: {len(df_merged)} 条")
                        
                    except Exception as e:
                        logger.warning(f"⚠️ 入库失败: {e}")
                    finally:
                        # 清理临时视图
                        try:
                            conn.unregister('temp_daily_df')
                        except:
                            pass
                    
                    success_count += len(batch_codes)
                    total_records += len(df_merged)
                    
                    # 定期提交和报告进度
                    if (batch_idx + 1) % commit_interval == 0:
                        logger.info(f"✅ 已处理 {success_count}/{len(df_stocks_to_process)} 只股票，共 {total_records} 条记录")
                
                # 严格流控：每批次成功后等待1.2秒
                time.sleep(1.2)
                
            except Exception as e:
                fail_count += len(batch_codes)
                logger.warning(f"批次 {batch_idx + 1}/{total_batches} 失败: {e}")
                # 失败后也等待，避免雪崩
                time.sleep(1.2)
        
        logger.info("\n" + "=" * 80)
        logger.info("步骤4: 更新股票信息表")
        logger.info("=" * 80)
        
        df_latest_basic = pro.daily_basic(trade_date=end_date)
        
        if not df_latest_basic.empty:
            df_stock_info = df_stocks.merge(df_latest_basic[['ts_code', 'pe_ttm', 'pb', 'total_mv']], on='ts_code', how='left')
            
            # 使用 DuckDB Native Dataframe Insert（零拷贝，极快）
            try:
                conn.register('temp_stock_info_df', df_stock_info)
                
                # 使用 INSERT OR REPLACE（如果主键冲突则替换）
                conn.execute('''
                    INSERT OR REPLACE INTO stock_info 
                    SELECT ts_code, name, market, pe_ttm, pb, total_mv
                    FROM temp_stock_info_df
                ''')
                
                logger.success(f"✅ 成功更新: {len(df_stock_info)} 条股票信息")
                
            except Exception as e:
                logger.warning(f"⚠️ 更新股票信息失败: {e}")
            finally:
                # 清理临时视图
                try:
                    conn.unregister('temp_stock_info_df')
                except:
                    pass
        
        logger.info("\n" + "=" * 80)
        logger.info("步骤5: 自动运行因子计算")
        logger.info("=" * 80)
        
        try:
            from src.factors.alpha_engine import AlphaEngine
            
            engine = AlphaEngine()
            engine.update_factors()
            engine.close()
            
            logger.success("因子计算完成")
        except Exception as e:
            logger.warning(f"因子计算失败: {e}")
        
        logger.info("\n" + "=" * 80)
        logger.info("步骤6: 结果验证")
        logger.info("=" * 80)
        
        result = conn.execute('SELECT COUNT(DISTINCT ts_code) as count FROM stock_daily').fetchone()
        stock_count = result[0]
        
        result = conn.execute('SELECT COUNT(*) as count FROM stock_daily').fetchone()
        record_count = result[0]
        
        result = conn.execute('SELECT COUNT(*) as count FROM stock_daily WHERE pe_ttm > 0').fetchone()
        pe_count = result[0]
        
        result = conn.execute('SELECT COUNT(*) as count FROM stock_daily WHERE total_mv > 0').fetchone()
        mv_count = result[0]
        
        pe_coverage = (pe_count / record_count * 100) if record_count > 0 else 0
        mv_coverage = (mv_count / record_count * 100) if record_count > 0 else 0
        
        logger.success(f"✅ 成功入库股票数量: {stock_count}")
        logger.success(f"📊 日线数据总量: {record_count}")
        logger.success(f"📊 有效 PE 数据覆盖率: {pe_coverage:.2f}%")
        logger.success(f"📊 有效 市值 数据覆盖率: {mv_coverage:.2f}%")
        
        result = conn.execute('''
        SELECT ts_code, COUNT(*) as count 
        FROM stock_daily 
        GROUP BY ts_code 
        ORDER BY count DESC 
        LIMIT 10
        ''').fetchdf()
        
        logger.info(f"\n数据最多的10只股票:")
        logger.info(result.to_string())
        
        logger.success("\n" + "=" * 80)
        logger.success("全市场数据初始化完成！")
        logger.success("=" * 80)
        
    except Exception as e:
        logger.error(f"初始化过程中发生错误: {e}")
        import traceback
        logger.error(traceback.format_exc())
    
    finally:
        # 循环结束后才关闭连接
        conn.close()
        logger.info("数据库连接已关闭")


if __name__ == "__main__":
    init_full_market()
