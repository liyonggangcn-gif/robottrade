"""
HybridStrategy: 混合选股策略引擎（双轨版 v2）

默认以 'dual' 模式运行，同时输出两条选股轨道：

  轨道 A — 行业轮动（sector_rotation）
    先按行业近20日动量过滤到前40%强势行业，
    再用 AI+事件+基本面+市值偏好 综合评分选 top_k//2 只。
    权重: AI=0.35, Event=0.25, Fundamental=0.10,
          SectorMom=0.20, MvPref=0.10

  轨道 B — 价值质量（value）
    门槛: ROE≥8%, PE∈(0,80), 净利润yoy≥-30%
    评分: 成长×盈利×护城河×估值×盈余质量
    选 top_k//2 只（与轨道A去重后补全 top_k）

新闻感知：传入 news_boost_sectors 时对应板块事件评分×1.5

用法:
    strategy = HybridStrategy()
    result_df = strategy.run(trade_date='2026-02-07', top_k=20)
    result_df = strategy.run(top_k=20, news_boost_sectors=['石油石化'])
    result_df = strategy.run(top_k=20, mode='tech')   # 仅技术面单轨
"""

import pandas as pd
import numpy as np
import time
from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.analysis.event_driver import EventDriver


class HybridStrategy:
    """混合AI+事件驱动+行业动量+AI赛道热度选股策略"""

    # A轨权重（已移除市值偏好，改为AI赛道Layer热度）
    W_AI = 0.35            # AI LightGBM 预测评分
    W_EVENT = 0.20         # 事件驱动（池内相对热度）
    W_FUNDAMENTAL = 0.10   # 基本面（ROE/PE），基本面差的直接被硬过滤
    W_SECTOR_MOM = 0.20    # 行业动量
    W_LAYER_HEAT = 0.15    # AI赛道Layer热度（layer1-5近期涨势）

    def __init__(self, hot_topics=None, extra_topics=None):
        """初始化混合策略

        Args:
            hot_topics: 热门主题列表, None 则从配置文件读取
            extra_topics: 动态追加的热点（如新闻检测板块），会与 hot_topics 合并
        """
        self.event_driver = EventDriver(hot_topics=hot_topics, extra_topics=extra_topics)

        # 读取配置
        mv_cfg = Config.get('hybrid_strategy') or {}
        self.max_mv_yi = mv_cfg.get('max_mv_yi', 800)
        self.W_LAYER_HEAT = mv_cfg.get('w_layer_heat', self.W_LAYER_HEAT)
        self.track_topk = mv_cfg.get('track_topk', 4)          # 每轨默认4只
        self.anti_chase_days = mv_cfg.get('anti_chase_days', 10)
        self.anti_chase_pct  = mv_cfg.get('anti_chase_pct', 15)
        self.short_term_amp  = mv_cfg.get('short_term_amp_threshold', 0.04)
        self.persistence_bonus  = mv_cfg.get('persistence_bonus', 0.08)
        self.max_turnover_pct   = mv_cfg.get('max_turnover_pct', 0.50)

        # 稳定性控制参数
        stab_cfg = mv_cfg.get('stability', {})
        self.score_ema_alpha    = stab_cfg.get('ema_alpha', 0.40)      # EMA平滑系数：0.4=当日40%+历史60%
        self.grace_period_days  = stab_cfg.get('grace_period_days', 3) # 淘汰缓冲期（交易日）
        self.stability_weight   = stab_cfg.get('stability_weight', 0.10) # 评分稳定性权重
        self.min_score_history  = stab_cfg.get('min_history_days', 5)  # 至少保留几天历史才启用EMA

        self._ensure_score_history_table()

        print("[HybridStrategy] 初始化完成")
        print(f"  A轨权重: AI={self.W_AI}, Event={self.W_EVENT}, "
              f"SectorMom={self.W_SECTOR_MOM}, LayerHeat={self.W_LAYER_HEAT}, "
              f"Fundamental={self.W_FUNDAMENTAL}")
        print(f"  每轨选股: {self.track_topk} 只 | 反追高: {self.anti_chase_days}日>{self.anti_chase_pct}% | "
              f"持续奖励: +{int(self.persistence_bonus*100)}% | 最大换手: {int(self.max_turnover_pct*100)}%")
        print(f"  稳定性: EMA={self.score_ema_alpha} 缓冲期={self.grace_period_days}天 "
              f"稳定性权重={self.stability_weight}")

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _get_latest_trade_date(self):
        """获取 stock_daily 表中最新交易日"""
        result = DBUtils.query_df(
            "SELECT MAX(trade_date) as max_date FROM stock_daily"
        )
        if not result.empty and pd.notna(result.iloc[0]['max_date']):
            return result.iloc[0]['max_date']
        return None

    def _get_us_market_score(self):
        """获取美股S&P500近5日表现，转换为等价A股上涨占比

        转换规则：0%收益 → 0.50，每+1%加0.04，每-1%减0.04
        上限0.75，下限0.25（防止单因子主导）

        Returns:
            (us_equiv_up_pct: float, us_ret5: float)  失败时返回 (0.50, 0.0)
        """
        try:
            import warnings
            warnings.filterwarnings('ignore')
            
            # 检查是否有缓存结果（5分钟内有效）
            cache_key = 'us_market_cache'
            if hasattr(self, '_us_cache') and self._us_cache.get('time', 0) > time.time() - 300:
                cached = self._us_cache.get('result')
                if cached:
                    print(f"  [US Market] 使用缓存: 等价上涨占比={cached[0]*100:.1f}%")
                    return cached
            
            # 快速失败：不等待，直接使用默认值
            print("  [US Market] 跳过（限流中），使用默认值")
            return 0.50, 0.0
                
            if df is None or len(df) < 2:
                return 0.50, 0.0
            closes = df['Close'].dropna()
            if len(closes) < 2:
                return 0.50, 0.0
            n = min(6, len(closes))
            us_ret5   = float(closes.iloc[-1] / closes.iloc[-n] - 1)
            us_latest = float(closes.iloc[-1] / closes.iloc[-2] - 1)
            us_equiv  = max(0.25, min(0.75, 0.50 + us_ret5 * 4.0))
            print(f"  [US Market] S&P500 5日={us_ret5*100:+.2f}% "
                  f"最新日={us_latest*100:+.2f}% -> 等价上涨占比={us_equiv*100:.1f}%")
            return us_equiv, us_ret5
        except Exception as e:
            print(f"  [US Market] 获取失败({e})，忽略美股因子")
            return 0.50, 0.0

    def _check_extreme_downside(self, trade_date: str) -> float:
        """极端行情检测：全市场普跌时缩减仓位

        SQL 统计近5日 close < open 的占比（即收跌个股比例），
        超过阈值则大幅减少选股数量。

        Returns:
            仓位乘数: 1.0(正常) / 0.5(减半) / 0.0(空仓)
        """
        try:
            threshold = float(Config.get('hybrid_strategy.extreme_down_threshold', 0.67))
            df = DBUtils.query_df("""
                SELECT trade_date,
                       SUM(CASE WHEN close < open THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS down_pct
                FROM stock_daily
                WHERE trade_date <= ?
                GROUP BY trade_date
                ORDER BY trade_date DESC
                LIMIT 5
            """, params=[trade_date])
            if df.empty:
                return 1.0
            avg_down = float(df['down_pct'].mean())
            if avg_down > threshold + 0.10:
                print(f"  [ExtremeDown] 5日平均下跌={avg_down:.1%}（>{threshold+0.10:.0%}），全空仓")
                return 0.0
            elif avg_down > threshold:
                print(f"  [ExtremeDown] 5日平均下跌={avg_down:.1%}（>{threshold:.0%}），选股减半")
                return 0.5
            return 1.0
        except Exception as e:
            logger.warning(f"[Hybrid] 极端行情检测失败: {e}")
            return 1.0

    def _get_market_regime(self):
        """市场状态检测：A股涨跌比（70%）+ 美股S&P500（30%）加权融合

        综合得分 combined = 0.70 × A股5日平均上涨占比 + 0.30 × 美股等价上涨占比

          >= 0.53: 强市  → top_k × 1.0
          0.42~0.53: 中性 → top_k × 0.8
          0.30~0.42: 弱市 → top_k × 0.5
          < 0.30 + 急跌  → top_k × 0.3（价值抄底模式）
          < 0.30 + 非急跌 → top_k × 0（全空仓）
        """
        try:
            sql = """
            SELECT trade_date,
                   SUM(CASE WHEN close > open THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS up_pct,
                   ROUND(AVG((close - open) / open * 100), 2) AS avg_chg
            FROM stock_daily
            GROUP BY trade_date
            ORDER BY trade_date DESC
            LIMIT 5
            """
            df = DBUtils.query_df(sql)
            if df.empty:
                return 'neutral', 1.0

            a_up_pct   = float(df['up_pct'].mean())
            latest_chg = float(df.iloc[0]['avg_chg'])

            # 美股因子（失败时等价上涨占比默认0.50，不影响结果）
            us_equiv, us_ret5 = self._get_us_market_score()

            # 加权融合
            combined = 0.70 * a_up_pct + 0.30 * us_equiv

            if combined >= 0.53:
                regime, mult = 'strong', 1.0
            elif combined >= 0.42:
                regime, mult = 'neutral', 0.8
            elif combined >= 0.30:
                regime, mult = 'weak', 0.5
            else:
                # 判断是否为急跌（抄底机会）：A股最新日跌>2% 或 美股近5日跌>2%
                if latest_chg <= -2.0 or us_ret5 <= -0.02:
                    regime, mult = 'dip_buy', 0.3
                else:
                    regime, mult = 'crash', 0.0

            tag = {'strong': '[BULL]', 'neutral': '[NEUTRAL]', 'weak': '[BEAR]',
                   'crash': '[CRASH]', 'dip_buy': '[DIP-BUY]'}[regime]
            print(f"  [MarketRegime] {tag} | A股={a_up_pct*100:.1f}% "
                  f"美股等价={us_equiv*100:.1f}% 综合={combined*100:.1f}% | top_k x{mult}")
            return regime, mult
        except Exception as e:
            raise RuntimeError(f"MarketRegime数据获取失败: {e}") from e

    def _get_previous_picks(self):
        """获取上次推荐的股票代码集合（用于持仓延续奖励）

        优先读 daily_picks 表最新一期；回退读最近 hybrid_picks CSV。
        """
        try:
            df = DBUtils.query_df(
                "SELECT DISTINCT ts_code FROM daily_picks WHERE trade_date = ("
                "  SELECT MAX(trade_date) FROM daily_picks"
                ")"
            )
            if not df.empty:
                return set(df['ts_code'].astype(str).str.strip())
        except Exception:
            pass

        import glob, os
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        files = sorted(glob.glob(os.path.join(root, 'output', 'hybrid_picks_*.csv')), reverse=True)
        if files:
            try:
                df = pd.read_csv(files[0])
                col = 'ts_code' if 'ts_code' in df.columns else 'code'
                return set(df[col].astype(str).str.strip())
            except Exception:
                pass
        return set()

    def _filter_short_term_stocks(self, df, trade_date):
        """过滤超短线投机股（不适合持有1周以上）

        判断标准：近5日日均振幅 (high-low)/close 超过阈值，属于高波动投机股。
        此类股票往往是游资炒作标的，无法以周为单位持有。
        """
        threshold = self.short_term_amp
        if threshold <= 0:
            return df
        try:
            # 优化：先获取最近5个交易日的日期，避免子查询
            dates_df = DBUtils.query_df('''
                SELECT DISTINCT trade_date FROM stock_daily 
                ORDER BY trade_date DESC LIMIT 5
            ''')
            dates = dates_df['trade_date'].tolist()
            if len(dates) < 5:
                return df
            start_date = dates[-1]
            
            sql = f"""
            SELECT ts_code,
                   AVG((high - low) / close) AS avg_amp
            FROM stock_daily
            WHERE trade_date >= '{start_date}'
              AND trade_date <= '{dates[0]}'
              AND close > 0 AND high >= low
            GROUP BY ts_code
            HAVING avg_amp IS NOT NULL
            """
            amp_df = DBUtils.query_df(sql)
            if amp_df.empty:
                return df
            volatile = set(amp_df[amp_df['avg_amp'] > threshold]['ts_code'].astype(str))
            before = len(df)
            df = df[~df['ts_code'].isin(volatile)]
            removed = before - len(df)
            if removed > 0:
                print(f"  [ShortTermFilter] 剔除超短线投机股 {removed} 只 "
                      f"(5日日均振幅>{threshold*100:.0f}%)")
        except Exception as e:
            print(f"  [ShortTermFilter] 异常: {e}，跳过")
        return df

    def _get_latest_ai_date(self):
        """获取 ai_predictions 表中最新预测日期"""
        try:
            result = DBUtils.query_df(
                "SELECT MAX(trade_date) as max_date FROM ai_predictions"
            )
            if not result.empty and pd.notna(result.iloc[0]['max_date']):
                return result.iloc[0]['max_date']
        except Exception:
            pass
        return None

    def _load_stock_universe(self, trade_date):
        """加载选股宇宙: 优先使用 stock_pool（PRD设计），pool 为空时 fallback 全市场

        Args:
            trade_date: 交易日期 (YYYY-MM-DD 格式)

        Returns:
            DataFrame: ts_code, name, close, pe_ttm, total_mv, roe, industry
        """
        # 优先从ClickHouse加载（更快）
        ch_loader = None
        use_ch = Config.get('use_clickhouse', False)
        
        if use_ch:
            try:
                from src.collector.ch_data_loader import get_ch_loader
                ch_loader = get_ch_loader()
                ch_date = trade_date.replace('-', '')
                df = ch_loader.get_stock_daily_with_industry(ch_date, ch_date)
                if not df.empty:
                    print(f"[HybridStrategy] 从ClickHouse加载 {len(df)} 条日线数据")
                    return df
            except Exception as e:
                print(f"[HybridStrategy] ClickHouse加载失败，回退MySQL: {e}")
        
        # 检查 stock_pool 是否有活跃股票
        pool_check = DBUtils.query_df(
            "SELECT COUNT(*) as cnt FROM stock_pool WHERE is_active = 1"
        )
        pool_size = int(pool_check.iloc[0]["cnt"]) if not pool_check.empty else 0

        if pool_size >= 10:
            # 以 stock_pool 为宇宙：只对池内股票评分选股（符合PRD设计）
            sql = """
            SELECT
                sd.ts_code,
                COALESCE(NULLIF(sp.company_name, ''), NULLIF(si.name, ''), sd.ts_code) AS name,
                sd.close,
                COALESCE(sd.pe_ttm, si.pe_ttm, 0) AS pe_ttm,
                COALESCE(si.total_mv, sd.total_mv, 0) AS total_mv,
                sd.roe,
                sd.gpr,
                sd.netprofit_yoy,
                si.industry,
                sp.company_type,
                sp.tier
            FROM stock_daily sd
            INNER JOIN stock_pool sp ON CONVERT(sd.ts_code USING utf8mb4) = CONVERT(sp.ts_code USING utf8mb4) AND sp.is_active = 1
            LEFT JOIN stock_info si ON CONVERT(sd.ts_code USING utf8mb4) = CONVERT(si.ts_code USING utf8mb4)
            WHERE sd.trade_date = ?
              AND sd.close IS NOT NULL
              AND sd.close > 0
            ORDER BY sd.ts_code
            """
            df = DBUtils.query_df(sql, params=[trade_date])
            print(f"[HybridStrategy] 使用股票池宇宙: {len(df)} 只 / 池内 {pool_size} 只 (日期: {trade_date})")
        else:
            # pool 未建立时降级到全市场，输出警告
            print(f"[HybridStrategy][WARN] stock_pool 股票数不足({pool_size}只)，"
                  f"降级为全市场扫描。建议运行 weekly_pool_refresh.py 建立股票池。")
            sql = """
            SELECT
                sd.ts_code,
                COALESCE(NULLIF(si.name, ''), sd.ts_code) AS name,
                sd.close,
                COALESCE(sd.pe_ttm, si.pe_ttm, 0) AS pe_ttm,
                COALESCE(si.total_mv, sd.total_mv, 0) AS total_mv,
                sd.roe,
                sd.gpr,
                sd.netprofit_yoy,
                si.industry
            FROM stock_daily sd
            LEFT JOIN stock_info si ON CONVERT(sd.ts_code USING utf8mb4) = CONVERT(si.ts_code USING utf8mb4)
            WHERE sd.trade_date = ?
              AND sd.close IS NOT NULL
              AND sd.close > 0
            ORDER BY sd.ts_code
            """
            df = DBUtils.query_df(sql, params=[trade_date])
            print(f"[HybridStrategy] 全市场宇宙: {len(df)} 只股票 (日期: {trade_date})")

        return df

    def _load_ai_scores(self, trade_date=None):
        """加载 AI 预测评分

        Args:
            trade_date: 指定日期, None 则使用 ai_predictions 最新日期

        Returns:
            DataFrame: ts_code, ai_score
        """
        try:
            if trade_date is None:
                trade_date = self._get_latest_ai_date()

            if trade_date is None:
                print("[HybridStrategy] ai_predictions 表为空, AI评分将全部为0")
                return pd.DataFrame(columns=['ts_code', 'ai_score'])

            sql = "SELECT ts_code, ai_score FROM ai_predictions WHERE trade_date = ?"
            df = DBUtils.query_df(sql, params=[trade_date])
            print(f"[HybridStrategy] 加载AI评分: {len(df)} 条 (日期: {trade_date})")
            return df
        except Exception as e:
            print(f"[HybridStrategy] 加载AI评分失败: {e}")
            return pd.DataFrame(columns=['ts_code', 'ai_score'])

    # ------------------------------------------------------------------
    # 过滤器 (Safety Net)
    # ------------------------------------------------------------------

    def _apply_filters(self, df, event_scores):
        """应用安全过滤器

        规则:
          1. 剔除 ST / *ST / 退市 股票
          2. 剔除 total_mv <= 0 的垃圾数据
          3. 剔除 pe_ttm < 0 (亏损股), 除非该股有高事件评分 (概念热度可忽略PE)

        Args:
            df: 股票池 DataFrame
            event_scores: pd.Series, index=ts_code, value=event_score

        Returns:
            过滤后的 DataFrame
        """
        before = len(df)

        # 1. 剔除 ST / 退市
        if 'name' in df.columns:
            mask_st = df['name'].str.contains('ST|退', na=False)
            df = df[~mask_st]
            removed_st = before - len(df)
            if removed_st > 0:
                print(f"  [Filter] 剔除ST/退市: {removed_st} 只")

        # 2. 剔除 total_mv <= 0 (垃圾数据)
        before_mv = len(df)
        df = df[df['total_mv'] > 0]
        removed_mv = before_mv - len(df)
        if removed_mv > 0:
            print(f"  [Filter] 剔除市值异常(<=0): {removed_mv} 只")

        # 2b. 市值上限已取消，A轨不再剔除大盘股

        # 3. 剔除 pe_ttm < 0 (亏损), 但保留有事件评分的股票
        before_pe = len(df)
        # 构建每只股票的事件评分查找
        has_event = df['ts_code'].map(
            lambda x: event_scores.get(x, 0.0) > 0
        )
        # pe_ttm >= 0 或 pe_ttm 为 NaN 或 有事件评分
        mask_pe_ok = (df['pe_ttm'] >= 0) | (df['pe_ttm'].isna()) | has_event
        df = df[mask_pe_ok]
        removed_pe = before_pe - len(df)
        if removed_pe > 0:
            print(f"  [Filter] 剔除亏损股(PE<0, 无概念加持): {removed_pe} 只")

        after = len(df)
        print(f"  [Filter] 过滤结果: {before} -> {after} 只股票")
        return df

    # ------------------------------------------------------------------
    # 评分计算
    # ------------------------------------------------------------------

    def _get_mom20_from_daily(self, trade_date):
        """从 stock_daily 计算近20日动量（stock_factors 不存在时的回退方案）

        mom_20 = close[today] / close[20 trading days ago] - 1

        Returns:
            DataFrame: ts_code, mom_20
        """
        from datetime import timedelta
        cutoff = (pd.Timestamp(trade_date) - timedelta(days=35)).strftime('%Y-%m-%d')
        sql = """
        SELECT ts_code, trade_date, close
        FROM stock_daily
        WHERE trade_date >= ? AND trade_date <= ?
          AND close IS NOT NULL AND close > 0
        ORDER BY ts_code, trade_date
        """
        hist = DBUtils.query_df(sql, params=[cutoff, trade_date])
        if hist.empty:
            return pd.DataFrame(columns=['ts_code', 'mom_20'])

        hist['close'] = pd.to_numeric(hist['close'], errors='coerce')
        records = []
        for code, grp in hist.groupby('ts_code'):
            grp = grp.sort_values('trade_date')
            if len(grp) < 2:
                continue
            c_now = grp['close'].iloc[-1]
            c_20d = grp['close'].iloc[0]   # earliest in window ≈ 20 days ago
            if c_20d > 0:
                records.append({'ts_code': code, 'mom_20': (c_now / c_20d - 1) * 100})
        return pd.DataFrame(records)

    def _calculate_sector_momentum(self, df, trade_date):
        """计算增强版行业动量评分

        增强点:
          1. 多周期动量: 5日(60%) + 20日(40%) 权重
          2. 相对强度: 行业 vs 市场平均
          3. 成交量确认: 排除无量上涨行业
          4. 动量加速: 最近5日 vs 前5日

        Args:
            df: 股票池 DataFrame（含 ts_code, industry 列）
            trade_date: 交易日期

        Returns:
            pd.Series: 行业动量评分, index 与 df 对齐
        """
        try:
            from datetime import timedelta

            end_date = pd.Timestamp(trade_date)
            start_60d = (end_date - timedelta(days=70)).strftime('%Y-%m-%d')

            sql = """
            SELECT ts_code, trade_date, close, vol
            FROM stock_daily
            WHERE trade_date >= ? AND trade_date <= ?
              AND close IS NOT NULL AND close > 0
            ORDER BY ts_code, trade_date
            """
            hist = DBUtils.query_df(sql, params=[start_60d, trade_date])

            if hist.empty:
                print("  [SectorMom] 无历史数据，回退为0")
                return pd.Series(0.0, index=df.index)

            hist['close'] = pd.to_numeric(hist['close'], errors='coerce')
            hist['vol'] = pd.to_numeric(hist['vol'], errors='coerce').fillna(0)

            # 计算个股多周期动量
            records = []
            for code, grp in hist.groupby('ts_code'):
                grp = grp.sort_values('trade_date').tail(25)  # 最近25个交易日
                if len(grp) < 10:
                    continue

                close_now = grp['close'].iloc[-1]

                # mom_5: 近5日动量
                if len(grp) >= 6:
                    mom_5 = (close_now / grp['close'].iloc[-6] - 1) * 100
                else:
                    mom_5 = 0

                # mom_20: 近20日动量
                if len(grp) >= 21:
                    mom_20 = (close_now / grp['close'].iloc[-21] - 1) * 100
                else:
                    mom_20 = mom_5 * 4 if mom_5 != 0 else 0

                # 动量加速度: 最近5日 vs 前5日
                if len(grp) >= 11:
                    c_5d_ago = grp['close'].iloc[-6]
                    c_10d_ago = grp['close'].iloc[-11]
                    mom_acc = (c_5d_ago / c_10d_ago - 1) * 100
                else:
                    mom_acc = 0

                # 平均成交量
                avg_vol = grp['vol'].iloc[-5:].mean()

                records.append({
                    'ts_code': code,
                    'mom_5': mom_5,
                    'mom_20': mom_20,
                    'mom_acc': mom_acc,
                    'avg_vol': avg_vol
                })

            if not records:
                print("  [SectorMom] 无法计算动量，回退为0")
                return pd.Series(0.0, index=df.index)

            mom_df = pd.DataFrame(records)

            # 确保有行业数据 (从Tushare获取缺失的)
            df = df.copy()
            # 转换industry列为字符串以便检查
            if 'industry' in df.columns:
                df['industry'] = df['industry'].astype(str)
            empty_mask = (df['industry'].isna() | (df['industry'] == 'nan') |
                      (df['industry'] == '') | (df['industry'] == 'None'))
            if empty_mask.any():
                try:
                    import tushare as ts
                    from src.utils.config_loader import Config
                    token = Config.get('tushare_token', '')
                    if token:
                        ts.set_token(token)
                        pro = ts.pro_api()
                        codes = [c for c in df.loc[empty_mask, 'ts_code'].tolist() if c]
                        if codes:
                            info_df = pro.stock_basic(ts_code=','.join(codes[:500]), fields='ts_code,industry')
                            if not info_df.empty:
                                ind_map = dict(zip(info_df['ts_code'], info_df['industry']))
                                for idx in df[empty_mask].index:
                                    code = df.loc[idx, 'ts_code']
                                    if code in ind_map:
                                        df.loc[idx, 'industry'] = ind_map[code]
                except:
                    pass

            # 合并到股票池
            merged = df[['ts_code', 'industry']].merge(mom_df, on='ts_code', how='left')

            # 计算市场平均收益率
            merged['mom_20'] = merged['mom_20'].fillna(0)
            market_avg = merged['mom_20'].mean()

            # ===== 行业动量计算 =====

            # 1. 多周期动量 (先计算个股加权动量)
            merged['weighted_mom'] = 0.6 * merged['mom_5'].fillna(0) + 0.4 * merged['mom_20'].fillna(0)

            # 按行业聚合
            industry_mom = merged.groupby('industry')['weighted_mom'].mean()

            # 2. 相对强度 (vs 市场平均)
            industry_rel = industry_mom - market_avg

            # 3. 动量加速度
            industry_acc = merged.groupby('industry')['mom_acc'].mean()

            # 4. 成交量确认 (平均成交量)
            industry_vol = merged.groupby('industry')['avg_vol'].mean()

            # 5. 资金流向 (优先从Tushare获取，fallback money_flow表)
            industry_flow = pd.Series(dtype=float)
            has_flow = False

            # 方案1: Tushare (批量获取行业资金流向)
            try:
                import tushare as ts
                from src.utils.config_loader import Config

                token = Config.get('tushare_token', '')
                if token:
                    ts.set_token(token)
                    pro = ts.pro_api()

                    # 获取指数成分股代码 (沪深300)
                    try:
                        index_df = pro.index_weight(index_code='000300.SH')
                        codes = index_df['con_code'].tolist() if not index_df.empty else []
                    except:
                        codes = []

                    td = trade_date.replace('-', '')
                    flow_data = []

                    for code in codes[:300]:
                        ts_code = code + '.SH' if code.startswith('6') else code + '.SZ'
                        try:
                            df = pro.moneyflow(ts_code=ts_code, start_date=td, end_date=td)
                            if not df.empty:
                                net_amount = df.iloc[0].get('net_mf_amount', 0) or 0
                                flow_data.append({'ts_code': ts_code, 'net_amount': net_amount})
                        except:
                            continue

                    if flow_data:
                        flow_df = pd.DataFrame(flow_data)
                        # 获取行业信息并join
                        info_df = DBUtils.query_df("SELECT ts_code, industry FROM stock_info")
                        if not info_df.empty:
                            flow_df = flow_df.merge(info_df, on='ts_code', how='left')
                            flow_df['industry'] = flow_df['industry'].fillna('')
                            flow_df = flow_df[flow_df['industry'] != '']
                            if not flow_df.empty:
                                industry_flow = flow_df.groupby('industry')['net_amount'].mean()
                                has_flow = True
                                print(f"  [SectorMom] Tushare资金流向: {len(industry_flow)} 个行业")
            except Exception as e:
                print(f"  [SectorMom] Tushare获取失败: {e}")

            # 方案2: money_flow表
            if not has_flow:
                try:
                    flow_df = DBUtils.query_df("""
                        SELECT si.industry, mf.net_amount
                        FROM money_flow mf
                        INNER JOIN stock_info si ON mf.code = si.ts_code
                        WHERE mf.trade_date = (
                            SELECT MAX(trade_date) FROM money_flow
                        )
                        AND mf.net_amount IS NOT NULL
                    """)
                    if not flow_df.empty and 'industry' in flow_df.columns:
                        industry_flow = flow_df.groupby('industry')['net_amount'].mean()
                        has_flow = True
                except:
                    pass

            # ===== 综合评分 =====

            # 归一化各维度
            def norm(series):
                min_v, max_v = series.min(), series.max()
                if max_v > min_v:
                    return (series - min_v) / (max_v - min_v)
                return pd.Series(0.5, index=series.index)

            # 归一化各维度
            def norm(series):
                if series.empty:
                    return series
                min_v, max_v = series.min(), series.max()
                if max_v > min_v:
                    return (series - min_v) / (max_v - min_v)
                return pd.Series(0.5, index=series.index)

            if industry_mom.empty:
                print("  [SectorMom] 无行业动量数据，回退为0")
                return pd.Series(0.0, index=df.index)

            mom_norm = norm(industry_mom)
            rel_norm = norm(industry_rel)
            acc_norm = norm(industry_acc)

            # 资金流向归一化
            has_flow = not industry_flow.empty
            if has_flow:
                flow_norm = norm(industry_flow)
                # 有资金流向: 动量30% + 相对强度20% + 加速15% + 资金流向25% - 低量惩罚10%
                w_mom, w_rel, w_acc, w_flow = 0.30, 0.20, 0.15, 0.25
            else:
                # 无资金流向: 重新分配权重 (动量45% + 相对强度30% + 加速25%)
                flow_norm = pd.Series(0, index=industry_mom.index)
                w_mom, w_rel, w_acc, w_flow = 0.45, 0.30, 0.25, 0

            # 成交量过滤: 无量上涨扣分
            vol_penalty = pd.Series(0, index=industry_vol.index)
            vol_median = industry_vol.median()
            low_vol_mask = industry_vol < vol_median * 0.5
            vol_penalty[low_vol_mask] = -0.1

            # 综合评分
            sector_score = (w_mom * mom_norm + w_rel * rel_norm + w_acc * acc_norm +
                       w_flow * flow_norm + vol_penalty)
            sector_score = sector_score.clip(0, 1)

            # 映射回每只股票
            sector_scores = df['industry'].map(sector_score).fillna(0.5)

            valid_count = (df['industry'].notna()).sum()
            print(f"  [SectorMom] 增强版行业动量完成: {valid_count} 只股票有行业归属")

            # Debug: 输出Top5行业
            top5 = sector_score.sort_values(ascending=False).head(5)
            print(f"    Top5行业: {list(top5.items())[:5]}")

            return sector_scores.values

        except Exception as e:
            print(f"  [SectorMom] 增强计算失败: {e}，回退为0")
            return pd.Series(0.0, index=df.index)

    def _calculate_mv_preference_score(self, df):
        """计算市值偏好评分（中小盘优先）

        评分曲线（以亿元为单位）:
          - mv < min_yi        : 线性从0爬升到1（太小有流动性风险）
          - min_yi <= mv <= max_yi : 1.0（偏好区间，满分）
          - mv > max_yi        : 指数衰减到0（越大盘越低分）

        Args:
            df: 含 total_mv 列（单位：万元）的 DataFrame

        Returns:
            np.ndarray: 归一化市值偏好评分 [0, 1]
        """
        mv_wan = df['total_mv'].fillna(0).values.astype(float)
        mv_yi = mv_wan / 10000.0     # 万元 → 亿元

        min_yi = self.mv_prefer_min_yi
        max_yi = self.mv_prefer_max_yi

        scores = np.zeros(len(mv_yi))
        for i, mv in enumerate(mv_yi):
            if mv <= 0:
                scores[i] = 0.0
            elif mv < min_yi:
                # 太小：线性从 0 爬升到 1（鼓励最小门槛以上的小盘）
                scores[i] = mv / min_yi * 0.6
            elif mv <= max_yi:
                # 偏好区间：满分
                scores[i] = 1.0
            else:
                # 超出偏好区间：指数衰减
                # max_yi 处为1.0，每翻倍减半
                ratio = max_yi / max(mv, 1)   # 0 < ratio <= 1
                scores[i] = max(ratio ** 1.5, 0.05)

        print(f"  [MvPref] 偏好区间 {min_yi}~{max_yi}亿, "
              f"满分股票: {int((scores == 1.0).sum())} 只, "
              f"硬上限已过滤 >{self.max_mv_yi}亿")
        return scores

    def _calculate_layer_heat_score(self, df: pd.DataFrame, trade_date: str) -> 'np.ndarray':
        """AI赛道 Layer 热度评分（A轨专用，替代市值偏好）

        逻辑：
          1. 从 stock_pool 读取每只股票的 ai_layer 标签（layer1~layer5）
          2. 计算每个 Layer 内股票的近5日平均涨幅，作为该 Layer 当前热度
          3. 将 Layer 热度归一化到 [0,1]，映射回每只股票
          4. 无 ai_layer 标签的股票给中性分 0.5

        热度越高说明该 Layer 近期资金流入越强，是当前主线赛道。
        """
        try:
            # 1. 读取 stock_pool 的 ai_layer 标签
            layer_df = DBUtils.query_df(
                "SELECT ts_code, ai_layer FROM stock_pool WHERE is_active=1 AND ai_layer IS NOT NULL"
            )
            if layer_df.empty:
                print("  [LayerHeat] stock_pool 无 ai_layer 数据，热度评分全部默认0.5")
                return np.full(len(df), 0.5)

            code_to_layer = dict(zip(layer_df['ts_code'].astype(str), layer_df['ai_layer'].astype(str)))

            # 2. 计算近5日每只股票的涨幅（取 stock_daily 最近两个交易日）
            from datetime import timedelta
            cutoff = (pd.Timestamp(trade_date) - timedelta(days=10)).strftime('%Y-%m-%d')
            sql = """
            SELECT ts_code, trade_date, close
            FROM stock_daily
            WHERE trade_date >= ? AND trade_date <= ?
              AND close > 0
            ORDER BY ts_code, trade_date
            """
            hist = DBUtils.query_df(sql, params=[cutoff, trade_date])
            if hist.empty:
                return np.full(len(df), 0.5)

            hist['close'] = pd.to_numeric(hist['close'], errors='coerce')
            ret5: dict = {}
            for code, grp in hist.groupby('ts_code'):
                grp = grp.sort_values('trade_date')
                if len(grp) < 2:
                    continue
                c_now  = grp['close'].iloc[-1]
                c_prev = grp['close'].iloc[0]
                if c_prev > 0:
                    ret5[str(code)] = (c_now / c_prev - 1) * 100

            # 3. 按 layer 计算平均涨幅（热度）
            layer_returns: dict = {}
            for code, layer in code_to_layer.items():
                if layer.startswith('layer') and code in ret5:
                    layer_returns.setdefault(layer, []).append(ret5[code])

            layer_heat: dict = {}
            for layer, rets in layer_returns.items():
                layer_heat[layer] = float(np.mean(rets))

            if not layer_heat:
                return np.full(len(df), 0.5)

            # 4. 归一化 layer 热度到 [0, 1]
            min_h = min(layer_heat.values())
            max_h = max(layer_heat.values())
            if max_h > min_h:
                layer_norm = {k: (v - min_h) / (max_h - min_h) for k, v in layer_heat.items()}
            else:
                layer_norm = {k: 0.5 for k in layer_heat}

            # 打印各 Layer 热度
            heat_str = '  '.join(f"{k}={layer_norm[k]:.2f}({layer_heat[k]:+.1f}%)"
                                 for k in sorted(layer_norm))
            print(f"  [LayerHeat] {heat_str}")

            # 5. 映射回每只股票
            scores = np.array([
                layer_norm.get(code_to_layer.get(str(c), ''), 0.5)
                for c in df['ts_code']
            ])
            valid = int((scores != 0.5).sum())
            print(f"  [LayerHeat] 命中 ai_layer 标签: {valid}/{len(df)} 只")
            return scores

        except Exception as e:
            print(f"  [LayerHeat] 计算失败: {e}，默认0.5")
            return np.full(len(df), 0.5)

    # LLM输出的板块描述 → A股 stock_info.industry 常见名称的模糊映射表
    # 左边是LLM可能输出的词（子串匹配），右边是DB里的行业名关键词
    # LLM描述关键词 → DB中 stock_info.industry 名称关键词（子串匹配）
    # 右边填DB实际存在的行业名片段，可以是列表（OR匹配）
    _SECTOR_ALIAS = {
        # 能源
        '石油': ['石油开采', '石油加工', '石油贸易'],
        '油气': ['石油开采', '石油加工'],
        '原油': ['石油开采', '石油加工'],
        '能源化工': ['石油开采', '石油加工', '化工原料'],
        '煤炭': ['煤炭开采', '焦炭加工'],
        '采掘': ['煤炭开采'],
        # 化工
        '化工': ['化工原料', '染料涂料', '农药化肥', '日用化工', '化工机械'],
        'PTA': ['化工原料', '化纤'],
        '涤纶': ['化纤'],
        '化纤': ['化纤'],
        # 金属/有色
        '有色': ['小金属', '铝', '铜', '铅锌', '黄金'],
        '铜': ['铜'],
        '铝': ['铝'],
        '黄金': ['黄金'],
        '钢铁': ['普钢', '特种钢', '钢加工'],
        # 科技
        '人工智能': ['软件服务', '互联网', 'IT设备'],
        'AI': ['软件服务', '互联网'],
        '算力': ['软件服务', 'IT设备', '通信设备'],
        '芯片': ['半导体', '元器件'],
        '半导体': ['半导体', '元器件'],
        '消费电子': ['元器件', '家用电器', 'IT设备'],
        '科技': ['软件服务', '半导体', '元器件', '通信设备'],
        # 新能源/电力
        '新能源': ['电气设备', '新型电力'],
        '光伏': ['电气设备'],
        '风电': ['电气设备', '新型电力'],
        '储能': ['电气设备', '新型电力'],
        '电力': ['火力发电', '水力发电', '新型电力', '供气供热'],
        # 金融
        '银行': ['银行'],
        '券商': ['证券'],
        '证券': ['证券'],
        '保险': ['保险'],
        '金融': ['银行', '证券', '保险', '多元金融'],
        # 医药
        '医药': ['化学制药', '中成药', '生物制药', '医疗保健', '医药商业'],
        '生物': ['生物制药'],
        '医疗': ['医疗保健'],
        # 消费
        '白酒': ['白酒'],
        '食品': ['食品', '乳制品', '啤酒', '软饮料', '红黄酒'],
        '消费': ['食品', '白酒', '家用电器', '家居用品', '百货'],
        # 军工
        '军工': ['航空', '船舶', '运输设备'],
        '航空航天': ['航空'],
        # 建筑/地产
        '房地产': ['区域地产', '全国地产', '房产服务', '园区开发'],
        '建筑': ['建筑工程', '装修装饰'],
        # 交通运输
        '交通运输': ['仓储物流', '水运', '港口', '铁路', '航空', '空运', '公路', '路桥'],
        '航运': ['水运', '港口'],
        '物流': ['仓储物流'],
        '航空': ['航空', '空运', '机场'],
        # 汽车
        '汽车': ['汽车整车', '汽车配件', '汽车服务'],
        # 环保
        '环保': ['环境保护', '水务'],
    }

    def _match_industry(self, sector_desc: str, db_industry: str) -> bool:
        """判断LLM输出的板块描述是否匹配DB中的行业名（模糊匹配）"""
        if not db_industry:
            return False
        # 先尝试直接包含
        if sector_desc in db_industry or db_industry in sector_desc:
            return True
        # 再通过别名表做关键词匹配（值支持字符串或列表）
        for alias_key, industry_kws in self._SECTOR_ALIAS.items():
            if alias_key not in sector_desc:
                continue
            kws = industry_kws if isinstance(industry_kws, list) else [industry_kws]
            if any(kw in db_industry for kw in kws):
                return True
        return False

    def _apply_profit_warning_penalty(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        读取 qmt.profit_warnings，对预警股票的 final_score 按等级打折：
          🔴 红色 → × 0.3（几乎排除）
          🟡 黄色 → × 0.6（明显降权）
        同时在 df 上增加 profit_warning / warning_level 列，供 pool.html 展示。
        """
        df['profit_warning'] = ''
        df['warning_level'] = ''

        try:
            import pymysql
            mysql = Config.mysql if hasattr(Config, 'mysql') else {}
            conn = pymysql.connect(
                host=mysql.get('host', '192.168.3.41'),
                port=int(mysql.get('port', 3306)),
                user=mysql.get('user', 'root'),
                password=mysql.get('password', ''),
                database='qmt',
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                connect_timeout=5,
            )
            cur = conn.cursor()
            cur.execute(
                "SELECT stock_code, stock_name, level, profit_change_pct, signals "
                "FROM profit_warnings WHERE resolved_date IS NULL OR resolved_date = 'None'"
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as e:
            print(f"[ProfitWarning] 读取失败，跳过降权: {e}")
            return df

        if not rows:
            return df

        # stock_code 可能为空，用 stock_name 匹配 company_name
        warn_by_code: dict[str, dict] = {}
        warn_by_name: dict[str, dict] = {}
        for r in rows:
            code = (r.get('stock_code') or '').strip()
            name = (r.get('stock_name') or '').strip()
            if code:
                warn_by_code[code] = r
            if name:
                warn_by_name[name] = r

        PENALTY = {'红': 0.3, '黄': 0.6}

        matched = 0
        for idx, row in df.iterrows():
            code = row.get('ts_code', '')
            name = str(row.get('company_name', row.get('name', '')))
            warn = warn_by_code.get(code) or warn_by_name.get(name)
            if not warn:
                continue
            level_str = str(warn.get('level', ''))
            # 提取颜色关键字
            color = '红' if '红' in level_str else ('黄' if '黄' in level_str else '')
            penalty = PENALTY.get(color, 0.7)
            df.loc[idx, 'final_score'] *= penalty
            df.loc[idx, 'profit_warning'] = str(warn.get('signals', ''))
            df.loc[idx, 'warning_level'] = color
            matched += 1

        print(f"[ProfitWarning] 共 {len(rows)} 条预警，命中 {matched} 只，已对 final_score 降权")
        return df

    def _apply_memory_facts_bonus(self, df, memory_facts: list):
        """
        应用记忆事实加成：来自 MemoryService 的历史决策事实
        
        加成规则：
          - 高置信度(≥0.9)事实命中的股票 → +0.05
          - 中置信度(0.7-0.9)事实命中的股票 → +0.03
          - 近期已推荐过的股票（事实中有记录）→ 避免重复推荐，×0.92
        """
        if df.empty or not memory_facts:
            return df

        df = df.copy()
        bonus_col = pd.Series(0.0, index=df.index)
        penalty_col = pd.Series(1.0, index=df.index)

        for fact in memory_facts:
            content = str(fact.get('content', ''))
            confidence = float(fact.get('confidence', 0))
            tags = fact.get('tags', []) or []
            
            if confidence < 0.7:
                continue

            for tag in tags:
                tag_lower = str(tag).lower()
                mask_by_name = df['name'].str.contains(tag, na=False)
                mask_by_code = df['ts_code'].str.contains(tag.replace('.SH', '').replace('.SZ', ''), na=False)
                mask = mask_by_name | mask_by_code
                if not mask.any():
                    continue

                if confidence >= 0.9:
                    bonus_col[mask] += 0.05
                    print(f"  [MemoryBonus] 高置信事实 '{content[:30]}...' +0.05 → {mask.sum()}只")
                elif confidence >= 0.7:
                    bonus_col[mask] += 0.03
                    print(f"  [MemoryBonus] 中置信事实 '{content[:30]}...' +0.03 → {mask.sum()}只")

        df['final_score'] = df['final_score'] + bonus_col
        total_bonus = (bonus_col > 0).sum()
        if total_bonus > 0:
            print(f"  [MemoryBonus] 共 {total_bonus} 只股票获得记忆加成")

        return df

    def _apply_external_signals(self, df, trade_date):
        """
        应用外部信号加成：期货板块信号、龙虎榜机构信号、政策板块信号

        加成规则：
          - 期货 BUY 信号 → 对应行业 +0.1 到 sector_momentum_score
          - 龙虎榜净买入 > 500万 → +0.03 到 final_score
          - 政策利好板块 → +0.05 到 event_score（新闻感知已覆盖，此处做兜底）
        """
        if df.empty:
            return df

        bonus_col = pd.Series(0.0, index=df.index)
        mom_col   = pd.Series(0.0, index=df.index)
        event_col = pd.Series(0.0, index=df.index)

        # 1. 期货板块信号
        try:
            from src.analysis.research_runner import ResearchRunner
            fut_signals = ResearchRunner.get_futures_signals(trade_date)
            if fut_signals:
                # 期货信号 → 行业匹配
                for sector, sig in fut_signals.items():
                    if sig.get('signal') == 'BUY' and sig.get('strength', 0) > 0:
                        mask = df['industry'].str.contains(sector, na=False)
                        if mask.any():
                            mom_col[mask] += 0.1
                            print(f"  [FuturesSignal] {sector} BUY信号，行业{mask.sum()}只股票 +0.1")
                    elif sig.get('signal') == 'SELL' and sig.get('strength', 0) > 0:
                        mask = df['industry'].str.contains(sector, na=False)
                        if mask.any():
                            mom_col[mask] -= 0.05
        except Exception as e:
            print(f"  [FuturesSignal] 读取失败: {e}")

        # 2. 龙虎榜机构信号
        try:
            inst_signals = ResearchRunner.get_institutional_signals(trade_date)
            if inst_signals:
                for code, net_amount in inst_signals.items():
                    mask = df['ts_code'] == code
                    if mask.any():
                        if net_amount >= 500:  # 净买入 ≥ 500万
                            bonus_col[mask] += 0.03
                            print(f"  [InstSignal] {code} 净买入{net_amount:.0f}万，+0.03")
                        elif net_amount <= -500:  # 净卖出 ≥ 500万
                            bonus_col[mask] -= 0.02
                            print(f"  [InstSignal] {code} 净卖出{abs(net_amount):.0f}万，-0.02")
        except Exception as e:
            print(f"  [InstSignal] 读取失败: {e}")

        # 3. 政策板块信号
        try:
            news_signals = ResearchRunner.get_news_sector_signals(trade_date)
            if news_signals:
                for sector, strength in news_signals.items():
                    if strength > 0:
                        mask = df['industry'].str.contains(sector, na=False)
                        if mask.any():
                            event_col[mask] += 0.05
        except Exception as e:
            print(f"  [NewsSector] 读取失败: {e}")

        # 4. 北向情绪：市场整体信号强度
        try:
            sentiment = ResearchRunner.get_market_sentiment(trade_date)
            sentiment_mult = sentiment.get('score', 0.5)
            # 情绪 > 0.6（看多）→ 总分加成；情绪 < 0.4（看空）→ 总分打折
            if sentiment_mult >= 0.65:
                bonus_col += 0.02
                print(f"  [Sentiment] 北向情绪看多({sentiment_mult:.2f})，全体+0.02")
            elif sentiment_mult <= 0.35:
                bonus_col -= 0.03
                print(f"  [Sentiment] 北向情绪看空({sentiment_mult:.2f})，全体-0.03")
        except Exception as e:
            print(f"  [Sentiment] 读取失败: {e}")

        # 应用加成
        df['sector_momentum_score'] = df['sector_momentum_score'].fillna(0.0) + mom_col
        df['event_score'] = df['event_score'].fillna(0.0) + event_col
        df['final_score'] = df['final_score'] + bonus_col

        return df

    def _apply_news_boost(self, df, event_scores, news_boost_sectors):
        """对新闻检测到的利好板块进行事件评分加成

        Args:
            df: 股票池 DataFrame（含 industry 列）
            event_scores: pd.Series, index=ts_code, 原始事件评分
            news_boost_sectors: list of str, 新闻检测到的利好行业名称（LLM输出）

        Returns:
            pd.Series: 加成后的事件评分
        """
        if not news_boost_sectors:
            return event_scores

        boosted = event_scores.copy()
        boost_count = 0
        # 预先建立 ts_code → industry 的映射，避免逐行查询
        code_to_industry = df.set_index('ts_code')['industry'].to_dict()

        for code, db_industry in code_to_industry.items():
            db_industry = str(db_industry or '')
            matched = any(
                self._match_industry(sector, db_industry)
                for sector in news_boost_sectors
            )
            if matched:
                orig = boosted.get(code, 0.0)
                boosted[code] = min(max(orig * 1.5, 0.4), 1.0)
                boost_count += 1

        print(f"  [NewsBoost] 新闻利好板块 {news_boost_sectors[:5]}，"
              f"加成了 {boost_count} 只股票的事件评分")
        return boosted

    def _apply_sector_rotation_filter(self, df, trade_date, top_pct=0.40, prev_picks=None):
        """行业轮动过滤：只保留近20日动量最强的前 top_pct 行业内的股票。
        优先读 stock_factors；表不存在则从 stock_daily 计算。
        若数据仍不足或行业数不足，则透传全量股票（不过滤）。
        prev_picks: 上期推荐的 ts_code 集合，即使行业轮出也强制保留在候选池，
                    确保换手限制能真正保护老股（不会因行业过滤而直接消失）。
        """
        try:
            factors_df = pd.DataFrame()
            try:
                sql = """
                SELECT ts_code, mom_20
                FROM stock_factors
                WHERE trade_date = (
                    SELECT MAX(trade_date) FROM stock_factors WHERE trade_date <= ?
                ) AND mom_20 IS NOT NULL
                """
                factors_df = DBUtils.query_df(sql, params=[trade_date])
            except Exception:
                pass

            if factors_df.empty:
                factors_df = self._get_mom20_from_daily(trade_date)

            if factors_df.empty or 'industry' not in df.columns:
                return df

            merged = df[['ts_code', 'industry']].merge(
                factors_df[['ts_code', 'mom_20']], on='ts_code', how='left'
            )
            ind_mom = merged.groupby('industry')['mom_20'].mean().dropna()
            if len(ind_mom) < 3:
                return df

            n_top = max(2, int(len(ind_mom) * top_pct))
            top_inds = set(ind_mom.nlargest(n_top).index.tolist())
            # 行业未知的股票保留；上期推荐的老股无论行业是否轮出都保留
            prev_mask = df['ts_code'].isin(prev_picks) if prev_picks else pd.Series(False, index=df.index)
            filtered = df[df['industry'].isin(top_inds) | df['industry'].isna() | prev_mask]
            held_over = int(prev_mask.sum()) - int(df[df['industry'].isin(top_inds) & prev_mask].shape[0])
            print(f"  [SectorRotation] 强势行业 {n_top}/{len(ind_mom)} 个, "
                  f"股票池: {len(df)} → {len(filtered)} 只"
                  + (f"（含{held_over}只老股强制保留）" if held_over > 0 else ""))
            filtered = filtered if len(filtered) >= 20 else df

            # ── 反追高过滤：近N日已涨超M%的股票排除（高位接盘风险） ──
            chase_days = self.anti_chase_days
            chase_pct  = self.anti_chase_pct / 100.0
            try:
                sql_chase = """
                SELECT t_now.ts_code,
                       (t_now.close / t_prev.close - 1) AS ret_n
                FROM stock_daily t_now
                JOIN stock_daily t_prev ON t_now.ts_code = t_prev.ts_code
                WHERE t_now.trade_date  = ?
                  AND t_prev.trade_date = (
                      SELECT trade_date FROM stock_daily
                      WHERE trade_date <= t_now.trade_date
                      ORDER BY trade_date DESC
                      LIMIT 1 OFFSET ?
                  )
                  AND t_prev.close > 0
                """
                chase_df = DBUtils.query_df(sql_chase, params=[trade_date, chase_days])
                if not chase_df.empty:
                    chased = set(chase_df[chase_df['ret_n'] > chase_pct]['ts_code'].astype(str))
                    before = len(filtered)
                    filtered = filtered[~filtered['ts_code'].isin(chased)]
                    removed = before - len(filtered)
                    if removed > 0:
                        print(f"  [AntiChase] 剔除近{chase_days}日涨幅>{chase_pct*100:.0f}%的追高股 "
                              f"{removed} 只")
            except Exception as e:
                print(f"  [AntiChase] 计算失败: {e}，跳过")

            # ── 无基本面数据 + 近20日大涨 → 纯炒作妖股，踢出 ──
            try:
                sql_ghost = """
                SELECT t_now.ts_code,
                       t_now.close / t_prev.close - 1 AS ret_20
                FROM stock_daily t_now
                JOIN (
                    SELECT ts_code, close
                    FROM (
                        SELECT ts_code, close,
                               ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                        FROM stock_daily
                        WHERE trade_date <= ?
                          AND close > 0
                    ) ranked
                    WHERE rn = 21
                ) t_prev ON t_now.ts_code = t_prev.ts_code
                WHERE t_now.trade_date = ?
                  AND t_now.roe IS NULL
                  AND t_now.netprofit_yoy IS NULL
                """
                ghost_df = DBUtils.query_df(sql_ghost, params=[trade_date, trade_date])
                if not ghost_df.empty:
                    ghost_codes = set(ghost_df[ghost_df['ret_20'] > 0.30]['ts_code'].astype(str))
                    before = len(filtered)
                    filtered = filtered[~filtered['ts_code'].isin(ghost_codes)]
                    removed = before - len(filtered)
                    if removed > 0:
                        print(f"  [GhostFilter] 剔除无基本面+近20日涨>30%的妖股 {removed} 只")
            except Exception as e:
                print(f"  [GhostFilter] 计算失败: {e}，跳过")

            return filtered if len(filtered) >= 5 else df
        except Exception as e:
            print(f"  [SectorRotation] 过滤异常: {e}，跳过")
            return df

    def _calculate_product_moat_score(self, df):
        """产品护城河因子（轨道B专用）

        逻辑：真正有护城河的企业，产品有定价权（毛利率高）且资本回报持续高（ROE高）。
        两者同时高 → 竞争壁垒真实存在；只有其一 → 可能是周期性或财务杠杆驱动。

        三个维度：
          1. 定价权（毛利率档位，60%）：
               GPR≥60% → 软件/医药/消费品，极强定价权，1.0
               GPR≥40% → 品牌消费/特色制造，0.8
               GPR≥25% → 普通制造业优势，0.5
               GPR≥15% → 低利润代工，0.2
               GPR<15%  → 无定价权，0.0
          2. 资本效率（ROE排名，25%）：ROE越高，竞争壁垒越可持续
          3. 护城河一致性（15%）：GPR和ROE同时位于行业前50%才计满分，
               防止"毛利高但ROE低（存货/应收账款侵蚀）"或"ROE高但毛利低（杠杆驱动）"

        Returns:
            pd.Series: 产品护城河评分 [0, 1]，index 与 df 对齐
        """
        gpr = df.get('gpr', pd.Series(np.nan, index=df.index))
        roe = df.get('roe', pd.Series(np.nan, index=df.index))

        has_gpr = gpr.notna().sum() > len(df) * 0.2

        # ── 定价权：毛利率分档（绝对门槛，不依赖排名）──
        def gpr_tier(g):
            if pd.isna(g): return 0.3   # 缺数据取保守中间值
            if g >= 60:   return 1.0    # 极强定价权（软件/医药/高端消费）
            if g >= 40:   return 0.8    # 品牌/特色制造
            if g >= 25:   return 0.5    # 普通制造业竞争优势
            if g >= 15:   return 0.2    # 低利润代工
            return 0.0                  # 无定价权

        if has_gpr:
            pricing_power = gpr.apply(gpr_tier)
        else:
            # 无毛利数据时退化为纯ROE排名代理定价权
            pricing_power = pd.Series(0.3, index=df.index)

        # ── 资本效率：ROE排名归一化 ──
        has_roe = roe.notna()
        roe_rank = pd.Series(0.5, index=df.index)
        if has_roe.sum() >= 2:
            r = roe[has_roe].rank(ascending=True, method='average')
            roe_rank[has_roe] = (r - 1) / (has_roe.sum() - 1)

        # ── 护城河一致性：GPR和ROE同时高才算真护城河 ──
        if has_gpr:
            gpr_pct = gpr.rank(pct=True).fillna(0.5)
            roe_pct = roe.rank(pct=True).fillna(0.5)
            # 短板效应：取最小值，两者都高才高分
            consistency = pd.concat([gpr_pct, roe_pct], axis=1).min(axis=1)
        else:
            consistency = roe_rank.copy()

        moat_score = (0.60 * pricing_power.values
                      + 0.25 * roe_rank.values
                      + 0.15 * consistency.values)

        result = pd.Series(moat_score, index=df.index)
        strong = (result >= 0.6).sum()
        print(f"  [ProductMoat] 强护城河股票(≥0.6): {strong} 只  "
              f"均值={result.mean():.3f}  {'GPR数据可用' if has_gpr else 'GPR缺失→ROE代理'}")
        return result

    # AI 赛道行业关键词（半导体/算力硬件/电子/通信设备）—— 给B轨评分加成
    AI_TRACK_INDUSTRIES = [
        '半导体', '集成电路', '芯片',
        '电子', '电子元件', '光学光电子',
        '计算机设备', '计算机硬件',
        '通信设备', '通信',
        '人工智能', '算力',
    ]

    def _calculate_value_score(self, df):
        """价值质量评分（轨道B）：成长×盈利×产品护城河×估值×盈余质量 + AI赛道加持。

        权重（含产品护城河因子）：
          成长      25%  — 净利润同比增速
          盈利      20%  — ROE + 毛利率
          市场护城河 15%  — 毛利率 + 市值规模（原moat）
          产品护城河 15%  — 定价权（GPR档位）× 资本效率（ROE）× 一致性
          估值      20%  — PE/PEG（低估值高分）
          盈余质量   5%  — ROE-GPR同向一致性（防财务粉饰）

        AI赛道加持：半导体/算力/电子/通信设备行业的股票，最终得分×1.10
        """
        def rank_norm(s):
            s = pd.Series(s, index=df.index) if not isinstance(s, pd.Series) else s
            has = s.notna()
            result = pd.Series(0.5, index=s.index)
            if has.sum() < 2:
                return result
            rank = s[has].rank(ascending=True, method='average')
            result[has] = (rank - 1) / (has.sum() - 1)
            return result

        yoy = df.get('netprofit_yoy', pd.Series(np.nan, index=df.index))
        roe = df.get('roe', pd.Series(np.nan, index=df.index))
        gpr = df.get('gpr', pd.Series(np.nan, index=df.index))
        pe  = df['pe_ttm'].replace(0, np.nan)

        growth = rank_norm(yoy.clip(-50, 300))

        if gpr.notna().sum() > len(df) * 0.2:
            profit = 0.6 * rank_norm(roe) + 0.4 * rank_norm(gpr)
            moat   = 0.6 * rank_norm(gpr) + 0.4 * rank_norm(
                np.log(df['total_mv'].clip(lower=1)))
            roe_pct = roe.rank(pct=True).fillna(0.5)
            gpr_pct = gpr.rank(pct=True).fillna(0.5)
            ar_quality = pd.concat([roe_pct, gpr_pct], axis=1).min(axis=1)
        else:
            profit = rank_norm(roe)
            moat   = rank_norm(np.log(df['total_mv'].clip(lower=1)))
            ar_quality = pd.Series(0.5, index=df.index)

        product_moat = self._calculate_product_moat_score(df)

        peg = (pe / (yoy / 100).clip(lower=0.05)).where(
            yoy.notna() & (yoy > 5), other=np.nan)
        val_pe = rank_norm(-pe.clip(upper=80))
        valuation = val_pe.copy()
        has_peg = peg.notna()
        valuation[has_peg] = 0.4 * val_pe[has_peg] + 0.6 * rank_norm(-peg)[has_peg]

        score = (0.25 * growth.values
                 + 0.20 * profit.values
                 + 0.15 * moat.values
                 + 0.15 * product_moat.values
                 + 0.20 * valuation.values
                 + 0.05 * ar_quality.values)
        result = pd.Series(score, index=df.index)

        # ── AI 赛道加持：半导体/算力硬件/电子/通信设备 × 1.10 ──
        if 'industry' in df.columns:
            ai_pattern = '|'.join(self.AI_TRACK_INDUSTRIES)
            is_ai = df['industry'].str.contains(ai_pattern, na=False)
            ai_count = is_ai.sum()
            if ai_count > 0:
                result[is_ai] = (result[is_ai] * 1.10).clip(upper=1.0)
                print(f"  [AIBoost] AI赛道行业加持 ×1.10: {ai_count} 只")

        return result

    # ──────────────────────────────────────────────────────────────
    # 红利B轨：可持续性评分 + 过滤器
    # ──────────────────────────────────────────────────────────────

    # 各 company_type 的稳定性基础分（越稳定越高）
    _DIV_TYPE_STABILITY = {
        'cashflow':       1.00,   # 公用事业/交通：监管费率保护，现金流极稳
        'rate_sensitive': 0.85,   # 银行/保险：利率周期影响，但长期可持续
        'brand':          0.75,   # 白酒/消费品：定价权强，但受消费周期影响
        'resource':       0.55,   # 煤炭/石化：大宗价格波动大，分红不稳
    }

    def _apply_dividend_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """红利B轨入场门槛过滤

        比价值轨道更宽松：
          - 去ST/退市
          - 市值 > 0（数据异常排除）
          - ROE >= 4%（银行/公用事业稳定低ROE也算）
          - 净利润不能连续崩溃（yoy >= -25%）
          - 不限PE上限（银行PE就5-8x，限PE会误伤）
        """
        before = len(df)
        df = df[~df['name'].str.contains('ST|退', na=False)]
        df = df[df['total_mv'] > 0]

        roe_valid = df['roe'].notna().mean() if 'roe' in df.columns else 0.0
        if roe_valid > 0.15:
            df = df[df['roe'].fillna(0) >= 4]

        if 'netprofit_yoy' in df.columns:
            # 放宽yoy限制，不再硬性过滤
            mask = df['netprofit_yoy'].isna() | (df['netprofit_yoy'] >= -50)
            df = df[mask]

        df = df.reset_index(drop=True)
        
        # B轨也需要sector_momentum_score列和final_score
        if 'sector_momentum_score' not in df.columns:
            df['sector_momentum_score'] = 0.0
        if 'layer_heat_score' not in df.columns:
            df['layer_heat_score'] = 0.5
        if 'final_score' not in df.columns:
            df['final_score'] = df.get('score', 0.5)
            
        print(f"  [DividendFilter] {before} → {len(df)} 只（ROE≥4%, yoy≥-25%）")
        return df

    def _calculate_dividend_sustainability_score(self, df: pd.DataFrame) -> pd.Series:
        """红利可持续性评分（B轨专用）

        核心问题：今年分红，明年还能分吗？

        四个维度：
          ROE水平×稳定性  35%  — ROE越高越好；yoy在合理区间（不暴涨暴跌）
          净利润不下滑    20%  — yoy >= 0 得满分，每下滑1%扣分，<-15%清零
          估值留空间      25%  — PE合理区间内才有分红能力；PE过高说明市场透支
          公司类型稳定    20%  — cashflow最稳，resource最不稳（大宗商品波动大）
        """
        n = len(df)
        if n == 0:
            return pd.Series(dtype=float)

        def rank_norm(s, ascending=True):
            s = pd.to_numeric(s, errors='coerce')
            has = s.notna()
            result = pd.Series(0.5, index=s.index)
            if has.sum() < 2:
                return result
            r = s[has].rank(ascending=ascending, method='average')
            result[has] = (r - 1) / (has.sum() - 1)
            return result

        # ── 1. ROE 水平与稳定性（35%）──
        roe = pd.to_numeric(df.get('roe', pd.Series(np.nan, index=df.index)),
                            errors='coerce')
        yoy = pd.to_numeric(df.get('netprofit_yoy', pd.Series(np.nan, index=df.index)),
                            errors='coerce')

        # ROE 绝对水平打分（非相对排名）：>15% 满分，6~15% 线性，<6% 降权
        def roe_abs(r):
            if pd.isna(r): return 0.4
            if r >= 15:    return 1.0
            if r >= 10:    return 0.8
            if r >= 6:     return 0.6 + (r - 6) / 40
            if r >= 4:     return 0.4
            return 0.2

        roe_level = roe.apply(roe_abs)

        # yoy 稳定性：在 -5%~30% 区间最好（说明业务稳定增长）；
        # 暴涨（>50%）可能是低基数，说明前一年差；暴跌（<-10%）分红能力存疑
        def yoy_stability(y):
            if pd.isna(y): return 0.5
            if -5 <= y <= 30:  return 1.0        # 最稳定区间
            if 30 < y <= 50:   return 0.85       # 稳健增长
            if -10 <= y < -5:  return 0.7        # 小幅下滑，可接受
            if 50 < y <= 100:  return 0.65       # 高增速=低基数，不稳定
            if -20 <= y < -10: return 0.4        # 明显下滑
            if y > 100:        return 0.5        # 暴增异常，存疑
            return 0.1                           # 严重下滑

        yoy_stab = yoy.apply(yoy_stability)
        roe_score = 0.6 * roe_level + 0.4 * yoy_stab

        # ── 2. 净利润不下滑（20%）──
        def yoy_trend(y):
            if pd.isna(y): return 0.5
            if y >= 0:      return 1.0
            if y >= -5:     return 0.8
            if y >= -10:    return 0.6
            if y >= -15:    return 0.4
            return 0.1

        decline_score = yoy.apply(yoy_trend)

        # ── 3. 估值留空间（25%）——PE合理区间内才有分红能力 ──
        pe = pd.to_numeric(df['pe_ttm'].replace(0, np.nan), errors='coerce')
        ctype = df.get('company_type', pd.Series('cashflow', index=df.index))

        # 不同类型对应的合理PE区间（超出区间扣分，极高PE=市场透支，无分红空间）
        pe_bounds = {
            'rate_sensitive': (4,  15),   # 银行/保险：4~15x合理
            'cashflow':       (8,  25),   # 公用事业：8~25x合理
            'resource':       (5,  18),   # 资源：5~18x（周期股用低PE买）
            'brand':          (12, 35),   # 消费品：12~35x合理
        }

        def pe_score(pe_val, ctype_val):
            if pd.isna(pe_val) or pe_val <= 0:
                return 0.5   # 无PE数据，中性
            lo, hi = pe_bounds.get(str(ctype_val), (8, 30))
            if lo <= pe_val <= hi:
                return 1.0                          # 合理区间，满分
            elif pe_val < lo:
                # 低于合理区间下限：超便宜（可能有问题，但对分红是好事）
                return 0.8
            else:
                # 超出上限：越贵越无分红空间
                ratio = hi / max(pe_val, 1)         # 0 < ratio < 1
                return max(ratio ** 1.5, 0.1)

        val_score = pd.Series(
            [pe_score(p, c) for p, c in zip(pe, ctype)],
            index=df.index
        )

        # ── 4. 公司类型稳定溢价（20%）──
        type_score = ctype.map(self._DIV_TYPE_STABILITY).fillna(0.6)

        # ── 合并 ──
        score = (0.35 * roe_score.values
                 + 0.20 * decline_score.values
                 + 0.25 * val_score.values
                 + 0.20 * type_score.values)
        result = pd.Series(score, index=df.index)

        # 打印分布
        strong = (result >= 0.7).sum()
        mid    = ((result >= 0.5) & (result < 0.7)).sum()
        weak   = (result < 0.5).sum()
        print(f"  [DivSustain] 可持续性分布: 强≥0.7={strong}只  中0.5-0.7={mid}只  弱<0.5={weak}只  "
              f"均值={result.mean():.3f}")
        return result

    # 价值轨道排除行业：软件和医药的高GPR源于商业模式/专利，不是产品护城河，排除避免干扰
    # 金融行业（银行/保险）虽然ROE由杠杆驱动，但低PE+高股息本身就是价值信号，不排除；
    # GPR缺失时 _calculate_product_moat_score 已自动退化为ROE代理，不影响排名。
    VALUE_EXCLUDE_INDUSTRIES = [
        '软件', '计算机软件', '互联网软件',           # 软件行业
        '医药', '医疗', '医疗器械', '生物制品',        # 医疗行业
        '化学制药', '中药', '药品',                    # 制药行业
    ]

    def _apply_value_filter(self, df):
        """价值轨道质量门槛过滤（含数据稀疏自动降级）

        数据完整时（ROE有效率>20%）：严格 ROE≥8 + PE<80 + yoy≥-30
        数据稀疏时（fast_sync 仅有 pe_ttm）：降级为 PE<50 + 市值>50亿 (PE-only 价值过滤)
        额外排除：软件和医药行业（GPR天然高，不反映真实产品护城河）
        """
        df = df[~df['name'].str.contains('ST|退', na=False)]

        # 排除软件和医药行业（关键词匹配 industry 列）
        if 'industry' in df.columns:
            exclude_pattern = '|'.join(self.VALUE_EXCLUDE_INDUSTRIES)
            excluded_mask = df['industry'].str.contains(exclude_pattern, na=False)
            removed_ind = excluded_mask.sum()
            df = df[~excluded_mask]
            if removed_ind > 0:
                print(f"  [ValueFilter] 排除软件/医药行业: {removed_ind} 只")

        roe_valid_rate = df['roe'].notna().mean() if 'roe' in df.columns else 0.0

        if roe_valid_rate > 0.20:
            # 完整财务数据模式
            df = df[df['total_mv'] >= 20 * 10000]
            df = df[(df['pe_ttm'] > 0) & (df['pe_ttm'] < 80)]
            df = df[df['roe'].fillna(0) >= 8]
            # yoy：-50% ~ 300%（上限与打分 clip 对齐，>300% 几乎必然是扣非亏损低基数效应）
            mask_yoy = df['netprofit_yoy'].isna() | (df['netprofit_yoy'].between(-50, 300))
            before = len(df)
            df = df[mask_yoy]
            removed = before - len(df)
            if removed > 0:
                print(f"  [ValueFilter] 剔除净利润暴增>300%（疑似扣非亏损/低基数）: {removed} 只")
            print(f"  [ValueFilter] 完整财务模式，过滤后: {len(df)} 只")
        else:
            # PE-only 降级模式（fast_sync 环境）：只过滤PE范围，同样加yoy上限
            df = df[(df['pe_ttm'] > 3) & (df['pe_ttm'] < 60)]
            mask_yoy = df['netprofit_yoy'].isna() | (df['netprofit_yoy'] <= 300)
            df = df[mask_yoy]
            print(f"  [ValueFilter] PE-only 降级模式 (ROE数据不足), 过滤后: {len(df)} 只")
        return df

    def _calculate_fundamental_score(self, df):
        """计算基本面评分

        策略:
          - 如果有 ROE 数据, 使用 ROE 排名归一化
          - 否则使用 PE 倒数 (1/PE) 排名归一化
          - 最终归一化到 [0, 1]

        Args:
            df: 股票池 DataFrame (需包含 roe, pe_ttm 列)

        Returns:
            pd.Series: 基本面评分, index 与 df 对齐
        """
        n = len(df)
        if n == 0:
            return pd.Series(dtype=float)

        # 尝试用 ROE
        if 'roe' in df.columns and df['roe'].notna().sum() > n * 0.3:
            # 超过30%的股票有ROE数据, 使用ROE排名
            roe_filled = df['roe'].fillna(df['roe'].median())
            # 排名归一化: 排名越高(ROE越大)得分越高
            rank = roe_filled.rank(method='average', ascending=True)
            score = (rank - 1) / max(n - 1, 1)
            print(f"  [Fundamental] 使用ROE排名, 有效数据: {df['roe'].notna().sum()}/{n}")
        else:
            # 使用 PE 倒数
            pe = df['pe_ttm'].copy()
            pe = pe.replace(0, np.nan)
            pe_inv = 1.0 / pe.abs().clip(lower=1)  # 避免除零, PE越小得分越高
            pe_inv = pe_inv.fillna(0)
            rank = pe_inv.rank(method='average', ascending=True)
            score = (rank - 1) / max(n - 1, 1)
            print(f"  [Fundamental] 使用PE倒数排名, 有效数据: {pe.notna().sum()}/{n}")

        return score.values

    def _normalize_ai_scores(self, scores):
        """将AI评分归一化到 [0, 1]

        Args:
            scores: pd.Series of ai_score

        Returns:
            pd.Series: 归一化后的评分
        """
        min_val = scores.min()
        max_val = scores.max()
        if pd.isna(min_val) or pd.isna(max_val) or max_val == min_val:
            return scores.fillna(0)
        return ((scores - min_val) / (max_val - min_val)).fillna(0)

    # ------------------------------------------------------------------
    # 评分历史与稳定性控制
    # ------------------------------------------------------------------

    def _ensure_score_history_table(self):
        """创建 score_history 表（幂等），用于存储每日个股评分，支持 EMA 平滑"""
        try:
            DBUtils.execute("""
            CREATE TABLE IF NOT EXISTS score_history (
                trade_date   VARCHAR(20) NOT NULL,
                ts_code      VARCHAR(15) NOT NULL,
                raw_score    DOUBLE,
                ema_score    DOUBLE,
                track        VARCHAR(20),
                PRIMARY KEY (trade_date, ts_code)
            )
            """)
        except Exception as e:
            print(f"[ScoreHistory] 建表跳过: {e}")

    def _load_score_history(self, days=10):
        """加载最近 N 天的评分历史

        Returns:
            dict: {ts_code: [(date, raw_score, ema_score), ...]}  按日期升序
        """
        try:
            df = DBUtils.query_df(
                "SELECT trade_date, ts_code, raw_score, ema_score "
                "FROM score_history ORDER BY trade_date DESC LIMIT ?",
                params=[days * 50]  # 每天最多50只，保守上限
            )
            if df.empty:
                return {}
            history: dict = {}
            for _, row in df.iterrows():
                code = str(row['ts_code'])
                history.setdefault(code, []).append((
                    str(row['trade_date']),
                    float(row['raw_score'] or 0),
                    float(row['ema_score'] or 0),
                ))
            # 每个股票的记录按日期升序
            for code in history:
                history[code].sort(key=lambda x: x[0])
            return history
        except Exception as e:
            print(f"[ScoreHistory] 加载历史失败: {e}")
            return {}

    def _apply_score_ema(self, df: pd.DataFrame, history: dict,
                         score_col: str = 'final_score') -> pd.Series:
        """对评分应用 EMA 平滑：new_ema = α × today + (1-α) × prev_ema

        如果某股票历史不足 min_score_history 天，则降低 α（更相信当日评分）。
        无历史的股票直接用当日评分。

        Args:
            df: 含 final_score 的 DataFrame
            history: 评分历史字典
            score_col: 原始评分列名

        Returns:
            pd.Series: 平滑后的评分
        """
        alpha = self.score_ema_alpha
        min_hist = self.min_score_history
        raw_scores = df[score_col].values.copy()
        ema_scores = raw_scores.copy()
        ema_applied = 0

        for i, code in enumerate(df['ts_code']):
            code = str(code)
            hist = history.get(code, [])
            if not hist:
                continue  # 无历史，直接用当日评分

            prev_ema = hist[-1][2]  # 上期 EMA
            if prev_ema <= 0 and len(hist) < min_hist:
                continue  # 历史不足且上期EMA为0，不平滑

            # 动态 α：历史越短越相信当日评分
            days_available = len(hist)
            if days_available < min_hist:
                dynamic_alpha = alpha * (days_available / min_hist)
            else:
                dynamic_alpha = alpha

            ema_scores[i] = dynamic_alpha * raw_scores[i] + (1 - dynamic_alpha) * prev_ema
            ema_applied += 1

        if ema_applied > 0:
            print(f"  [ScoreEMA] 对 {ema_applied}/{len(df)} 只股票应用 EMA 平滑 "
                  f"(α={alpha}, 最小历史={min_hist}天)")

        return pd.Series(ema_scores, index=df.index)

    def _apply_grace_period(self, df: pd.DataFrame, history: dict,
                            top_n: int) -> pd.DataFrame:
        """淘汰缓冲期：最近 grace_period_days 天曾在 top_n 内的股票，
        即使今日评分不够，也给予缓冲加分，避免一日游。

        Args:
            df: 已评分的候选 DataFrame（按 final_score 降序）
            history: 评分历史
            top_n: 每日选股数量（用于判断"曾在前列"）

        Returns:
            df 增加 grace_bonus 列
        """
        grace_days = self.grace_period_days
        df['grace_bonus'] = 0.0

        # 收集近 grace_days 天曾在 top_n 内的股票
        grace_codes: set = set()
        grace_count: dict = {}  # code → 出现次数
        for code, hist in history.items():
            # 只看最近 grace_days 天的记录
            recent = hist[-grace_days:] if len(hist) >= grace_days else hist
            if len(recent) < 2:
                continue  # 历史太短，无法判断
            # 如果该股票在最近几天内多次出现（说明是常客），给缓冲
            grace_codes.add(code)
            grace_count[code] = len(recent)

        if not grace_codes:
            return df

        # 对 grace_codes 中的股票，如果今日不在 top_n 内，给小幅加分
        current_top = set(df.head(top_n)['ts_code'].astype(str))
        for i, code in enumerate(df['ts_code']):
            code = str(code)
            if code not in grace_codes:
                continue
            if code in current_top:
                continue  # 已在前排，不需要缓冲
            # 缓冲加分：出现越频繁加分越多，最多 +5%
            freq = grace_count.get(code, 1)
            bonus = min(0.05, freq * 0.015)
            df.iloc[i, df.columns.get_loc('final_score')] *= (1 + bonus)
            df.iloc[i, df.columns.get_loc('grace_bonus')] = bonus

        applied = int((df['grace_bonus'] > 0).sum())
        if applied > 0:
            print(f"  [GracePeriod] 给 {applied} 只近期常客缓冲加分（最多+5%）")

        return df

    def _save_scores_to_history(self, df: pd.DataFrame, trade_date: str):
        """将今日评分写入 score_history 表"""
        if df.empty:
            return
        try:
            # 先删除今日旧记录（幂等）
            DBUtils.execute("DELETE FROM score_history WHERE trade_date = ?",
                          params=[trade_date])
            rows = []
            for _, row in df.iterrows():
                rows.append((
                    trade_date,
                    str(row['ts_code']),
                    float(row.get('final_score', 0)),
                    float(row.get('final_score', 0)),  # 首日 raw=ema
                ))
            for r in rows:
                try:
                    DBUtils.execute(
                        "INSERT INTO score_history (trade_date,ts_code,raw_score,ema_score) "
                        "VALUES (?,?,?,?)",
                        r
                    )
                except Exception:
                    pass  # 忽略重复键等
            print(f"  [ScoreHistory] 已保存 {len(rows)} 条评分记录 ({trade_date})")
        except Exception as e:
            print(f"[ScoreHistory] 保存失败: {e}")

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(self, trade_date=None, top_k=20, news_boost_sectors=None, mode='dual',
            memory_facts=None):
        """执行混合策略选股

        Args:
            trade_date: 交易日期 (YYYY-MM-DD), None 则使用最新交易日
            top_k:      返回前 N 只股票
            news_boost_sectors: 新闻检测到的利好行业名称列表（加成事件评分）
            mode:       'dual'  = 行业轮动(A轨) + 价值(B轨) 各出一半后合并 [默认]
                        'tech'  = 仅行业轮动+AI+事件轨道
                        'value' = 仅价值质量轨道

        Returns:
            DataFrame with 'track' column ('sector_rotation'|'value'|'both')
        """
        print("\n" + "=" * 60)
        print("  HybridStrategy - 混合AI+事件驱动选股")
        print("=" * 60)

        # 0. 确定交易日期
        if trade_date is None:
            trade_date = self._get_latest_trade_date()
        if trade_date is None:
            print("[ERROR] 无可用交易数据")
            return self._empty_result()

        # 统一日期格式
        trade_date = pd.Timestamp(trade_date).strftime('%Y-%m-%d')

        # 0b. 市场状态检测 → 动态调整 top_k
        print(f"\n[Step 0] 市场状态检测...")

        # 0b-i. 极端行情检测：三分之二以上股票下跌 → 大幅缩减仓位
        extreme_mult = self._check_extreme_downside(trade_date)
        if extreme_mult == 0:
            print("[WARN] [EXTREME-DOWN] 全市场普跌，空仓避险")
            return self._empty_result()

        # 0b-ii. A股+美股市场制度检测
        regime, regime_mult = self._get_market_regime()

        if regime == 'dip_buy':
            print("[INFO] [DIP-BUY] 单日急跌，切换为价值抄底模式（仅B轨，少量仓位）")
            mode = 'value'

        # 两层缩减取更保守的
        effective_mult = min(regime_mult, extreme_mult)

        if effective_mult == 0:
            print("[WARN] 市场持续弱势，全空仓，不推股票")
            return self._empty_result()

        # 按市场状态调整每轨数量（弱市最少3只，保持基本分散）
        effective_track_topk = max(3, round(self.track_topk * effective_mult))
        effective_top_k = min(top_k, effective_track_topk * 2)
        print(f"  每轨选股: {self.track_topk} → {effective_track_topk} 只 "
              f"（市场 {regime}×{regime_mult}, 极端行情×{extreme_mult}, 综合×{effective_mult}）")

        print(f"\n[Step 1] 交易日期: {trade_date}")

        # 1. 加载股票池
        df = self._load_stock_universe(trade_date)
        if df.empty:
            print("[ERROR] 股票池为空")
            return self._empty_result()

        # 2. 加载 AI 评分
        print(f"\n[Step 2] 加载AI评分...")
        ai_df = self._load_ai_scores()

        # 合并 AI 评分到股票池 (左连接, 无AI评分的填0)
        if not ai_df.empty:
            df = df.merge(ai_df[['ts_code', 'ai_score']], on='ts_code', how='left')
        else:
            df['ai_score'] = 0.0
        df['ai_score'] = df['ai_score'].fillna(0.0)

        # 归一化 AI 评分
        df['ai_score'] = self._normalize_ai_scores(df['ai_score'])

        # 3. 计算事件评分
        print(f"\n[Step 3] 计算事件评分...")
        stock_list = df['ts_code'].tolist()
        event_scores = self.event_driver.get_event_scores(stock_list)

        # 3b. 新闻板块加成（如有）
        if news_boost_sectors:
            print(f"\n[Step 3b] 新闻感知加成 (利好板块: {news_boost_sectors[:5]})...")
            event_scores = self._apply_news_boost(df, event_scores, news_boost_sectors)

        # 合并事件评分
        df['event_score'] = df['ts_code'].map(event_scores).fillna(0.0)

        # 4. 应用过滤器（A轨：含大盘市值上限；B轨：保留全市值，仅去ST+异常）
        print(f"\n[Step 4] 应用安全过滤器...")
        df_b_base = df.copy()   # B轨原始副本，稍后单独过滤（不受大盘上限约束）
        df = self._apply_filters(df, event_scores)
        if df.empty:
            print("[WARN] 过滤后无剩余股票")
            return self._empty_result()

        # 4b. 超短线投机股过滤（日均振幅过大，不适合持有1周）
        print(f"\n[Step 4b] 过滤超短线投机股...")
        df = self._filter_short_term_stocks(df, trade_date)
        if df.empty:
            print("[WARN] 超短线过滤后无剩余股票")
            return self._empty_result()

        # 重置索引 (过滤后索引可能不连续)
        df = df.reset_index(drop=True)

        # B轨基础过滤：去ST/退市/市值<=0，并复用超短线过滤（防高振幅投机股进B轨）
        df_b_base = df_b_base[df_b_base['total_mv'] > 0]
        df_b_base = df_b_base[~df_b_base['name'].str.contains('ST|退', na=False)]
        df_b_base['event_score'] = df_b_base['ts_code'].map(event_scores).fillna(0.0)
        df_b_base = self._filter_short_term_stocks(df_b_base, trade_date)
        df_b_base = df_b_base.reset_index(drop=True)

        # 5. 计算基本面评分
        print(f"\n[Step 5] 计算基本面评分...")
        df['fundamental_score'] = self._calculate_fundamental_score(df)

        # 6. 行业动量评分（tech/dual 模式：同时作为过滤器）
        print(f"\n[Step 6] 计算行业动量评分...")
        try:
            sector_mom_result = self._calculate_sector_momentum(df, trade_date)
            if isinstance(sector_mom_result, pd.Series):
                df['sector_momentum_score'] = sector_mom_result.values
            elif isinstance(sector_mom_result, np.ndarray):
                df['sector_momentum_score'] = sector_mom_result
            else:
                df['sector_momentum_score'] = 0.0
        except Exception as e:
            print(f"  [SectorMom] 计算失败: {e}")
            df['sector_momentum_score'] = 0.0

        # 6b. AI赛道 Layer 热度评分（替代市值偏好）
        print(f"\n[Step 6b] 计算AI赛道Layer热度评分...")
        try:
            layer_heat_result = self._calculate_layer_heat_score(df, trade_date)
            if isinstance(layer_heat_result, np.ndarray):
                df['layer_heat_score'] = layer_heat_result
            elif isinstance(layer_heat_result, (list, tuple)):
                df['layer_heat_score'] = np.array(layer_heat_result)
            else:
                df['layer_heat_score'] = 0.5
        except Exception as e:
            print(f"  [LayerHeat] 计算失败: {e}")
            df['layer_heat_score'] = 0.5

        # 6c. A轨基本面硬过滤：基本面极差的股票直接淘汰，不给低分慢慢排出去
        print(f"\n[Step 6c] A轨基本面硬过滤...")
        before_fund = len(df)
        # fundamental_score < 0.25 = ROE/PE极差（亏损边缘或严重高估），直接踢出A轨候选
        # 保留：fundamental_score 缺失（部分港股无PE数据），或评分 >= 0.25
        fund_ok = df['fundamental_score'].isna() | (df['fundamental_score'] >= 0.25)
        df = df[fund_ok].reset_index(drop=True)
        removed_fund = before_fund - len(df)
        if removed_fund > 0:
            print(f"  [FundFilter] 剔除基本面极差股票(fundamental_score<0.25): {removed_fund} 只")
        else:
            print(f"  [FundFilter] 无需剔除，全部 {len(df)} 只通过基本面门槛")

        # 6d. 事件评分相对化：在候选池内做 rank 归一化，消除全员满分问题
        # 原始 event_score 是绝对概念匹配度，池内 AI 股几乎全部命中 → 区分度低
        # 改为：同批候选中按 event_score 排名归一化，让相对热度决定权重
        if df['event_score'].nunique() > 1:
            ev = df['event_score']
            ev_min, ev_max = ev.min(), ev.max()
            df['event_score'] = (ev - ev_min) / (ev_max - ev_min)
            print(f"  [EventNorm] 事件评分相对化: 原始范围 [{ev_min:.3f},{ev_max:.3f}] → [0,1]")
        else:
            print(f"  [EventNorm] 事件评分无差异（全员相同），保持原值")

        # 7. 技术面综合评分（轨道A）— 自适应权重
        print(f"\n[Step 7] 计算技术面综合评分（轨道A，自适应权重）...")

        # 根据市场状态动态调整评分权重
        if regime == 'strong':
            w_ai = 0.35
            w_event = 0.15
            w_fund = 0.05
            w_mom = 0.30
            w_layer = 0.15
            print(f"  [RegimeWeight] 强市模式: AI={w_ai} Event={w_event} "
                  f"Fund={w_fund} Mom={w_mom} Layer={w_layer}")
        elif regime in ('weak', 'crash'):
            w_ai = 0.30
            w_event = 0.20
            w_fund = 0.20
            w_mom = 0.15
            w_layer = 0.15
            print(f"  [RegimeWeight] 弱市模式: AI={w_ai} Event={w_event} "
                  f"Fund={w_fund} Mom={w_mom} Layer={w_layer}")
        else:
            w_ai = self.W_AI
            w_event = self.W_EVENT
            w_fund = self.W_FUNDAMENTAL
            w_mom = self.W_SECTOR_MOM
            w_layer = self.W_LAYER_HEAT

        df['final_score'] = (
            w_ai          * df['ai_score'] +
            w_event       * df['event_score'] +
            w_fund        * df['fundamental_score'].fillna(0.5) +
            w_mom         * df['sector_momentum_score'] +
            w_layer       * df['layer_heat_score']
        )

        # 7b. 持仓延续奖励：上期已推荐的股票加成，减少无谓换手
        if self.persistence_bonus > 0:
            prev_picks = self._get_previous_picks()
            if prev_picks:
                mask = df['ts_code'].isin(prev_picks)
                df.loc[mask, 'final_score'] *= (1 + self.persistence_bonus)
                print(f"  [Persistence] 上期推荐 {len(prev_picks)} 只，"
                      f"本期重叠 {mask.sum()} 只，给予 +{int(self.persistence_bonus*100)}% 加成")

        # 7c. 评分 EMA 平滑：融合历史评分，减少单日波动导致的排名剧变
        history = self._load_score_history(days=10)
        if history:
            df['final_score'] = self._apply_score_ema(df, history, score_col='final_score')

        # 7d. profit_warnings 降权：QMT 预警股票 final_score 打折
        df = self._apply_profit_warning_penalty(df)

        # 7e. 外部信号加成：期货板块信号 + 龙虎榜机构信号 + 北向情绪
        df = self._apply_external_signals(df, trade_date)

        # 7f. 记忆事实加成（MemoryService 高置信度事实）
        if memory_facts:
            df = self._apply_memory_facts_bonus(df, memory_facts)

        # 8. 获取概念标签
        print(f"\n[Step 8] 获取概念标签...")
        concepts_series = self.event_driver.get_concepts_for_stocks(
            df['ts_code'].tolist()
        )
        df['concepts'] = df['ts_code'].map(concepts_series).fillna('')

        # 9. 双轨选股
        print(f"\n[Step 9] 双轨选股 (mode={mode})...")

        if mode == 'tech':
            # ── 单轨：行业轮动+技术面 ──
            sr_df = self._apply_sector_rotation_filter(df, trade_date)
            sr_df = sr_df.sort_values('final_score', ascending=False)
            sr_df['track'] = 'sector_rotation'
            result = sr_df.head(effective_top_k).copy()

        elif mode == 'value':
            # ── 单轨：纯价值 ──
            val_df = self._apply_value_filter(df.copy())
            if not val_df.empty:
                val_df['value_score'] = self._calculate_value_score(val_df)
                val_df = val_df.sort_values('value_score', ascending=False)
                val_df['final_score'] = val_df['value_score']
            val_df['track'] = 'value'
            result = val_df.head(effective_top_k).copy()

        else:
            # ── 双轨：AI行业轮动(A轨) + 红利可持续(B轨) ──
            prev_picks = self._get_previous_picks()

            # ── A 轨：AI/成长股 → 行业轮动 + 技术面评分 ──
            # 优先用 company_type 限定 AI/成长/政策驱动类型
            _ai_types = {'growth', 'policy'}
            if 'company_type' in df.columns:
                df_a = df[df['company_type'].isin(_ai_types)].copy()
                if len(df_a) < 5:   # 池内AI股太少时回退全量
                    df_a = df.copy()
                print(f"  [A轨] AI/成长股候选: {len(df_a)} 只")
            else:
                df_a = df.copy()

            sr_df = self._apply_sector_rotation_filter(df_a, trade_date, prev_picks=prev_picks)
            sr_df = sr_df.sort_values('final_score', ascending=False)
            sr_df['track'] = 'sector_rotation'
            track_a = self._apply_turnover_limit(
                sr_df, prev_picks, effective_track_topk, label='A轨'
            )

            # ── B 轨：红利/现金流股 → 红利可持续性评分 ──
            # 从 df_b_base（无大盘市值上限）中取红利类型股票
            _div_types = {'cashflow', 'rate_sensitive', 'resource', 'brand'}
            if 'company_type' in df_b_base.columns:
                df_b = df_b_base[df_b_base['company_type'].isin(_div_types)].copy()
                if len(df_b) < 5:   # 池内红利股太少时回退全量
                    df_b = df_b_base.copy()
                print(f"  [B轨] 红利/现金流股候选: {len(df_b)} 只")
            else:
                df_b = df_b_base.copy()

            # B轨不走行业轮动过滤和反追高（红利股本来就不追热点）
            div_df = self._apply_dividend_filter(df_b)

            # B轨趋势门槛：剔除近20日跌幅过大的股票（红利股也不在下跌趋势中买入）
            # mom_20 < -15% = 月线级别的大幅下跌，价值陷阱信号
            if not div_df.empty:
                try:
                    sql_mom = """
                    SELECT ts_code, mom_20
                    FROM stock_factors
                    WHERE trade_date = (
                        SELECT MAX(trade_date) FROM stock_factors WHERE trade_date <= ?
                    ) AND mom_20 IS NOT NULL
                    """
                    mom_df = DBUtils.query_df(sql_mom, params=[trade_date])
                    if not mom_df.empty:
                        before_trend = len(div_df)
                        div_df = div_df.merge(mom_df[['ts_code', 'mom_20']], on='ts_code', how='left')
                        # mom_20 缺失（新股）时保留；下跌超 -15% 剔除
                        div_df = div_df[div_df['mom_20'].isna() | (div_df['mom_20'] >= -15)]
                        removed_trend = before_trend - len(div_df)
                        if removed_trend > 0:
                            print(f"  [B轨TrendFilter] 剔除近20日跌幅>15%的趋势下行股 {removed_trend} 只")
                        div_df = div_df.drop(columns=['mom_20'], errors='ignore').reset_index(drop=True)
                except Exception as e:
                    print(f"  [B轨TrendFilter] 跳过: {e}")

            if not div_df.empty:
                div_df['event_score'] = div_df['ts_code'].map(event_scores).fillna(0.0)
                # 外部信号对 B轨的 event_score 加成（与 A轨一致）
                div_df = self._apply_external_signals(div_df, trade_date)
                div_df['div_score'] = self._calculate_dividend_sustainability_score(div_df)
                div_df['final_score'] = 0.90 * div_df['div_score'] + 0.10 * div_df['event_score']
                div_df = div_df.sort_values('final_score', ascending=False)
                if 'fundamental_score' not in div_df.columns:
                    div_df['fundamental_score'] = 0.5
                if 'sector_momentum_score' not in div_df.columns:
                    div_df['sector_momentum_score'] = 0.0
                if 'layer_heat_score' not in div_df.columns:
                    div_df['layer_heat_score'] = 0.0
            div_df['track'] = 'dividend'
            a_codes = set(track_a['ts_code'])
            div_df = div_df[~div_df['ts_code'].isin(a_codes)]
            track_b = self._apply_turnover_limit(
                div_df, prev_picks - a_codes, effective_track_topk, label='B轨'
            )

            result = pd.concat([track_a, track_b], ignore_index=True)
            print(f"  轨道A(AI行业轮动): {len(track_a)} 只, "
                  f"轨道B(红利可持续): {len(track_b)} 只, "
                  f"合计: {len(result)} 只")

        # 输出列整理
        output_cols = [
            'ts_code', 'name', 'close', 'pe_ttm', 'total_mv', 'track',
            'ai_score', 'event_score', 'fundamental_score',
            'sector_momentum_score', 'layer_heat_score',
            'final_score', 'concepts'
        ]
        output_cols = [c for c in output_cols if c in result.columns]
        result = result[output_cols].reset_index(drop=True)

        # 打印摘要
        print(f"\n{'=' * 60}")
        print(f"  选股结果: {len(result)} 只  (模式: {mode})")
        print(f"{'=' * 60}")
        for i, (_, row) in enumerate(result.iterrows(), 1):
            mv_yi = row.get('total_mv', 0) / 10000 if row.get('total_mv', 0) > 0 else 0
            track_tag = f"[{row.get('track','')[:3]}]"
            concept_tag = f" {str(row['concepts'])[:15]}" if row.get('concepts') and str(row.get('concepts', '')) not in ('', 'nan') else ""
            print(
                f"  #{i:2d} {track_tag} {row['ts_code']} {row.get('name', '')[:6]:>6s} "
                f"Score={row['final_score']:.3f} "
                f"AI={row.get('ai_score',0):.2f} Evt={row.get('event_score',0):.2f}"
                f"{concept_tag}"
            )

        # 保存今日评分到历史（供明日 EMA 平滑使用）
        self._save_scores_to_history(result, trade_date)

        return result

    def _apply_turnover_limit(self, sorted_df, prev_picks, n, label=''):
        """换手限制：从 sorted_df 取 n 只，但每次最多替换 max_turnover_pct 比例的持仓。

        逻辑：
          1. 先保留上期已有且仍在当前排名前 n×(1+buffer) 的股票（"保留席位"）
          2. 用剩余席位补入新股，新股数不超过 max_turnover_pct×n
          3. 凑不满则直接用排名靠前的补全
          4. 新增：老股评分下降不超过15%时强制保留（防一日游）
        """
        if sorted_df.empty:
            return sorted_df.head(0)

        max_new = max(1, round(n * self.max_turnover_pct))   # 最多换入几只新股
        max_keep = n - max_new                                 # 至少保留几只老股

        # 候选池：前 n + 缓冲区（避免老股刚好在 n+1 位）
        buffer = max(n, 10)
        pool = sorted_df.head(n + buffer).reset_index(drop=True)

        # 分开老股（上期已在）和新股
        old_mask = pool['ts_code'].isin(prev_picks)
        old_df   = pool[old_mask]
        new_df   = pool[~old_mask]

        # 保留老股（按当前评分排，至少 max_keep 只）
        keep = old_df.head(max_keep)

        # 新股补足至 n
        slots = n - len(keep)
        fill  = new_df.head(slots)
        result = pd.concat([keep, fill], ignore_index=True)

        # 若还不够（老股太少），用排名前的补全
        if len(result) < n:
            already = set(result['ts_code'])
            extra = pool[~pool['ts_code'].isin(already)].head(n - len(result))
            result = pd.concat([result, extra], ignore_index=True)

        new_cnt  = len(result[~result['ts_code'].isin(prev_picks)])
        keep_cnt = len(result) - new_cnt
        print(f"  [Turnover {label}] 保留 {keep_cnt} 只 + 换入 {new_cnt} 只 "
              f"(上限 {max_new} 只/次，共 {len(result)} 只)")
        return result.head(n)

    @staticmethod
    def _empty_result():
        """返回空结果 DataFrame"""
        return pd.DataFrame(columns=[
            'ts_code', 'name', 'close', 'pe_ttm', 'total_mv',
            'ai_score', 'event_score', 'fundamental_score',
            'sector_momentum_score', 'layer_heat_score', 'final_score', 'concepts'
        ])


# ======================================================================
# 命令行入口
# ======================================================================
if __name__ == '__main__':
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

    strategy = HybridStrategy()
    result = strategy.run(top_k=20)

    if result is not None and not result.empty:
        print(f"\n[DONE] 成功选出 {len(result)} 只股票")
    else:
        print("\n[DONE] 未能选出股票, 请检查数据是否已同步")
