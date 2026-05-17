import os
import pandas as pd
import numpy as np
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils

class AlphaEngine:
    """因子计算引擎"""
    
    def __init__(self):
        """初始化因子计算引擎"""
        # 初始化因子表（使用短连接）
        self._init_factor_table()
    
    def _init_factor_table(self):
        """初始化因子表"""
        db_type = Config.get('db_type', 'sqlite')
        num_type = 'DOUBLE' if db_type == 'mysql' else 'REAL'
        DBUtils.execute(f'''
        CREATE TABLE IF NOT EXISTS stock_factors (
            trade_date VARCHAR(20),
            ts_code VARCHAR(20),
            mom_20 {num_type},
            vol_20 {num_type},
            rsi_14 {num_type},
            atr_14 {num_type},
            macd_hist {num_type},
            bb_width {num_type},
            vol_ratio {num_type},
            pvt_ma {num_type},
            log_mv {num_type},
            pe_inv {num_type},
            pb_inv {num_type},
            roe_factor {num_type},
            growth_score {num_type},
            quality_score {num_type},
            turnover_approx {num_type},
            turnover_ratio {num_type},
            turnover_ma5 {num_type},
            drawdown_20 {num_type},
            gain_10d {num_type},
            price_pos_52w {num_type},
            PRIMARY KEY (trade_date, ts_code)
        )
        ''')
        
        # 检查并添加缺失的列（兼容 SQLite 和 MySQL）
        try:
            db_type = Config.get('db_type', 'sqlite')
            if db_type == 'mysql':
                result = DBUtils.query_df(
                    "SELECT COLUMN_NAME as name FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='stock_factors'"
                )
            else:
                result = DBUtils.query_df('PRAGMA table_info(stock_factors)')
            columns = result['name'].tolist() if not result.empty else []

            columns_to_add = {
                'atr_14': 'ATR',
                'macd_hist': 'MACD Histogram',
                'bb_width': 'Bollinger Band Width',
                'vol_ratio': 'Volume Ratio',
                'pvt_ma': 'Price Volume Trend',
                'log_mv': 'Log Market Cap',
                'pe_inv': 'PE Inverse',
                'pb_inv': 'PB Inverse',
                'roe_factor': 'ROE Factor',
                'growth_score': 'Growth Score',
                'quality_score': 'Quality Score',
                'turnover_approx': 'Turnover Approx',
                'turnover_ratio': 'Turnover Ratio',
                'turnover_ma5': 'Turnover MA5',
                'drawdown_20': 'Drawdown 20',
                'gain_10d': 'Gain 10D',
                'price_pos_52w': 'Price Position 52W',
                # Tier-1 新因子（2026-03）
                'rev_1m': '1-Month Return Reversal',           # 1月反转（负向因子）
                'turnover_vol_20': 'Turnover Volatility 20D',  # 换手率波动率（负向因子）
                # Alpha101 因子（聚宽WorldQuant Alpha因子库，基于OHLCV）
                'alpha_001': 'Alpha001 Rank SignedPower TsArgMax',
                'alpha_003': 'Alpha003 Corr RankOpen RankVol',
                'alpha_004': 'Alpha004 Neg TsRank Low',
                'alpha_005': 'Alpha005 Open VWAP Deviation',
                'alpha_006': 'Alpha006 Corr Open Vol',
                'alpha_007': 'Alpha007 Volume Confirmed Trend',
                'alpha_008': 'Alpha008 Open Return Trend Change',
                'alpha_012': 'Alpha012 Volume Price Reversal',
                'alpha_014': 'Alpha014 Short Reversal',
                'alpha_016': 'Alpha016 Price Vol Covariance',
                'alpha_018': 'Alpha018 Intraday Vol Trend',
                'alpha_020': 'Alpha020 Position In Range',
                'alpha_026': 'Alpha026 Vol Confirmed Mom',
                'alpha_035': 'Alpha035 Open Vol Imbalance',
                'alpha_041': 'Alpha041 Hilo Geo VWAP',
            }

            for column, description in columns_to_add.items():
                if column not in columns:
                    print(f"Adding {column} column to stock_factors table...")
                    if db_type == 'mysql':
                        DBUtils.execute(f'ALTER TABLE stock_factors ADD COLUMN {column} DOUBLE')
                    else:
                        DBUtils.execute(f'ALTER TABLE stock_factors ADD COLUMN IF NOT EXISTS {column} REAL')
                    print(f"Successfully added {column} column")
        except Exception as e:
            print(f"Error checking/adding columns: {e}")
        
        print("Successfully initialized stock_factors table")
    
    def get_latest_factor_date(self):
        """获取因子表中最新的日期"""
        result = DBUtils.query_df('''
        SELECT MAX(trade_date) as max_date FROM stock_factors
        ''')
        
        if not result.empty and pd.notna(result.iloc[0]['max_date']):
            return result.iloc[0]['max_date']
        else:
            return None
    
    def get_stock_daily_data(self, start_date=None):
        """获取股票日线数据（包含财务指标）
        
        Args:
            start_date: 开始日期，如果为None则获取全部数据
            
        Returns:
            pandas DataFrame
        """
        # pb列在MySQL中可能不存在，用NULL占位
        try:
            test = DBUtils.query_df('SELECT pb FROM stock_daily LIMIT 1')
            has_pb = True
        except Exception:
            has_pb = False
        pb_col = 'pb' if has_pb else 'NULL as pb'
        query = f"SELECT trade_date, ts_code, open, high, low, close, vol, amount, pe_ttm, {pb_col}, total_mv, roe, gpr, netprofit_yoy FROM stock_daily"
        
        if start_date:
            query += f" WHERE trade_date >= '{start_date}'"
        
        query += " ORDER BY ts_code, trade_date"
        
        print(f"Loading stock_daily data...")
        df = DBUtils.query_df(query)
        print(f"Loaded {len(df)} records")
        
        return df
    
    def calculate_momentum(self, df, window=20):
        """计算动量因子
        
        Args:
            df: 股票日线数据
            window: 计算窗口
            
        Returns:
            动量因子值
        """
        # 计算动量因子：close / close_shift(window) - 1
        momentum = df.groupby('ts_code')['close'].transform(
            lambda x: x / x.shift(window) - 1
        )
        return momentum
    
    def calculate_volatility(self, df, window=20):
        """计算波动率因子
        
        Args:
            df: 股票日线数据
            window: 计算窗口
            
        Returns:
            波动率因子值
        """
        # 计算日收益率
        df['return'] = df.groupby('ts_code')['close'].transform(lambda x: x.pct_change())
        
        # 计算波动率：过去window个交易日的日收益率标准差
        volatility = df.groupby('ts_code')['return'].transform(lambda x: x.rolling(window=window).std())
        
        # 清理临时列
        df.drop('return', axis=1, inplace=True)
        
        return volatility
    
    def calculate_rsi(self, df, window=14):
        """计算RSI因子
        
        Args:
            df: 股票日线数据
            window: 计算窗口
            
        Returns:
            RSI因子值
        """
        # 计算日收益率
        df['return'] = df.groupby('ts_code')['close'].transform(lambda x: x.pct_change())
        
        # 计算上涨和下跌
        df['gain'] = df['return'].transform(lambda x: x if x > 0 else 0)
        df['loss'] = df['return'].transform(lambda x: abs(x) if x < 0 else 0)
        
        # 计算平均上涨和平均下跌
        df['avg_gain'] = df.groupby('ts_code')['gain'].transform(lambda x: x.rolling(window=window).mean())
        df['avg_loss'] = df.groupby('ts_code')['loss'].transform(lambda x: x.rolling(window=window).mean())
        
        # 计算RSI
        def calculate_rsi_row(row):
            if row['avg_loss'] == 0:
                return 100
            rs = row['avg_gain'] / row['avg_loss']
            return 100 - (100 / (1 + rs))
        
        df['rsi'] = df.apply(calculate_rsi_row, axis=1)
        rsi = df['rsi']
        
        # 清理临时列
        df.drop(['return', 'gain', 'loss', 'avg_gain', 'avg_loss', 'rsi'], axis=1, inplace=True)
        
        return rsi
    
    def calculate_atr(self, df, window=14):
        """计算ATR (Average True Range)因子
        
        Args:
            df: 股票日线数据
            window: 计算窗口
            
        Returns:
            ATR因子值
        """
        # 计算前一天的收盘价
        df['prev_close'] = df.groupby('ts_code')['close'].shift(1)
        
        # 计算TR (True Range)
        df['tr1'] = df['high'] - df['low']
        df['tr2'] = abs(df['high'] - df['prev_close'])
        df['tr3'] = abs(df['low'] - df['prev_close'])
        df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        
        # 计算ATR
        atr = df.groupby('ts_code')['tr'].transform(lambda x: x.rolling(window=window).mean())
        
        # 清理临时列
        df.drop(['prev_close', 'tr1', 'tr2', 'tr3', 'tr'], axis=1, inplace=True)
        
        return atr
    
    def calculate_macd(self, df, fast=12, slow=26, signal=9):
        """计算MACD因子
        
        Args:
            df: 股票日线数据
            fast: 快线周期
            slow: 慢线周期
            signal: 信号线周期
            
        Returns:
            MACD因子值（MACD柱）
        """
        # 计算EMA
        df['ema_fast'] = df.groupby('ts_code')['close'].transform(lambda x: x.ewm(span=fast, adjust=False).mean())
        df['ema_slow'] = df.groupby('ts_code')['close'].transform(lambda x: x.ewm(span=slow, adjust=False).mean())
        
        # 计算MACD线
        df['macd_line'] = df['ema_fast'] - df['ema_slow']
        
        # 计算信号线
        df['signal_line'] = df.groupby('ts_code')['macd_line'].transform(lambda x: x.ewm(span=signal, adjust=False).mean())
        
        # 计算MACD柱
        macd_hist = df['macd_line'] - df['signal_line']
        
        # 清理临时列
        df.drop(['ema_fast', 'ema_slow', 'macd_line', 'signal_line'], axis=1, inplace=True)
        
        return macd_hist
    
    def calculate_bollinger_bands(self, df, window=20, num_std=2):
        """计算布林带因子
        
        Args:
            df: 股票日线数据
            window: 计算窗口
            num_std: 标准差倍数
            
        Returns:
            布林带宽度因子（上轨-下轨）/ 中轨
        """
        # 计算中轨（移动平均）
        df['middle_band'] = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(window=window).mean())
        
        # 计算标准差
        df['std_dev'] = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(window=window).std())
        
        # 计算上轨和下轨
        df['upper_band'] = df['middle_band'] + (df['std_dev'] * num_std)
        df['lower_band'] = df['middle_band'] - (df['std_dev'] * num_std)
        
        # 计算布林带宽度因子（归一化）
        bb_width = (df['upper_band'] - df['lower_band']) / df['middle_band']
        
        # 清理临时列
        df.drop(['middle_band', 'std_dev', 'upper_band', 'lower_band'], axis=1, inplace=True)
        
        return bb_width
    
    def calculate_volume_ratio(self, df, window=5):
        """计算量比因子（当前成交量 / 过去N天平均成交量）
        
        Args:
            df: 股票日线数据
            window: 计算窗口
            
        Returns:
            量比因子值
        """
        # 计算过去N天的平均成交量
        avg_volume = df.groupby('ts_code')['vol'].transform(lambda x: x.rolling(window=window).mean())
        
        # 计算量比
        volume_ratio = df['vol'] / avg_volume
        
        return volume_ratio
    
    def calculate_price_volume_trend(self, df, window=20):
        """计算价量趋势因子（PVT）
        
        Args:
            df: 股票日线数据
            window: 计算窗口
            
        Returns:
            价量趋势因子值
        """
        # 计算日收益率
        df['return'] = df.groupby('ts_code')['close'].transform(lambda x: x.pct_change())
        
        # 计算PVT
        df['pvt'] = df['return'] * df['vol']
        
        # 计算PVT的移动平均
        pvt_ma = df.groupby('ts_code')['pvt'].transform(lambda x: x.rolling(window=window).mean())
        
        # 清理临时列
        df.drop(['return', 'pvt'], axis=1, inplace=True)
        
        return pvt_ma
    
    # ─── Alpha101 因子计算 ───────────────────────────────────────────
    # 实现自聚宽 WorldQuant Alpha101 因子库中基于 OHLCV 的因子子集
    # 专为 A 股适配（T+1, 涨跌停限制）
    # ─────────────────────────────────────────────────────────────────

    def _alpha_rank(self, s: pd.Series) -> pd.Series:
        """截面排名归一化到 [0,1]"""
        return s.rank(pct=True)

    def _alpha_ts_rank(self, s: pd.Series, d: int) -> pd.Series:
        """时间序列排名：过去 d 天内的百分位"""
        return s.rolling(d).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) == d else np.nan)

    def _alpha_ts_argmax(self, s: pd.Series, d: int) -> pd.Series:
        """过去 d 天内最大值所在位置（距当前天数）"""
        return s.rolling(d).apply(lambda x: (d - 1 - x.argmax()) if len(x) == d else np.nan, raw=True)

    def _alpha_signed_power(self, x, e: float) -> float:
        """带符号的幂：sign(x) * |x|^e"""
        return np.sign(x) * (abs(x) ** e)

    def _alpha_correlation(self, a: pd.Series, b: pd.Series, d: int) -> pd.Series:
        """滚动相关系数"""
        return a.rolling(d).corr(b)

    def _alpha_covariance(self, a: pd.Series, b: pd.Series, d: int) -> pd.Series:
        """滚动协方差"""
        return a.rolling(d).cov(b)

    def _alpha_delta(self, s: pd.Series, d: int) -> pd.Series:
        """d 日差分"""
        return s.diff(d)

    def _alpha_scale(self, s: pd.Series, a: float = 1.0) -> pd.Series:
        """缩放因子，使 |sum(s)| = a"""
        total = s.abs().sum()
        return s * (a / total) if total > 0 else s

    def calculate_alpha_001(self, df: pd.DataFrame) -> pd.Series:
        """Alpha001: (rank(Ts_ArgMax(SignedPower(((returns<0)?stddev(returns,20):close), 2.), 5)) - 0.5)
        短期趋势强度因子，捕捉动量惯性
        """
        returns = df.groupby('ts_code')['close'].transform(lambda x: x.pct_change())
        std20 = df.groupby('ts_code')['returns'] if 'returns' in df.columns else \
                df.groupby('ts_code')['close'].transform(lambda x: x.pct_change()).rolling(20).std()
        # 实际计算时使用列操作
        def _calc(group):
            ret = group['close'].pct_change()
            cond = ret < 0
            base = pd.Series(0.0, index=group.index)
            std20_grp = ret.rolling(20).std()
            base[cond] = std20_grp[cond]
            base[~cond] = group['close'][~cond]
            powered = base.apply(lambda x: self._alpha_signed_power(x, 2.0))
            argmax5 = powered.rolling(5).apply(
                lambda x: (4 - x.argmax()) if len(x) == 5 else np.nan, raw=True
            )
            return argmax5.rank(pct=True) - 0.5
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_003(self, df: pd.DataFrame) -> pd.Series:
        """Alpha003: -1 * correlation(rank(open), rank(volume), 10)
        开盘价与成交量的排名相关性（负），量价背离信号
        """
        def _calc(group):
            open_rank = group['open'].rank(pct=True)
            vol_rank = group['vol'].rank(pct=True)
            return -1 * open_rank.rolling(10).corr(vol_rank)
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_004(self, df: pd.DataFrame) -> pd.Series:
        """Alpha004: -1 * Ts_Rank(rank(low), 9)
        最低价排名的时间序列趋势，低位反弹信号
        """
        def _calc(group):
            low_rank = group['low'].rank(pct=True)
            return -1 * low_rank.rolling(9).apply(
                lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) == 9 else np.nan
            )
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_005(self, df: pd.DataFrame) -> pd.Series:
        """Alpha005: (rank((open - (sum(vwap, 10) / 10))) * (-1 * abs(rank((close - vwap)))))
        VWAP 偏离度因子，开盘价高于 VWAP 越多且收盘价偏离 VWAP 越少→高评分
        """
        vwap = (df['high'] + df['low'] + df['close']) / 3 * df['vol']  # 近似VWAP分母
        vol_sum = df.groupby('ts_code')['vol'].transform(lambda x: x.rolling(10).sum())
        vwap_sum = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(10).sum())  # 简化
        # 简化版：用 close 均值近似 VWAP
        vwap_10 = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(10).mean())
        open_dev = df['open'] - vwap_10
        close_dev = df['close'] - vwap_10
        rank_open_dev = open_dev.rank(pct=True)
        rank_close_dev = close_dev.abs().rank(pct=True)
        return rank_open_dev * (-1 * rank_close_dev)

    def calculate_alpha_006(self, df: pd.DataFrame) -> pd.Series:
        """Alpha006: -1 * correlation(open, volume, 10)
        开盘价与成交量的相关系数（负），放量低开 = 恐慌信号
        """
        def _calc(group):
            return -1 * group['open'].rolling(10).corr(group['vol'])
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_007(self, df: pd.DataFrame) -> pd.Series:
        """Alpha007: ((adv20 < volume) ? ((-1 * ts_rank(abs(delta(close, 7)), 60)) * sign(delta(close, 7))) : (-1 * 1))
        成交量确认趋势：放量时趋势可信，缩量时看空
        """
        def _calc(group):
            adv20 = group['vol'].rolling(20).mean()
            delta7 = group['close'].diff(7)
            abs_delta7 = delta7.abs()
            tsrank_60 = abs_delta7.rolling(60).apply(
                lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) == 60 else np.nan
            )
            sign_delta7 = delta7.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
            cond = adv20 < group['vol']
            result = pd.Series(-1.0, index=group.index)
            result[cond] = (-1 * tsrank_60 * sign_delta7)[cond]
            return result
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_008(self, df: pd.DataFrame) -> pd.Series:
        """Alpha008: -1 * rank(((sum(open, 5) * sum(returns, 5)) - delay((sum(open, 5) * sum(returns, 5)), 10)))
        开盘价×收益率的变化趋势，捕捉量价同步改善
        """
        def _calc(group):
            ret = group['close'].pct_change()
            sum_open5 = group['open'].rolling(5).sum()
            sum_ret5 = ret.rolling(5).sum()
            prod = sum_open5 * sum_ret5
            delay10 = prod.shift(10)
            raw = prod - delay10
            return -1 * raw.rank(pct=True)
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_012(self, df: pd.DataFrame) -> pd.Series:
        """Alpha012: (sign(delta(volume, 1)) * (-1 * delta(close, 1)))
        量价反转：放量下跌→买入信号，缩量上涨→卖出信号
        """
        vol_delta = df.groupby('ts_code')['vol'].transform(lambda x: x.diff(1))
        close_delta = df.groupby('ts_code')['close'].transform(lambda x: x.diff(1))
        vol_sign = vol_delta.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        return vol_sign * (-1 * close_delta)

    def calculate_alpha_014(self, df: pd.DataFrame) -> pd.Series:
        """Alpha014: (-1 * rank(delta(returns, 3))) * correlation(open, volume, 10)
        短期反转因子：3日收益率变化为负（变差）→买入信号
        """
        def _calc(group):
            ret = group['close'].pct_change()
            delta_ret3 = ret.diff(3)
            rank_part = -1 * delta_ret3.rank(pct=True)
            corr_part = group['open'].rolling(10).corr(group['vol'])
            return rank_part * corr_part
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_016(self, df: pd.DataFrame) -> pd.Series:
        """Alpha016: -1 * rank(covariance(rank(close), rank(volume), 5))
        收盘价排名与成交量排名的协方差（负），量价稳定性
        """
        def _calc(group):
            close_rank = group['close'].rank(pct=True)
            vol_rank = group['vol'].rank(pct=True)
            cov5 = close_rank.rolling(5).cov(vol_rank)
            return -1 * cov5.rank(pct=True)
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_018(self, df: pd.DataFrame) -> pd.Series:
        """Alpha018: -1 * rank(((stddev(abs((close - open)), 5) + (close - open)) + correlation(close, open, 10)))
        日内波动+趋势综合因子：实体小+高波动→不稳定
        """
        def _calc(group):
            body = (group['close'] - group['open']).abs()
            std5 = body.rolling(5).std()
            co = group['close'] - group['open']
            corr10 = group['close'].rolling(10).corr(group['open'])
            raw = (std5 + co) + corr10.fillna(0)
            return -1 * raw.rank(pct=True)
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_020(self, df: pd.DataFrame) -> pd.Series:
        """Alpha020: (((close - low) - (high - close)) / (high - low)).rolling(5).mean()
        价格在日内区间位置（接近上轨=强，接近下轨=弱），5日均值
        """
        def _calc(group):
            hl = group['high'] - group['low']
            pos = ((group['close'] - group['low']) - (group['high'] - group['close'])) / hl.replace(0, np.nan)
            return pos.rolling(5).mean()
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_026(self, df: pd.DataFrame) -> pd.Series:
        """Alpha026: (-1 * correlation(close, volume, 5)) * correlation(close, returns, 5)
        成交量确认动量：量缩上涨→假突破，量增上涨→真趋势
        """
        def _calc(group):
            ret = group['close'].pct_change()
            corr_cv5 = group['close'].rolling(5).corr(group['vol'])
            corr_cr5 = group['close'].rolling(5).corr(ret)
            return (-1 * corr_cv5) * corr_cr5
        return df.groupby('ts_code', group_keys=False).apply(_calc)

    def calculate_alpha_035(self, df: pd.DataFrame) -> pd.Series:
        """Alpha035: (rank(open) * (1 - rank(volume))) / (rank(close) * (1 - rank(close)))
        开盘量价失衡：高开低量→弱势，低开高量→吸筹
        """
        open_r = df.groupby('ts_code')['open'].transform(lambda x: x.rank(pct=True))
        vol_r = df.groupby('ts_code')['vol'].transform(lambda x: x.rank(pct=True))
        close_r = df.groupby('ts_code')['close'].transform(lambda x: x.rank(pct=True))
        numerator = open_r * (1 - vol_r)
        denominator = (close_r * (1 - close_r)).replace(0, np.nan)
        return numerator / denominator

    def calculate_alpha_041(self, df: pd.DataFrame) -> pd.Series:
        """Alpha041: (((high * low)^0.5) - vwap)
        最高最低价几何均值与VWAP的偏离：>0=买方强势
        """
        vwap = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(10).mean())  # VWAP近似
        hl_geo = (df['high'] * df['low']) ** 0.5
        return hl_geo - vwap

    def calculate_alpha_factors(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算所有Alpha101因子"""
        print("Calculating Alpha101 factors...")

        df['alpha_001'] = self.calculate_alpha_001(df)
        print("  Alpha001 done")

        df['alpha_003'] = self.calculate_alpha_003(df)
        print("  Alpha003 done")

        df['alpha_004'] = self.calculate_alpha_004(df)
        print("  Alpha004 done")

        df['alpha_005'] = self.calculate_alpha_005(df)
        print("  Alpha005 done")

        df['alpha_006'] = self.calculate_alpha_006(df)
        print("  Alpha006 done")

        df['alpha_007'] = self.calculate_alpha_007(df)
        print("  Alpha007 done")

        df['alpha_008'] = self.calculate_alpha_008(df)
        print("  Alpha008 done")

        df['alpha_012'] = self.calculate_alpha_012(df)
        print("  Alpha012 done")

        df['alpha_014'] = self.calculate_alpha_014(df)
        print("  Alpha014 done")

        df['alpha_016'] = self.calculate_alpha_016(df)
        print("  Alpha016 done")

        df['alpha_018'] = self.calculate_alpha_018(df)
        print("  Alpha018 done")

        df['alpha_020'] = self.calculate_alpha_020(df)
        print("  Alpha020 done")

        df['alpha_026'] = self.calculate_alpha_026(df)
        print("  Alpha026 done")

        df['alpha_035'] = self.calculate_alpha_035(df)
        print("  Alpha035 done")

        df['alpha_041'] = self.calculate_alpha_041(df)
        print("  Alpha041 done")

        return df

    def add_fundamental_factors(self, df):
        """添加基本面因子（直接从stock_daily表中获取财务指标）
        
        Args:
            df: 因子数据DataFrame
            
        Returns:
            添加了基本面因子的DataFrame
        """
        print("Adding fundamental factors...")
        
        # 计算成长因子（净利润增长率）
        if 'netprofit_yoy' in df.columns:
            # 成长因子：净利润增长率越高越好
            # 处理0值和NaN值
            df['growth_score'] = df['netprofit_yoy'].replace(0, np.nan)
        else:
            df['growth_score'] = np.nan
        
        # 计算质量因子（ROE + 毛利率）
        if 'roe' in df.columns and 'gpr' in df.columns:
            # 质量因子：ROE > 15% 且 毛利率 > 20% 为优质资产
            # 算法：0.6 * rank(roe) + 0.4 * rank(gpr)
            # 处理0值和NaN值
            df['roe'] = pd.to_numeric(df['roe'], errors='coerce')
            df['gpr'] = pd.to_numeric(df['gpr'], errors='coerce')
            df['roe_factor'] = df['roe'].replace(0, np.nan)
            df['quality_score'] = 0.6 * df['roe'].replace(0, np.nan) + 0.4 * df['gpr'].replace(0, np.nan)
        elif 'roe' in df.columns:
            df['roe'] = pd.to_numeric(df['roe'], errors='coerce')
            df['roe_factor'] = df['roe'].replace(0, np.nan)
            df['quality_score'] = df['roe'].replace(0, np.nan)
        else:
            df['roe_factor'] = np.nan
            df['quality_score'] = np.nan
        
        # 计算价值因子（PE倒数）
        if 'pe_ttm' in df.columns:
            # 处理0值和NaN值，PE为0或负数时设为NaN
            df['pe_ttm'] = pd.to_numeric(df['pe_ttm'], errors='coerce')
            df['pe_inv'] = np.where(
                (df['pe_ttm'] > 0) & (df['pe_ttm'].notna()),
                1.0 / df['pe_ttm'],
                np.nan
            )
        else:
            df['pe_inv'] = np.nan
        
        # 计算PB倒数因子（PB越低，因子值越高）
        if 'pb' in df.columns:
            # 处理0值和NaN值，PB为0或负数时设为NaN
            df['pb'] = pd.to_numeric(df['pb'], errors='coerce')
            df['pb_inv'] = np.where(
                (df['pb'] > 0) & (df['pb'].notna()),
                1.0 / df['pb'],
                np.nan
            )
        else:
            df['pb_inv'] = np.nan
        
        # 计算市值对数因子（避免市值过大影响）
        if 'total_mv' in df.columns:
            # 处理0值和NaN值，总市值为0时设为NaN
            df['total_mv'] = pd.to_numeric(df['total_mv'], errors='coerce')
            df['log_mv'] = np.where(
                (df['total_mv'] > 0) & (df['total_mv'].notna()),
                np.log(df['total_mv']),
                np.nan
            )
        else:
            df['log_mv'] = np.nan
        
        print("Added fundamental factors: log_mv, pe_inv, pb_inv, growth_score, quality_score")
        
        return df
    
    def calculate_factors(self, df):
        """计算所有因子

        Args:
            df: 股票日线数据

        Returns:
            包含因子的DataFrame
        """
        print("Calculating factors...")

        # 数据修复
        try:
            from src.utils.data_repair import DataRepair
            df = DataRepair.repair_stock_daily(df)
            print("[DataRepair] 日线数据修复完成")
        except Exception as e:
            print(f"[DataRepair] 数据修复跳过: {e}")

        # 确保数据按股票和日期排序
        df = df.sort_values(['ts_code', 'trade_date'])
        
        # 计算动量因子
        df['mom_20'] = self.calculate_momentum(df, window=20)
        print("Calculated Momentum_20")
        
        # 计算波动率因子
        df['vol_20'] = self.calculate_volatility(df, window=20)
        print("Calculated Volatility_20")
        
        # 计算RSI因子
        df['rsi_14'] = self.calculate_rsi(df, window=14)
        print("Calculated RSI_14")
        
        # 计算ATR因子
        df['atr_14'] = self.calculate_atr(df, window=14)
        print("Calculated ATR_14")
        
        # 计算MACD因子
        df['macd_hist'] = self.calculate_macd(df, fast=12, slow=26, signal=9)
        print("Calculated MACD_Hist")
        
        # 计算布林带因子
        df['bb_width'] = self.calculate_bollinger_bands(df, window=20, num_std=2)
        print("Calculated BB_Width")
        
        # 计算量比因子
        df['vol_ratio'] = self.calculate_volume_ratio(df, window=5)
        print("Calculated Vol_Ratio")
        
        # 计算价量趋势因子
        df['pvt_ma'] = self.calculate_price_volume_trend(df, window=20)
        print("Calculated PVT_MA")

        # 换手率代理及变化
        df['turnover_approx'] = df['amount'] / df['total_mv'].replace(0, np.nan)
        df['turnover_ratio'] = df.groupby('ts_code')['turnover_approx'].transform(
            lambda x: x / x.rolling(5).mean()
        )
        df['turnover_ma5'] = df.groupby('ts_code')['turnover_approx'].transform(
            lambda x: x.rolling(5).mean()
        )

        # 回撤与过热指标
        df['drawdown_20'] = df.groupby('ts_code')['close'].transform(
            lambda x: x / x.rolling(20, min_periods=5).max() - 1
        )
        df['gain_10d'] = df.groupby('ts_code')['close'].transform(
            lambda x: x / x.shift(10) - 1
        )
        df['price_pos_52w'] = df.groupby('ts_code')['close'].transform(
            lambda x: (x - x.rolling(250, min_periods=20).min()) /
                      (x.rolling(250, min_periods=20).max() - x.rolling(250, min_periods=20).min() + 1e-9)
        )
        print("Calculated Turnover/Drawdown/Overbought factors")

        # ── Tier-1 新因子 ──────────────────────────────────────────────────
        # rev_1m：1月收益率反转因子（负向，A股最强Alpha之一）
        # 逻辑：散户过度反应 + T+1制度导致短期反转效应显著
        df['rev_1m'] = df.groupby('ts_code')['close'].transform(
            lambda x: x / x.shift(20) - 1   # 过去20交易日涨跌幅（取负向使用）
        )
        print("Calculated Rev_1M (1-month reversal factor)")

        # turnover_vol_20：换手率波动率（负向，高波动=游资炒作=不稳定）
        # 使用 turnover_approx 的20日滚动标准差
        if 'turnover_approx' in df.columns:
            df['turnover_vol_20'] = df.groupby('ts_code')['turnover_approx'].transform(
                lambda x: x.rolling(20, min_periods=5).std()
            )
        else:
            df['turnover_vol_20'] = np.nan
        print("Calculated TurnoverVol_20 (turnover volatility factor)")

        # 计算Alpha101因子
        df = self.calculate_alpha_factors(df)

        # 添加基本面因子
        df = self.add_fundamental_factors(df)

        # 只保留需要的列
        alpha_cols = ['alpha_001', 'alpha_003', 'alpha_004', 'alpha_005', 'alpha_006',
                      'alpha_007', 'alpha_008', 'alpha_012', 'alpha_014', 'alpha_016',
                      'alpha_018', 'alpha_020', 'alpha_026', 'alpha_035', 'alpha_041']
        factor_cols = ['trade_date', 'ts_code', 'mom_20', 'vol_20', 'rsi_14', 'atr_14',
                       'macd_hist', 'bb_width', 'vol_ratio', 'pvt_ma',
                       'log_mv', 'pe_inv', 'pb_inv', 'roe_factor',
                       'growth_score', 'quality_score',
                       'turnover_approx', 'turnover_ratio', 'turnover_ma5',
                       'drawdown_20', 'gain_10d', 'price_pos_52w',
                       'rev_1m', 'turnover_vol_20'] + alpha_cols
        factor_df = df[[c for c in factor_cols if c in df.columns]]

        # 只去除技术因子为NA的行（计算窗口内的数据），保留基本面因子为NaN的数据
        # 新增的换手率/回撤/过热因子/Alpha101列为可选，不纳入必需dropna集合
        tech_factor_cols = ['mom_20', 'vol_20', 'rsi_14', 'atr_14', 'macd_hist', 'bb_width', 'vol_ratio', 'pvt_ma']
        factor_df = factor_df.dropna(subset=tech_factor_cols)
        print(f"Final factor data: {len(factor_df)} records")
        
        return factor_df
    
    def save_factors(self, factor_df):
        """保存因子到数据库（兼容 SQLite 和 MySQL）

        Args:
            factor_df: 包含因子的DataFrame
        """
        if len(factor_df) == 0:
            print("No factors to save")
            return

        # 转换日期格式为字符串
        if 'trade_date' in factor_df.columns:
            factor_df = factor_df.copy()
            factor_df['trade_date'] = factor_df['trade_date'].apply(
                lambda x: x.strftime('%Y-%m-%d') if pd.notna(x) and isinstance(x, pd.Timestamp) else str(x)
            )

        alpha_cols = ['alpha_001', 'alpha_003', 'alpha_004', 'alpha_005', 'alpha_006',
                       'alpha_007', 'alpha_008', 'alpha_012', 'alpha_014', 'alpha_016',
                       'alpha_018', 'alpha_020', 'alpha_026', 'alpha_035', 'alpha_041']
        factor_cols = ['mom_20', 'vol_20', 'rsi_14', 'atr_14', 'macd_hist', 'bb_width',
                       'vol_ratio', 'pvt_ma', 'log_mv', 'pe_inv', 'pb_inv',
                       'roe_factor', 'growth_score', 'quality_score',
                       'turnover_approx', 'turnover_ratio', 'turnover_ma5',
                       'drawdown_20', 'gain_10d', 'price_pos_52w',
                       'rev_1m', 'turnover_vol_20'] + alpha_cols
        # 只取表中存在的因子列
        avail_cols = [c for c in factor_cols if c in factor_df.columns]
        insert_cols = ['trade_date', 'ts_code'] + avail_cols
        placeholders = ', '.join(['?'] * len(insert_cols))
        col_str = ', '.join(insert_cols)
        sql = f"INSERT INTO stock_factors ({col_str}) VALUES ({placeholders})"

        # 按日期批量删除旧记录（避免主键冲突）
        for d in factor_df['trade_date'].unique():
            try:
                DBUtils.execute('DELETE FROM stock_factors WHERE trade_date = ?', [d])
            except Exception:
                pass

        # 构建记录列表
        records = []
        for _, row in factor_df.iterrows():
            vals = [row.get('trade_date'), row.get('ts_code')]
            for c in avail_cols:
                v = row.get(c)
                vals.append(None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v))
            records.append(tuple(vals))

        # 分批 executemany 写入（兼容 MySQL / SQLite）
        BATCH = 500
        saved = 0
        try:
            with DBUtils.get_conn() as conn:
                cursor = conn.cursor()
                for i in range(0, len(records), BATCH):
                    cursor.executemany(sql, records[i:i + BATCH])
                    saved += len(records[i:i + BATCH])
            print(f"Saved {saved} factor records to database")
        except Exception as e:
            print(f"Error saving factors: {e}")
            import traceback
            traceback.print_exc()
    
    def update_factors(self):
        """增量更新因子
        
        只计算stock_daily中有但stock_factors中没有日期的因子
        """
        try:
            # 获取因子表中最新的日期
            latest_factor_date = self.get_latest_factor_date()
            
            if latest_factor_date:
                # 从最新因子日期的下一天开始
                start_date = (pd.to_datetime(latest_factor_date) + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
                # 为了保证计算窗口有足够的数据，查询开始日期往前推60天
                query_start_date = (pd.to_datetime(start_date) - pd.Timedelta(days=60)).strftime('%Y-%m-%d')
            else:
                # 从最早开始
                start_date = None
                query_start_date = None
            
            # 读取数据
            df = self.get_stock_daily_data(query_start_date)
            
            if len(df) == 0:
                print("No data to process")
                return
            
            # 计算因子
            factor_df = self.calculate_factors(df)
            
            # 过滤出需要更新的日期范围
            if start_date:
                factor_df = factor_df[factor_df['trade_date'] >= start_date]
                print(f"Filtered factor data: {len(factor_df)} records from {start_date}")
            
            # 保存因子
            self.save_factors(factor_df)

            # 用 financial_data 补充 roe_factor（stock_daily.roe 在 MySQL 中为空）
            self._fill_roe_from_financial(new_dates=factor_df['trade_date'].unique().tolist())

            print("Factor update completed successfully!")
            
        except Exception as e:
            print(f"Failed to update factors: {e}")
            raise
    
    def _fill_roe_from_financial(self, new_dates=None):
        """从 financial_data 补充 roe_factor（针对 MySQL 中 stock_daily.roe 为空的情况）"""
        try:
            fin = DBUtils.query_df(
                'SELECT ts_code, end_date, roe FROM financial_data WHERE roe IS NOT NULL ORDER BY ts_code, end_date'
            )
            if fin.empty:
                return
            fin['avail_date'] = pd.to_datetime(fin['end_date'], format='%Y%m%d') + pd.DateOffset(months=4)

            from collections import defaultdict
            stock_roe = defaultdict(list)
            for _, row in fin.iterrows():
                stock_roe[row['ts_code']].append((row['avail_date'], float(row['roe'])))

            if new_dates:
                dates = sorted(new_dates)
            else:
                dates = DBUtils.query_df('SELECT DISTINCT trade_date FROM stock_factors ORDER BY trade_date')['trade_date'].tolist()

            update_data = []
            for d_str in dates:
                d = pd.to_datetime(d_str)
                for ts_code, roe_list in stock_roe.items():
                    valid = [(avail, roe) for avail, roe in roe_list if avail <= d]
                    if valid:
                        latest_roe = sorted(valid, key=lambda x: x[0])[-1][1]
                        update_data.append((latest_roe, latest_roe, ts_code, d_str))

            if not update_data:
                return

            sql = 'UPDATE stock_factors SET roe_factor=?, quality_score=? WHERE ts_code=? AND trade_date=?'
            BATCH = 1000
            with DBUtils.get_conn() as conn:
                cursor = conn.cursor()
                for i in range(0, len(update_data), BATCH):
                    cursor.executemany(sql, update_data[i:i + BATCH])
            print(f"[AlphaEngine] ROE补充: {len(update_data)} 条 from financial_data")
        except Exception as e:
            print(f"[AlphaEngine] ROE补充失败: {e}")

    def get_factors(self, ts_code=None, start_date=None, end_date=None):
        """获取因子数据
        
        Args:
            ts_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            因子数据
        """
        query = "SELECT * FROM stock_factors"
        
        conditions = []
        params = []
        
        if ts_code:
            conditions.append("ts_code = ?")
            params.append(ts_code)
        
        if start_date:
            conditions.append("trade_date >= ?")
            params.append(start_date)
        
        if end_date:
            conditions.append("trade_date <= ?")
            params.append(end_date)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY ts_code, trade_date"
        
        return DBUtils.query_df(query, params)
    
    def close(self):
        """关闭数据库连接"""
        # 使用短连接模式，无需维护长连接
        print("SQLite connection already managed by short-lived mode")
    
    def __del__(self):
        """析构函数，确保连接被关闭"""
        self.close()

if __name__ == '__main__':
    # 测试代码
    engine = AlphaEngine()
    
    # 更新因子
    engine.update_factors()
    
    # 获取因子数据
    df = engine.get_factors('600519.SH')
    print(f"贵州茅台因子数据条数: {len(df)}")
    print(df.tail())
    
    engine.close()
