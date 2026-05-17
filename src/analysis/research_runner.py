"""
ResearchRunner: 市场研究流水线
在选股前并行执行所有研究模块，结果写入信号表，供 HybridStrategy 消费
"""
import time
from datetime import datetime
from typing import Dict, Any, List

from loguru import logger

from src.utils.db_utils import DBUtils
from src.analysis.research_tables import ensure_research_tables


class ResearchRunner:
    """
    市场研究流水线

    执行顺序:
    1. gov_news → news_sector_signals    (政策板块信号)
    2. futures_prices → futures_sector_signals  (期货板块信号)
    3. northbound_flow → market_sentiment  (北向情绪)
    4. dragon_tiger → institutional_signals  (龙虎榜机构信号)
    5. hot_topic_detector → hot_topics_log   (热点主题日志)
    6. industry_timing → sector_timing_log   (行业轮动)
    """

    def __init__(self, trade_date: str = None):
        self.trade_date = trade_date or datetime.now().strftime('%Y-%m-%d')
        ensure_research_tables()

    def run_all(self) -> Dict[str, Any]:
        """
        执行全部研究模块
        Returns: {module: result, ...}
        """
        results = {}

        # 1. 政策板块信号
        try:
            r = self._run_gov_news_signals()
            results['gov_news_signals'] = r
            logger.info(f"[Research] gov_news_signals: {r.get('count', 0)} 条")
        except Exception as e:
            logger.error(f"[Research] gov_news_signals failed: {e}")

        # 2. 期货板块信号
        try:
            r = self._run_futures_signals()
            results['futures_signals'] = r
            logger.info(f"[Research] futures_signals: {r.get('count', 0)} 条")
        except Exception as e:
            logger.error(f"[Research] futures_signals failed: {e}")

        # 3. 北向情绪
        try:
            r = self._run_market_sentiment()
            results['market_sentiment'] = r
            logger.info(f"[Research] market_sentiment: {r}")
        except Exception as e:
            logger.error(f"[Research] market_sentiment failed: {e}")

        # 4. 龙虎榜机构信号
        try:
            r = self._run_institutional_signals()
            results['institutional_signals'] = r
            logger.info(f"[Research] institutional_signals: {r.get('count', 0)} 条")
        except Exception as e:
            logger.error(f"[Research] institutional_signals failed: {e}")

        # 5. 热点主题日志
        try:
            r = self._run_hot_topics()
            results['hot_topics'] = r
            logger.info(f"[Research] hot_topics: {r.get('count', 0)} 条")
        except Exception as e:
            logger.error(f"[Research] hot_topics failed: {e}")

        # 6. 行业轮动
        try:
            r = self._run_sector_timing()
            results['sector_timing'] = r
            logger.info(f"[Research] sector_timing: {r.get('count', 0)} 条")
        except Exception as e:
            logger.error(f"[Research] sector_timing failed: {e}")

        return results

    # ── 1. gov_news → news_sector_signals ───────────────────────────────
    def _run_gov_news_signals(self) -> Dict[str, Any]:
        """
        读取 gov_news 表中近3天有 sector_tags 的记录，
        按板块聚合利多/利空，转换为 [-0.1, +0.1] 的信号强度
        """
        cutoff = datetime.now().strftime('%Y-%m-%d 00:00:00')
        df = DBUtils.query_df(f"""
            SELECT source_key, sector_tags, sentiment, llm_summary, fetched_at
            FROM gov_news
            WHERE llm_processed = 1
              AND sector_tags IS NOT NULL AND sector_tags != ''
              AND fetched_at >= ?
        """, (cutoff,))

        if df.empty:
            return {'count': 0}

        rows = []
        for _, row in df.iterrows():
            tags = str(row['sector_tags'] or '')
            sentiment = str(row['sentiment'] or 'neutral')
            if not tags:
                continue

            # 解析板块标签（逗号分隔）
            for tag in tags.split(','):
                tag = tag.strip()
                if not tag:
                    continue

                # 信号强度: 利多=+0.1, 中性=0, 利空=-0.1
                if sentiment == 'positive':
                    strength = 0.1
                elif sentiment == 'negative':
                    strength = -0.1
                else:
                    strength = 0.0

                rows.append({
                    'trade_date': self.trade_date,
                    'source_key': str(row['source_key'] or ''),
                    'sector_name': tag,
                    'sentiment': sentiment,
                    'signal_strength': strength,
                    'llm_summary': str(row['llm_summary'] or ''),
                    'fetched_at': str(row['fetched_at'] or ''),
                })

        # 按板块聚合：同一板块多个来源取平均强度
        if not rows:
            return {'count': 0}

        import pandas as pd
        agg_df = pd.DataFrame(rows)
        agg_df = agg_df.groupby(['trade_date', 'sector_name'], as_index=False).agg({
            'signal_strength': 'mean',
            'sentiment': 'first',
            'source_key': 'first',
            'llm_summary': 'first',
            'fetched_at': 'max',
        })

        saved = 0
        for _, r in agg_df.iterrows():
            try:
                DBUtils.execute("""
                    INSERT OR REPLACE INTO news_sector_signals
                    (trade_date, source_key, sector_name, sentiment, signal_strength, llm_summary, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (r['trade_date'], r['source_key'], r['sector_name'], r['sentiment'],
                      float(r['signal_strength']), r['llm_summary'], r['fetched_at']))
                saved += 1
            except Exception as e:
                logger.debug(f"[gov_news_signals] skip: {e}")

        return {'count': saved}

    # ── 2. futures_prices → futures_sector_signals ─────────────────────
    def _run_futures_signals(self) -> Dict[str, Any]:
        """
        读取 futures_etf_signal 生成的板块信号，写入 futures_sector_signals
        """
        df = DBUtils.query_df("SELECT COUNT(*) FROM futures_prices")
        if df.empty or df.iloc[0][0] == 0:
            logger.warning("[futures] futures_prices 表为空，跳过")
            return {'count': 0}

        try:
            from src.analysis.futures_etf_signal import FuturesETFSignalGenerator
            gen = FuturesETFSignalGenerator()
            all_signals = gen.get_all_sector_signals()

            saved = 0
            for sector, sig in all_signals.items():
                if sig.get('signal') in (None, 'N/A'):
                    continue
                try:
                    DBUtils.execute("""
                        INSERT OR REPLACE INTO futures_sector_signals
                        (trade_date, sector, signal, strength, score, reason)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        self.trade_date,
                        sector,
                        sig.get('signal', 'HOLD'),
                        float(sig.get('strength', 0)),
                        float(sig.get('score', 0)),
                        sig.get('reason', ''),
                    ))
                    saved += 1
                except Exception as e:
                    logger.debug(f"[futures_signals] skip {sector}: {e}")

            return {'count': saved}
        except Exception as e:
            logger.error(f"[futures_signals] generator error: {e}")
            return {'count': 0}

    # ── 3. northbound_flow → market_sentiment ───────────────────────────
    def _run_market_sentiment(self) -> Dict[str, Any]:
        """
        读取近5日北向资金，计算情绪分位，存入 market_sentiment
        """
        df = DBUtils.query_df("""
            SELECT trade_date, cflow_hs
            FROM northbound_flow
            ORDER BY trade_date DESC
            LIMIT 20
        """)
        if df.empty:
            return {'level': 'unknown', 'score': 0.5}

        # 计算分位
        values = df['cflow_hs'].dropna().sort_values().values
        if len(values) == 0:
            return {'level': 'unknown', 'score': 0.5}

        latest = float(df.iloc[0]['cflow_hs'])
        percentile = (values < latest).sum() / len(values)

        if percentile >= 0.7:
            level = 'bullish'
            score = min(0.5 + (percentile - 0.5) * 0.5, 0.9)
        elif percentile <= 0.3:
            level = 'bearish'
            score = max(0.5 - (0.5 - percentile) * 0.5, 0.1)
        else:
            level = 'neutral'
            score = 0.5

        try:
            DBUtils.execute("""
                INSERT OR REPLACE INTO market_sentiment
                (trade_date, northbound_flow, northbound_pct, sentiment_level, score)
                VALUES (?, ?, ?, ?, ?)
            """, (self.trade_date, latest, percentile, level, score))
        except Exception as e:
            logger.debug(f"[market_sentiment] skip: {e}")

        return {'level': level, 'score': score, 'percentile': percentile}

    # ── 4. dragon_tiger → institutional_signals ─────────────────────────
    def _run_institutional_signals(self) -> Dict[str, Any]:
        """
        读取龙虎榜数据，计算净买入，写入 institutional_signals
        """
        df = DBUtils.query_df(f"""
            SELECT trade_date, code, name, reason, net_amount
            FROM dragon_tiger
            WHERE trade_date = ?
        """, (self.trade_date.replace('-', ''),))

        if df.empty:
            return {'count': 0}

        saved = 0
        for _, row in df.iterrows():
            try:
                net = float(row['net_amount'] or 0)
                buy = net if net > 0 else 0
                sell = abs(net) if net < 0 else 0
                DBUtils.execute("""
                    INSERT OR REPLACE INTO institutional_signals
                    (trade_date, ts_code, name, reason, buy_amount, sell_amount, net_amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    self.trade_date,
                    str(row['code'] or ''),
                    str(row['name'] or ''),
                    str(row['reason'] or ''),
                    buy, sell, net,
                ))
                saved += 1
            except Exception as e:
                logger.debug(f"[institutional] skip: {e}")

        return {'count': saved}

    # ── 5. hot_topic_detector → hot_topics_log ─────────────────────────
    def _run_hot_topics(self) -> Dict[str, Any]:
        """
        运行热点检测器，输出写入 hot_topics_log
        """
        try:
            from src.analysis.hot_topic_detector import HotTopicDetector
            detector = HotTopicDetector()
            topics = detector.detect(top_k=20)

            saved = 0
            for topic, score in topics.items():
                try:
                    DBUtils.execute("""
                        INSERT OR REPLACE INTO hot_topics_log
                        (trade_date, topic, score, source)
                        VALUES (?, ?, ?, ?)
                    """, (self.trade_date, topic, float(score), 'auto'))
                    saved += 1
                except Exception as e:
                    logger.debug(f"[hot_topics] skip {topic}: {e}")

            return {'count': saved, 'topics': list(topics.keys())[:5]}
        except Exception as e:
            logger.error(f"[hot_topics] detector error: {e}")
            return {'count': 0}

    # ── 6. industry_timing → sector_timing_log ─────────────────────────
    def _run_sector_timing(self) -> Dict[str, Any]:
        """
        运行行业轮动分析，结果写入 sector_timing_log
        """
        try:
            from src.analysis.industry_timing import IndustryTiming
            timing = IndustryTiming()
            df = timing.run()

            if df.empty:
                return {'count': 0}

            saved = 0
            for _, row in df.iterrows():
                try:
                    DBUtils.execute("""
                        INSERT OR REPLACE INTO sector_timing_log
                        (trade_date, industry, return_pct, relative_strength,
                         penetration_phase, cycle_type, suggestion)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        self.trade_date,
                        str(row['industry']),
                        float(row.get('return_pct', 0)),
                        float(row.get('relative_strength', 0)),
                        str(row.get('penetration_phase', '')),
                        str(row.get('cycle_type', '')),
                        str(row.get('suggest', '')),
                    ))
                    saved += 1
                except Exception as e:
                    logger.debug(f"[sector_timing] skip: {e}")

            return {'count': saved}
        except Exception as e:
            logger.error(f"[sector_timing] error: {e}")
            return {'count': 0}

    # ── 读取器：供 HybridStrategy 消费 ───────────────────────────────────
    @staticmethod
    def get_news_sector_signals(trade_date: str = None) -> Dict[str, float]:
        """返回 {板块名: 信号强度}"""
        cutoff = datetime.now().strftime('%Y-%m-%d 00:00:00')
        if trade_date:
            cutoff = trade_date + ' 00:00:00'
        df = DBUtils.query_df("""
            SELECT sector_name, AVG(signal_strength) as avg_strength
            FROM news_sector_signals
            WHERE fetched_at >= ?
            GROUP BY sector_name
        """, (cutoff,))
        return {str(r['sector_name']): float(r['avg_strength'])
                for _, r in df.iterrows()}

    @staticmethod
    def get_futures_signals(trade_date: str = None) -> Dict[str, Dict[str, Any]]:
        """返回 {板块名: {signal, strength, score}}"""
        date_filter = f"trade_date = '{trade_date}'" if trade_date else \
            "trade_date = (SELECT MAX(trade_date) FROM futures_sector_signals)"
        df = DBUtils.query_df(f"SELECT * FROM futures_sector_signals WHERE {date_filter}")
        return {str(r['sector']): {
            'signal': str(r['signal']),
            'strength': float(r['strength'] or 0),
            'score': float(r['score'] or 0),
        } for _, r in df.iterrows()}

    @staticmethod
    def get_market_sentiment(trade_date: str = None) -> Dict[str, Any]:
        """返回 {level, score, percentile}"""
        date_filter = f"trade_date = '{trade_date}'" if trade_date else \
            "trade_date = (SELECT MAX(trade_date) FROM market_sentiment)"
        df = DBUtils.query_df(f"SELECT * FROM market_sentiment WHERE {date_filter}")
        if df.empty:
            return {'level': 'neutral', 'score': 0.5, 'percentile': 0.5}
        r = df.iloc[0]
        return {
            'level': str(r['sentiment_level']),
            'score': float(r['score'] or 0.5),
            'percentile': float(r['northbound_pct'] or 0.5),
        }

    @staticmethod
    def get_institutional_signals(trade_date: str = None) -> Dict[str, float]:
        """返回 {ts_code: net_amount}"""
        date_filter = f"trade_date = '{trade_date}'" if trade_date else \
            f"trade_date = '{datetime.now().strftime('%Y%m%d')}'"
        df = DBUtils.query_df(f"""
            SELECT ts_code, SUM(net_amount) as total_net
            FROM institutional_signals
            WHERE {date_filter}
            GROUP BY ts_code
        """)
        return {str(r['ts_code']): float(r['total_net']) for _, r in df.iterrows()}

    @staticmethod
    def get_hot_topics(trade_date: str = None, top_k: int = 20) -> List[str]:
        """返回热点主题列表"""
        date_filter = f"trade_date = '{trade_date}'" if trade_date else \
            "trade_date = (SELECT MAX(trade_date) FROM hot_topics_log)"
        df = DBUtils.query_df(f"""
            SELECT topic FROM hot_topics_log
            WHERE {date_filter}
            ORDER BY score DESC LIMIT {top_k}
        """)
        return [str(r['topic']) for _, r in df.iterrows()]

    @staticmethod
    def get_sector_timing(trade_date: str = None) -> Dict[str, Dict[str, Any]]:
        """返回 {行业名: {relative_strength, suggest}}"""
        date_filter = f"trade_date = '{trade_date}'" if trade_date else \
            "trade_date = (SELECT MAX(trade_date) FROM sector_timing_log)"
        df = DBUtils.query_df(f"SELECT * FROM sector_timing_log WHERE {date_filter}")
        return {str(r['industry']): {
            'relative_strength': float(r['relative_strength'] or 0),
            'return_pct': float(r['return_pct'] or 0),
            'penetration_phase': str(r['penetration_phase'] or ''),
            'cycle_type': str(r['cycle_type'] or ''),
            'suggest': str(r['suggest'] or ''),
        } for _, r in df.iterrows()}
