"""
A/B 实验管理器
定义实验、选择执行 arm、记录结果、统计检验
"""
import json
import uuid
from datetime import datetime
from typing import Dict, List, Any, Optional

import pandas as pd
from loguru import logger

from src.utils.db_utils import DBUtils
from src.backtest.tables import ensure_backtest_tables


EXPERIMENT_TEMPLATES = {
    "futures_weight": {
        "name": "期货信号权重测试",
        "description": "期货BUY信号加成0.1 vs 0.15",
        "hypothesis": "提高期货信号权重是否能提升收益",
        "arms": [
            {"arm_id": "A", "name": "Control", "description": "期货加成0.10", "futures_bonus": 0.10},
            {"arm_id": "B", "name": "Treatment", "description": "期货加成0.15", "futures_bonus": 0.15},
        ]
    },
    "ai_weight_sweep": {
        "name": "AI权重扫描",
        "description": "AI权重50% vs 40% vs 60%",
        "hypothesis": "不同AI权重对收益的影响",
        "arms": [
            {"arm_id": "A", "name": "LowAI", "description": "AI权重40%", "ai_weight": 0.40},
            {"arm_id": "B", "name": "MidAI", "description": "AI权重50%", "ai_weight": 0.50},
            {"arm_id": "C", "name": "HighAI", "description": "AI权重60%", "ai_weight": 0.60},
        ]
    },
    "topk_sweep": {
        "name": "选股数量测试",
        "description": "top_k=10 vs 20 vs 30",
        "hypothesis": "不同选股数量对分散化和收益的影响",
        "arms": [
            {"arm_id": "A", "name": "Top10", "description": "选10只", "top_k": 10},
            {"arm_id": "B", "name": "Top20", "description": "选20只", "top_k": 20},
            {"arm_id": "C", "name": "Top30", "description": "选30只", "top_k": 30},
        ]
    },
    "institutional_bonus": {
        "name": "龙虎榜加成测试",
        "description": "龙虎榜净买入>500万 加成0.03 vs 0.05",
        "hypothesis": "提高龙虎榜信号权重",
        "arms": [
            {"arm_id": "A", "name": "Control", "description": "龙虎榜加成0.03", "institutional_bonus": 0.03},
            {"arm_id": "B", "name": "Treatment", "description": "龙虎榜加成0.05", "institutional_bonus": 0.05},
        ]
    },
}


class ABExperimentManager:
    """A/B 实验管理器"""

    def __init__(self):
        ensure_backtest_tables()

    def create_experiment(
        self,
        template_key: str,
        custom_params: Dict = None,
    ) -> str:
        """
        从模板创建实验
        """
        if template_key not in EXPERIMENT_TEMPLATES:
            raise ValueError(f"未知模板: {template_key}")

        tmpl = EXPERIMENT_TEMPLATES[template_key]
        exp_id = f"exp_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"

        DBUtils.execute("""
            INSERT INTO ab_experiments (experiment_id, name, description, hypothesis, status)
            VALUES (?, ?, ?, ?, 'running')
        """, (exp_id, tmpl['name'], tmpl['description'], tmpl['hypothesis']))

        for arm in tmpl['arms']:
            merged = {**arm}
            if custom_params:
                merged.update(custom_params)
            params_json = json.dumps(merged, ensure_ascii=False)

            DBUtils.execute("""
                INSERT INTO ab_arms
                (experiment_id, arm_id, name, description, params,
                 ai_weight, event_weight, fundamental_weight, sector_weight,
                 futures_bonus, institutional_bonus, news_bonus, northbound_bonus, top_k)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                exp_id, arm['arm_id'], arm['name'], arm['description'], params_json,
                merged.get('ai_weight'),
                merged.get('event_weight'),
                merged.get('fundamental_weight'),
                merged.get('sector_weight'),
                merged.get('futures_bonus'),
                merged.get('institutional_bonus'),
                merged.get('news_bonus'),
                merged.get('northbound_bonus'),
                merged.get('top_k'),
            ))

        logger.info(f"[AB] 创建实验 {exp_id}: {tmpl['name']}")
        return exp_id

    def get_running_experiments(self) -> List[Dict]:
        """获取所有运行中的实验"""
        df = DBUtils.query_df(
            "SELECT * FROM ab_experiments WHERE status = 'running' ORDER BY created_at DESC"
        )
        return df.to_dict('records') if not df.empty else []

    def get_experiment_arms(self, experiment_id: str) -> List[Dict]:
        """获取实验的所有 arm"""
        df = DBUtils.query_df(
            "SELECT * FROM ab_arms WHERE experiment_id = ? ORDER BY arm_id",
            (experiment_id,)
        )
        return df.to_dict('records') if not df.empty else []

    def get_arm_params(self, experiment_id: str, arm_id: str) -> Dict:
        """获取指定 arm 的参数"""
        df = DBUtils.query_df(
            "SELECT params FROM ab_arms WHERE experiment_id = ? AND arm_id = ?",
            (experiment_id, arm_id)
        )
        if df.empty:
            return {}
        params_str = df.iloc[0]['params']
        if params_str:
            return json.loads(params_str)
        return {}

    def record_daily_result(
        self,
        experiment_id: str,
        arm_id: str,
        trade_date: str,
        picks_count: int,
        ret_1d: float = None,
        ret_5d: float = None,
        ret_10d: float = None,
        ret_20d: float = None,
        avg_score: float = None,
        win_rate_5d: float = None,
    ):
        """记录实验某 arm 某日的表现"""
        DBUtils.execute("""
            INSERT OR REPLACE INTO ab_daily_results
            (experiment_id, arm_id, trade_date, picks_count, ret_1d, ret_5d, ret_10d, ret_20d, avg_score, win_rate_5d)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            experiment_id, arm_id,
            trade_date.replace('-', ''),
            picks_count,
            ret_1d, ret_5d, ret_10d, ret_20d,
            avg_score, win_rate_5d,
        ))

    def get_experiment_results(self, experiment_id: str, days: int = 30) -> pd.DataFrame:
        """获取实验结果"""
        df = DBUtils.query_df("""
            SELECT abr.*, aa.name as arm_name, aa.params
            FROM ab_daily_results abr
            JOIN ab_arms aa ON abr.experiment_id = aa.experiment_id AND abr.arm_id = aa.arm_id
            WHERE abr.experiment_id = ?
            ORDER BY abr.trade_date DESC, abr.arm_id
            LIMIT ?
        """, (experiment_id, days * 3))
        return df

    def select_arm(self, experiment_id: str) -> str:
        """
        根据历史表现选择今日执行哪个 arm
        策略：
        1. 有统计显著差异 → 选胜者
        2. 无显著差异 → 选累计收益高的
        3. 无数据 → 默认选 A
        """
        df = self.get_experiment_results(experiment_id, days=20)
        if df.empty:
            logger.info(f"[AB] {experiment_id} 无历史数据，默认 arm A")
            return "A"

        arms = df['arm_id'].unique()
        if len(arms) < 2:
            return "A"

        # 计算各 arm 的累计收益
        arm_perf = {}
        for arm in arms:
            arm_df = df[df['arm_id'] == arm].sort_values('trade_date')
            if arm_df.empty or 'ret_5d' not in arm_df.columns:
                continue
            rets = arm_df['ret_5d'].dropna()
            if len(rets) < 3:
                continue
            cum_ret = (1 + rets).prod() - 1
            win_rate = (rets > 0).mean()
            avg_ret = rets.mean()
            arm_perf[arm] = {
                'cum_ret': cum_ret,
                'win_rate': win_rate,
                'avg_ret': avg_ret,
                'count': len(rets),
            }

        if not arm_perf:
            return "A"

        # 简单选择：累计收益最高的 arm
        best_arm = max(arm_perf.keys(), key=lambda a: arm_perf[a]['cum_ret'])
        logger.info(f"[AB] {experiment_id} arm选择: {best_arm} (cum_ret={arm_perf[best_arm]['cum_ret']:.3f})")
        return best_arm

    def test_significance(self, experiment_id: str) -> Dict[str, Any]:
        """
        对实验进行统计显著性检验（t-test）
        """
        df = self.get_experiment_results(experiment_id, days=60)
        if df.empty:
            return {"significant": False, "reason": "数据不足"}

        arms = df['arm_id'].unique()
        if len(arms) < 2:
            return {"significant": False, "reason": "arm数不足"}

        # pairwise t-test A vs others
        arm_a = df[df['arm_id'] == arms[0]]['ret_5d'].dropna()
        results = {}

        for arm in arms[1:]:
            arm_b = df[df['arm_id'] == arm]['ret_5d'].dropna()
            if len(arm_a) < 5 or len(arm_b) < 5:
                continue

            try:
                from scipy.stats import ttest_ind
                t, p = ttest_ind(arm_a, arm_b)
                results[arm] = {
                    't_stat': float(t),
                    'p_value': float(p),
                    'significant': float(p) < 0.05,
                    'mean_a': float(arm_a.mean()),
                    'mean_b': float(arm_b.mean()),
                }
            except Exception as e:
                logger.debug(f"[AB] t-test 失败: {e}")

        return results

    def close_experiment(self, experiment_id: str, winner: str = None):
        """结束实验"""
        stats = self.test_significance(experiment_id)
        p_value = None
        t_stat = None
        if stats and 'A' in stats:
            p_value = stats['A'].get('p_value')
            t_stat = stats['A'].get('t_stat')
        DBUtils.execute("""
            UPDATE ab_experiments
            SET status = 'completed', winner = ?, p_value = ?, t_stat = ?,
                completed_at = ?
            WHERE experiment_id = ?
        """, (
            winner or stats.get('winner'),
            p_value, t_stat,
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            experiment_id
        ))
        logger.info(f"[AB] 实验 {experiment_id} 已结束，winner={winner}")

    def get_active_experiments_summary(self) -> List[Dict]:
        """获取运行中实验的摘要"""
        exps = self.get_running_experiments()
        summary = []
        for exp in exps:
            exp_id = exp['experiment_id']
            results = self.get_experiment_results(exp_id, days=30)
            arms = self.get_experiment_arms(exp_id)

            arm_summaries = []
            for arm in arms:
                arm_id = arm['arm_id']
                arm_data = results[results['arm_id'] == arm_id]
                rets = arm_data['ret_5d'].dropna() if not arm_data.empty else pd.Series([])
                cum_ret = float((1 + rets).prod() - 1) if len(rets) > 0 else 0.0
                arm_summaries.append({
                    'arm_id': arm_id,
                    'name': arm['name'],
                    'description': arm['description'],
                    'days': len(rets),
                    'cum_ret': round(cum_ret, 4),
                    'avg_ret': round(float(rets.mean()), 4) if len(rets) > 0 else 0.0,
                    'win_rate': round(float((rets > 0).mean()), 4) if len(rets) > 0 else 0.0,
                })

            summary.append({
                'experiment_id': exp_id,
                'name': exp['name'],
                'status': exp['status'],
                'created_at': exp['created_at'],
                'arms': arm_summaries,
            })

        return summary
