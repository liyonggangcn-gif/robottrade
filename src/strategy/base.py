"""
BaseStrategy: 策略中心抽象基类（v2 统一版）

所有策略必须继承此类，统一：
  - 接口规范：run() 返回固定列格式的 DataFrame
  - 基础过滤：filter_universe() 剔除ST/退市/次新股/极微盘
  - 信号存储：save_signals() 写入 strategy_signals 表
  - 消息推送：notify() 默认钉钉格式，子类可覆盖
  - 因子处理：_rank_norm / _winsorize / _zscore / _industry_neutral
  - 评分稳定：_apply_score_ema() 防止评分每日剧变
  - 空仓判断：_should_empty_position() 动态空仓（财报季/极端行情）

strategy_signals 表结构（首次调用 save_signals 时自动建表）：
  trade_date      DATE         信号日期
  strategy        VARCHAR(30)  策略名称（小写下划线）
  ts_code         VARCHAR(15)  股票代码 000001.SZ
  name            VARCHAR(20)  股票名称
  score           FLOAT        综合评分 [0, 1]
  rank_in_strategy INT         策略内排名
  signal_detail   TEXT         各维度子分（JSON字符串）
  macro_level     VARCHAR(10)  推送时的宏观风险等级
  created_at      DATETIME     写入时间
"""

import json
import numpy as np
import pandas as pd
from abc import ABC, abstractmethod
from datetime import datetime
from loguru import logger

from src.utils.db_utils import DBUtils
from src.utils.notifier import send_alert


# ─────────────────────────────────────────────
# DDL：strategy_signals 表
# ─────────────────────────────────────────────
_CREATE_SIGNALS_TABLE_SQLITE = """
CREATE TABLE IF NOT EXISTS strategy_signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date       VARCHAR(20)  NOT NULL,
    strategy         VARCHAR(30)  NOT NULL,
    ts_code          VARCHAR(15)  NOT NULL,
    name             VARCHAR(20),
    score            REAL,
    rank_in_strategy INTEGER,
    signal_detail    TEXT,
    macro_level      VARCHAR(10)  DEFAULT 'NORMAL',
    created_at       VARCHAR(30)  DEFAULT (datetime('now', 'localtime')),
    UNIQUE (trade_date, strategy, ts_code)
)
"""

_CREATE_SIGNALS_TABLE_MYSQL = """
CREATE TABLE IF NOT EXISTS strategy_signals (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    trade_date       VARCHAR(20)  NOT NULL,
    strategy         VARCHAR(30)  NOT NULL,
    ts_code          VARCHAR(15)  NOT NULL,
    name             VARCHAR(20),
    score            DOUBLE,
    rank_in_strategy INT,
    signal_detail    TEXT,
    macro_level      VARCHAR(10)  DEFAULT 'NORMAL',
    created_at       DATETIME     DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_signal (trade_date, strategy, ts_code)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci
"""


class BaseStrategy(ABC):
    """策略抽象基类：定义统一接口与公共能力"""

    # 子类必须定义
    name: str = "base"
    version: str = "1.0"
    display_name: str = "基础策略"  # 中文显示名

    # ──────────────────────────────────────────
    # 抽象接口
    # ──────────────────────────────────────────

    @abstractmethod
    def run(self, trade_date: str = None, top_k: int = 20) -> pd.DataFrame:
        """执行策略选股，返回标准格式 DataFrame

        Returns:
            DataFrame，固定列：
                ts_code         str    股票代码，如 000001.SZ
                name            str    股票名称
                score           float  综合评分，归一化到 [0, 1]
                rank            int    策略内排名（1 = 最优）
                strategy        str    策略名称（self.name）
                signal_reason   str    入选理由简述
                sub_scores      dict   各子维度评分（会被序列化为JSON存储）
                trade_date      str    信号日期 YYYY-MM-DD
        """

    # ──────────────────────────────────────────
    # 公共工具：宇宙过滤
    # ──────────────────────────────────────────

    def filter_universe(self, df: pd.DataFrame,
                        min_mv_yi: float = 10.0,
                        min_days_listed: int = 90) -> pd.DataFrame:
        """通用基础过滤，剔除不可投标的

        过滤规则：
          1. 名称含 ST / *ST / 退市 → 剔除
          2. 上市天数 < min_days_listed → 剔除次新股
          3. 总市值 < min_mv_yi 亿 → 剔除极微盘（流动性不足）

        Args:
            df:              含 name / list_date / total_mv 列的 DataFrame
            min_mv_yi:       最低市值（亿元），默认 10 亿
            min_days_listed: 最短上市天数，默认 90 天

        Returns:
            过滤后的 DataFrame（重置 index）
        """
        before = len(df)

        # 1. ST / 退市过滤
        if 'name' in df.columns:
            mask_st = df['name'].str.contains(r'ST|\*ST|退', na=False, regex=True)
            df = df[~mask_st]

        # 2. 次新股过滤（需要 list_date 列）
        if 'list_date' in df.columns and min_days_listed > 0:
            today = datetime.now().date()
            df['_list_days'] = pd.to_datetime(
                df['list_date'], format='%Y%m%d', errors='coerce'
            ).apply(lambda d: (today - d.date()).days if pd.notna(d) else 0)
            df = df[df['_list_days'] >= min_days_listed]
            df = df.drop(columns=['_list_days'])

        # 3. 极微盘过滤（total_mv 单位：万元 → 换算亿元）
        if 'total_mv' in df.columns and min_mv_yi > 0:
            mv_col = df['total_mv']
            if mv_col.median() > 10000:
                mv_in_yi = mv_col / 10000
            else:
                mv_in_yi = mv_col
            df = df[mv_in_yi >= min_mv_yi]

        after = len(df)
        logger.debug(f"[{self.name}] filter_universe: {before} → {after} 只（剔除 {before-after}）")
        return df.reset_index(drop=True)

    # ──────────────────────────────────────────
    # 公共工具：因子处理
    # ──────────────────────────────────────────

    @staticmethod
    def _rank_norm(s: pd.Series, ascending: bool = True) -> pd.Series:
        """排名归一化到 [0, 1]，处理缺失值和全相同情况

        Args:
            s: 原始因子值
            ascending: True=值越大排名越高（正向因子），False=值越小排名越高（反向因子）

        Returns:
            归一化后的 Series，缺失值填 0.5
        """
        result = pd.Series(0.5, index=s.index)
        has = s.notna()
        if has.sum() < 2:
            return result
        rank = s[has].rank(ascending=ascending, method='average')
        result[has] = (rank - 1) / (has.sum() - 1)
        return result

    @staticmethod
    def _normalize_score(series: pd.Series) -> pd.Series:
        """Min-Max 归一化到 [0, 1]，处理全相同情况"""
        mn, mx = series.min(), series.max()
        if mx == mn:
            return pd.Series([0.5] * len(series), index=series.index)
        return (series - mn) / (mx - mn)

    @staticmethod
    def _zscore(series: pd.Series) -> pd.Series:
        """Z-score 标准化，标准差为 0 时返回全 0"""
        std = series.std()
        if std == 0:
            return pd.Series([0.0] * len(series), index=series.index)
        return (series - series.mean()) / std

    @staticmethod
    def _winsorize(series: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
        """去极值：按分位数截断"""
        lo = series.quantile(lower)
        hi = series.quantile(upper)
        return series.clip(lo, hi)

    def _industry_neutral(self, df: pd.DataFrame, factor_col: str) -> pd.Series:
        """行业中性化：因子值减去行业均值，保留行业内相对排名

        用于 Barra 风格多因子模型，消除行业暴露。

        Args:
            df: 含 industry 列和 factor_col 列
            factor_col: 需要中性化的因子列名

        Returns:
            中性化后的 Series（行业内 z-score）
        """
        if 'industry' not in df.columns or factor_col not in df.columns:
            return df[factor_col]

        result = pd.Series(np.nan, index=df.index)
        for ind, grp in df.groupby('industry'):
            if len(grp) < 3:
                result[grp.index] = 0.0
                continue
            vals = grp[factor_col].dropna()
            if vals.std() == 0 or len(vals) < 2:
                result[grp.index] = 0.0
            else:
                result[grp.index] = (vals - vals.mean()) / vals.std()
        return result.fillna(0.0)

    def _size_neutral(self, df: pd.DataFrame, factor_col: str) -> pd.Series:
        """市值中性化：对因子做市值回归，取残差

        Args:
            df: 含 total_mv 列和 factor_col 列
            factor_col: 需要中性化的因子列名

        Returns:
            中性化后的 Series
        """
        if 'total_mv' not in df.columns or factor_col not in df.columns:
            return df[factor_col]

        valid = df[[factor_col, 'total_mv']].dropna()
        if len(valid) < 10:
            return df[factor_col]

        import statsmodels.api as sm
        X = sm.add_constant(np.log(valid['total_mv']))
        y = valid[factor_col]
        try:
            model = sm.OLS(y, X).fit()
            residuals = model.resid
            result = pd.Series(0.0, index=df.index)
            result[valid.index] = residuals
            return result
        except Exception:
            return df[factor_col]

    # ──────────────────────────────────────────
    # 公共工具：评分稳定性（EMA 平滑）
    # ──────────────────────────────────────────

    def _apply_score_ema(self, df: pd.DataFrame,
                         score_col: str = 'score',
                         alpha: float = 0.40) -> pd.Series:
        """对评分应用 EMA 平滑：new_ema = α × today + (1-α) × prev_ema

        从 score_history 表读取上期 EMA，无历史则直接用当日评分。

        Args:
            df: 含 ts_code 和 score_col 的 DataFrame
            score_col: 原始评分列名
            alpha: EMA 系数（0.4 = 当日40% + 历史60%）

        Returns:
            pd.Series: 平滑后的评分
        """
        try:
            history = DBUtils.query_df(
                "SELECT ts_code, ema_score FROM score_history "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM score_history)"
            )
            if history.empty:
                return df[score_col]

            ema_map = dict(zip(history['ts_code'].astype(str), history['ema_score']))
            raw = df[score_col].values.copy()
            smoothed = raw.copy()
            applied = 0

            for i, code in enumerate(df['ts_code']):
                prev = ema_map.get(str(code))
                if prev is not None and prev > 0:
                    smoothed[i] = alpha * raw[i] + (1 - alpha) * prev
                    applied += 1

            if applied > 0:
                logger.debug(f"[{self.name}] EMA平滑 {applied}/{len(df)} 只 (α={alpha})")

            return pd.Series(smoothed, index=df.index)
        except Exception as e:
            logger.debug(f"[{self.name}] EMA平滑失败: {e}，使用原始评分")
            return df[score_col]

    def _save_scores_to_history(self, df: pd.DataFrame, trade_date: str,
                                score_col: str = 'score'):
        """将今日评分写入 score_history 表（供明日 EMA 使用）"""
        if df.empty or 'ts_code' not in df.columns:
            return
        try:
            DBUtils.execute("DELETE FROM score_history WHERE trade_date = ?",
                          params=[trade_date])
            for _, row in df.iterrows():
                try:
                    DBUtils.execute(
                        "INSERT INTO score_history (trade_date,ts_code,raw_score,ema_score) "
                        "VALUES (?,?,?,?)",
                        (trade_date, str(row['ts_code']),
                         float(row.get(score_col, 0)), float(row.get(score_col, 0)))
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[{self.name}] 保存评分历史失败: {e}")

    # ──────────────────────────────────────────
    # 公共工具：动态空仓判断
    # ──────────────────────────────────────────

    def _should_empty_position(self, trade_date: str = None,
                               calendar_months: list = None,
                               market_threshold: float = None) -> bool:
        """动态空仓判断：财报季 + 极端行情自动空仓

        Args:
            trade_date: 当前交易日期
            calendar_months: 固定空仓月份（如 [1, 4] = 1月和4月）
            market_threshold: 全市场近5日平均下跌占比阈值（如 0.35 = 65%+股票下跌时空仓）

        Returns:
            True = 应空仓，False = 可正常选股
        """
        if trade_date is None:
            trade_date = self._resolve_trade_date()

        dt = pd.Timestamp(trade_date)

        # 1. 财报季空仓：4月（年报+一季报）和1月（年报预告）
        if calendar_months and dt.month in calendar_months:
            logger.info(f"[{self.name}] 财报季空仓：{dt.month}月")
            return True

        # 2. 极端行情空仓：全市场连续下跌
        if market_threshold is not None:
            try:
                df = DBUtils.query_df("""
                    SELECT trade_date,
                           SUM(CASE WHEN close < open THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS down_pct
                    FROM stock_daily
                    WHERE trade_date <= ?
                    GROUP BY trade_date
                    ORDER BY trade_date DESC
                    LIMIT 5
                """, params=[trade_date])
                if not df.empty:
                    avg_down = df['down_pct'].mean()
                    if avg_down > market_threshold:
                        logger.info(f"[{self.name}] 极端行情空仓：5日平均下跌占比={avg_down:.1%}")
                        return True
            except Exception:
                pass

        return False

    # ──────────────────────────────────────────
    # 公共工具：信号存储
    # ──────────────────────────────────────────

    def save_signals(self, result_df: pd.DataFrame,
                     macro_level: str = 'NORMAL') -> int:
        """将选股结果写入 strategy_signals 表"""
        if result_df is None or result_df.empty:
            return 0

        self._ensure_signals_table()

        rows = []
        for _, row in result_df.iterrows():
            sub_scores_json = json.dumps(
                row.get('sub_scores', {}), ensure_ascii=False
            ) if isinstance(row.get('sub_scores'), dict) else '{}'

            rows.append((
                str(row.get('trade_date', '')),
                self.name,
                str(row.get('ts_code', '')),
                str(row.get('name', '')),
                float(row.get('score', 0.0)),
                int(row.get('rank', 0)),
                sub_scores_json,
                macro_level,
            ))

        from src.utils.config_loader import Config
        is_mysql = Config.get('db_type', 'sqlite') == 'mysql'
        ok = 0
        for row in rows:
            try:
                if is_mysql:
                    DBUtils.execute(
                        "INSERT INTO strategy_signals"
                        " (trade_date, strategy, ts_code, name, score,"
                        "  rank_in_strategy, signal_detail, macro_level)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON DUPLICATE KEY UPDATE score=VALUES(score),"
                        "  rank_in_strategy=VALUES(rank_in_strategy),"
                        "  signal_detail=VALUES(signal_detail)",
                        row
                    )
                else:
                    DBUtils.execute(
                        "INSERT OR REPLACE INTO strategy_signals"
                        " (trade_date, strategy, ts_code, name, score,"
                        "  rank_in_strategy, signal_detail, macro_level)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        row
                    )
                ok += 1
            except Exception as e:
                logger.warning(f"[{self.name}] save row 失败: {e}")
        logger.info(f"[{self.name}] 写入 strategy_signals {ok} 条")
        return ok

    def _ensure_signals_table(self):
        """建表（幂等，已存在则跳过）"""
        try:
            from src.utils.config_loader import Config
            is_mysql = Config.get('db_type', 'sqlite') == 'mysql'
            ddl = _CREATE_SIGNALS_TABLE_MYSQL if is_mysql else _CREATE_SIGNALS_TABLE_SQLITE
            DBUtils.execute(ddl)
        except Exception as e:
            logger.warning(f"[{self.name}] 建表 strategy_signals 异常（可忽略）: {e}")

    # ──────────────────────────────────────────
    # 公共工具：消息推送
    # ──────────────────────────────────────────

    def notify(self, result_df: pd.DataFrame,
               trade_date: str = '',
               macro_level: str = 'NORMAL') -> bool:
        """推送选股结果到钉钉（子类可覆盖以定制格式）"""
        if result_df is None or result_df.empty:
            logger.warning(f"[{self.name}] notify: 结果为空，跳过推送")
            return False

        level_tag = {
            'CRISIS': '🔴 流动性危机模式',
            'HIGH':   '🟠 高风险模式',
            'MEDIUM': '🟡 中风险模式',
            'NORMAL': '🟢 正常模式',
        }.get(macro_level, '')

        title = f"【{self.display_name}】选股信号 {trade_date}"
        lines = []

        if level_tag:
            lines.append(f"> {level_tag}\n")

        lines.append(f"**策略**：{self.display_name}  "
                     f"**日期**：{trade_date}  "
                     f"**入选**：{len(result_df)} 只\n")
        lines.append("| 排名 | 代码 | 名称 | 评分 | 入选理由 |")
        lines.append("|------|------|------|------|----------|")

        for _, row in result_df.head(20).iterrows():
            reason = str(row.get('signal_reason', ''))[:30]
            lines.append(
                f"| {int(row.get('rank', 0))} "
                f"| {row.get('ts_code', '')} "
                f"| {row.get('name', '')} "
                f"| {float(row.get('score', 0)):.3f} "
                f"| {reason} |"
            )

        content = '\n'.join(lines)
        return send_alert(title, content, message_type=self.name)

    # ──────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────

    @staticmethod
    def _resolve_trade_date(trade_date: str = None) -> str:
        """若未传日期，取数据库最新交易日"""
        if trade_date:
            return trade_date
        try:
            df = DBUtils.query_df(
                "SELECT MAX(trade_date) AS dt FROM stock_daily"
            )
            return df.iloc[0]['dt']
        except Exception:
            return datetime.now().strftime('%Y-%m-%d')

    @staticmethod
    def _empty_result(columns: list = None) -> pd.DataFrame:
        """返回空结果 DataFrame"""
        if columns is None:
            columns = [
                'ts_code', 'name', 'score', 'rank', 'strategy',
                'signal_reason', 'sub_scores', 'trade_date',
            ]
        return pd.DataFrame(columns=columns)
