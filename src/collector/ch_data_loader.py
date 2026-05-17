#!/usr/bin/env python3
"""
ClickHouse 数据加载器 - 供选股策略使用
替换 MySQL 数据源以提升并发性能
"""
import os
import sys

class ClickHouseLoader:
    """ClickHouse 数据加载器"""
    
    _client = None
    
    def __init__(self, host='192.168.3.51', port=8123, username='default', password='clickhouse123'):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
    
    def _get_client(self):
        if self._client is None:
            import clickhouse_connect
            self._client = clickhouse_connect.get_client(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password
            )
        return self._client
    
    def get_stock_daily_with_industry(self, start_date: str, end_date: str, ts_codes: list = None) -> 'pd.DataFrame':
        """获取股票日线数据（含行业）"""
        import pandas as pd
        
        client = self._get_client()
        
        sql = f"""
        SELECT
            d.trade_date,
            d.ts_code,
            i.name,
            d.close,
            d.open,
            d.high,
            d.low,
            d.vol,
            d.amount,
            d.pe_ttm,
            d.roe,
            d.gpr,
            d.netprofit_yoy,
            d.total_mv,
            i.industry
        FROM stock_daily d
        LEFT JOIN stock_info i ON d.ts_code = i.ts_code
        WHERE d.trade_date >= '{start_date}' AND d.trade_date <= '{end_date}'
          AND d.close IS NOT NULL AND d.close > 0
        """
        
        if ts_codes:
            codes_str = "','".join(ts_codes)
            sql += f" AND d.ts_code IN ('{codes_str}')"
        
        sql += " ORDER BY d.trade_date, d.ts_code"
        
        try:
            result = client.query(sql)
            df = pd.DataFrame(result.result_rows, columns=result.column_names)
            return df
        except Exception as e:
            print(f"[ClickHouseLoader] 查询失败: {e}")
            return pd.DataFrame()
    
    def get_active_stocks(self) -> 'pd.DataFrame':
        """获取活跃股票池"""
        import pandas as pd
        from src.utils.db_utils import DBUtils
        return DBUtils.query_df("SELECT * FROM stock_pool WHERE is_active = 1")
    
    def get_latest_trade_date(self) -> str:
        """获取最新交易日期"""
        client = self._get_client()
        try:
            result = client.query("SELECT MAX(trade_date) as max_date FROM stock_daily")
            row = result.first_row
            return str(row[0]) if row else None
        except Exception as e:
            print(f"[ClickHouseLoader] 获取最新日期失败: {e}")
            return None
    
    def get_stock_info(self) -> 'pd.DataFrame':
        """获取股票基本信息"""
        import pandas as pd
        
        client = self._get_client()
        try:
            result = client.query("SELECT * FROM stock_info ORDER BY ts_code")
            df = pd.DataFrame(result.result_rows, columns=result.column_names)
            return df
        except Exception as e:
            print(f"[ClickHouseLoader] stock_info查询失败: {e}")
            return pd.DataFrame()
    
    def get_ai_scores(self, trade_date: str) -> 'pd.DataFrame':
        """获取AI预测评分"""
        import pandas as pd
        
        # 从MySQL查询AI评分（数据量小）
        from src.utils.db_utils import DBUtils
        return DBUtils.query_df(f"""
            SELECT ts_code, ai_score, trade_date 
            FROM ai_predictions 
            WHERE trade_date = '{trade_date}'
        """)


# 全局实例
_ch_loader = None

def get_ch_loader() -> ClickHouseLoader:
    global _ch_loader
    if _ch_loader is None:
        _ch_loader = ClickHouseLoader()
    return _ch_loader


if __name__ == '__main__':
    # 测试
    loader = get_ch_loader()
    print(f"最新日期: {loader.get_latest_date()}")
    
    df = loader.get_stock_daily('2026-04-01', '2026-04-03')
    print(f"日线数据: {len(df)} 行")
