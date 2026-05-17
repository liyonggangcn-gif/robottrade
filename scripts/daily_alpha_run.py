"""
Daily Alpha Run - 一键执行完整量化流水线

流水线步骤:
  1. sync_daily_data()   -- 同步最新行情数据 (Tushare/eFinance/Baostock)
  2. sync_concepts()     -- 同步概念/题材映射 (Tushare)
  3. run_ai_model()      -- LightGBM AI 训练 & 预测 (可选)
  4. HybridStrategy.run() -- 生成混合策略选股信号

用法:
    python scripts/daily_alpha_run.py
    python scripts/daily_alpha_run.py --skip-qlib    # 跳过 AI 训练 (加速，qlib 已移除)
    python scripts/daily_alpha_run.py --skip-sync    # 跳过数据同步 (仅选股)
    python scripts/daily_alpha_run.py --top-k 30      # 选出 Top 30

Windows 注意: 使用 UTF-8 输出, 避免 GBK 编码错误
"""

import sys
import os
import io
import time
import argparse
import pandas as pd
from datetime import datetime

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 初始化日志（同时输出到控制台和 logs/daily_alpha_run_YYYYMMDD.log）
from src.utils.log_utils import init_logger
logger = init_logger("daily_alpha_run")


def _quick_alert(msg: str):
    """立即向钉钉发送错误告警，不等到流水线结束。"""
    try:
        from src.utils.config_loader import Config
        from src.utils.notifier import NotifierFactory
        cfg = Config.get('notification', {})
        if not cfg.get('enabled', False):
            return
        dt_cfg = cfg.get('dingtalk', {})
        webhook = dt_cfg.get('webhook')
        if not webhook:
            return
        notifier = NotifierFactory.create_notifier(
            'dingtalk',
            webhook_url=webhook,
            secret_word=dt_cfg.get('secret_word', '提醒'),
        )
        title = f"⚠️ 量化系统告警 {datetime.now().strftime('%m月%d日 %H:%M')}"
        notifier.send_message(title, f"**【错误】** {msg}\n\n请检查服务器日志。", message_type='error_alert')
        print(f"[ALERT] 已发送钉钉告警: {msg}")
    except Exception as ex:
        print(f"[ALERT] 告警发送失败: {ex}")


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='Daily Alpha Run - 每日量化流水线')
    parser.add_argument('--skip-sync', action='store_true',
                        help='跳过数据同步步骤 (仅运行选股)')
    parser.add_argument('--skip-qlib', action='store_true',
                        help='跳过 AI 训练步骤 (使用已有 AI 评分，qlib 已卸载)')
    parser.add_argument('--skip-news', action='store_true',
                        help='跳过新闻LLM分析步骤 (加速选股)')
    parser.add_argument('--skip-concepts', action='store_true',
                        help='跳过概念同步 (使用已有概念数据)')
    parser.add_argument('--top-k', type=int, default=20,
                        help='选股数量 (默认: 20)')
    parser.add_argument('--watch-list-only', action='store_true',
                        help='已废弃：现始终同步全市场，此参数无效果')
    parser.add_argument('--skip-notification', action='store_true',
                        help='跳过钉钉推送通知')
    parser.add_argument('--full-fundamental', action='store_true',
                        help='强制更新全部基本面数据（默认只增量更新，一周一次全量）')
    parser.add_argument('--wait-sync', action='store_true',
                        help='拆分部署模式：等待群晖 sync_data.py 写入 sync_status=done 后再运行')
    return parser.parse_args()


def _get_previous_picks_sell_list(output_dir, today_str, current_ts_codes_set, top_k_keep=20):
    """
    对比「上一期推荐」与「本期推荐」，上期有、本期已掉出 top 的视为建议卖出(看跌)。

    Returns:
        list of dict: [{"ts_code": str, "name": str}, ...]，无则返回 []
    """
    import glob
    if not current_ts_codes_set or not output_dir:
        return []
    pattern = os.path.join(output_dir, "hybrid_picks_*.csv")
    files = glob.glob(pattern)
    # 排除今日，按文件名日期取最近一期
    prev_files = [f for f in files if os.path.basename(f) != f"hybrid_picks_{today_str}.csv"]
    if not prev_files:
        return []
    prev_files.sort(key=lambda x: os.path.basename(x), reverse=True)
    prev_path = prev_files[0]
    try:
        import pandas as pd
        prev = pd.read_csv(prev_path, encoding="utf-8-sig")
    except Exception:
        return []
    if prev.empty or "ts_code" not in prev.columns:
        return []
    # 上期取前 top_k_keep 只，本期未入选的为建议卖出
    prev = prev.head(top_k_keep)
    prev_codes = set(prev["ts_code"].astype(str).str.strip())
    sell_codes = prev_codes - current_ts_codes_set
    if not sell_codes:
        return []
    prev["ts_code"] = prev["ts_code"].astype(str).str.strip()
    sell_df = prev[prev["ts_code"].isin(sell_codes)][["ts_code", "name"]].drop_duplicates("ts_code")
    return sell_df.to_dict("records")


def _format_report_section_1_guide(result_df, sell_list=None, tracker_info=None):
    """类型一：选股+持仓追踪（手机端短行优化）"""
    track_icons = {'sector_rotation': '🔄', 'value': '💎', 'both': '⭐'}
    out = "**一、今日选股（双轨 A/B各50%仓位）**\n"
    if result_df is not None and len(result_df) > 0:
        # 按轨道分组显示，各轨50%仓位
        for track_label, track_name, track_letter in [
            ('sector_rotation', '行业轮动', 'A'),
            ('value',           '价值质量', 'B'),
        ]:
            sub = result_df[result_df['track'] == track_label].reset_index(drop=True) \
                  if 'track' in result_df.columns else result_df
            if sub.empty and track_label == 'sector_rotation':
                sub = result_df  # 降级兼容
            icon = track_icons.get(track_label, '')
            out += f"{icon}**{track_letter}轨·{track_name}**（50%仓位）\n"
            for i, (_, row) in enumerate(sub.head(10).iterrows(), 1):
                name = (row.get("name") or "")[:4]
                code = (row.get("ts_code") or "")[:6]
                score = f"{row['final_score']:.2f}" if "final_score" in row else "-"
                out += f"{i}. {name} {code} {score}\n"
        out += "\n"
    else:
        out += "今日无选股结果\n\n"

    # 持仓追踪（核心信息）
    if tracker_info:
        out += tracker_info

    # 建议卖出
    if sell_list:
        out += "\n**调出卖出**\n"
        for item in sell_list[:10]:
            name = (item.get("name") or "")[:4]
            code = (item.get("ts_code") or "")[:6]
            pct = item.get("profit_pct")
            days = item.get("holding_days")
            if pct is not None:
                icon = "+" if pct >= 0 else ""
                pct_s = f"{icon}{pct:.1f}%"
            else:
                pct_s = ""
            days_s = f"{days}天" if days is not None else ""
            out += f"{'🔴' if (pct or 0) < 0 else '🟢'}{name} {code} {pct_s} {days_s}\n"
    return out


def _format_report_section_2_industry(industry_timing_data, etf_selector_result=None):
    """类型二：行业+ETF（手机端精简）"""
    out = "\n**二、行业与ETF**\n"

    # 行业择机
    if industry_timing_data:
        cycle = industry_timing_data.get("current_cycle", "")
        cycle_cn = {"early": "早周期", "mid": "中周期", "late": "晚周期", "defensive": "防御"}.get(cycle, cycle)
        out += f"周期：{cycle_cn}\n"
        emerging_df = industry_timing_data.get("emerging")
        mature_df = industry_timing_data.get("mature")
        if emerging_df is not None and not emerging_df.empty:
            pen_cn = {"early_growth": "破壁", "mid_growth": "高速", "mature": "饱和", "late": "晚", "decline": "衰退"}
            items = []
            for _, row in emerging_df.head(5).iterrows():
                pen = pen_cn.get(row.get("penetration_phase", ""), "")
                items.append(f"{row['industry']}({pen})")
            out += "新兴：" + " ".join(items) + "\n"
        if mature_df is not None and not mature_df.empty:
            items = []
            for _, row in mature_df.head(5).iterrows():
                match = "✓" if row.get("cycle_match") else ""
                items.append(f"{row['industry']}{match}")
            out += "成熟：" + " ".join(items) + "\n"

    # ETF 推荐（只展示评分 >= 2 的，精简格式）
    if etf_selector_result and etf_selector_result.get("by_industry"):
        from src.utils.config_loader import Config
        cfg = Config.get("etf_selector") or {}
        hold_hint = cfg.get("holding_period_hint", "1～3个月")
        out += f"\n**ETF推荐**（持有{hold_hint}）\n"
        sig_icon = {"重点关注": "★", "积极配置": "▲", "适度配置": "●", "观望": "○", "回避": "×"}
        # 按评分降序，只展示 >= 0 的
        all_etfs = []
        for ind, etf_list in etf_selector_result["by_industry"].items():
            for x in (etf_list or []):
                x["_ind"] = ind
                all_etfs.append(x)
        all_etfs.sort(key=lambda e: e.get("score", 0), reverse=True)
        # 只展示评分 >= 2 的前 10 只
        top_etfs = [e for e in all_etfs if e.get("score", 0) >= 2][:10]
        if top_etfs:
            for x in top_etfs:
                icon = sig_icon.get(x.get("signal", ""), "")
                # ETF名称截短
                name = (x.get("name") or "")
                if len(name) > 8:
                    name = name[:8]
                code = (x.get("code") or "")[-6:]
                score = x.get("score", 0)
                ind = x.get("_ind", "")
                out += f"{icon}{name} {code} {ind}\n"
        else:
            out += "暂无高评分ETF\n"

        # 评分 <= 0 的汇总一行
        low_etfs = [e for e in all_etfs if e.get("score", 0) <= 0]
        if low_etfs:
            low_inds = list(dict.fromkeys(e.get("_ind", "") for e in low_etfs))
            out += f"观望：{'、'.join(low_inds[:6])}\n"

        # DeepSeek 建议（精简）
        if etf_selector_result.get("llm_advice"):
            advice = etf_selector_result["llm_advice"].strip()
            # 截取前200字
            if len(advice) > 200:
                advice = advice[:200] + "..."
            out += f"\nAI建议：{advice}\n"
    return out


def _format_report_section_3_status(results, data_warnings, minutes, seconds, today):
    """类型三：系统状态（精简）"""
    out = f"\n**三、系统** {today} 耗时{minutes}分{seconds}秒\n"
    # 只展示非 OK 的步骤，全部OK则一句话
    issues = []
    for k, v in results.items():
        v_str = str(v)
        if 'FAILED' in v_str:
            issues.append(f"{k}: {v_str[:30]}")
    if data_warnings:
        for w in data_warnings[:3]:
            issues.append(str(w)[:40])
    if issues:
        out += "异常：\n"
        for w in issues:
            out += f"· {w}\n"
    else:
        out += "各步骤正常\n"
    return out


def _format_report_section_value(value_df):
    """价值选股板块（高增长×高利润×高护城河×低估值）"""
    out = "\n**价值精选**（长线视角）\n"
    if value_df is None or value_df.empty:
        out += "暂无数据\n"
        return out

    for idx, row in value_df.head(8).iterrows():
        rank = idx + 1
        name = (row.get('name') or '')[:5]
        code = (row.get('ts_code') or '')[:6]
        roe = row.get('roe')
        pe = row.get('pe_ttm')
        peg = row.get('peg')
        yoy = row.get('netprofit_yoy')
        score = row.get('value_score', 0)

        metrics = []
        if pd.notna(roe):
            metrics.append(f"ROE{roe:.0f}%")
        if pd.notna(pe):
            metrics.append(f"PE{pe:.0f}")
        if pd.notna(peg):
            metrics.append(f"PEG{peg:.1f}")
        if pd.notna(yoy):
            sign = "+" if yoy >= 0 else ""
            metrics.append(f"增长{sign}{yoy:.0f}%")

        metric_str = " ".join(metrics)
        out += f"{rank}. {name} {code} {score:.2f} {metric_str}\n"
    return out


def _format_report_section_news(news_analysis):
    """零节：宏观新闻风险摘要（仅在中高风险或有强利好板块时展示）"""
    if not news_analysis:
        return ""

    risk = news_analysis.get('risk_level', '低')
    sentiment = news_analysis.get('market_sentiment', '中性')
    summary = news_analysis.get('summary', '')
    recommendation = news_analysis.get('recommendation', '')
    sectors = news_analysis.get('sector_impacts', [])
    action = news_analysis.get('action', 'hold')

    # 低风险中性时不展示（减少噪音）
    if risk == '低' and sentiment == '中性' and action == 'hold':
        return ""

    risk_emoji = {"极高": "🚨", "高": "🔴", "中": "🟡", "低": "🟢"}.get(risk, "⚠️")
    action_map = {
        "full_liquidate": "🚨立即清仓", "reduce_major": "🔴大幅减仓",
        "reduce_minor": "🟡小幅减仓", "hold": "🟢继续持仓", "add_position": "💹逢低加仓"
    }
    action_text = action_map.get(action, action)

    out = f"\n**零、市场风险预警**\n"
    out += f"{risk_emoji}风险:{risk} | {action_text}\n"
    if summary:
        out += f"> {summary}\n"

    # 利好板块（最多3个）
    bull_sectors = [s for s in sectors if s.get('direction') == '利好' and s.get('strength') in ('强', '中')]
    if bull_sectors:
        out += "利好: " + "、".join(f"{s['sector']}({s.get('strength','')})" for s in bull_sectors[:3]) + "\n"

    # 利空板块（最多2个）
    bear_sectors = [s for s in sectors if s.get('direction') == '利空' and s.get('strength') == '强']
    if bear_sectors:
        out += "利空: " + "、".join(f"{s['sector']}" for s in bear_sectors[:2]) + "\n"

    if recommendation and risk in ('极高', '高'):
        out += f"建议: {recommendation[:80]}\n"

    out += "\n"
    return out


def send_dingtalk_notification(results, result_df, minutes, seconds, success_status, industry_timing_data=None, data_warnings=None, sell_list=None, etf_selector_result=None, tracker_info=None, news_analysis=None, value_result_df=None):
    """发送钉钉推送通知

    Args:
        results: 执行结果字典
        result_df: 选股结果DataFrame
        minutes: 执行时间（分钟）
        seconds: 执行时间（秒）
        success_status: 是否执行成功
        industry_timing_data: 行业择机 run_split 结果（可选）
        data_warnings: 数据源/接口异常列表（可选）
        sell_list: 建议卖出列表（可选）[{"ts_code","name","profit_pct","holding_days"},...]
        etf_selector_result: ETF挑选结果（可选），与行业择机联动的主题ETF推荐
        tracker_info: 推荐追踪格式化文本（可选），由 RecommendationTracker.format_for_dingtalk() 生成
    """
    from src.utils.config_loader import Config
    from src.utils.notifier import NotifierFactory
    
    # 读取通知配置
    notification_config = Config.get('notification', {})
    if not notification_config.get('enabled', False):
        print("[INFO] 通知功能未启用")
        return
    
    provider = notification_config.get('provider', 'dingtalk')
    if provider != 'dingtalk':
        print(f"[INFO] 当前通知provider为{provider}，跳过钉钉推送")
        return
    
    dingtalk_config = notification_config.get('dingtalk', {})
    webhook_url = dingtalk_config.get('webhook')
    secret_word = dingtalk_config.get('secret_word', '提醒')
    
    if not webhook_url:
        print("[WARN] 钉钉webhook未配置")
        return
    
    # 创建钉钉推送实例
    notifier = NotifierFactory.create_notifier(
        'dingtalk',
        webhook_url=webhook_url,
        secret_word=secret_word
    )
    
    # 钉钉日报按三种类型组织：选股操作指南、行业推荐(周期+渗透率)、数据与异常
    today = datetime.now().strftime('%Y-%m-%d %H:%M')
    status_emoji = "✅" if success_status else "⚠️"
    title = f"{status_emoji} 量化选股日报 {datetime.now().strftime('%m月%d日')}"

    section_news = _format_report_section_news(news_analysis)
    section1 = _format_report_section_1_guide(result_df, sell_list=sell_list or [], tracker_info=tracker_info)
    section_value = _format_report_section_value(value_result_df)
    section2 = _format_report_section_2_industry(industry_timing_data, etf_selector_result)
    section3 = _format_report_section_3_status(results, data_warnings or [], minutes, seconds, today)
    tips = "\n---\n仅供参考，理性决策，注意风险。"

    content = section_news + section1 + section_value + section2 + section3 + tips
    
    # 发送通知
    print("[INFO] 正在发送钉钉通知...")
    success = notifier.send_message(title, content, message_type='daily_alpha_run')
    if success:
        print("[OK] 钉钉通知发送成功")
    else:
        print("[WARN] 钉钉通知发送失败")


def step_banner(step_num, total, title):
    """打印步骤横幅（含时间戳心跳）"""
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"\n{'=' * 60}")
    print(f"  Step {step_num}/{total}: {title}  [{ts}]")
    print(f"{'=' * 60}\n")


class _StepTimer:
    """Context manager：自动记录步骤耗时并心跳输出"""
    def __init__(self, label):
        self.label = label
        self.t0 = None

    def __enter__(self):
        self.t0 = time.time()
        print(f"[HB] {self.label} 开始 @ {datetime.now().strftime('%H:%M:%S')}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.time() - self.t0
        if exc_type:
            print(f"[HB] {self.label} 异常结束 ({elapsed:.1f}s): {exc_val}")
        else:
            print(f"[HB] {self.label} 完成 ({elapsed:.1f}s)")
        return False  # 不吞异常


def wait_for_sync(timeout_min: int = 60) -> bool:
    """
    等待群晖（192.168.3.41）的 sync_data.py 完成。
    仅当 --wait-sync 参数传入时调用。
    返回 True=数据就绪, False=超时/失败。

    优化：若今日同步已运行超过 30 分钟仍未完成，说明是全量长跑型同步，
    无需等待——直接放行用昨日数据，避免延误选股推送。
    """
    from src.utils.db_utils import DBUtils
    import datetime as _dt
    deadline = time.time() + timeout_min * 60
    print(f"[Wait] 等待数据同步完成（超时 {timeout_min} 分钟）...")
    while time.time() < deadline:
        try:
            df = DBUtils.query_df(
                "SELECT status, started_at FROM sync_status WHERE sync_date = CURDATE()"
            )
            if not df.empty:
                status = df.iloc[0]['status']
                if status == 'done':
                    print("[Wait] 数据同步完成，继续执行")
                    return True
                if status == 'fail':
                    print("[Wait] 数据同步报告失败，继续执行（使用昨日数据）")
                    return False
                if status == 'running':
                    started_at = df.iloc[0].get('started_at')
                    if started_at is not None and not _dt.datetime == type(started_at):
                        try:
                            started_at = _dt.datetime.fromisoformat(str(started_at))
                        except Exception:
                            started_at = None
                    if started_at is not None:
                        running_min = (
                            _dt.datetime.now() - started_at.replace(tzinfo=None)
                        ).total_seconds() / 60
                        if running_min > 30:
                            print(f"[Wait] 同步已运行 {running_min:.0f} 分钟仍未完成"
                                  f"（全量同步），跳过等待，使用昨日数据")
                            return False
        except Exception as e:
            print(f"[Wait] 查询 sync_status 失败: {e}")
        time.sleep(60)
    print("[Wait] 等待超时，继续执行（使用已有数据）")
    return False


def run_pipeline(args):
    """执行完整流水线"""

    # 拆分部署模式：等待群晖数据同步完成
    if getattr(args, 'wait_sync', False):
        wait_for_sync(timeout_min=60)

    # 交易日检查（非交易日仅提示，不阻断）
    try:
        from src.utils.trade_calendar import is_trade_day
        if not is_trade_day():
            print("[WARN] 今日非交易日，选股结果可能为前一交易日")
    except Exception:
        pass

    # 代理自适应：代理连不上时自动取消，改用直连
    try:
        from src.utils.network_utils import ensure_proxy_adaptive
        ensure_proxy_adaptive()
    except Exception as e:
        logger.warning(f"网络检测跳过: {e}")

    start_time = time.time()
    total_steps = 6
    results = {}
    data_warnings = []  # 数据源/接口异常，供钉钉日报展示
    news_analysis = None        # 新闻LLM分析结果
    news_boost_sectors = []     # 新闻检测到的利好行业，传给HybridStrategy
    dynamic_hot_topics = []     # 动态识别的热点主题（自动选出，替代静态配置）
    news_position_scale = 1.0   # 情绪择时仓位缩放（0=清仓, 0.3=大幅减, 0.6=小幅减, 1.0=正常, 1.3=加仓）
    today_date = datetime.now().strftime('%Y-%m-%d')
    today_str = datetime.now().strftime('%Y%m%d')

    print("=" * 60)
    print("  Daily Alpha Run - 每日量化流水线")
    print(f"  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ==================================================================
    # Step 0: 宏观新闻分析（MarketNewsAnalyzer → 动态热点感知）
    # ==================================================================
    if getattr(args, 'skip_news', False):
        # 使用涨停数据判断热门板块（无LLM，快速）
        try:
            from src.analysis.limitup_sector_analyzer import get_limitup_analysis
            limitup_result = get_limitup_analysis(today_date)
            news_boost_sectors = limitup_result.get('hot_sectors', [])[:6]
            print(f"[OK] 涨停热门板块 ({limitup_result['limitup_count']}只): {news_boost_sectors[:6]}")
            results['news_analysis'] = (
                f'OK (涨停={limitup_result["limitup_count"]}只, '
                f'热门板块={len(news_boost_sectors)}个)'
            )
        except Exception as e:
            print(f"[WARN] 涨停分析失败: {e}")
            results['news_analysis'] = 'FAILED: ' + str(e)[:50]
    else:
        step_banner(0, total_steps, "宏观新闻分析（LLM感知市场风险）")
        try:
            from src.risk.market_news_analyzer import MarketNewsAnalyzer
            analyzer = MarketNewsAnalyzer()
            news_analysis = analyzer.analyze(hours=12, max_news=60)

            risk_level = news_analysis.get('risk_level', '低')
            sentiment = news_analysis.get('market_sentiment', '中性')
            action = news_analysis.get('action', 'hold')
            pos_rec = analyzer.recommend_position_ratio(news_analysis)
            print(f"[OK] 新闻分析完成: 风险={risk_level}, 情绪={sentiment}, 建议={action}")
            print(f"[OK] 建议持仓比例: {pos_rec['ratio']}%  "
                  f"区间[{pos_rec['band'][0]}%~{pos_rec['band'][1]}%]  "
                  f"({pos_rec['reasoning']})")

            # 提取利好板块 → 作为动态热点传给策略
            for sector_info in news_analysis.get('sector_impacts', []):
                direction = sector_info.get('direction', '')
                strength = sector_info.get('strength', '弱')
                sector_name = sector_info.get('sector', '')
                if direction == '利好' and strength in ('强', '中') and sector_name:
                    news_boost_sectors.append(sector_name)

            if news_boost_sectors:
                print(f"[INFO] 新闻利好板块 ({len(news_boost_sectors)}个): {news_boost_sectors[:6]}")
            else:
                print("[INFO] 未检测到强烈利好板块信号")

            # 风险等级 → 仓位倍率（用于控制本次选股数量）
            # full_liquidate=0  reduce_major=0.3  reduce_minor=0.6  hold=1.0  add_position=1.3
            ACTION_SCALE = {
                'full_liquidate': 0.0,
                'reduce_major':   0.3,
                'reduce_minor':   0.6,
                'hold':           1.0,
                'add_position':   1.3,
            }
            news_position_scale = ACTION_SCALE.get(action, 1.0)
            if news_position_scale < 1.0:
                data_warnings.append(
                    f"市场风险{risk_level}({action})！{news_analysis.get('summary', '')} "
                    f"→ 选股数量缩减至{int(news_position_scale*100)}%"
                )
                logger.warning(f"市场风险等级: {risk_level}，action={action}，"
                      f"选股仓位缩减至 {int(news_position_scale*100)}%")

            results['news_analysis'] = (
                f'OK (风险={risk_level}, 情绪={sentiment}, action={action}, '
                f'仓位={int(news_position_scale*100)}%, 利好板块={len(news_boost_sectors)}个)'
            )
        except Exception as e:
            news_position_scale = 1.0   # 分析失败时不限制仓位
            results['news_analysis'] = f'SKIPPED: {str(e)[:50]}'
            logger.warning(f"新闻分析跳过: {e}")

    # ------------------------------------------------------------------
    # Step 0b: 动态热点识别（市场动量 + 新闻融合）
    # ------------------------------------------------------------------
    step_banner(0, total_steps, "动态热点识别（市场动量+新闻融合）")
    try:
        from src.analysis.hot_topic_detector import HotTopicDetector
        from src.utils.config_loader import Config
        detector = HotTopicDetector()
        fallback = Config.get('hot_topics_fallback') or []
        dynamic_hot_topics, topic_scores = detector.detect(
            news_analysis=news_analysis,
            top_k=25,
        )
        if dynamic_hot_topics:
            print(f"[OK] 动态热点识别完成: {len(dynamic_hot_topics)} 个主题")
            print(f"     TOP5: {dynamic_hot_topics[:5]}")
            results['hot_topics'] = f'OK ({len(dynamic_hot_topics)}个热点)'
        else:
            _quick_alert("动态热点识别无结果，事件驱动得分将为0")
            results['hot_topics'] = 'WARN: 无热点'
    except Exception as e:
        dynamic_hot_topics = []
        results['hot_topics'] = f'FAILED: {str(e)[:40]}'
        _quick_alert(f"动态热点识别失败: {str(e)[:80]}")
        logger.warning(f"动态热点识别跳过: {e}")

    # ==================================================================
    # Step 1: 同步行情数据（含重试 + 同步后质量检查）
    # ==================================================================
    if not args.skip_sync:
        step_banner(1, total_steps, "同步日线行情数据")
        
        # 判断是否需要全量更新基本面（一周一次：周六或强制参数）
        today_weekday = datetime.now().weekday()  # 0=周一, 5=周六, 6=周日
        full_fundamental = args.full_fundamental or (today_weekday == 5)  # 每周六全量更新
        if full_fundamental:
            print("[INFO] 今天是周六/强制全量更新，将更新全部基本面数据")
        
        try:
            from src.collector.data_loader import DataLoader
            loader = DataLoader()
            sync_result = loader.sync_daily_data(full_market=True, full_fundamental=full_fundamental)
            loader.close()
            if isinstance(sync_result, dict) and sync_result.get("success"):
                n = sync_result.get("total_inserted", 0)
                ok_count = sync_result.get("success_count", 0)
                results['sync_daily'] = f'OK ({ok_count}只 {n}条)'
                print(f"[OK] 行情数据同步完成（全市场）: {ok_count} 只股票 {n} 条记录")

                # 数据质量门控：检查数据库最新交易日实际股票数（而非本次同步数）
                # 8:30 cron 运行时市场未开盘，sync 可能无新数据，但不代表数据不完整
                MIN_STOCK_COUNT = 3000
                try:
                    _cnt_df = DBUtils.query_df(
                        "SELECT COUNT(*) AS cnt FROM stock_daily "
                        "WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)"
                    )
                    _actual_cnt = int(_cnt_df.iloc[0]['cnt']) if not _cnt_df.empty else 0
                except Exception:
                    _actual_cnt = 0
                if _actual_cnt < MIN_STOCK_COUNT and _actual_cnt > 0:
                    msg = (f"数据质量门控：最新日仅 {_actual_cnt} 只股票（阈值 {MIN_STOCK_COUNT}），"
                           f"数据严重残缺，中止流水线避免生成垃圾选股结果")
                    print(f"[ABORT] {msg}")
                    _quick_alert(msg)
                    results['abort_reason'] = msg
                    return False
                elif _actual_cnt == 0:
                    print(f"[WARN] 无法获取最新日股票数，使用同步结果校验: {ok_count} 只")
                    if ok_count < MIN_STOCK_COUNT:
                        msg = (f"数据质量门控：同步到 {ok_count} 只股票（阈值 {MIN_STOCK_COUNT}），"
                               f"数据严重残缺，中止流水线")
                        print(f"[ABORT] {msg}")
                        _quick_alert(msg)
                        results['abort_reason'] = msg
                        return False

                # 同步后做数据质量检查，不合格则写入 data_warnings
                try:
                    from src.utils.data_quality_monitor import DataQualityMonitor
                    monitor = DataQualityMonitor()
                    quality_report = monitor.check_latest_data_quality()
                    if quality_report and quality_report.get("has_data"):
                        score = quality_report.get("quality_score", 0)
                        if not quality_report.get("is_acceptable", True):
                            data_warnings.append(f"数据质量偏低: 评分 {score:.0f}/100，建议检查或全量同步")
                            logger.warning(f"数据质量评分 {score:.0f}/100，未达可接受标准")
                        else:
                            print(f"[OK] 数据质量评分 {score:.0f}/100")
                    elif quality_report and not quality_report.get("has_data"):
                        data_warnings.append("同步后最新日无数据，请检查数据源")
                except Exception as qe:
                    data_warnings.append(f"数据质量检查失败: {str(qe)[:50]}")
            else:
                err = (sync_result or {}).get("error") or "未知错误"
                results['sync_daily'] = f'FAILED: {err[:60]}'
                data_warnings.append(f"行情同步失败: {err[:80]}")
                _quick_alert(f"行情同步失败: {err[:80]}")
                logger.error(f"行情同步失败: {err}", exc_info=True)
        except Exception as e:
            results['sync_daily'] = f'FAILED: {str(e)[:60]}'
            data_warnings.append(f"行情同步失败: {str(e)[:80]}")
            _quick_alert(f"行情同步失败: {str(e)[:80]}")
            logger.error(f"行情同步失败: {e}", exc_info=True)
    else:
        step_banner(1, total_steps, "同步日线行情数据 [SKIPPED]")
        results['sync_daily'] = 'SKIPPED'
        print("[SKIP] 已跳过行情同步")

    # ==================================================================
    # Step 1b: 持仓同步 → stock_pool core_holding（每日自动标记）
    # ==================================================================
    try:
        from src.universe.stock_pool import StockPool
        sync_res = StockPool().sync_positions_to_pool()
        results['sync_positions'] = (
            f"OK (升级{sync_res['upgraded']} 新增{sync_res['added']} 降级{sync_res['downgraded']})"
        )
    except Exception as e:
        results['sync_positions'] = f'WARN: {str(e)[:60]}'
        logger.warning(f"持仓同步跳过: {e}")

    # ==================================================================
    # Step 1c: ROE / netprofit_yoy 修补（同步后立即更新最新行）
    # ==================================================================
    if not args.skip_sync:
        try:
            from scripts.patch_roe import run_patch as patch_roe
            patch_result = patch_roe(source="auto", dry_run=False)
            results['patch_roe'] = (
                f"OK (updated={patch_result['updated']} "
                f"no_data={patch_result['no_data']})"
            )
        except Exception as e:
            results['patch_roe'] = f'WARN: {str(e)[:60]}'
            logger.warning(f"ROE补丁跳过: {e}")

    # ==================================================================
    # Step 1d: 计算技术因子（AlphaEngine → stock_factors 表）
    # 在数据同步 + ROE 修补之后运行，保证因子用最新干净数据
    # ==================================================================
    if not args.skip_sync:
        step_banner(1, total_steps, "计算技术因子")
        try:
            from src.factors.alpha_engine import AlphaEngine
            engine = AlphaEngine()
            engine.update_factors()
            latest_factor_date = engine.get_latest_factor_date()
            results['factors'] = f"OK ({latest_factor_date})"
            print(f"[OK] 因子计算完成，最新日期: {latest_factor_date}")
        except Exception as e:
            results['factors'] = f'WARN: {str(e)[:60]}'
            logger.warning(f"因子计算跳过: {e}")
    else:
        step_banner(1, total_steps, "计算技术因子 [SKIPPED — 跳过数据同步]")
        results['factors'] = 'SKIPPED'

    # ==================================================================
    # 数据质量门控：最新交易日股票数 < 3000 时跳过策略，发送告警
    # ==================================================================
    _DATA_QUALITY_MIN_STOCKS = 3000
    _skip_strategy = False
    try:
        _dq = DBUtils.query_df(
            "SELECT COUNT(*) AS cnt FROM stock_daily "
            "WHERE trade_date = (SELECT MAX(trade_date) FROM stock_daily)"
        )
        _latest_cnt = int(_dq.iloc[0]['cnt']) if not _dq.empty else 0
        if _latest_cnt < _DATA_QUALITY_MIN_STOCKS:
            msg = (f"[质量门控] 最新交易日股票数 {_latest_cnt} < {_DATA_QUALITY_MIN_STOCKS}，"
                   f"数据不完整，跳过策略步骤")
            print(msg)
            data_warnings.append(msg)
            _quick_alert(msg)
            results['data_quality'] = f'WARN: 股票数={_latest_cnt}'
            _skip_strategy = True
        else:
            results['data_quality'] = f'OK ({_latest_cnt}只)'
            print(f"[质量门控] 数据完整: {_latest_cnt} 只股票")
    except Exception as _dqe:
        logger.warning(f"数据质量门控检查失败（不影响主流程）: {_dqe}")

    # ==================================================================
    # Step 2: 同步概念/题材
    # ==================================================================
    if not args.skip_sync and not args.skip_concepts:
        step_banner(2, total_steps, "同步概念/题材数据")
        try:
            from src.collector.data_loader import DataLoader
            loader = DataLoader()
            success = loader.sync_concepts()
            loader.close()
            results['sync_concepts'] = 'OK' if success else 'WARN: No data'
            print("[OK] 概念数据同步完成" if success else "[WARN] 概念同步未获取到数据")
        except Exception as e:
            results['sync_concepts'] = f'FAILED: {e}'
            data_warnings.append(f"概念同步失败: {str(e)[:80]}")
            logger.error(f"概念同步失败: {e}", exc_info=True)
    else:
        step_banner(2, total_steps, "同步概念/题材数据 [SKIPPED]")
        results['sync_concepts'] = 'SKIPPED'
        print("[SKIP] 已跳过概念同步")

    # ==================================================================
    # Step 3: LightGBM AI 训练 & 预测 (Direct-SQLite)
    # ==================================================================
    if _skip_strategy:
        step_banner(3, total_steps, "LightGBM AI [跳过 — 数据质量门控]")
        results['ai_model'] = 'SKIPPED (数据质量不足)'
    elif not args.skip_qlib:
        step_banner(3, total_steps, "LightGBM AI 训练 & 预测")
        try:
            from scripts.run_ai_model import main as run_ai_model
            success = run_ai_model()
            results['ai_model'] = 'OK' if success else 'FAILED'
            print("[OK] LightGBM AI 流程完成" if success else "[WARN] AI 流程未完成")
        except ImportError as ie:
            results['ai_model'] = 'SKIPPED (missing dependency)'
            logger.warning(f"缺少依赖, 跳过 AI 训练: {ie}")
            print("       安装方法: pip install lightgbm scikit-learn")
        except Exception as e:
            results['ai_model'] = f'FAILED: {e}'
            data_warnings.append(f"AI模型失败: {str(e)[:80]}")
            logger.error(f"LightGBM AI 流程失败: {e}", exc_info=True)
    else:
        step_banner(3, total_steps, "LightGBM AI 训练 & 预测 [SKIPPED]")
        results['ai_model'] = 'SKIPPED'
        print("[SKIP] 已跳过 AI 训练")

    # ==================================================================
    # Step 4: 混合策略选股（含新闻动态热点加成）
    # ==================================================================
    # 数据质量门控：数据不足时强制 result_df=None，后续 if result_df is not None 自然跳过
    if _skip_strategy:
        result_df = None
        results['hybrid'] = 'SKIPPED (数据质量不足)'
        step_banner(4, total_steps, "混合策略选股 [SKIPPED — 数据质量门控]")
    if not _skip_strategy:
        step_banner(4, total_steps, "混合策略选股")
    try:
        # 先运行市场研究流水线：期货/政策新闻/北向/龙虎榜/热点/行业 → 信号表
        try:
            from src.analysis.research_runner import ResearchRunner
            print("[INFO] 运行市场研究流水线...")
            rr = ResearchRunner(trade_date=today_date)
            research_results = rr.run_all()
            print(f"[INFO] 市场研究完成: {list(research_results.keys())}")
        except Exception as ex:
            print(f"[WARN] 市场研究失败 (继续执行): {ex}")

        from src.strategy.hybrid_strategy import HybridStrategy

        # 情绪择时：根据新闻分析 action 缩放选股数量
        # full_liquidate=0只  reduce_major=30%  reduce_minor=60%  hold/add=正常
        scaled_topk = max(0, int(args.top_k * news_position_scale))
        if scaled_topk == 0:
            logger.warning(f"新闻分析建议清仓(action=full_liquidate)，跳过选股，仅推送风险预警")
            result_df = None
            results['hybrid'] = 'SKIPPED (full_liquidate by news)'
        else:
            if scaled_topk < args.top_k:
                print(f"[INFO] 新闻情绪择时：top_k {args.top_k} → {scaled_topk} "
                      f"(scale={news_position_scale:.1f})")

            # 传入动态热点（自动识别），无需手动维护列表
            strategy = HybridStrategy(
                hot_topics=dynamic_hot_topics if dynamic_hot_topics else None
            )
            # mode='dual'：行业轮动(A轨) + 价值质量(B轨) 各出一半后合并
            result_df = strategy.run(
                top_k=scaled_topk,
                news_boost_sectors=news_boost_sectors or [],
                mode='dual',
            )

        if result_df is not None and not result_df.empty:
            results['hybrid'] = f'OK ({len(result_df)} stocks)'
            print(f"\n[OK] 混合策略选出 {len(result_df)} 只股票")

            # 保存选股结果到 CSV (方便查看)
            output_dir = os.path.join(os.path.dirname(__file__), '..', 'output')
            os.makedirs(output_dir, exist_ok=True)
            csv_path = os.path.join(output_dir, f'hybrid_picks_{today_str}.csv')
            result_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            print(f"[OK] 选股结果已保存至: {csv_path}")

            # 保存多策略缓存（供选股中心快速读取）
            try:
                import json
                cache_path = os.path.join(output_dir, f'multi_strategy_{today_str}.csv'.replace('.csv', '.json'))
                cache_data = {
                    "success": True,
                    "strategies_run": ["hybrid"],
                    "ensemble": False,
                    "picks": result_df.fillna('').to_dict('records'),
                    "total": len(result_df),
                    "date": today_date,
                    "generated_at": datetime.now().isoformat(),
                }
                with open(cache_path, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, ensure_ascii=False, indent=2)
                print(f"[OK] 多策略缓存已保存至: {cache_path}")
            except Exception as e:
                print(f"[WARN] 多策略缓存保存失败: {e}")

            # 同步写入数据库 daily_picks 表
            try:
                from src.utils.db_utils import DBUtils
                with DBUtils.get_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS daily_picks (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            trade_date VARCHAR(8) NOT NULL,
                            ts_code VARCHAR(20) NOT NULL,
                            name VARCHAR(100),
                            final_score FLOAT,
                            ai_score FLOAT,
                            event_score FLOAT,
                            fund_score FLOAT,
                            track VARCHAR(20),
                            concept TEXT,
                            industry VARCHAR(100),
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            UNIQUE KEY uniq_date_code (trade_date, ts_code)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """)
                    # 删除今日旧数据后重新插入
                    cursor.execute("DELETE FROM daily_picks WHERE trade_date = ?", (today_str,))
                    def _sf(v):
                        """安全转 float，nan/None/''/inf → None"""
                        import math
                        if v is None or v == '':
                            return None
                        try:
                            f = float(v)
                            return None if (math.isnan(f) or math.isinf(f)) else f
                        except (TypeError, ValueError):
                            return None

                    for _, row in result_df.iterrows():
                        cursor.execute("""
                            INSERT INTO daily_picks
                                (trade_date, ts_code, name, final_score, ai_score,
                                 event_score, fund_score, track, concept, industry)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            today_str,
                            str(row.get('ts_code', '')),
                            str(row.get('name', '')),
                            _sf(row.get('final_score')),
                            _sf(row.get('ai_score')),
                            _sf(row.get('event_score')),
                            _sf(row.get('fund_score', row.get('fundamental_score'))),
                            str(row.get('track', '')),
                            str(row.get('concept', row.get('concepts', row.get('concept_name', '')))),
                            str(row.get('industry', '')),
                        ))
                    conn.commit()
                print(f"[OK] 选股结果已写入数据库 daily_picks 表 ({len(result_df)} 条)")
            except Exception as e:
                logger.warning(f"写入 daily_picks 表失败（不影响主流程）: {e}")
        else:
            results['hybrid'] = 'WARN: No results'
            print("[WARN] 混合策略未返回结果")
    except Exception as e:
        results['hybrid'] = f'FAILED: {e}'
        data_warnings.append(f"混合选股失败: {str(e)[:80]}")
        _quick_alert(f"混合选股失败，今日无推荐: {str(e)[:100]}")
        logger.error(f"混合策略执行失败: {e}", exc_info=True)
        import traceback
        traceback.print_exc()

    # ==================================================================
    # Step 4b: 价值轨道已并入 HybridStrategy dual 模式，此处从结果中拆分
    # ==================================================================
    value_result_df = None
    try:
        # result_df 在 Step 4 中定义
        if result_df is not None and not result_df.empty and 'track' in result_df.columns:
            value_result_df = result_df[result_df['track'].isin(['value', 'dividend', 'both'])].copy()
            if not value_result_df.empty:
                results['value_picks'] = f'OK ({len(value_result_df)} stocks, 来自双轨B轨)'
                value_csv = os.path.join(
                    output_dir if 'output_dir' in dir() else
                    os.path.join(os.path.dirname(__file__), '..', 'output'),
                    f'value_picks_{datetime.now().strftime("%Y%m%d")}.csv')
                os.makedirs(os.path.dirname(value_csv), exist_ok=True)
                value_result_df.to_csv(value_csv, index=False, encoding='utf-8-sig')
                print(f"[OK] 价值轨道结果已保存: {value_csv}")
            else:
                results['value_picks'] = 'WARN: 双轨B轨无结果'
    except NameError:
        results['value_picks'] = 'SKIPPED: result_df 未定义'
    except Exception as e:
        results['value_picks'] = f'FAILED: {str(e)[:60]}'

    # ==================================================================
    # Step 4c: 新策略整合（三轨并行）★ PB-ROA / 可转债 / 指数增强
    # ==================================================================
    # 确保 output_dir 和 today_str 已定义（即使混合策略失败）
    output_dir = os.path.join(os.path.dirname(__file__), '..', 'output')
    os.makedirs(output_dir, exist_ok=True)
    
    new_strategy_results = {}
    step_banner(4, total_steps, "新策略整合")
    
    def _convert_new_strategy_to_daily_picks(strategy_df, track_name, strategy_name):
        """将新策略结果转换为 daily_picks 表格式"""
        if strategy_df is None or strategy_df.empty:
            return None
        df = strategy_df.copy()
        # 统一列名
        df['track'] = track_name
        df['ai_score'] = None
        df['event_score'] = None
        df['fund_score'] = df['score']
        df['concept'] = strategy_name
        # 确保有 ai_score/event_score 列（daily_picks 表结构）
        return df
    
    def _append_to_daily_picks(strategy_df, track_name, today_str):
        """将策略结果追加到 daily_picks 表"""
        if strategy_df is None or strategy_df.empty:
            return 0
        
        from src.utils.db_utils import DBUtils
        
        def _sf(v):
            import math
            if v is None or v == '':
                return None
            try:
                f = float(v)
                return None if (math.isnan(f) or math.isinf(f)) else f
            except (TypeError, ValueError):
                return None
        
        count = 0
        try:
            with DBUtils.get_conn() as conn:
                for _, row in strategy_df.iterrows():
                    try:
                        cursor = conn.cursor()
                        cursor.execute("""
                            INSERT INTO daily_picks
                                (trade_date, ts_code, name, final_score, ai_score,
                                 event_score, fund_score, track, concept, industry)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON DUPLICATE KEY UPDATE
                                final_score = VALUES(final_score),
                                fund_score = VALUES(fund_score),
                                track = VALUES(track),
                                concept = VALUES(concept)
                        """, (
                            today_str,
                            str(row.get('ts_code', '')),
                            str(row.get('name', '')),
                            _sf(row.get('score', row.get('final_score'))),
                            _sf(row.get('ai_score')),
                            _sf(row.get('event_score')),
                            _sf(row.get('fund_score')),
                            track_name,
                            str(row.get('signal_reason', ''))[:200],
                            str(row.get('industry', '')),
                        ))
                        count += 1
                    except Exception as e:
                        pass  # 忽略单条插入失败
                conn.commit()
        except Exception as e:
            logger.warning(f"[{track_name}] 写入 daily_picks 失败: {e}")
        return count
    
    # PB-ROA 策略
    try:
        from src.strategy.pb_roa_strategy import PbRoaStrategy
        pb_roa = PbRoaStrategy()
        pb_roa_df = pb_roa.run(top_k=10)
        if pb_roa_df is not None and not pb_roa_df.empty:
            pb_roa_converted = _convert_new_strategy_to_daily_picks(pb_roa_df, 'pb_roa', 'PB-ROA')
            count = _append_to_daily_picks(pb_roa_converted, 'pb_roa', today_str)
            new_strategy_results['pb_roa'] = f'OK ({count}只)'
            # 保存独立 CSV
            pb_csv = os.path.join(output_dir, f'pb_roa_picks_{today_str}.csv')
            pb_roa_df.to_csv(pb_csv, index=False, encoding='utf-8-sig')
            print(f"[OK] PB-ROA策略: {count}只 → {pb_csv}")
        else:
            new_strategy_results['pb_roa'] = 'WARN: 无结果'
    except Exception as e:
        new_strategy_results['pb_roa'] = f'FAILED: {str(e)[:60]}'
        logger.warning(f"[PB-ROA] 策略执行失败: {e}")
    
    # 可转债策略
    try:
        from src.strategy.convertible_bond_strategy import ConvertibleBondStrategy
        cb_strategy = ConvertibleBondStrategy()
        cb_df = cb_strategy.run(top_k=10)
        if cb_df is not None and not cb_df.empty:
            cb_converted = _convert_new_strategy_to_daily_picks(cb_df, 'convertible_bond', '可转债')
            count = _append_to_daily_picks(cb_converted, 'convertible_bond', today_str)
            new_strategy_results['convertible_bond'] = f'OK ({count}只)'
            # 保存独立 CSV
            cb_csv = os.path.join(output_dir, f'cb_picks_{today_str}.csv')
            cb_df.to_csv(cb_csv, index=False, encoding='utf-8-sig')
            print(f"[OK] 可转债策略: {count}只 → {cb_csv}")
        else:
            new_strategy_results['convertible_bond'] = 'WARN: 无结果'
    except Exception as e:
        new_strategy_results['convertible_bond'] = f'FAILED: {str(e)[:60]}'
        logger.warning(f"[可转债] 策略执行失败: {e}")
    
    # 指数增强策略
    try:
        from src.strategy.index_enhance_strategy import IndexEnhanceStrategy
        index_enh = IndexEnhanceStrategy()
        index_df = index_enh.run(top_k=10)
        if index_df is not None and not index_df.empty:
            index_converted = _convert_new_strategy_to_daily_picks(index_df, 'index_enhance', '指数增强')
            count = _append_to_daily_picks(index_converted, 'index_enhance', today_str)
            new_strategy_results['index_enhance'] = f'OK ({count}只)'
            # 保存独立 CSV
            index_csv = os.path.join(output_dir, f'index_enhance_picks_{today_str}.csv')
            index_df.to_csv(index_csv, index=False, encoding='utf-8-sig')
            print(f"[OK] 指数增强策略: {count}只 → {index_csv}")
        else:
            new_strategy_results['index_enhance'] = 'WARN: 无结果'
    except Exception as e:
        new_strategy_results['index_enhance'] = f'FAILED: {str(e)[:60]}'
        logger.warning(f"[指数增强] 策略执行失败: {e}")
    
    # ETF 统一策略（仅生成 CSV，不写入 daily_picks）
    try:
        from src.strategy.etf_unified_strategy import ETFUnifiedStrategy
        etf_strat = ETFUnifiedStrategy()
        etf_df = etf_strat.run(top_n=10)
        if etf_df is not None and not etf_df.empty:
            etf_csv = os.path.join(output_dir, f'etf_picks_{today_str}.csv')
            etf_df.to_csv(etf_csv, index=False, encoding='utf-8-sig')
            new_strategy_results['etf'] = f'OK ({len(etf_df)}只)'
            print(f"[OK] ETF策略: {len(etf_df)}只 → {etf_csv}")
        else:
            new_strategy_results['etf'] = 'WARN: 无结果'
    except Exception as e:
        new_strategy_results['etf'] = f'FAILED: {str(e)[:60]}'
        logger.warning(f"[ETF] 策略执行失败: {e}")
    
    results['new_strategies'] = str(new_strategy_results)

    # ==================================================================
    # 选股绩效跟踪：记录本次入选股票（供后续 N 日收益率跟踪）
    # ==================================================================
    try:
        from src.backtest.performance_tracker import PerformanceTracker
        tracker_pt = PerformanceTracker()
        recorded = 0

        # 1) 混合策略双轨结果
        if result_df is not None and not result_df.empty:
            hybrid_records = result_df.to_dict('records')
            for r in hybrid_records:
                r['sector_momentum_score'] = r.get('sector_momentum_score', 0)
                r['layer_heat_score'] = r.get('layer_heat_score', 0)
            tracker_pt.record_picks(hybrid_records, today_date)
            recorded += len(hybrid_records)

        # 2) PB-ROA / 可转债 / 指数增强（各自只有 score/fund_score）
        for strat_name in ['pb_roa_df', 'cb_df', 'index_df']:
            strat_df = locals().get(strat_name)
            if strat_df is not None and not strat_df.empty:
                strat_records = strat_df.rename(columns={
                    'score': 'final_score',
                    'fund_score': 'fundamental_score',
                }).to_dict('records')
                for r in strat_records:
                    r['sector_momentum_score'] = 0
                    r['layer_heat_score'] = 0
                tracker_pt.record_picks(strat_records, today_date)
                recorded += len(strat_records)

        results['performance_tracker'] = f'OK ({recorded} picks recorded)'
        print(f"[OK] 选股绩效跟踪: 已记录 {recorded} 只入选股票")
    except Exception as e:
        results['performance_tracker'] = f'WARN: {str(e)[:60]}'
        logger.warning(f"[PerformanceTracker] 记录选股失败: {e}")

    # ==================================================================
    # Step 4d: 股票池买入信号扫描（PoolStrategy）
    # ==================================================================
    pool_result = None
    step_banner(4, total_steps, "股票池买入信号扫描")
    try:
        from src.strategy.pool_strategy import PoolStrategy
        from src.valuation.valuation_engine import ValuationEngine

        # 先更新估值（今日数据同步完成后）
        ve = ValuationEngine()
        ve.update_pool()

        ps = PoolStrategy()
        pool_result = ps.run(update_valuation=False)  # 估值已更新，跳过重复执行
        sig_count = len(pool_result["signals"]) if pool_result else 0
        app_count = len(pool_result["approaching"]) if pool_result else 0
        results['pool_scan'] = f'OK (信号{sig_count}只, 接近{app_count}只)'
        print(f"[OK] 股票池扫描完成: {pool_result['summary']}")

        # 有买入信号时自动触发个股深度分析（最多5只，避免大量信号拖慢流水线）
        MAX_AUTO_ANALYZE = 5
        if sig_count > 0:
            print(f"[INFO] 检测到 {sig_count} 只买入信号，自动触发深度分析（最多{MAX_AUTO_ANALYZE}只）...")
            try:
                from src.analysis.stock_analyzer import StockAnalyzer
                analyzer = StockAnalyzer()
                signals_df = pool_result["signals"].head(MAX_AUTO_ANALYZE)
                for _, row in signals_df.iterrows():
                    report = analyzer.analyze(row["ts_code"], trigger="buy_signal")
                    report_md = analyzer.format_report_for_dingtalk(report)
                    results[f'analysis_{row["ts_code"]}'] = report["action"]
                    # 报告追加到通知队列（在 Step 6 推送）
                    data_warnings.append(f"__ANALYSIS_REPORT__{report_md}")
            except Exception as e:
                logger.warning(f"深度分析失败（不影响主流程）: {e}")
    except Exception as e:
        results['pool_scan'] = f'FAILED: {e}'
        logger.warning(f"股票池扫描失败（不影响主流程）: {e}")

    # ==================================================================
    # Step 4d: 估值异动 & profit_warnings 新增推送
    # ==================================================================
    try:
        from src.utils.alert_monitor import run_all_alerts
        alert_res = run_all_alerts()
        results['alert_monitor'] = (
            f"OK (估值异动{alert_res.get('valuation_changes',0)}只 "
            f"新增预警{alert_res.get('new_warnings',0)}条)"
        )
    except Exception as e:
        results['alert_monitor'] = f'WARN: {str(e)[:60]}'
        logger.warning(f"异动监控跳过: {e}")

    # ==================================================================
    # Step 4e: 持仓稳定性管理（HoldingManager）
    # ==================================================================
    hm_decision = None
    try:
        from src.portfolio.holding_manager import HoldingManager
        hm = HoldingManager()
        # 传递上期推荐列表，让 HoldingManager 识别近期常客
        prev_picks_set = set()
        if result_df is not None and not result_df.empty:
            prev_picks_set = set(result_df['ts_code'].astype(str).str.strip())
        hm_decision = hm.decide(result_df, trade_date=today_date, prev_picks=prev_picks_set)
        summary = hm_decision.summary
        print(f"\n[持仓管理] 买入{summary['buy']}只  卖出{summary['sell']}只  "
              f"持有{summary['hold']}只  强制止损{summary['forced_sell']}只")
        if hm_decision.buy_list:
            print("  买入: " + ", ".join(
                f"{s.get('name','?')}({s.get('ts_code','')})" for s in hm_decision.buy_list[:5]
            ))
        if hm_decision.sell_list:
            print("  卖出: " + ", ".join(
                f"{s.get('name','?')}({s.get('ts_code','')})[{s.get('reason','')}]"
                for s in hm_decision.sell_list[:5]
            ))
        results['holding_manager'] = (
            f"OK (买{summary['buy']} 卖{summary['sell']} 持{summary['hold']} 止损{summary['forced_sell']})"
        )
    except Exception as e:
        results['holding_manager'] = f'WARN: {str(e)[:60]}'
        logger.warning(f"[HoldingManager] 跳过（不影响主流程）: {e}", exc_info=True)

    # ==================================================================
    # Step 5: 自动交易
    # ==================================================================
    total_steps = 6
    step_banner(5, total_steps, "自动交易")
    try:
        from src.trading.auto_trader import AutoTrader
        auto_trader = AutoTrader()

        # 若 HoldingManager 给出了决策，用结构化决策驱动交易；否则降级用原始选股结果
        if hm_decision is not None:
            # 将 HoldingManager 决策转成 AutoTrader 期望的 result_df 格式
            # buy_list 为新建仓，sell_list 为平仓，hold_list 维持不动
            all_codes = (
                {s['ts_code'] for s in hm_decision.buy_list}
                | {s['ts_code'] for s in hm_decision.hold_list}
            )
            if result_df is not None and not result_df.empty:
                trade_df = result_df[result_df['ts_code'].isin(all_codes)].copy()
            else:
                trade_df = pd.DataFrame(hm_decision.buy_list)
            trade_results = auto_trader.auto_trade_stocks(trade_df)
        else:
            trade_results = auto_trader.auto_trade_stocks(result_df)

        results['auto_trade'] = f'OK (买入{trade_results["buy_count"]}只, 卖出{trade_results["sell_count"]}只)'
        print(f"[OK] 自动交易完成: 买入{trade_results['buy_count']}只, 卖出{trade_results['sell_count']}只, 持有{trade_results['hold_count']}只")

        if trade_results['buy_list']:
            print("\n买入列表:")
            for item in trade_results['buy_list'][:5]:
                print(f"  + {item['name']}({item['ts_code']}) 评分: {item['score']:.2f}")
        if trade_results['sell_list']:
            print("\n卖出列表:")
            for item in trade_results['sell_list'][:5]:
                print(f"  - {item['name']}({item['ts_code']}) 原因: {item['reason']}")

    except Exception as e:
        results['auto_trade'] = f'FAILED: {e}'
        data_warnings.append(f"自动交易失败: {str(e)[:80]}")
        logger.error(f"自动交易执行失败: {e}", exc_info=True)

    # ==================================================================
    # 汇总报告
    # ==================================================================
    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    print(f"\n{'=' * 60}")
    print(f"  Daily Alpha Run - 执行报告")
    print(f"  总耗时: {minutes}分{seconds}秒")
    print(f"{'=' * 60}")
    print(f"  Step 0 - 新闻分析:   {results.get('news_analysis', 'N/A')}")
    print(f"  Step 1 - 行情同步:   {results.get('sync_daily', 'N/A')}")
    print(f"  Step 1d- 因子计算:   {results.get('factors', 'N/A')}")
    print(f"  Step 2 - 概念同步:   {results.get('sync_concepts', 'N/A')}")
    print(f"  Step 3 - AI模型:     {results.get('ai_model', 'N/A')}")
    print(f"  Step 4 - 混合选股:   {results.get('hybrid', 'N/A')}")
    print(f"  Step 4e- 持仓管理:   {results.get('holding_manager', 'N/A')}")
    print(f"  Step 5 - 自动交易:   {results.get('auto_trade', 'N/A')}")
    print(f"{'=' * 60}")

    # 判断整体是否成功
    has_failure = any('FAILED' in str(v) for v in results.values())
    if has_failure:
        print("\n[WARN] 部分步骤执行失败, 请检查上方日志")
        success_status = False
    else:
        print("\n[DONE] 流水线执行完成!")
        success_status = True
    
    # ==================================================================
    # 行业择机（科技按渗透率、成熟按周期），供日报展示
    # ==================================================================
    industry_timing_data = None
    try:
        from src.analysis.industry_timing import IndustryTiming
        timing = IndustryTiming()
        industry_timing_data = timing.run_split(max_industries=40, emerging_top=6, mature_top=6)
        industry_timing_data["lookback_days"] = timing.lookback_days
    except Exception as e:
        data_warnings.append(f"行业择机失败: {str(e)[:80]}")
        logger.warning(f"行业择机跳过: {e}")

    # 主题ETF挑选（与行业择机联动，供日报展示）
    etf_selector_result = None
    if industry_timing_data:
        try:
            from src.analysis.etf_selector import run_with_industry_timing
            etf_selector_result = run_with_industry_timing(industry_timing_data, top_per_industry=2)
            if etf_selector_result.get("error"):
                logger.warning(f"ETF挑选: {etf_selector_result['error']}")
            # DeepSeek 作为数据源：生成 ETF 买卖/选择建议（可结合模型知识或联网信息）
            from src.utils.config_loader import Config
            if (Config.get("etf_selector") or {}).get("use_llm_advice") and etf_selector_result and etf_selector_result.get("by_industry"):
                try:
                    from src.utils.llm_client import LLMClient
                    client = LLMClient()
                    if client.is_available():
                        cycle_cn = {"early": "早周期", "mid": "中周期", "late": "晚周期", "defensive": "防御"}.get(industry_timing_data.get("current_cycle", ""), "中周期")
                        bench = industry_timing_data.get("benchmark_return_pct", 0)
                        summary_parts = [f"当前周期 {cycle_cn}，基准近段收益 {bench:.1f}%。"]
                        edf = industry_timing_data.get("emerging")
                        if edf is not None and not edf.empty and "industry" in edf.columns:
                            summary_parts.append("新兴(渗透率): " + "、".join(edf["industry"].astype(str).tolist()[:6]))
                        mdf = industry_timing_data.get("mature")
                        if mdf is not None and not mdf.empty and "industry" in mdf.columns:
                            summary_parts.append("成熟(周期): " + "、".join(mdf["industry"].astype(str).tolist()[:6]))
                        industry_timing_summary = "\n".join(summary_parts)
                        hold_hint = (Config.get("etf_selector") or {}).get("holding_period_hint", "1周～3个月")
                        advice = client.generate_etf_advice(industry_timing_summary, etf_selector_result.get("by_industry"), hold_hint)
                        if advice:
                            etf_selector_result["llm_advice"] = advice
                            print("[OK] Grok ETF 建议已生成")
                except Exception as llm_e:
                    logger.warning(f"Grok ETF 建议跳过: {llm_e}")
        except Exception as e:
            logger.warning(f"ETF挑选跳过: {e}")

    # ==================================================================
    # 推荐标的追踪：记录买入/卖出/盈亏（每次选股后自动更新）
    # ==================================================================
    tracker_info = ""
    tracker_sell_enriched = []  # 带盈亏的卖出列表
    try:
        from src.portfolio.recommendation_tracker import RecommendationTracker
        tracker = RecommendationTracker()
        track_result = tracker.update(result_df, today_str=today_date)
        print(f"[OK] 推荐追踪更新: 新入{track_result['new_entries']} 持有{track_result['still_holding']} 调出{track_result['newly_sold']}")
        # 生成钉钉日报用的追踪文本
        tracker_info = tracker.format_for_dingtalk()
        # 调出标的带上盈亏信息
        tracker_sell_enriched = track_result.get('newly_sold_list', [])
        # 统计胜率
        stats = tracker.get_stats()
        if stats.get('total_sold', 0) > 0:
            print(f"[INFO] 历史统计: 已调出{stats['total_sold']}只 胜率{stats['win_rate']}% 平均盈亏{stats['avg_profit']}% 平均持仓{stats['avg_holding_days']}天")
    except Exception as e:
        logger.warning(f"推荐追踪更新跳过: {e}")
        import traceback
        traceback.print_exc()

    # ==================================================================
    # 建议卖出：上期推荐、本期已掉出名单的标的（看跌/调出）
    # ==================================================================
    sell_list = []
    try:
        current_set = set(result_df['ts_code'].astype(str).str.strip()) if result_df is not None and not result_df.empty else set()
        sell_list = _get_previous_picks_sell_list(output_dir, today_str, current_set, top_k_keep=args.top_k)
    except Exception as e:
        logger.warning(f"建议卖出列表获取跳过: {e}")

    # 合并追踪器的调出信息到 sell_list（补充盈亏数据）
    if tracker_sell_enriched:
        enriched_codes = {item['ts_code'] for item in tracker_sell_enriched}
        # 用追踪器的数据覆盖旧 sell_list 中同代码的条目
        new_sell_list = []
        for item in sell_list:
            code = item.get('ts_code', '')
            if code in enriched_codes:
                # 找到追踪器中的详细信息
                for t_item in tracker_sell_enriched:
                    if t_item['ts_code'] == code:
                        item.update({
                            'profit_pct': t_item.get('profit_pct'),
                            'holding_days': t_item.get('holding_days'),
                            'buy_date': t_item.get('buy_date'),
                            'sell_date': t_item.get('sell_date'),
                        })
                        break
            new_sell_list.append(item)
        # 追踪器有但 sell_list 没有的也加上
        for t_item in tracker_sell_enriched:
            if t_item['ts_code'] not in {s.get('ts_code') for s in new_sell_list}:
                new_sell_list.append(t_item)
        sell_list = new_sell_list

    # ==================================================================
    # 止损预警推送（独立于日报，即时性更强）
    # ==================================================================
    if not args.skip_notification:
        try:
            from src.utils.db_utils import DBUtils
            from src.utils.notifier import send_alert
            stop_df = DBUtils.query_df(
                """SELECT p.ts_code, p.name, p.current_price, p.stop_loss_price,
                          p.profit_loss_pct, p.avg_cost
                   FROM positions p
                   WHERE p.shares > 0
                     AND p.stop_loss_price > 0
                     AND p.current_price <= p.stop_loss_price * 1.02"""
            )
            if not stop_df.empty:
                lines = []
                for _, r in stop_df.iterrows():
                    pct = float(r.get('profit_loss_pct') or 0) * 100  # 转为百分比
                    sign = '+' if pct >= 0 else ''
                    lines.append(
                        f"- **{r['name']}**（{str(r['ts_code'])[:6]}）"
                        f" 现价 ¥{float(r['current_price']):.2f}"
                        f" 止损 ¥{float(r['stop_loss_price']):.2f}"
                        f" 浮盈 {sign}{pct:.1f}%"
                    )
                content = "以下持仓已触及或接近止损价（2%以内），请关注：\n\n" + "\n".join(lines)
                send_alert("🔴 止损预警", content, message_type="stop_loss")
                print(f"[止损预警] 已推送 {len(stop_df)} 只接近止损")
        except Exception as e:
            logger.warning(f"止损预警推送跳过: {e}")

    # ==================================================================
    # 钉钉推送通知（日报）
    # ==================================================================
    if not args.skip_notification:
        # 提取并分离深度分析报告（避免混入 data_warnings 的 issue 过滤）
        analysis_reports = []
        clean_warnings = []
        for w in (data_warnings or []):
            if str(w).startswith('__ANALYSIS_REPORT__'):
                analysis_reports.append(str(w)[len('__ANALYSIS_REPORT__'):])
            else:
                clean_warnings.append(w)
        data_warnings = clean_warnings

        try:
            send_dingtalk_notification(
                results, result_df, minutes, seconds, success_status,
                industry_timing_data, data_warnings, sell_list,
                etf_selector_result, tracker_info=tracker_info,
                news_analysis=news_analysis,
                value_result_df=value_result_df
            )
        except Exception as e:
            logger.warning(f"钉钉推送失败: {e}")

        # 买入信号深度分析报告：每只单独推送（不挤进日报）
        if analysis_reports:
            from src.utils.notifier import send_alert
            for report_md in analysis_reports:
                try:
                    # 从报告里提取股票名作标题
                    first_line = report_md.strip().splitlines()[0] if report_md.strip() else ''
                    title = f"📊 深度分析 {first_line[:20]}" if first_line else "📊 买入信号深度分析"
                    send_alert(title, report_md, message_type="buy_signal_analysis")
                except Exception as e:
                    logger.warning(f"分析报告推送失败: {e}")

        # ETF 抄底反弹 —— 单独第二条钉钉消息
        try:
            from src.strategy.etf_bottom_fish_strategy import ETFBottomFishStrategy, format_dingtalk_message
            from src.utils.notifier import NotifierFactory
            from src.utils.config_loader import Config

            print("\n[ETF] 运行 ETF 抄底反弹策略...")
            etf_picks = ETFBottomFishStrategy().run(top_n=8)
            if etf_picks is not None and not etf_picks.empty:
                etf_msg = format_dingtalk_message(etf_picks)
                notification_config = Config.get('notification', {})
                dingtalk_config = notification_config.get('dingtalk', {})
                webhook_url = dingtalk_config.get('webhook')
                secret_word = dingtalk_config.get('secret_word', '提醒')
                if webhook_url:
                    etf_notifier = NotifierFactory.create_notifier(
                        'dingtalk', webhook_url=webhook_url, secret_word=secret_word
                    )
                    etf_notifier.send_message(
                        title=f"ETF抄底反弹雷达 {datetime.now().strftime('%m月%d日')}（提醒）",
                        content=etf_msg,
                    )
                    print("[OK] ETF抄底反弹钉钉推送成功")
            else:
                print("[INFO] ETF策略无信号，跳过推送")
        except Exception as e:
            logger.warning(f"ETF推送失败: {e}")
    else:
        print("\n[SKIP] 已跳过钉钉推送")
    
    return success_status


def run_weekly_pool_refresh_if_sunday():
    """每周日自动运行股票池刷新（集成到日常流水线）"""
    from datetime import datetime
    if datetime.now().weekday() != 6:   # 6 = 周日
        return
    try:
        print("\n[周日任务] 开始每周股票池刷新...")
        from scripts.weekly_pool_refresh import run_weekly_refresh
        run_weekly_refresh(dry_run=False, auto_add=True)
        print("[周日任务] 股票池刷新完成")
    except Exception as e:
        logger.warning(f"股票池周刷新失败: {e}")


if __name__ == '__main__':
    args = parse_args()
    success = run_pipeline(args)
    # 周日额外运行股票池刷新
    run_weekly_pool_refresh_if_sunday()
    sys.exit(0 if success else 1)
