"""
StrategyAgent: 选股策略Agent

负责：
1. 市场状态检测（强市/中性/弱市）
2. 调用 HybridStrategy 生成候选股票
3. ETF 策略选股
4. 可转债策略选股
5. 行业/概念热点分析
6. 输出按策略分组的 top_picks 和 sector_analysis
"""
import pandas as pd
import threading
import akshare as ak
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

from src.strategy.hybrid_strategy import HybridStrategy
from src.analysis.event_driver import EventDriver
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class StrategyAgent:
    """选股策略Agent"""

    def __init__(self, hot_topics: Optional[List[str]] = None):
        self.hot_topics = hot_topics or self._load_hot_topics()
        self.hybrid_strategy = HybridStrategy(hot_topics=self.hot_topics)
        self.event_driver = EventDriver(hot_topics=self.hot_topics)
        print(f"[StrategyAgent] 初始化完成，监控热点: {self.hot_topics[:5]}...")

    def _get_latest_trade_date(self) -> Optional[str]:
        """获取 stock_daily 最新交易日"""
        try:
            df = DBUtils.query_df(
                "SELECT MAX(trade_date) as latest FROM stock_daily"
            )
            if not df.empty:
                return str(df.iloc[0]['latest'])
        except Exception:
            pass
        return None

    def _load_hot_topics(self) -> List[str]:
        """从配置加载热点主题"""
        try:
            topics = Config.get('event_driver.hot_topics')
            if topics:
                return topics
        except Exception:
            pass
        return [
            "人工智能", "半导体", "新能源汽车", "光伏",
            "生物医药", "云计算", "军工", "数字经济"
        ]

    def analyze_market_regime(self, trade_date: str) -> Dict[str, Any]:
        """检测市场状态"""
        try:
            regime, mult = self.hybrid_strategy._get_market_regime()
            return {
                "regime": regime,
                "top_k_mult": mult,
                "trade_date": trade_date
            }
        except Exception as e:
            print(f"[StrategyAgent] 市场状态检测失败: {e}")
            return {"regime": "neutral", "top_k_mult": 1.0, "trade_date": trade_date}

    def _run_market_research(self, trade_date: str):
        """运行市场研究流水线，结果写入信号表供 HybridStrategy 消费"""
        try:
            from src.analysis.research_runner import ResearchRunner
            runner = ResearchRunner(trade_date=trade_date)
            results = runner.run_all()
            modules = list(results.keys())
            print(f"[StrategyAgent] 市场研究完成: {modules}")
        except Exception as e:
            print(f"[StrategyAgent] 市场研究流水线失败: {e}")

    def get_hot_sectors(self, trade_date: str, top_n: int = 10) -> List[str]:
        """获取热点行业（用 Python 计算涨幅，避免 SQL  collation 问题）"""
        try:
            today_df = DBUtils.query_df(
                f"SELECT ts_code, close FROM stock_daily WHERE trade_date = '{trade_date}'"
            )
            if today_df.empty:
                return []
            prev_df = DBUtils.query_df(f"""
                SELECT t1.ts_code, t1.close
                FROM stock_daily t1
                INNER JOIN (
                    SELECT ts_code, MAX(trade_date) as max_dt
                    FROM stock_daily WHERE trade_date < '{trade_date}'
                    GROUP BY ts_code
                ) t2 ON t1.ts_code = t2.ts_code AND t1.trade_date = t2.max_dt
            """)
            if prev_df.empty:
                return []
            prev_df = prev_df.rename(columns={'close': 'prev_close'})
            today_df = today_df.rename(columns={'close': 'close'})
            merged = today_df.merge(prev_df, on='ts_code', how='inner')
            merged = merged[merged['prev_close'] > 0]
            merged['pct_chg'] = (merged['close'] - merged['prev_close']) / merged['prev_close'] * 100
            info_df = DBUtils.query_df(
                f"SELECT ts_code, industry FROM stock_info WHERE industry IS NOT NULL AND industry != ''"
            )
            if info_df.empty:
                return []
            merged = merged.merge(info_df, on='ts_code', how='inner')
            if merged.empty:
                return []
            sector_df = merged.groupby('industry')['pct_chg'].mean().reset_index()
            sector_df = sector_df.sort_values('pct_chg', ascending=False).head(top_n)
            return sector_df['industry'].tolist()
        except Exception as e:
            print(f"[StrategyAgent] 热点行业查询失败: {e}")
            return []

    def get_hot_concepts(self, trade_date: str, top_n: int = 10) -> List[str]:
        """获取热点概念"""
        try:
            df = DBUtils.query_df("""
                SELECT sc.concept_name, COUNT(*) as stock_count,
                       AVG((sd.close - prev.close) / prev.close * 100) as avg_pct
                FROM stock_concepts sc
                JOIN stock_daily sd ON sc.ts_code = sd.ts_code AND sd.trade_date = ?
                LEFT JOIN (
                    SELECT ts_code, close
                    FROM stock_daily
                    WHERE trade_date = (
                        SELECT MAX(trade_date) FROM stock_daily WHERE trade_date < ?
                    )
                ) prev ON sd.ts_code = prev.ts_code
                GROUP BY sc.concept_name
                HAVING avg_pct IS NOT NULL
                ORDER BY avg_pct DESC
                LIMIT ?
            """, (trade_date, trade_date, top_n))
            if df.empty:
                return []
            return df['concept_name'].tolist()
        except Exception as e:
            print(f"[StrategyAgent] 热点概念查询失败: {e}")
            return []

    def generate_picks(
        self,
        trade_date: str,
        top_k: int = 20,
        news_boost_sectors: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """生成选股结果"""
        try:
            result_df = self.hybrid_strategy.run(
                trade_date=trade_date,
                top_k=top_k,
                news_boost_sectors=news_boost_sectors
            )
            if result_df.empty:
                print("[StrategyAgent] HybridStrategy 返回空结果")
                return []

            picks = []
            for _, row in result_df.iterrows():
                ai_score = float(row.get("ai_score", 0)) if pd.notna(row.get("ai_score")) else 0.0
                event_score = float(row.get("event_score", 0)) if pd.notna(row.get("event_score")) else 0.0
                fund_score = float(row.get("fundamental_score", 0)) if pd.notna(row.get("fundamental_score")) else 0.0
                pick = {
                    "ts_code": str(row.get("ts_code", "")),
                    "name": str(row.get("name", row.get("company_name", ""))),
                    "final_score": float(row.get("final_score", 0)),
                    "ai_score": ai_score,
                    "event_score": event_score,
                    "fundamental_score": fund_score,
                    "industry": str(row.get("industry", "")),
                    "track": str(row.get("track", "")),
                    "close": float(row.get("close", 0)),
                    "roe": float(row.get("roe", 0)) if pd.notna(row.get("roe")) else None,
                    "pe_ttm": float(row.get("pe_ttm", 0)) if pd.notna(row.get("pe_ttm")) else None,
                    "concepts": str(row.get("concepts", "")) if pd.notna(row.get("concepts")) else "",
                }
                picks.append(pick)

            print(f"[StrategyAgent] 生成 {len(picks)} 只选股结果")
            return picks

        except Exception as e:
            print(f"[StrategyAgent] 选股生成失败: {e}")
            return []

    def analyze_sector(self, trade_date: str, sectors: List[str]) -> str:
        """分析指定行业（用 Python 计算涨幅）"""
        analysis_parts = []
        try:
            today_df = DBUtils.query_df(
                f"SELECT sd.ts_code, sd.close, si.industry FROM stock_daily sd "
                f"LEFT JOIN stock_info si ON sd.ts_code = si.ts_code "
                f"WHERE sd.trade_date = '{trade_date}'"
            )
            if today_df.empty:
                return "无行业数据"
            prev_df = DBUtils.query_df(f"""
                SELECT t1.ts_code, t1.close
                FROM stock_daily t1
                INNER JOIN (
                    SELECT ts_code, MAX(trade_date) as max_dt
                    FROM stock_daily WHERE trade_date < '{trade_date}'
                    GROUP BY ts_code
                ) t2 ON t1.ts_code = t2.ts_code AND t1.trade_date = t2.max_dt
            """)
            if prev_df.empty:
                return "无行业数据"
            prev_df = prev_df.rename(columns={'close': 'prev_close'})
            today_df = today_df.rename(columns={'close': 'close'})
            merged = today_df.merge(prev_df, on='ts_code', how='inner')
            merged = merged[merged['prev_close'] > 0]
            merged['pct_chg'] = (merged['close'] - merged['prev_close']) / merged['prev_close'] * 100
            for sector in sectors[:5]:
                sector_data = merged[merged['industry'] == sector]
                if not sector_data.empty:
                    analysis_parts.append(
                        f"{sector}: {len(sector_data)}只 平均涨幅{sector_data['pct_chg'].mean():.2f}% "
                        f"换手率0.00%"
                    )
        except Exception:
            pass
        return "; ".join(analysis_parts) if analysis_parts else "无行业数据"

    def run(self, trade_date: str, top_k: int = 20) -> Dict[str, Any]:
        """完整执行策略分析（仅股票策略）"""
        latest_date = self._get_latest_trade_date()
        if latest_date and trade_date > latest_date:
            print(f"[StrategyAgent] 今日({trade_date})无数据，使用最新日期{latest_date}")
            trade_date = latest_date

        # 先运行市场研究流水线（期货/新闻/行业/北向/龙虎榜 → 信号表）
        self._run_market_research(trade_date)

        print(f"\n{'='*60}")
        print(f"[StrategyAgent] 开始执行 | 日期: {trade_date} | top_k: {top_k}")
        print(f"{'='*60}")

        hot_sectors = self.get_hot_sectors(trade_date)
        hot_concepts = self.get_hot_concepts(trade_date)
        regime_info = self.analyze_market_regime(trade_date)

        effective_top_k = int(top_k * regime_info.get("top_k_mult", 1.0))
        picks = self.generate_picks(trade_date, top_k=effective_top_k)

        sector_analysis = self.analyze_sector(trade_date, hot_sectors[:5])

        result = {
            "candidates": picks,
            "hot_sectors": hot_sectors,
            "hot_concepts": hot_concepts,
            "sector_analysis": sector_analysis,
            "top_picks": picks[:top_k],
            "market_regime": regime_info,
            "trade_date": trade_date,
            "total_picks": len(picks),
            "etf_picks": [],
            "cb_picks": [],
        }

        print(f"[StrategyAgent] 执行完成 | 选出 {len(picks)} 只 | 市场状态: {regime_info['regime']}")
        return result

    def run_multi_strategy(self, trade_date: str, top_k: int = 20) -> Dict[str, Any]:
        """多策略并行选股：股票 + ETF + 可转债"""
        latest_date = self._get_latest_trade_date()
        if latest_date and trade_date > latest_date:
            trade_date = latest_date

        # 先运行市场研究流水线（期货/新闻/行业/北向/龙虎榜 → 信号表）
        self._run_market_research(trade_date)

        print(f"\n{'='*60}")
        print(f"[StrategyAgent] 多策略执行 | 日期: {trade_date} | top_k: {top_k}")
        print(f"{'='*60}")

        hot_sectors = self.get_hot_sectors(trade_date)
        hot_concepts = self.get_hot_concepts(trade_date)
        regime_info = self.analyze_market_regime(trade_date)
        effective_top_k = int(top_k * regime_info.get("top_k_mult", 1.0))
        sector_analysis = self.analyze_sector(trade_date, hot_sectors[:5])

        stock_picks = []
        etf_picks = []
        cb_picks = []
        stock_error = None
        etf_error = None
        cb_error = None

        def run_stocks():
            nonlocal stock_picks, stock_error
            try:
                stock_picks = self.generate_picks(trade_date, top_k=effective_top_k)
            except Exception as e:
                stock_error = str(e)
                print(f"[StrategyAgent] 股票策略失败: {e}")

        def run_etf():
            nonlocal etf_picks, etf_error
            try:
                etf_picks = self._get_etf_picks(top_n=top_k)
            except Exception as e:
                etf_error = str(e)
                print(f"[StrategyAgent] ETF策略失败: {e}")

        def run_cb():
            nonlocal cb_picks, cb_error
            try:
                cb_picks = self._get_cb_picks(top_n=top_k)
            except Exception as e:
                cb_error = str(e)
                print(f"[StrategyAgent] 可转债策略失败: {e}")

        threads = [
            threading.Thread(target=run_stocks, name="stock-strategy"),
            threading.Thread(target=run_etf, name="etf-strategy"),
            threading.Thread(target=run_cb, name="cb-strategy"),
        ]
        for t in threads:
            t.start()

        for t in threads:
            t.join()

        all_picks = stock_picks + etf_picks + cb_picks

        result = {
            "trade_date": trade_date,
            "market_regime": regime_info,
            "hot_sectors": hot_sectors,
            "hot_concepts": hot_concepts,
            "sector_analysis": sector_analysis,
            "stock_picks": stock_picks,
            "etf_picks": etf_picks,
            "cb_picks": cb_picks,
            "candidates": all_picks,
            "top_picks": all_picks[:top_k],
            "total_picks": len(all_picks),
            "stock_count": len(stock_picks),
            "etf_count": len(etf_picks),
            "cb_count": len(cb_picks),
            "stock_error": stock_error,
            "etf_error": etf_error,
            "cb_error": cb_error,
        }

        print(f"[StrategyAgent] 多策略完成 | 股票:{len(stock_picks)} ETF:{len(etf_picks)} 可转债:{len(cb_picks)}")
        return result

    def _get_etf_picks(self, top_n: int = 10) -> List[Dict[str, Any]]:
        """ETF 策略选股"""
        import glob, os
        ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        output_dir = os.path.join(ROOT, 'output')
        files = sorted(glob.glob(os.path.join(output_dir, 'etf_picks_*.csv')), reverse=True)
        if files:
            try:
                df = pd.read_csv(files[0])
                picks = []
                for _, row in df.head(top_n).iterrows():
                    picks.append({
                        "ts_code": str(row.get("code", row.get("ts_code", ""))),
                        "name": str(row.get("name", "")),
                        "final_score": float(row.get("score", row.get("final_score", 0)) or 0),
                        "track": "etf",
                        "close": float(row.get("price", row.get("close", 0)) or 0),
                        "advice": str(row.get("advice", "")),
                        "ret_5d": float(row.get("ret_5d", 0)) or 0,
                        "etf_type": str(row.get("etf_type", "astock")),
                    })
                return picks
            except Exception:
                pass
        return []

    def _get_cb_picks(self, top_n: int = 10) -> List[Dict[str, Any]]:
        """可转债 策略选股"""
        try:
            df = ak.bond_cb_jsl()
            picks = []
            for _, row in df.head(top_n * 3).iterrows():
                try:
                    price = float(row.iloc[8]) if len(row) > 8 else 0
                    premium = float(row.iloc[12]) if len(row) > 12 else 0
                    ytm_raw = row.iloc[16] if len(row) > 16 else 0
                    ytm = float(ytm_raw) if ytm_raw and str(ytm_raw) not in ('nan', '') else 0
                    scale_raw = row.iloc[18] if len(row) > 18 else 0
                    scale = float(scale_raw) if scale_raw and str(scale_raw) not in ('nan', '') else 0
                    cb_name = str(row.iloc[3]) if len(row) > 3 else ''
                    stock_code = str(row.iloc[1]) if len(row) > 1 else ''
                    stock_name = str(row.iloc[2]) if len(row) > 2 else ''
                    if price <= 0:
                        continue
                    if scale > 1 and scale < 15 and premium < 50:
                        score = 100.0
                        if ytm > 0:
                            score += 20
                        if premium < 20:
                            score += 20
                        picks.append({
                            "ts_code": cb_name,
                            "name": cb_name,
                            "stock_code": stock_code,
                            "stock_name": stock_name,
                            "final_score": score,
                            "track": "cb",
                            "close": price,
                            "premium_ratio": premium,
                            "ytm": ytm,
                            "scale": scale,
                            "reason": f"溢价{premium:.1f}% YTM={ytm*100:.1f}% 规模{scale:.1f}亿",
                        })
                except Exception:
                    continue
            picks.sort(key=lambda x: x['final_score'], reverse=True)
            return picks[:top_n]
        except Exception as e:
            print(f"[StrategyAgent] 可转债策略失败: {e}")
            return []
