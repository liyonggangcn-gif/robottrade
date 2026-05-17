"""
历史回溯分析引擎 (HistoricalBacktester)

支持:
- 1/3/5/10 年完整历史回溯
- Walk-Forward 滚动窗口分析
- 因子归因 (AI/Event/Fundamental/Sector Momentum 各因子贡献度)
- 基准对比 (沪深300/中证500)
- 结果自动入库 backtest_runs 表
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Literal
from loguru import logger
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config

import hashlib


@dataclass
class BacktestMetrics:
    total_return: float = 0.0
    annualized_return: float = 0.0
    benchmark_return: float = 0.0
    alpha: float = 0.0
    max_drawdown: float = 0.0
    volatility: float = 0.0
    sharpe_ratio: float = 0.0
    calmar_ratio: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    avg_return_per_trade: float = 0.0
    factor_attribution: Dict[str, float] = field(default_factory=dict)
    track_stats: Dict[str, Dict] = field(default_factory=dict)


class HistoricalBacktester:
    TRADE_DAYS_PER_YEAR = 252

    BENCHMARK_MAP = {
        'hs300': '000300',
        'zz500': '000905',
        'zz1000': '000985',
    }

    def __init__(self, db_path: str = None):
        self.db_path = db_path or Config.get('database', {}).get('path', 'data/quant.db')

    def run(
        self,
        years: int = 1,
        mode: Literal['full', 'walk_forward'] = 'full',
        top_k: int = 10,
        hold_days: int = 5,
        benchmark: str = 'hs300',
        track: str = None,
        name: str = None,
        walk_forward_window: int = 60,
        walk_forward_step: int = 20,
    ) -> Dict[str, Any]:
        """
        运行历史回溯

        Args:
            years: 回溯时长 (1/3/5/10)
            mode: 'full'=完整回溯, 'walk_forward'=滚动窗口
            top_k: 每次选股数量
            hold_days: 持仓天数
            benchmark: 'hs300'/'zz500'/'zz1000'
            track: 'sector_rotation'/'dividend'/None(全部)
            name: 回测名称 (用于记录)
            walk_forward_window: Walk-Forward 训练窗口 (交易日)
            walk_forward_step: Walk-Forward 步长 (交易日)
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=365 * years)

        start_str = start_date.strftime('%Y%m%d')
        end_str = end_date.strftime('%Y%m%d')

        benchmark_code = self.BENCHMARK_MAP.get(benchmark, '000300')

        print(f"\n{'='*60}")
        print(f"  历史回溯引擎 — {years}年{' Walk-Forward' if mode=='walk_forward' else ''}")
        print(f"{'='*60}")
        print(f"  时间: {start_date.strftime('%Y-%m-%d')} → {end_date.strftime('%Y-%m-%d')}")
        print(f"  参数: top_k={top_k}, hold_days={hold_days}, benchmark={benchmark}")
        print(f"  模式: {mode}")

        trade_dates = self._get_trade_dates(start_str, end_str)
        if len(trade_dates) < 20:
            logger.error(f"交易日不足: 仅 {len(trade_dates)} 天")
            return {}

        print(f"  交易日: {len(trade_dates)} 天")

        if mode == 'walk_forward':
            result = self._run_walk_forward(
                trade_dates, top_k, hold_days, benchmark_code,
                track, walk_forward_window, walk_forward_step, name or f"WF_{years}y",
            )
        else:
            result = self._run_full(
                trade_dates, top_k, hold_days, benchmark_code, track, name or f"FULL_{years}y",
            )

        result['config'] = {
            'years': years, 'mode': mode, 'top_k': top_k,
            'hold_days': hold_days, 'benchmark': benchmark,
            'track': track,
            'start_date': start_str, 'end_date': end_str,
        }
        return result

    def _run_full(
        self, trade_dates: List[str], top_k: int, hold_days: int,
        benchmark_code: str, track: str, name: str,
    ) -> Dict[str, Any]:
        """完整历史回溯（无未来数据泄露）"""
        rets = []
        benchmark_rets = []
        factor_contrib = {'ai': [], 'event': [], 'fundamental': [], 'sector_momentum': [], 'layer_heat': []}

        pick_dates = trade_dates[::hold_days]
        n_dates = len(pick_dates)

        for i, td in enumerate(pick_dates):
            picks = self._get_picks_for_date(td, top_k, track)
            if picks is None or picks.empty:
                continue

            for _, p in picks.iterrows():
                for w in [1, 5, 10, 20]:
                    ret = self._calc_return(p['ts_code'], td, w)
                    if ret is not None:
                        rets.append({
                            'trade_date': td,
                            'ts_code': p['ts_code'],
                            'hold_days': w,
                            'return': ret,
                            'ai_score': p.get('ai_score', 0),
                            'event_score': p.get('event_score', 0),
                            'fundamental_score': p.get('fundamental_score', 0),
                            'sector_momentum_score': p.get('sector_momentum_score', 0),
                            'layer_heat_score': p.get('layer_heat_score', 0),
                            'final_score': p.get('final_score', 0),
                            'track': p.get('track', ''),
                        })

            bm_ret = self._get_benchmark_return(benchmark_code, td, hold_days)
            if bm_ret is not None:
                benchmark_rets.append({'trade_date': td, 'return': bm_ret})

            if (i + 1) % 20 == 0:
                print(f"  进度 {i+1}/{n_dates} ({100*(i+1)//n_dates}%)")

        if not rets:
            logger.warning("无有效收益数据")
            return {}

        df = pd.DataFrame(rets)
        bm_df = pd.DataFrame(benchmark_rets) if benchmark_rets else pd.DataFrame()

        metrics = self._calc_metrics(df, bm_df, hold_days)
        metrics.track_stats = self._calc_track_stats(df)

        self._save_run(name, df['trade_date'].min(), df['trade_date'].max(), {
            'top_k': top_k, 'hold_days': hold_days,
        }, metrics)

        self._print_metrics(metrics, name)
        return {'metrics': asdict(metrics), 'trades_df': df, 'benchmark_df': bm_df}

    def _run_walk_forward(
        self, trade_dates: List[str], top_k: int, hold_days: int,
        benchmark_code: str, track: str, window: int, step: int, name: str,
    ) -> Dict[str, Any]:
        """Walk-Forward 滚动窗口回溯"""
        all_rets = []
        window_results = []

        for start_idx in range(0, len(trade_dates) - window, step):
            train_dates = trade_dates[start_idx:start_idx + window]
            test_dates = trade_dates[start_idx + window: start_idx + window + step]

            if len(test_dates) == 0:
                continue

            td_test = test_dates[0]
            td_train_end = train_dates[-1]

            if td_test <= td_train_end:
                continue

            picks = self._get_picks_for_date(td_test, top_k, track)
            if picks is None or picks.empty:
                continue

            for _, p in picks.iterrows():
                for w in [1, 5]:
                    ret = self._calc_return(p['ts_code'], td_test, w)
                    if ret is not None:
                        all_rets.append({
                            'window_start': train_dates[0],
                            'window_end': td_train_end,
                            'test_date': td_test,
                            'ts_code': p['ts_code'],
                            'hold_days': w,
                            'return': ret,
                            'track': p.get('track', ''),
                        })

            w_returns = [r['return'] for r in all_rets if r['test_date'] == td_test]
            if w_returns:
                window_results.append({
                    'test_date': td_test,
                    'avg_return': np.mean(w_returns),
                    'win_rate': np.mean([r > 0 for r in w_returns]),
                })

            print(f"  WF [{train_dates[0]}→{td_train_end}] test={td_test} picks={len(picks)} avg={np.mean(w_returns):.2%}")

        if not all_rets:
            return {}

        df = pd.DataFrame(all_rets)
        metrics = self._calc_metrics(df, pd.DataFrame(), hold_days)
        metrics.track_stats = {}

        self._save_run(name, df['test_date'].min(), df['test_date'].max(), {
            'top_k': top_k, 'hold_days': hold_days,
            'walk_forward_window': window, 'walk_forward_step': step,
        }, metrics)

        self._print_metrics(metrics, f"{name} (WF)")
        return {
            'metrics': asdict(metrics),
            'trades_df': df,
            'window_results': window_results,
        }

    def _get_picks_for_date(
        self, trade_date: str, top_k: int, track: str = None,
    ) -> Optional[pd.DataFrame]:
        """从 daily_picks 表获取指定日期的选股结果"""
        date_fmt = trade_date.replace('-', '')
        track_clause = f"AND track = '{track}'" if track else ""
        query = f"""
            SELECT ts_code, name, final_score, ai_score, event_score,
                   fund_score AS fundamental_score, track,
                   sector_momentum_score, layer_heat_score
            FROM daily_picks
            WHERE trade_date = '{date_fmt}' {track_clause}
            ORDER BY final_score DESC
            LIMIT {top_k}
        """
        df = DBUtils.query_df(query)
        if df.empty:
            return None

        df = df.rename(columns={
            'fundamental_score': 'fundamental_score',
            'ai_score': 'ai_score',
            'event_score': 'event_score',
            'sector_momentum_score': 'sector_momentum_score',
            'layer_heat_score': 'layer_heat_score',
            'final_score': 'final_score',
        })
        if 'layer_heat_score' not in df.columns:
            df['layer_heat_score'] = 0.0
        if 'sector_momentum_score' not in df.columns:
            df['sector_momentum_score'] = 0.0
        if 'fundamental_score' not in df.columns:
            df['fundamental_score'] = 0.0

        return df

    def _calc_return(self, ts_code: str, start_date: str, hold_days: int) -> Optional[float]:
        """计算从 start_date 起持有 N 日的收益率"""
        start_fmt = start_date.replace('-', '')

        price_df = DBUtils.query_df("""
            SELECT trade_date, close FROM stock_daily
            WHERE ts_code = ? AND trade_date >= ?
            ORDER BY trade_date
            LIMIT ?
        """, (ts_code, start_fmt, hold_days + 2))

        if price_df is None or price_df.empty or len(price_df) < 2:
            return None

        entry = float(price_df.iloc[0]['close'])
        exit_p = float(price_df.iloc[min(hold_days, len(price_df) - 1)]['close'])

        if entry <= 0:
            return None

        return (exit_p - entry) / entry

    def _get_benchmark_return(self, benchmark_code: str, start_date: str, hold_days: int) -> Optional[float]:
        """获取基准指数持有 N 日收益率"""
        code_map = {'000300': 'sh000300', '000905': 'sh000905', '000985': 'sh000985'}
        bm_sym = code_map.get(benchmark_code, 'sh000300')

        try:
            import akshare as ak
            start_fmt = start_date.replace('-', '')
            end_dt = datetime.strptime(start_fmt, '%Y%m%d') + timedelta(days=hold_days * 2)
            end_fmt = end_dt.strftime('%Y%m%d')

            df = ak.index_zh_a_hist(symbol=bm_sym, start_date=start_fmt, end_date=end_fmt, period='daily')
            if df is None or df.empty or len(df) < 2:
                return None

            entry = float(df.iloc[0]['收盘'])
            exit_p = float(df.iloc[min(hold_days, len(df) - 1)]['收盘'])

            if entry <= 0:
                return None

            return (exit_p - entry) / entry
        except Exception:
            return None

    def _calc_metrics(self, df: pd.DataFrame, bm_df: pd.DataFrame, hold_days: int) -> BacktestMetrics:
        """计算回测绩效指标"""
        rets = df['return'].dropna().values
        if len(rets) == 0:
            return BacktestMetrics()

        total_ret = np.prod(1 + rets) - 1

        n_days_approx = len(rets) * hold_days
        n_years = n_days_approx / self.TRADE_DAYS_PER_YEAR
        ann_ret = (1 + total_ret) ** (1 / max(n_years, 0.01)) - 1

        cum = np.cumprod(1 + rets)
        peak = np.maximum.accumulate(cum)
        drawdown = (cum - peak) / peak
        max_dd = drawdown.min()

        vol = np.std(rets) * np.sqrt(self.TRADE_DAYS_PER_YEAR)
        sharpe = (ann_ret / vol) if vol > 0 else 0
        calmar = (ann_ret / abs(max_dd)) if max_dd != 0 else 0

        bm_ret = 0.0
        alpha = ann_ret
        if not bm_df.empty:
            bm_rets = bm_df['return'].dropna().values
            if len(bm_rets) > 0:
                bm_ret = np.prod(1 + bm_rets) - 1
                alpha = ann_ret - bm_ret

        win_rate = np.mean(rets > 0)
        avg_ret = np.mean(rets)

        factor_attr = self._calc_factor_attribution(df)

        metrics = BacktestMetrics(
            total_return=total_ret,
            annualized_return=ann_ret,
            benchmark_return=bm_ret,
            alpha=alpha,
            max_drawdown=max_dd,
            volatility=vol,
            sharpe_ratio=sharpe,
            calmar_ratio=calmar,
            total_trades=len(rets),
            win_rate=win_rate,
            avg_return_per_trade=avg_ret,
            factor_attribution=factor_attr,
        )
        return metrics

    def _calc_factor_attribution(self, df: pd.DataFrame) -> Dict[str, float]:
        """计算各因子对收益的贡献度（基于加权相关性）"""
        factors = ['ai_score', 'event_score', 'fundamental_score',
                   'sector_momentum_score', 'layer_heat_score']
        weights = {'ai_score': 0.35, 'event_score': 0.20,
                   'fundamental_score': 0.10, 'sector_momentum_score': 0.20,
                   'layer_heat_score': 0.15}

        contributions = {}
        total_weight = sum(weights.get(f, 0) for f in factors)

        for factor in factors:
            if factor in df.columns:
                scores = df[factor].fillna(0).values
                rets = df['return'].fillna(0).values

                valid = ~np.isnan(scores) & ~np.isnan(rets)
                if valid.sum() > 10:
                    corr = np.corrcoef(scores[valid], rets[valid])[0, 1]
                    if np.isnan(corr):
                        corr = 0
                    w = weights.get(factor, 0) / max(total_weight, 0.01)
                    contributions[factor] = round(float(corr * w * 100), 4)
                else:
                    contributions[factor] = 0.0
            else:
                contributions[factor] = 0.0

        return contributions

    def _calc_track_stats(self, df: pd.DataFrame) -> Dict[str, Dict]:
        """按轨道分组统计"""
        stats = {}
        for track_val in df['track'].unique():
            sub = df[df['track'] == track_val]
            rets = sub['return'].dropna()
            if len(rets) > 0:
                stats[str(track_val)] = {
                    'count': int(len(rets)),
                    'avg_return': round(float(np.mean(rets)), 4),
                    'win_rate': round(float(np.mean(rets > 0)), 4),
                    'sharpe': round(float(np.mean(rets) / max(np.std(rets), 0.0001) * np.sqrt(252)), 3),
                }
        return stats

    def _get_trade_dates(self, start: str, end: str) -> List[str]:
        """获取交易日列表"""
        df = DBUtils.query_df(f"""
            SELECT DISTINCT trade_date FROM stock_daily
            WHERE trade_date >= '{start}' AND trade_date <= '{end}'
            ORDER BY trade_date
        """)
        if df is None or df.empty:
            return []
        return [str(d) for d in df['trade_date'].tolist()]

    def _save_run(
        self, name: str, start_date: str, end_date: str,
        params: Dict, metrics: BacktestMetrics,
    ):
        """保存回测结果到 backtest_runs 表"""
        import json
        run_id = hashlib.md5(f"{name}{start_date}{end_date}{datetime.now().isoformat()}".encode()).hexdigest()[:16]

        try:
            DBUtils.execute("""
                REPLACE INTO backtest_runs
                (run_id, name, start_date, end_date, params,
                 total_return, annualized_return, sharpe_ratio, max_drawdown,
                 win_rate, total_trades)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id,
                name,
                str(start_date),
                str(end_date),
                json.dumps(params, ensure_ascii=False),
                metrics.total_return,
                metrics.annualized_return,
                metrics.sharpe_ratio,
                metrics.max_drawdown,
                metrics.win_rate,
                metrics.total_trades,
            ))
            logger.info(f"[HistoricalBacktester] 保存回测记录: {run_id} ({name})")
        except Exception as e:
            logger.warning(f"[HistoricalBacktester] 保存回测失败: {e}")

    def _print_metrics(self, metrics: BacktestMetrics, name: str):
        """打印绩效指标"""
        print(f"\n{'='*60}")
        print(f"  回测结果: {name}")
        print(f"{'='*60}")
        print(f"  累计收益:   {metrics.total_return:>+.2%}")
        print(f"  年化收益:   {metrics.annualized_return:>+.2%}")
        print(f"  基准收益:   {metrics.benchmark_return:>+.2%}")
        print(f"  Alpha:     {metrics.alpha:>+.2%}")
        print(f"  最大回撤:   {metrics.max_drawdown:>+.2%}")
        print(f"  年化波动:   {metrics.volatility:>+.2%}")
        print(f"  夏普比率:   {metrics.sharpe_ratio:>.2f}")
        print(f"  卡玛比率:   {metrics.calmar_ratio:>.2f}")
        print(f"  交易次数:   {metrics.total_trades}")
        print(f"  胜率:       {metrics.win_rate:>+.2%}")
        print(f"  均收益/笔:  {metrics.avg_return_per_trade:>+.2%}")

        if metrics.factor_attribution:
            print(f"\n  因子归因:")
            for f, v in metrics.factor_attribution.items():
                bar = '█' * max(1, int(abs(v) * 5))
                sign = '+' if v >= 0 else ''
                print(f"    {f:<25} {sign}{v:.2f}%  {bar}")

        if metrics.track_stats:
            print(f"\n  轨道统计:")
            for t, s in metrics.track_stats.items():
                print(f"    {t:<20} n={s['count']:>4}  均收益={s['avg_return']:>+.2%}  胜率={s['win_rate']:>+.2%}  夏普={s['sharpe']:>.2f}")

        print(f"{'='*60}")

    def get_backtest_history(self, limit: int = 20) -> pd.DataFrame:
        """获取历史回测记录"""
        return DBUtils.query_df(f"""
            SELECT run_id, name, start_date, end_date, params,
                   total_return, annualized_return, sharpe_ratio, max_drawdown,
                   win_rate, total_trades, created_at
            FROM backtest_runs
            ORDER BY created_at DESC
            LIMIT {limit}
        """)

    def compare_strategies(self, years: int = 1) -> pd.DataFrame:
        """对比不同回溯期内的策略表现"""
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=365 * years)).strftime('%Y%m%d')

        return DBUtils.query_df(f"""
            SELECT name, total_return, annualized_return, sharpe_ratio,
                   max_drawdown, win_rate, total_trades
            FROM backtest_runs
            WHERE start_date >= '{start_date}' AND end_date <= '{end_date}'
            ORDER BY sharpe_ratio DESC
        """)


if __name__ == '__main__':
    bt = HistoricalBacktester()
    result = bt.run(years=1, mode='full', top_k=10, hold_days=5, benchmark='hs300')
    print(f"\n[OK] 回溯完成: {result.get('metrics', {}).get('total_return', 0):.2%}")
