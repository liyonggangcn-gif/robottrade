"""
HoldingManager: 持仓稳定性管理器

核心职责：
  在新的选股信号和当前实际持仓之间做智能换仓决策，解决「换股过频」问题。

核心规则（settings.yaml holding_manager 段可配置）：
  min_hold_days      = 10   最小持仓期（交易日），未满不卖出（止损除外）
  score_threshold    = 0.15  新候选股评分必须比现有持仓高出此值才替换
  max_turnover_pct   = 0.30  单次最多替换 30% 的持仓数量
  stop_loss_pct      = -0.08 亏损超 -8% 强制止损
  take_profit_pct    = 0.25  盈利超 25% 减至半仓（止盈减仓）
  top_k              = 20    目标持仓数量

用法：
    from src.portfolio.holding_manager import HoldingManager
    hm = HoldingManager()
    decision = hm.decide(new_signals_df, trade_date='2026-03-21')
    # decision.buy_list / decision.sell_list / decision.hold_list / decision.summary
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional

import pandas as pd
from loguru import logger

from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


# ── 建表 DDL（幂等）──────────────────────────────────────────
_CREATE_HOLDING_LOG = """
CREATE TABLE IF NOT EXISTS holding_log (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    trade_date   VARCHAR(20) NOT NULL,
    ts_code      VARCHAR(15) NOT NULL,
    name         VARCHAR(30),
    action       VARCHAR(10) NOT NULL,
    reason       VARCHAR(100),
    days_held    INT,
    pnl_pct      DOUBLE,
    score_old    DOUBLE,
    score_new    DOUBLE,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_date (trade_date)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci
"""

_CREATE_HOLDING_LOG_SQLITE = """
CREATE TABLE IF NOT EXISTS holding_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date   VARCHAR(20) NOT NULL,
    ts_code      VARCHAR(15) NOT NULL,
    name         VARCHAR(30),
    action       VARCHAR(10) NOT NULL,
    reason       VARCHAR(100),
    days_held    INT,
    pnl_pct      REAL,
    score_old    REAL,
    score_new    REAL,
    created_at   VARCHAR(30) DEFAULT (datetime('now','localtime'))
)
"""


@dataclass
class PositionRecord:
    """单只持仓记录"""
    ts_code:        str
    name:           str
    avg_cost:       float
    current_price:  float
    shares:         float
    profit_loss_pct: float   # 浮动盈亏 %（小数，如 -0.08 = -8%）
    stop_loss_price: float
    buy_date:       str      # YYYY-MM-DD
    days_held:      int      # 已持有交易日数
    current_score:  float    # 当前在新信号中的评分（0 = 未入选）
    track:          str = '' # 'sector_rotation'|'dividend'|'value'|'both'|''
    min_hold:       int = 5  # 该持仓适用的最短持仓交易日数（A轨5/B轨15）


@dataclass
class HoldingDecision:
    """持仓决策结果"""
    buy_list:    List[Dict] = field(default_factory=list)   # 新买入
    sell_list:   List[Dict] = field(default_factory=list)   # 卖出（含原因）
    hold_list:   List[Dict] = field(default_factory=list)   # 继续持有
    forced_sell: List[Dict] = field(default_factory=list)   # 强制止损
    partial_sell: List[Dict] = field(default_factory=list)  # 止盈减仓
    summary:     str = ""

    @property
    def all_sell(self) -> List[Dict]:
        return self.forced_sell + self.partial_sell + self.sell_list


class HoldingManager:
    """持仓稳定性管理器：解决换股过频导致的摩擦损耗"""

    # A轨（行业动量/AI赛道）最短持仓5交易日（周频调仓）
    # B轨（红利/价值）最短持仓15交易日（~3周，等待均值回归/分红）
    MIN_HOLD_A = 5
    MIN_HOLD_B = 15
    _B_TRACKS  = {'dividend', 'value'}   # 归属B轨的 track 值

    def __init__(self):
        cfg = Config.get('holding_manager') or {}
        self.min_hold_days    = int(cfg.get('min_hold_days', self.MIN_HOLD_A))  # 默认/兜底
        self.score_threshold  = float(cfg.get('score_threshold', 0.15))
        self.max_turnover_pct = float(cfg.get('max_turnover_pct', 0.30))
        self.stop_loss_pct    = float(cfg.get('stop_loss_pct', -0.08))
        self.take_profit_pct  = float(cfg.get('take_profit_pct', 0.25))
        self.top_k            = int(cfg.get('top_k', 20))

        logger.info(
            f"[HoldingManager] 初始化 "
            f"最小持仓期={self.min_hold_days}日 "
            f"换仓阈值={self.score_threshold:.0%} "
            f"单次换仓上限={self.max_turnover_pct:.0%} "
            f"止损={self.stop_loss_pct:.0%} "
            f"止盈={self.take_profit_pct:.0%}"
        )
        self._ensure_log_table()

    # ──────────────────────────────────────────────────────────
    # 主接口
    # ──────────────────────────────────────────────────────────

    def decide(self, new_signals: pd.DataFrame,
               trade_date: str = None,
               prev_picks: set = None) -> HoldingDecision:
        """
        核心决策：对比新信号与当前持仓，输出换仓决策。

        Args:
            new_signals:  StrategyCenter 或 HybridStrategy 输出的选股 DataFrame
                          必须含 ts_code 列；评分列优先读 score，其次 final_score
            trade_date:   信号日期（默认今天）
            prev_picks:   上期推荐的股票代码集合（用于识别近期常客，给予保护）

        Returns:
            HoldingDecision
        """
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')

        logger.info(f"\n[HoldingManager] ===== 换仓决策 {trade_date} =====")

        # 1. 规范化新信号评分列
        signals = self._normalize_signals(new_signals)
        if signals.empty:
            logger.warning("[HoldingManager] 新信号为空，维持全部持仓")
            return HoldingDecision(summary="新信号为空，维持全部持仓")

        # 2. 加载当前持仓 + 计算持仓天数
        positions = self._load_positions(trade_date)
        logger.info(f"  当前持仓: {len(positions)} 只，新候选: {len(signals)} 只")

        decision = HoldingDecision()

        # 3. 止损检查（优先，无视最小持仓期）
        decision.forced_sell = self._check_stop_loss(positions)

        # 4. 止盈减仓检查
        decision.partial_sell = self._check_take_profit(positions)

        # 移除已决定止损/止盈的持仓，剩余参与换仓决策
        excluded = {r['ts_code'] for r in decision.forced_sell + decision.partial_sell}
        active_positions = [p for p in positions if p.ts_code not in excluded]

        # 5. 保护期内持仓（A轨5天/B轨15天，按各自 min_hold 判断）
        protected = [p for p in active_positions if p.days_held < p.min_hold]
        eligible  = [p for p in active_positions if p.days_held >= p.min_hold]
        protected_codes = {p.ts_code for p in protected}

        if protected:
            logger.info(f"  保护期持仓（不换）: {len(protected)} 只 "
                        f"({[p.ts_code for p in protected[:5]]})")

        # 5b. 近期常客保护：即使持仓期满，如果该股票近3天都在推荐列表中，
        #     则提高替换门槛（需要高出更多才换）
        recent_regulars: set = set()
        if prev_picks:
            held_codes = {p.ts_code for p in active_positions}
            recent_regulars = held_codes & prev_picks
            if recent_regulars:
                logger.info(f"  近期常客（提高替换门槛）: {len(recent_regulars)} 只 "
                            f"({list(recent_regulars)[:5]})")

        # 6. 计算替换候选
        held_codes = {p.ts_code for p in active_positions}
        new_codes  = set(signals['ts_code'])

        # 新信号中未持仓的候选
        candidates = signals[~signals['ts_code'].isin(held_codes)].copy()

        # 已持仓但新信号也有的：更新 current_score
        score_map = dict(zip(signals['ts_code'], signals['score']))
        for p in active_positions:
            p.current_score = score_map.get(p.ts_code, 0.0)

        # 7. 对可替换持仓（持仓期满）按评分升序排（最弱先换）
        eligible_sorted = sorted(eligible, key=lambda p: p.current_score)

        # 计算本次最多可替换数量（按持仓总量的 max_turnover_pct）
        total_positions = len(active_positions)
        if total_positions > 0:
            max_replace = max(1, math.floor(total_positions * self.max_turnover_pct))
        else:
            # 空仓启动：按 top_k 比例计算初始建仓数量，不限死在1只
            max_replace = max(1, math.floor(self.top_k * self.max_turnover_pct))
        logger.info(f"  可替换持仓: {len(eligible)} 只，本次上限: {max_replace} 只")

        replaced = 0
        sell_eligible = []
        buy_candidates = candidates.head(max_replace * 2).to_dict('records')  # 备选池

        for pos in eligible_sorted:
            if replaced >= max_replace:
                break
            if not buy_candidates:
                break

            # 找最佳候选：评分 > 当前持仓 + 阈值
            best = buy_candidates[0]
            improvement = best['score'] - pos.current_score

            # 近期常客需要更高的替换门槛（阈值×1.5）
            is_regular = pos.ts_code in recent_regulars
            required_threshold = self.score_threshold * 1.5 if is_regular else self.score_threshold

            if improvement >= required_threshold:
                reason_suffix = " (常客+50%门槛)" if is_regular else ""
                sell_eligible.append({
                    'ts_code':   pos.ts_code,
                    'name':      pos.name,
                    'reason':    f"评分{pos.current_score:.3f}→新{best['score']:.3f}"
                                 f"(+{improvement:.3f}){reason_suffix}",
                    'days_held': pos.days_held,
                    'pnl_pct':   pos.profit_loss_pct,
                    'score_old': pos.current_score,
                })
                decision.buy_list.append({
                    'ts_code':   best['ts_code'],
                    'name':      best.get('name', best['ts_code']),
                    'score':     best['score'],
                    'reason':    f"替换{pos.name}({pos.ts_code}){reason_suffix}",
                    'strategy':  best.get('strategy', ''),
                })
                buy_candidates.pop(0)
                replaced += 1
            else:
                # 最佳候选也不够好，不换
                break

        decision.sell_list = sell_eligible

        # 8. 持有列表（active_positions 中未被卖出的）
        sell_codes = {r['ts_code'] for r in decision.all_sell}
        decision.hold_list = [
            {
                'ts_code':   p.ts_code,
                'name':      p.name,
                'days_held': p.days_held,
                'pnl_pct':   p.profit_loss_pct,
                'score':     p.current_score,
                'track':     p.track,
                'min_hold':  p.min_hold,
                'protected': p.ts_code in protected_codes,
            }
            for p in active_positions
            if p.ts_code not in sell_codes
        ]

        # 9. 如果持仓数量不足 top_k，补充买入
        current_after = len(decision.hold_list) + len(decision.buy_list)
        slots_left = self.top_k - current_after
        if slots_left > 0 and buy_candidates:
            added = 0
            all_held_and_buying = held_codes | {r['ts_code'] for r in decision.buy_list}
            for cand in buy_candidates:
                if added >= slots_left:
                    break
                if cand['ts_code'] not in all_held_and_buying:
                    decision.buy_list.append({
                        'ts_code': cand['ts_code'],
                        'name':    cand.get('name', cand['ts_code']),
                        'score':   cand['score'],
                        'reason':  '补仓位（持仓不足）',
                        'strategy': cand.get('strategy', ''),
                    })
                    added += 1

        # 10. 生成摘要
        decision.summary = self._format_summary(decision, trade_date)
        logger.info(f"\n{decision.summary}")

        # 11. 记录决策日志
        self._log_decisions(decision, trade_date)

        return decision

    # ──────────────────────────────────────────────────────────
    # 持仓加载与计算
    # ──────────────────────────────────────────────────────────

    def _load_positions(self, trade_date: str) -> List[PositionRecord]:
        """加载当前持仓，并计算已持有交易日数。

        优先从 agent_sim_positions（TradingAgent实盘表）读取，
        若为空则兜底读 positions（PositionManager模拟表）。
        """
        df = self._load_from_agent_sim(trade_date)
        if df.empty:
            df = self._load_from_positions_table()
        if df.empty:
            return []

        days_map = self._calc_hold_days(df['buy_date'].tolist(), trade_date)

        # 从 daily_picks 最新一期查各股票所属轨道，决定最短持仓期
        track_map: dict = {}
        try:
            codes = df['ts_code'].tolist()
            ph = ','.join(['?'] * len(codes))
            tk_df = DBUtils.query_df(
                f"SELECT ts_code, track FROM daily_picks "
                f"WHERE ts_code IN ({ph}) "
                f"AND trade_date = (SELECT MAX(trade_date) FROM daily_picks)",
                tuple(codes)
            )
            if not tk_df.empty:
                track_map = dict(zip(tk_df['ts_code'].astype(str), tk_df['track'].astype(str)))
        except Exception:
            pass

        records = []
        for _, row in df.iterrows():
            buy_date = str(row.get('buy_date', '') or '')
            pnl_pct  = float(row.get('profit_loss_pct') or 0)
            ts_code  = str(row['ts_code'])
            track    = track_map.get(ts_code, '')
            min_hold = self.MIN_HOLD_B if track in self._B_TRACKS else self.MIN_HOLD_A
            records.append(PositionRecord(
                ts_code         = ts_code,
                name            = str(row.get('name', ts_code)),
                avg_cost        = float(row.get('avg_cost') or 0),
                current_price   = float(row.get('current_price') or 0),
                shares          = float(row.get('shares') or 0),
                profit_loss_pct = pnl_pct,
                stop_loss_price = float(row.get('stop_loss_price') or 0),
                buy_date        = buy_date,
                days_held       = days_map.get(buy_date, 0),
                current_score   = 0.0,
                track           = track,
                min_hold        = min_hold,
            ))
        return records

    def _load_from_agent_sim(self, trade_date: str) -> 'pd.DataFrame':
        """从 agent_sim_positions 读取持仓，关联 stock_daily 最新价计算盈亏"""
        try:
            df = DBUtils.query_df(
                "SELECT ts_code, name, volume AS shares, cost AS avg_cost, buy_date "
                "FROM agent_sim_positions WHERE volume > 0"
            )
            if df.empty:
                return df

            # 获取最新收盘价（用 trade_date 对应日或最新日）
            latest_price_df = DBUtils.query_df(
                "SELECT ts_code, close AS current_price "
                "FROM stock_daily "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily WHERE trade_date <= ?)",
                params=(trade_date,)
            )
            if not latest_price_df.empty:
                df = df.merge(latest_price_df, on='ts_code', how='left')
            else:
                df['current_price'] = df['avg_cost']

            df['current_price'] = df['current_price'].fillna(df['avg_cost'])
            # 计算盈亏率；avg_cost=0 时默认 0
            df['profit_loss_pct'] = df.apply(
                lambda r: (r['current_price'] - r['avg_cost']) / r['avg_cost']
                if r['avg_cost'] > 0 else 0.0,
                axis=1
            )
            # 止损价 = 成本 × (1 - stop_loss_pct)
            df['stop_loss_price'] = df['avg_cost'] * (1 + self.stop_loss_pct)
            return df

        except Exception as e:
            logger.debug(f"[HoldingManager] agent_sim_positions 读取跳过: {e}")
            return pd.DataFrame()

    def _load_from_positions_table(self) -> 'pd.DataFrame':
        """兜底：从旧版 positions 表读取"""
        try:
            return DBUtils.query_df(
                "SELECT ts_code, name, shares, avg_cost, current_price, "
                "profit_loss_pct, stop_loss_price, buy_date "
                "FROM positions WHERE shares > 0"
            )
        except Exception as e:
            logger.warning(f"[HoldingManager] positions 表读取失败: {e}")
            return pd.DataFrame()

    def _calc_hold_days(self, buy_dates: List[str], trade_date: str) -> Dict[str, int]:
        """批量计算每个买入日期到 trade_date 之间的交易日数"""
        if not buy_dates:
            return {}
        min_date = min(d for d in buy_dates if d)
        if not min_date:
            return {}
        try:
            df = DBUtils.query_df(
                "SELECT DISTINCT trade_date FROM stock_daily "
                "WHERE trade_date > ? AND trade_date <= ? ORDER BY trade_date",
                params=(min_date, trade_date)
            )
            all_dates = sorted(df['trade_date'].tolist())
        except Exception:
            return {}

        result = {}
        for buy_date in set(buy_dates):
            if not buy_date:
                result[buy_date] = 0
            else:
                result[buy_date] = sum(1 for d in all_dates if d > buy_date)
        return result

    # ──────────────────────────────────────────────────────────
    # 止损止盈
    # ──────────────────────────────────────────────────────────

    def _check_stop_loss(self, positions: List[PositionRecord]) -> List[Dict]:
        """止损预警：亏损超阈值时标记，实际执行由 RiskController 负责（单一入口）"""
        result = []
        for p in positions:
            if p.profit_loss_pct <= self.stop_loss_pct:
                result.append({
                    'ts_code':   p.ts_code,
                    'name':      p.name,
                    'reason':    f"止损预警: 亏损{p.profit_loss_pct:.1%} ≤ {self.stop_loss_pct:.1%}",
                    'days_held': p.days_held,
                    'pnl_pct':   p.profit_loss_pct,
                    'action':    'stop_loss_alert',   # 仅告警，不执行——执行权在 RiskController
                })
                logger.warning(f"  [止损预警] {p.name}({p.ts_code}) "
                               f"亏损{p.profit_loss_pct:.1%} 持有{p.days_held}日"
                               f"  → 请 RiskController 执行平仓")
        return result

    def _check_take_profit(self, positions: List[PositionRecord]) -> List[Dict]:
        """止盈减仓：盈利超阈值减至半仓"""
        result = []
        for p in positions:
            if p.profit_loss_pct >= self.take_profit_pct:
                result.append({
                    'ts_code':   p.ts_code,
                    'name':      p.name,
                    'reason':    f"止盈减仓: 盈利{p.profit_loss_pct:.1%} ≥ {self.take_profit_pct:.1%}",
                    'days_held': p.days_held,
                    'pnl_pct':   p.profit_loss_pct,
                    'action':    'take_profit_partial',  # 减半仓，不全卖
                })
                logger.info(f"  止盈减仓: {p.name}({p.ts_code}) "
                            f"盈利{p.profit_loss_pct:.1%} 持有{p.days_held}日")
        return result

    # ──────────────────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_signals(df: pd.DataFrame) -> pd.DataFrame:
        """统一 HybridStrategy / StrategyCenter 的评分列名"""
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.copy()
        # 统一评分列为 score
        if 'score' not in df.columns:
            for alt in ['final_score', 'ai_score', 'composite_score']:
                if alt in df.columns:
                    df['score'] = pd.to_numeric(df[alt], errors='coerce').fillna(0)
                    break
            else:
                df['score'] = 0.0
        df['score'] = pd.to_numeric(df['score'], errors='coerce').fillna(0)
        # 按分降序，去重
        return (df.sort_values('score', ascending=False)
                  .drop_duplicates('ts_code')
                  .reset_index(drop=True))

    def _format_summary(self, d: HoldingDecision, trade_date: str) -> str:
        lines = [
            f"[HoldingManager] 换仓决策 {trade_date}",
            f"  强制止损: {len(d.forced_sell)} 只  "
            f"止盈减仓: {len(d.partial_sell)} 只  "
            f"换仓卖出: {len(d.sell_list)} 只",
            f"  买入: {len(d.buy_list)} 只  持有: {len(d.hold_list)} 只",
        ]
        if d.forced_sell:
            lines.append("  ⚠️ 止损: " + ", ".join(
                f"{r['name']}({r['pnl_pct']:.1%})" for r in d.forced_sell
            ))
        if d.sell_list:
            lines.append("  📤 换出: " + ", ".join(
                f"{r['name']}" for r in d.sell_list[:5]
            ))
        if d.buy_list:
            lines.append("  📥 换入: " + ", ".join(
                f"{r['name']}" for r in d.buy_list[:5]
            ))
        protected = [r for r in d.hold_list if r.get('protected')]
        if protected:
            # 区分 A/B轨 保护期
            a_prot = [r for r in protected if r.get('track', '') not in self._B_TRACKS]
            b_prot = [r for r in protected if r.get('track', '') in self._B_TRACKS]
            if a_prot:
                lines.append(f"  🔒 A轨保护期（≤{self.MIN_HOLD_A}日）: "
                             + ", ".join(r['name'] for r in a_prot[:5]))
            if b_prot:
                lines.append(f"  🔒 B轨保护期（≤{self.MIN_HOLD_B}日）: "
                             + ", ".join(r['name'] for r in b_prot[:5]))
        return "\n".join(lines)

    def _log_decisions(self, decision: HoldingDecision, trade_date: str):
        """将决策写入 holding_log 表"""
        rows = []
        for r in decision.forced_sell:
            rows.append((trade_date, r['ts_code'], r.get('name',''), 'stop_loss',
                         r.get('reason',''), r.get('days_held',0), r.get('pnl_pct',0), 0, 0))
        for r in decision.partial_sell:
            rows.append((trade_date, r['ts_code'], r.get('name',''), 'take_profit',
                         r.get('reason',''), r.get('days_held',0), r.get('pnl_pct',0), 0, 0))
        for r in decision.sell_list:
            rows.append((trade_date, r['ts_code'], r.get('name',''), 'sell',
                         r.get('reason',''), r.get('days_held',0), r.get('pnl_pct',0),
                         r.get('score_old',0), 0))
        for r in decision.buy_list:
            rows.append((trade_date, r['ts_code'], r.get('name',''), 'buy',
                         r.get('reason',''), 0, 0, 0, r.get('score',0)))
        if not rows:
            return
        try:
            from src.utils.config_loader import Config
            is_mysql = Config.get('db_type', 'sqlite') == 'mysql'
            sql = (
                "INSERT INTO holding_log "
                "(trade_date,ts_code,name,action,reason,days_held,pnl_pct,score_old,score_new) "
                "VALUES (?,?,?,?,?,?,?,?,?)"
            )
            for row in rows:
                try:
                    DBUtils.execute(sql, row)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[HoldingManager] 写日志失败（忽略）: {e}")

    def _ensure_log_table(self):
        try:
            from src.utils.config_loader import Config
            is_mysql = Config.get('db_type', 'sqlite') == 'mysql'
            ddl = _CREATE_HOLDING_LOG if is_mysql else _CREATE_HOLDING_LOG_SQLITE
            DBUtils.execute(ddl)
        except Exception as e:
            logger.debug(f"[HoldingManager] 建表跳过: {e}")

    # ──────────────────────────────────────────────────────────
    # 便捷查询
    # ──────────────────────────────────────────────────────────

    def get_position_status(self, trade_date: str = None) -> pd.DataFrame:
        """返回当前持仓状态（含持有天数、评分、保护期标记），供 morning_push 使用"""
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')
        positions = self._load_positions(trade_date)
        if not positions:
            return pd.DataFrame()
        rows = []
        for p in positions:
            protected = p.days_held < p.min_hold
            rows.append({
                'ts_code':    p.ts_code,
                'name':       p.name,
                'avg_cost':   p.avg_cost,
                'current_price': p.current_price,
                'pnl_pct':    p.profit_loss_pct,
                'days_held':  p.days_held,
                'track':      p.track,
                'min_hold':   p.min_hold,
                'protected':  protected,
                'stop_loss':  p.stop_loss_price,
                'buy_date':   p.buy_date,
            })
        return pd.DataFrame(rows)
