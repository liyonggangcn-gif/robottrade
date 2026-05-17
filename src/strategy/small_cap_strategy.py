"""
SmallCapStrategy: 质量小市值策略 v3（重构版）

核心依据：
  - A股小市值效应是实证最强因子，CSI 2000宇宙 RankIC > 11%
  - 全面注册制后壳价值消失，纯市值排序危险，必须叠加质量过滤
  - 学术来源：Fama-French CH-3（中国版：E/P代替B/P，更可靠）

v3 改进：
  - 市值权重从 65% 降至 45%，避免过度依赖单一因子
  - 新增反转因子（近1月跌幅，小盘股短期反转效应强）
  - 新增财务排雷：应收/营收比、经营现金流/净利润比
  - 动态空仓：4月（年报+一季报）+ 1月（年报预告）自动空仓
  - 评分 EMA 平滑，防止每日排名剧变

评分公式（v3 六维加权）：
  score = 0.45 × rank(log_mv, 越小越高)       # 市值因子（↓ 从65%降至45%）
        + 0.10 × rank(roe, 越大越好)            # 质量
        + 0.15 × rank(mom_20, 越大越好)         # 20日动量
        + 0.10 × rank(reversal_5, 越大越好)     # 5日反转（新）
        + 0.07 × rank(macd_hist, 越大越好)      # 趋势强度
        + 0.03 × rank(price_pos_52w, 越大越好)  # 52周相对强度
        + 0.10 × rank(quality_trap, 越大越好)   # 财务排雷（新）

流动性惩罚：
  市值<30亿 且 日均成交<5000万 → score × 0.8

财务排雷（硬过滤）：
  - 应收/营收 > 80% → 剔除（收入确认激进）
  - 经营现金流/净利润 < 0.3 → 剔除（利润纸面化）
"""

import numpy as np
import pandas as pd
from loguru import logger

from src.strategy.base import BaseStrategy
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class SmallCapStrategy(BaseStrategy):
    """质量小市值策略：市值因子 × 质量过滤 × 动量确认 × 财务排雷"""

    name = 'small_cap'
    version = '3.0'
    display_name = '质量小市值策略'

    # 搅屎棍行业（存量市场下排除，来自 JQ JSG_HY_LIST）
    JSG_INDUSTRIES = {'银行', '煤炭', '钢铁'}

    def __init__(self):
        cfg = Config.get('small_cap') or {}
        self.MIN_MV_YI        = cfg.get('min_mv_亿', 15.0)
        self.MAX_MV_YI        = cfg.get('max_mv_亿', 200.0)
        self.MIN_ROE          = cfg.get('min_roe_pct', 0.0)  # 取消ROE限制
        self.MAX_DEBT_RATIO   = cfg.get('max_debt_ratio', 0.70)
        self.MAX_PE           = cfg.get('max_pe', 150.0)
        self.MOMENTUM_FLOOR   = cfg.get('momentum_floor', -0.05)
        self.MIN_DAYS_LISTED  = cfg.get('min_days_listed', 375)

        # v3 趋势确认阈值
        self.RSI_FLOOR        = cfg.get('rsi_floor', 35.0)
        self.DRAWDOWN_MAX     = cfg.get('drawdown_max', -0.12)
        self.PRICE_POS_MIN    = cfg.get('price_pos_min', 0.10)

        # 财务排雷阈值
        self.MAX_AR_REVENUE_RATIO = cfg.get('max_ar_revenue_ratio', 0.80)  # 应收/营收
        self.MIN_CFO_NI_RATIO     = cfg.get('min_cfo_ni_ratio', 0.30)      # 经营现金流/净利润

        # 风控开关
        self.ENABLE_KCBJ_FILTER  = cfg.get('enable_kcbj_filter', True)
        self.ENABLE_NEW_STOCK    = cfg.get('enable_new_stock_filter', True)
        self.ENABLE_MARKET_ENV   = cfg.get('enable_market_env', True)
        self.ENABLE_FINANCIAL_TRAP = cfg.get('enable_financial_trap', True)

        # 动态空仓
        self.EMPTY_MONTHS       = cfg.get('empty_months', [1])  # 1月财报季（临时关闭4月）
        self.EMPTY_DOWN_THRESHOLD = cfg.get('empty_down_threshold', 0.65)  # 65%+股票下跌

        # 流动性惩罚阈值
        self.ILLIQ_MV_YI      = 30.0
        self.ILLIQ_AMOUNT_W   = 5000.0

        # EMA 平滑
        self.EMA_ALPHA = cfg.get('ema_alpha', 0.40)

        # 行业中性过滤（聚宽模式：每行业市值最小1只）
        self.ENABLE_INDUSTRY_NEUTRAL = cfg.get('enable_industry_neutral', True)

        logger.info(f"[SmallCapStrategy v3] 初始化 "
                    f"市值范围=[{self.MIN_MV_YI},{self.MAX_MV_YI}]亿 "
                    f"ROE>={self.MIN_ROE}% PE<={self.MAX_PE} "
                    f"财务排雷={'开' if self.ENABLE_FINANCIAL_TRAP else '关'} "
                    f"动态空仓月={self.EMPTY_MONTHS}")

    # ──────────────────────────────────────────
    # 主接口
    # ──────────────────────────────────────────

    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        """执行质量小市值策略选股"""
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"\n[SmallCapStrategy v3] ===== 选股 {trade_date} =====")

        # Step 0: 动态空仓检查
        if self._should_empty_position(
            trade_date=trade_date,
            calendar_months=self.EMPTY_MONTHS,
            market_threshold=self.EMPTY_DOWN_THRESHOLD
        ):
            logger.warning("[SmallCapStrategy] 触发空仓条件，返回空结果")
            return self._empty_result()

        # Step 1: 加载数据
        df = self._load_universe(trade_date)
        if df.empty:
            logger.error("[SmallCapStrategy] 数据为空")
            return self._empty_result()
        logger.info(f"  [Step 1] 原始宇宙: {len(df)} 只")

        # Step 2: 基础过滤（ST/退市）
        if 'name' in df.columns:
            before = len(df)
            df = df[~df['name'].str.contains(r'ST|\*ST|退', na=False, regex=True)]
            logger.info(f"  [Step 2] ST过滤: {before}→{len(df)}")

        # Step 2b: 科创板 + 北交所过滤
        if self.ENABLE_KCBJ_FILTER:
            df = self._filter_kcbj(df)
            logger.info(f"  [Step 2b] 科创/北交过滤后: {len(df)} 只")

        # Step 2c: 次新股过滤
        if self.ENABLE_NEW_STOCK:
            df = self._filter_new_stock(df, trade_date)
            logger.info(f"  [Step 2c] 次新股过滤后: {len(df)} 只")

        # Step 3: 市值范围过滤
        df = self._filter_mv_range(df)
        logger.info(f"  [Step 3] 市值过滤后: {len(df)} 只")

        # Step 4: 流动性门槛
        df = self._filter_liquidity(df)
        logger.info(f"  [Step 4] 流动性过滤后: {len(df)} 只")

        # Step 5: 质量门槛（ROE、PE）
        df = self._filter_quality(df)
        logger.info(f"  [Step 5] 质量过滤后: {len(df)} 只")

        # Step 5b: 财务排雷（v3 新增）
        if self.ENABLE_FINANCIAL_TRAP:
            df = self._filter_financial_trap(df)
            logger.info(f"  [Step 5b] 财务排雷后: {len(df)} 只")

        # Step 6: 趋势确认
        df = self._filter_trend(df)
        logger.info(f"  [Step 6] 趋势过滤后: {len(df)} 只")

        # Step 6b: 市场环境 + 搅屎棍行业过滤
        if self.ENABLE_MARKET_ENV:
            market_env = self._calc_market_env(trade_date)
            df = self._filter_jsg_industry(df, market_env)
            logger.info(f"  [Step 6b] 市场环境={market_env} 搅屎棍过滤后: {len(df)} 只")
        else:
            market_env = 'N/A'

        if df.empty:
            logger.warning("[SmallCapStrategy] 过滤后为空")
            return self._empty_result()

        # Step 7: 评分合成（v3 六维）
        df = self._calc_score(df)

        # Step 8: 流动性惩罚
        df = self._apply_liquidity_penalty(df)

        # Step 9: EMA 平滑（v3 新增）
        df['score'] = self._apply_score_ema(df, score_col='score', alpha=self.EMA_ALPHA)

        # Step 9b: 行业中性过滤（聚宽模式）
        if self.ENABLE_INDUSTRY_NEUTRAL and 'industry' in df.columns:
            before = len(df)
            df = self._filter_industry_neutral(df, top_k)
            logger.info(f"  [Step 9b] 行业中性过滤: {before}→{len(df)} 只")
            if df.empty:
                logger.warning("[SmallCapStrategy] 行业中性过滤后为空")
                return self._empty_result()

        # Step 10: 排序输出
        result = (df.sort_values('score', ascending=False)
                    .head(top_k)
                    .reset_index(drop=True))
        result['rank'] = range(1, len(result) + 1)
        result['strategy'] = self.name
        result['trade_date'] = trade_date
        result['signal_reason'] = result.apply(self._format_reason, axis=1)
        result['market_env'] = market_env
        result['sub_scores'] = result.apply(lambda r: {
            'total_mv_亿': round(float(r.get('total_mv', 0) or 0) / 10000, 1),
            'roe':        round(float(r.get('roe', 0) or 0), 2),
            'mom_20':     round(float(r.get('mom_20', 0) or 0), 4),
            'reversal_5': round(float(r.get('reversal_5', 0) or 0), 4),
            'rsi_14':     round(float(r.get('rsi_14', 0) or 0), 1),
            'macd_hist':  round(float(r.get('macd_hist', 0) or 0), 3),
            'price_pos_52w': round(float(r.get('price_pos_52w', 0) or 0), 3),
            'drawdown_20':   round(float(r.get('drawdown_20', 0) or 0), 3),
            'quality_trap':  round(float(r.get('quality_trap', 0) or 0), 3),
        }, axis=1)

        self._print_result(result)

        # 保存评分历史（供明日EMA使用）
        self._save_scores_to_history(result, trade_date, score_col='score')

        out_cols = ['ts_code', 'name', 'score', 'rank', 'strategy',
                    'signal_reason', 'sub_scores', 'trade_date',
                    'total_mv', 'roe', 'industry']
        out_cols = [c for c in out_cols if c in result.columns]
        return result[out_cols]

    # ──────────────────────────────────────────
    # 数据加载
    # ──────────────────────────────────────────

    def _load_universe(self, trade_date: str) -> pd.DataFrame:
        """加载股票基础数据 + 因子 + 财务排雷指标"""
        sql_max_date = "SELECT MAX(trade_date) AS dt FROM stock_daily WHERE trade_date <= ?"
        sql_max_factor_date = "SELECT MAX(trade_date) AS dt FROM stock_factors WHERE trade_date <= ?"

        sql_daily = """
        SELECT sd.ts_code, sd.total_mv, sd.pe_ttm, sd.roe, sd.amount
        FROM stock_daily sd WHERE sd.trade_date = ?
        """
        sql_factor = """
        SELECT sf.ts_code, sf.log_mv, sf.mom_20, sf.vol_20, sf.pe_inv,
               sf.rsi_14, sf.macd_hist, sf.price_pos_52w, sf.drawdown_20
        FROM stock_factors sf WHERE sf.trade_date = ?
        """
        try:
            DBUtils.query_df("SELECT list_date FROM stock_info LIMIT 1")
            sql_info = "SELECT ts_code, name, industry, list_date, total_mv AS info_total_mv FROM stock_info"
        except Exception:
            sql_info = "SELECT ts_code, name, industry, total_mv AS info_total_mv FROM stock_info"

        sql_amount = """
        SELECT ts_code, AVG(amount) AS avg_amount_20d
        FROM stock_daily
        WHERE trade_date > ? AND trade_date <= ?
        GROUP BY ts_code
        """
        # 财务排雷数据（应收、现金流）
        sql_finance = """
        SELECT fd.ts_code,
               fd.accounts_receivable, fd.total_revenue,
               fd.cashflow_from_operations, fd.net_profit
        FROM financial_data fd
        INNER JOIN (
            SELECT ts_code, MAX(end_date) AS max_end
            FROM financial_data
            GROUP BY ts_code
        ) latest ON fd.ts_code = latest.ts_code AND fd.end_date = latest.max_end
        """

        try:
            max_daily  = DBUtils.query_df(sql_max_date, params=(trade_date,)).iloc[0]['dt']
            max_factor = DBUtils.query_df(sql_max_factor_date, params=(trade_date,)).iloc[0]['dt']
            cutoff_20d = (pd.Timestamp(max_daily) - pd.Timedelta(days=30)).strftime('%Y-%m-%d')

            df       = DBUtils.query_df(sql_daily, params=(max_daily,))
            df_fac   = DBUtils.query_df(sql_factor, params=(max_factor,))
            df_info  = DBUtils.query_df(sql_info)
            df_amount = DBUtils.query_df(sql_amount, params=(cutoff_20d, max_daily))
            if self.ENABLE_FINANCIAL_TRAP:
                df_fin   = DBUtils.query_df(sql_finance)
            else:
                df_fin = pd.DataFrame()

            df = df.merge(df_fac, on='ts_code', how='left')
            df = df.merge(df_info, on='ts_code', how='left')
            if not df_amount.empty:
                df = df.merge(df_amount, on='ts_code', how='left')
            else:
                df['avg_amount_20d'] = np.nan
            if not df_fin.empty:
                df = df.merge(df_fin, on='ts_code', how='left')

            df['total_mv'] = df['total_mv'].where(df['total_mv'].notna(), df.get('info_total_mv'))
            df['name'] = df['name'].where(df['name'].notna(), df['ts_code'])
            df['industry']  = df.get('industry', pd.Series('', index=df.index)).fillna('')
            df['list_date'] = df.get('list_date', pd.Series('', index=df.index)).fillna('')

            for col in ['total_mv', 'pe_ttm', 'roe', 'amount',
                        'log_mv', 'mom_20', 'vol_20', 'avg_amount_20d',
                        'rsi_14', 'macd_hist', 'price_pos_52w', 'drawdown_20',
                        'accounts_receivable', 'total_revenue',
                        'cashflow_from_operations', 'net_profit']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            # ROE 回退
            if 'roe' in df.columns and df['roe'].isna().all():
                sql_max_roe = "SELECT MAX(trade_date) AS dt FROM stock_daily WHERE roe IS NOT NULL AND trade_date <= ?"
                max_roe_date = DBUtils.query_df(sql_max_roe, params=(max_daily,)).iloc[0]['dt']
                if max_roe_date:
                    df_roe = DBUtils.query_df(
                        "SELECT ts_code, roe FROM stock_daily WHERE trade_date = ?",
                        params=(max_roe_date,)
                    )
                    df = df.drop(columns=['roe']).merge(df_roe[['ts_code', 'roe']], on='ts_code', how='left')
                    df['roe'] = pd.to_numeric(df['roe'], errors='coerce')

            return df
        except Exception as e:
            logger.error(f"[SmallCapStrategy] 数据加载失败: {e}")
            return pd.DataFrame()

    # ──────────────────────────────────────────
    # 过滤层
    # ──────────────────────────────────────────

    def _filter_kcbj(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df[~df['ts_code'].str.startswith('688')]
        df = df[~df['ts_code'].str.endswith('.BJ')]
        return df.reset_index(drop=True)

    def _filter_new_stock(self, df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        if 'list_date' not in df.columns:
            return df
        trade_dt = pd.to_datetime(trade_date, errors='coerce')
        if pd.isna(trade_dt):
            return df
        list_dt = pd.to_datetime(df['list_date'], errors='coerce')
        listed_days = (trade_dt - list_dt).dt.days
        mask = list_dt.isna() | (listed_days >= self.MIN_DAYS_LISTED)
        return df[mask].reset_index(drop=True)

    def _calc_market_env(self, trade_date: str,
                         ma_window: int = 20, slope_window: int = 5) -> str:
        """判断市场环境：增量 or 存量"""
        try:
            cutoff = (pd.Timestamp(trade_date) - pd.Timedelta(days=(ma_window + slope_window) * 2)
                      ).strftime('%Y-%m-%d')
            sql = """
            SELECT trade_date, SUM(amount) AS total_amount
            FROM stock_daily
            WHERE trade_date >= ? AND trade_date <= ?
            GROUP BY trade_date ORDER BY trade_date
            """
            agg = DBUtils.query_df(sql, params=(cutoff, trade_date))
            if agg.empty or len(agg) < ma_window + 2:
                return '存量'
            agg['total_amount'] = pd.to_numeric(agg['total_amount'], errors='coerce')
            ma = agg['total_amount'].rolling(ma_window, min_periods=ma_window).mean().dropna()
            if len(ma) < slope_window + 1:
                return '存量'
            change = (ma.iloc[-1] - ma.iloc[-slope_window - 1]) / (ma.iloc[-slope_window - 1] + 1e-9)
            return '增量' if change > 0.10 else '存量'
        except Exception as e:
            logger.warning(f"  [MarketEnv] 计算失败，默认存量: {e}")
            return '存量'

    def _filter_jsg_industry(self, df: pd.DataFrame, market_env: str) -> pd.DataFrame:
        if market_env != '存量' or 'industry' not in df.columns:
            return df
        before = len(df)
        df = df[~df['industry'].isin(self.JSG_INDUSTRIES)]
        removed = before - len(df)
        if removed:
            logger.info(f"  [JSG] 搅屎棍过滤 {removed} 只（银行/煤炭/钢铁）")
        return df.reset_index(drop=True)

    def _filter_mv_range(self, df: pd.DataFrame) -> pd.DataFrame:
        min_mv_w = self.MIN_MV_YI * 10000
        max_mv_w = self.MAX_MV_YI * 10000
        return df[
            df['total_mv'].notna() &
            (df['total_mv'] >= min_mv_w) &
            (df['total_mv'] <= max_mv_w)
        ].reset_index(drop=True)

    def _filter_liquidity(self, df: pd.DataFrame) -> pd.DataFrame:
        if 'avg_amount_20d' in df.columns:
            mask = df['avg_amount_20d'].isna() | (df['avg_amount_20d'] >= 2000.0)
            df = df[mask]
        return df.reset_index(drop=True)

    def _filter_quality(self, df: pd.DataFrame) -> pd.DataFrame:
        before = len(df)
        if 'roe' in df.columns:
            mask = df['roe'].notna() & (df['roe'] >= self.MIN_ROE)
            df = df[mask]
        if 'pe_ttm' in df.columns:
            mask = df['pe_ttm'].notna() & (df['pe_ttm'] > 0) & (df['pe_ttm'] <= self.MAX_PE)
            df = df[mask]
        logger.info(f"  [Quality] {before}→{len(df)}")
        return df.reset_index(drop=True)

    def _filter_financial_trap(self, df: pd.DataFrame) -> pd.DataFrame:
        """财务排雷（v3 新增）：剔除收入质量差、利润纸面化的股票"""
        before = len(df)

        # 应收/营收比 > 阈值 → 剔除（收入确认激进，可能虚增）
        if 'accounts_receivable' in df.columns and 'total_revenue' in df.columns:
            mask_ar = df['total_revenue'].isna() | (df['total_revenue'] <= 0) | \
                      (df['accounts_receivable'] / df['total_revenue'].replace(0, np.nan) <= self.MAX_AR_REVENUE_RATIO)
            df = df[mask_ar]

        # 经营现金流/净利润 < 阈值 → 剔除（利润没有现金支撑）
        if 'cashflow_from_operations' in df.columns and 'net_profit' in df.columns:
            mask_cfo = df['net_profit'].isna() | (df['net_profit'] <= 0) | \
                       (df['cashflow_from_operations'] / df['net_profit'].replace(0, np.nan) >= self.MIN_CFO_NI_RATIO)
            df = df[mask_cfo]

        removed = before - len(df)
        if removed:
            logger.info(f"  [FinTrap] 财务排雷剔除 {removed} 只 "
                        f"(应收/营收>{self.MAX_AR_REVENUE_RATIO:.0%} 或 CFO/NI<{self.MIN_CFO_NI_RATIO:.0%})")
        return df.reset_index(drop=True)

    def _filter_trend(self, df: pd.DataFrame) -> pd.DataFrame:
        """趋势确认过滤 v3"""
        before = len(df)
        if 'mom_20' in df.columns:
            df = df[df['mom_20'].isna() | (df['mom_20'] >= self.MOMENTUM_FLOOR)]
        if 'rsi_14' in df.columns:
            df = df[df['rsi_14'].isna() | (df['rsi_14'] >= self.RSI_FLOOR)]
        if 'drawdown_20' in df.columns:
            df = df[df['drawdown_20'].isna() | (df['drawdown_20'] >= self.DRAWDOWN_MAX)]
        if 'price_pos_52w' in df.columns:
            df = df[df['price_pos_52w'].isna() | (df['price_pos_52w'] >= self.PRICE_POS_MIN)]
        removed = before - len(df)
        if removed:
            logger.info(f"  [Trend v3] 过滤 {removed} 只")
        return df.reset_index(drop=True)

    # ──────────────────────────────────────────
    # 评分（v3 六维）
    # ──────────────────────────────────────────

    def _calc_score(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        n = len(df)

        # 市值因子（主因子，负向）
        if 'log_mv' in df.columns and df['log_mv'].notna().sum() > n * 0.5:
            mv_factor = df['log_mv'].fillna(df['log_mv'].median())
        else:
            mv_factor = np.log(df['total_mv'].replace(0, np.nan).fillna(1))
        mv_rank = mv_factor.rank(ascending=True, method='average')
        mv_score = self._normalize_score(mv_rank)

        # ROE 质量
        if 'roe' in df.columns and df['roe'].notna().sum() > 5:
            roe_vals = self._winsorize(df['roe'].fillna(0), 0.02, 0.98)
            quality_score = self._normalize_score(roe_vals)
        else:
            quality_score = pd.Series([0.5] * n, index=df.index)

        # 20日动量
        if 'mom_20' in df.columns and df['mom_20'].notna().sum() > 5:
            mom_vals = self._winsorize(df['mom_20'].fillna(0), 0.02, 0.98)
            mom_score = self._normalize_score(mom_vals)
        else:
            mom_score = pd.Series([0.5] * n, index=df.index)

        # 5日反转因子（v3 新增）：小盘股短期反转效应强
        # 近5日跌幅越大，反转得分越高（负向动量 → 正向评分）
        if 'mom_20' in df.columns and 'rsi_14' in df.columns:
            # 用 mom_20 的短期部分近似：如果 RSI < 40 且 mom_20 < 0，反转概率高
            reversal = pd.Series(0.5, index=df.index)
            rsi_ok = df['rsi_14'].notna() & (df['rsi_14'] < 45)
            mom_ok = df['mom_20'].notna() & (df['mom_20'] < 0)
            mask = rsi_ok & mom_ok
            # RSI越低 + 跌幅越大 → 反转得分越高
            if mask.sum() > 0:
                rsi_rev = (45 - df.loc[mask, 'rsi_14']) / 45  # 0~1
                mom_rev = (-df.loc[mask, 'mom_20']).clip(0, 0.3) / 0.3  # 0~1
                reversal[mask] = 0.5 * rsi_rev + 0.5 * mom_rev
            df['reversal_5'] = reversal
            reversal_score = self._normalize_score(reversal)
        else:
            df['reversal_5'] = 0.5
            reversal_score = pd.Series([0.5] * n, index=df.index)

        # MACD 柱（趋势强度）
        if 'macd_hist' in df.columns and df['macd_hist'].notna().sum() > 5:
            macd_vals = self._winsorize(df['macd_hist'].fillna(0), 0.02, 0.98)
            macd_score = self._normalize_score(macd_vals)
        else:
            macd_score = pd.Series([0.5] * n, index=df.index)

        # 52周价格位置
        if 'price_pos_52w' in df.columns and df['price_pos_52w'].notna().sum() > 5:
            pos_vals = self._winsorize(df['price_pos_52w'].fillna(0.5), 0.02, 0.98)
            price_pos_score = self._normalize_score(pos_vals)
        else:
            price_pos_score = pd.Series([0.5] * n, index=df.index)

        # 财务质量排雷评分（v3 新增）
        quality_trap = self._calc_quality_trap_score(df)
        df['quality_trap'] = quality_trap
        quality_trap_score = self._normalize_score(quality_trap)

        # 合成评分（v3 六维权重）
        df['score'] = (
            0.45 * mv_score +           # 市值（从65%降至45%）
            0.10 * quality_score +      # ROE质量
            0.15 * mom_score +          # 20日动量
            0.10 * reversal_score +     # 5日反转（新）
            0.07 * macd_score +         # MACD趋势
            0.03 * price_pos_score +    # 52周位置
            0.10 * quality_trap_score   # 财务排雷（新）
        )
        return df

    def _calc_quality_trap_score(self, df: pd.DataFrame) -> pd.Series:
        """财务质量排雷评分：应收质量 + 现金流质量

        返回 Series [0, 1]，越高越安全
        """
        n = len(df)
        score = pd.Series(0.5, index=df.index)

        # 应收/营收比：越低越好
        if 'accounts_receivable' in df.columns and 'total_revenue' in df.columns:
            ar_ratio = (df['accounts_receivable'] / df['total_revenue'].replace(0, np.nan)).fillna(0.5)
            ar_score = 1.0 - self._normalize_score(ar_ratio.clip(0, 1.5))
            score = score * 0.5 + ar_score * 0.5

        # 经营现金流/净利润：越高越好
        if 'cashflow_from_operations' in df.columns and 'net_profit' in df.columns:
            cfo_ratio = (df['cashflow_from_operations'] / df['net_profit'].replace(0, np.nan)).fillna(0.5)
            cfo_score = self._normalize_score(cfo_ratio.clip(-1, 3))
            score = score * 0.5 + cfo_score * 0.5

        return score

    def _apply_liquidity_penalty(self, df: pd.DataFrame) -> pd.DataFrame:
        if 'avg_amount_20d' not in df.columns:
            return df
        illiq_mv_w = self.ILLIQ_MV_YI * 10000
        illiq_mask = (
            (df['total_mv'] < illiq_mv_w) &
            (df['avg_amount_20d'] < self.ILLIQ_AMOUNT_W) &
            df['avg_amount_20d'].notna()
        )
        count = illiq_mask.sum()
        if count > 0:
            df.loc[illiq_mask, 'score'] *= 0.8
            logger.info(f"  [Penalty] 流动性惩罚 {count} 只")
        return df

    # ──────────────────────────────────────────
    # 辅助
    # ──────────────────────────────────────────

    def _filter_industry_neutral(self, df: pd.DataFrame, top_k: int = 20) -> pd.DataFrame:
        """行业中性过滤：每行业选评分最高的1只，确保行业分散

        新逻辑改进了聚宽原版"每行业市值最小1只"策略：
        先用评分在行业内排序取最优，若同分则取市值最小。
        无行业归属的股票(industry='')统一放入"未知"组竞争。
        """
        n_before = len(df)
        df = df.copy()

        # 对空行业股票统一标记
        df['_ind_group'] = df['industry'].fillna('').apply(
            lambda x: x if x and x.strip() else '未知板块'
        )

        # 行业内排序：先按评分降序，再按市值升序（同分取最小市值）
        df['_ind_rank'] = df.groupby('_ind_group')['score'].rank(ascending=False, method='first')
        df['_mv_rank'] = df.groupby('_ind_group')['total_mv'].rank(ascending=True, method='first')

        # 选出每行业评分最高的股票；若多只同分则取市值最小
        # 按行业分组，先按score降序再按total_mv升序，取每组第一条
        best = df.sort_values(['_ind_group', 'score', 'total_mv'],
                              ascending=[True, False, True])
        best = best.groupby('_ind_group').head(1).reset_index(drop=True)

        # 避免行业太多导致选股激进：限制行业数量 = max(top_k, 30)
        # 按score降序取前N个行业
        max_industries = max(top_k, 30)

        best = best.sort_values('score', ascending=False).head(max_industries)

        n_after = len(best)
        industries_used = best['industry'].nunique()
        logger.info(f"  [IndNeutral] {n_before}→{n_after} 只, 涉及 {industries_used} 个行业")

        best.drop(columns=['_ind_group', '_ind_rank', '_mv_rank'], inplace=True, errors='ignore')
        return best.reset_index(drop=True)

    def _format_reason(self, row) -> str:
        mv_yi = float(row.get('total_mv', 0) or 0) / 10000
        roe   = float(row.get('roe', 0) or 0)
        mom   = float(row.get('mom_20', 0) or 0)
        rev   = float(row.get('reversal_5', 0) or 0)
        parts = [f"市值{mv_yi:.0f}亿"]
        if roe:
            parts.append(f"ROE={roe:.1f}%")
        if mom:
            parts.append(f"20日{mom*100:+.1f}%")
        if rev > 0.6:
            parts.append("反转↑")
        return ' '.join(parts)

    def _print_result(self, result: pd.DataFrame):
        logger.info(f"\n[SmallCapStrategy v3] ===== Top {len(result)} =====")
        for _, row in result.iterrows():
            mv_yi = float(row.get('total_mv', 0) or 0) / 10000
            logger.info(
                f"  #{int(row['rank']):2d} {row['ts_code']} "
                f"{str(row.get('name', ''))[:6]:6s} "
                f"Score={float(row['score']):.3f} "
                f"市值={mv_yi:.0f}亿 "
                f"ROE={float(row.get('roe', 0) or 0):.1f}% "
                f"[{row.get('industry', '')}]"
            )
