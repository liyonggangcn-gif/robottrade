"""
Self-Reflection - 选股失败自动反思机制
当持仓股票表现不佳时自动触发反思，保存到VectorMemory
"""
from datetime import datetime
from typing import List, Dict, Any
from loguru import logger

from src.utils.db_utils import DBUtils
from src.agent.vector_memory import VectorMemory


class SelfReflector:
    """自动反思机制"""

    # 触发条件
    LOSS_THRESHOLD = -0.05  # 亏损5%触发反思
    DAYS_TO_CHECK = 5  # 持有N天后检查

    def __init__(self):
        self.vm = VectorMemory()

    def analyze_low_scores(self, trade_date: str = None) -> List[Dict]:
        """分析低分选股"""
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y%m%d')

        # 查询近期低分选股（分数<0.5）
        df = DBUtils.query_df("""
            SELECT ts_code, name, final_score, track, trade_date
            FROM daily_picks
            WHERE final_score < 0.5 AND trade_date >= ?
            ORDER BY trade_date DESC
            LIMIT 10
        """, (trade_date,))

        if df.empty:
            return []

        # 对每只低分股票进行反思
        reflected = []
        for _, row in df.iterrows():
            ts_code = row['ts_code']
            score = row['final_score']
            
            reflection = f"""
选股分数过低分析: {ts_code} {row.get('name', '')}
日期: {row['trade_date']}
策略: {row['track']}
分数: {score}

反思要点：
1. 分数低的原因是什么？
2. 是否误选了垃圾股？
3. 需要调整哪些因子权重？
"""
            # 保存到VectorMemory
            self.vm.save(
                memory_type="loss_pattern",
                title=f"低分选股: {ts_code} 分数{score:.2f}",
                content=reflection.strip(),
                ts_code=ts_code,
                trade_date=str(row['trade_date']),
                importance=3
            )
            
            reflected.append({
                "ts_code": ts_code,
                "score": score,
                "reflection": reflection[:200]
            })

        return reflected

    def _generate_reflection(self, ts_code: str, pct_chg: float, row: Dict) -> str:
        """生成反思内容"""
        # 尝试获取更多上下文
        try:
            df_factors = DBUtils.query_df("""
                SELECT rsi_14, macd_hist, price_pos_52w, drawdown_20
                FROM stock_factors
                WHERE ts_code = %s
                ORDER BY trade_date DESC
                LIMIT 1
            """, (ts_code,))
            
            factors = ""
            if not df_factors.empty:
                f = df_factors.iloc[0]
                factors = f"  因子: RSI14={f.get('rsi_14')}, MACD={f.get('macd_hist')}, 位置={f.get('price_pos_52w')}"
        except:
            factors = ""

        reflection = f"""
股票: {ts_code} {row.get('name', '')}
买入: {row.get('buy_date')} @ {row.get('buy_price')}
卖出: {row.get('sell_date')} @ {row.get('sell_price')}
亏损: {pct_chg:.1f}%
{factors}

反思要点：
1. 买入时机是否正确？
2. 是否违反了选股策略？
3. 市场环境是否适合？
4. 需要改进的因子筛选？
"""
        return reflection.strip()

    def analyze_failed_pick(self, ts_code: str, reason: str = "") -> str:
        """分析单只失败选股"""
        # 查询该股票的历史选股记录
        df = DBUtils.query_df("""
            SELECT trade_date, track, final_score
            FROM daily_picks
            WHERE ts_code = %s
            ORDER BY trade_date DESC
            LIMIT 5
        """, (ts_code,))

        if df.empty:
            return ""

        # 生成反思
        lines = [f"选股失败分析: {ts_code}"]
        for _, r in df.iterrows():
            lines.append(f"  {r['trade_date']} {r['track']} 分数={r['final_score']}")

        if reason:
            lines.append(f"失败原因: {reason}")

        # 保存
        reflection = "\n".join(lines)
        self.vm.save(
            memory_type="loss_pattern",
            title=f"选股失败: {ts_code}",
            content=reflection,
            ts_code=ts_code,
            importance=4
        )

        return reflection


# 测试
if __name__ == "__main__":
    reflector = SelfReflector()
    
    print("=== Test Self-Reflection ===")
    results = reflector.analyze_low_scores()
    print(f"Reflected {len(results)} positions")
    for r in results:
        print(f"  {r['ts_code']}: score={r['score']:.2f}")