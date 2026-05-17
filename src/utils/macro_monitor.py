"""
MacroMonitor: 跨资产宏观预警模块

监控逻辑：
  每日从 yfinance 拉取跨资产价格，计算六类预警信号，
  输出宏观风险等级（NORMAL / MEDIUM / HIGH / CRISIS）。

六类预警：
  1. 流动性危机  — 股+金+债同时5日跌幅>3%     → CRISIS
  2. VIX飙升    — VIX > 35                    → CRISIS
  3. 利率冲击   — 10年美债单周跳升>25bp        → HIGH
  4. 美元强势   — DXY 5日涨幅>2%              → HIGH
  5. 黄金异动   — 黄金5日跌幅>4%              → MEDIUM
  6. 跨资产相关性 — 股+金+债 20日相关>0.6      → MEDIUM

等级对应策略仓位系数：
  CRISIS → 0.20   HIGH → 0.50   MEDIUM → 0.80   NORMAL → 1.00

结果缓存到 macro_indicators 表，CRISIS/HIGH 自动推送钉钉。

用法：
    monitor = MacroMonitor()
    state = monitor.assess()
    print(state.level, state.multiplier, state.details)
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from src.utils.db_utils import DBUtils
from src.utils.notifier import send_alert


# ─────────────────────────────────────────────
# DDL
# ─────────────────────────────────────────────
_CREATE_MACRO_TABLE = """
CREATE TABLE IF NOT EXISTS macro_indicators (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    data_date VARCHAR(20) NOT NULL,
    indicator VARCHAR(50) NOT NULL,
    value     REAL,
    freq      VARCHAR(10) DEFAULT 'daily',
    source    VARCHAR(20) DEFAULT 'yfinance',
    UNIQUE (data_date, indicator)
)
"""

# 风险等级 → 仓位系数
_LEVEL_MULT = {
    'CRISIS': 0.20,
    'HIGH':   0.50,
    'MEDIUM': 0.80,
    'NORMAL': 1.00,
}

# 风险等级优先级（数字越大越高）
_LEVEL_RANK = {'NORMAL': 0, 'MEDIUM': 1, 'HIGH': 2, 'CRISIS': 3}


@dataclass
class MacroState:
    """宏观风险状态"""
    level: str = 'NORMAL'               # NORMAL / MEDIUM / HIGH / CRISIS
    multiplier: float = 1.0             # 仓位系数
    triggered: List[str] = field(default_factory=list)   # 触发的预警名称列表
    details: Dict[str, float] = field(default_factory=dict)  # 各指标最新值
    as_of: str = ''                     # 数据日期

    def is_alert(self) -> bool:
        return self.level in ('HIGH', 'CRISIS')


class MacroMonitor:
    """跨资产宏观预警器"""

    # yfinance 数据源映射
    TICKERS = {
        'vix':   '^VIX',        # 恐慌指数
        'gold':  'GLD',         # 黄金ETF（SPDR）
        'us10y': '^TNX',        # 10年美债收益率（%）
        'dxy':   'DX-Y.NYB',    # 美元指数
        'sp500': '^GSPC',       # 标普500
    }

    # 预警阈值（可通过 config 覆盖）
    THRESHOLDS = {
        'vix_crisis':        35.0,    # VIX > 35 → CRISIS
        'crisis_ret5':       -0.03,   # 5日跌幅 < -3%（股+金+债同时）
        'us10y_weekly_jump': 0.25,    # 美债单周跳升 > 25bp → HIGH
        'dxy_ret5':          0.02,    # DXY 5日涨幅 > 2% → HIGH
        'gold_ret5':         -0.04,   # 黄金5日跌幅 < -4% → MEDIUM
        'corr_threshold':    0.60,    # 跨资产20日相关 > 0.6 → MEDIUM
    }

    def __init__(self, cache_hours: int = 12):
        """
        Args:
            cache_hours: 缓存有效时长（小时），避免频繁拉取
        """
        self.cache_hours = cache_hours
        self._ensure_table()

    # ──────────────────────────────────────────
    # 主接口
    # ──────────────────────────────────────────

    def assess(self, force_refresh: bool = False) -> MacroState:
        """评估当前宏观风险等级

        Args:
            force_refresh: 忽略缓存强制重新拉取

        Returns:
            MacroState 对象
        """
        logger.info("[MacroMonitor] 开始跨资产宏观评估...")

        # 尝试从缓存读取
        if not force_refresh:
            cached = self._load_from_cache()
            if cached is not None:
                logger.info(f"[MacroMonitor] 使用缓存数据 ({cached.as_of})，等级={cached.level}")
                return cached

        # 拉取最新跨资产数据
        price_data = self._fetch_prices()
        if price_data is None or price_data.empty:
            logger.warning("[MacroMonitor] 数据拉取失败，返回 NORMAL（不阻断策略）")
            return MacroState(level='NORMAL', multiplier=1.0,
                              triggered=[], details={}, as_of='')

        # 计算各指标
        details = self._calc_indicators(price_data)

        # 逐条规则判断
        state = self._apply_rules(details)
        state.as_of = datetime.now().strftime('%Y-%m-%d')

        # 保存到数据库
        self._save_to_db(details, state)

        # 高危时推送钉钉
        if state.is_alert():
            self._notify(state)

        logger.info(f"[MacroMonitor] 评估完成：等级={state.level} "
                    f"系数={state.multiplier} 触发={state.triggered}")
        return state

    # ──────────────────────────────────────────
    # 数据获取
    # ──────────────────────────────────────────

    def _fetch_prices(self, days: int = 30) -> Optional[pd.DataFrame]:
        """从 yfinance 拉取近30日跨资产收盘价"""
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("[MacroMonitor] yfinance 未安装，跳过跨资产监控")
            return None

        end = datetime.now()
        start = end - timedelta(days=days + 10)   # 多取10天防假期缺口

        tickers = list(self.TICKERS.values())
        col_map = {v: k for k, v in self.TICKERS.items()}   # ^VIX → vix

        try:
            raw = yf.download(
                tickers,
                start=start.strftime('%Y-%m-%d'),
                end=end.strftime('%Y-%m-%d'),
                auto_adjust=True,
                progress=False,
            )
            # 取收盘价
            if isinstance(raw.columns, pd.MultiIndex):
                close = raw['Close'].copy()
            else:
                close = raw[['Close']].copy()

            close = close.rename(columns=col_map)
            close = close.dropna(how='all').tail(days)
            logger.info(f"[MacroMonitor] 拉取 {len(close)} 日跨资产数据，"
                        f"列={list(close.columns)}")
            return close
        except Exception as e:
            logger.error(f"[MacroMonitor] yfinance 拉取失败: {e}")
            return None

    # ──────────────────────────────────────────
    # 指标计算
    # ──────────────────────────────────────────

    def _calc_indicators(self, df: pd.DataFrame) -> Dict[str, float]:
        """计算各预警指标值"""
        result = {}

        def ret_n(col: str, n: int) -> Optional[float]:
            """计算最近n日涨跌幅"""
            if col not in df.columns or len(df) < n + 1:
                return None
            vals = df[col].dropna()
            if len(vals) < n + 1:
                return None
            return float(vals.iloc[-1] / vals.iloc[-(n+1)] - 1)

        def latest(col: str) -> Optional[float]:
            if col not in df.columns:
                return None
            vals = df[col].dropna()
            return float(vals.iloc[-1]) if len(vals) > 0 else None

        # VIX 最新值
        result['vix_latest'] = latest('vix') or 0.0

        # 各资产5日涨跌幅
        result['sp500_ret5'] = ret_n('sp500', 5) or 0.0
        result['gold_ret5']  = ret_n('gold', 5)  or 0.0
        result['us10y_ret5'] = ret_n('us10y', 5) or 0.0   # 收益率5日变化
        result['dxy_ret5']   = ret_n('dxy', 5)   or 0.0

        # 美债单周绝对变化（bp）：us10y 是收益率%，差值×100=bp
        if 'us10y' in df.columns and len(df) >= 6:
            vals = df['us10y'].dropna()
            if len(vals) >= 6:
                result['us10y_weekly_chg_bp'] = float(
                    (vals.iloc[-1] - vals.iloc[-6]) * 100
                )
            else:
                result['us10y_weekly_chg_bp'] = 0.0
        else:
            result['us10y_weekly_chg_bp'] = 0.0

        # 跨资产20日滚动相关（股+金+债）
        result['cross_asset_corr'] = self._calc_cross_corr(df, window=20)

        return result

    def _calc_cross_corr(self, df: pd.DataFrame, window: int = 20) -> float:
        """计算股+金+债三者两两相关均值（判断分散化是否失效）"""
        needed = ['sp500', 'gold', 'us10y']
        available = [c for c in needed if c in df.columns]
        if len(available) < 2:
            return 0.0

        sub = df[available].dropna().tail(window)
        if len(sub) < 10:
            return 0.0

        ret = sub.pct_change().dropna()
        corr_matrix = ret.corr()

        # 取上三角非对角线均值（两两相关的平均）
        pairs, total = 0, 0.0
        cols = list(corr_matrix.columns)
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                val = corr_matrix.iloc[i, j]
                if pd.notna(val):
                    total += abs(val)
                    pairs += 1

        return float(total / pairs) if pairs > 0 else 0.0

    # ──────────────────────────────────────────
    # 规则判断
    # ──────────────────────────────────────────

    def _apply_rules(self, d: Dict[str, float]) -> MacroState:
        """逐条规则判断，取最高等级"""
        triggered = []
        max_level = 'NORMAL'

        def upgrade(level: str, name: str):
            nonlocal max_level
            triggered.append(name)
            if _LEVEL_RANK[level] > _LEVEL_RANK[max_level]:
                max_level = level

        t = self.THRESHOLDS

        # ── CRISIS 级 ────────────────────────
        # 规则1：流动性危机（股+金+债同时暴跌）
        if (d.get('sp500_ret5', 0) < t['crisis_ret5'] and
                d.get('gold_ret5',  0) < t['crisis_ret5'] and
                d.get('us10y_ret5', 0) < t['crisis_ret5']):
            upgrade('CRISIS', '流动性危机(股+金+债同跌)')

        # 规则2：VIX 飙升
        if d.get('vix_latest', 0) > t['vix_crisis']:
            upgrade('CRISIS', f"VIX飙升({d['vix_latest']:.1f})")

        # ── HIGH 级 ──────────────────────────
        # 规则3：美债收益率单周跳升
        if d.get('us10y_weekly_chg_bp', 0) > t['us10y_weekly_jump'] * 100:
            upgrade('HIGH', f"美债利率冲击(+{d['us10y_weekly_chg_bp']:.0f}bp)")

        # 规则4：美元急涨（新兴市场资金外流信号）
        if d.get('dxy_ret5', 0) > t['dxy_ret5']:
            upgrade('HIGH', f"美元急涨({d['dxy_ret5']*100:+.1f}%)")

        # ── MEDIUM 级 ────────────────────────
        # 规则5：黄金异动下跌（流动性收紧前兆）
        if d.get('gold_ret5', 0) < t['gold_ret5']:
            upgrade('MEDIUM', f"黄金异动({d['gold_ret5']*100:+.1f}%)")

        # 规则6：跨资产相关性骤升（分散化失效）
        if d.get('cross_asset_corr', 0) > t['corr_threshold']:
            upgrade('MEDIUM', f"跨资产相关({d['cross_asset_corr']:.2f})")

        return MacroState(
            level=max_level,
            multiplier=_LEVEL_MULT[max_level],
            triggered=triggered,
            details=d,
        )

    # ──────────────────────────────────────────
    # 缓存读写
    # ──────────────────────────────────────────

    def _save_to_db(self, details: Dict[str, float], state: MacroState):
        """将各指标值写入 macro_indicators 表"""
        today = datetime.now().strftime('%Y-%m-%d')
        rows = [(today, k, v, 'daily', 'yfinance')
                for k, v in details.items()]
        # 额外存一条综合等级
        rows.append((today, '_macro_level',
                     float(_LEVEL_RANK[state.level]), 'daily', 'computed'))

        from src.utils.config_loader import Config
        is_mysql = Config.get('db_type', 'sqlite') == 'mysql'
        for data_date, indicator, value, freq, source in rows:
            try:
                if is_mysql:
                    DBUtils.execute(
                        "INSERT INTO macro_indicators (data_date, indicator, value, freq, source)"
                        " VALUES (?, ?, ?, ?, ?)"
                        " ON DUPLICATE KEY UPDATE value=VALUES(value)",
                        (data_date, indicator, value, freq, source)
                    )
                else:
                    DBUtils.execute(
                        "INSERT OR REPLACE INTO macro_indicators"
                        " (data_date, indicator, value, freq, source)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (data_date, indicator, value, freq, source)
                    )
            except Exception as e:
                logger.warning(f"[MacroMonitor] 写入 {indicator} 失败: {e}")

    def _load_from_cache(self) -> Optional[MacroState]:
        """从数据库读取今日缓存（有效期内）"""
        today = datetime.now().strftime('%Y-%m-%d')
        try:
            df = DBUtils.query_df(
                "SELECT indicator, value FROM macro_indicators WHERE data_date = ?",
                params=(today,)
            )
            if df.empty:
                return None

            row_map = dict(zip(df['indicator'], df['value']))
            level_rank = row_map.get('_macro_level', 0)
            # 反查等级名
            level = next(
                (k for k, v in _LEVEL_RANK.items() if v == int(level_rank)),
                'NORMAL'
            )
            details = {k: v for k, v in row_map.items()
                       if not k.startswith('_')}

            return MacroState(
                level=level,
                multiplier=_LEVEL_MULT[level],
                triggered=[],
                details=details,
                as_of=today,
            )
        except Exception as e:
            logger.debug(f"[MacroMonitor] 读取缓存失败: {e}")
            return None

    # ──────────────────────────────────────────
    # 推送通知
    # ──────────────────────────────────────────

    def _notify(self, state: MacroState):
        """CRISIS / HIGH 时推送钉钉预警"""
        level_icon = {'CRISIS': '🔴', 'HIGH': '🟠'}.get(state.level, '🟡')
        title = f"{level_icon} 【宏观预警】{state.level} 级风险"

        d = state.details
        lines = [
            f"**风险等级**：{level_icon} {state.level}  "
            f"**仓位系数**：{state.multiplier:.0%}\n",
            "**触发规则**：",
        ]
        for rule in state.triggered:
            lines.append(f"- {rule}")

        lines += [
            "\n**跨资产指标**：",
            f"| 指标 | 值 |",
            f"|------|-----|",
            f"| VIX  | {d.get('vix_latest', 0):.1f} |",
            f"| 标普500 5日 | {d.get('sp500_ret5', 0)*100:+.1f}% |",
            f"| 黄金 5日 | {d.get('gold_ret5', 0)*100:+.1f}% |",
            f"| 美债10Y 周变化 | {d.get('us10y_weekly_chg_bp', 0):+.0f}bp |",
            f"| 美元指数 5日 | {d.get('dxy_ret5', 0)*100:+.1f}% |",
            f"| 跨资产相关 | {d.get('cross_asset_corr', 0):.2f} |",
            f"\n> 策略仓位已自动压缩至 **{state.multiplier:.0%}**",
        ]

        send_alert(title, '\n'.join(lines), message_type='macro_alert')

    # ──────────────────────────────────────────
    # 内部初始化
    # ──────────────────────────────────────────

    def _ensure_table(self):
        try:
            DBUtils.execute(_CREATE_MACRO_TABLE)
        except Exception as e:
            logger.warning(f"[MacroMonitor] 建表 macro_indicators 异常: {e}")

    # ──────────────────────────────────────────
    # 便捷方法
    # ──────────────────────────────────────────

    def get_latest_state(self) -> MacroState:
        """直接读取数据库缓存（不重新拉取），用于快速查询"""
        cached = self._load_from_cache()
        if cached:
            return cached
        return MacroState(level='NORMAL', multiplier=1.0)

    def summary(self) -> str:
        """返回当前宏观状态的单行摘要（用于日志/推送头部）"""
        state = self.get_latest_state()
        icon = {'CRISIS': '🔴', 'HIGH': '🟠',
                'MEDIUM': '🟡', 'NORMAL': '🟢'}.get(state.level, '')
        return (f"{icon} 宏观={state.level} "
                f"系数={state.multiplier:.0%} "
                f"VIX={state.details.get('vix_latest', 0):.1f} "
                f"黄金5日={state.details.get('gold_ret5', 0)*100:+.1f}%")
