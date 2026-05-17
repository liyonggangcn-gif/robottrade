import os
import pandas as pd
import numpy as np
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils

# 由于Qlib安装复杂，我们使用自己的实现
# 移除对Qlib的依赖

class TopKStrategy:
    """Top-K选股策略"""
    
    def __init__(self, weights=None, read_only: bool = False):
        """初始化TopK策略
        
        Args:
            weights: 因子权重字典，默认值为 {'mom_20': 0.3, 'vol_20': -0.4, 'rsi_14': 0.3, 'atr_14': -0.2}
                     注意：vol_20 (波动率) 和 atr_14 (ATR) 权重为负，意味着我们喜欢低波动的股票
            read_only: 是否以只读模式连接数据库，默认False
        """
        # 初始化因子权重（优化：降低动量权重，增加波动率稳定性权重）
        if weights is None:
            self.weights = {'mom_20': 0.3, 'vol_20': -0.4, 'rsi_14': 0.3, 'atr_14': -0.2}
        else:
            self.weights = weights
        print(f"Initialized with weights: {self.weights}")
    
    def get_factor_data(self, date=None, start_date=None, end_date=None):
        """获取因子数据（包含财务指标）
        
        Args:
            date: 单个日期，如果为None则获取指定时间段的数据
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            pandas DataFrame
        """
        # 全部用 Python merge 替代 SQL JOIN，彻底规避跨表 collation 不一致（MySQL 1267）
        sf_query = """
        SELECT
            trade_date, ts_code,
            mom_20, vol_20, rsi_14, atr_14,
            macd_hist, bb_width, vol_ratio, pvt_ma,
            log_mv, pe_inv, pb_inv,
            roe_factor, growth_score, quality_score
        FROM stock_factors
        """
        if date:
            sf_query += f" WHERE trade_date = '{date}'"
        elif start_date and end_date:
            sf_query += f" WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'"
        sf_query += " ORDER BY trade_date, ts_code"

        print(f"Loading factor data...")
        df = DBUtils.query_df(sf_query)

        # Python merge: stock_daily（roe/gpr/netprofit_yoy）
        if not df.empty:
            try:
                sd_cols = "ts_code, trade_date, roe, gpr, netprofit_yoy"
                if date:
                    sd_df = DBUtils.query_df(
                        f"SELECT {sd_cols} FROM stock_daily WHERE trade_date = '{date}'"
                    )
                elif start_date and end_date:
                    sd_df = DBUtils.query_df(
                        f"SELECT {sd_cols} FROM stock_daily WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'"
                    )
                else:
                    sd_df = pd.DataFrame()
                if not sd_df.empty:
                    df = df.merge(sd_df, on=['ts_code', 'trade_date'], how='left')
            except Exception as e:
                print(f"Warning: stock_daily merge failed: {e}")

        # Python merge with stock_info（避免 MySQL collation 冲突）
        if not df.empty:
            try:
                si_df = DBUtils.query_df(
                    "SELECT ts_code, name, pe_ttm, pb, total_mv FROM stock_info"
                )
                if not si_df.empty:
                    df = df.merge(si_df, on='ts_code', how='left')
            except Exception as e:
                print(f"Warning: stock_info merge failed: {e}")

        print(f"Loaded {len(df)} records")
        return df
    
    def get_latest_trade_date(self):
        """获取最新交易日"""
        # 优先从stock_factors表获取，如果为空则从stock_daily表获取
        result = DBUtils.query_df('''
        SELECT MAX(trade_date) as max_date FROM stock_factors
        ''')
        
        if not result.empty and pd.notna(result.iloc[0]['max_date']):
            return result.iloc[0]['max_date']
        
        # 如果stock_factors表为空，从stock_daily表获取
        result = DBUtils.query_df('''
        SELECT MAX(trade_date) as max_date FROM stock_daily
        ''')
        
        if not result.empty and pd.notna(result.iloc[0]['max_date']):
            return result.iloc[0]['max_date']
        else:
            return None
    
    def add_future_performance(self, df, current_date, hold_days=5):
        """计算未来表现
        
        Args:
            df: 包含股票的DataFrame
            current_date: 当前日期
            hold_days: 持仓天数
            
        Returns:
            添加了未来表现列的DataFrame
        """
        if df is None or df.empty:
            print("No data to calculate future performance")
            return df
        
        print(f"Calculating future performance for {hold_days} days from {current_date}")
        
        # 获取最新交易日
        latest_date_df = DBUtils.query_df('''
        SELECT MAX(trade_date) FROM stock_daily
        ''')
        latest_date = latest_date_df.iloc[0, 0]
        
        # 计算目标日期
        target_date = (pd.to_datetime(current_date) + pd.Timedelta(days=hold_days * 2)).strftime('%Y-%m-%d')
        
        # 查询每只股票在未来N天的收盘价
        future_returns = []
        
        for _, row in df.iterrows():
            ts_code = row['ts_code']
            
            # 查询当前日期的收盘价
            current_price_result = DBUtils.query_df('''
            SELECT close FROM stock_daily 
            WHERE ts_code = ? AND trade_date = ?
            ''', [ts_code, current_date])
            
            if current_price_result.empty or pd.isna(current_price_result.iloc[0]['close']):
                future_returns.append(None)
                continue
            current_price = current_price_result.iloc[0]['close']
            
            # 查询未来N天的收盘价
            future_price_query = f'''
            SELECT close FROM stock_daily 
            WHERE ts_code = '{ts_code}' 
            AND trade_date > '{current_date}'
            AND trade_date <= '{target_date}'
            ORDER BY trade_date ASC
            LIMIT 1
            '''
            
            future_price_result = DBUtils.query_df(future_price_query)
            
            if future_price_result.empty or pd.isna(future_price_result.iloc[0]['close']):
                # 没有未来数据，可能是持仓中
                future_returns.append(None)
                continue
            
            future_price = future_price_result.iloc[0]['close']
            
            # 计算收益率
            return_rate = (future_price - current_price) / current_price * 100
            # 保留2位小数
            return_rate = round(return_rate, 2)
            future_returns.append(return_rate)
        
        # 添加未来表现列
        df['future_return'] = future_returns
        
        # 标记状态
        df['status'] = df['future_return'].apply(
            lambda x: '持仓中' if pd.isna(x) else ('盈利' if x > 0 else '亏损')
        )
        
        print(f"Calculated future performance for {len(df)} stocks")
        return df
    
    def _calculate_market_sentiment(self, target_date, window=5):
        """计算市场情绪指标
        
        Args:
            target_date: 目标日期
            window: 计算窗口（天数）
            
        Returns:
            市场情绪指标（正数表示上涨情绪，负数表示下跌情绪）
        """
        try:
            # 获取目标日期前window天的市场数据
            query = f'''
            SELECT 
                trade_date,
                AVG(pct_chg) as avg_return
            FROM stock_daily
            WHERE trade_date <= '{target_date}'
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT {window}
            '''
            
            df = DBUtils.query_df(query)
            
            if len(df) == 0:
                return 0
            
            # 计算市场情绪：最近N天的平均涨跌幅
            sentiment = df['avg_return'].mean()
            
            return sentiment
        except Exception as e:
            print(f"Error calculating market sentiment: {e}")
            return 0
    
    def winsorize(self, series, lower=0.01, upper=0.99):
        """去极值
        
        Args:
            series: 因子序列
            lower: 下界分位数
            upper: 上界分位数
            
        Returns:
            去极值后的序列
        """
        q_low = series.quantile(lower)
        q_high = series.quantile(upper)
        return series.clip(lower=q_low, upper=q_high)
    
    def standardize(self, df, factor_cols):
        """标准化因子
        
        Args:
            df: 因子数据
            factor_cols: 因子列名列表
            
        Returns:
            标准化后的因子数据
        """
        # 按交易日分组标准化
        for col in factor_cols:
            df[col] = df.groupby('trade_date')[col].transform(
                lambda x: (x - x.mean()) / x.std()
            )
        return df
    
    def process_data(self, df):
        """处理因子数据（四因子模型）
        
        Args:
            df: 因子数据
            
        Returns:
            处理后的因子数据
        """
        print("Processing data...")
        
        # 因子列（四因子模型）
        # 分为技术因子和财务因子
        tech_factor_cols = ['mom_20', 'rsi_14']
        value_factor_cols = ['pe_inv']
        financial_factor_cols = ['growth_score', 'quality_score']
        
        all_factor_cols = tech_factor_cols + value_factor_cols + financial_factor_cols
        print(f"Processing factors: {all_factor_cols}")
        
        # 先处理技术因子和价值因子（必须有值）
        for col in tech_factor_cols + value_factor_cols:
            if col in df.columns:
                # 技术因子和价值因子用0填充
                df[col] = df[col].fillna(0)
        
        # 财务因子保持NaN（后续会根据是否有数据决定使用哪种模型）
        # 对于有财务数据的股票，保留其值；对于没有的，保持NaN
        
        # 1. 去极值 (Winsorize)
        for col in all_factor_cols:
            if col in df.columns:
                # 按日期分组去极值
                df[col] = df.groupby('trade_date')[col].transform(
                    lambda x: self.winsorize(x)
                )
        print("Winsorized factors")
        
        # 2. 标准化 (Z-Score) - 按日期分组
        for col in all_factor_cols:
            if col in df.columns:
                # 按日期分组标准化
                df[col] = df.groupby('trade_date')[col].transform(
                    lambda x: (x - x.mean()) / x.std() if x.std() > 0 else 0
                )
        print("Standardized factors")
        
        # 3. 缺失值填充
        # 技术因子和价值因子用当天均值填充
        for col in tech_factor_cols + value_factor_cols:
            if col in df.columns:
                df[col] = df.groupby('trade_date')[col].transform(
                    lambda x: x.fillna(x.mean())
                )
        
        # 财务因子保持NaN（不填充，后续会根据是否有数据决定模型）
        print("Handled missing values")
        
        return df
    
    def calculate_score(self, df):
        """计算综合得分（四因子模型：趋势+价值+成长+质量）
        
        Args:
            df: 处理后的因子数据
            
        Returns:
            带得分的因子数据
        """
        print("Calculating scores...")
        
        # 计算技术面得分（动量+RSI）
        df['technical_score'] = (
            0.5 * df['mom_20'].fillna(0) + 
            0.5 * df['rsi_14'].fillna(0)
        )
        
        # 计算价值面得分（PE倒数）
        df['value_score'] = df['pe_inv'].fillna(0)
        
        # 保存原始的财务因子数据（不填充）
        # 检查是否有财务指标数据
        has_growth = df['growth_score'].notna().any()
        has_quality = df['quality_score'].notna().any()
        
        if has_growth and has_quality:
            # 使用完整的四因子模型
            # 对于有财务数据的股票，使用财务因子
            # 对于没有财务数据的股票，使用0代替
            growth_score = df['growth_score'].fillna(0)
            quality_score = df['quality_score'].fillna(0)
            
            df['score'] = (
                0.3 * df['technical_score'] +  # 技术面 (趋势+RSI)
                0.2 * df['value_score'] +      # 价值面 (PE倒数)
                0.3 * growth_score +           # 成长面 (净利增长)
                0.2 * quality_score            # 质量面 (ROE)
            )
            print("Using 4-factor model: Technical + Value + Growth + Quality")
        else:
            # 使用简化版模型（技术面+价值面）
            df['score'] = (
                0.6 * df['technical_score'] +  # 技术面 (趋势+RSI)
                0.4 * df['value_score']        # 价值面 (PE倒数)
            )
            print("Using 2-factor model: Technical + Value (Growth/Quality data not available)")
        
        print("Calculated scores")
        return df
    
    def run(self, trade_date=None, top_k=20):
        """统一入口，供 StrategyCenter 调用"""
        if trade_date is None:
            trade_date = self.get_latest_trade_date()
        return self.get_top_stocks(trade_date, top_k=top_k)

    def get_top_stocks(self, target_date, top_k=10):
        """获取指定日期的Top K股票
        
        Args:
            target_date: 目标日期
            top_k: 选取数量
            
        Returns:
            Top K股票数据
        """
        print(f"Getting top {top_k} stocks for {target_date}")
        
        # 转换日期格式为 YYYY-MM-DD
        formatted_date = pd.Timestamp(target_date).strftime('%Y-%m-%d')
        
        # 1. 读取指定日期的全市场因子数据（不 JOIN stock_info，避免 MySQL collation 冲突）
        query = f'''
        WITH factor_data AS (
            SELECT
                trade_date,
                ts_code,
                mom_20,
                vol_20,
                rsi_14,
                atr_14,
                pe_inv,
                growth_score,
                quality_score,
                CASE
                    WHEN INSTR(ts_code, '.') > 0 THEN
                        SUBSTR(ts_code, 1, INSTR(ts_code, '.') - 1)
                    ELSE
                        ts_code
                END as code_only
            FROM stock_factors
            WHERE trade_date = '{formatted_date}'
        ),
        best_factor_data AS (
            SELECT
                *,
                ROW_NUMBER() OVER (PARTITION BY code_only ORDER BY
                    CASE
                        WHEN growth_score IS NOT NULL AND quality_score IS NOT NULL THEN 1
                        ELSE 2
                    END,
                    ts_code
                ) as rn
            FROM factor_data
        )
        SELECT
            trade_date,
            ts_code,
            mom_20,
            vol_20,
            rsi_14,
            atr_14,
            pe_inv,
            growth_score,
            quality_score
        FROM best_factor_data
        WHERE rn = 1
        ORDER BY trade_date, ts_code
        '''

        print(f"Loading factor data...")
        df = DBUtils.query_df(query)
        print(f"Loaded {len(df)} records")

        if df.empty:
            print(f"No factor data for {target_date}")
            return None

        # Python merge with stock_info（避免 MySQL collation 冲突）
        try:
            si_df = DBUtils.query_df(
                "SELECT ts_code, name, pe_ttm, total_mv FROM stock_info"
            )
            if not si_df.empty:
                df = df.merge(si_df, on='ts_code', how='left')
                # 过滤掉无名股票（相当于原来的 WHERE si.name IS NOT NULL）
                df = df[df['name'].notna()].copy()
        except Exception as e:
            print(f"Warning: stock_info merge failed ({e})，跳过名称过滤")
        
        if len(df) == 0:
            print(f"No factor data for {target_date}")
            return None
        
        # 2. 负面清单剔除 (Blacklist)
        print("Applying blacklist filters...")
        initial_count = len(df)

        # 剔除ST和退市股（name 列不存在时跳过）
        if 'name' in df.columns:
            df = df[~df['name'].fillna('').str.contains('ST|退')]
        
        # 剔除亏损股 (PE < 0)，保留PE为0或NaN的股票
        df = df[(df['pe_ttm'] >= 0) | df['pe_ttm'].isna()]
        
        # 剔除小市值股 (总市值 < 10亿)，但保留总市值为0或NaN的股票
        # 同时保留有财务数据的股票（即使市值较小）
        df = df[(df['total_mv'] >= 1000000000) | (df['total_mv'].isna()) | (df['total_mv'] == 0) | 
                ((df['growth_score'].notna()) & (df['quality_score'].notna()))]
        
        filtered_count = len(df)
        print(f"Blacklist filter applied: {initial_count} → {filtered_count} stocks (removed {initial_count - filtered_count} stocks)")
        
        if len(df) == 0:
            print(f"No stocks passed blacklist filters for {target_date}")
            return None
        
        # 3. 市场情绪过滤
        print("Applying market sentiment filter...")
        market_sentiment = self._calculate_market_sentiment(target_date)
        print(f"Market sentiment: {market_sentiment:.4f}")
        
        # 如果市场情绪过差（低于-0.05），减少选股数量或降低仓位
        if market_sentiment < -0.05:
            print(f"Market sentiment is poor ({market_sentiment:.4f}), reducing top_k by 50%")
            top_k = max(1, int(top_k * 0.5))
        
        # 4. 计算MA60并进行趋势过滤
        print("Calculating MA60 (Batch Processing)...")
        
        # 1. 获取所有候选股票代码
        candidate_codes = df['ts_code'].unique().tolist()
        if not candidate_codes:
            return None
            
        # 格式化代码列表用于 SQL IN 查询
        codes_str = "', '".join(candidate_codes)
        
        # 2. 计算起始日期 (为了算MA60，需要往前推至少90天)
        start_date_buffer = (pd.to_datetime(target_date) - pd.Timedelta(days=100)).strftime('%Y-%m-%d')
        
        # 3. 使用 DuckDB 窗口函数批量计算
        # 只查询候选股票，大幅减少数据量
        query_ma60 = f"""
        WITH ma_calc AS (
            SELECT 
                ts_code,
                trade_date,
                close,
                AVG(close) OVER (
                    PARTITION BY ts_code 
                    ORDER BY trade_date 
                    ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
                ) as ma60,
                ROW_NUMBER() OVER (
                    PARTITION BY ts_code 
                    ORDER BY trade_date DESC
                ) as rn
            FROM stock_daily
            WHERE trade_date <= '{target_date}' 
              AND trade_date >= '{start_date_buffer}'
              AND ts_code IN ('{codes_str}')
        )
        SELECT ts_code, close, ma60
        FROM ma_calc
        WHERE rn = 1  -- 只取最新的一天
        """
        
        ma60_df = DBUtils.query_df(query_ma60)
        
        # 4. 合并数据 (注意：df 中可能已经有 close 列，合并前先处理)
        if 'close' in df.columns:
            del df['close'] # 移除旧的，使用从 stock_daily 最新查出来的，更准
            
        df = df.merge(ma60_df, on='ts_code', how='left')
        
        # 趋势过滤：保留多头排列（close>=MA60）或MA60数据不足（新股）或动量为正的股票
        # 注意：有无财务数据不能作为绕过趋势过滤的理由
        initial_trend_count = len(df)
        df = df[(df['close'] >= df['ma60']) | (df['ma60'].isna()) |
                (df['mom_20'] > 0)]  # close<MA60但动量仍为正→可能在反弹中
        trend_filtered_count = len(df)
        
        print(f"Trend filter applied: {initial_trend_count} → {trend_filtered_count} stocks (removed {initial_trend_count - trend_filtered_count} weak stocks)")
        
        if len(df) == 0:
            print(f"No stocks passed trend filter for {target_date}")
            return None
        
        # 4. 处理数据
        df = self.process_data(df)
        
        # 5. 计算得分
        df = self.calculate_score(df)
        
        # 6. 按得分降序排列
        df_sorted = df.sort_values('score', ascending=False)
        
        # 7. 返回Top K
        top_stocks = df_sorted.head(top_k).copy()
        
        # 8. 计算止损位 (Risk Control)
        print("Calculating stop loss prices...")
        
        # 从数据库中获取原始的ATR值（未标准化的）
        ts_codes = top_stocks['ts_code'].tolist()
        code_only_list = [code.split('.')[0] if '.' in code else code for code in ts_codes]
        code_only_str = "', '".join(code_only_list)
        query_atr = f'''
        SELECT 
            sf.ts_code,
            sf.atr_14 as original_atr
        FROM stock_factors sf
        WHERE sf.trade_date = '{target_date}' 
        AND (sf.ts_code IN ('{"', '".join(ts_codes)}') OR SUBSTR(sf.ts_code, 1, 6) IN ('{code_only_str}'))
        '''
        
        atr_df = DBUtils.query_df(query_atr)
        
        # 合并原始ATR值
        top_stocks = top_stocks.merge(atr_df, on='ts_code', how='left')
        
        # 使用原始ATR值计算止损价
        top_stocks['stop_loss_price'] = top_stocks.apply(
            lambda row: max(0, row['close'] - 2.0 * row.get('original_atr', 0)) if not pd.isna(row['close']) else 0,
            axis=1
        )
        
        # 删除临时列
        top_stocks = top_stocks.drop(columns=['original_atr'], errors='ignore')
        
        print(f"Selected {len(top_stocks)} top stocks")
        return top_stocks
    
    def select_topk(self, df, top_k=10):
        """选取Top K股票
        
        Args:
            df: 带得分的因子数据
            top_k: 选取数量
            
        Returns:
            Top K股票
        """
        print(f"Selecting Top {top_k} stocks...")
        
        # 按交易日分组，选取得分最高的top_k只股票
        topk_df = df.groupby('trade_date').apply(
            lambda x: x.nlargest(top_k, 'score')
        ).reset_index(drop=True)
        
        # 计算排名
        topk_df['rank'] = topk_df.groupby('trade_date')['score'].rank(ascending=False, method='first')
        
        print(f"Selected {len(topk_df)} stocks")
        return topk_df
    
    def get_latest_signals(self, top_k=10):
        """获取最新交易日的选股信号
        
        Args:
            top_k: 选取数量
            
        Returns:
            选股信号
        """
        # 获取最新交易日
        latest_date = self.get_latest_trade_date()
        
        if not latest_date:
            print("No factor data available")
            return None
        
        # 使用新的get_top_stocks方法
        top_stocks = self.get_top_stocks(latest_date, top_k)
        
        if top_stocks is not None:
            # 输出结果，按照要求的格式
            print(f"\nTop {top_k} stocks for {latest_date}:")
            for i, (_, row) in enumerate(top_stocks.iterrows(), 1):
                # 获取各因子值
                factors_info = []
                for factor in self.weights.keys():
                    factors_info.append(f"{factor.upper()}: {row[factor]:.2f}")
                factors_str = " | ".join(factors_info)
                
                # 输出止损价
                stop_loss = row.get('stop_loss_price', 0)
                print(f"Rank {i}: {row['ts_code']} | Score: {row['score']:.4f} | Stop Loss: {stop_loss:.2f} | {factors_str}")
        
        return top_stocks
    
    def run_backtest(self, start_date, end_date, top_k=10):
        """回测策略
        
        Args:
            start_date: 回测开始日期
            end_date: 回测结束日期
            top_k: 选取数量
            
        Returns:
            回测结果
        """
        print(f"Running backtest from {start_date} to {end_date}")
        
        # 获取指定时间段的因子数据
        df = self.get_factor_data(start_date=start_date, end_date=end_date)
        
        if len(df) == 0:
            print("No factor data available for backtest")
            return None
        
        # 处理因子
        df = self.process_data(df)
        
        # 计算得分
        df = self.calculate_score(df)
        
        # 选取Top K
        topk_df = self.select_topk(df, top_k)
        
        print(f"Backtest completed. Selected {len(topk_df)} stocks in total")
        
        return topk_df
    
    def get_stock_detail(self, ts_code, date):
        """获取股票详细数据
        
        Args:
            ts_code: 股票代码
            date: 日期
            
        Returns:
            股票详细数据字典
        """
        try:
            # 获取股票基本信息
            stock_info = DBUtils.query_df(f'''
            SELECT * FROM stock_info WHERE ts_code = '{ts_code}'
            ''')
            
            if stock_info.empty:
                return None
            
            # 获取股票日线数据
            stock_daily = DBUtils.query_df(f'''
            SELECT * FROM stock_daily 
            WHERE ts_code = '{ts_code}' AND trade_date = '{date}'
            ''')
            
            if stock_daily.empty:
                return None
            
            # 获取股票因子数据
            stock_factors = DBUtils.query_df(f'''
            SELECT * FROM stock_factors 
            WHERE ts_code = '{ts_code}' AND trade_date = '{date}'
            ''')
            
            if stock_factors.empty:
                return None
            
            # 合并数据
            stock_data = {}
            stock_data.update(stock_info.iloc[0].to_dict())
            stock_data.update(stock_daily.iloc[0].to_dict())
            stock_data.update(stock_factors.iloc[0].to_dict())
            
            # 计算止损价
            atr_14 = stock_data.get('atr_14', 0)
            close = stock_data.get('close', 0)
            stock_data['stop_loss_price'] = close - 2.0 * atr_14
            
            return stock_data
            
        except Exception as e:
            print(f"Error getting stock detail: {e}")
            return None
    
    def close(self):
        """关闭数据库连接"""
        # 使用短连接模式，无需维护长连接
        print("SQLite connection already managed by short-lived mode")
    
    def __del__(self):
        """析构函数，确保连接被关闭"""
        try:
            self.close()
        except:
            pass

if __name__ == '__main__':
    # 测试代码
    # 初始化策略，使用默认权重
    strategy = TopKStrategy()
    
    # 获取最新交易日
    latest_date = strategy.get_latest_trade_date()
    
    if latest_date:
        # 示例1：使用get_top_stocks方法
        print("=== Example 1: Using get_top_stocks ===")
        top_stocks = strategy.get_top_stocks(latest_date, top_k=10)
        
        if top_stocks is not None:
            # 按照要求的格式输出
            print(f"\nTop 10 stocks for {latest_date}:")
            for _, row in top_stocks.iterrows():
                # 提取因子值
                mom_value = row.get('mom_20', 0)
                vol_value = row.get('vol_20', 0)
                rsi_value = row.get('rsi_14', 0)
                
                print(f"Date: {latest_date} | Code: {row['ts_code']} | Score: {row['score']:.2f} | Mom: {mom_value:.2f} | Vol: {vol_value:.2f} | RSI: {rsi_value:.2f}")
        
        # 示例2：使用get_latest_signals方法
        print("\n=== Example 2: Using get_latest_signals ===")
        signals = strategy.get_latest_signals(top_k=10)
    else:
        print("No data available")
    
    # 回测
    # backtest_result = strategy.run_backtest('2023-01-01', '2023-12-31', top_k=10)
    
    strategy.close()
