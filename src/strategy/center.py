"""
StrategyCenter: 策略中心统一调度器

职责：
  1. 宏观保护层  — 先调 MacroMonitor，按风险等级压缩 top_k
  2. 策略注册    — REGISTRY 统一管理所有策略类
  3. 多策略执行  — 顺序执行各策略，收集结果
  4. 结果融合    — 简单合并去重 OR ensemble加权合成
  5. 统一存储    — 写入 strategy_signals 表
  6. 统一推送    — 汇总后推送钉钉

用法：
    center = StrategyCenter()

    # 运行单个策略
    df = center.run_single('dividend', trade_date='2026-03-21', top_k=20)

    # 运行多个策略，结果合并去重
    df = center.run(['hybrid', 'dividend', 'quant'], top_k=20)

    # 加权融合模式
    df = center.run_ensemble(
        weights={'hybrid': 0.40, 'dividend': 0.30, 'quant': 0.30},
        top_k=20
    )
"""

from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from src.utils.macro_monitor import MacroMonitor, MacroState
from src.utils.notifier import send_alert


# ─────────────────────────────────────────────
# 策略注册表（懒加载，避免循环导入）
# ─────────────────────────────────────────────
def _get_registry() -> dict:
    """延迟导入策略类，防止模块级循环依赖"""
    registry = {}

    # 核心策略
    try:
        from src.strategy.hybrid_strategy import HybridStrategy
        registry['hybrid'] = HybridStrategy
    except ImportError as e:
        logger.warning(f"[StrategyCenter] hybrid 导入失败: {e}")

    try:
        from src.strategy.value_strategy import ValueStrategy
        registry['value'] = ValueStrategy
    except ImportError as e:
        logger.warning(f"[StrategyCenter] value 导入失败: {e}")

    try:
        from src.strategy.topk_strategy import TopKStrategy
        registry['topk'] = TopKStrategy
    except ImportError as e:
        logger.warning(f"[StrategyCenter] topk 导入失败: {e}")

    # 新策略（Phase 2-3 重构后）
    try:
        from src.strategy.dividend_strategy import DividendStrategy
        registry['dividend'] = DividendStrategy
    except ImportError:
        pass

    try:
        from src.strategy.quant_factor_strategy import QuantFactorStrategy
        registry['quant'] = QuantFactorStrategy
    except ImportError:
        pass

    try:
        from src.strategy.small_cap_strategy import SmallCapStrategy
        registry['small_cap'] = SmallCapStrategy
    except ImportError:
        pass

    # 小市值变体（纯市值排序 / Jinx行业择时）
    try:
        from src.strategy.small_cap_pure import PureSmallCapStrategy
        registry['small_cap_pure'] = PureSmallCapStrategy
    except ImportError:
        pass

    try:
        from src.strategy.small_cap_jinx import SmallCapJinxStrategy
        registry['small_cap_jinx'] = SmallCapJinxStrategy
    except ImportError:
        pass

    try:
        from src.strategy.cyclical_strategy import CyclicalStrategy
        registry['cyclical'] = CyclicalStrategy
    except ImportError:
        pass

    # 新增策略（Phase 4-6）
    try:
        from src.strategy.pb_roa_strategy import PbRoaStrategy
        registry['pb_roa'] = PbRoaStrategy
    except ImportError:
        pass

    try:
        from src.strategy.convertible_bond_strategy import ConvertibleBondStrategy
        registry['convertible_bond'] = ConvertibleBondStrategy
    except ImportError:
        pass

    try:
        from src.strategy.index_enhance_strategy import IndexEnhanceStrategy
        registry['index_enhance'] = IndexEnhanceStrategy
    except ImportError:
        pass

    # 新增动量策略
    try:
        from src.strategy.momentum_short import MomentumShortTermStrategy
        registry['momentum_short'] = MomentumShortTermStrategy
    except ImportError:
        pass

    try:
        from src.strategy.momentum_residual import MomentumResidualStrategy
        registry['momentum_residual'] = MomentumResidualStrategy
    except ImportError:
        pass

    # 新增成长策略
    try:
        from src.strategy.garp_growth import GarpsGrowthStrategy
        registry['garp_growth'] = GarpsGrowthStrategy
    except ImportError:
        pass

    # 新增盈余动量策略
    try:
        from src.strategy.earnings_momentum import EarningsMomentumStrategy
        registry['earnings_momentum'] = EarningsMomentumStrategy
    except ImportError:
        pass

    # 新增高ROE策略
    try:
        from src.strategy.high_roe import HighRoeStrategy
        registry['high_roe'] = HighRoeStrategy
    except ImportError:
        pass

    # 新增简化小市值策略
    try:
        from src.strategy.small_cap_simple import SimpleSmallCapStrategy
        registry['small_cap_simple'] = SimpleSmallCapStrategy
    except ImportError:
        pass

    # ETF动量轮动策略（聚宽模式）
    try:
        from src.strategy.etf_momentum_rotation import ETFMomentumRotation
        registry['etf_momentum_rotation'] = ETFMomentumRotation
    except ImportError:
        pass

    # 5层量化赛道策略
    try:
        from src.strategy.sector_5layer_strategy import Sector5LayerStrategy
        registry['sector_5layer'] = Sector5LayerStrategy
    except ImportError as e:
        logger.warning(f"[StrategyCenter] sector_5layer 导入失败: {e}")

    return registry


# 各策略中文名（推送时使用）
_STRATEGY_NAMES = {
    'hybrid':             'AI混合策略',
    'value':              '价值策略',
    'topk':               '技术多因子策略',
    'dividend':           '红利策略',
    'quant':              '量化多因子策略',
    'small_cap':          '质量小市值策略',
    'small_cap_pure':     '纯小市值策略',
    'small_cap_jinx':     '小市值Jinx择时',
    'cyclical':           '周期轮动策略',
    'pb_roa':             'PB-ROA价值策略',
    'convertible_bond':   '可转债策略',
    'index_enhance':      '指数增强策略',
    'momentum_short':     '中期动量策略',
    'momentum_residual':  '残差动量策略',
    'garp_growth':        'GARP成长策略',
    'earnings_momentum':  '盈余动量策略',
    'high_roe':           '高ROE策略',
    'small_cap_simple':   '简化小市值策略',
    'etf_momentum_rotation': 'ETF动量轮动策略',
    'sector_5layer':         '5层量化赛道策略',
}


class StrategyCenter:
    """策略中心：统一调度、融合输出、宏观保护"""

    # 宏观等级 → top_k 系数
    _MACRO_MULT = {
        'CRISIS': 0.20,
        'HIGH':   0.50,
        'MEDIUM': 0.80,
        'NORMAL': 1.00,
    }

    def __init__(self, enable_macro: bool = True, notify: bool = True):
        """
        Args:
            enable_macro: 是否启用宏观预警（测试时可关闭）
            notify:       是否推送钉钉
        """
        self.enable_macro = enable_macro
        self.notify_enabled = notify
        self._registry = None      # 懒加载
        self._macro = MacroMonitor() if enable_macro else None

    # ──────────────────────────────────────────
    # 主接口
    # ──────────────────────────────────────────

    def run(self,
            strategies: List[str] = None,
            trade_date: str = None,
            top_k: int = 20,
            ensemble: bool = False,
            ensemble_weights: Dict[str, float] = None) -> pd.DataFrame:
        """运行多策略并合并结果

        Args:
            strategies:        策略名称列表，None 表示运行所有已注册策略
            trade_date:        交易日期 YYYY-MM-DD，None 取数据库最新日
            top_k:             每策略选股上限（宏观调整前）
            ensemble:          True=加权融合输出，False=合并去重
            ensemble_weights:  各策略权重，None 则等权

        Returns:
            标准选股 DataFrame
        """
        trade_date = self._resolve_date(trade_date)
        registry = self._get_registry()

        if strategies is None:
            strategies = list(registry.keys())

        # 验证策略名称
        valid = [s for s in strategies if s in registry]
        invalid = [s for s in strategies if s not in registry]
        if invalid:
            logger.warning(f"[StrategyCenter] 未知策略（跳过）: {invalid}")
        if not valid:
            logger.error("[StrategyCenter] 无有效策略，退出")
            return pd.DataFrame()

        # Step 1: 宏观预警 → 调整 top_k
        macro_state, top_k_adj = self._macro_guard(top_k)
        logger.info(f"[StrategyCenter] 日期={trade_date} 策略={valid} "
                    f"top_k={top_k}→{top_k_adj} 宏观={macro_state.level}")

        # Step 2: 逐策略执行
        results: Dict[str, pd.DataFrame] = {}
        for name in valid:
            df = self._run_one(name, registry[name], trade_date, top_k_adj, memory_facts=None)
            if df is not None and not df.empty:
                results[name] = df

        if not results:
            logger.warning("[StrategyCenter] 所有策略均无结果")
            return pd.DataFrame()

        # Step 3: 融合
        if ensemble:
            merged = self._ensemble_merge(results, ensemble_weights, top_k_adj)
        else:
            merged = self._simple_merge(results, top_k_adj)

        # Step 4: 写入数据库
        self._save_all(results, macro_state.level)

        # Step 5: 推送汇总
        if self.notify_enabled:
            self._notify_summary(merged, valid, trade_date, macro_state)

        return merged

    def run_single(self, strategy_name: str,
                   trade_date: str = None,
                   top_k: int = 20,
                   skip_macro: bool = False) -> pd.DataFrame:
        """运行单个策略（调试/回测用）

        Args:
            strategy_name: 策略标识，如 'dividend'
            trade_date:    交易日期
            top_k:         选股数
            skip_macro:    跳过宏观压缩（用于回测）

        Returns:
            标准选股 DataFrame
        """
        registry = self._get_registry()
        if strategy_name not in registry:
            available = list(registry.keys())
            raise ValueError(f"未知策略 '{strategy_name}'，可用：{available}")

        trade_date = self._resolve_date(trade_date)

        if skip_macro:
            top_k_adj = top_k
            macro_level = 'NORMAL'
        else:
            macro_state, top_k_adj = self._macro_guard(top_k)
            macro_level = macro_state.level

        df = self._run_one(strategy_name, registry[strategy_name],
                           trade_date, top_k_adj)

        if df is not None and not df.empty:
            from src.strategy.base import BaseStrategy
            if issubclass(registry[strategy_name], BaseStrategy):
                inst = registry[strategy_name]()
                inst.save_signals(df, macro_level)

        return df if df is not None else pd.DataFrame()

    def run_ensemble(self,
                     weights: Dict[str, float] = None,
                     trade_date: str = None,
                     top_k: int = 20) -> pd.DataFrame:
        """加权融合模式（语法糖）

        Args:
            weights:    {'hybrid': 0.40, 'dividend': 0.30, 'quant': 0.30}
            trade_date: 交易日期
            top_k:      最终输出数量

        Returns:
            加权融合后 DataFrame
        """
        strategies = list(weights.keys()) if weights else None
        return self.run(
            strategies=strategies,
            trade_date=trade_date,
            top_k=top_k,
            ensemble=True,
            ensemble_weights=weights,
        )

    def available_strategies(self) -> List[str]:
        """返回当前已注册可用的策略名称列表"""
        return list(self._get_registry().keys())

    # ──────────────────────────────────────────
    # 内部：宏观保护
    # ──────────────────────────────────────────

    def _macro_guard(self, top_k: int):
        """调用 MacroMonitor，返回 (MacroState, 调整后top_k)"""
        if not self.enable_macro or self._macro is None:
            from src.utils.macro_monitor import MacroState
            return MacroState(level='NORMAL', multiplier=1.0), top_k

        try:
            state = self._macro.assess()
            top_k_adj = max(5, int(top_k * state.multiplier))
            if state.level != 'NORMAL':
                logger.warning(
                    f"[StrategyCenter] 宏观={state.level} "
                    f"top_k 压缩 {top_k}→{top_k_adj} "
                    f"触发={state.triggered}"
                )
            return state, top_k_adj
        except Exception as e:
            logger.warning(f"[StrategyCenter] MacroMonitor 异常({e})，使用 NORMAL")
            from src.utils.macro_monitor import MacroState
            return MacroState(level='NORMAL', multiplier=1.0), top_k

    # ──────────────────────────────────────────
    # 内部：单策略执行
    # ──────────────────────────────────────────

    def _run_one(self, name: str, cls, trade_date: str,
                 top_k: int, memory_facts: Optional[list] = None) -> Optional[pd.DataFrame]:
        """安全执行单个策略，异常时返回 None"""
        logger.info(f"[StrategyCenter] 执行策略: {name}")
        try:
            import inspect
            instance = cls()

            from src.strategy.base import BaseStrategy
            if not hasattr(instance, 'run'):
                logger.warning(f"[StrategyCenter] {name} 没有 run() 方法，跳过")
                return None

            sig = inspect.signature(instance.run)
            params = {'trade_date': trade_date}
            param_names = list(sig.parameters.keys())
            if 'top_k' in param_names:
                params['top_k'] = top_k
            elif 'top_n' in param_names:
                params['top_n'] = top_k
            elif 'top' in param_names:
                params['top'] = top_k
            if 'memory_facts' in param_names and memory_facts:
                params['memory_facts'] = memory_facts

            df = instance.run(**params)

            if df is None or df.empty:
                logger.warning(f"[StrategyCenter] {name} 返回空结果")
                return None

            # 统一补充 strategy 列
            df = df.copy()
            df['strategy'] = name

            # 统一补充 rank 列（若不存在）
            if 'rank' not in df.columns:
                if 'score' in df.columns:
                    df['rank'] = df['score'].rank(ascending=False,
                                                   method='first').astype(int)
                else:
                    df['rank'] = range(1, len(df) + 1)
            
            # 统一补充 score 列（若不存在，但有 final_score）
            if 'score' not in df.columns and 'final_score' in df.columns:
                df['score'] = df['final_score']

            logger.info(f"[StrategyCenter] {name} 完成，返回 {len(df)} 只")
            return df

        except Exception as e:
            logger.error(f"[StrategyCenter] {name} 执行异常: {e}", exc_info=True)
            return None

    # ──────────────────────────────────────────
    # 内部：结果融合
    # ──────────────────────────────────────────

    def _simple_merge(self, results: Dict[str, pd.DataFrame],
                      top_k: int) -> pd.DataFrame:
        """简单合并：各策略结果拼接，同一股票保留最高分

        Returns:
            按分数降序，取前 top_k 只
        """
        frames = []
        for name, df in results.items():
            sub = df.copy()
            # 统一列（兼容已有策略的不同列名）
            sub = self._normalize_columns(sub, name)
            frames.append(sub)

        if not frames:
            return pd.DataFrame()

        merged = pd.concat(frames, ignore_index=True)

        # 同一股票：保留评分最高的那条（来自不同策略时取最高分）
        if 'score' in merged.columns and 'ts_code' in merged.columns:
            merged = (merged
                      .sort_values('score', ascending=False)
                      .drop_duplicates(subset='ts_code', keep='first')
                      .head(top_k)
                      .reset_index(drop=True))
            merged['rank'] = range(1, len(merged) + 1)
        else:
            merged = merged.head(top_k).reset_index(drop=True)

        return merged

    def _ensemble_merge(self, results: Dict[str, pd.DataFrame],
                        weights: Optional[Dict[str, float]],
                        top_k: int) -> pd.DataFrame:
        """加权融合：对每只股票的跨策略分数加权平均

        Returns:
            按 ensemble_score 降序，取前 top_k 只
        """
        names = list(results.keys())

        # 等权（若未指定）
        if not weights:
            w = {n: 1.0 / len(names) for n in names}
        else:
            # 归一化
            total = sum(weights.get(n, 0) for n in names)
            w = {n: weights.get(n, 0) / total for n in names} if total > 0 \
                else {n: 1.0 / len(names) for n in names}

        # 构建 {ts_code: {strategy: score}} 映射
        score_map: Dict[str, Dict[str, float]] = {}
        meta_map:  Dict[str, dict] = {}   # ts_code → 基础信息（保留所有列）

        for name, df in results.items():
            sub = self._normalize_columns(df, name)
            for _, row in sub.iterrows():
                code = str(row.get('ts_code', ''))
                if not code:
                    continue
                if code not in score_map:
                    score_map[code] = {}
                    # 保留所有列，不只是基础列
                    row_dict = row.to_dict()
                    # 移除重复的score（会用ensemble_score替代）
                    row_dict.pop('score', None)
                    meta_map[code] = row_dict
                score_map[code][name] = float(row.get('score', 0.0))

        # 加权合成
        records = []
        for code, scores in score_map.items():
            ensemble_score = sum(
                scores.get(n, 0.0) * w.get(n, 0.0)
                for n in names
            )
            strategies_hit = [n for n in names if n in scores]
            rec = meta_map[code].copy()
            rec['score'] = ensemble_score
            rec['sub_scores'] = scores
            rec['strategy'] = '+'.join(strategies_hit)
            rec['signal_reason'] = f"命中策略: {', '.join(strategies_hit)}"
            records.append(rec)

        if not records:
            return pd.DataFrame()

        result = (pd.DataFrame(records)
                  .sort_values('score', ascending=False)
                  .head(top_k)
                  .reset_index(drop=True))
        result['rank'] = range(1, len(result) + 1)
        return result

    @staticmethod
    def _normalize_columns(df: pd.DataFrame, strategy_name: str) -> pd.DataFrame:
        """统一不同策略的列名差异"""
        df = df.copy()

        # score 列：兼容不同策略返回的不同列名
        if 'score' not in df.columns:
            for alt in ('combined_score', 'final_score', 'rank_score', 'value_score', 'dividend_score', 'quant_score'):
                if alt in df.columns:
                    df['score'] = df[alt]
                    break
            else:
                df['score'] = 0.5   # fallback

        # name 列：优先用 stock_name，否则用 ts_code
        if 'name' not in df.columns:
            for alt in ('stock_name', '名称'):
                if alt in df.columns:
                    df['name'] = df[alt]
                    break
            else:
                df['name'] = df['ts_code']  # fallback to ts_code

        # signal_reason 列
        if 'signal_reason' not in df.columns:
            df['signal_reason'] = strategy_name

        # sub_scores 列
        if 'sub_scores' not in df.columns:
            df['sub_scores'] = [{}] * len(df)

        # 补充 industry 列（从 concepts 或其他来源）
        if 'industry' not in df.columns and 'concepts' in df.columns:
            df['industry'] = df['concepts']
        
        # 保留更多常用列：ai_score, event_score, fundamental_score等
        # 这些列如果存在就保留，不做额外处理
        pass  # Columns already preserved from original DataFrame
            
        return df

    # ──────────────────────────────────────────
    # 内部：存储
    # ──────────────────────────────────────────

    def _save_all(self, results: Dict[str, pd.DataFrame],
                  macro_level: str):
        """各策略独立写入 strategy_signals"""
        from src.strategy.base import BaseStrategy
        registry = self._get_registry()

        for name, df in results.items():
            try:
                cls = registry.get(name)
                if cls and issubclass(cls, BaseStrategy):
                    inst = cls.__new__(cls)   # 不重新执行 __init__
                    inst.name = name
                    inst.save_signals(df, macro_level)
            except Exception as e:
                logger.warning(f"[StrategyCenter] {name} save_signals 失败: {e}")

    # ──────────────────────────────────────────
    # 内部：推送
    # ──────────────────────────────────────────

    def _notify_summary(self, merged: pd.DataFrame,
                        strategies: List[str],
                        trade_date: str,
                        macro_state: MacroState):
        """推送策略中心汇总"""
        if merged is None or merged.empty:
            return

        level_icon = {'CRISIS': '🔴', 'HIGH': '🟠',
                      'MEDIUM': '🟡', 'NORMAL': '🟢'}.get(macro_state.level, '')
        strategy_labels = [_STRATEGY_NAMES.get(s, s) for s in strategies]

        title = f"{level_icon}【策略中心】{trade_date} 综合选股"
        lines = [
            f"**运行策略**：{' | '.join(strategy_labels)}",
            f"**宏观状态**：{level_icon} {macro_state.level}  "
            f"**仓位系数**：{macro_state.multiplier:.0%}  "
            f"**入选**：{len(merged)} 只\n",
            "| 排名 | 代码 | 名称 | 评分 | 来源策略 |",
            "|------|------|------|------|----------|",
        ]
        for _, row in merged.head(20).iterrows():
            strategy_src = str(row.get('strategy', ''))
            # 简化策略名
            for k, v in _STRATEGY_NAMES.items():
                strategy_src = strategy_src.replace(k, v)
            lines.append(
                f"| {int(row.get('rank', 0))} "
                f"| {row.get('ts_code', '')} "
                f"| {row.get('name', '')} "
                f"| {float(row.get('score', 0)):.3f} "
                f"| {strategy_src[:20]} |"
            )

        send_alert(title, '\n'.join(lines), message_type='strategy_center')

    # ──────────────────────────────────────────
    # 内部：工具
    # ──────────────────────────────────────────

    def _get_registry(self) -> dict:
        if self._registry is None:
            self._registry = _get_registry()
        return self._registry

    @staticmethod
    def _resolve_date(trade_date: str = None) -> str:
        if trade_date:
            return trade_date
        try:
            from src.utils.db_utils import DBUtils
            df = DBUtils.query_df(
                "SELECT MAX(trade_date) AS dt FROM stock_daily"
            )
            return str(df.iloc[0]['dt'])
        except Exception:
            return datetime.now().strftime('%Y-%m-%d')
