import sys
import os
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 初始化日志（输出到 logs/select_stocks_today_YYYYMMDD.log + 控制台）
from src.utils.log_utils import init_logger
_logger = init_logger("select_stocks_today")

from src.strategy.topk_strategy import TopKStrategy
from src.utils.config_loader import Config
from loguru import logger
import pandas as pd

logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")
logger.add(os.path.join(os.path.dirname(__file__), '..', 'logs', f'select_stocks_today_{datetime.now().strftime("%Y%m%d")}.log'),
           format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}", encoding="utf-8")

def select_stocks():
    """选择今天可交易的10只股票"""
    logger.info("=" * 80)
    logger.info("开始选股：今天可交易的10只股票")
    logger.info("=" * 80)
    
    try:
        # 初始化策略
        strategy = TopKStrategy(
            weights={'mom_20': 0.3, 'vol_20': -0.4, 'rsi_14': 0.3, 'atr_14': -0.2},
            read_only=True
        )
        
        # 获取最新交易日期
        result = strategy.conn.execute('SELECT MAX(trade_date) as max_date FROM stock_daily').fetchone()
        latest_date = result[0]
        
        logger.info(f"最新交易日期: {latest_date}")
        
        # 执行选股
        logger.info("\n正在执行选股策略...")
        selected_stocks = strategy.get_top_stocks(latest_date, top_k=10)
        
        if selected_stocks is not None and not selected_stocks.empty:
            logger.success(f"\n✅ 成功选出 {len(selected_stocks)} 只股票")
            
            # 显示选中的股票
            logger.info("\n" + "=" * 80)
            logger.info("选中的股票列表")
            logger.info("=" * 80)
            
            for idx, row in selected_stocks.iterrows():
                logger.info(f"\n股票 {idx + 1}:")
                logger.info(f"  代码: {row['ts_code']}")
                logger.info(f"  名称: {row['name']}")
                logger.info(f"  综合得分: {row['score']:.4f}")
                logger.info(f"  最新价: {row['close']:.2f}")
                logger.info(f"  止损价: {row['stop_loss_price']:.2f}")
                if 'pe_ttm' in row and pd.notna(row['pe_ttm']):
                    logger.info(f"  PE(TTM): {row['pe_ttm']:.2f}")
                if 'total_mv' in row and pd.notna(row['total_mv']):
                    logger.info(f"  总市值: {row['total_mv']:.2f}万元")
                logger.info(f"  动量20日: {row['mom_20']:.4f}")
                logger.info(f"  波动率20日: {row['vol_20']:.4f}")
                logger.info(f"  RSI14: {row['rsi_14']:.4f}")
                logger.info(f"  ATR14: {row['atr_14']:.4f}")
            
            # 保存结果到文件
            output_file = 'selected_stocks_today.csv'
            selected_stocks.to_csv(output_file, index=False, encoding='utf-8-sig')
            logger.info(f"\n✅ 选股结果已保存到: {output_file}")
            
            # 统计信息
            logger.info("\n" + "=" * 80)
            logger.info("选股统计")
            logger.info("=" * 80)
            logger.info(f"选股数量: {len(selected_stocks)} 只")
            logger.info(f"平均得分: {selected_stocks['score'].mean():.4f}")
            logger.info(f"最高得分: {selected_stocks['score'].max():.4f}")
            logger.info(f"最低得分: {selected_stocks['score'].min():.4f}")
            logger.info(f"平均收盘价: {selected_stocks['close'].mean():.2f}")
            logger.info(f"总市值合计: {selected_stocks['total_mv'].sum():.2f}万元")
            
        else:
            logger.warning("未选出任何股票")
        
        # 关闭连接
        strategy.close()
        
        logger.success("\n" + "=" * 80)
        logger.success("选股完成！")
        logger.success("=" * 80)
        
    except Exception as e:
        logger.error(f"选股过程中发生错误: {e}")
        import traceback
        logger.error(traceback.format_exc())

if __name__ == "__main__":
    select_stocks()
