"""
Event Driver: Concept-based event scoring for stocks.

Uses the stock_concepts table (synced via data_loader.sync_concepts)
to identify stocks belonging to "hot topics" and assign event scores.

评分规则（加权多命中）:
  - 基础分: min(命中热点数 / 3, 1.0)，命中越多得分越高
  - 权重加成: settings.yaml hot_topic_weights 中可配置每个热点的权重倍数
  - 最终 event_score 归一化到 [0, 1]
"""

import pandas as pd
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


# Default hot topics if none configured
DEFAULT_HOT_TOPICS = [
    '低空经济',
    '黄金',
    '人工智能',
    '机器人',
    '芯片',
    '石油',
    '电力设备',
    '军工',
]


class EventDriver:
    """Event-driven factor engine based on concept/theme matching.

    Identifies stocks that belong to currently "hot" market themes
    and assigns them an event_score of 1.0 (others get 0.0).

    Usage:
        driver = EventDriver(hot_topics=['人工智能', '芯片'])
        scores = driver.get_event_scores(['600519.SH', '000001.SZ', ...])
        hot_df = driver.get_hot_stocks()
    """

    def __init__(self, hot_topics=None, extra_topics=None):
        """Initialize EventDriver.

        Args:
            hot_topics: list of str, concept names to track.
                        If None, reads from Config('hot_topics')
                        or falls back to DEFAULT_HOT_TOPICS.
            extra_topics: list of str, additional topics to append
                          (e.g., dynamically detected from news analysis).
        """
        if hot_topics is not None:
            self.hot_topics = list(hot_topics)
        else:
            # 优先用动态识别传入的列表；兜底读配置文件的 fallback 列表
            configured = Config.get('hot_topics_fallback') or Config.get('hot_topics')
            if configured and isinstance(configured, list):
                self.hot_topics = configured
            else:
                self.hot_topics = DEFAULT_HOT_TOPICS

        # 追加动态热点（新闻分析检测到的板块）
        if extra_topics:
            for t in extra_topics:
                if t not in self.hot_topics:
                    self.hot_topics.append(t)

        # 读取权重配置（topic -> weight_multiplier）
        self.topic_weights = Config.get('hot_topic_weights') or {}

        print(f"[EventDriver] 初始化完成，热点主题 {len(self.hot_topics)} 个:")
        for topic in self.hot_topics:
            w = self.topic_weights.get(topic, 1.0)
            print(f"  - {topic} (权重 {w}x)")

    def _check_concepts_table(self):
        """Check if stock_concepts table has data.

        Returns:
            int: number of rows in stock_concepts, or 0 if empty/missing
        """
        try:
            result = DBUtils.query_df("SELECT COUNT(*) as cnt FROM stock_concepts")
            count = int(result.iloc[0]['cnt']) if not result.empty else 0
            return count
        except Exception:
            return 0

    def _query_hot_concept_stocks(self):
        """Query stocks that match any hot topic from stock_concepts table.

        Returns:
            pd.DataFrame with columns [ts_code, concept_name],
            or empty DataFrame if no matches.
        """
        concept_count = self._check_concepts_table()
        if concept_count == 0:
            print("[EventDriver] WARNING: stock_concepts table is empty.")
            print("              Run data_loader.sync_concepts() to populate it.")
            return pd.DataFrame(columns=['ts_code', 'concept_name'])

        # Build parameterized query for hot topics
        if not self.hot_topics:
            return pd.DataFrame(columns=['ts_code', 'concept_name'])

        # Use LIKE matching for flexibility (partial concept name matching)
        # e.g., hot topic "特斯拉" matches concept "特斯拉概念"
        conditions = []
        params = []
        for topic in self.hot_topics:
            conditions.append("concept_name LIKE ?")
            params.append(f"%{topic}%")

        where_clause = " OR ".join(conditions)
        sql = f"""
            SELECT DISTINCT ts_code, concept_name
            FROM stock_concepts
            WHERE {where_clause}
        """

        try:
            df = DBUtils.query_df(sql, params=params)
            return df
        except Exception as e:
            print(f"[EventDriver] Error querying concepts: {e}")
            return pd.DataFrame(columns=['ts_code', 'concept_name'])

    def get_event_scores(self, stock_list):
        """计算股票事件评分（加权多命中模式）。

        评分规则:
          - 每命中一个热点，累加该热点的权重值（默认1.0）
          - 累加权重除以最大可能权重（所有热点权重总和的一半），归一化到 [0, 1]
          - 命中越多、权重越高的热点，得分越高

        Args:
            stock_list: list of str, ts_codes to score
                        (e.g., ['600519.SH', '000001.SZ'])

        Returns:
            pd.Series indexed by ts_code with float event_score values [0, 1]
        """
        if not stock_list:
            return pd.Series(dtype=float)

        df_hot = self._query_hot_concept_stocks()

        if df_hot.empty:
            print("[EventDriver] 未找到热点概念股，事件评分全为0")
            return pd.Series(0.0, index=stock_list)

        # 为每条匹配记录计算其热点权重
        def _match_weight(concept_name):
            """从 concept_name 中反查匹配的热点，返回权重"""
            for topic in self.hot_topics:
                if topic in concept_name:
                    return self.topic_weights.get(topic, 1.0)
            return 1.0

        df_hot = df_hot.copy()
        df_hot['weight'] = df_hot['concept_name'].apply(_match_weight)

        # 按 ts_code 累加权重（多命中累加）
        weighted_sum = df_hot.groupby('ts_code')['weight'].sum()

        # 归一化: 用所有热点权重总和的50%作为满分基准
        # 这样命中一半高权重热点就能拿到高分，不需要命中全部
        total_topic_weight = sum(self.topic_weights.get(t, 1.0) for t in self.hot_topics)
        norm_base = max(total_topic_weight * 0.5, 1.0)

        scores = pd.Series(
            [min(weighted_sum.get(code, 0.0) / norm_base, 1.0) for code in stock_list],
            index=stock_list,
            name='event_score'
        )

        matched_count = int((scores > 0).sum())
        high_score_count = int((scores >= 0.5).sum())
        print(f"[EventDriver] {matched_count}/{len(stock_list)} 只股票命中热点 "
              f"(高分≥0.5的: {high_score_count} 只)")

        return scores

    def get_hot_stocks(self):
        """Get all stocks that match hot topics with their concept names.

        Returns:
            pd.DataFrame with columns [ts_code, concept_name, concepts_str]
            where concepts_str aggregates all matching concepts per stock.
        """
        df_hot = self._query_hot_concept_stocks()

        if df_hot.empty:
            return pd.DataFrame(columns=['ts_code', 'concept_name', 'concepts_str'])

        # Aggregate concepts per stock
        concepts_agg = df_hot.groupby('ts_code')['concept_name'].apply(
            lambda x: ', '.join(sorted(set(x)))
        ).reset_index()
        concepts_agg.columns = ['ts_code', 'concepts_str']

        # Merge back
        result = df_hot.merge(concepts_agg, on='ts_code', how='left')
        result = result.drop_duplicates(subset=['ts_code'])

        print(f"[EventDriver] Found {len(result)} unique hot-topic stocks")
        return result

    def get_stock_concepts(self, ts_code):
        """Get all concepts for a specific stock.

        Args:
            ts_code: str, e.g. '600519.SH'

        Returns:
            list of str, concept names
        """
        try:
            sql = "SELECT concept_name FROM stock_concepts WHERE ts_code = ?"
            df = DBUtils.query_df(sql, params=(ts_code,))
            if df.empty:
                return []
            return df['concept_name'].tolist()
        except Exception:
            return []

    def get_concepts_for_stocks(self, stock_list):
        """Get concept strings for multiple stocks (for display in dashboard).

        Args:
            stock_list: list of str, ts_codes

        Returns:
            pd.Series indexed by ts_code with comma-separated concept strings
        """
        if not stock_list:
            return pd.Series(dtype=str)

        df_hot = self._query_hot_concept_stocks()

        if df_hot.empty:
            return pd.Series("", index=stock_list, name='concepts')

        # Filter to only stocks in our list
        df_filtered = df_hot[df_hot['ts_code'].isin(stock_list)]

        if df_filtered.empty:
            return pd.Series("", index=stock_list, name='concepts')

        # Aggregate concept names per stock
        concepts_map = df_filtered.groupby('ts_code')['concept_name'].apply(
            lambda x: ', '.join(sorted(set(x)))
        )

        # Build result series with all stocks
        result = pd.Series("", index=stock_list, name='concepts')
        for code in stock_list:
            if code in concepts_map.index:
                result[code] = concepts_map[code]

        return result
