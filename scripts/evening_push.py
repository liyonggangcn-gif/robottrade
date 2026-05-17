"""
收盘推送 - 16:00执行
推送今日结果总结和复盘报告
"""
import sys
import os
import io
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 初始化日志
from src.utils.log_utils import init_logger
logger = init_logger("evening_push")

from src.utils.config_loader import Config
from src.utils.notifier import NotifierFactory
from src.portfolio.position_manager import PositionManager
from src.utils.db_utils import DBUtils
import pandas as pd
import subprocess


def get_ai_analysis_section():
    """获取AI分析报告段落"""
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    print("[AI分析] 获取选股结果...")
    
    # 获取今日选股结果
    try:
        import glob
        output_dir = os.path.join(os.path.dirname(__file__), '..', 'output')
        today_str = datetime.now().strftime('%Y%m%d')
        csv_files = glob.glob(os.path.join(output_dir, f'hybrid_picks_{today_str}.csv'))
        if not csv_files:
            csv_files = glob.glob(os.path.join(output_dir, 'hybrid_picks_*.csv'))
            if csv_files:
                csv_files = sorted(csv_files, reverse=True)
        if not csv_files:
            print("[AI分析] 无选股结果")
            return None
        
        picked_stocks = pd.read_csv(csv_files[0])
        stock_codes = picked_stocks['ts_code'].tolist()[:5]
    except Exception as e:
        print(f"[AI分析] 获取选股失败: {e}")
        return None
    
    print(f"[AI分析] 分析股票: {stock_codes}")
    
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
    
    if not results:
        return None
    
    # 生成报告
    ai_section = "\n---\n### 🤖 AI大师分析\n"
    if buy_signals:
        ai_section += f"**买入信号**: {', '.join(buy_signals)}\n"
    if sell_signals:
        ai_section += f"**卖出信号**: {', '.join(sell_signals)}\n"
    if hold_signals:
        ai_section += f"**持有观察**: {', '.join(hold_signals)}\n"
    ai_section += "\n_AI量化对冲基金分析_"
    
    return ai_section


def get_todays_performance():
    """获取今日推荐股票的表现
    
    Returns:
        dict: 表现统计
    """
    db = DBUtils()
    
    # 读取今日推荐的股票（从output目录）
    try:
        import glob
        output_dir = os.path.join(os.path.dirname(__file__), '..', 'output')
        today_str = datetime.now().strftime('%Y%m%d')
        csv_files = glob.glob(os.path.join(output_dir, f'hybrid_picks_{today_str}.csv'))
        
        if not csv_files:
            # 如果没有今天的文件，尝试最新的文件
            csv_files = glob.glob(os.path.join(output_dir, 'hybrid_picks_*.csv'))
            if csv_files:
                csv_files = sorted(csv_files, reverse=True)
        
        if not csv_files:
            return None
        
        picked_stocks = pd.read_csv(csv_files[0])
        ts_codes = picked_stocks['ts_code'].tolist()
        
    except Exception as e:
        print(f"[WARN] 无法读取推荐股票: {e}")
        return None
    
    # 获取今日和昨日数据
    sql_latest = "SELECT MAX(trade_date) as latest_date FROM stock_daily"
    latest_date = db.query_df(sql_latest)['latest_date'][0]
    
    sql_prev = f"""
    SELECT DISTINCT trade_date 
    FROM stock_daily 
    WHERE trade_date < '{latest_date}'
    ORDER BY trade_date DESC 
    LIMIT 1
    """
    prev_df = db.query_df(sql_prev)
    if prev_df.empty:
        print("[WARN] 无法获取前一交易日数据")
        return None
    prev_date = prev_df['trade_date'][0]
    
    # 获取推荐股票的今日表现
    ts_codes_str = "','".join(ts_codes)
    sql_performance = f"""
    SELECT 
        today.ts_code,
        today.close as today_close,
        prev.close as prev_close,
        (today.close - prev.close) / prev.close * 100 as change_pct,
        today.vol,
        today.total_mv
    FROM stock_daily today
    LEFT JOIN stock_daily prev ON today.ts_code = prev.ts_code AND prev.trade_date = '{prev_date}'
    WHERE today.trade_date = '{latest_date}'
    AND today.ts_code IN ('{ts_codes_str}')
    """
    
    performance = db.query_df(sql_performance)
    
    if len(performance) == 0:
        return None
    
    # 统计
    rise_count = len(performance[performance['change_pct'] > 0])
    fall_count = len(performance[performance['change_pct'] < 0])
    flat_count = len(performance[performance['change_pct'] == 0])
    avg_change = performance['change_pct'].mean()
    max_change = performance['change_pct'].max()
    min_change = performance['change_pct'].min()
    
    # 获取涨跌幅前3
    top3 = performance.nlargest(3, 'change_pct')
    bottom3 = performance.nsmallest(3, 'change_pct')
    
    return {
        'total': len(performance),
        'rise': rise_count,
        'fall': fall_count,
        'flat': flat_count,
        'avg_change': avg_change,
        'max_change': max_change,
        'min_change': min_change,
        'top3': top3,
        'bottom3': bottom3,
        'performance': performance
    }


def get_stock_detail(ts_code, db=None):
    """获取股票详细信息，包括板块、技术指标和今日表现
    
    Args:
        ts_code: 股票代码
        db: 数据库连接（可选）
        
    Returns:
        dict: 股票详细信息
    """
    if db is None:
        db = DBUtils()
    
    try:
        # 获取最新交易日期
        sql_latest = "SELECT MAX(trade_date) as latest_date FROM stock_daily"
        latest_date = db.query_df(sql_latest)['latest_date'][0]
        
        # 获取前一交易日
        sql_prev = f"""
        SELECT DISTINCT trade_date 
        FROM stock_daily 
        WHERE trade_date < '{latest_date}'
        ORDER BY trade_date DESC 
        LIMIT 1
        """
        prev_df = db.query_df(sql_prev)
        prev_date = prev_df['trade_date'][0] if not prev_df.empty else None
        
        # 获取股票基本信息（stock_info.ts_code存储完整格式，如 000001.SZ）
        sql_info = f"""
        SELECT ts_code, name, industry
        FROM stock_info
        WHERE ts_code = '{ts_code}'
        """
        info_df = db.query_df(sql_info)
        
        sector = "未知板块"
        name = ts_code
        if not info_df.empty:
            name = info_df.iloc[0]['name'] if pd.notna(info_df.iloc[0]['name']) else ts_code
            sector = info_df.iloc[0]['industry'] if pd.notna(info_df.iloc[0]['industry']) else "未知板块"
        
        # 获取今日和昨日行情数据（pb未同步到MySQL，不查询）
        sql_today = f"""
        SELECT close, vol, total_mv, pe_ttm
        FROM stock_daily
        WHERE ts_code = '{ts_code}' AND trade_date = '{latest_date}'
        """
        today_df = db.query_df(sql_today)
        
        sql_prev_data = f"""
        SELECT close
        FROM stock_daily
        WHERE ts_code = '{ts_code}' AND trade_date = '{prev_date}'
        """
        prev_data_df = db.query_df(sql_prev_data)
        
        # 计算今日涨跌幅
        today_change = 0
        current_price = 0
        if not today_df.empty and not prev_data_df.empty:
            today_close = today_df.iloc[0]['close']
            prev_close = prev_data_df.iloc[0]['close']
            if prev_close > 0:
                today_change = (today_close - prev_close) / prev_close * 100
            current_price = today_close
        
        # 获取技术指标数据（从stock_daily表直接计算）
        # 计算20日动量
        sql_mom = f"""
        SELECT close FROM stock_daily 
        WHERE ts_code = '{ts_code}' 
        AND trade_date <= '{latest_date}'
        ORDER BY trade_date DESC 
        LIMIT 21
        """
        mom_df = db.query_df(sql_mom)
        
        mom_20 = 0
        rsi_14 = 0
        vol_20 = 0
        
        if not mom_df.empty and len(mom_df) >= 2:
            # 计算20日动量
            if len(mom_df) >= 21:
                price_20d_ago = mom_df.iloc[19]['close']
                current = mom_df.iloc[0]['close']
                if price_20d_ago > 0:
                    mom_20 = (current - price_20d_ago) / price_20d_ago * 100
            
            # 计算14日RSI
            if len(mom_df) >= 15:
                gains = []
                losses = []
                for i in range(len(mom_df) - 1):
                    change = mom_df.iloc[i]['close'] - mom_df.iloc[i+1]['close']
                    if change > 0:
                        gains.append(change)
                        losses.append(0)
                    else:
                        gains.append(0)
                        losses.append(abs(change))
                
                avg_gain = sum(gains[:14]) / 14 if len(gains) >= 14 else 0
                avg_loss = sum(losses[:14]) / 14 if len(losses) >= 14 else 0
                
                if avg_loss == 0:
                    rsi_14 = 100
                else:
                    rs = avg_gain / avg_loss
                    rsi_14 = 100 - (100 / (1 + rs))
        
        # ATR需要High/Low数据，暂时设为0
        atr_14 = 0
        
        # 基本面数据：优先从 stock_daily 读（有 roe/gpr/pe_ttm），stock_info 兜底
        pe_ttm = 0
        roe = 0
        gpr = 0
        total_mv = 0
        sql_fund = f"""
        SELECT sd.pe_ttm, sd.roe, sd.gpr,
               COALESCE(si.total_mv, sd.total_mv, 0) AS total_mv
        FROM stock_daily sd
        LEFT JOIN stock_info si ON sd.ts_code COLLATE utf8mb4_general_ci = si.ts_code COLLATE utf8mb4_general_ci
        WHERE sd.ts_code = ? AND sd.trade_date = ?
        LIMIT 1
        """
        fund_df = db.query_df(sql_fund, params=[ts_code, latest_date])
        if not fund_df.empty:
            r = fund_df.iloc[0]
            pe_ttm   = float(r['pe_ttm'])   if pd.notna(r['pe_ttm'])   and r['pe_ttm']   > 0 else 0
            roe      = float(r['roe'])       if pd.notna(r['roe'])       else 0
            gpr      = float(r['gpr'])       if pd.notna(r.get('gpr'))   else 0
            total_mv = float(r['total_mv'])  if pd.notna(r['total_mv'])  else 0

        return {
            'ts_code': ts_code,
            'name': name,
            'sector': sector,
            'current_price': current_price,
            'today_change': today_change,
            'mom_20': mom_20,
            'vol_20': vol_20,
            'rsi_14': rsi_14,
            'atr_14': atr_14,
            'pe_ttm': pe_ttm,
            'roe': roe,
            'gpr': gpr,
            'total_mv': total_mv,
        }
        
    except Exception as e:
        print(f"[WARN] 获取股票详细信息失败 {ts_code}: {e}")
        return {
            'ts_code': ts_code,
            'name': ts_code,
            'sector': '未知板块',
            'current_price': 0,
            'today_change': 0,
            'mom_20': 0,
            'vol_20': 0,
            'rsi_14': 0,
            'atr_14': 0,
            'pe_ttm': 0,
            'pb': 0,
            'total_mv': 0,
            'roe': 0
        }


def get_market_summary():
    """获取市场整体表现
    
    Returns:
        dict: 市场统计，失败或空数据时返回 None
    """
    try:
        # 选最近一个数据量完整（>= 2000只）的交易日
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
            return None
        prev_date = str(prev_df.iloc[0]['trade_date']).strip()
        
        sql_stats = f"""
        SELECT 
            COUNT(*) as total_stocks,
            SUM(CASE WHEN today.close > prev.close THEN 1 ELSE 0 END) as rise_count,
            SUM(CASE WHEN today.close < prev.close THEN 1 ELSE 0 END) as fall_count,
            SUM(CASE WHEN today.close = prev.close THEN 1 ELSE 0 END) as flat_count,
            AVG((today.close - prev.close) / prev.close * 100) as avg_change,
            SUM(CASE WHEN (today.close - prev.close) / prev.close > 0.095 THEN 1 ELSE 0 END) as limit_up,
            SUM(CASE WHEN (today.close - prev.close) / prev.close < -0.095 THEN 1 ELSE 0 END) as limit_down
        FROM stock_daily today
        LEFT JOIN stock_daily prev ON today.ts_code = prev.ts_code AND prev.trade_date = '{prev_date}'
        WHERE today.trade_date = '{latest_date}'
        """
        stats = DBUtils.query_df(sql_stats)
        if stats.empty:
            return None
        s = stats.iloc[0]
        return {
            'date': latest_date,
            'total': int(s['total_stocks']) if pd.notna(s['total_stocks']) else 0,
            'rise': int(s['rise_count']) if pd.notna(s['rise_count']) else 0,
            'fall': int(s['fall_count']) if pd.notna(s['fall_count']) else 0,
            'flat': int(s['flat_count']) if pd.notna(s['flat_count']) else 0,
            'avg_change': float(s['avg_change']) if pd.notna(s['avg_change']) else 0,
            'limit_up': int(s['limit_up']) if pd.notna(s['limit_up']) else 0,
            'limit_down': int(s['limit_down']) if pd.notna(s['limit_down']) else 0
        }
    except Exception as e:
        print(f"[WARN] 获取市场概况失败: {e}")
        return None


def build_evening_message(today_perf, market_summary, position_summary=None,
                          stop_loss_list=None, take_profit_list=None,
                          sell_signals=None, holding_dashboard=None):
    """构建收盘推送消息

    结构: ⚡风险提示 → 💼持仓今日表现 → 🌍市场复盘 → 📈命中率

    Returns:
        tuple: (title, content)
    """
    today = datetime.now().strftime('%m月%d日')
    title = f"📊 收盘复盘 {today}"

    # 初始化LLM
    try:
        from src.utils.llm_client import LLMClient
        llm_client = LLMClient()
        llm_available = llm_client.is_available()
    except Exception as e:
        print(f"[WARN] 初始化LLM客户端失败: {e}")
        llm_client = None
        llm_available = False

    sections = []

    # ── 1. ⚡ 风险提示（卖出信号/止损止盈，最高优先级）─────────────
    risk_lines = []
    if sell_signals:
        URGENCY = {"high": "🚨[立即]", "medium": "⚠️[关注]"}
        TYPE_MAP = {"stop_loss": "价格止损", "valuation_expensive": "估值到顶", "profit_driver_broken": "盈利恶化"}
        risk_lines.append(f"**⚡ 触发卖出信号（{len(sell_signals)}只）**")
        for s in sell_signals:
            urg = URGENCY.get(s['urgency'], "⚠️")
            tname = TYPE_MAP.get(s['signal_type'], s['signal_type'])
            pl_str = f"{s['profit_loss_pct']*100:+.1f}%" if s.get('profit_loss_pct') is not None else ""
            risk_lines.append(f"- {urg} **{s['name']}**({s['ts_code'][:6]}) [{tname}] {s['reason']} {pl_str}")
    else:
        if stop_loss_list:
            risk_lines.append(f"**🚨 触发止损（{len(stop_loss_list)}只）**")
            for st in stop_loss_list:
                risk_lines.append(
                    f"- {st['name']}({st['ts_code'][:6]}) 现价{st['current_price']:.2f} ≤ 止损{st['stop_loss_price']:.2f} ({st['profit_loss_pct']*100:.1f}%)"
                )
        if take_profit_list:
            risk_lines.append(f"**🎯 触发止盈（{len(take_profit_list)}只）**")
            for st in take_profit_list:
                risk_lines.append(
                    f"- {st['name']}({st['ts_code'][:6]}) 现价{st['current_price']:.2f} ≥ 止盈{st['take_profit_price']:.2f} (+{st['profit_loss_pct']*100:.1f}%)"
                )
    if risk_lines:
        sections.append("\n".join(risk_lines))

    # ── 2. 💼 持仓今日表现 ──────────────────────────────────────
    if position_summary and position_summary.get('stock_count', 0) > 0:
        total_pl_pct = position_summary['total_profit_loss_pct'] * 100
        pos_pct_val  = position_summary['total_position_pct'] * 100
        pl_emoji = "🟢" if total_pl_pct > 0 else ("🔴" if total_pl_pct < 0 else "⚪")

        # 获取持仓轨道和持仓天数
        track_map = {}
        days_map  = {}
        B_TRACKS   = {'dividend', 'value'}
        TRACK_LABEL = {'sector_rotation': 'A轨', 'dividend': 'B轨', 'value': 'B轨', 'both': 'AB轨'}
        try:
            db_q = DBUtils()
            ts_list = [p['ts_code'] for p in position_summary['positions']]
            ts_str  = "','".join(ts_list)
            sql_track = f"""
            SELECT ts_code, track, trade_date as buy_date
            FROM daily_picks
            WHERE ts_code IN ('{ts_str}')
            ORDER BY trade_date DESC
            """
            track_df = db_q.query_df(sql_track)
            if not track_df.empty:
                for _, row in track_df.drop_duplicates('ts_code').iterrows():
                    tc = row['ts_code']
                    track_map[tc] = str(row['track']) if pd.notna(row['track']) else ''
                    try:
                        buy_d = pd.to_datetime(str(row['buy_date']))
                        days_map[tc] = (datetime.now() - buy_d).days
                    except Exception:
                        pass
        except Exception as e:
            print(f"[WARN] 获取持仓轨道信息失败: {e}")

        hold_lines = [
            f"**💼 持仓今日表现** ({position_summary['stock_count']}只 · 仓位{pos_pct_val:.1f}% · 浮盈{pl_emoji}{total_pl_pct:+.1f}%)"
        ]

        db = DBUtils()
        positions = position_summary['positions']

        # 批量抓取个股新闻（LLM可用时）
        stock_news_map = {}
        if llm_available:
            try:
                from src.feeds.stock_news_fetcher import fetch_stock_news_batch
                from src.feeds.news_fetcher import NewsFetcher
                print("[EveningPush] 抓取泛市场快讯用于个股过滤...")
                general_news = NewsFetcher().fetch(hours=24)
                top5_info = [{'ts_code': p['ts_code'], 'name': p['name'][:6]} for p in positions[:5]]
                stock_news_map = fetch_stock_news_batch(
                    top5_info, general_news=general_news, max_news_per_stock=8, sleep_sec=0.5
                )
                total_news = sum(len(v) for v in stock_news_map.values())
                print(f"[EveningPush] 共抓取 {total_news} 条个股相关新闻")
            except Exception as e:
                print(f"[WARN] 个股新闻抓取失败: {e}")

        for i, pos in enumerate(positions[:5]):
            ts_code = pos['ts_code']
            name    = pos['name'][:6]
            avg_cost        = pos['avg_cost']
            current_price   = pos['current_price']
            pl_pct  = pos['profit_loss_pct'] * 100
            pos_pct = pos['position_pct'] * 100

            track       = track_map.get(ts_code, '')
            min_hold    = 15 if track in B_TRACKS else 5
            track_label = TRACK_LABEL.get(track, '—')
            days_held   = days_map.get(ts_code)

            if days_held is not None:
                locked   = days_held < min_hold
                lock_str = f"⛔锁定({min_hold - days_held}天)" if locked else "✅可操作"
                days_str = f"{days_held}天"
            else:
                lock_str = "✅可操作"
                days_str = "—"

            stock_detail = get_stock_detail(ts_code, db)
            today_change = stock_detail['today_change']
            today_e = "🟢" if today_change > 0 else ("🔴" if today_change < 0 else "⚪")
            pl_e    = "🟢" if pl_pct > 0 else ("🔴" if pl_pct < 0 else "⚪")

            # 摘要行：名称(轨道·天数) 今日涨跌 持仓盈亏 锁定状态
            hold_lines.append(
                f"\n{i+1}. **{name}**({ts_code[:6]}) [{track_label}·{days_str}] "
                f"{today_e}今日{today_change:+.1f}%  {pl_e}持仓{pl_pct:+.1f}%  {lock_str}"
            )

            # 指标行（只显示有效数据）
            indic = []
            if stock_detail['pe_ttm'] > 0:
                indic.append(f"PE{stock_detail['pe_ttm']:.0f}x")
            if stock_detail['roe'] != 0:
                indic.append(f"ROE{stock_detail['roe']:.1f}%")
            if stock_detail.get('gpr', 0) > 0:
                indic.append(f"毛利{stock_detail['gpr']:.0f}%")
            if stock_detail['rsi_14'] > 0:
                rsi_val = stock_detail['rsi_14']
                rsi_state = "超买" if rsi_val > 70 else ("超卖" if rsi_val < 30 else "")
                indic.append(f"RSI{rsi_val:.0f}{rsi_state}")
            if stock_detail['mom_20'] != 0:
                indic.append(f"动量{stock_detail['mom_20']:+.1f}%")
            # 添加技术面状态
            if stock_detail['rsi_14'] > 0:
                if stock_detail['rsi_14'] > 70:
                    indic.append("⚠️RSI超买")
                elif stock_detail['rsi_14'] < 30:
                    indic.append("🔥RSI超卖")
            if indic:
                hold_lines.append(f"   📊 {' | '.join(indic)}")

            # LLM 新闻点评（首行摘要）
            if llm_available:
                stock_news = stock_news_map.get(ts_code, [])
                try:
                    stock_data_for_llm = {
                        'ts_code': ts_code, 'name': name, 'close': current_price,
                        'today_change': today_change, 'pe_ttm': stock_detail['pe_ttm'],
                        'roe': stock_detail['roe'], 'gpr': stock_detail.get('gpr', 0),
                        'total_mv': stock_detail['total_mv'], 'rsi_14': stock_detail['rsi_14'],
                        'mom_20': stock_detail['mom_20'], 'avg_cost': avg_cost,
                        'pl_pct': pl_pct, 'sector': stock_detail['sector'],
                    }
                    if stock_news:
                        commentary = llm_client.generate_stock_analysis_with_news(stock_data_for_llm, stock_news)
                    else:
                        context = f"成本{avg_cost:.2f} 现价{current_price:.2f} 浮盈{pl_pct:+.1f}% 今日{today_change:+.1f}%"
                        commentary = llm_client.generate_analysis(stock_data_for_llm, context)
                    if commentary and "LLM服务未配置" not in commentary:
                        news_tag   = f"(基于{len(stock_news)}条新闻)" if stock_news else "(无新闻)"
                        # 去头尾空行，找第一个有内容的句子
                        lines = [l.strip() for l in commentary.strip().split('\n') if l.strip() and not l.strip().startswith('#') and not l.strip().startswith('<think')]
                        excerpt = (lines[0] if lines else commentary.strip())[:80]
                        hold_lines.append(f"   💬 AI点评{news_tag}: {excerpt}")
                    
                    # 显示个股最新新闻摘要
                    if stock_news:
                        news_titles = [n.get('title', '')[:25] for n in stock_news[:3]]
                        if news_titles:
                            hold_lines.append(f"   📰 要闻: {' | '.join(news_titles)}")
                except Exception as e:
                    print(f"[WARN] {name} 点评生成失败: {e}")

        sections.append("\n".join(hold_lines))

    # ── 3. 🌍 市场复盘 ──────────────────────────────────────────
    total     = market_summary.get('total') or 0
    avg_chg   = market_summary.get('avg_change', 0)
    rise_pct  = (market_summary['rise'] / total * 100) if total > 0 else 0
    mkt_sent  = "🔴偏弱" if avg_chg < -1 else ("🟢强势" if avg_chg > 1 else "🟡震荡")

    mkt_lines = [f"🌍 市场复盘 | {mkt_sent} 均涨{avg_chg:+.2f}%"]
    mkt_lines.append(
        f"涨{market_summary['rise']}只({rise_pct:.0f}%) 跌{market_summary['fall']}只 "
        f"涨停{market_summary['limit_up']}只 跌停{market_summary['limit_down']}只"
    )
    # 动态仓位提示（有持仓时才显示）
    if position_summary:
        pos_pct_val = position_summary['total_position_pct'] * 100
        if pos_pct_val > 85:
            mkt_lines.append(f"⚠️ 仓位偏重({pos_pct_val:.0f}%)，{'市场偏弱建议适当减仓' if avg_chg < 0 else '注意分散风险'}")
        elif pos_pct_val < 40 and avg_chg > 1:
            mkt_lines.append(f"💡 仓位较轻({pos_pct_val:.0f}%)，市场走强可考虑加仓")
    sections.append("\n".join(mkt_lines))

    # ── 4. 📰 财经新闻汇总 ────────────────────────────────────────
    try:
        from src.feeds.news_fetcher import NewsFetcher
        news_fetcher = NewsFetcher()
        market_news = news_fetcher.fetch(hours=24, limit_per_source=15)
        
        if market_news:
            news_lines = ["**📰 财经要闻**"]
            for item in market_news[:8]:  # 最多8条
                title = item.title[:40] if hasattr(item, 'title') else item.get('title', '')[:40]
                source = item.source if hasattr(item, 'source') else item.get('source', '')
                news_lines.append(f"- {title} ({source})")
            sections.append("\n".join(news_lines))
    except Exception as e:
        print(f"[WARN] 财经新闻获取失败: {e}")

    # ── 5. 📈 今日推荐命中率 ────────────────────────────────────
    if today_perf and today_perf.get('total', 0) > 0:
        win_rate   = today_perf['rise'] / today_perf['total'] * 100
        beat_mkt   = today_perf['avg_change'] > avg_chg
        perf_emoji = "🎉" if win_rate >= 60 else ("👍" if win_rate >= 50 else "😐")
        beat_str   = "✅跑赢大盘" if beat_mkt else "⚠️跑输大盘"

        perf_lines = [
            f"📈 今日推荐命中率 {perf_emoji} {win_rate:.0f}% ({today_perf['rise']}涨/{today_perf['fall']}跌) | {beat_str}",
            f"均涨{today_perf['avg_change']:+.2f}% | 最强{today_perf['max_change']:+.2f}% | 最弱{today_perf['min_change']:+.2f}%",
        ]
        top3_parts = []
        for _, row in today_perf['top3'].iterrows():
            top3_parts.append(f"{row['ts_code'][:6]}({row['change_pct']:+.1f}%)")
        if top3_parts:
            perf_lines.append(f"涨幅前三: {' '.join(top3_parts)}")
        sections.append("\n".join(perf_lines))

    content = "\n\n".join(s for s in sections if s.strip())
    return title, content


def should_send_evening_push(force=False):
    """检查是否应该发送收盘推送（去重：最近1小时内发过则跳过）"""
    if force:
        print("[INFO] 强制推送模式")
        return True
        
    try:
        db = DBUtils()
        with db.get_conn() as conn:
            cursor = conn.cursor()
            # 检查最近1小时是否已有收盘推送
            cursor.execute("""
                SELECT COUNT(*) FROM push_messages
                WHERE message_type = 'evening_push'
                  AND created_at >= DATE_SUB(NOW(), INTERVAL 1 HOUR)
            """)
            recent_count = cursor.fetchone()[0]
            
            if recent_count > 0:
                print("[INFO] 最近1小时内已有收盘推送，跳过")
                return False
            return True
    except Exception as e:
        print(f"[WARN] 检查推送去重失败: {e}")
        return True  # 检查失败时允许推送


def send_evening_push():
    """发送收盘推送（增强版：含持仓分析）"""
    print("=" * 60)
    print("  收盘推送 - 16:00 (含持仓分析)")
    print("=" * 60)
    
    # 0. 推送去重检查
    if not should_send_evening_push():
        print("[INFO] 跳过收盘推送（已发送）")
        return True
    
    try:
        # 0. 交易日检查（改为警告，不阻断）
        from src.utils.trade_calendar import is_trade_day
        is_trading = is_trade_day()
        if not is_trading:
            print("[WARN] 今日可能非交易日，但继续尝试推送（如有持仓数据）")
        else:
            print("[INFO] 今日为交易日")
        
        # 1. MySQL 可用性检查（MySQL模式下，连不上则立即告警并退出）
        if DBUtils._is_mysql_mode():
            print("[收盘推送] 检查MySQL连接...")
            test_conn = DBUtils._get_mysql_conn()
            if test_conn is None:
                err_msg = "收盘推送失败（提醒）：数据库（MySQL）不可达，请检查 192.168.3.41 服务状态"
                print(f"[ERROR] {err_msg}")
                # 尝试发送告警
                try:
                    notification_config = Config.get('notification', {})
                    dingtalk_config = notification_config.get('dingtalk', {})
                    webhook_url = dingtalk_config.get('webhook')
                    secret_word = dingtalk_config.get('secret_word', '提醒')
                    if webhook_url:
                        notifier = NotifierFactory.create_notifier(
                            'dingtalk', webhook_url=webhook_url, secret_word=secret_word
                        )
                        notifier.send_message("⚠️ 收盘推送失败（提醒）", err_msg)
                except Exception:
                    pass
                return False
            test_conn.close()

        # 2. 更新选股绩效（N日收益率）
        print("[收盘推送] 更新选股绩效...")
        try:
            from src.backtest.performance_tracker import PerformanceTracker
            pt = PerformanceTracker()
            result = pt.update_picks_performance()
            updated = result.get('updated', 0)
            print(f"[OK] 选股绩效更新: {updated} 只已完成收益计算")
        except Exception as e:
            print(f"[WARN] 选股绩效更新失败: {e}")
        
        # 3. 初始化仓位管理器
        print("[收盘推送] 初始化仓位管理器...")
        pm = PositionManager()
        
        # 2. 更新持仓价格
        print("[收盘推送] 更新持仓价格...")
        pm.update_position_prices()
        
        # 3. 获取持仓汇总
        print("[收盘推送] 获取持仓汇总...")
        position_summary = pm.get_position_summary()
        
        # 4. 盘后止损止盈展示（仅供人工参考，实际执行由 RiskController 在盘中完成）
        print("[收盘推送] 检查止损止盈（展示用）...")
        stop_loss_list, take_profit_list = pm.check_stop_loss_take_profit()
        if stop_loss_list:
            print(f"  [WARN] {len(stop_loss_list)} 只股票触发止损")
        if take_profit_list:
            print(f"  [OK] {len(take_profit_list)} 只股票触发止盈")

        # 4b. 三类卖出信号检查（新）
        print("[收盘推送] 检查三类卖出信号...")
        sell_signals = []
        holding_dashboard = ""
        try:
            sell_signals = pm.check_sell_signals()
            holding_dashboard = pm.format_holding_dashboard()
            by_type = {}
            for s in sell_signals:
                by_type.setdefault(s['signal_type'], []).append(s['name'])
            for stype, names in by_type.items():
                print(f"  [{stype}] {len(names)}只: {', '.join(names[:3])}")
        except Exception as e:
            print(f"[WARN] 三类卖出信号跳过: {e}")
        
        # 5. 获取今日推荐表现
        print("[收盘推送] 获取今日推荐表现...")
        today_perf = get_todays_performance()
        
        # 6. 获取市场概况
        print("[收盘推送] 获取市场概况...")
        market_summary = get_market_summary()
        if market_summary is None:
            market_summary = {'date': '-', 'total': 0, 'rise': 0, 'fall': 0, 'flat': 0, 'avg_change': 0, 'limit_up': 0, 'limit_down': 0}
        
        # 7. 构建推送消息
        print("[收盘推送] 构建推送消息...")
        title, content = build_evening_message(
            today_perf,
            market_summary,
            position_summary,
            stop_loss_list,
            take_profit_list,
            sell_signals=sell_signals,
            holding_dashboard=holding_dashboard,
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
        
        # 7. AI选股分析（可选，失败不影响主流程）
        print("[收盘推送] 尝试AI分析...")
        try:
            ai_section = get_ai_analysis_section()
            if ai_section:
                content = content + "\n" + ai_section
                print("[OK] AI分析已添加到推送内容")
        except Exception as e:
            print(f"[WARN] AI分析跳过: {e}")
        
        # 8. 发送推送
        print("[收盘推送] 正在发送钉钉通知...")
        success = notifier.send_message(title, content, message_type='evening_push')
        
        if success:
            print("[OK] 收盘推送发送成功！")
            return True
        else:
            print("[ERROR] 收盘推送发送失败")
            return False
            
    except Exception as e:
        print(f"[ERROR] 收盘推送执行失败: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    print(f"\n[INFO] 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    success = send_evening_push()
    sys.exit(0 if success else 1)
