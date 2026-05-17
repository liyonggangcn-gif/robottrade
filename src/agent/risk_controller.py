#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风险控制器
规则层：止损、滑动止盈、仓位集中度（每次检查）
LLM层：技术面/资金面/消息面/行业面/大盘面多维扫描（每N次检查一次）
"""
import json
import re
from datetime import datetime
from typing import List, Optional, Dict

from loguru import logger

from src.broker.base_broker import BaseBroker
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config


class RiskController:
    """
    两层风控：
    1. 规则层（每次）：固定止损 / 滑动止盈 / 仓位集中度
    2. LLM层（每N次）：技术面 / 资金面 / 消息面 / 行业面 / 大盘面综合评估
    """

    def __init__(self, broker: BaseBroker, llm_router=None):
        self._broker = broker
        self._llm = llm_router  # 可选，不传则跳过 LLM 扫描

        cfg = Config.get('trading_agent.risk') or {}
        # 配置可填正数(0.08)或负数(-0.08)，统一转为负数存储
        self._stop_loss: float = -abs(float(cfg.get('stop_loss', 0.08)))
        self._trailing_stop: float = abs(float(cfg.get('trailing_stop', 0.05)))
        self._max_single_position: float = float(cfg.get('max_single_position', 0.25))
        # LLM 扫描间隔：每隔 N 次规则检查跑一次（默认 6 次=30min@5min间隔）
        self._llm_scan_interval: int = int(cfg.get('llm_scan_interval', 6))
        # LLM 高风险是否允许自动清仓（默认 False，只减仓+告警）
        self._llm_enable_sell: bool = bool(cfg.get('llm_enable_sell', False))

        # ── 基本面止损 ──────────────────────────────────────────────────
        # 净利润同比下滑超此阈值（负数）→ 不管盈亏立即清仓
        self._fundamental_stop_yoy: float = float(cfg.get('fundamental_stop_yoy', -30.0))

        # ── 分批止盈（Staged Take-Profit）────────────────────────────────
        # 每个阶段：profit_pct=触发盈利率阈值，sell_ratio=卖出当前仓位的比例
        #   第1档 +12.5% 卖 50%  → 剩 50%
        #   第2档 +25%   卖 60%  → 剩 20%（= 原始 20%）
        #   第3档 +37.5% 卖 100% → 全清
        default_tp = [
            {'profit_pct': 0.125, 'sell_ratio': 0.50},
            {'profit_pct': 0.250, 'sell_ratio': 0.60},
            {'profit_pct': 0.375, 'sell_ratio': 1.00},
        ]
        self._tp_stages = cfg.get('tp_stages') or default_tp
        # 每只股票已触发到哪个止盈阶段（0=未触发任何）
        self._tp_stage_map: Dict[str, int] = {}
        self._ensure_tp_stages_table()
        self._load_tp_stages()

        # ── MA20 趋势止盈 ─────────────────────────────────────────────────
        # 盈利仓位跌破 MA20 → 清仓锁利（只对盈利仓位生效，避免割亏损肉）
        self._use_ma20_stop: bool = bool(cfg.get('use_ma20_trend_stop', True))

        # 价格高点持久化到数据库，防止重启丢失
        self._price_peaks: Dict[str, float] = {}
        self._ensure_peaks_table()
        self._load_peaks()

        # LLM 扫描计数器
        self._check_count: int = 0
        
        # ── 策略差异化风控配置 ★ ──────────────────────────────────────────────
        # 根据持仓的 track 字段应用差异化止损/止盈
        default_strategy_risk = {
            'sector_rotation': {'stop_loss': -0.08, 'take_profit': 0.20, 'min_hold_days': 5},
            'value': {'stop_loss': -0.10, 'take_profit': 0.25, 'min_hold_days': 15},
            'dividend': {'stop_loss': -0.10, 'take_profit': 0.25, 'min_hold_days': 15},
            'pb_roa': {'stop_loss': -0.12, 'take_profit': 0.30, 'min_hold_days': 20},  # 长持策略更宽容
            'convertible_bond': {'stop_loss': -0.06, 'take_profit': 0.15, 'min_hold_days': 10},  # 债底保护
            'index_enhance': {'stop_loss': -0.08, 'take_profit': 0.20, 'min_hold_days': 10},
            'etf': {'stop_loss': -0.05, 'take_profit': 0.10, 'min_hold_days': 3},  # ETF波动小
        }
        self._strategy_risk = cfg.get('strategy_risk') or default_strategy_risk

        logger.info(f"[Risk] 初始化  stop_loss={self._stop_loss}  "
                    f"trailing_stop={self._trailing_stop}  "
                    f"fundamental_stop_yoy={self._fundamental_stop_yoy}%  "
                    f"tp_stages={len(self._tp_stages)}档  "
                    f"ma20_stop={'启用' if self._use_ma20_stop else '禁用'}  "
                    f"llm={'启用' if self._llm else '禁用'}  "
                    f"策略风控差异化={'启用' if self._strategy_risk else '禁用'}")

    # ------------------------------------------------------------------ #
    #  分批止盈阶段持久化
    # ------------------------------------------------------------------ #
    def _ensure_tp_stages_table(self):
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS agent_tp_stages (
                ts_code VARCHAR(20) PRIMARY KEY,
                stage INT NOT NULL DEFAULT 0,
                updated_at VARCHAR(20)
            )
        """)

    def _load_tp_stages(self):
        """从数据库恢复各持仓的止盈阶段"""
        try:
            df = DBUtils.query_df("SELECT ts_code, stage FROM agent_tp_stages")
            if not df.empty:
                self._tp_stage_map = dict(zip(df['ts_code'], df['stage'].astype(int)))
                logger.info(f"[Risk] 加载止盈阶段 {len(self._tp_stage_map)} 只")
        except Exception as e:
            logger.warning(f"[Risk] 加载止盈阶段失败: {e}")

    def _save_tp_stage(self, ts_code: str, stage: int):
        """持久化单只股票的止盈阶段"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            DBUtils.execute(
                """INSERT INTO agent_tp_stages (ts_code, stage, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(ts_code) DO UPDATE SET stage=excluded.stage,
                   updated_at=excluded.updated_at""",
                (ts_code, stage, now)
            )
        except Exception:
            try:
                DBUtils.execute(
                    "UPDATE agent_tp_stages SET stage=?, updated_at=? WHERE ts_code=?",
                    (stage, now, ts_code)
                )
                df = DBUtils.query_df(
                    "SELECT ts_code FROM agent_tp_stages WHERE ts_code=?", (ts_code,)
                )
                if df.empty:
                    DBUtils.execute(
                        "INSERT INTO agent_tp_stages (ts_code, stage, updated_at) VALUES (?, ?, ?)",
                        (ts_code, stage, now)
                    )
            except Exception as e2:
                logger.warning(f"[Risk] 保存止盈阶段失败 {ts_code}: {e2}")
        self._tp_stage_map[ts_code] = stage

    def _reset_tp_stage(self, ts_code: str):
        """股票清仓后重置止盈阶段"""
        self._tp_stage_map.pop(ts_code, None)
        try:
            DBUtils.execute("DELETE FROM agent_tp_stages WHERE ts_code=?", (ts_code,))
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  峰值持久化
    # ------------------------------------------------------------------ #
    def _ensure_peaks_table(self):
        DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS agent_risk_peaks (
                ts_code VARCHAR(20) PRIMARY KEY,
                peak_price FLOAT NOT NULL,
                updated_at VARCHAR(20)
            )
        """)

    def _load_peaks(self):
        """从数据库恢复峰值记录"""
        try:
            df = DBUtils.query_df("SELECT ts_code, peak_price FROM agent_risk_peaks")
            if not df.empty:
                self._price_peaks = dict(zip(df['ts_code'], df['peak_price'].astype(float)))
                logger.info(f"[Risk] 加载价格峰值 {len(self._price_peaks)} 只")
        except Exception as e:
            logger.warning(f"[Risk] 加载价格峰值失败: {e}")

    def _save_peak(self, ts_code: str, peak: float):
        """持久化单只股票的峰值"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            DBUtils.execute(
                """INSERT INTO agent_risk_peaks (ts_code, peak_price, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(ts_code) DO UPDATE SET peak_price=excluded.peak_price,
                   updated_at=excluded.updated_at""",
                (ts_code, peak, now)
            )
        except Exception:
            # 兼容不支持 ON CONFLICT 的 MySQL
            try:
                DBUtils.execute(
                    "UPDATE agent_risk_peaks SET peak_price=?, updated_at=? WHERE ts_code=?",
                    (peak, now, ts_code)
                )
                df = DBUtils.query_df(
                    "SELECT ts_code FROM agent_risk_peaks WHERE ts_code=?", (ts_code,)
                )
                if df.empty:
                    DBUtils.execute(
                        "INSERT INTO agent_risk_peaks (ts_code, peak_price, updated_at) VALUES (?, ?, ?)",
                        (ts_code, peak, now)
                    )
            except Exception as e2:
                logger.warning(f"[Risk] 保存峰值失败 {ts_code}: {e2}")

    # ------------------------------------------------------------------ #
    #  价格查询
    # ------------------------------------------------------------------ #
    def get_price(self, ts_code: str) -> float:
        """从 stock_daily 取最新收盘价"""
        try:
            df = DBUtils.query_df(
                "SELECT close FROM stock_daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
                (ts_code,)
            )
            if not df.empty:
                return float(df['close'].iloc[0])
        except Exception as e:
            logger.warning(f"[Risk] 获取 {ts_code} 价格失败: {e}")
        return 0.0

    def _get_ma20(self, ts_code: str) -> float:
        """计算 20 日均线（用 stock_daily 最近 20 个收盘价）"""
        try:
            df = DBUtils.query_df(
                "SELECT close FROM stock_daily WHERE ts_code=? ORDER BY trade_date DESC LIMIT 20",
                (ts_code,)
            )
            if not df.empty and len(df) >= 10:
                return float(df['close'].mean())
        except Exception as e:
            logger.debug(f"[Risk] 获取 {ts_code} MA20 失败: {e}")
        return 0.0

    def _fetch_prices(self, ts_codes: List[str]) -> Dict[str, float]:
        """批量获取最新价格"""
        prices = {}
        for ts_code in ts_codes:
            p = self.get_price(ts_code)
            if p > 0:
                prices[ts_code] = p
        return prices

    # ------------------------------------------------------------------ #
    #  峰值更新
    # ------------------------------------------------------------------ #
    def update_peaks(self, positions: list, prices: Dict[str, float]):
        """如果当前价格超过历史峰值，则更新峰值"""
        for p in positions:
            ts_code = p.ts_code if hasattr(p, 'ts_code') else p.get('ts_code', '')
            if not ts_code:
                continue
            current = prices.get(ts_code, 0)
            if current <= 0:
                continue
            old_peak = self._price_peaks.get(ts_code, 0)
            if current > old_peak:
                self._price_peaks[ts_code] = current
                self._save_peak(ts_code, current)
                if old_peak > 0:
                    logger.debug(f"[Risk] {ts_code} 价格峰值更新: {old_peak:.2f} → {current:.2f}")

    # ------------------------------------------------------------------ #
    #  LLM 多维风险扫描
    # ------------------------------------------------------------------ #
    def _fetch_position_context(self, ts_code: str, name: str,
                                cost: float, current_price: float,
                                news_items: List[dict] = None) -> dict:
        """
        汇集单只持仓股的多维度数据，供 LLM 分析
        涵盖：价格趋势 / 量比 / 技术因子 / 行业 / 大盘 / 个股新闻公告
        """
        ctx = {
            'ts_code': ts_code,
            'name': name,
            'cost': round(cost, 2),
            'current_price': round(current_price, 2),
            'profit_pct': round((current_price - cost) / cost * 100, 2) if cost > 0 else 0,
            'industry': '',
            'pct_5d': None,       # 近5日涨跌幅
            'vol_ratio': None,    # 最新量 / 近10日均量
            'rsi_14': None,
            'macd_hist': None,
            'bb_width': None,
            'price_pos_52w': None,  # 价格在52周区间的位置 0~1
            'drawdown_20': None,
            'news': news_items or [],  # 个股新闻+公告列表
        }

        # 1. 近10日行情（涨跌幅 + 量比）
        try:
            df = DBUtils.query_df(
                """SELECT trade_date, close, vol, pct_chg
                   FROM stock_daily WHERE ts_code=?
                   ORDER BY trade_date DESC LIMIT 10""",
                (ts_code,)
            )
            if not df.empty and len(df) >= 2:
                avg_vol = df['vol'].mean()
                ctx['vol_ratio'] = round(float(df['vol'].iloc[0]) / avg_vol, 2) if avg_vol > 0 else 1.0
                if len(df) >= 5:
                    ctx['pct_5d'] = round(
                        (float(df['close'].iloc[0]) / float(df['close'].iloc[4]) - 1) * 100, 2
                    )
        except Exception:
            pass

        # 2. 行业信息
        try:
            df = DBUtils.query_df(
                "SELECT industry FROM stock_info WHERE ts_code=? LIMIT 1", (ts_code,)
            )
            if not df.empty:
                ctx['industry'] = str(df['industry'].iloc[0])
        except Exception:
            pass

        # 3. 技术因子（RSI / MACD / 布林带宽 / 52周位置 / 20日回撤）
        try:
            df = DBUtils.query_df(
                """SELECT rsi_14, macd_hist, bb_width, price_pos_52w, drawdown_20
                   FROM stock_factors WHERE ts_code=?
                   ORDER BY trade_date DESC LIMIT 1""",
                (ts_code,)
            )
            if not df.empty:
                row = df.iloc[0]
                for col in ['rsi_14', 'macd_hist', 'bb_width', 'price_pos_52w', 'drawdown_20']:
                    val = row.get(col)
                    if val is not None and str(val) not in ('', 'nan', 'None'):
                        ctx[col] = round(float(val), 3)
        except Exception:
            pass

        return ctx

    def _fetch_market_context(self) -> str:
        """获取大盘近5日收盘涨跌（上证指数 000001.SH）"""
        try:
            df = DBUtils.query_df(
                """SELECT trade_date, close, pct_chg FROM stock_daily
                   WHERE ts_code='000001.SH'
                   ORDER BY trade_date DESC LIMIT 5"""
            )
            if not df.empty:
                lines = [f"{row['trade_date']} {float(row['close']):.2f} ({float(row['pct_chg']):+.2f}%)"
                         for _, row in df.iterrows()]
                return "上证指数近5日: " + " | ".join(lines)
        except Exception:
            pass
        return ""

    def _fetch_macro_news_context(self) -> str:
        """
        调用 MarketNewsAnalyzer 获取宏观快讯风险评估摘要
        返回格式化字符串，注入提示词的"大盘面/宏观面"部分
        """
        try:
            from src.risk.market_news_analyzer import MarketNewsAnalyzer
            analyzer = MarketNewsAnalyzer()
            result = analyzer.analyze(hours=4, max_news=40)
            risk = result.get('risk_level', '低')
            sentiment = result.get('market_sentiment', '中性')
            action = result.get('action', 'hold')
            summary = result.get('summary', '')
            sectors = result.get('sector_impacts', [])

            # 受影响板块摘要（取前3条利空）
            bearish = [s for s in sectors if s.get('direction') == '利空'][:3]
            sector_lines = '、'.join(
                f"{s['sector']}({s.get('strength','')})" for s in bearish
            ) if bearish else '无明显利空板块'

            return (
                f"【宏观快讯/近4h】风险:{risk}  情绪:{sentiment}  建议:{action}\n"
                f"  摘要:{summary}\n"
                f"  利空板块:{sector_lines}"
            )
        except Exception as e:
            logger.debug(f"[Risk] 宏观快讯获取失败（非关键）: {e}")
            return ""

    def _build_risk_prompt(self, contexts: List[dict], market_ctx: str) -> str:
        """构建多维风险评估提示词"""
        holding_lines = []
        for c in contexts:
            pct_str = f"{c['pct_5d']:+.1f}%" if c['pct_5d'] is not None else "N/A"
            line = (
                f"- {c['name']}({c['ts_code']})  行业:{c['industry'] or '未知'}\n"
                f"  持仓盈亏:{c['profit_pct']:+.1f}%  成本:{c['cost']}  现价:{c['current_price']}"
                f"  近5日:{pct_str}"
            )
            # 技术/资金指标
            indicators = []
            if c['rsi_14'] is not None:
                indicators.append(f"RSI={c['rsi_14']:.1f}")
            if c['macd_hist'] is not None:
                indicators.append(f"MACD柱={c['macd_hist']:.3f}")
            if c['bb_width'] is not None:
                indicators.append(f"布林带宽={c['bb_width']:.3f}")
            if c['price_pos_52w'] is not None:
                indicators.append(f"52周位置={c['price_pos_52w']:.0%}")
            if c['drawdown_20'] is not None:
                indicators.append(f"20日回撤={c['drawdown_20']:.1%}")
            if c['vol_ratio'] is not None:
                indicators.append(f"量比={c['vol_ratio']:.2f}x")
            if indicators:
                line += "\n  技术/资金: " + "  ".join(indicators)
            # 个股新闻/公告（最多5条）
            news_list = c.get('news', [])
            if news_list:
                news_lines = []
                for n in news_list[:5]:
                    tag = '📢公告' if n.get('type') == 'notice' else '📰新闻'
                    news_lines.append(f"    {tag}[{n.get('time','')}] {n.get('title','')}")
                line += "\n  消息面:\n" + "\n".join(news_lines)
            else:
                line += "\n  消息面: 暂无近期新闻"
            holding_lines.append(line)

        holdings_text = "\n\n".join(holding_lines)

        prompt = f"""你是A股专业风险管理员。请对以下持仓股票进行多维度风险评估。

{market_ctx}

## 当前持仓

{holdings_text}

---
请从以下5个维度对每只股票评估风险：

1. **技术面**：趋势（RSI超买/超卖、MACD死叉、布林带收窄）、52周低位、20日持续回撤
2. **资金面**：量比异常（>2.5倍放量出逃 或 <0.5倍缩量无人接盘）
3. **消息面**：基于上方已提供的真实新闻/公告标题进行判断，重点识别：减持公告、业绩预亏、监管问询、股东纠纷、行业政策利空等负面信号；无新闻则基于行业背景推断
4. **行业面**：所在行业近期是否处于政策利空或周期下行阶段
5. **大盘面**：结合宏观快讯和大盘近期走势，判断系统性风险是否上升

综合评估后，对每只股票给出：
- risk_level：high（需立即处置）/ medium（需关注）/ low（无需操作）
- action：sell（建议清仓，仅限极高风险）/ reduce（建议减仓30-50%）/ watch（观察告警，暂不操作）/ hold（维持不动）
- reason：主要风险描述，50字以内，需包含具体维度

**重要原则**：
- 已盈利持仓（profit_pct > 0）且无明确利空消息 → 优先给 hold，不要无故建议卖出
- 轻微亏损（-10% 以内）且基本面无异常 → 给 watch，不给 sell/reduce
- 只有出现以下情形之一才给 reduce/sell：减持公告、业绩预亏/爆雷、监管立案、主力出逃（量比>3且大跌）、技术面三连阴破位

**只输出 JSON 数组，不要其他文字：**
[
  {{
    "ts_code": "代码",
    "name": "名称",
    "risk_level": "high/medium/low",
    "action": "sell/reduce/watch/hold",
    "dimensions": {{
      "technical": "技术面评估",
      "capital": "资金面评估",
      "news": "消息面评估",
      "sector": "行业面评估",
      "market": "大盘面评估"
    }},
    "reason": "综合风险摘要"
  }}
]
"""
        return prompt

    def _parse_llm_risk_response(self, response: str) -> List[dict]:
        """解析 LLM 返回的风险评估 JSON"""
        # 提取 JSON 数组
        try:
            return json.loads(response.strip())
        except Exception:
            pass
        m = re.search(r'```(?:json)?\s*(\[[\s\S]+?\])\s*```', response)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        m = re.search(r'\[[\s\S]+\]', response)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        logger.warning("[Risk/LLM] 无法解析响应 JSON")
        return []

    def _llm_risk_scan(self, positions: list, prices: Dict[str, float]) -> List[dict]:
        """
        LLM 多维度风险扫描
        Returns:
            风险动作列表（仅包含需要处置的信号，action != hold 且 risk_level != low）
        """
        if not self._llm or not positions:
            return []

        logger.info(f"[Risk/LLM] 开始多维风险扫描，持仓 {len(positions)} 只")

        # 1. 批量拉取个股新闻/公告
        news_map: Dict[str, List[dict]] = {}
        try:
            from src.feeds.stock_news_fetcher import fetch_stock_news_batch
            pos_list = [{'ts_code': p.ts_code, 'name': p.name} for p in positions]
            news_map = fetch_stock_news_batch(pos_list, max_news_per_stock=5, sleep_sec=0.3)
            logger.info(f"[Risk/LLM] 个股新闻拉取完成，覆盖 {len(news_map)} 只")
        except Exception as e:
            logger.warning(f"[Risk/LLM] 个股新闻拉取失败（非关键）: {e}")

        # 2. 构建每只持仓的上下文（含新闻）
        contexts = []
        for pos in positions:
            current_price = prices.get(pos.ts_code, pos.current_price)
            if current_price <= 0:
                continue
            ctx = self._fetch_position_context(
                pos.ts_code, pos.name, pos.cost, current_price,
                news_items=news_map.get(pos.ts_code, [])
            )
            contexts.append(ctx)

        if not contexts:
            return []

        # 3. 大盘价格走势 + 宏观快讯摘要
        market_ctx = self._fetch_market_context()
        macro_ctx = self._fetch_macro_news_context()
        if macro_ctx:
            market_ctx = market_ctx + "\n" + macro_ctx if market_ctx else macro_ctx
        prompt = self._build_risk_prompt(contexts, market_ctx)

        # 调用 V3（分析模型），风控扫描不需要 R1 的深度推理
        system = (
            "你是专业的A股风险管理专家，擅长识别技术面、资金面、消息面的综合风险信号。"
            "评估要客观均衡：对基本面优质（ROE>10%、PE合理、已盈利）的股票，"
            "除非存在明确利空消息或技术面严重恶化，否则应给出 hold/watch，不要轻易建议卖出。"
            "只对真正高风险信号（如减持公告、业绩预亏、技术破位、资金大幅流出）给出 reduce/sell。"
        )
        response = self._llm.analyze(prompt, system=system, max_tokens=2000)
        if not response:
            logger.warning("[Risk/LLM] LLM 无响应，跳过本次扫描")
            return []

        items = self._parse_llm_risk_response(response)
        logger.info(f"[Risk/LLM] 解析到 {len(items)} 条风险评估结果")

        # 转换为风险动作
        actions = []
        for item in items:
            ts_code = item.get('ts_code', '')
            name = item.get('name', '')
            risk_level = item.get('risk_level', 'low')
            action = item.get('action', 'hold')
            reason = item.get('reason', '')
            dimensions = item.get('dimensions', {})

            if risk_level == 'low' or action == 'hold':
                continue

            current_price = prices.get(ts_code, 0)
            # 找到对应持仓的成本和盈亏
            pos_obj = next((p for p in positions if p.ts_code == ts_code), None)
            cost = pos_obj.cost if pos_obj else 0
            profit_pct = ((current_price - cost) / cost * 100) if cost > 0 else 0

            # 安全策略：LLM 触发 sell → 降级为 reduce（除非配置允许）
            final_action = action
            if action == 'sell' and not self._llm_enable_sell:
                final_action = 'reduce'
                reason = f"[LLM建议清仓→降级为减仓] {reason}"

            dim_text = "  ".join(
                f"{k}:{v}" for k, v in dimensions.items() if v
            )
            full_reason = f"[LLM风控/{risk_level.upper()}] {reason} | {dim_text}"

            actions.append({
                'ts_code': ts_code,
                'name': name,
                'action': final_action,
                'reason': full_reason,
                'current_price': current_price,
                'cost': cost,
                'profit_pct': profit_pct,
                'risk_level': risk_level,
                'source': 'llm',
            })

            level_icon = {'high': '🔴', 'medium': '🟡'}.get(risk_level, '⚪')
            logger.warning(f"[Risk/LLM] {level_icon} {name}({ts_code}) "
                           f"risk={risk_level} action={final_action}: {reason}")

        return actions

    # ------------------------------------------------------------------ #
    #  基本面健康度检查
    # ------------------------------------------------------------------ #
    def _check_business_health(self, ts_code: str) -> dict:
        """
        检查公司基本面健康度（ROE 和净利润同比）
        Returns:
            {'roe': float, 'netprofit_yoy': float, 'is_healthy': bool}
        健康标准：ROE > 5% 且净利润同比 > -30%
        不健康（业务恶化）→ 应用严格止损；健康 → 给更多波动空间
        """
        try:
            df = DBUtils.query_df(
                """SELECT roe, netprofit_yoy FROM stock_daily
                   WHERE ts_code=? AND roe IS NOT NULL
                   ORDER BY trade_date DESC LIMIT 1""",
                (ts_code,)
            )
            if not df.empty:
                roe = float(df['roe'].iloc[0] or 0)
                yoy = float(df['netprofit_yoy'].iloc[0] or 0)
                # ROE > 5% 且净利润同比 > -30% 认为基本面健康
                is_healthy = roe > 5.0 and yoy > -30.0
                return {'roe': roe, 'netprofit_yoy': yoy, 'is_healthy': is_healthy}
        except Exception as e:
            logger.debug(f"[Risk] 获取 {ts_code} 基本面失败: {e}")
        # 数据缺失时默认假设健康（保守：不因数据缺失提前止损）
        return {'roe': 0.0, 'netprofit_yoy': 0.0, 'is_healthy': True}

    def _check_financial_quality(self, ts_code: str) -> dict:
        """
        财务质量深度检查（现金流 + 营收 + 负债）★
        Returns:
            {'cashflow_quality': float|None, 'revenue_yoy': float|None,
             'debt_ratio': float|None, 'warnings': list[str]}
        """
        result = {'cashflow_quality': None, 'revenue_yoy': None,
                  'debt_ratio': None, 'warnings': []}
        try:
            df = DBUtils.query_df(
                """SELECT cashflow_quality, revenue_yoy, debt_ratio
                   FROM financial_data WHERE ts_code=?
                   ORDER BY end_date DESC LIMIT 1""",
                (ts_code,)
            )
            if df.empty:
                return result
            row = df.iloc[0]
            cq = row.get('cashflow_quality')
            rev = row.get('revenue_yoy')
            debt = row.get('debt_ratio')

            if cq is not None and str(cq) not in ('nan', 'None', ''):
                result['cashflow_quality'] = float(cq)
                if float(cq) < 0.3:
                    result['warnings'].append(f"现金流极差({float(cq):.2f})")
                elif float(cq) < 0.5:
                    result['warnings'].append(f"现金流偏低({float(cq):.2f})")

            if rev is not None and str(rev) not in ('nan', 'None', ''):
                result['revenue_yoy'] = float(rev)
                if float(rev) < -10:
                    result['warnings'].append(f"营收大幅下滑({float(rev):.1f}%)")

            if debt is not None and str(debt) not in ('nan', 'None', ''):
                result['debt_ratio'] = float(debt)
                if float(debt) > 70:
                    result['warnings'].append(f"高杠杆({float(debt):.1f}%)")

        except Exception as e:
            logger.debug(f"[Risk] 财务质量检查 {ts_code} 异常: {e}")
        return result

    # ------------------------------------------------------------------ #
    #  获取持仓的策略轨迹
    # ------------------------------------------------------------------ #
    def _get_position_tracks(self, ts_codes: List[str]) -> Dict[str, str]:
        """从 daily_picks 获取各持仓的 track 字段"""
        if not ts_codes:
            return {}
        try:
            placeholders = ','.join(['?' for _ in ts_codes])
            df = DBUtils.query_df(
                f"SELECT ts_code, track FROM daily_picks WHERE ts_code IN ({placeholders})",
                tuple(ts_codes)
            )
            if not df.empty:
                return dict(zip(df['ts_code'].astype(str), df['track'].astype(str)))
        except Exception as e:
            logger.debug(f"[Risk] 获取持仓 track 失败: {e}")
        return {}

    # ------------------------------------------------------------------ #
    #  规则层风险检查
    # ------------------------------------------------------------------ #
    def _get_protected_codes(self) -> set:
        """
        获取被决策引擎最近标记为"增持/买入"的股票，这些股票在规则层享受宽松止损。
        读取 agent_decisions 表最近1天的 plan_json，解析出 action=buy/add 的 ts_code。
        注意：agent_decisions 表无 action 列，买卖指令嵌套在 plan_json.trades[] 中。
        """
        import json
        try:
            today = __import__('datetime').date.today().isoformat()
            df = DBUtils.query_df(
                "SELECT plan_json FROM agent_decisions WHERE trade_date >= ? ORDER BY id DESC LIMIT 5",
                (today,)
            )
            if df.empty:
                return set()
            protected = set()
            buy_actions = {'buy', 'add', '增持', '买入'}
            for plan_json_str in df['plan_json']:
                try:
                    plan = json.loads(plan_json_str or '{}')
                    for trade in plan.get('trades', []):
                        if trade.get('action', '') in buy_actions:
                            ts_code = trade.get('ts_code', '')
                            if ts_code:
                                protected.add(ts_code)
                except Exception:
                    continue
            return protected
        except Exception:
            pass
        return set()

    def _rule_check(self, positions: list, prices: Dict[str, float],
                    total_assets: float) -> List[dict]:
        """
        规则层（每次检查，按优先级顺序）：
        1. 固定止损（基本面感知阈值）
        2. 基本面止损（净利润 YoY < -30%，不管盈亏立即清仓）
        3. 滑动止盈（从峰值回落超阈值）
        4. 分批止盈（+12.5% 卖50%，+25% 再卖60%，+37.5% 清仓）
        5. MA20 趋势止盈（盈利仓位跌破 20 日均线 → 清仓锁利）
        6. 仓位集中度（单只超最大仓位限制 → 减仓）
        """
        actions = []
        triggered = set()

        # 决策引擎最近推荐增持的股票，给予宽松止损
        protected_codes = self._get_protected_codes()
        if protected_codes:
            logger.debug(f"[Risk] 决策保护股票 {len(protected_codes)} 只: {protected_codes}")

        # 获取各持仓的策略轨迹 ★
        track_map = self._get_position_tracks([p.ts_code for p in positions])

        for pos in positions:
            ts_code = pos.ts_code
            cost = pos.cost
            volume = pos.volume

            if volume <= 0 or cost <= 0:
                continue

            current_price = prices.get(ts_code, pos.current_price)
            if current_price <= 0:
                current_price = pos.current_price
            if current_price <= 0:
                continue

            profit_pct = (current_price - cost) / cost

            # 每只股票只检查一次基本面（供多个规则复用）
            biz = self._check_business_health(ts_code)

            # 获取该持仓的策略轨迹和对应风控参数 ★
            track = track_map.get(ts_code, '')
            track_risk = self._strategy_risk.get(track) or self._strategy_risk.get('sector_rotation', {})
            track_stop = abs(track_risk.get('stop_loss', 0.08))

            # ── 规则1：固定止损（基本面 + 策略轨迹感知）──────────────────────
            if ts_code in protected_codes:
                effective_stop_loss = max(-0.15, -(track_stop + 0.05))
                biz_note = f'决策保护股/{track or "?"}'
            elif biz['is_healthy']:
                # 健康基本面：用策略轨迹的止损阈值，再放宽 4%
                effective_stop_loss = max(-(track_stop + 0.04), -0.15)
                biz_note = f"健康(ROE={biz['roe']:.1f}%)/{track or '?'}→宽松{effective_stop_loss*100:.0f}%"
            else:
                # 业务恶化：用策略轨迹的止损阈值，不额外放宽
                effective_stop_loss = -track_stop
                biz_note = f"恶化(YoY={biz['netprofit_yoy']:.1f}%)/{track or '?'}→严格{effective_stop_loss*100:.0f}%"

            # 硬性止损：亏损超 -18% 强制清仓
            if profit_pct <= -0.18:
                effective_stop_loss = -0.18
                biz_note += '→超硬性止损'

            if profit_pct <= effective_stop_loss:
                actions.append({
                    'ts_code': ts_code, 'name': pos.name, 'action': 'sell',
                    'reason': (f'止损（亏{profit_pct*100:.1f}%，阈值{effective_stop_loss*100:.1f}%）'
                               f' [{biz_note}]'),
                    'current_price': current_price, 'cost': cost,
                    'profit_pct': profit_pct * 100, 'source': 'rule',
                })
                triggered.add(ts_code)
                self._reset_tp_stage(ts_code)
                logger.warning(f"[Risk/止损] {ts_code} {pos.name} pnl={profit_pct*100:.1f}%  {biz_note}")
                continue

            # ── 规则2：基本面止损（净利润 YoY 暴雷，不管盈亏清仓）──────
            if ts_code not in triggered and biz['netprofit_yoy'] < self._fundamental_stop_yoy:
                sign = '+' if profit_pct >= 0 else ''
                actions.append({
                    'ts_code': ts_code, 'name': pos.name, 'action': 'sell',
                    'reason': (f'基本面止损（净利润YoY={biz["netprofit_yoy"]:.1f}%'
                               f'<{self._fundamental_stop_yoy:.0f}%，核心逻辑恶化）'
                               f' 当前盈亏{sign}{profit_pct*100:.1f}%'),
                    'current_price': current_price, 'cost': cost,
                    'profit_pct': profit_pct * 100, 'source': 'rule',
                })
                triggered.add(ts_code)
                self._reset_tp_stage(ts_code)
                logger.warning(f"[Risk/基本面止损] {ts_code} {pos.name} "
                               f"YoY={biz['netprofit_yoy']:.1f}%  当前盈亏={profit_pct*100:.1f}%")
                continue

            # ── 规则3：滑动止盈（从峰值回落超阈值）─────────────────────
            # 仅当基本面健康时才触发，恶化时跳过（让止损处理）
            if ts_code not in triggered and biz['is_healthy']:
                peak = self._price_peaks.get(ts_code, current_price)
                if peak > cost:
                    drawdown = (current_price - peak) / peak
                    if drawdown <= -self._trailing_stop:
                        actions.append({
                            'ts_code': ts_code, 'name': pos.name, 'action': 'sell',
                            'reason': (f'滑动止盈（峰值{peak:.2f}回落{abs(drawdown)*100:.1f}%'
                                       f'≥阈值{self._trailing_stop*100:.1f}%，锁定盈利）'),
                            'current_price': current_price, 'cost': cost,
                            'profit_pct': profit_pct * 100, 'source': 'rule',
                        })
                        triggered.add(ts_code)
                        self._reset_tp_stage(ts_code)
                        logger.warning(f"[Risk/滑动止盈] {ts_code} {pos.name} "
                                       f"peak={peak:.2f} drawdown={drawdown*100:.1f}%")
                        continue

            # ── 规则4：分批止盈（+12.5%/+25%/+37.5% 阶梯减仓）──────────
            # 仅当基本面健康时才触发止盈，基本面恶化则跳过
            if ts_code not in triggered and profit_pct > 0 and biz['is_healthy']:
                current_stage = self._tp_stage_map.get(ts_code, 0)
                for stage_idx, tp in enumerate(self._tp_stages):
                    if stage_idx < current_stage:
                        continue  # 已触发过的阶段跳过
                    if profit_pct >= tp['profit_pct']:
                        sell_ratio = float(tp['sell_ratio'])
                        sell_volume = int(volume * sell_ratio / 100) * 100
                        if sell_volume <= 0:
                            sell_volume = volume  # 避免数量为0时无法执行
                        next_stage = stage_idx + 1
                        stage_label = f"第{next_stage}档止盈({tp['profit_pct']*100:.0f}%→减{sell_ratio*100:.0f}%仓)"
                        actions.append({
                            'ts_code': ts_code, 'name': pos.name, 'action': 'reduce',
                            'reason': (f'分批止盈{stage_label}，当前盈利{profit_pct*100:.1f}%，'
                                       f'减仓{sell_ratio*100:.0f}%（约{sell_volume}股）'),
                            'current_price': current_price, 'cost': cost,
                            'profit_pct': profit_pct * 100, 'source': 'rule',
                            '_sell_volume': sell_volume,   # 执行层直接用此数量
                            '_next_stage': next_stage,     # 执行后更新阶段
                        })
                        triggered.add(ts_code)
                        logger.info(f"[Risk/分批止盈] {ts_code} {pos.name} {stage_label} "
                                    f"盈利{profit_pct*100:.1f}%  减仓{sell_volume}股")
                        break  # 每次只触发一档，下次检查再看是否触发下一档

# ── 规则5：MA20 趋势止盈（盈利仓位跌破均线锁利）────────────
            # 要求盈利 ≥ 8%（相当于已超过固定止损幅度），避免微小盈利时频繁误触发
            # 仅当基本面健康时才触发，基本面恶化则跳过
            if ts_code not in triggered and profit_pct >= 0.08 and self._use_ma20_stop and biz['is_healthy']:
                ma20 = self._get_ma20(ts_code)
                if ma20 > 0 and current_price < ma20:
                    actions.append({
                        'ts_code': ts_code, 'name': pos.name, 'action': 'sell',
                        'reason': (f'趋势止盈（现价{current_price:.2f}跌破MA20={ma20:.2f}）'
                                   f' 盈利{profit_pct*100:.1f}%锁利'),
                        'current_price': current_price, 'cost': cost,
                        'profit_pct': profit_pct * 100, 'source': 'rule',
                    })
                    triggered.add(ts_code)
                    self._reset_tp_stage(ts_code)
                    logger.info(f"[Risk/MA20止盈] {ts_code} {pos.name} "
                                f"盈利{profit_pct*100:.1f}% 跌破MA20={ma20:.2f}")
                    continue

            # ── 规则7：持仓天数检查（来自 RiskAgent）───────────────────────
            holding_days = getattr(pos, 'holding_days', 0) or 0
            
            # 持仓超10天且亏损 → 警告
            if ts_code not in triggered and holding_days > 10 and profit_pct < 0:
                actions.append({
                    'ts_code': ts_code, 'name': pos.name, 'action': 'watch',
                    'reason': (f'持仓{holding_days}天且亏损{profit_pct*100:.1f}%，建议关注'),
                    'current_price': current_price, 'cost': cost,
                    'profit_pct': profit_pct * 100, 'source': 'rule',
                })
                logger.info(f"[Risk/持仓天数] {ts_code} {pos.name} 持仓{holding_days}天 亏损{profit_pct*100:.1f}%")
            
            # 持仓超15天且亏损 → 强制复盘
            if ts_code not in triggered and holding_days > 15 and profit_pct < 0:
                actions.append({
                    'ts_code': ts_code, 'name': pos.name, 'action': 'sell',
                    'reason': (f'持仓超15天({holding_days}天)且亏损{profit_pct*100:.1f}%，强制复盘'),
                    'current_price': current_price, 'cost': cost,
                    'profit_pct': profit_pct * 100, 'source': 'rule',
                })
                triggered.add(ts_code)
                self._reset_tp_stage(ts_code)
                logger.warning(f"[Risk/强制复盘] {ts_code} {pos.name} 持仓{holding_days}天 亏损{profit_pct*100:.1f}%")

            # ── 规则8：市场熔断时加强风控（来自 RiskAgent）───────────────
            if ts_code not in triggered and market_status.get('circuit_breaker') and profit_pct < 0:
                # 市场跌超-2%时，亏损持仓减仓50%
                actions.append({
                    'ts_code': ts_code, 'name': pos.name, 'action': 'reduce',
                    'reason': (f'市场熔断（今日平均跌{abs(market_status["market_avg"]):.1f}%）'
                               f'且持仓亏损{profit_pct*100:.1f}%，减仓50%'),
                    'current_price': current_price, 'cost': cost,
                    'profit_pct': profit_pct * 100, 'source': 'rule',
                    '_sell_volume': int(volume * 0.5 / 100) * 100,
                })
                triggered.add(ts_code)
                logger.warning(f"[Risk/熔断减仓] {ts_code} {pos.name} 亏损{profit_pct*100:.1f}% 减仓50%")

            # ── 规则6：仓位集中度（超限减仓）────────────────────────────
            if ts_code not in triggered and total_assets > 0:
                market_value = current_price * volume
                weight = market_value / total_assets
                if weight > self._max_single_position:
                    actions.append({
                        'ts_code': ts_code, 'name': pos.name, 'action': 'reduce',
                        'reason': (f'仓位集中度超限（{weight*100:.1f}%'
                                   f'>{self._max_single_position*100:.1f}%）'),
                        'current_price': current_price, 'cost': cost,
                        'profit_pct': profit_pct * 100, 'source': 'rule',
                    })
                    triggered.add(ts_code)
                    logger.warning(f"[Risk/仓位] {ts_code} {pos.name} weight={weight*100:.1f}%")

        return actions, triggered

    def _get_market_status(self, trade_date: str = None) -> dict:
        """获取市场状态（用于熔断判断）"""
        try:
            if trade_date is None:
                trade_date = datetime.now().strftime('%Y-%m-%d')
            
            df = DBUtils.query_df("""
                SELECT AVG((close - open) / open * 100) as avg_chg
                FROM stock_daily
                WHERE trade_date = ?
            """, (trade_date,))
            
            if df.empty:
                return {"market_avg": 0, "circuit_breaker": False}
            
            market_avg = float(df.iloc[0]['avg_chg'] or 0)
            return {
                "market_avg": market_avg,
                "circuit_breaker": market_avg < -2.0
            }
        except Exception as e:
            logger.debug(f"[Risk] 市场状态查询失败: {e}")
            return {"market_avg": 0, "circuit_breaker": False}

    # ------------------------------------------------------------------ #
    #  主入口
    # ------------------------------------------------------------------ #
    def check(self, prices: Dict[str, float] = None) -> List[dict]:
        """
        检查所有持仓的风险信号
        - 每次：规则层（止损/止盈/仓位）
        - 每 llm_scan_interval 次：LLM 多维扫描
        Args:
            prices: {ts_code: current_price}，None 则自动获取
        Returns:
            需要处理的风险信号列表（包含 source: 'rule' 或 'llm'）
        """
        self._check_count += 1

        try:
            positions = self._broker.get_positions()
        except Exception as e:
            logger.error(f"[Risk] 获取持仓失败: {e}")
            return []

        if not positions:
            return []

        # 账户总资产
        try:
            account = self._broker.get_account()
            total_assets = account.total_assets
        except Exception:
            total_assets = 0

        # 价格：优先 broker 实时价（IQuantBroker），再回退数据库
        if prices is None:
            prices = {p.ts_code: p.current_price for p in positions if p.current_price > 0}
            missing = [p.ts_code for p in positions if p.ts_code not in prices]
            if missing:
                prices.update(self._fetch_prices(missing))

        # 更新价格峰值
        self.update_peaks(positions, prices)

        # 获取市场状态（熔断判断）
        market_status = self._get_market_status()
        if market_status.get('circuit_breaker'):
            logger.warning(f"[Risk] ⚠️ 市场熔断触发！今日平均跌幅 {market_status['market_avg']:.1f}%")

        # --- 规则层 ---
        rule_actions, rule_triggered = self._rule_check(positions, prices, total_assets)

        # --- LLM层（每 N 次） ---
        llm_actions = []
        if self._llm and (self._check_count % self._llm_scan_interval == 0):
            raw_llm = self._llm_risk_scan(positions, prices)
            # 过滤掉已被规则层处理的股票，避免重复操作
            llm_actions = [a for a in raw_llm if a['ts_code'] not in rule_triggered]

        all_actions = rule_actions + llm_actions

        # watch 类动作：只告警不执行，从执行列表中分离
        watch_actions = [a for a in llm_actions if a.get('action') == 'watch']
        execute_actions = [a for a in all_actions if a.get('action') != 'watch']

        # watch 直接发告警
        for act in watch_actions:
            self._send_llm_watch_alert(act)

        if execute_actions:
            logger.info(f"[Risk] 共发现 {len(execute_actions)} 个执行信号 "
                        f"（规则:{len(rule_actions)} LLM:{len([a for a in llm_actions if a.get('action')!='watch'])}）")
        if watch_actions:
            logger.info(f"[Risk] {len(watch_actions)} 个 LLM 观察告警已推送")

        return execute_actions

    # ------------------------------------------------------------------ #
    #  执行风险动作
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_market_open() -> bool:
        """判断当前是否在A股交易时段（09:30-11:30 / 13:00-15:00）"""
        now = datetime.now()
        h, m = now.hour, now.minute
        t = h * 60 + m  # 转分钟数
        morning = (9 * 60 + 30) <= t <= (11 * 60 + 30)
        afternoon = (13 * 60) <= t <= (15 * 60)
        return morning or afternoon

    def execute_actions(self, actions: List[dict]) -> List[dict]:
        """
        根据风险信号执行交易（仅在A股交易时段内执行）
        Returns:
            执行结果列表
        """
        if not self.is_market_open():
            now_str = datetime.now().strftime('%H:%M')
            logger.warning(f"[Risk] 当前 {now_str} 非交易时段，跳过执行 {len(actions)} 条风控指令")
            return [{'ts_code': a['ts_code'], 'action': a['action'],
                     'success': False, 'msg': f'非交易时段({now_str})，指令暂缓'} for a in actions]

        results = []
        for act in actions:
            ts_code = act['ts_code']
            action = act['action']
            reason = act['reason']
            current_price = act.get('current_price', 0)

            logger.info(f"[Risk] 执行风险操作: {ts_code} {action}  原因: {reason}")

            try:
                if action == 'sell':
                    order = self._broker.sell(ts_code, price=current_price)
                    result = {
                        'ts_code': ts_code,
                        'action': action,
                        'reason': reason,
                        'success': order.success,
                        'msg': order.msg,
                        'price': order.price,
                        'volume': order.volume
                    }

                elif action == 'reduce':
                    pos = self._broker.get_position(ts_code)
                    if pos and current_price > 0:
                        source = act.get('source', 'rule')
                        # 分批止盈：直接使用规则层计算的 _sell_volume
                        if '_sell_volume' in act:
                            reduce_volume = min(int(act['_sell_volume']), pos.volume)
                        elif source == 'llm':
                            reduce_volume = int(pos.volume * 0.3 / 100) * 100
                        else:
                            # 仓位集中度：减到上限以下
                            account = self._broker.get_account()
                            target_mv = account.total_assets * self._max_single_position * 0.9
                            excess = current_price * pos.volume - target_mv
                            reduce_volume = int(excess / current_price / 100) * 100 if excess > 0 else 0

                        if reduce_volume > 0:
                            order = self._broker.sell_volume(ts_code, reduce_volume, current_price)
                            result = {
                                'ts_code': ts_code, 'action': action, 'reason': reason,
                                'success': order.success, 'msg': order.msg,
                                'price': order.price, 'volume': reduce_volume,
                            }
                            # 分批止盈成功后推进阶段
                            if order.success and '_next_stage' in act:
                                self._save_tp_stage(ts_code, act['_next_stage'])
                                logger.info(f"[Risk] {ts_code} 止盈阶段更新为 {act['_next_stage']}")
                        else:
                            result = {'ts_code': ts_code, 'action': action,
                                      'success': False, 'msg': '减仓量为0'}
                    else:
                        result = {'ts_code': ts_code, 'action': action,
                                  'success': False, 'msg': '持仓或价格为0'}

                else:
                    result = {'ts_code': ts_code, 'action': action,
                              'success': False, 'msg': f'未知动作: {action}'}

                results.append(result)

                if result.get('success'):
                    self._send_alert(ts_code, act.get('name', ''), action, reason,
                                     current_price, act.get('profit_pct', 0))

            except Exception as e:
                logger.error(f"[Risk] 执行 {ts_code} {action} 失败: {e}")
                results.append({
                    'ts_code': ts_code,
                    'action': action,
                    'success': False,
                    'msg': str(e)
                })

        return results

    # ------------------------------------------------------------------ #
    #  告警推送
    # ------------------------------------------------------------------ #
    def _send_alert(self, ts_code: str, name: str, action: str,
                    reason: str, price: float, profit_pct: float):
        """发送钉钉风控执行告警"""
        try:
            from src.utils.notifier import send_alert
            action_zh = {'sell': '清仓', 'reduce': '减仓'}.get(action, action)
            title = f"【风控执行】{name}({ts_code}) {action_zh}"
            content = (
                f"**{title}**\n\n"
                f"- 操作: {action_zh}\n"
                f"- 原因: {reason}\n"
                f"- 当前价: {price:.2f}\n"
                f"- 持仓盈亏: {profit_pct:+.1f}%\n"
            )
            send_alert(title, content, message_type='risk_control')
        except Exception as e:
            logger.debug(f"[Risk] 发送执行告警失败（非关键错误）: {e}")

    def _send_llm_watch_alert(self, act: dict):
        """发送 LLM 观察预警（不执行交易）"""
        try:
            from src.utils.notifier import send_alert
            ts_code = act.get('ts_code', '')
            name = act.get('name', '')
            risk_level = act.get('risk_level', 'medium')
            reason = act.get('reason', '')
            price = act.get('current_price', 0)
            profit_pct = act.get('profit_pct', 0)

            level_zh = {'high': '高风险⚠️', 'medium': '中风险注意'}.get(risk_level, '风险')
            title = f"【风控预警/{level_zh}】{name}({ts_code})"
            content = (
                f"**{title}**\n\n"
                f"- 风险等级: {level_zh}\n"
                f"- 风险摘要: {reason}\n"
                f"- 当前价: {price:.2f}\n"
                f"- 持仓盈亏: {profit_pct:+.1f}%\n"
                f"- 本次仅预警，未自动操作\n"
            )
            send_alert(title, content, message_type='risk_watch')
            logger.info(f"[Risk/LLM] 观察预警已推送: {name}({ts_code})")
        except Exception as e:
            logger.debug(f"[Risk] 发送预警失败（非关键错误）: {e}")
