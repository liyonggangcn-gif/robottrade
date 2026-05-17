"""
ETFMomentumRotation: ETF 动量轮动策略

参考聚宽"二八轮动"和"ETF动量轮动"经典策略：
  1. 在宽基ETF + 行业ETF + 债券ETF + 商品ETF 之间轮动
  2. 计算各ETF过去 N 日动量（默认20日），取前 K 只
  3. 若无一为正动量，转入货币ETF/国债ETF防御

聚宽回测参照：8年13倍 ETF动量轮动 (年化37%, 夏普1.0)
"""

import numpy as np
import pandas as pd
from loguru import logger

from src.strategy.base import BaseStrategy
from src.utils.config_loader import Config


# 核心ETF池（宽基+行业+债券+商品+货币）
CORE_ETF_POOL = [
    # 宽基指数
    {"code": "510300", "name": "沪深300ETF",   "category": "宽基"},
    {"code": "510500", "name": "中证500ETF",   "category": "宽基"},
    {"code": "159915", "name": "创业板ETF",    "category": "宽基"},
    {"code": "588000", "name": "科创50ETF",    "category": "宽基"},
    {"code": "512100", "name": "中证1000ETF",  "category": "宽基"},
    {"code": "159845", "name": "中证2000ETF",  "category": "宽基"},
    # 行业ETF
    {"code": "512880", "name": "证券ETF",      "category": "行业"},
    {"code": "512660", "name": "军工ETF",      "category": "行业"},
    {"code": "159865", "name": "养殖ETF",      "category": "行业"},
    {"code": "515030", "name": "新能源车ETF",  "category": "行业"},
    {"code": "515050", "name": "5GETF",        "category": "行业"},
    {"code": "512480", "name": "半导体ETF",    "category": "行业"},
    {"code": "159766", "name": "旅游ETF",      "category": "行业"},
    {"code": "512010", "name": "医药ETF",      "category": "行业"},
    {"code": "159928", "name": "消费ETF",      "category": "行业"},
    {"code": "515900", "name": "食品饮料ETF",  "category": "行业"},
    # 债券
    {"code": "511010", "name": "国债ETF",      "category": "债券"},
    {"code": "511880", "name": "银华日利ETF",  "category": "货币"},
    # 商品
    {"code": "518880", "name": "黄金ETF",      "category": "商品"},
]


class ETFMomentumRotation(BaseStrategy):
    """ETF动量轮动策略：多资产动量排名，只持动量最强的N只"""

    name = 'etf_momentum_rotation'
    version = '1.0'
    display_name = 'ETF动量轮动策略'

    # 默认动量周期（日）
    MOM_WINDOWS = [20, 60, 120]  # 多周期动量加权

    def __init__(self):
        cfg = Config.get('etf_momentum_rotation') or {}
        self.top_n = cfg.get('top_n', 3)            # 持有几只ETF
        self.min_momentum = cfg.get('min_momentum', 0.0)  # 最低动量阈值
        self.defensive_etf = cfg.get('defensive_etf', '511880')  # 防御ETF代码
        self.momentum_weights = cfg.get('momentum_weights', [0.5, 0.3, 0.2])  # 多周期动量权重
        logger.info(f"[ETFMomentumRotation] 初始化: top_n={self.top_n}, "
                    f"min_momentum={self.min_momentum:.1%}")

    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        """执行ETF动量轮动选股

        Args:
            trade_date: 交易日期（仅用于日志）
            top_k: 最终输出的数量（受self.top_n限制）

        Returns:
            标准 DataFrame (ts_code, name, score, rank, ...)
        """
        trade_date = self._resolve_trade_date(trade_date)
        logger.info(f"\n[ETFMomentumRotation] ===== ETF轮动 {trade_date} =====")

        try:
            import akshare as ak
        except ImportError:
            logger.error("[ETFMomentumRotation] akshare 未安装，无法获取ETF行情")
            return self._empty_result()

        # Step 1: 获取ETF池
        etf_pool = self._load_etf_pool()
        if not etf_pool:
            logger.error("[ETFMomentumRotation] ETF池为空")
            return self._empty_result()
        logger.info(f"  [Step 1] ETF池: {len(etf_pool)} 只")

        # Step 2: 获取历史行情并计算动量
        records = []
        for etf in etf_pool:
            try:
                hist = ak.fund_etf_hist_em(
                    symbol=etf['code'],
                    period='daily',
                    start_date='',
                    end_date='',
                    adjust='qfq'
                )
                if hist is None or hist.empty or len(hist) < max(self.MOM_WINDOWS):
                    continue

                # 计算多周期动量
                closes = hist['收盘'].values.astype(float)
                mom_scores = []
                for w, weight in zip(self.MOM_WINDOWS, self.momentum_weights):
                    mom = closes[-1] / closes[-w] - 1 if len(closes) >= w else 0
                    mom_scores.append(mom * weight)

                composite_mom = sum(mom_scores)
                latest_close = closes[-1]

                records.append({
                    'ts_code': etf['code'],
                    'name': etf['name'],
                    'category': etf['category'],
                    'close': latest_close,
                    'mom_20d': round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else 0,
                    'mom_60d': round((closes[-1] / closes[-61] - 1) * 100, 2) if len(closes) >= 61 else 0,
                    'mom_120d': round((closes[-1] / closes[-121] - 1) * 100, 2) if len(closes) >= 121 else 0,
                    'score': composite_mom,
                })
            except Exception as e:
                logger.debug(f"  [SKIP] {etf['name']}({etf['code']}): {e}")
                continue

        if not records:
            logger.warning("[ETFMomentumRotation] 无有效ETF数据")
            return self._empty_result()

        df = pd.DataFrame(records)

        # Step 3: 动量过滤 + 排名
        # 负动量ETF排除（除非全部为负，则保留最强的一个或转防御）
        positive = df[df['score'] > self.min_momentum].copy()
        if positive.empty:
            logger.warning("[ETFMomentumRotation] 全ETF动量为负，转入防御模式")
            # 使用防御ETF
            defense = df[df['ts_code'] == self.defensive_etf]
            if not defense.empty:
                result = defense.copy()
                result['score'] = 0.01  # 低分但正数
            else:
                return self._empty_result()
        else:
            result = positive

        # Step 4: 按 composite momentum 排序取 top_n
        result = result.sort_values('score', ascending=False).head(self.top_n).reset_index(drop=True)

        # Step 5: 标准化输出
        result['score'] = self._normalize_score(result['score'])
        result['rank'] = range(1, len(result) + 1)
        result['strategy'] = self.name
        result['trade_date'] = trade_date
        result['signal_reason'] = result.apply(
            lambda r: f"{r['name']} 20日{r['mom_20d']:+.1f}% 60日{r['mom_60d']:+.1f}%",
            axis=1
        )
        result['sub_scores'] = result.apply(
            lambda r: {'mom_20d': r['mom_20d'], 'mom_60d': r['mom_60d'],
                       'mom_120d': r['mom_120d'], 'category': r['category']},
            axis=1
        )
        result['industry'] = result['category']

        self._print_result(result)

        out_cols = ['ts_code', 'name', 'score', 'rank', 'strategy',
                    'signal_reason', 'sub_scores', 'trade_date', 'industry']
        return result[[c for c in out_cols if c in result.columns]]

    def _load_etf_pool(self) -> list:
        """加载ETF池（配置覆盖默认）"""
        cfg = Config.get('etf_momentum_rotation') or {}
        custom_pool = cfg.get('etf_pool')
        if custom_pool:
            return custom_pool
        return CORE_ETF_POOL

    def _print_result(self, result: pd.DataFrame):
        logger.info(f"\n[ETFMomentumRotation] ===== Top {len(result)} =====")
        for _, row in result.iterrows():
            logger.info(
                f"  #{int(row['rank']):2d} {row['name']}({row['ts_code']}) "
                f"Score={float(row['score']):.3f} "
                f"20日={row['mom_20d']:+.1f}% 60日={row['mom_60d']:+.1f}% "
                f"[{row.get('category', '')}]"
            )

    @staticmethod
    def _empty_result() -> pd.DataFrame:
        return pd.DataFrame(columns=[
            'ts_code', 'name', 'score', 'rank', 'strategy',
            'signal_reason', 'sub_scores', 'trade_date', 'industry',
        ])
