"""
HotTopicDetector: 动态热点板块自动识别

从两个信号源自动发现当前市场热点主题，无需手动维护列表：

  1. 市场动量信号 (权重 60%)
     - stock_concepts × stock_factors: 按概念板块聚合 mom_20 (20日动量)
     - stock_concepts × stock_daily:  按概念板块聚合近5日涨跌幅
     - 两者加权，排名靠前的概念即为市场热点

  2. 新闻LLM信号 (权重 40%)
     - MarketNewsAnalyzer 输出的 sector_impacts
     - "利好" + 强/中 强度的板块直接纳入

融合后返回排序好的热点关键词列表，传给 EventDriver。

用法:
    detector = HotTopicDetector()
    topics = detector.detect(news_analysis=news_result, top_k=20)
    # topics = ['人工智能', '石油', '电力设备', ...]
"""

import re
import pandas as pd
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


# 概念名称清洗：去除常见后缀，提取核心关键词
_SUFFIX_STRIP = re.compile(r'(概念股?|板块|指数|ETF|主题|龙头|概念指数)$')

# 过滤掉过于泛化/无意义的概念（这些无法指导选股方向）
_BLACKLIST_PATTERNS = [
    '沪深300', '中证500', '上证', '创业板', '科创板', '北交所',
    'ST', '退市', '次新股', '低价股', '高送转', '业绩预增',
    '融资融券', '股权激励', '回购', '增持', '分拆上市',
    '连续涨停', '涨价', '降价', '季报', '年报',
]


class HotTopicDetector:
    """动态热点检测器：从市场数据和新闻中自动发现热点板块"""

    # 信号权重
    W_MARKET = 0.60
    W_NEWS = 0.40

    # 市场动量子信号权重
    W_MOM20 = 0.55   # 20日动量（趋势）
    W_RET5 = 0.45    # 5日涨幅（短期热度）

    def __init__(self):
        # 读取配置：可选的种子关键词权重（仍可配置，但不再是唯一来源）
        self.seed_weights = Config.get('hot_topic_weights') or {}
        # 最少持有股票数：概念内股票太少不可靠
        self.min_stocks_per_concept = 5
        # 市场动量信号只取前多少个概念
        self.market_top_n = 40

    # ------------------------------------------------------------------
    # 主接口
    # ------------------------------------------------------------------

    def detect(self, news_analysis=None, top_k=20, fallback_topics=None):
        """检测当前市场热点主题

        Args:
            news_analysis: dict, MarketNewsAnalyzer.analyze() 的返回值（可选）
            top_k: 返回热点数量上限
            fallback_topics: list of str, 若市场数据不足时的兜底列表

        Returns:
            list of str: 热点关键词，按综合得分降序排列
            dict: 评分明细 {keyword: score, ...}
        """
        print("[HotTopicDetector] 开始动态热点识别...")

        # 1. 市场动量信号
        market_scores = self._detect_from_market()

        # 2. 新闻LLM信号
        news_scores = self._detect_from_news(news_analysis)

        # 3. 融合两个信号
        final_scores = self._merge_scores(market_scores, news_scores)

        if not final_scores:
            print("[HotTopicDetector] 未检测到有效热点，使用兜底列表")
            return fallback_topics or [], {}

        # 4. 排序，取 Top K
        ranked = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)
        top_topics = [kw for kw, _ in ranked[:top_k]]
        score_detail = dict(ranked[:top_k])

        print(f"[HotTopicDetector] 识别到 {len(top_topics)} 个热点主题:")
        for kw, score in ranked[:10]:
            src = []
            if kw in market_scores:
                src.append(f"市场{market_scores[kw]:.2f}")
            if kw in news_scores:
                src.append(f"新闻{news_scores[kw]:.2f}")
            print(f"  [{score:.3f}] {kw}  ({', '.join(src)})")

        return top_topics, score_detail

    # ------------------------------------------------------------------
    # 信号1: 市场动量（概念板块聚合）
    # ------------------------------------------------------------------

    def _detect_from_market(self):
        """从 stock_factors × stock_concepts 计算概念板块热度

        Returns:
            dict: {concept_keyword: normalized_score (0~1)}
        """
        try:
            # 1a. 按概念聚合 mom_20（20日动量）
            mom_scores = self._query_concept_momentum()

            # 1b. 按概念聚合近5日涨幅（换手率/短期热度）
            ret5_scores = self._query_concept_recent_return(days=5)

            if not mom_scores and not ret5_scores:
                print("[HotTopicDetector] 市场数据不足，跳过市场信号")
                return {}

            # 合并并加权
            all_concepts = set(mom_scores) | set(ret5_scores)
            raw = {}
            for c in all_concepts:
                m = mom_scores.get(c, 0.0)
                r = ret5_scores.get(c, 0.0)
                raw[c] = self.W_MOM20 * m + self.W_RET5 * r

            # 归一化到 [0, 1]
            normalized = self._normalize(raw)
            print(f"[HotTopicDetector] 市场信号: {len(normalized)} 个概念有效")
            return normalized

        except Exception as e:
            print(f"[HotTopicDetector] 市场信号计算失败: {e}")
            return {}

    def _query_concept_momentum(self):
        """查询各概念板块平均 mom_20
        优先从 stock_factors 读取；表不存在时从 stock_daily 实时计算。
        """
        # 优先：从 stock_factors 读取
        sql = """
        SELECT /*+ MAX_EXECUTION_TIME(30000) */
            sc.concept_name,
            AVG(sf.mom_20) AS avg_mom,
            COUNT(DISTINCT sc.ts_code) AS stock_count
        FROM stock_concepts sc
        INNER JOIN stock_factors sf ON sc.ts_code = sf.ts_code
        WHERE sf.trade_date = (SELECT MAX(trade_date) FROM stock_factors)
          AND sf.mom_20 IS NOT NULL
        GROUP BY sc.concept_name
        HAVING stock_count >= ?
        ORDER BY avg_mom DESC
        LIMIT ?
        """
        try:
            df = DBUtils.query_df(sql, params=[self.min_stocks_per_concept, self.market_top_n])
            if not df.empty:
                df = df[df['avg_mom'].notna()]
                return dict(zip(
                    df['concept_name'].apply(self._clean_concept_name),
                    df['avg_mom']
                ))
        except Exception:
            pass  # 表不存在，走回退路径

        # 回退：从 stock_daily 计算近20日动量
        print("  [Market] stock_factors 不可用，从 stock_daily 计算概念板块 mom_20...")
        return self._query_concept_momentum_from_daily()

    def _query_concept_momentum_from_daily(self):
        """从 stock_daily 计算概念板块近20日动量（stock_factors 回退）"""
        try:
            sql_dates = """
            SELECT DISTINCT trade_date FROM stock_daily
            ORDER BY trade_date DESC LIMIT 22
            """
            dates_df = DBUtils.query_df(sql_dates)
            if dates_df.empty or len(dates_df) < 2:
                return {}

            latest_date = str(dates_df.iloc[0]['trade_date'])
            early_date = str(dates_df.iloc[-1]['trade_date'])

            sql = """
            SELECT /*+ MAX_EXECUTION_TIME(30000) */
                sc.concept_name,
                AVG(t_now.close / NULLIF(t_prev.close, 0) - 1) AS avg_mom,
                COUNT(DISTINCT sc.ts_code) AS stock_count
            FROM stock_concepts sc
            INNER JOIN stock_daily t_now  ON sc.ts_code = t_now.ts_code  AND t_now.trade_date  = ?
            INNER JOIN stock_daily t_prev ON sc.ts_code = t_prev.ts_code AND t_prev.trade_date = ?
            WHERE t_now.close > 0 AND t_prev.close > 0
            GROUP BY sc.concept_name
            HAVING stock_count >= ?
            ORDER BY avg_mom DESC
            LIMIT ?
            """
            df = DBUtils.query_df(sql, params=[latest_date, early_date,
                                               self.min_stocks_per_concept, self.market_top_n])
            if df.empty:
                return {}
            df = df[df['avg_mom'].notna()]
            return dict(zip(
                df['concept_name'].apply(self._clean_concept_name),
                df['avg_mom']
            ))
        except Exception as e:
            print(f"  [Market] stock_daily mom_20 回退失败: {e}")
            return {}

    def _query_concept_recent_return(self, days=5):
        """查询各概念板块近 N 日平均涨幅"""
        try:
            dates_sql = """
            SELECT DISTINCT trade_date FROM stock_daily
            ORDER BY trade_date DESC LIMIT ?
            """
            dates_df = DBUtils.query_df(dates_sql, params=[days + 1])
            if dates_df.empty or len(dates_df) < 2:
                return {}
            start_date = str(dates_df.iloc[-1]['trade_date'])

            prev_cte = f"""
                SELECT p1.ts_code, p1.trade_date, p1.close
                FROM stock_daily p1
                INNER JOIN (
                    SELECT ts_code, MAX(trade_date) as max_date
                    FROM stock_daily
                    WHERE trade_date < (SELECT MIN(trade_date) FROM stock_daily WHERE trade_date >= '{start_date}')
                    GROUP BY ts_code
                ) p2 ON p1.ts_code COLLATE utf8mb4_general_ci = p2.ts_code COLLATE utf8mb4_general_ci
                    AND p1.trade_date = p2.max_date
            """

            sql = f"""
            SELECT /*+ MAX_EXECUTION_TIME(30000) */ /*+ QB_NAME(sub) */
                sc.concept_name,
                AVG((sd.close - prev.close) / prev.close * 100) AS avg_pct_chg,
                COUNT(DISTINCT sc.ts_code) AS stock_count
            FROM stock_concepts sc
            INNER JOIN stock_daily sd ON sc.ts_code = sd.ts_code
            INNER JOIN ({prev_cte}) prev ON sd.ts_code = prev.ts_code
            WHERE sd.trade_date >= '{start_date}'
              AND prev.close > 0
            GROUP BY sc.concept_name
            HAVING stock_count >= {self.min_stocks_per_concept}
            ORDER BY avg_pct_chg DESC
            LIMIT {self.market_top_n}
            """
            df = DBUtils.query_df(sql)
            if df.empty:
                # 兼容：stock_daily 可能没有 pct_chg，改用 close 计算
                return self._query_concept_return_from_close(days)
            df = df[df['avg_pct_chg'].notna()]
            return dict(zip(
                df['concept_name'].apply(self._clean_concept_name),
                df['avg_pct_chg']
            ))
        except Exception:
            return self._query_concept_return_from_close(days)

    def _query_concept_return_from_close(self, days=5):
        """兜底：用 close 价格计算近 N 日涨幅"""
        try:
            # 取最近两个有效交易日的收盘价计算涨幅
            sql_dates = """
            SELECT DISTINCT trade_date FROM stock_daily
            ORDER BY trade_date DESC LIMIT ?
            """
            dates_df = DBUtils.query_df(sql_dates, params=[days + 1])
            if dates_df.empty or len(dates_df) < 2:
                return {}

            latest_date = str(dates_df.iloc[0]['trade_date'])
            early_date = str(dates_df.iloc[-1]['trade_date'])

            sql = """
            SELECT /*+ MAX_EXECUTION_TIME(30000) */
                sc.concept_name,
                AVG(t_now.close / NULLIF(t_prev.close, 0) - 1) AS avg_return,
                COUNT(DISTINCT sc.ts_code) AS stock_count
            FROM stock_concepts sc
            INNER JOIN stock_daily t_now  ON sc.ts_code = t_now.ts_code  AND t_now.trade_date  = ?
            INNER JOIN stock_daily t_prev ON sc.ts_code = t_prev.ts_code AND t_prev.trade_date = ?
            WHERE t_now.close > 0 AND t_prev.close > 0
            GROUP BY sc.concept_name
            HAVING stock_count >= ?
            ORDER BY avg_return DESC
            LIMIT ?
            """
            df = DBUtils.query_df(sql, params=[latest_date, early_date,
                                               self.min_stocks_per_concept, self.market_top_n])
            if df.empty:
                return {}
            df = df[df['avg_return'].notna()]
            return dict(zip(
                df['concept_name'].apply(self._clean_concept_name),
                df['avg_return']
            ))
        except Exception as e:
            print(f"  [Market] close 涨幅查询失败: {e}")
            return {}

    # ------------------------------------------------------------------
    # 信号2: 新闻LLM
    # ------------------------------------------------------------------

    def _detect_from_news(self, news_analysis):
        """从 MarketNewsAnalyzer 输出提取利好板块评分

        Args:
            news_analysis: dict from MarketNewsAnalyzer.analyze()

        Returns:
            dict: {keyword: score (0~1)}
        """
        if not news_analysis:
            return {}

        scores = {}
        strength_map = {'强': 1.0, '中': 0.6, '弱': 0.3}

        for sector_info in news_analysis.get('sector_impacts', []):
            direction = sector_info.get('direction', '')
            strength = sector_info.get('strength', '弱')
            sector_name = sector_info.get('sector', '').strip()

            if not sector_name:
                continue

            if direction == '利好':
                scores[sector_name] = strength_map.get(strength, 0.3)
            elif direction == '利空':
                # 利空板块给负分，最终融合时会降低权重
                scores[sector_name] = -strength_map.get(strength, 0.3)

        if scores:
            print(f"[HotTopicDetector] 新闻信号: {len(scores)} 个板块 "
                  f"(利好{sum(1 for v in scores.values() if v > 0)}个, "
                  f"利空{sum(1 for v in scores.values() if v < 0)}个)")
        return scores

    # ------------------------------------------------------------------
    # 融合
    # ------------------------------------------------------------------

    def _merge_scores(self, market_scores, news_scores):
        """融合市场动量和新闻信号

        Returns:
            dict: {keyword: final_score}，只保留正分（热点）
        """
        all_keys = set(market_scores) | {k for k, v in news_scores.items() if v > 0}
        final = {}

        for kw in all_keys:
            # 跳过黑名单
            if self._is_blacklisted(kw):
                continue

            m_score = market_scores.get(kw, 0.0)
            n_score = max(news_scores.get(kw, 0.0), 0.0)   # 负分（利空）不参与正向融合

            # 配置的种子权重作为乘数加成（已知重要主题额外加分）
            seed_w = self.seed_weights.get(kw, 1.0)
            seed_bonus = (seed_w - 1.0) * 0.1  # 1.5x → +0.05 bonus

            score = (self.W_MARKET * m_score + self.W_NEWS * n_score + seed_bonus)

            # 利空板块降权（即使市场动量强，也要打折）
            if news_scores.get(kw, 0.0) < -0.5:
                score *= 0.5

            if score > 0:
                final[kw] = score

        return final

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _clean_concept_name(self, name):
        """清洗概念名称，提取核心关键词"""
        if not name:
            return name
        cleaned = _SUFFIX_STRIP.sub('', str(name)).strip()
        return cleaned if cleaned else name

    def _is_blacklisted(self, keyword):
        """判断关键词是否在黑名单中（过于泛化/无意义）"""
        for pattern in _BLACKLIST_PATTERNS:
            if pattern in keyword:
                return True
        return False

    def _normalize(self, scores_dict):
        """将 dict 值归一化到 [0, 1]"""
        if not scores_dict:
            return {}
        values = list(scores_dict.values())
        min_v, max_v = min(values), max(values)
        if max_v == min_v:
            return {k: 0.5 for k in scores_dict}
        return {k: (v - min_v) / (max_v - min_v) for k, v in scores_dict.items()}
