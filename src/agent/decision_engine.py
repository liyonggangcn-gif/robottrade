#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
决策引擎
读取量化选股结果 → LLM分析 → 综合决策 → 生成交易计划
"""
import glob
import json
import os
import re
from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd
from loguru import logger

from src.broker.base_broker import BaseBroker, Position
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.utils.llm_router import LLMRouter
from src.agent.trade_memory import TradeMemory

try:
    from src.agent.multi_agent.memory_service import MemoryService
    MEMORY_SERVICE_ENABLED = True
except ImportError:
    MEMORY_SERVICE_ENABLED = False

# 项目根目录（src/agent/../../ = project root）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


class DecisionEngine:
    """
    交易决策引擎
    步骤：量化候选 → LLM逐一分析 → R1综合推理 → 输出交易计划
    """

    def __init__(self, broker: BaseBroker, llm_router: LLMRouter, memory: TradeMemory):
        self._broker = broker
        self._router = llm_router
        self._memory = memory

        # 记忆服务 (MemoryService 集成)
        self._memory_service = None
        self._memory_bonus_map = {}
        if MEMORY_SERVICE_ENABLED:
            try:
                self._memory_service = MemoryService()
                logger.info("[Decision] MemoryService 已集成")
            except Exception as e:
                logger.warning(f"[Decision] MemoryService 初始化失败: {e}")

        # 读取决策参数配置
        cfg = Config.get('trading_agent.decision') or {}
        self._topk_a: int = int(cfg.get('topk_a', 4))
        self._topk_b: int = int(cfg.get('topk_b', 4))

        # 分析缓存：避免同一天重复调用LLM
        self._analysis_cache = {}  # key: f"{trade_date}_{hash_of_candidates}"
        self._topk_pb_roa: int = int(cfg.get('topk_pb_roa', 2))  # 新增 PB-ROA 配额
        self._topk_cb: int = int(cfg.get('topk_cb', 2))             # 新增 可转债 配额
        self._topk_index_enhance: int = int(cfg.get('topk_index_enhance', 2))  # 新增 指数增强 配额
        self._max_single_weight: float = float(cfg.get('max_single_weight', 0.20))
        self._cash_reserve: float = float(cfg.get('cash_reserve', 0.20))

        # 策略轨道映射（用于分析框架差异化）
        self._STRATEGY_FRAMEWORKS = {
            'sector_rotation': {'name': '行业轮动', 'min_hold_days': 5, 'focus': 'AI Layer景气度 + 催化剂'},
            'value': {'name': '价值质量', 'min_hold_days': 15, 'focus': '股息率 + ROE稳定性 + 估值分位'},
            'dividend': {'name': '红利', 'min_hold_days': 15, 'focus': '股息率 + 派息稳定性'},
            'pb_roa': {'name': 'PB-ROA价值', 'min_hold_days': 20, 'focus': 'PB分位 + ROA绝对值 + 负债率'},
            'convertible_bond': {'name': '可转债', 'min_hold_days': 10, 'focus': 'YTM安全垫 + 转股溢价率 + 正股动量'},
            'index_enhance': {'name': '指数增强', 'min_hold_days': 10, 'focus': 'Alpha因子 + 行业中性 + 跟踪误差'},
            'etf': {'name': 'ETF', 'min_hold_days': 3, 'focus': '行业趋势 + 流动性 + 期货信号'},
        }

        self._ensure_table()
        
        # 加载记忆加分
        self._load_memory_bonus()
        
        logger.info(f"[Decision] 初始化  topk_a={self._topk_a}  topk_b={self._topk_b}  "
                    f"topk_pb_roa={self._topk_pb_roa}  topk_cb={self._topk_cb}  "
                    f"topk_index_enhance={self._topk_index_enhance}")

    # ------------------------------------------------------------------ #
    #  记忆加分 (MemoryService 集成)
    # ------------------------------------------------------------------ #
    def _load_memory_bonus(self):
        """从 MemoryService 加载高置信度记忆，用于候选股加分"""
        if not self._memory_service:
            return
        
        try:
            facts = self._memory_service.get_top_facts(limit=30)
            self._memory_bonus_map = {}
            
            # 过滤高置信度(≥0.7)
            for fact in facts:
                if fact.confidence < 0.7:
                    continue
                content = fact.content
                if '买入' in content or '推荐' in content:
                    for code in self._extract_codes(content):
                        if code not in self._memory_bonus_map:
                            self._memory_bonus_map[code] = 0.0
                        self._memory_bonus_map[code] += fact.confidence * 0.03
            
            if self._memory_bonus_map:
                logger.info(f"[Decision] 记忆加分: {len(self._memory_bonus_map)} 只股票")
        except Exception as e:
            logger.warning(f"[Decision] 加载记忆加分失败: {e}")
    
    def _extract_codes(self, text: str) -> List[str]:
        """从文本中提取股票代码"""
        import re
        codes = re.findall(r'\b\d{6}\.\w{2,3}\b', text)
        return codes
    
    def _apply_memory_bonus(self, candidates: List[dict]) -> List[dict]:
        """应用记忆加分到候选股"""
        if not self._memory_bonus_map:
            return candidates
        
        bonus_count = 0
        for c in candidates:
            ts_code = c.get('ts_code', '')
            bonus = self._memory_bonus_map.get(ts_code, 0)
            if bonus > 0:
                c['final_score'] = c.get('final_score', 0) + bonus
                c['memory_bonus'] = bonus
                bonus_count += 1
        
        if bonus_count > 0:
            candidates.sort(key=lambda x: x.get('final_score', 0), reverse=True)
            logger.info(f"[Decision] 记忆加分应用: {bonus_count} 只股票")
        
        return candidates
    
    def _save_decision_to_memory(self, trade_date: str, decision_result: dict):
        """保存决策结果到记忆服务"""
        if not self._memory_service:
            return
        
        try:
            self._memory_service.save_decision(
                trade_date=trade_date,
                decision_type="decision",
                content={
                    'market_regime': decision_result.get('market_regime'),
                    'confidence': decision_result.get('confidence'),
                    'trades': decision_result.get('trades', []),
                    'reasoning': decision_result.get('reasoning', '')[:500]
                },
                tags=['decision', trade_date]
            )
            logger.info("[Decision] 决策已保存到记忆")
        except Exception as e:
            logger.warning(f"[Decision] 保存记忆失败: {e}")

    # ------------------------------------------------------------------ #
    #  表结构
    # ------------------------------------------------------------------ #
    def _ensure_table(self):
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS agent_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date VARCHAR(10),
                plan_json TEXT,
                confidence FLOAT,
                market_regime VARCHAR(20),
                generated_at VARCHAR(20)
            )
        """)

    # ------------------------------------------------------------------ #
    #  步骤1：获取量化候选股（支持多策略 ★）
    # ------------------------------------------------------------------ #
    def _get_quant_candidates(self, trade_date: str) -> List[dict]:
        """获取所有策略选股结果

        1. 优先从 daily_picks 读取最近一个有数据的选股结果
        2. 如果数据不足（<20只），自动运行 StrategyCenter 执行所有策略
        daily_picks.trade_date 格式为 YYYYMMDD（无连字符）
        """
        import pandas as pd
        date_compact = trade_date.replace('-', '')

        def _get_latest_picks_date(before_date: str) -> str:
            """获取daily_picks中最近一个有数据的日期"""
            sql = "SELECT DISTINCT trade_date FROM daily_picks WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 1"
            df = DBUtils.query_df(sql, (before_date,))
            if df.empty:
                return None
            return df.iloc[0]['trade_date']

        def _get_latest_trade_date(before_date: str) -> str:
            """获取stock_daily中最近一个有数据的交易日"""
            sql = "SELECT DISTINCT trade_date FROM stock_daily WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 1"
            df = DBUtils.query_df(sql, (before_date,))
            if df.empty:
                return before_date
            return df.iloc[0]['trade_date']

        picks_date = _get_latest_picks_date(date_compact)
        
        try:
            df = None
            # 如果最近的选股数据不足20只，继续向前查找
            while picks_date is not None:
                stock_date = _get_latest_trade_date(picks_date)
                
                sql = """
                    SELECT dp.ts_code, dp.name, dp.final_score, dp.track, dp.industry,
                           dp.ai_score, dp.event_score, dp.fund_score,
                           sd.pe_ttm, sd.roe, sd.netprofit_yoy, sd.total_mv
                    FROM daily_picks dp
                    LEFT JOIN (
                        SELECT ts_code, pe_ttm, roe, netprofit_yoy, total_mv
                        FROM stock_daily
                        WHERE trade_date = ?
                    ) sd ON dp.ts_code = sd.ts_code
                    WHERE dp.trade_date = ?
                    ORDER BY dp.final_score DESC
                """
                df = DBUtils.query_df(sql, (stock_date, picks_date))
                logger.info(f"[Decision] 从 daily_picks 读取 {picks_date} 的 {len(df) if df is not None else 0} 只候选股")
                
                if df is not None and len(df) >= 20:
                    break  # 找到足够数据
                
                # 数据不足，继续向前查找
                prev_date = (datetime.strptime(picks_date, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
                picks_date = _get_latest_picks_date(prev_date)
                logger.warning(f"[Decision] {picks_date} 数据不足，继续向前查找...")
            
            if df is None or df.empty or (len(df) < 20):
                logger.warning(f"[Decision] 无足够选股结果，自动运行策略中心...")
                df = self._run_all_strategies(trade_date)

            if df.empty:
                logger.warning("[Decision] 策略中心也无结果，返回空列表")
                return []

            all_records = df.to_dict('records')
            all_records.sort(key=lambda x: x.get('final_score', 0), reverse=True)
            # 严格控制候选股数量，只取 top 30（避免后续LLM调用过多）
            if len(all_records) > 30:
                logger.info(f"[Decision] 候选股从 {len(all_records)} 只截断到 30 只")
                all_records = all_records[:30]
            logger.info(f"[Decision] 量化候选股 {len(all_records)} 只")
            return all_records

        except Exception as e:
            logger.error(f"[Decision] 查询量化候选股失败: {e}")
            return []

    def _run_all_strategies(self, trade_date: str) -> pd.DataFrame:
        """运行 StrategyCenter 执行所有策略，返回合并结果"""
        try:
            from src.strategy.center import StrategyCenter
            center = StrategyCenter(enable_macro=False, notify=False)

            result = center.run(
                strategies=['hybrid', 'dividend', 'quant', 'small_cap', 'cyclical',
                           'pb_roa', 'index_enhance', 'momentum_short', 'momentum_residual',
                           'earnings_momentum', 'high_roe', 'value'],
                trade_date=trade_date,
                top_k=30,
                ensemble=False
            )

            if result is not None and not result.empty:
                logger.info(f"[Decision] 策略中心返回 {len(result)} 只候选股，开始保存...")
                today_str = datetime.now().strftime('%Y%m%d')
                result = result.drop_duplicates(subset=['ts_code', 'strategy'], keep='first')
                result = result.fillna(0)
                result = result.rename(columns={'score': 'final_score', 'strategy': 'track'})
                result['track'] = result['track'].fillna('unknown')
                result['final_score'] = pd.to_numeric(result.get('final_score', result.get('score', 0)), errors='coerce').fillna(0)
                
                # 安全处理ai_score列
                if 'ai_score' not in result.columns:
                    result['ai_score'] = 0.0
                else:
                    result['ai_score'] = pd.to_numeric(result['ai_score'], errors='coerce').fillna(0)
                
                if 'event_score' not in result.columns:
                    result['event_score'] = 0.0
                else:
                    result['event_score'] = pd.to_numeric(result['event_score'], errors='coerce').fillna(0)
                
                if 'fundamental_score' not in result.columns:
                    result['fundamental_score'] = 0.0
                else:
                    result['fundamental_score'] = pd.to_numeric(result['fundamental_score'], errors='coerce').fillna(0)

# 保存选股结果到 daily_picks
                today_str = datetime.now().strftime('%Y%m%d')
                logger.info(f"[Decision] 保存选股结果: trade_date={today_str}, count={len(result)}")
                
                try:
                    DBUtils.execute("DELETE FROM daily_picks WHERE trade_date = ?", (today_str,))
                    logger.info(f"[Decision] 已清除旧数据")
                    
                    for _, row in result.iterrows():
                        score = float(row.get('final_score', 0)) if not pd.isna(row.get('final_score', 0)) else 0.0
                        ai = float(row.get('ai_score', 0)) if not pd.isna(row.get('ai_score', 0)) else 0.0
                        evt = float(row.get('event_score', 0)) if not pd.isna(row.get('event_score', 0)) else 0.0
                        fund = float(row.get('fundamental_score', 0)) if not pd.isna(row.get('fundamental_score', 0)) else 0.0
                        DBUtils.execute("""
                            INSERT INTO daily_picks (trade_date, ts_code, name, final_score, ai_score, event_score, fund_score, track, industry)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            today_str,
                            str(row.get('ts_code', '')),
                            str(row.get('name', '')),
                            score,
                            ai, evt, fund,
                            str(row.get('track', 'unknown')),
                            str(row.get('industry', ''))
                        ))
                    logger.info(f"[Decision] 策略中心选出 {len(result)} 只股票")
                except Exception as save_err:
                    logger.error(f"[Decision] 保存选股结果失败: {save_err}")
                    import traceback
                    traceback.print_exc()
            else:
                logger.warning("[Decision] 策略中心无结果")

            return result if result is not None else pd.DataFrame()

        except Exception as e:
            logger.error(f"[Decision] 策略中心执行失败: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()

    # ------------------------------------------------------------------ #
    #  步骤2：获取当前持仓
    # ------------------------------------------------------------------ #
    def _get_current_positions(self) -> dict:
        """返回 {ts_code: Position} 字典"""
        try:
            positions = self._broker.get_positions()
            return {p.ts_code: p for p in positions}
        except Exception as e:
            logger.error(f"[Decision] 获取持仓失败: {e}")
            return {}

    # ------------------------------------------------------------------ #
    #  步骤2.1：技术指标补充
    # ------------------------------------------------------------------ #
    def _enrich_with_tech(self, candidates: List[dict]) -> List[dict]:
        """从 stock_factors 读取最新技术指标，补充到候选股
        字段：RSI / MACD / 20日动量 / 52w位置 / 量比 / 布林带宽 / ATR / 质量分 / 成长分
        """
        if not candidates:
            return candidates
        codes = [c['ts_code'] for c in candidates if c.get('ts_code')]
        if not codes:
            return candidates
        ph = ','.join(['?'] * len(codes))
        try:
            df = DBUtils.query_df(
                f"""SELECT sf.ts_code, sf.rsi_14, sf.macd_hist, sf.mom_20,
                           sf.price_pos_52w, sf.vol_ratio,
                           sf.bb_width, sf.atr_14, sf.quality_score, sf.growth_score
                    FROM stock_factors sf
                    INNER JOIN (
                        SELECT ts_code, MAX(trade_date) as max_date
                        FROM stock_factors
                        WHERE ts_code IN ({ph})
                        GROUP BY ts_code
                    ) lm ON sf.ts_code = lm.ts_code AND sf.trade_date = lm.max_date""",
                tuple(codes)
            )
            if df.empty:
                logger.debug("[Decision] stock_factors 无数据，跳过技术指标补充")
                return candidates
            tech = {str(r['ts_code']): r for _, r in df.iterrows()}
            for c in candidates:
                t = tech.get(c.get('ts_code', ''), {})
                c['rsi_14']        = round(float(t.get('rsi_14', 50) or 50), 1)
                c['macd_hist']     = round(float(t.get('macd_hist', 0) or 0), 4)
                c['mom_20']        = round(float(t.get('mom_20', 0) or 0) * 100, 1)    # → %
                c['price_pos_52w'] = round(float(t.get('price_pos_52w', 0.5) or 0.5) * 100, 1)  # → %
                c['vol_ratio']     = round(float(t.get('vol_ratio', 1) or 1), 2)
                c['bb_width']      = round(float(t.get('bb_width', 0) or 0), 4)
                c['atr_14']        = round(float(t.get('atr_14', 0) or 0), 3)
                c['quality_score'] = round(float(t.get('quality_score', 0) or 0), 3)
                c['growth_score']  = round(float(t.get('growth_score', 0) or 0), 3)
            logger.info(f"[Decision] 技术指标补充完成 {len(tech)} 只")
        except Exception as e:
            logger.warning(f"[Decision] 技术指标补充失败: {e}")
        return candidates

    # ------------------------------------------------------------------ #
    #  步骤3：LLM批量分析候选股
    # ------------------------------------------------------------------ #
    # 轨道标签说明（用于提示词）
    _TRACK_LABEL = {
        'sector_rotation': '[A轨-动量]',
        'cyclical': '[A轨-周期]',
        'momentum_short': '[A轨-动量]',
        'momentum_residual': '[A轨-动量]',
        'earnings_momentum': '[A轨-成长]',
        'high_roe': '[A轨-成长]',
        'index_enhance': '[A轨-增强]',
        'small_cap': '[A轨-小盘]',
        'value': '[B轨-价值]',
        'dividend': '[B轨-红利]',
        'quant': '[B轨-量化]',
        'pb_roa': '[B轨-价值]',
        'hybrid': '[AB混合]',
        'both': '[AB双轨]',
    }

    def _build_candidate_table(self, candidates: List[dict]) -> str:
        """将候选股列表格式化为表格字符串（含轨道标签 + 技术指标）"""
        has_tech = any('rsi_14' in c for c in candidates)
        if has_tech:
            header = ("代码 | 名称 | 轨道 | 量化得分 | 行业 | PE | ROE | 净利YoY | 市值(亿)"
                      " | RSI | MACD | 20日涨 | 52w位 | BB宽 | ATR | 质量分 | 成长分")
        else:
            header = "代码 | 名称 | 轨道 | 量化得分 | 行业 | PE | ROE | 净利YoY | 市值(亿)"
        rows = [header, '-' * (160 if has_tech else 90)]
        for c in candidates:
            total_mv = c.get('total_mv', 0)
            mv_yi = round(float(total_mv) / 1e8, 1) if total_mv else 0
            track_raw = str(c.get('track', ''))
            track_label = self._TRACK_LABEL.get(track_raw, track_raw)
            row = (f"{c.get('ts_code','')} | {c.get('name','')} | "
                   f"{track_label} | {c.get('final_score', 0):.3f} | "
                   f"{c.get('industry','')} | {c.get('pe_ttm', '-')} | "
                   f"{c.get('roe', '-')} | {c.get('netprofit_yoy', '-')} | {mv_yi}")
            if has_tech:
                rsi = c.get('rsi_14', 50)
                rsi_tag = '⚠超买' if rsi > 70 else ('⚡超卖' if rsi < 30 else '')
                macd_hist = c.get('macd_hist', 0)
                macd_str = '↑' if macd_hist > 0 else '↓'
                mom = c.get('mom_20', 0)
                pos52 = c.get('price_pos_52w', 50)
                bb = c.get('bb_width', 0)
                # 布林带宽：相对值，用★标注是否处于收窄（低于0.05=压缩蓄势）
                bb_tag = '★压缩' if bb < 0.05 else ('⚡扩张' if bb > 0.15 else '')
                atr = c.get('atr_14', 0)
                qs = c.get('quality_score', 0)
                gs = c.get('growth_score', 0)
                row += (f" | {rsi:.0f}{rsi_tag} | {macd_str} | {mom:+.1f}%"
                        f" | {pos52:.0f}% | {bb:.3f}{bb_tag} | {atr:.2f}"
                        f" | {qs:.2f} | {gs:.2f}")
            rows.append(row)
        return '\n'.join(rows)

    def _parse_analysis_scores(self, response: str, candidates: List[dict]) -> List[dict]:
        """从 LLM 分析文本中提取每只股票的操作评分（1-10）
        优先尝试正则+JSON解析兜底
        """
        import json as _json
        result = []
        
        # 策略1：优先尝试从response中提取JSON数组
        try:
            # 找[...]区间
            start = response.find('[')
            end = response.rfind(']') + 1
            if start != -1 and end > start:
                json_str = response[start:end]
                data = _json.loads(json_str)
                if isinstance(data, list):
                    # 建 score 索引
                    score_idx = {c.get('ts_code'): c.get('score', 5) for c in data if isinstance(c, dict)}
                    for cand in candidates:
                        cand = dict(cand)
                        cand['llm_score'] = score_idx.get(cand.get('ts_code'), 5)
                        result.append(cand)
                    if result:
                        logger.info(f"[Decision] JSON解析成功 {len(result)} 只")
                        return result
        except Exception:
            pass
        
        # 策略2：正则提取兜底（原有逻辑）
        for c in candidates:
            ts_code = c.get('ts_code', '')
            name = c.get('name', '')
            score = 5  # 默认中性

            # 尝试从回复中找到对应股票的评分，如"操作评分: 8" 或 "8/10" 或 "评分：7"
            # 先按代码匹配，再按名字匹配
            search_keys = [ts_code, name]
            for key in search_keys:
                if not key:
                    continue
                # 在该 key 附近（±200字符）找评分数字
                idx = response.find(key)
                if idx == -1:
                    continue
                snippet = response[max(0, idx-50):idx+300]
                # 匹配 "评分.*?(\d+)" 或 "(\d+)/10" 或 "(\d+)分"
                patterns = [
                    r'操作评分[：:]\s*(\d+)',
                    r'评分[：:]\s*(\d+)',
                    r'(\d+)\s*/\s*10',
                    r'(\d+)\s*分',
                ]
                for pat in patterns:
                    m = re.search(pat, snippet)
                    if m:
                        try:
                            score = int(m.group(1))
                            score = max(1, min(10, score))
                        except (ValueError, IndexError):
                            pass
                        break
                break

            c_copy = dict(c)
            c_copy['llm_score'] = score
            result.append(c_copy)

        return result

    def _fetch_news_for_candidates(self, candidates: List[dict]) -> str:
        """从 news_cache 读取候选股近48小时相关新闻，生成 LLM 提示词摘要
        优先用本地缓存（毫秒级），缓存无数据时 fallback 到 web_search
        """
        import json as _json
        try:
            from src.utils.db_utils import DBUtils as _DB
            cutoff = (datetime.now() - timedelta(hours=48)).strftime('%Y-%m-%d %H:%M:%S')
            df = _DB.query_df(
                """SELECT title, summary, source, published_at, matched_stocks
                   FROM news_cache
                   WHERE fetched_at >= ? AND matched_stocks != '[]'
                   ORDER BY published_at DESC LIMIT 300""",
                (cutoff,)
            )
            if not df.empty:
                # 建 ts_code → [news] 索引
                code_news: dict = {}
                for _, row in df.iterrows():
                    try:
                        matched = _json.loads(row['matched_stocks'] or '[]')
                    except Exception:
                        continue
                    pub_time = str(row['published_at'])[5:16]
                    snippet = f"[{pub_time} {row['source']}] {row['title']}"
                    if row['summary']:
                        snippet += f" — {str(row['summary'])[:80]}"
                    for m in matched:
                        code = m.get('ts_code', '')
                        if code not in code_news:
                            code_news[code] = []
                        code_news[code].append(snippet)

                lines = []
                for c in candidates:
                    ts_code = c.get('ts_code', '')
                    name = c.get('name', '')
                    snippets = code_news.get(ts_code, [])
                    if snippets:
                        lines.append(f"\n### {name}({ts_code}) 近期新闻")
                        lines.extend(f"  · {s}" for s in snippets[:5])

                if lines:
                    logger.debug(f"[Decision] news_cache 命中 {len([c for c in candidates if c.get('ts_code','') in code_news])} 只候选股")
                    return '\n'.join(lines)
        except Exception as e:
            logger.debug(f"[Decision] news_cache 读取失败: {e}")

        # Fallback：web_search（实时，较慢）
        try:
            from src.utils.web_search import search_stock_news, format_for_llm
            lines = []
            for c in candidates[:6]:
                ts_code = c.get('ts_code', '')
                name = c.get('name', '')
                if not ts_code:
                    continue
                news = search_stock_news(ts_code, name, max_results=4)
                if news:
                    lines.append(f"\n### {name}({ts_code}) 最新动态")
                    lines.append(format_for_llm(news, max_chars=400))
            return '\n'.join(lines) if lines else ''
        except Exception as e:
            logger.debug(f"[Decision] 候选股新闻搜索失败: {e}")
            return ''

    def _analyze_candidates(self, candidates: List[dict]) -> List[dict]:
        """
        [已弃用] 原分批调用 LLM 分析候选股，不再从 run() 调用
        保留以兼容外部直接调用。
        """
        if not candidates:
            return []
        
        logger.info(f"[Decision] _analyze_candidates 收到 {len(candidates)} 只候选股")

        # 缓存检查：同一天同一批候选股不重复调用LLM
        codes_tuple = tuple(sorted(c.get('ts_code', '') for c in candidates))
        cache_key = f"{len(candidates)}_{hash(codes_tuple)}"
        if cache_key in self._analysis_cache:
            logger.info(f"[Decision] 命中缓存，直接返回 {len(self._analysis_cache[cache_key])} 只分析结果")
            return self._analysis_cache[cache_key]

        # 每批最多30只
        batch_size = 30
        all_results = []

        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i+batch_size]
            table = self._build_candidate_table(batch)

            # 判断本批次包含哪些轨道，动态调整分析重点
            tracks = {c.get('track', '') for c in batch}
            has_a = 'sector_rotation' in tracks or 'both' in tracks
            has_b = 'value' in tracks or 'dividend' in tracks or 'both' in tracks

            track_guidance = ""
            if has_a and has_b:
                track_guidance = (
                    "\n【分析框架】\n"
                    "- [A轨-AI赛道]股票：\n"
                    "  ① 判断所属AI五层(Layer1算力/Layer2基础设施/Layer3云网络/Layer4应用/Layer5机器人)中的景气位置\n"
                    "  ② 有无直接AI收入确认（订单、合同、量产交付）—— 确定性关键\n"
                    "  ③ 近期是否有催化剂（大厂采购消息/产品发布/政策落地）\n"
                    "  ④ PEG是否合理（<1.5），避免估值泡沫已充分透支预期的个股\n"
                    "  ⑤ 操作评分：10=AI主收入+景气上行+估值合理；1=纯概念无业绩+高估值\n"
                    "- [B轨-红利]股票：\n"
                    "  ① 近3年股息率是否≥4%且稳定（无大幅缩减）\n"
                    "  ② 派息率是否合理（30%-70%之间，过高说明无法持续）\n"
                    "  ③ ROE是否≥8%且趋势稳定或向上\n"
                    "  ④ PE/PB是否低于历史中位数，提供足够安全边际\n"
                    "  ⑤ 操作评分：10=高股息+ROE稳定+低估值+行业护城河；1=股息率骤降+ROE恶化\n"
                    "- [AB双轨]股票：AI+红利双重属性（如某些云计算基础设施公司），优先级最高，评分可适当上调。\n"
                )
            elif has_a:
                track_guidance = (
                    "\n【分析框架】这批均为A轨AI赛道股，按以下要点打分：\n"
                    "① 所属AI五层(Layer1算力/Layer2基础设施/Layer3云网络/Layer4应用/Layer5机器人)景气度\n"
                    "② AI收入确认度（直接收入>间接受益>纯概念）—— 确定性是核心\n"
                    "③ 近期催化剂（大厂订单/产品量产/政策/AI大会）\n"
                    "④ 估值（PEG<1.5为宜，警惕纯主题炒作脱离基本面）\n"
                    "评分：10=AI主营+景气上行+有催化+估值合理；5=AI受益但收入占比小；1=概念炒作无实质。\n"
                )
            elif has_b:
                track_guidance = (
                    "\n【分析框架】这批均为B轨红利股，按以下要点打分：\n"
                    "① 股息率稳定性：近3年是否维持≥4%（银行/能源/消费各有合理区间）\n"
                    "② 派息可持续性：派息率30%-70%、自由现金流覆盖分红\n"
                    "③ ROE质量：≥8%且稳定，警惕ROE因财务杠杆虚高\n"
                    "④ 估值安全边际：PE/PB低于历史20%分位为优选买点\n"
                    "⑤ 行业护城河：品牌/政策/资源垄断等稳定现金流来源\n"
                    "评分：10=高股息+ROE好+低估值+护城河；5=股息尚可但成长性弱；1=股息不稳+ROE恶化。\n"
                )

            # 搜索候选股新闻（合并获取，减少调用）
            # 不再传给LLM，减少prompt长度
            # news_context = self._fetch_news_for_candidates(batch)
            # news_section = f"\n## 候选股最新新闻\n{news_context}\n" if news_context else ""

            # 简化技术指标说明（只留关键）
            tech_hint = (
                "\n【参考指标】RSI<30超卖/ >70超买；MACD↑金叉/↓死叉；"
                "52w位置>80%年高压力/<20%年低支撑；ATR高波动大/低震荡。\n"
            ) if any('rsi_14' in c for c in batch) else ""

            prompt = (
                f"请为以下{len(batch)}只候选股逐一给出操作评分(1-10分，10=强烈推荐买入)。\n"
                f"{track_guidance}"
                f"{tech_hint}"
                f"务必分析全部{len(batch)}只，不要遗漏。\n"
                "输出严格的JSON数组，每只股票格式："
                "[{\"ts_code\":\"xxx\",\"score\":8}, ...]\n"
                "不要输出其他文字，只输出JSON数组。\n"
                "候选股列表（供参考）：\n"
                f"{table}"
            )
            
            batch_num = i//batch_size + 1
            total_batches = (len(candidates) + batch_size - 1) // batch_size
            logger.info(f"[Decision] LLM分析 批次{batch_num}/{total_batches}（{len(batch)}只）")
            
            response = self._router.analyze(prompt, max_tokens=4000, timeout=120.0)  # 30只约需4000 tokens
            
            if not response:
                logger.warning(f"[Decision] 第{batch_num}批分析无响应，使用默认评分5")
                for c in batch:
                    c['llm_score'] = 5
                all_results.extend(batch)
            else:
                scored = self._parse_analysis_scores(response, batch)
                all_results.extend(scored)
                logger.info(f"[Decision] 第{batch_num}批完成 {len(scored)}/{len(batch)} 只")
        
        # 存入缓存（避免重复分析）
        self._analysis_cache[cache_key] = all_results
        logger.info(f"[Decision] 结果已缓存 key={cache_key[:50]}...")
        
        return all_results

    # ------------------------------------------------------------------ #
    #  步骤3.5：读取 ETF / 期货 / 价值选股 上下文
    # ------------------------------------------------------------------ #
    def _get_etf_futures_context(self, trade_date: str) -> str:
        """
        读取已生成的 ETF 选股和价值选股 CSV，提炼行业信号和期货信号，
        作为市场背景注入决策 prompt。只读本地文件，不发起网络请求。
        """
        lines = []

        # ── 1. ETF 行业信号 ──────────────────────────────────────────────
        try:
            import pandas as pd
            output_dir = os.path.join(_PROJECT_ROOT, 'output')
            etf_files = sorted(glob.glob(os.path.join(output_dir, 'etf_picks_*.csv')), reverse=True)
            if etf_files:
                df = pd.read_csv(etf_files[0], encoding='utf-8-sig')
                # 必要列检查
                if {'industry', 'name', 'signal', 'score'}.issubset(df.columns):
                    # 取信号≠HOLD 的前8行，或全取前6行
                    active = df[df['signal'].astype(str).str.upper() != 'HOLD'].head(8)
                    if active.empty:
                        active = df.head(6)
                    etf_date = str(df['date'].iloc[0]) if 'date' in df.columns else os.path.basename(etf_files[0])
                    lines.append(f"## ETF 行业信号（{etf_date}，来自 ETF 抄底/动量策略）")
                    lines.append("行业 | ETF名称 | 信号 | 得分 | 策略 | 期货确认")
                    for _, row in active.iterrows():
                        fut_sig = str(row.get('futures_signal', '')).strip()
                        fut_reason = str(row.get('futures_reason', '')).strip()
                        fut_str = f"{fut_sig}（{fut_reason[:20]}）" if fut_sig and fut_sig != 'nan' else '-'
                        strat = str(row.get('strategy', '')).replace(' | ', '/').strip()[:20]
                        lines.append(
                            f"{row.get('industry','')} | {row.get('name','')} | "
                            f"{row.get('signal','')} | {row.get('score',0):.0f} | "
                            f"{strat} | {fut_str}"
                        )
                    lines.append("")
        except Exception as e:
            logger.debug(f"[Decision] 读取ETF信号失败: {e}")

        # ── 2. 期货品种涨跌（从 ETF CSV 提取，无需额外API） ──────────────
        try:
            import pandas as pd
            output_dir = os.path.join(_PROJECT_ROOT, 'output')
            etf_files = sorted(glob.glob(os.path.join(output_dir, 'etf_picks_*.csv')), reverse=True)
            if etf_files:
                df = pd.read_csv(etf_files[0], encoding='utf-8-sig')
                # 找有期货信号的行
                if 'futures_sector' in df.columns and 'futures_score' in df.columns:
                    fdf = df[df['futures_sector'].notna() & (df['futures_sector'].astype(str) != 'nan')].drop_duplicates('futures_sector')
                    if not fdf.empty:
                        lines.append("## 期货板块得分（驱动ETF信号）")
                        for _, row in fdf.iterrows():
                            score_val = row.get('futures_score', 0)
                            reason = str(row.get('futures_reason', '')).strip()
                            direction = '[看涨]' if float(score_val or 0) > 0 else ('[看跌]' if float(score_val or 0) < 0 else '[中性]')
                            lines.append(f"  {direction} {row['futures_sector']}  得分={float(score_val or 0):.2f}  {reason[:40]}")
                        lines.append("")
        except Exception as e:
            logger.debug(f"[Decision] 读取期货板块得分失败: {e}")

        # ── 3. 价值选股（B轨独立参考）────────────────────────────────────
        try:
            import pandas as pd
            output_dir = os.path.join(_PROJECT_ROOT, 'output')
            val_files = sorted(glob.glob(os.path.join(output_dir, 'value_picks_*.csv')), reverse=True)
            if val_files:
                df = pd.read_csv(val_files[0], encoding='utf-8-sig')
                val_date = os.path.basename(val_files[0]).replace('value_picks_', '').replace('.csv', '')
                # 取前6只
                top = df.head(6)
                if not top.empty:
                    lines.append(f"## 价值策略独立选股（{val_date}，可与B轨候选交叉验证）")
                    code_col = next((c for c in top.columns if 'code' in c.lower() or 'ts_code' in c.lower()), None)
                    name_col = next((c for c in top.columns if 'name' in c.lower()), None)
                    score_col = next((c for c in top.columns if 'score' in c.lower()), None)
                    if code_col and name_col:
                        for _, row in top.iterrows():
                            score_str = f"  得分={float(row[score_col]):.3f}" if score_col else ""
                            lines.append(f"  {row[code_col]} {row[name_col]}{score_str}")
                    lines.append("")
        except Exception as e:
            logger.debug(f"[Decision] 读取价值选股失败: {e}")

        return '\n'.join(lines) if lines else ''

    # ------------------------------------------------------------------ #
    #  步骤3.6：政策背景（gov_news）
    # ------------------------------------------------------------------ #
    def _get_policy_context(self) -> str:
        """读取近48小时政府网站政策信号，格式化为决策 prompt 片段"""
        try:
            from src.collector.gov_news_fetcher import get_recent_signals
            signals = get_recent_signals(hours=48)
            if not signals:
                return ''
            lines = ["## 近期政策信号（来自政府官网，48小时内）"]
            # 先输出利多/利空，再中性
            for sentiment_label, filter_val in [('利多', 'positive'), ('利空', 'negative'), ('中性', 'neutral')]:
                group = [s for s in signals if s.get('sentiment') == filter_val]
                if not group:
                    continue
                lines.append(f"\n**{sentiment_label}政策**")
                for s in group[:5]:  # 每类最多5条
                    pub = str(s.get('published_at') or '')[:10]
                    src = s.get('source_name', '')
                    tags = s.get('sector_tags', '')
                    summary = s.get('llm_summary', '') or s.get('title', '')[:40]
                    lines.append(f"  [{pub} {src}] {summary}  → 板块:{tags}")
            lines.append(
                "\n（政策信号参考：利多政策支持加仓相关板块；利空政策规避相关板块；"
                "确认性强的政策（已颁布>预期中>征求意见稿）权重更高）"
            )
            return '\n'.join(lines)
        except Exception as e:
            logger.debug(f"[Decision] 政策信号读取失败: {e}")
            return ''

    # ------------------------------------------------------------------ #
    #  步骤3.7：市场情绪背景
    # ------------------------------------------------------------------ #
    def _get_market_sentiment(self) -> str:
        """
        采集三类A股市场情绪指标，返回格式化文本注入决策 prompt：
          1. 北向资金近5日净流入/流出及连续方向
          2. 全市场涨停/跌停数（今日 & 近5日均值）
          3. 全市场成交额 vs 20日均线偏离度
        只读本地 DB，不发起网络请求；任一来源失败均降级跳过。
        """
        lines = ["## 市场情绪背景"]
        any_data = False

        # ── 1. 北向资金 ────────────────────────────────────────────────────
        try:
            df_nb = DBUtils.query_df(
                """SELECT trade_date, north_net_inflow, north_acc_inflow
                   FROM northbound_flow ORDER BY trade_date DESC LIMIT 20"""
            )
            if not df_nb.empty:
                grp = df_nb.set_index('trade_date')['north_net_inflow'].sort_index(ascending=False)
                recent5 = grp.head(5)
                total5 = recent5.sum() / 1e8
                today_flow = recent5.iloc[0] / 1e8 if len(recent5) > 0 else 0
                consecutive = 0
                first_val = recent5.iloc[0] if len(recent5) > 0 else 0
                for v in recent5:
                    if (v > 0) == (first_val > 0):
                        consecutive += 1
                    else:
                        break
                sentiment_nb = '看多信号' if total5 > 0 else '看空信号'
                direction = '净流入' if first_val > 0 else '净流出'
                lines.append(
                    f"北向资金：今日{today_flow:+.1f}亿，近5日合计{total5:+.1f}亿，"
                    f"连续{consecutive}日{direction}（{sentiment_nb}）"
                )
                any_data = True
        except Exception as e:
            logger.debug(f"[Decision] 北向资金读取失败: {e}")

        # ── 2. 涨停/跌停数 ──────────────────────────────────────────────────
        try:
            cutoff_7d = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
            df_lim = DBUtils.query_df(
                """SELECT trade_date,
                          SUM(CASE WHEN (close - open) / open * 100 >= 9.9 THEN 1 ELSE 0 END) AS limit_up,
                          SUM(CASE WHEN (close - open) / open * 100 <= -9.9 THEN 1 ELSE 0 END) AS limit_down
                   FROM stock_daily
                   WHERE trade_date >= ?
                   GROUP BY trade_date
                   ORDER BY trade_date DESC
                   LIMIT 5""",
                (cutoff_7d,)
            )
            if not df_lim.empty:
                today_row = df_lim.iloc[0]
                lu = int(today_row['limit_up'])
                ld = int(today_row['limit_down'])
                ratio = lu / max(ld, 1)
                avg_lu5 = df_lim['limit_up'].mean()
                avg_ld5 = df_lim['limit_down'].mean()
                sentiment_lim = '情绪偏热' if ratio >= 2 else ('情绪偏冷' if ratio <= 0.5 else '情绪中性')
                lines.append(
                    f"涨跌停比：今日涨停{lu}家/跌停{ld}家，比值={ratio:.1f}（{sentiment_lim}）；"
                    f"近5日均值 涨停{avg_lu5:.0f}/跌停{avg_ld5:.0f}"
                )
                any_data = True
        except Exception as e:
            logger.debug(f"[Decision] 涨跌停数读取失败: {e}")

        # ── 3. 全市场成交额偏离度 ───────────────────────────────────────────
        try:
            cutoff_30d = (datetime.now() - timedelta(days=35)).strftime('%Y-%m-%d')
            df_amt = DBUtils.query_df(
                """SELECT trade_date, SUM(amount) AS total_amount
                   FROM stock_daily
                   WHERE trade_date >= ?
                   GROUP BY trade_date
                   ORDER BY trade_date DESC
                   LIMIT 25""",
                (cutoff_30d,)
            )
            if not df_amt.empty and len(df_amt) >= 5:
                # 用相对偏离，不依赖绝对单位
                ma20 = df_amt['total_amount'].iloc[:20].mean()
                deviation = (df_amt['total_amount'].iloc[0] - ma20) / ma20 * 100
                sentiment_amt = '成交放量' if deviation > 20 else ('成交萎缩' if deviation < -20 else '成交平稳')
                lines.append(f"全市场成交额：相对20日均线偏离{deviation:+.1f}%（{sentiment_amt}）")
                any_data = True
        except Exception as e:
            logger.debug(f"[Decision] 成交额偏离读取失败: {e}")

        if not any_data:
            return ''

        lines.append("（市场情绪仅供参考，北向资金持续净流入+情绪热+成交放量 = 偏多；反之偏谨慎）")
        return '\n'.join(lines)

    # ------------------------------------------------------------------ #
    #  步骤3.8：热点行业/概念背景
    # ------------------------------------------------------------------ #
    def _get_hot_sector_concept_context(self, trade_date: str) -> str:
        """
        采集热点行业和热点概念（当日涨幅排名），作为市场背景注入 prompt。
        只读本地 DB，不发起网络请求。
        """
        lines = []
        any_data = False

        # ── 1. 热点行业（Python计算涨幅避免SQL collation问题）─────────────────
        try:
            today_df = DBUtils.query_df(
                f"SELECT ts_code, close FROM stock_daily WHERE trade_date = '{trade_date}'"
            )
            if not today_df.empty:
                prev_df = DBUtils.query_df(f"""
                    SELECT t1.ts_code, t1.close
                    FROM stock_daily t1
                    INNER JOIN (
                        SELECT ts_code, MAX(trade_date) as max_dt
                        FROM stock_daily WHERE trade_date < '{trade_date}'
                        GROUP BY ts_code
                    ) t2 ON t1.ts_code = t2.ts_code AND t1.trade_date = t2.max_dt
                """)
                if not prev_df.empty:
                    prev_df = prev_df.rename(columns={'close': 'prev_close'})
                    merged = today_df.merge(prev_df, on='ts_code', how='inner')
                    merged = merged[merged['prev_close'] > 0]
                    merged['pct_chg'] = (merged['close'] - merged['prev_close']) / merged['prev_close'] * 100

                    info_df = DBUtils.query_df(
                        "SELECT ts_code, industry FROM stock_info WHERE industry IS NOT NULL AND industry != ''"
                    )
                    if not info_df.empty:
                        merged = merged.merge(info_df, on='ts_code', how='inner')
                        if not merged.empty:
                            sector_df = merged.groupby('industry')['pct_chg'].mean().reset_index()
                            sector_df = sector_df.sort_values('pct_chg', ascending=False).head(10)
                            if not sector_df.empty:
                                sectors = ', '.join(sector_df['industry'].tolist()[:5])
                                lines.append(f"热点行业（涨幅Top5）: {sectors}")
                                any_data = True
        except Exception as e:
            logger.debug(f"[Decision] 热点行业查询失败: {e}")

        # ── 2. 热点概念 ─────────────────────────────────────────────────────────
        try:
            df_conc = DBUtils.query_df(f"""
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
                LIMIT 10
            """, (trade_date, trade_date))
            if not df_conc.empty:
                concepts = ', '.join(df_conc['concept_name'].tolist()[:5])
                lines.append(f"热点概念（涨幅Top5）: {concepts}")
                any_data = True
        except Exception as e:
            logger.debug(f"[Decision] 热点概念查询失败: {e}")

        if not any_data:
            return ''

        lines.append("（热点行业/概念仅供 LLM 选股参考，非强制买入信号）")
        return '\n'.join(lines)

    # ------------------------------------------------------------------ #
    #  步骤4：R1综合推理生成交易计划
    # ------------------------------------------------------------------ #
    def _positions_summary(self, positions: dict, stop_loss_pct: float = 0.08,
                          track_map: dict = None) -> str:
        """持仓摘要，含止损位、距止损空间、持仓天数和轨道（A轨最短5天，B轨最短15天）

        Args:
            stop_loss_pct: 止损比例（默认0.08=8%）
            track_map: {ts_code: 'sector_rotation'|'dividend'|'value'|'both'} 来自 daily_picks

        Returns:
            str: 格式化的持仓摘要文本
        """
        if not positions:
            return "（当前无持仓）"
        track_map = track_map or {}
        today = datetime.now().date()
        MIN_HOLD = {'sector_rotation': 5, 'dividend': 15, 'value': 15, 'both': 5}
        lines = ["代码 | 名称 | 轨道 | 持仓天数 | 最短要求 | 可操作 | 成本 | 现价 | 盈亏% | 止损价 | 距止损"]
        for p in positions.values():
            stop_price = round(p.cost * (1 - stop_loss_pct), 2)
            gap_to_stop = (p.current_price - stop_price) / p.current_price * 100 if p.current_price > 0 else 0
            gap_str = f"{gap_to_stop:+.1f}%"
            days_held_n = None
            if p.buy_date:
                try:
                    buy_dt = datetime.strptime(p.buy_date[:10], '%Y-%m-%d').date()
                    days_held_n = (today - buy_dt).days
                except Exception:
                    pass
            days_held = f"{days_held_n}天" if days_held_n is not None else "?"
            track = track_map.get(p.ts_code, '')
            track_short = {'sector_rotation': 'A轨', 'dividend': 'B轨',
                           'value': 'B轨', 'both': 'AB'}.get(track, '?')
            min_days = MIN_HOLD.get(track, 5)
            actionable = '✅可操作' if (days_held_n is not None and days_held_n >= min_days) else f'⛔锁定({min_days}天)'
            lines.append(
                f"{p.ts_code} | {p.name} | {track_short} | {days_held} | "
                f"≥{min_days}天 | {actionable} | "
                f"{p.cost:.2f} | {p.current_price:.2f} | {p.profit_pct:+.1f}% | "
                f"{stop_price:.2f} | {gap_str}"
            )
        return '\n'.join(lines)

    def _synthesize_decision(self, candidates: List[dict],
                             positions: dict,
                             memory_context: str,
                             trade_date: str,
                             trade_freq: dict = None,
                             quant_sell_signals: dict = None,
                             quant_buy_signals: dict = None,
                             quant_trades: dict = None) -> dict:
        """[已弃用] 原调用 LLM 验证/调整量化交易计划，不再从 run() 调用
        保留以兼容外部直接调用。
        """
        # 如果有量化交易计划，先用这个作为基础
        if quant_trades and quant_trades.get('trades'):
            base_trades = quant_trades['trades']
            quant_buy_count = quant_trades.get('quant_buy_count', 0)
            quant_sell_count = quant_trades.get('quant_sell_count', 0)
            logger.info(f"[Decision] 基于量化交易计划: 买入{quant_buy_count}只, 卖出{quant_sell_count}只")
        else:
            base_trades = []
        
        # LLM作为验证层：检查量化计划是否合理，可调整但不能完全推翻
        # 从配置读止损参数，用于持仓摘要和提示词
        risk_cfg = Config.get('trading_agent.risk') or {}
        stop_loss_pct = abs(float(risk_cfg.get('stop_loss', 0.08)))
        trailing_stop_pct = abs(float(risk_cfg.get('trailing_stop', 0.05)))

        candidate_table = self._build_candidate_table(candidates)

        # 分A/B轨分别列出候选，让LLM按策略配置仓位
        a_track = [c for c in candidates if c.get('track') in (
            'sector_rotation', 'cyclical', 'momentum_short', 'momentum_residual',
            'earnings_momentum', 'high_roe', 'index_enhance', 'small_cap', 'both'
        )]
        b_track = [c for c in candidates if c.get('track') in (
            'value', 'dividend', 'quant', 'pb_roa', 'hybrid'
        )]

        track_summary_lines = [f"A轨(动量/成长) {len(a_track)} 只: " +
                               ', '.join(f"{c['name']}({c['ts_code']})" for c in a_track)]
        track_summary_lines.append(f"B轨(价值/红利) {len(b_track)} 只: " +
                                   ', '.join(f"{c['name']}({c['ts_code']})" for c in b_track))

        # LLM评分表
        score_lines = ["代码 | 轨道 | 量化得分 | LLM评分(1-10)"]
        for c in candidates:
            track_label = self._TRACK_LABEL.get(c.get('track', ''), c.get('track', ''))
            score_lines.append(
                f"{c.get('ts_code','')} | {track_label} | "
                f"{c.get('final_score', 0):.3f} | {c.get('llm_score', 5)}"
            )
        score_table = '\n'.join(score_lines)

        # 从 daily_picks 查各持仓股票的轨道（最近一次出现在哪条轨道）
        track_map: dict = {}
        try:
            pos_codes = list(positions.keys())
            if pos_codes:
                ph = ','.join(['?'] * len(pos_codes))
                tk_df = DBUtils.query_df(
                    f"SELECT ts_code, track FROM daily_picks WHERE ts_code IN ({ph})"
                    f" AND trade_date = (SELECT MAX(trade_date) FROM daily_picks)",
                    tuple(pos_codes)
                )
                if not tk_df.empty:
                    track_map = dict(zip(tk_df['ts_code'].astype(str), tk_df['track'].astype(str)))
        except Exception:
            pass

        positions_text = self._positions_summary(positions, stop_loss_pct=stop_loss_pct,
                                                  track_map=track_map)

        # ETF / 期货 / 价值选股 市场背景
        etf_futures_context = self._get_etf_futures_context(trade_date)

        # 政策背景（政府官网 gov_news）
        policy_context = self._get_policy_context()

        # 市场情绪背景（北向资金/涨跌停/成交额偏离）
        market_sentiment = self._get_market_sentiment()

        # 热点行业/概念背景
        hot_sector_concept = self._get_hot_sector_concept_context(trade_date)

        account = self._broker.get_account()
        account_text = (
            f"总资产: {account.total_assets:,.0f}元  "
            f"现金: {account.cash:,.0f}元  "
            f"持仓市值: {account.market_value:,.0f}元  "
            f"当前仓位: {account.market_value/account.total_assets*100:.0f}%"
            if account.total_assets > 0 else "总资产: 0"
        )

        system = (
            "你是一位职业基金经理，管理一个A股「双轨」量化+AI混合组合，核心投资逻辑是：\n"
            "\n"
            "【A轨——AI赛道确定性投资】\n"
            "  当前最重要的主线：AI算力→基础设施→云网络→大模型应用→机器人/物理AI 五层价值链，"
            "  全球AI资本开支持续高增（英伟达/微软/谷歌持续加码），中国AI政策全力支持。\n"
            "  五层映射：Layer1(算力芯片)→Layer2(AI基础设施/服务器/PCB)→Layer3(云/网络/IDC)"
            "  →Layer4(大模型/AI应用/SaaS)→Layer5(机器人/具身智能/工业AI)。\n"
            "  评估框架：①所在Layer的景气度是否处于上行期；②公司是否有AI直接收入/订单确认；"
            "  ③技术壁垒（专利/独家客户/规模效应）；④估值是否反映了中期增速（PEG<1.5为宜）。\n"
            "  注意：AI赛道轮动快，Layer热度须跟踪市场最新共识，避免押注已见顶的Layer。\n"
            "\n"
            "【B轨——红利资产稳定性投资】\n"
            "  核心逻辑：利率下行周期中，高股息率资产相对债券具有吸引力，适合长期底仓配置。\n"
            "  三个子赛道：\n"
            "    • 银行红利：关注净息差稳定性、不良贷款率、股息率（>4%为佳）、H股折价机会\n"
            "    • 能源红利：关注分红承诺持续性、现金流覆盖率、大宗商品价格中枢、政策导向\n"
            "    • 消费红利：关注品牌护城河、ROE稳定性（>10%）、自由现金流、估值（PB合理区间）\n"
            "  评估框架：①近3年股息率是否稳定且可持续；②派息率不超过70%（留有余量）；"
            "  ③ROE≥8%且趋势向好；④PE/PB处于历史20%分位以下为理想买点。\n"
            "\n"
            "决策原则：A轨顺AI主线趋势布局、确定性优先；B轨逢低收集红利资产、稳定性优先；"
            "控制回撤，A轨止损严格（-8%），B轨可适当放宽（-10%，等基本面恶化信号）。"
        )

        # ETF/期货 section（有数据才插入）
        etf_section = (
            f"\n{etf_futures_context}\n"
            "（ETF行业信号用于：①确认A轨动量真实性；②期货涨跌判断行业景气；③价值选股交叉验证B轨候选）\n"
            if etf_futures_context else ""
        )

        # 政策 section（有数据才插入）
        policy_section = f"\n{policy_context}\n" if policy_context else ""

        # 市场情绪 section（有数据才插入）
        sentiment_section = f"\n{market_sentiment}\n" if market_sentiment else ""

        # 热点行业/概念 section（有数据才插入）
        hot_sector_section = f"\n{hot_sector_concept}\n" if hot_sector_concept else ""

        # 交易频率信息（按周调仓节奏）
        freq = trade_freq or {}
        week_buys = freq.get('week_buys', 0)
        month_buys = freq.get('month_buys', 0)
        year_buys = freq.get('year_buys', 0)
        # 本周买入配额（全年 ≤ 50 次）
        # week_quota_left = max(0, 2 - week_buys)
        year_quota_left = max(0, 50 - year_buys)
        freq_note = (f"本年已买入 {year_buys} 次（全年剩余配额 {year_quota_left} 次）")

        prompt = f"""
 今日日期：{trade_date}
 账户情况：{account_text}
 {etf_section}{policy_section}{sentiment_section}{hot_sector_section}
 【决策原则（由 LLM 自主判断）】
- 基于市场情绪、个股基本面、技术面、量化信号综合决策
- 持仓是否卖出取决于：
  ① 趋势变坏（MA死叉/RSI超买/放量下跌/跌破关键均线/MACD死叉）
  ② 基本面恶化（业绩下滑/估值过高/业务逻辑生变）
  ③ 量化卖出信号触发
  ④ 涨幅过大需要止盈
- 【重要】不要仅因为"持有时间到了"就卖出，持有期应基于趋势和基本面决定
- 持仓是否买入取决于：量化候选评分、LLM 分析结果、资金仓位管理
- 估值陷阱（下跌趋势+业绩持续恶化）的候选股已在上游过滤，不会出现在本次候选列表
交易频率参考：
  - {freq_note}
  - 具体买卖数量由 LLM 根据实际情况决定

【风控规则（系统强制执行）】
- 固定止损：持仓亏损达 -{stop_loss_pct*100:.0f}% 时系统自动清仓
- 滑动止盈：盈利后从峰值回落 {trailing_stop_pct*100:.0f}% 时系统自动清仓
- 单只最大仓位：{self._max_single_weight*100:.0f}%（超出系统自动减仓）
- 现金保留：至少 {self._cash_reserve*100:.0f}%
- 稳定性原则：已持仓股票非必要不止损，LLM 决策卖出后系统执行

【双轨选股结果】
{chr(10).join(track_summary_lines)}

## 量化候选股明细（含LLM评分 + 技术指标）
{candidate_table}
（技术参考：RSI超卖↑/超买↓；MACD↑金叉/↓死叉；BB★压缩=蓄势待发；ATR高=波动大须轻仓；质量分高利于B轨；成长分高利于A轨）

{score_table}

## 当前持仓（⚠️ 现价为最近收盘价，非实时，止损由风控模块盘中自动执行，此处无需输出止损 sell）
{positions_text}

{memory_context}

---
请综合以上信息，输出今日交易计划。决策要求：
0. 【最重要】根据市场研判和量化信号自主决定买卖，卖出信号触发时优先考虑执行卖出
1. A轨AI赛道股：
   - 优先选【收入确认度高+所在Layer景气上行+有近期催化剂】的标的，量化得分+LLM评分均高
   - 仓位：Layer1/2/3(基础设施确定性强)可略重10-15%；Layer4/5(应用/机器人)弹性大但风险高，8-12%
   - 若该股所在行业 ETF 信号「强烈买入」，可上调仓位；期货信号看空时降仓或暂缓
   - 【AI赛道轮动提示】在 reasoning 中说明当前哪个 Layer 景气度最强，作为本次 A轨配置核心
2. B轨红利股：
   - 优先选【股息率≥4%+ROE稳定+低估值+护城河强】的标的，仓位可略重10-15%
   - 与「价值策略独立选股」交叉验证：两者同时推荐的个股，信心+1级，仓位可上限15%
   - 【红利稳定性验证】在 reasoning 中说明 B轨整体股息率水平及分红可持续性判断
3. 【卖出决策核心】：
   - 趋势变坏才卖：MA死叉/放量下跌/RSI极度超买/跌破MA60/量能萎缩
   - 基本面恶化才卖：业绩大幅下滑/业务逻辑改变/估值泡沫化
   - 持有时间不是卖出依据——趋势向上+基本面健康则持有，趋势向下+基本面变差则卖出
   - 止损由系统风控模块盘中自动执行，不在此处输出 sell 止损（除非基本面出现根本性恶化，需要手动清仓）
4. 总仓位不超过 {(1-self._cash_reserve)*100:.0f}%
5. 止损价 = 成本价 × (1 - {stop_loss_pct})，请填写具体价格，不能为 0
6. 持仓股票若今日未出现在候选列表，默认 action=hold，不要因未入选就 sell

以 JSON 格式回复：
{{
  "market_regime": "bull/bear/neutral",
  "confidence": 0.0-1.0,
  "reasoning": "市场判断+A/B轨策略逻辑（200字以内）",
  "trades": [
    {{
      "ts_code": "600519.SH",
      "name": "贵州茅台",
      "track": "value",
      "action": "buy",
      "weight": 0.12,
      "entry_price": 1750.00,
      "stop_loss_price": 1680.00,
      "reason": "B轨价值股，PE处历史低位，ROE稳定，逢调布局（50字以内）"
    }}
  ],
  "cash_reserve": {self._cash_reserve}
}}

action 取值：buy（新买入）| sell（清仓）| hold（持有不动）| reduce（减仓）
weight 为占总资产比例
entry_price：买入目标价（当前价或期望的回调买入价）。
  - 若当前价已在合理区间，直接填当前价（盘前立即执行）
  - 若希望等回调，填期望的入场价（低于当前价，盘中等价格到位后执行）
  - 0 或不填 = 盘前立即以市价执行
stop_loss_price 为具体止损价格（成本价 × (1 - {stop_loss_pct})），必须填写具体数字
只输出 JSON，不要其他文字。
"""

        response = self._router.reason(prompt, system=system, max_tokens=3000)
        if not response:
            logger.warning("[Decision] R1 无响应，降级尝试 V3")
            # R1 失败时用 V3 做简化决策（去掉复杂推理链要求，只要 JSON 输出）
            v3_prompt = prompt + "\n\n（注意：请直接输出 JSON，不需要推理过程）"
            response = self._router.analyze(v3_prompt, system=system, max_tokens=2000)
            if not response:
                logger.warning("[Decision] V3 也无响应，返回保守计划")
                return self._fallback_plan(trade_date)
            logger.info("[Decision] V3 降级成功，继续解析")

        # 提取 JSON 块
        plan = self._extract_json(response)
        if plan is None:
            logger.warning("[Decision] 无法解析响应 JSON，返回保守计划")
            logger.debug(f"[Decision] 原始响应: {response[:500]}")
            return self._fallback_plan(trade_date)

        plan['trade_date'] = trade_date
        plan['generated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return plan

    def _extract_json(self, text: str) -> Optional[dict]:
        """从文本中提取 JSON 对象"""
        text = text.strip()
        
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 找 ```json ... ``` 块
        m = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 找以 { 开始到第一个 } 结束的内容（不贪婪匹配）
        m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        # 尝试找 "trades": [ 之后的内容
        trades_match = re.search(r'"trades"\s*:\s*\[([\s\S]*?)\]', text)
        if trades_match:
            # 构建完整的JSON
            try:
                prefix = text[:trades_match.start()]
                suffix = text[trades_match.end():]
                # 尝试提取关键字段
                regime = re.search(r'"market_regime"\s*:\s*"(\w+)"', text)
                confidence = re.search(r'"confidence"\s*:\s*([\d.]+)', text)
                if regime and confidence:
                    return {
                        'market_regime': regime.group(1),
                        'confidence': float(confidence.group(1)),
                        'trades': [],
                        'reasoning': '从响应中提取'
                    }
            except:
                pass

        return None

    def _fallback_plan(self, trade_date: str) -> dict:
        """LLM 失败时的保守兜底计划（持有现仓，不新买）"""
        return {
            'trade_date': trade_date,
            'market_regime': 'neutral',
            'confidence': 0.3,
            'reasoning': 'LLM决策失败，执行保守计划：维持现有持仓，不新增买入',
            'trades': [],
            'cash_reserve': self._cash_reserve,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

    # ------------------------------------------------------------------ #
    #  铁则辅助方法
    # ------------------------------------------------------------------ #
    def _is_value_trap(self, ts_code: str) -> tuple:
        """
        增强版估值陷阱检测 ★
        多维度综合判断 → 避免误杀：
          1. 净利润YoY连续下滑（近60日负值占比 > 60%）
          2. 价格处于下跌趋势（低于MA60超3%）
        加分项（任一满足即强化陷阱判定）：
          3. 经营现金流/净利润 < 0.3（利润纸面化，缺现金支撑）
          4. 营收YoY连续下滑（营收利润双杀）
          5. 资产负债率 > 70%（高杠杆叠加业绩恶化）
        Returns:
            (is_trap: bool, reason: str)
        """
        try:
            df = DBUtils.query_df(
                """SELECT close, netprofit_yoy FROM stock_daily
                   WHERE ts_code=? ORDER BY trade_date DESC LIMIT 126""",
                (ts_code,)
            )
            if df.empty or len(df) < 20:
                return False, ""

            closes = df['close'].astype(float).tolist()
            yoys = df['netprofit_yoy'].tolist()

            # 条件1：近60个数据点（约3个月）中净利润YoY负值占比 > 60%
            recent_yoys = [float(y) for y in yoys[:60] if y is not None and str(y) not in ('', 'nan', 'None')]
            if len(recent_yoys) >= 10:
                neg_ratio = sum(1 for y in recent_yoys if y < -10) / len(recent_yoys)
            else:
                neg_ratio = 0

            # 条件2：现价低于 MA60 超过 3%（下跌趋势）
            current_price = closes[0] if closes else 0
            ma60 = sum(closes[:60]) / min(60, len(closes)) if len(closes) >= 5 else 0
            in_downtrend = ma60 > 0 and current_price < ma60 * 0.97

            if not (neg_ratio > 0.8 and in_downtrend):  # 放宽至80%才判定为估值陷阱
                return False, ""

            # ── 基础条件满足，追加财务质量检查 ★ ──
            extra_flags = []

            # 3. 现金流质量检查
            try:
                fin_df = DBUtils.query_df(
                    """SELECT cashflow_quality, revenue_yoy, debt_ratio
                       FROM financial_data WHERE ts_code=?
                       ORDER BY end_date DESC LIMIT 1""",
                    (ts_code,)
                )
                if not fin_df.empty:
                    row = fin_df.iloc[0]
                    cq = row.get('cashflow_quality')
                    if cq is not None and str(cq) not in ('nan', 'None', ''):
                        cq = float(cq)
                        if cq < 0.3:
                            extra_flags.append(f"现金流质量差({cq:.2f}<0.3)")
                        elif cq < 0.5:
                            extra_flags.append(f"现金流偏低({cq:.2f})")

                    rev_yoy = row.get('revenue_yoy')
                    if rev_yoy is not None and str(rev_yoy) not in ('nan', 'None', ''):
                        if float(rev_yoy) < -5:
                            extra_flags.append(f"营收下滑({float(rev_yoy):.1f}%)")

                    debt = row.get('debt_ratio')
                    if debt is not None and str(debt) not in ('nan', 'None', ''):
                        if float(debt) > 70:
                            extra_flags.append(f"高负债({float(debt):.1f}%)")
            except Exception:
                pass

            # 构建原因描述
            base = (f"估值陷阱（净利润YoY连续下滑占比{neg_ratio*100:.0f}%"
                    f"，现价{current_price:.2f}低于MA60 {ma60:.2f}）")
            if extra_flags:
                base += " 强化信号：" + "、".join(extra_flags)

            return True, base
        except Exception as e:
            logger.debug(f"[Decision] 估值陷阱检测 {ts_code} 异常: {e}")
            return False, ""

    def _get_quant_composite_signals(self, stock_codes: List[str], days_data: dict = None) -> dict:
        """统一量化复合信号评分 ★
        
        各指标贡献正/负分，净分决定买卖方向：
          score >= +50 → 强烈买入
          score >  +10 → 轻度买入
          -10 <= score <= +10 → 中性(持有)
          score <  -10 → 轻度卖出
          score <= -50 → 强烈卖出
          
        Args:
            stock_codes: 股票代码列表
            days_data: 可选，已加载的日线数据 {ts_code: [{close,vol,pct_chg},...]}
            
        Returns:
            {ts_code: {'score': int, 'reasons': str, 'severity': 'hard'|'soft'}}
        """
        if not stock_codes:
            return {}
        
        signals = {}
        placeholders = ','.join(['?' for _ in stock_codes])
        
        try:
            # 加载技术因子
            factor_df = DBUtils.query_df(
                f"""SELECT sf.ts_code, sf.rsi_14, sf.macd_hist, sf.vol_ratio
                   FROM stock_factors sf
                   INNER JOIN (
                       SELECT ts_code, MAX(trade_date) as max_date
                       FROM stock_factors
                       WHERE ts_code IN ({placeholders})
                       GROUP BY ts_code
                   ) lm ON sf.ts_code = lm.ts_code AND sf.trade_date = lm.max_date""",
                tuple(stock_codes)
            )
            
            # 加载日线数据用于均线/趋势/量能判断
            daily_df = DBUtils.query_df(
                f"""SELECT ts_code, trade_date, close, vol
                   FROM stock_daily
                   WHERE ts_code IN ({placeholders})
                   ORDER BY ts_code, trade_date DESC""",
                tuple(stock_codes)
            )
            
            # 聚合日线
            from collections import defaultdict
            price_map = defaultdict(list)
            prev_close = {}
            for _, row in daily_df.iterrows():
                ts = str(row['ts_code'])
                close = float(row['close']) if row['close'] else 0
                vol = float(row['vol']) if row['vol'] else 0
                pct_chg = 0
                if ts in prev_close and prev_close[ts] > 0:
                    pct_chg = (close / prev_close[ts] - 1) * 100
                price_map[ts].append({'close': close, 'vol': vol, 'pct_chg': pct_chg})
                prev_close[ts] = close
            
            # 因子map
            factor_map = {}
            for _, row in factor_df.iterrows():
                factor_map[str(row['ts_code'])] = {
                    'rsi': float(row['rsi_14']) if row.get('rsi_14') else 50,
                    'macd_hist': float(row['macd_hist']) if row.get('macd_hist') else 0,
                    'vol_ratio': float(row['vol_ratio']) if row.get('vol_ratio') else 1,
                }
            
            # 对每只股票计算复合评分
            for code in stock_codes:
                factors = factor_map.get(code, {})
                days = price_map.get(code, [])
                if len(days) < 5:
                    continue
                
                score = 0
                reasons = []
                closes = [d['close'] for d in days]
                vols = [d['vol'] for d in days]
                rsi = factors.get('rsi', 50)
                macd = factors.get('macd_hist', 0)
                vol_ratio = factors.get('vol_ratio', 1.0)
                price = closes[0]
                
                # ── 趋势判断（给信号提供上下文）──
                ma20 = sum(closes[:20]) / 20 if len(closes) >= 20 else price
                ma10 = sum(closes[:10]) / 10 if len(closes) >= 10 else price
                is_uptrend = price > ma20 and ma10 > ma20
                is_downtrend = price < ma20 and ma10 < ma20
                
                # ── RSI评分（趋势中衰减信号强度）──
                if rsi > 85:
                    if is_downtrend:
                        score -= 60
                        reasons.append(f"RSI超买+下跌趋势({rsi:.0f})")
                    elif is_uptrend:
                        score -= 15
                        reasons.append(f"RSI超买但趋势向上({rsi:.0f})")
                    else:
                        score -= 40
                        reasons.append(f"RSI超买({rsi:.0f})")
                elif rsi < 35:
                    if is_uptrend:
                        score += 20
                        reasons.append(f"RSI回调({rsi:.0f})")
                    else:
                        score += 40
                        reasons.append(f"RSI超卖({rsi:.0f})")
                
                # ── MACD评分 ──
                if macd > 0:
                    score += 25
                    if not is_uptrend:
                        score += 10  # 非上升趋势中MACD金叉 → 更强的反转信号
                    if 'MACD' not in str(reasons):
                        reasons.append("MACD金叉")
                elif macd < 0:
                    score -= 25
                    if not is_downtrend:
                        score -= 10  # 非下降趋势中MACD死叉 → 更需警惕
                    if 'MACD' not in str(reasons):
                        reasons.append("MACD死叉")
                
                # ── MA死叉/金叉 ──
                if len(closes) >= 20:
                    ma5_curr = sum(closes[:5]) / 5
                    ma20_curr = sum(closes[:20]) / 20
                    ma5_prev = sum(closes[1:6]) / 5
                    ma20_prev = sum(closes[1:21]) / 20
                    if ma5_curr < ma20_curr and ma5_prev >= ma20_prev:
                        score -= 50
                        reasons.append("MA死叉")
                    elif ma5_curr > ma20_curr and ma5_prev <= ma20_prev:
                        score += 50
                        reasons.append("MA金叉")
                
                # ── 成交量评分 ──
                if vol_ratio > 2.5:
                    pct_chg = days[0].get('pct_chg', 0)
                    if pct_chg > 2:
                        score += 20
                        reasons.append(f"放量上涨({vol_ratio:.1f}x)")
                    elif pct_chg < -2:
                        score -= 30
                        reasons.append(f"放量下跌({vol_ratio:.1f}x)")
                    else:
                        score -= 10
                        reasons.append(f"放量滞涨({vol_ratio:.1f}x)")
                elif len(vols) >= 4:
                    avg_vol_base = sum(vols[3:]) / max(len(vols)-3, 1)
                    if vols[0] < avg_vol_base * 0.5 and vols[1] < avg_vol_base * 0.5 and vols[2] < avg_vol_base * 0.5:
                        score -= 15
                        reasons.append("缩量")
                
                # ── 连续阴线 ──
                if len(days) >= 3:
                    pct_changes = [d.get('pct_chg', 0) for d in days[:3]]
                    if all(p < 0 for p in pct_changes) and closes[0] < ma10:
                        score -= 20
                        reasons.append("三连阴破MA10")
                
                # ── 超买否决：RSI > 80 时禁止任何买入信号 ──
                if rsi > 80 and score > 0:
                    reasons.append(f"RSI超买否决({rsi:.0f}>80)")
                    score = min(score, 0)
                
                # ── 动量过热否决：近20日涨幅超50%禁止买入 ──
                if len(closes) >= 20:
                    mom_20 = closes[0] / closes[19] - 1 if closes[19] > 0 else 0
                    if mom_20 > 0.50 and score > 0:
                        reasons.append(f"动量过热否决({mom_20*100:.0f}%>50%)")
                        score = min(score, 0)
                
                # 确定严重程度
                if score >= 50 or score <= -50:
                    severity = 'hard'
                elif score > 10 or score < -10:
                    severity = 'soft'
                else:
                    severity = 'neutral'
                
                signals[code] = {
                    'score': score,
                    'reasons': ' + '.join(reasons[:4]),
                    'severity': severity,
                }
                    
        except Exception as e:
            logger.warning(f"[Decision] 量化复合信号检测异常: {e}")
        
        return signals

    def _generate_quant_trades(self, candidates: List[dict], positions: dict, 
                            quant_signals: dict) -> dict:
        """量化信号生成交易计划（基于复合评分）★
        
        同一只股票只出现在一个操作中：
          score <= -50 → 强制卖出 (hard)
          -50 < score < -10 → 减仓/预警 (soft)
          -10 <= score <= +10 → 持有 (中性)
          +10 < score < +50 → 买入 (soft)
          score >= +50 → 强烈买入 (hard)
        """
        trades = []
        
        # 收集所有持仓信息和候选评分
        pos_map = {code: pos for code, pos in positions.items()}
        cand_map = {c.get('ts_code', ''): c for c in candidates if c.get('ts_code')}
        all_codes = set(pos_map.keys()) | set(cand_map.keys())
        
        buy_list, sell_list, hold_list = [], [], []
        
        for ts_code in all_codes:
            sig = quant_signals.get(ts_code, {})
            score = sig.get('score', 0)
            reason = sig.get('reasons', '') or '无量化信号'
            severity = sig.get('severity', 'neutral')
            
            # 获取股票名称
            pos_obj = pos_map.get(ts_code)
            cand = cand_map.get(ts_code)
            try:
                name = pos_obj.name if pos_obj and hasattr(pos_obj, 'name') else (
                    cand.get('name', ts_code) if cand else ts_code)
            except:
                name = ts_code
            
            is_held = ts_code in pos_map
            is_candidate = ts_code in cand_map
            
            if is_held and score <= -50:
                # 持仓 + 强烈卖出信号 → 强制卖出
                sell_list.append({
                    'ts_code': ts_code, 'name': name, 'track': 'quant_sell',
                    'action': 'sell', 'weight': 0,
                    'reason': f"量化信号:{reason}",
                    'quant_score': score,
                })
                logger.info(f"[Decision] 量化卖出: {ts_code} score={score} {reason}")
                
            elif is_held and score < -10:
                # 持仓 + 轻度卖出 → 减仓
                sell_list.append({
                    'ts_code': ts_code, 'name': name, 'track': 'quant_reduce',
                    'action': 'reduce', 'weight': 0,
                    'reason': f"量化预警:{reason}",
                    'quant_score': score,
                })
                logger.info(f"[Decision] 量化减仓: {ts_code} score={score} {reason}")
                
            elif is_held and not is_candidate and score <= 10:
                # 持仓 + 无候选身份 + 无买入信号 → 持有
                hold_list.append({
                    'ts_code': ts_code, 'name': name, 'track': 'hold',
                    'action': 'hold', 'weight': 0,
                    'reason': f"量化中性:{reason}",
                })
                
            elif is_candidate and score >= 50:
                # 候选 + 强烈买入 → 买入
                buy_list.append({
                    'ts_code': ts_code, 'name': name,
                    'track': cand.get('track', 'unknown'),
                    'score': cand.get('final_score', 0),
                    'action': 'buy', 'weight': 0.08,
                    'reason': f"量化买入:{reason}",
                    'quant_score': score,
                })
                
            elif is_candidate and score > 10:
                # 候选 + 轻度买入 → 买入
                buy_list.append({
                    'ts_code': ts_code, 'name': name,
                    'track': cand.get('track', 'unknown'),
                    'score': cand.get('final_score', 0),
                    'action': 'buy', 'weight': 0.08,
                    'reason': f"量化买入:{reason}",
                    'quant_score': score,
                })
                
            elif is_held:
                # 持仓 + 无信号 → 持有
                hold_list.append({
                    'ts_code': ts_code, 'name': name, 'track': 'hold',
                    'action': 'hold', 'weight': 0,
                    'reason': f"量化中性:{reason}",
                })
        
        # 先卖后买（资金释放后才有钱买）
        trades.extend(sell_list)
        
        # 买入按策略评分排序取 top
        buy_list.sort(key=lambda x: x.get('score', 0), reverse=True)
        trades.extend(buy_list[:self._topk_a])
        
        trades.extend(hold_list)
        
        return {
            'trades': trades,
            'quant_sell_count': len(sell_list),
            'quant_buy_count': len(buy_list[:self._topk_a]),
        }

    def _get_trade_frequency(self) -> dict:
        """
        统计 Agent 本周/本月已执行的买入次数，用于控制交易频率。
        策略：按周调仓，每周最多 2 次新建仓，每月最多 8 次（约2只/周×4周）。
        """
        try:
            now = datetime.now()
            # 本周起点（周一）
            week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
            month_start = now.strftime('%Y-%m-01')
            year_start = now.strftime('%Y-01-01')
            w_df = DBUtils.query_df(
                "SELECT COUNT(*) as cnt FROM agent_sim_orders WHERE side='buy' AND created_at >= ?",
                (week_start,)
            )
            m_df = DBUtils.query_df(
                "SELECT COUNT(*) as cnt FROM agent_sim_orders WHERE side='buy' AND created_at >= ?",
                (month_start,)
            )
            y_df = DBUtils.query_df(
                "SELECT COUNT(*) as cnt FROM agent_sim_orders WHERE side='buy' AND created_at >= ?",
                (year_start,)
            )
            return {
                'week_buys': int(w_df['cnt'].iloc[0]) if not w_df.empty else 0,
                'month_buys': int(m_df['cnt'].iloc[0]) if not m_df.empty else 0,
                'year_buys': int(y_df['cnt'].iloc[0]) if not y_df.empty else 0,
            }
        except Exception:
            return {'week_buys': 0, 'month_buys': 0, 'year_buys': 0}

    def _get_agent_buy_dates(self) -> dict:
        """
        读取 agent_sim_orders 中每只股票最近一次买入日期，
        供硬拦截判断是否满足最短持仓 5 交易日。
        Returns:
            {ts_code: 'YYYY-MM-DD'} 最近买入日期
        """
        try:
            df = DBUtils.query_df(
                """SELECT ts_code, MAX(created_at) as last_buy
                   FROM agent_sim_orders WHERE side='buy'
                   GROUP BY ts_code"""
            )
            if df.empty:
                return {}
            result = {}
            for _, row in df.iterrows():
                ts_code = str(row['ts_code'])
                last_buy = str(row['last_buy'])[:10]
                result[ts_code] = last_buy
            return result
        except Exception:
            return {}

    # ------------------------------------------------------------------ #
    #  主入口
    # ------------------------------------------------------------------ #
    def run(self, trade_date: str = None) -> dict:
        """
        执行完整决策流程
        Returns:
            交易计划 dict
        """
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')

        logger.info(f"[Decision] 开始决策  trade_date={trade_date}")

        # 步骤1：量化候选
        candidates = self._get_quant_candidates(trade_date)
        if not candidates:
            logger.warning("[Decision] 无量化候选股，返回保守计划")
            return self._fallback_plan(trade_date)

        # 步骤1.5：铁则前置过滤 ─ 移除估值陷阱候选股（下跌趋势+业绩持续恶化）
        filtered_candidates = []
        trap_removed = []
        for c in candidates:
            is_trap, trap_reason = self._is_value_trap(c.get('ts_code', ''))
            if is_trap:
                trap_removed.append(f"{c.get('name', c['ts_code'])}（{trap_reason}）")
                logger.warning(f"[Decision] ⛔ 估值陷阱过滤: {c['ts_code']} {c.get('name','')} — {trap_reason}")
            else:
                filtered_candidates.append(c)
        if trap_removed:
            logger.info(f"[Decision] 估值陷阱过滤掉 {len(trap_removed)} 只: {trap_removed}")
        candidates = filtered_candidates if filtered_candidates else candidates[:max(1, len(candidates)//2)]

        # 步骤1.6：应用记忆加分
        candidates = self._apply_memory_bonus(candidates)

        # 步骤2.1：技术指标补充（RSI / MACD / 动量 / 52w位置）
        candidates = self._enrich_with_tech(candidates)

        # 步骤2：当前持仓
        positions = self._get_current_positions()
        logger.info(f"[Decision] 当前持仓 {len(positions)} 只")

        # 步骤2.5：统计交易频率（供提示词约束）
        trade_freq = self._get_trade_frequency()
        logger.info(f"[Decision] 本周已买入 {trade_freq['week_buys']} 次，本月 {trade_freq['month_buys']} 次，本年 {trade_freq['year_buys']} 次")

        # 步骤2.5-2.7：统一量化复合评分（替代旧版独立买卖信号）★
        all_codes = list(set(list(positions.keys()) + [c.get('ts_code','') for c in candidates if c.get('ts_code')]))
        quant_signals = self._get_quant_composite_signals(all_codes)
        if quant_signals:
            n_buy = sum(1 for s in quant_signals.values() if s.get('score',0) >= 10)
            n_sell = sum(1 for s in quant_signals.values() if s.get('score',0) <= -10)
            logger.info(f"[Decision] 量化复合信号: {len(quant_signals)}只, 买入{n_buy}个, 卖出{n_sell}个")

        quant_trades = self._generate_quant_trades(candidates, positions, quant_signals)
        logger.info(f"[Decision] 量化交易计划: 买入{quant_trades.get('quant_buy_count',0)}只, 卖出{quant_trades.get('quant_sell_count',0)}只")

        # 步骤3：量化候选统一评分（跳过LLM，直接使用量化得分）
        for c in candidates:
            c['llm_score'] = 5

        # 步骤4：直接使用量化交易计划（跳过LLM验证层）
        plan = {
            'trade_date': trade_date,
            'market_regime': 'neutral',
            'confidence': 0.5,
            'reasoning': '基于量化信号生成交易计划（LLM决策层已跳过）',
            'trades': quant_trades.get('trades', []) if quant_trades else [],
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        # 步骤6：保存决策到数据库
        try:
            if not isinstance(plan, dict):
                logger.warning(f"[Decision] plan不是dict类型，跳过保存: {type(plan)}")
            else:
                plan_json = json.dumps(plan, ensure_ascii=False)
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                DBUtils.execute(
                    """INSERT INTO agent_decisions
                       (trade_date, plan_json, confidence, market_regime, generated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (trade_date,
                     plan_json,
                     plan.get('confidence', 0),
                     plan.get('market_regime', 'neutral'),
                     now)
                )
                logger.info(f"[Decision] 决策已保存  market_regime={plan.get('market_regime')}  "
                            f"confidence={plan.get('confidence')}  "
                            f"trades={len(plan.get('trades', []))}")
                
                # 保存到记忆服务
                self._save_decision_to_memory(trade_date, plan)
        except Exception as e:
            logger.error(f"[Decision] 保存决策失败: {e}")

        # 确保返回dict，不是字符串
        if not isinstance(plan, dict):
            logger.warning(f"[Decision] plan类型错误={type(plan)}，返回保守计划")
            plan = self._fallback_plan(trade_date)
        
        return plan
