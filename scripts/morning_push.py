"""
早盘推送 - 8:30执行
推送今日选股结果和操作指引
"""
import sys
import os
import io
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 初始化日志
from src.utils.log_utils import init_logger
logger = init_logger("morning_push")

from src.utils.config_loader import Config
from src.utils.notifier import NotifierFactory
from src.portfolio.position_manager import PositionManager
from src.utils.db_utils import DBUtils
import pandas as pd
import subprocess


def add_ai_analysis_to_content(content):
    """调用AI分析选股结果并添加到推送内容（使用缓存）"""
    import os
    import glob
    import json
    
    print("[AI分析] 开始获取选股结果...")
    
    # 使用缓存文件获取选股结果
    output_dir = os.path.join(os.path.dirname(__file__), '..', 'output')
    cache_files = glob.glob(os.path.join(output_dir, 'multi_strategy_*.json'))
    
    if not cache_files:
        print("[AI分析] 无缓存文件")
        return None
    
    try:
        best_file = sorted(cache_files)[-1]
        with open(best_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if not data.get('picks'):
            print("[AI分析] 无选股结果")
            return None
        
        picks = data['picks'][:5]
        stock_codes = [p['ts_code'] for p in picks]
        print(f"[AI分析] 分析股票: {stock_codes}")
    
    except Exception as e:
        print(f"[AI分析] 读取缓存失败: {e}")
        return None
    
    if not stock_codes:
        print("[AI分析] 无股票代码")
        return None
    
    # 并行调用AI分析
    def analyze_remote(ts_code):
        try:
            cmd = f"cd /home/li/ai_fund/ai-hedge-fund && /home/li/robottrade/venv/bin/python run_cn.py {ts_code}"
            env = os.environ.copy()
            env['DEEPSEEK_API_KEY'] = 'sk-e4cd8339e40c42cb9275d6a16e0f56a1'
            result = subprocess.run(
                ['ssh', 'li@192.168.3.22', cmd],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=180,
                env=env
            )
            if result.returncode == 0:
                return {'ts_code': ts_code, 'success': True, 'output': result.stdout}
            else:
                return {'ts_code': ts_code, 'success': False, 'error': result.stderr}
        except Exception as e:
            return {'ts_code': ts_code, 'success': False, 'error': str(e)}
    
    print("[AI分析] 调用远程AI分析...")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(analyze_remote, code): code for code in stock_codes}
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            status = 'OK' if r['success'] else 'FAIL'
            print(f"  [{status}] {r['ts_code']}")
    
    # 解析结果
    buy_signals = []
    sell_signals = []
    hold_signals = []
    
    for r in results:
        if not r['success']:
            continue
        output = r['output']
        bullish = output.count('bullish')
        bearish = output.count('bearish')
        
        if bullish > bearish:
            buy_signals.append(r['ts_code'])
        elif bearish > bullish:
            sell_signals.append(r['ts_code'])
        else:
            hold_signals.append(r['ts_code'])
    
    # 生成AI分析报告
    if not results:
        return None
    
    ai_section = "\n---\n### 🤖 AI大师分析\n"
    if buy_signals:
        ai_section += f"**买入信号**: {', '.join(buy_signals)}\n"
    if sell_signals:
        ai_section += f"**卖出信号**: {', '.join(sell_signals)}\n"
    if hold_signals:
        ai_section += f"**持有观察**: {', '.join(hold_signals)}\n"
    ai_section += "\n_AI量化对冲基金分析_"
    
    return content + ai_section


def _auto_deep_analyze(signals_df: pd.DataFrame, max_stocks: int = 3) -> list:
    """
    对买入信号股票触发自动深度分析。
    规则：
      1. 优先 watch 层（已重点关注）
      2. 跳过最近3天内分析过的股票（避免重复）
      3. 最多分析 max_stocks 只（控制 LLM 调用量）
    """
    from datetime import date, timedelta
    from src.analysis.stock_analyzer import StockAnalyzer

    threshold = (date.today() - pd.Timedelta(days=3)).strftime("%Y-%m-%d")

    # 查近3天已分析的股票
    recent_analyzed = DBUtils.query_df(
        f"SELECT DISTINCT ts_code FROM research_log WHERE log_date >= '{threshold}'"
    )
    analyzed_codes = set(recent_analyzed["ts_code"].tolist()) if not recent_analyzed.empty else set()

    # 排序：watch > reserve，过滤掉近期已分析
    df = signals_df.copy()
    df["tier_order"] = df["tier"].map({"watch": 0, "reserve": 1}).fillna(2)
    df = df[~df["ts_code"].isin(analyzed_codes)]
    df = df.sort_values("tier_order").head(max_stocks)

    if df.empty:
        print("[DeepAnalyze] 今日信号股票均已近期分析过，跳过")
        return []

    analyzer = StockAnalyzer()
    reports = []
    for _, row in df.iterrows():
        try:
            result = analyzer.analyze(row["ts_code"], trigger="buy_signal")
            reports.append(result)
            print(f"[DeepAnalyze] {row['ts_code']} {row.get('company_name','')} -> {result['action']}")
        except Exception as e:
            print(f"[DeepAnalyze] {row['ts_code']} 分析失败: {e}")
    return reports


def get_morning_stock_picks(top_k=20):
    """获取今日选股结果（使用动态热点识别 + 宏观新闻感知）

    Returns:
        pd.DataFrame: 选股结果
    """
    print("[早盘推送] 运行混合策略选股...")

    # Step 1: 宏观新闻分析（识别利好板块）
    news_analysis = None
    news_boost_sectors = []
    try:
        from src.risk.market_news_analyzer import MarketNewsAnalyzer
        analyzer = MarketNewsAnalyzer()
        news_analysis = analyzer.analyze()
        if news_analysis and news_analysis.get('sector_impacts'):
            for impact in news_analysis['sector_impacts']:
                if impact.get('direction') == '利好' and impact.get('strength') in ('强', '中'):
                    sector = impact.get('sector', '')
                    if sector:
                        news_boost_sectors.append(sector)
            if news_boost_sectors:
                print(f"[OK] 新闻利好板块: {news_boost_sectors[:5]}")
    except Exception as e:
        print(f"[WARN] 宏观新闻分析跳过: {e}")

    # Step 2: 动态热点识别（市场动量 + 新闻）
    dynamic_hot_topics = []
    try:
        from src.analysis.hot_topic_detector import HotTopicDetector
        detector = HotTopicDetector()
        dynamic_hot_topics, _ = detector.detect(top_k=20, news_analysis=news_analysis)
        if dynamic_hot_topics:
            print(f"[OK] 动态热点: {dynamic_hot_topics[:5]}...")
    except Exception as e:
        print(f"[WARN] 热点识别跳过: {e}")

    strategy = HybridStrategy(
        hot_topics=dynamic_hot_topics if dynamic_hot_topics else None
    )
    result_df = strategy.run(
        top_k=top_k, mode='dual',
        news_boost_sectors=news_boost_sectors if news_boost_sectors else None
    )
    
    if result_df is not None and len(result_df) > 0:
        print(f"[OK] 选出 {len(result_df)} 只股票")
        return result_df
    else:
        print("[WARN] 未获取到选股结果")
        return None


def get_all_strategy_picks(top_k=10):
    """获取所有策略选股结果（优先使用缓存）
    
    1. 优先读取 daily_alpha_run 预生成的缓存文件
    2. 无缓存时运行快速策略
    """
    import glob
    
    print("[DEBUG get_all_strategy_picks] 开始...")
    
    # Step 1: 检查是否有预生成的缓存文件（优先今天，其次最新）
    output_dir = os.path.join(os.path.dirname(__file__), '..', 'output')
    cache_files = glob.glob(os.path.join(output_dir, 'multi_strategy_*.json'))
    print(f"[DEBUG] 找到缓存文件: {len(cache_files)}个")
    
    # ===== 直接返回缓存，不执行任何策略 =====
    if cache_files:
        try:
            import json
            best_file = sorted(cache_files)[-1]
            print(f"[DEBUG] 读取缓存: {best_file}")
            with open(best_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get('success') and data.get('picks'):
                print(f"[策略中心] 使用缓存文件: {best_file}")
                import pandas as pd
                picks_df = pd.DataFrame(data['picks'])
                if 'strategy' not in picks_df.columns:
                    picks_df['strategy'] = data.get('strategies_run', ['hybrid'])[0]
                strategy_picks = {}
                for strat in picks_df['strategy'].unique():
                    strat_df = picks_df[picks_df['strategy'] == strat].copy()
                    strat_df = strat_df.sort_values('final_score', ascending=False).head(top_k)
                    strategy_picks[strat] = strat_df
                print(f"[OK] 从缓存读取 {len(picks_df)} 只，{len(strategy_picks)} 个策略")
                return strategy_picks
        except Exception as e:
            print(f"[WARN] 读取缓存失败: {e}")
        
        try:
            from src.utils.db_utils import DBUtils
            latest = DBUtils.query_df("SELECT MAX(trade_date) as d FROM daily_picks").iloc[0]['d']
            df = DBUtils.query_df("""
                SELECT ts_code, name, final_score, track
                FROM daily_picks
                WHERE trade_date = %s
                ORDER BY final_score DESC
                LIMIT 30
            """, (latest,))
            if not df.empty:
                print(f"[OK] 从daily_picks读取 {len(df)} 只候选股 (date={latest})")
                strategy_picks = {'hybrid': df}
                return strategy_picks
            else:
                print(f"[WARN] daily_picks为空")
        except Exception as e:
            print(f"[WARN] 读取daily_picks失败: {e}")
        
        print("[WARN] 无缓存，无策略执行")
        return {}
    
    try:
        from src.strategy.center import StrategyCenter, _STRATEGY_NAMES
        center = StrategyCenter(enable_macro=False, notify=False)
        result = center.run(strategies=core_strategies, top_k=top_k, ensemble=False)
        
        if result is None or result.empty:
            print("[WARN] 策略中心无选股结果")
            return {}
        
        if 'name' not in result.columns:
            result['name'] = result['ts_code']
        if 'final_score' not in result.columns:
            result['final_score'] = 0.5
        
        strategy_picks = {}
        for strat in result['strategy'].unique():
            strat_df = result[result['strategy'] == strat].copy()
            strat_df = strat_df.sort_values('final_score', ascending=False).head(top_k)
            strategy_picks[strat] = strat_df
            print(f"  {strat}: {len(strat_df)} 只")
        
        print(f"[OK] 策略中心选出 {len(result)} 只股票，{len(strategy_picks)} 个策略")
        return strategy_picks
    except Exception as e:
        import traceback
        print(f"[WARN] 策略中心运行失败: {e}")
        traceback.print_exc()
        return {}


def get_data_quality_check():
    """检查数据质量，返回检查结果"""
    print("[数据质量] 检查...")
    try:
        from src.utils.db_utils import DBUtils
        import clickhouse_connect
        
        ch_config = {'host': '192.168.3.51', 'port': 8123, 'username': 'default', 'password': 'clickhouse123'}
        
        # MySQL 最新日期
        mysql_date = DBUtils.query_df("""
            SELECT MAX(trade_date) as max_date FROM stock_daily
        """).iloc[0]['max_date']
        
        # ClickHouse 最新日期
        ch_client = clickhouse_connect.get_client(**ch_config)
        ch_result = ch_client.query("SELECT MAX(trade_date) as max_date FROM stock_daily")
        ch_date = ch_result.result_rows[0][0] if ch_result.result_rows else None
        ch_client.close()
        
        mysql_date_str = str(mysql_date) if mysql_date else 'N/A'
        
        # 判断是否有延迟
        ch_lag = (mysql_date - ch_date) if ch_date and mysql_date else None
        ch_lag_days = ch_lag.days if ch_lag else 0
        
        result = {
            'mysql_date': mysql_date_str,
            'clickhouse_date': str(ch_date) if ch_date else 'N/A',
            'clickhouse_lag_days': ch_lag_days,
            'status': 'OK' if ch_lag_days <= 1 else 'WARN'
        }
        
        print(f"  MySQL: {result['mysql_date']}")
        print(f"  ClickHouse: {result['clickhouse_date']} (lag: {ch_lag_days} days)")
        
        return result
    except Exception as e:
        print(f"[WARN] 数据质量检查失败: {e}")
        return {'status': 'ERROR', 'error': str(e)}


def get_market_overview():
    """获取市场概况
    
    Returns:
        dict: 市场统计信息，失败或空数据时返回 None
    """
    try:
        # 选最近一个数据量完整（>= 2000只）的交易日，避免当日同步残缺数据干扰
        complete_date_df = DBUtils.query_df("""
            SELECT trade_date, COUNT(*) as cnt
            FROM stock_daily
            GROUP BY trade_date
            HAVING COUNT(*) >= 2000
            ORDER BY trade_date DESC
            LIMIT 1
        """)
        if complete_date_df.empty:
            print("[WARN] stock_daily 无完整交易日数据（<2000只）")
            return None
        latest_date = str(complete_date_df.iloc[0]['trade_date']).strip()

        sql_prev = f"""
        SELECT DISTINCT trade_date
        FROM stock_daily
        WHERE trade_date < '{latest_date}'
        ORDER BY trade_date DESC
        LIMIT 1
        """
        prev_df = DBUtils.query_df(sql_prev)
        if prev_df.empty or pd.isna(prev_df.iloc[0]['trade_date']):
            print("[WARN] 无法获取前一交易日")
            return {
                'latest_date': latest_date,
                'prev_date': '-',
                'total_stocks': 0,
                'rise_count': 0,
                'fall_count': 0,
                'avg_change': 0
            }
        prev_date = str(prev_df.iloc[0]['trade_date']).strip()

        sql_stats = f"""
        SELECT
            COUNT(*) as total_stocks,
            SUM(CASE WHEN today.close > prev.close THEN 1 ELSE 0 END) as rise_count,
            SUM(CASE WHEN today.close < prev.close THEN 1 ELSE 0 END) as fall_count,
            AVG((today.close - prev.close) / prev.close * 100) as avg_change
        FROM stock_daily today
        LEFT JOIN stock_daily prev ON today.ts_code = prev.ts_code AND prev.trade_date = '{prev_date}'
        WHERE today.trade_date = '{latest_date}'
        """
        stats = DBUtils.query_df(sql_stats)
        if stats.empty:
            return None
        total = int(stats['total_stocks'][0]) if pd.notna(stats['total_stocks'][0]) else 0
        rise = int(stats['rise_count'][0]) if pd.notna(stats['rise_count'][0]) else 0
        fall = int(stats['fall_count'][0]) if pd.notna(stats['fall_count'][0]) else 0
        avg_chg = float(stats['avg_change'][0]) if pd.notna(stats['avg_change'][0]) else 0.0
        
        return {
            'latest_date': latest_date,
            'prev_date': prev_date,
            'total_stocks': total,
            'rise_count': rise,
            'fall_count': fall,
            'avg_change': avg_chg
        }
    except Exception as e:
        print(f"[WARN] 获取市场概况失败: {e}")
        return None


def _get_agent_plan_section(timeout: int = 120) -> str:
    """调用 DecisionEngine 生成今日执行计划并格式化为 markdown。
    超时或报错时返回空字符串，不阻塞主推送流程。
    """
    try:
        import concurrent.futures
        from src.agent.decision_engine import DecisionEngine
        from src.broker.sim_broker import SimBroker
        from src.utils.llm_router import LLMRouter

        def _run():
            from src.agent.review_agent import TradeMemory
            broker = SimBroker()
            llm_router = LLMRouter()
            memory = TradeMemory()
            engine = DecisionEngine(broker=broker, llm_router=llm_router, memory=memory)
            trade_date = datetime.now().strftime('%Y%m%d')
            return engine.run(trade_date=trade_date)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_run)
            try:
                plan = fut.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                print(f"[WARN] DecisionEngine 超时（>{timeout}s），跳过执行计划")
                return ""

        if not plan or 'trades' not in plan:
            return ""

        regime_icon = {'bull': '🐂', 'bear': '🐻', 'neutral': '➖'}.get(plan.get('market_regime', ''), '📊')
        regime_name = {'bull': '牛市', 'bear': '熊市', 'neutral': '震荡'}.get(plan.get('market_regime', ''), plan.get('market_regime', ''))
        confidence = plan.get('confidence', 0)
        reasoning = plan.get('reasoning', '')
        cash_reserve = plan.get('cash_reserve', 0)
        trades = plan.get('trades', [])

        lines = [
            "\n---\n### 🤖 今日执行计划 (AI Agent)\n",
            f"**市场研判**: {regime_icon} {regime_name}  |  **置信度**: {confidence:.0%}  |  **建议现金**: {cash_reserve:.0%}",
        ]
        if reasoning:
            lines.append(f"\n> {reasoning}\n")

        if trades:
            action_icon = {'buy': '🟢买入', 'sell': '🔴卖出', 'reduce': '🟡减仓', 'hold': '⚪持有'}
            lines.append("\n**操作指令**\n")
            for t in trades:
                if not isinstance(t, dict):
                    continue
                icon = action_icon.get(t.get('action', ''), t.get('action', ''))
                name = t.get('name', t.get('ts_code', ''))
                code = t.get('ts_code', '')
                weight = t.get('weight', 0)
                entry = t.get('entry_price', 0)
                stop = t.get('stop_loss_price', 0)
                reason = t.get('reason', '')
                entry_str = f"目标价 ≤{entry:.2f}" if entry and entry > 0 else "市价"
                stop_str = f"止损 {stop:.2f}" if stop and stop > 0 else ""
                lines.append(
                    f"- **{icon}** {name}({code})  仓位{weight:.0%}  {entry_str}"
                    + (f"  {stop_str}" if stop_str else "")
                    + (f"  _{reason}_" if reason else "")
                )
        else:
            lines.append("\n> 今日无操作指令，维持现有仓位。\n")

        lines.append("\n---\n")
        return "\n".join(lines)

    except Exception as e:
        print(f"[WARN] 获取执行计划失败: {e}")
        return ""


def build_morning_message(result_df, market_info, position_df=None, pm=None,
                          etf_df=None, pool_result=None,
                          phase1_candidates=None, agent_plan_section=None,
                          holding_status=None, strategy_picks=None, data_quality=None):
    """构建早盘推送消息

    信息层次：操作指令 → 持仓状态 → 市场脉搏 → 数据质量 → 本周选股 → ETF → 个股新闻

    Returns:
        tuple: (title, content)
    """
    now = datetime.now()
    today = now.strftime('%m月%d日')
    weekday_cn = {'Monday': '一', 'Tuesday': '二', 'Wednesday': '三',
                  'Thursday': '四', 'Friday': '五', 'Saturday': '六', 'Sunday': '日'}
    weekday = f"周{weekday_cn.get(now.strftime('%A'), '')}"
    title = f"📈 早盘推送 {today} {weekday}"

    sections = []

    # ── 1. AI 操作指令（最重要，置顶）────────────────────────────
    if agent_plan_section:
        sections.append(agent_plan_section.strip())
        sections.append("---")

    # ── 2. 持仓状态（一眼看清哪些能动、哪些不能动）──────────────
    if holding_status:
        pos_rows  = holding_status.get('position_rows', [])
        stop_risk = holding_status.get('stop_loss_risk', [])
        lines = ["### 🔒 持仓状态（A轨≥5天/B轨≥15天可操作）\n"]

        if pos_rows:
            B_TRACKS = {'dividend', 'value'}
            a_lock = [r for r in pos_rows if r.get('track', '') not in B_TRACKS and r.get('protected')]
            b_lock = [r for r in pos_rows if r.get('track', '') in B_TRACKS and r.get('protected')]
            a_free = [r for r in pos_rows if r.get('track', '') not in B_TRACKS and not r.get('protected')]
            b_free = [r for r in pos_rows if r.get('track', '') in B_TRACKS and not r.get('protected')]

            def _fmtpos(lst):
                return " ".join(
                    f"{r.get('name', r.get('ts_code',''))[:4]}({r.get('days_held','?')}天"
                    f"{r.get('pnl_pct', 0)*100:+.1f}%)" for r in lst[:5]
                )

            if a_lock: lines.append(f"⛔ A轨锁仓: {_fmtpos(a_lock)}")
            if b_lock: lines.append(f"⛔ B轨锁仓: {_fmtpos(b_lock)}")
            if a_free: lines.append(f"✅ A轨可操作: {_fmtpos(a_free)}")
            if b_free: lines.append(f"✅ B轨可操作: {_fmtpos(b_free)}")
            if not any([a_lock, b_lock, a_free, b_free]):
                lines.append("暂无持仓记录")
        else:
            p = holding_status.get('protected_count', 0)
            e = holding_status.get('eligible_count', 0)
            lines.append(f"锁仓 {p} 只 | 可操作 {e} 只")

        if stop_risk:
            names = " ".join(
                f"{s.get('name', '?')[:4]}({s.get('pnl_pct', 0)*100:+.1f}%)" for s in stop_risk[:4]
            )
            lines.append(f"⚠️ 接近止损(-8%): {names}")

        sections.append("\n".join(lines))
        sections.append("---")

    # ── 3. 市场脉搏（2行，够了）──────────────────────────────────
    if market_info and market_info.get('total_stocks', 0) > 0:
        total = market_info['total_stocks']
        rise_pct = market_info['rise_count'] / total * 100 if total else 0
        chg = market_info['avg_change']
        sentiment = "🟢 强势" if chg > 1 else ("🔴 偏弱" if chg < -1 else "🟡 震荡")
        sections.append(
            f"### 🌍 市场脉搏 ({market_info['latest_date']})\n\n"
            f"{sentiment} | 上涨 {market_info['rise_count']}只({rise_pct:.0f}%) "
            f"平均 {chg:+.2f}%"
        )
        sections.append("---")

    # ── 3b. 数据质量检查 ───────────────────────────────────────────
    if data_quality and data_quality.get('status'):
        dq = data_quality
        lag = dq.get('clickhouse_lag_days', 0)
        icon = "✅" if dq.get('status') == 'OK' else "⚠️"
        lines = [f"### 📊 数据质量 {icon}\n"]
        lines.append(f"- MySQL: {dq.get('mysql_date', 'N/A')}")
        lines.append(f"- ClickHouse: {dq.get('clickhouse_date', 'N/A')} (延迟{lag}天)")
        if lag > 1:
            lines.append("⚠️ 数据延迟警告!")
        sections.append("\n".join(lines))
        sections.append("---")

    # ── 4. 股票池买入信号（核心：今日到底有什么信号）────────────
    if pool_result is not None:
        try:
            from src.strategy.pool_strategy import PoolStrategy
            pool_md = PoolStrategy().format_dingtalk(pool_result)
            if pool_md.strip():
                sections.append(pool_md.strip())
                sections.append("---")
        except Exception:
            pass

    # ── 5. 本周双轨选股（简洁表格，无冗余操作建议）──────────────
    if result_df is not None and len(result_df) > 0:
        df_display = result_df.copy()
        if position_df is not None and 'position_pct' in position_df.columns:
            if 'track' not in position_df.columns and 'track' in result_df.columns:
                df_display = position_df.merge(
                    result_df[['ts_code', 'track', 'concepts']], on='ts_code', how='left'
                )
            else:
                df_display = position_df.copy()

        half_yi = (pm.total_capital * pm.max_total_position / 2 / 10000) if pm else 0

        lines = [f"### 🎯 双轨选股\n"]
        for track_label, track_icon, track_letter, track_name in [
            ('sector_rotation', '🔄', 'A', 'AI赛道·行业动量'),
            ('dividend',        '💎', 'B', '红利·价值底仓'),
        ]:
            hint = f"≈{half_yi:.0f}万" if half_yi > 0 else ""
            lines.append(f"**{track_icon} {track_letter}轨·{track_name}** {hint}")

            sub = df_display[df_display['track'] == track_label].reset_index(drop=True) \
                if 'track' in df_display.columns else df_display

            if sub.empty:
                lines.append("暂无\n")
                continue

            for i, row in sub.iterrows():
                name    = (row.get('name') or '')[:5]
                code    = (row.get('ts_code') or '')[:9]
                score   = row.get('final_score', 0)
                concepts = str(row.get('concepts') or '')[:10]
                stop    = row.get('stop_loss_price', 0)
                pos_pct = row.get('position_pct', 0)

                parts = [f"{i+1}. **{name}**({code}) {score:.2f}分"]
                if pos_pct > 0:  parts.append(f"{pos_pct*100:.0f}%仓")
                if stop > 0:     parts.append(f"止损{stop:.2f}")
                if concepts:     parts.append(f"[{concepts}]")
                lines.append("  " + " | ".join(parts))
            lines.append("")

        sections.append("\n".join(lines))
        sections.append("---")

    # ── 5b. 全策略选股结果 ───────────────────────────────────────
    if strategy_picks and len(strategy_picks) > 0:
        from src.strategy.center import _STRATEGY_NAMES
        lines = ["### 📊 全策略选股\n"]
        
        # 定义要展示的策略（按优先级排序，使用注册的实际名称）
        priority_strategies = ['hybrid', 'value', 'dividend', 'quant', 
                              'small_cap', 'cyclical', 'pb_roa', 'index_enhance']
        
        shown = 0
        for strat_name in priority_strategies:
            if strat_name not in strategy_picks:
                continue
            
            df = strategy_picks[strat_name]
            if df is None or df.empty:
                continue
            
            # 获取策略中文名
            strat_label = _STRATEGY_NAMES.get(strat_name, strat_name)
            lines.append(f"**◆ {strat_label}**")
            
            for i, row in df.head(5).iterrows():
                name = str(row.get('name', row.get('ts_code', '')))[:6]
                code = str(row.get('ts_code', ''))[:9]
                score = row.get('final_score', 0)
                concepts = str(row.get('concepts') or '')[:12]
                
                parts = [f"{i+1}. **{name}**({code}) {score:.2f}分"]
                if concepts:
                    parts.append(f"[{concepts}]")
                lines.append("  " + " | ".join(parts))
            
            lines.append("")
            shown += 1
            if shown >= 4:  # 最多显示4个策略
                break
        
        if shown > 0:
            sections.append("\n".join(lines))
            
            # 添加策略说明
            strategy_desc = """
> **策略说明**:
> - **混合策略**: AI预测(50%) + 事件驱动(30%) + 基本面(20%)
> - **价值策略**: 高ROE + 低PE + 稳定盈利
> - **红利策略**: 高股息 + 稳定分红
> - **小市值**: 优质小盘股成长潜力
> - **行业轮动**: 跟随市场热点板块
"""
            sections.append(strategy_desc)
            sections.append("---")

    # ── 6. ETF 机会（紧凑版，最多3只）──────────────────────────
    if etf_df is not None and not etf_df.empty:
        lines = ["### 📉 ETF 超卖雷达\n"]
        for i, r in etf_df.head(3).iterrows():
            icon = r.get("type_icon", "📊")
            lines.append(
                f"{i+1}. **{icon}{r['name']}**({r['code']}) "
                f"回调{r['drawdown']:.0f}% RSI{r['rsi']:.0f} "
                f"5日{r['ret_5d']:+.1f}%"
            )
        sections.append("\n".join(lines))
        sections.append("---")

    # ── 7. 个股新闻/研报/公告───────────────────────────────
    pool_signals = pool_result.get('signals') if pool_result else None
    if pool_result and pool_signals is not None and not pool_signals.empty:
        try:
            from src.feeds.stock_news_fetcher import fetch_stock_news_batch
            signal_list = pool_signals.to_dict('records') if hasattr(pool_signals, 'to_dict') else pool_signals
            signal_codes = [{'ts_code': s['ts_code'], 'name': s.get('name', s['ts_code'])} for s in signal_list[:10]]
            news_map = fetch_stock_news_batch(signal_codes, max_news_per_stock=3, sleep_sec=0.3)
            if news_map:
                parts = ["### 📰 个股最新资讯\n"]
                for s in signal_codes:
                    ts_code = s['ts_code']
                    name = s['name'][:6]
                    news_list = news_map.get(ts_code, [])
                    if news_list:
                        code = ts_code.split('.')[0]
                        parts.append(f"**{name}({code}):**")
                        for n in news_list[:2]:
                            pub_time = n.get('time', '')[:10]
                            parts.append(f"  • {pub_time} {n.get('title', '')[:35]}")
                sections.append("\n".join(parts))
                sections.append("---")
        except Exception as e:
            print(f"[WARN] 获取个股新闻失败: {e}")

    # ── 尾部 ─────────────────────────────────────────────────────
    sections.append("_16:00 收盘复盘推送 | 仅供参考，不构成投资建议_")

    content = "\n\n".join(s for s in sections if s.strip())
    return title, content


def send_morning_push():
    """发送早盘推送（增强版：含仓位管理）"""
    print("=" * 60)
    print("  早盘推送 - 8:30 (含仓位管理)")
    print("=" * 60)
    
    try:
        # 0. 交易日检查（改为警告，不阻断）
        from src.utils.trade_calendar import is_trade_day
        is_trading = is_trade_day()
        if not is_trading:
            print("[WARN] 今日可能非交易日，但继续尝试推送（如有选股结果）")
        else:
            print("[INFO] 今日为交易日")
        
        # 1. V2：只推池内股票，全市场扫描由每周刷新负责
        result_df = None
        position_df = None

        # 2. 初始化仓位管理器
        print("[早盘推送] 初始化仓位管理器...")
        pm = PositionManager()

        # 4. 获取市场概况
        print("[早盘推送] 获取市场概况...")
        market_info = get_market_overview()
        if market_info is None:
            market_info = {'latest_date': '-', 'prev_date': '-', 'total_stocks': 0, 'rise_count': 0, 'fall_count': 0, 'avg_change': 0}

        # 4b. 数据质量检查
        data_quality = {}
        print("[早盘推送] 检查数据质量...")
        try:
            data_quality = get_data_quality_check()
            print(f"[OK] 数据质量: {data_quality.get('status', 'N/A')}")
        except Exception as e:
            print(f"[WARN] 数据质量检查跳过: {e}")

        # 4b. 全策略选股结果（添加超时保护）
        strategy_picks = {}
        print("[早盘推送] 获取全策略选股...")
        try:
            import concurrent.futures
            
            print("[DEBUG] 开始获取策略...")
            
            # 使用线程池超时控制
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(get_all_strategy_picks, 10)
                try:
                    strategy_picks = future.result(timeout=30)  # 30秒超时
                    if strategy_picks:
                        total_picks = sum(len(v) for v in strategy_picks.values())
                        print(f"[OK] 全策略选股: {total_picks} 只，{len(strategy_picks)} 个策略")
                except concurrent.futures.TimeoutError:
                    print("[WARN] 全策略选股超时(30秒)，跳过")
                    strategy_picks = {}
                    
        except Exception as e:
            print(f"[WARN] 全策略选股跳过: {e}")

        # 5. ETF 抄底选择（已禁用）
        etf_df = None
        # print("[早盘推送] 获取ETF抄底选择...")
        # try:
        #     import concurrent.futures
        #     from src.strategy.etf_bottom_fish_strategy import ETFBottomFishStrategy
        #     with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
        #         _fut = _ex.submit(ETFBottomFishStrategy().run, top_n=5, sleep_sec=0.05, max_etf=300)
        #         try:
        #             etf_df = _fut.result(timeout=30)
        #         except concurrent.futures.TimeoutError:
        #             print("[WARN] ETF抄底策略超时（>90s），跳过")
        # except Exception as e:
        #     print(f"[WARN] ETF抄底策略跳过: {e}")

        # 5b. 股票池买入信号扫描（复用 daily_alpha_run 已更新的估值，不重跑全量）
        pool_result = None
        print("[早盘推送] 扫描股票池买入信号...")
        try:
            from src.strategy.pool_strategy import PoolStrategy
            pool_result = PoolStrategy().run(update_valuation=False)
            print(f"[OK] 股票池扫描: {pool_result['summary']}")
        except Exception as e:
            print(f"[WARN] 股票池扫描跳过: {e}")

        # 5b2. HoldingManager 持仓状态
        holding_status = None
        print("[早盘推送] 获取持仓稳定性状态...")
        try:
            from src.portfolio.holding_manager import HoldingManager
            hm = HoldingManager()
            status_df = hm.get_position_status()
            if status_df is not None and not status_df.empty:
                protected_count = int(status_df['protected'].sum()) if 'protected' in status_df.columns else 0
                eligible_count  = int((~status_df['protected']).sum()) if 'protected' in status_df.columns else 0
                stop_risk_df    = status_df[status_df['pnl_pct'] <= -0.06] if 'pnl_pct' in status_df.columns else pd.DataFrame()
                stop_loss_risk  = stop_risk_df.to_dict('records') if not stop_risk_df.empty else []
                holding_status = {
                    'protected_count': protected_count,
                    'eligible_count':  eligible_count,
                    'stop_loss_risk':  stop_loss_risk,
                    'min_hold_days':   hm.min_hold_days,
                    'position_rows':   status_df.to_dict('records') if not status_df.empty else [],
                }
                print(f"[OK] 持仓状态: 锁仓{protected_count}只 可换仓{eligible_count}只 止损风险{len(stop_loss_risk)}只")
        except Exception as e:
            print(f"[WARN] HoldingManager 状态跳过: {e}")

        # 5c. 分批加仓扫描（对 buy_phase=1 且盈利的持仓，提示可加仓）
        phase1_candidates = []
        try:
            phase1_candidates = pm.get_phase1_candidates()
            if phase1_candidates:
                names = [p.get('name', p['ts_code'])[:4] for p in phase1_candidates[:5]]
                print(f"[早盘推送] 分批加仓候选: {', '.join(names)}")
        except Exception as e:
            print(f"[WARN] 分批加仓扫描跳过: {e}")

        # 5d. 自动深度分析（对今日买入信号中未近期分析过的股票，最多3只）
        # deep_reports = []
        # if pool_result and not pool_result.get("signals", pd.DataFrame()).empty:
        #     print("[早盘推送] 触发个股深度分析...")
        #     try:
        #         deep_reports = _auto_deep_analyze(pool_result["signals"], max_stocks=3)
        #         print(f"[OK] 深度分析完成: {len(deep_reports)} 只")
        #     except Exception as e:
        #         print(f"[WARN] 深度分析跳过: {e}")

        # 5e. DecisionEngine 今日执行计划
        agent_plan_section = ""
        print("[早盘推送] 获取AI执行计划...")
        try:
            agent_plan_section = _get_agent_plan_section(timeout=180)  # 缩短超时到3分钟
            if agent_plan_section:
                print("[OK] AI执行计划生成完毕")
            else:
                print("[WARN] AI执行计划为空，跳过")
        except Exception as e:
            print(f"[WARN] AI执行计划跳过: {e}")

        # 6. 构建推送消息
        print("[早盘推送] 构建推送消息...")
        title, content = build_morning_message(
            result_df, market_info, position_df, pm,
            etf_df=etf_df, pool_result=pool_result,
            phase1_candidates=phase1_candidates,
            agent_plan_section=agent_plan_section,
            holding_status=holding_status,
            strategy_picks=strategy_picks,
            data_quality=data_quality,
        )
        
        # 检查消息内容是否有效
        if not content or len(content.strip()) < 50:
            print("[WARN] 消息内容过短，可能数据不完整")
        
        # 4. 读取通知配置
        notification_config = Config.get('notification', {})
        if not notification_config.get('enabled', False):
            print("[WARN] 通知功能未启用")
            return False
        
        provider = notification_config.get('provider', 'dingtalk')
        if provider != 'dingtalk':
            print(f"[INFO] 当前provider为{provider}")
        
        dingtalk_config = notification_config.get('dingtalk', {})
        webhook_url = dingtalk_config.get('webhook')
        secret_word = dingtalk_config.get('secret_word', '提醒')
        
        if not webhook_url:
            print("[ERROR] 钉钉webhook未配置")
            return False
        
        # 5. 创建推送实例
        notifier = NotifierFactory.create_notifier(
            'dingtalk',
            webhook_url=webhook_url,
            secret_word=secret_word
        )
        
        # 6. AI选股分析（可选，失败不影响主流程）
        ai_analysis_result = None
        print("[早盘推送] 尝试AI分析...")
        try:
            ai_analysis_result = add_ai_analysis_to_content(content)
            if ai_analysis_result:
                content = ai_analysis_result
                print("[OK] AI分析已添加到推送内容")
        except Exception as e:
            print(f"[WARN] AI分析跳过: {e}")
        
        # 7. 发送推送
        print("[早盘推送] 正在发送钉钉通知...")
        success = notifier.send_message(title, content, message_type='morning_push')
        
        if success:
            print("[OK] 早盘推送发送成功！")
            return True
        else:
            print("[ERROR] 早盘推送发送失败")
            return False
            
    except Exception as e:
        print(f"[ERROR] 早盘推送执行失败: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    print(f"\n[INFO] 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    success = send_morning_push()
    sys.exit(0 if success else 1)
