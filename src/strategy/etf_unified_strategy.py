"""
ETF统一策略框架

将原有分散的5个ETF策略（抄底反弹、趋势动量、双重动量、资金流、波动率择时）
融合为统一的 ETFUnifiedStrategy，根据市场环境自动选择策略组合。

策略融合逻辑：
  进攻模式（PMI扩张 + 市场上涨）：
    趋势动量(40%) + 双重动量(30%) + 资金流(30%)
  防御模式（PMI收缩 + 市场下跌）：
    抄底反弹(40%) + 波动率择时(30%) + 资金流(30%)
  均衡模式：
    五策略等权

数据优化：
  ETF数据入库（etf_daily表），避免每次akshare实时拉取
  每日同步一次，策略直接从数据库读取
"""

import numpy as np
import pandas as pd
from loguru import logger
from typing import Optional

from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class ETFUnifiedStrategy:
    """ETF统一策略：环境自适应 × 多策略融合"""

    name = 'etf_unified'
    display_name = 'ETF统一策略'

    # 策略权重配置（按市场环境）
    MODE_WEIGHTS = {
        'offensive': {
            'momentum':      0.40,
            'dual_momentum': 0.30,
            'smart_money':   0.30,
        },
        'defensive': {
            'bottom_fish':   0.40,
            'vol_timing':    0.30,
            'smart_money':   0.30,
        },
        'balanced': {
            'bottom_fish':   0.20,
            'momentum':      0.20,
            'dual_momentum': 0.20,
            'smart_money':   0.20,
            'vol_timing':    0.20,
        },
    }

    def __init__(self):
        cfg = Config.get('etf_unified') or {}
        self.top_n = cfg.get('top_n', 6)
        self.min_amount_wan = cfg.get('min_amount_wan', 500)
        self.min_mv_yi = cfg.get('min_mv_yi', 2.0)

        logger.info(f"[ETFUnifiedStrategy] 初始化 Top{self.top_n} "
                    f"成交额>{self.min_amount_wan}万 市值>{self.min_mv_yi}亿")

    def run(self, trade_date: str = None, top_n: int = None) -> pd.DataFrame:
        """执行ETF统一策略

        Args:
            trade_date: 交易日期
            top_n: 输出数量

        Returns:
            DataFrame with columns: code, name, score, strategy, advice
        """
        if top_n:
            self.top_n = top_n
        if trade_date is None:
            try:
                df = DBUtils.query_df("SELECT MAX(trade_date) AS dt FROM stock_daily")
                trade_date = df.iloc[0]['dt'] if not df.empty else None
            except Exception:
                trade_date = pd.Timestamp.now().strftime('%Y-%m-%d')

        logger.info(f"\n[ETFUnifiedStrategy] ===== {trade_date} =====")

        # Step 1: 判断市场环境
        mode = self._detect_mode(trade_date)
        weights = self.MODE_WEIGHTS.get(mode, self.MODE_WEIGHTS['balanced'])
        logger.info(f"  [Mode] {mode} 权重={weights}")

        # Step 2: 加载ETF数据
        etf_df = self._load_etf_data(trade_date)
        if etf_df is None or etf_df.empty:
            logger.warning("[ETFUnifiedStrategy] ETF数据为空")
            return pd.DataFrame()

        # Step 3: 运行各子策略
        sub_results = {}
        for strategy_name in weights.keys():
            try:
                result = self._run_sub_strategy(strategy_name, etf_df, trade_date)
                if result is not None and not result.empty:
                    sub_results[strategy_name] = result
            except Exception as e:
                logger.warning(f"  [ETF] {strategy_name} 运行失败: {e}")

        if not sub_results:
            logger.warning("[ETFUnifiedStrategy] 所有子策略均无结果")
            return pd.DataFrame()

        # Step 4: 融合评分
        result = self._merge_results(sub_results, weights)

        logger.info(f"\n[ETFUnifiedStrategy] ===== Top {self.top_n} ({mode}) =====")
        for i, row in result.iterrows():
            logger.info(
                f"  #{i+1} {row['code']} {str(row.get('name',''))[:10]:10s} "
                f"Score={float(row['score']):.3f} "
                f"策略={row.get('strategies','')}"
            )

        return result

    def _detect_mode(self, trade_date: str) -> str:
        """检测市场环境：offensive / defensive / balanced"""
        try:
            # 市场宽度：近5日上涨股票占比
            sql = """
            SELECT trade_date,
                   SUM(CASE WHEN close > open THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS up_pct
            FROM stock_daily
            WHERE trade_date <= ?
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT 5
            """
            df = DBUtils.query_df(sql, params=(trade_date,))
            if df.empty:
                return 'balanced'

            avg_up = df['up_pct'].mean()

            if avg_up > 0.55:
                return 'offensive'
            elif avg_up < 0.40:
                return 'defensive'
            else:
                return 'balanced'
        except Exception as e:
            logger.debug(f"[ETFUnifiedStrategy] 模式检测失败: {e}")
            return 'balanced'

    def _load_etf_data(self, trade_date: str) -> Optional[pd.DataFrame]:
        """从数据库加载ETF数据（优先）或从akshare实时获取"""
        try:
            # 尝试从 etf_daily 表读取
            sql = """
            SELECT code, name, price, pct_chg, amount, total_mv,
                   close_5d, close_20d, close_60d, vol_5d, vol_20d
            FROM etf_daily
            WHERE trade_date = (
                SELECT MAX(trade_date) FROM etf_daily WHERE trade_date <= ?
            )
            AND amount >= ?
            """
            df = DBUtils.query_df(sql, params=(trade_date, self.min_amount_wan * 10000))
            if not df.empty:
                logger.info(f"  [Data] 从数据库加载 {len(df)} 只ETF")
                return df
        except Exception:
            pass

        # 降级：从akshare实时获取
        logger.info("  [Data] etf_daily表为空，从akshare实时获取...")
        return self._fetch_etf_live(trade_date)

    def _fetch_etf_live(self, trade_date: str) -> Optional[pd.DataFrame]:
        """从akshare实时获取ETF数据（降级方案）"""
        try:
            import akshare as ak
            df = ak.fund_etf_spot_em()
            if df is None or df.empty:
                return None

            # 标准化列名
            rename_map = {}
            for col in df.columns:
                lc = col.lower()
                if '代码' in col or 'code' in lc: rename_map[col] = 'code'
                elif '名称' in col or 'name' in lc: rename_map[col] = 'name'
                elif '最新价' in col or 'price' in lc: rename_map[col] = 'price'
                elif '涨跌幅' in col or 'pct' in lc: rename_map[col] = 'pct_chg'
                elif '成交额' in col or 'amount' in lc: rename_map[col] = 'amount'
                elif '总市值' in col or 'market' in lc: rename_map[col] = 'total_mv'
            df = df.rename(columns=rename_map)

            for col in ['price', 'pct_chg', 'amount', 'total_mv']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            # 过滤
            df = df[df['amount'] >= self.min_amount_wan * 10000]
            df = df[~df['name'].str.contains('杠杆|反向|货币|国债|利率', na=False)]

            logger.info(f"  [Data] akshare获取 {len(df)} 只ETF")
            return df
        except Exception as e:
            logger.error(f"  [Data] akshare获取失败: {e}")
            return None

    def _run_sub_strategy(self, name: str, etf_df: pd.DataFrame,
                          trade_date: str) -> Optional[pd.DataFrame]:
        """运行单个子策略"""
        if name == 'bottom_fish':
            return self._strategy_bottom_fish(etf_df)
        elif name == 'momentum':
            return self._strategy_momentum(etf_df)
        elif name == 'dual_momentum':
            return self._strategy_dual_momentum(etf_df)
        elif name == 'smart_money':
            return self._strategy_smart_money(etf_df)
        elif name == 'vol_timing':
            return self._strategy_vol_timing(etf_df)
        return None

    def _strategy_bottom_fish(self, df: pd.DataFrame) -> pd.DataFrame:
        """抄底反弹：RSI超卖 + 放量回升"""
        result = df.copy()
        result['sub_score'] = 0.0

        # RSI超卖（用pct_chg近似：近5日平均跌幅）
        if 'pct_chg' in result.columns:
            oversold = result['pct_chg'] < -2.0
            result.loc[oversold, 'sub_score'] += 0.5

        # 放量（vol_5d / vol_20d > 1.2）
        if 'vol_5d' in result.columns and 'vol_20d' in result.columns:
            vol_ratio = result['vol_5d'] / result['vol_20d'].replace(0, np.nan)
            result['sub_score'] += (vol_ratio > 1.2).astype(float) * 0.5

        result = result[result['sub_score'] > 0].sort_values('sub_score', ascending=False)
        result['strategy'] = 'bottom_fish'
        return result.head(self.top_n * 2)

    def _strategy_momentum(self, df: pd.DataFrame) -> pd.DataFrame:
        """趋势动量：强者恒强"""
        result = df.copy()
        result['sub_score'] = 0.0

        # 近20日涨幅
        if 'close_20d' in result.columns and 'price' in result.columns:
            ret_20 = (result['price'] - result['close_20d']) / result['close_20d'].replace(0, np.nan)
            result['sub_score'] += (ret_20 > 0).astype(float) * 0.5
            result['ret_20d'] = ret_20

        # 近5日涨幅
        if 'close_5d' in result.columns and 'price' in result.columns:
            ret_5 = (result['price'] - result['close_5d']) / result['close_5d'].replace(0, np.nan)
            result['sub_score'] += (ret_5 > 0).astype(float) * 0.5

        result = result[result['sub_score'] > 0].sort_values('sub_score', ascending=False)
        result['strategy'] = 'momentum'
        return result.head(self.top_n * 2)

    def _strategy_dual_momentum(self, df: pd.DataFrame) -> pd.DataFrame:
        """双重动量：绝对动量 + 相对动量"""
        result = df.copy()
        result['sub_score'] = 0.0

        # 绝对动量：近60日收益 > 1.5%（3个月货币基金收益）
        if 'close_60d' in result.columns and 'price' in result.columns:
            ret_60 = (result['price'] - result['close_60d']) / result['close_60d'].replace(0, np.nan)
            abs_ok = ret_60 > 0.015
            result.loc[abs_ok, 'sub_score'] += 0.5
            result = result[abs_ok]

        # 相对动量：排名
        if not result.empty and 'ret_20d' in result.columns:
            result['sub_score'] += result['ret_20d'].rank(pct=True) * 0.5

        result = result[result['sub_score'] > 0].sort_values('sub_score', ascending=False)
        result['strategy'] = 'dual_momentum'
        return result.head(self.top_n * 2)

    def _strategy_smart_money(self, df: pd.DataFrame) -> pd.DataFrame:
        """资金净流入：价稳量增"""
        result = df.copy()
        result['sub_score'] = 0.0

        # 价格横盘（近5日涨跌幅在 -3% ~ +2%）
        if 'pct_chg' in result.columns:
            stable = result['pct_chg'].between(-3, 2)
            result.loc[stable, 'sub_score'] += 0.3

        # 量增（vol_5d / vol_20d > 1.15）
        if 'vol_5d' in result.columns and 'vol_20d' in result.columns:
            vol_ratio = result['vol_5d'] / result['vol_20d'].replace(0, np.nan)
            result['sub_score'] += (vol_ratio > 1.15).astype(float) * 0.4

        # 成交额放大
        if 'amount' in result.columns:
            result['sub_score'] += result['amount'].rank(pct=True) * 0.3

        result = result[result['sub_score'] > 0.3].sort_values('sub_score', ascending=False)
        result['strategy'] = 'smart_money'
        return result.head(self.top_n * 2)

    def _strategy_vol_timing(self, df: pd.DataFrame) -> pd.DataFrame:
        """波动率择时：防御型ETF"""
        result = df.copy()
        result['sub_score'] = 0.0

        # 防御型关键词
        defensive_kw = ['国债', '债券', '消费', '医疗', '医药', '食品', '公用', '银行', '黄金']
        mask = result['name'].apply(lambda n: any(kw in str(n) for kw in defensive_kw))
        result.loc[mask, 'sub_score'] = 0.5

        # 流动性加分
        if 'amount' in result.columns:
            result['sub_score'] += result['amount'].rank(pct=True) * 0.5

        result = result[result['sub_score'] > 0].sort_values('sub_score', ascending=False)
        result['strategy'] = 'vol_timing'
        return result.head(self.top_n * 2)

    def _merge_results(self, sub_results: dict, weights: dict) -> pd.DataFrame:
        """融合各子策略结果"""
        score_map: dict = {}
        meta_map: dict = {}
        strategy_map: dict = {}

        for strategy_name, df in sub_results.items():
            w = weights.get(strategy_name, 0)
            if w == 0:
                continue

            for _, row in df.iterrows():
                code = str(row.get('code', ''))
                if not code:
                    continue
                if code not in score_map:
                    score_map[code] = 0.0
                    meta_map[code] = {
                        'code': code,
                        'name': row.get('name', ''),
                    }
                    strategy_map[code] = []

                score_map[code] += float(row.get('sub_score', 0)) * w
                strategy_map[code].append(strategy_name)

        records = []
        for code, score in score_map.items():
            rec = meta_map[code].copy()
            rec['score'] = score
            rec['strategies'] = ','.join(sorted(set(strategy_map[code])))
            records.append(rec)

        if not records:
            return pd.DataFrame()

        result = (pd.DataFrame(records)
                  .sort_values('score', ascending=False)
                  .head(self.top_n)
                  .reset_index(drop=True))
        result['advice'] = '分批建仓，止损-5%'
        return result
