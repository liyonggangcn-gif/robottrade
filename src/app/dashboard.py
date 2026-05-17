import streamlit as st
import pandas as pd
import numpy as np
import os
import sys
import time

# --- 1. 核心修复：强制清除代理 (解决 Error Captcha fails) ---
# 这段代码必须放在最前面，防止 VPN 导致的网络报错
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("all_proxy", None)

# 添加src目录到Python路径
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

# 导入核心模块
from src.collector.data_loader import DataLoader
from src.factors.alpha_engine import AlphaEngine
from src.strategy.topk_strategy import TopKStrategy
from src.strategy.small_cap_jinx import SmallCapJinxStrategy
from src.strategy.hybrid_strategy import HybridStrategy
from src.strategy.pb_roa_strategy import PbRoaStrategy
from src.strategy.index_enhance_strategy import IndexEnhanceStrategy
from src.backtest.backtest_engine import BacktestEngine
from src.utils.llm_client import LLMClient
from src.utils.config_loader import Config
from src.analysis.industry_timing import IndustryTiming

# 页面配置
st.set_page_config(
    page_title="QuantAgent 交易驾驶舱",
    page_icon="🚀",
    layout="wide"
)

# 侧边栏导航（整合原有导航和新功能）
st.sidebar.title("📊 导航菜单")
main_page = st.sidebar.selectbox(
    "主页面",
    ["交易驾驶舱", "策略中心", "ETF策略", "可转债", "数据源状态", "推送消息历史", "自动交易管理"],
    index=0
)

# --- 2. 核心功能函数 ---
def run_sync_task():
    """执行全量同步任务（含概念/题材同步）"""
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    try:
        status_text.text("🚀 正在初始化数据加载器...")
        loader = DataLoader()  # 即用即毁，防止断连
        
        status_text.text("📥 正在同步日线行情 (AkShare/Tushare)...")
        loader.sync_daily_data(limit=None) 
        progress_bar.progress(33)
        
        status_text.text("🔗 正在同步概念/题材数据 (Tushare)...")
        loader.sync_concepts()
        loader.close()
        progress_bar.progress(66)
        
        status_text.text("🧮 正在计算 Alpha 因子...")
        engine = AlphaEngine()
        engine.update_factors()
        engine.close()
        progress_bar.progress(100)
        
        status_text.text("✅ 更新完成！")
        st.success("数据已更新至最新！（含概念/题材）")
        time.sleep(1)
        st.rerun()
        
    except Exception as e:
        st.error(f"更新失败: {e}")
    finally:
        progress_bar.empty()

def generate_ai_comment(row):
    """生成AI点评"""
    # 修复：使用中文 Key 获取数据
    mom = row.get('动量', 0)
    vol = row.get('波动率', 0)
    rsi = row.get('RSI', 0)
    score = row.get('综合得分', 0)
    
    comments = []
    if mom > 1.0: comments.append("🚀 动量极强")
    elif mom > 0.5: comments.append("📈 趋势向上")
    elif mom < -0.5: comments.append("📉 动量较弱")
    
    if vol < -0.5: comments.append("🛡️ 波动极低")
    elif vol > 0.5: comments.append("⚡ 波动剧烈")
    
    if rsi > 70: comments.append("⚠️ RSI超买")
    elif rsi < 30: comments.append("💎 RSI超卖")
    
    if score > 2.0: comments.append("🌟 **极力推荐**")
    
    if not comments: return "走势平稳"
    return " | ".join(comments)

def display_industry_timing(date_str=None):
    """展示行业择机：新兴按渗透率、成熟按生命周期(季/年)。使用缓存避免重复请求。"""
    @st.cache_data(ttl=3600, show_spinner=False)
    def _fetch_timing(_date_str):
        try:
            from datetime import datetime
            end_date = datetime.strptime(_date_str, "%Y-%m-%d") if _date_str else datetime.now()
            timing = IndustryTiming()
            return timing.run_split(end_date=end_date, max_industries=40, emerging_top=8, mature_top=8)
        except Exception as e:
            return {"error": str(e), "current_cycle": "", "emerging": None, "mature": None}
    with st.spinner("正在加载行业择机数据…"):
        data = _fetch_timing(date_str or pd.Timestamp.now().strftime("%Y-%m-%d"))
    if data.get("error"):
        st.warning(f"行业择机数据暂不可用：{data['error']}")
        return
    cycle_cn = {"early": "早周期", "mid": "中周期", "late": "晚周期", "defensive": "防御"}.get(data.get("current_cycle", ""), data.get("current_cycle", ""))
    st.caption(f"当前经济周期：**{cycle_cn}** | 基准(沪深300)60日收益：{data.get('benchmark_return_pct', 0):.1f}%")
    emerging_df = data.get("emerging")
    if emerging_df is not None and not emerging_df.empty:
        st.markdown("**新兴行业（按渗透率）**")
        pen_cn = {"early_growth": "破壁", "mid_growth": "高速", "mature": "饱和", "late": "晚周期", "decline": "衰退"}
        show = emerging_df[["industry", "penetration_phase", "relative_strength", "return_pct"]].copy()
        show.columns = ["行业", "渗透阶段", "相对强度(%)", "区间收益(%)"]
        show["渗透阶段"] = show["渗透阶段"].map(lambda x: pen_cn.get(x, x))
        st.dataframe(show, use_container_width=True, hide_index=True)
    mature_df = data.get("mature")
    if mature_df is not None and not mature_df.empty:
        st.markdown("**成熟行业（按生命周期 季/年）**")
        cyc_cn = {"early": "早周期", "mid": "中周期", "late": "晚周期", "defensive": "防御"}
        cols = ["industry", "cycle_type", "cycle_match", "quarter_rs", "year_rs"]
        if "quarter_rs" not in mature_df.columns:
            cols = ["industry", "cycle_type", "cycle_match", "relative_strength"]
        show = mature_df[[c for c in cols if c in mature_df.columns]].copy()
        if "quarter_rs" in show.columns:
            show.columns = ["行业", "周期属性", "周期匹配", "季度相对强度(%)", "年度相对强度(%)"]
        else:
            show.columns = ["行业", "周期属性", "周期匹配", "相对强度(%)"][: len(show.columns)]
        show["周期属性"] = show["周期属性"].map(lambda x: cyc_cn.get(x, x))
        if "周期匹配" in show.columns:
            show["周期匹配"] = show["周期匹配"].map({True: "✓", False: ""})
        st.dataframe(show, use_container_width=True, hide_index=True)


def run_backtest(start_date, end_date, top_k=10, hold_days=5):
    """运行回测"""
    try:
        with st.spinner('正在运行回测...'):
            engine = BacktestEngine()
            result = engine.run_backtest(
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d'),
                top_k=top_k,
                hold_days=hold_days
            )
            engine.close()
        return result
    except Exception as e:
        st.error(f"回测失败: {e}")
        return None


# =====================================================================
# 混合策略辅助函数
# =====================================================================

def _highlight_hybrid_row(row):
    """混合策略表格行高亮

    - ai_score > 0.8 => 绿色高亮
    - event_score > 0 => 橙色高亮 (概念热股)
    """
    styles = [''] * len(row)

    ai_col = 'AI评分'
    event_col = '事件评分'

    try:
        ai_val = float(row.get(ai_col, 0) if not isinstance(row.get(ai_col), str) else 0)
    except (ValueError, TypeError):
        ai_val = 0

    try:
        event_val = float(row.get(event_col, 0) if not isinstance(row.get(event_col), str) else 0)
    except (ValueError, TypeError):
        event_val = 0

    if ai_val > 0.8:
        styles = ['background-color: rgba(0, 200, 83, 0.15)'] * len(row)
    elif event_val > 0:
        styles = ['background-color: rgba(255, 152, 0, 0.12)'] * len(row)

    return styles


def display_hybrid_strategy(date_str):
    """展示混合策略选股结果"""

    # 1. 热门主题指标
    hot_topics = Config.get('hot_topics') or []
    if hot_topics:
        topics_str = ', '.join(hot_topics)
        st.markdown(f"🔥 **当前追踪热点主题:** {topics_str}")

    # 2. 运行混合策略
    with st.spinner('正在运行 AI + 事件混合策略...'):
        strategy = HybridStrategy()
        result = strategy.run(trade_date=date_str, top_k=20)

    if result is None or result.empty:
        st.warning("混合策略未返回结果，请确认数据已同步。")
        return

    # 3. 顶部指标
    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    with col_m1:
        st.metric("入选股票数", f"{len(result)}")
    with col_m2:
        hot_count = int((result['event_score'] > 0).sum())
        st.metric("🔥 概念热股", f"{hot_count}")
    with col_m3:
        ai_count = int((result['ai_score'] > 0.8).sum())
        st.metric("🤖 AI高分股", f"{ai_count}")
    with col_m4:
        avg_score = result['final_score'].mean()
        st.metric("平均综合分", f"{avg_score:.3f}")

    # 4. 构建展示 DataFrame
    display_df = result.copy()

    # 名称列：给有事件评分的股票加 🔥 标记
    display_df['name_display'] = display_df.apply(
        lambda r: f"🔥 {r['name']}" if r.get('event_score', 0) > 0 else r['name'],
        axis=1
    )

    # 市值格式化
    if 'total_mv' in display_df.columns:
        display_df['total_mv_fmt'] = display_df['total_mv'].apply(
            lambda x: f"{x / 1e8:.1f}" if pd.notna(x) and x > 0 else "-"
        )

    # 构建最终展示表
    show_df = pd.DataFrame({
        '代码': display_df['ts_code'],
        '名称': display_df['name_display'],
        '收盘价': display_df['close'].apply(lambda x: f"{x:.2f}" if pd.notna(x) else "-"),
        'AI评分': display_df['ai_score'].apply(lambda x: round(x, 3)),
        '事件评分': display_df['event_score'].apply(lambda x: round(x, 1)),
        '基本面': display_df['fundamental_score'].apply(lambda x: round(x, 3)),
        '综合评分': display_df['final_score'].apply(lambda x: round(x, 3)),
        '概念标签': display_df['concepts'],
    })

    if 'total_mv_fmt' in display_df.columns:
        show_df.insert(3, '市值(亿)', display_df['total_mv_fmt'])

    # 名称查名修正 (stock_info 补全)
    from src.utils.db_utils import DBUtils
    for i, row in show_df.iterrows():
        raw_name = str(row['名称']).replace('🔥 ', '')
        code = str(row['代码'])
        if raw_name == code:
            try:
                r = DBUtils.query_df(
                    f"SELECT name FROM stock_info WHERE ts_code = '{code}'"
                )
                if not r.empty and pd.notna(r.iloc[0]['name']):
                    prefix = "🔥 " if "🔥" in str(row['名称']) else ""
                    show_df.at[i, '名称'] = prefix + r.iloc[0]['name']
            except Exception:
                pass

    # 5. 样式化表格
    styled = show_df.style.apply(_highlight_hybrid_row, axis=1)

    st.subheader(f"🏆 {date_str} AI+事件混合 Top {len(show_df)}")
    st.caption(
        "高亮说明: 绿色 = AI评分 > 0.8 | 橙色 = 概念热股 | 🔥 = 匹配热门主题"
    )
    st.dataframe(styled, width=None, use_container_width=True, hide_index=True)

    # 6. 个股诊断
    st.divider()
    st.subheader("🔬 个股 AI 诊断")
    selected_code = st.selectbox(
        "选择股票", show_df['代码'].tolist(), key='hybrid_stock_select'
    )
    target = result[result['ts_code'] == selected_code]
    if not target.empty:
        row = target.iloc[0]
        parts = []
        if row['ai_score'] > 0.8:
            parts.append("🤖 AI强烈看好")
        elif row['ai_score'] > 0.5:
            parts.append("🤖 AI适度看好")
        else:
            parts.append("🤖 AI评分一般")

        if row['event_score'] > 0:
            parts.append(f"🔥 概念热股: {row['concepts']}")

        if row['fundamental_score'] > 0.7:
            parts.append("📊 基本面优秀")
        elif row['fundamental_score'] > 0.4:
            parts.append("📊 基本面中等")

        parts.append(f"综合评分: {row['final_score']:.3f}")
        st.info(" | ".join(parts))

    # LLM 深度分析
    llm_client = LLMClient()
    if llm_client.is_available():
        if st.button("🧠 生成AI深度分析", key="hybrid_ai_analysis"):
            with st.spinner('正在生成AI分析...'):
                topk_strategy = TopKStrategy(read_only=True)
                try:
                    full_stock_data = topk_strategy.get_stock_detail(
                        selected_code, date_str
                    )
                finally:
                    topk_strategy.close()

                if full_stock_data is not None:
                    ai_analysis = llm_client.generate_analysis(full_stock_data)
                    st.markdown("### 🤖 AI 深度分析")
                    st.write(ai_analysis)
                else:
                    st.warning("无法获取股票详细数据")
    else:
        st.caption("💡 提示：配置Grok API Key后可使用AI深度分析功能")


def display_pb_roa_strategy(date_str):
    """展示PB-ROA价值策略选股结果"""
    with st.spinner('正在运行 PB-ROA 价值策略...'):
        strategy = PbRoaStrategy()
        result = strategy.run(trade_date=date_str, top_k=20)

    if result is None or result.empty:
        st.warning("PB-ROA策略未返回结果，请确认数据已同步。")
        return

    col_m1, col_m2, col_m3 = st.columns(3)
    with col_m1:
        st.metric("入选股票数", f"{len(result)}")
    with col_m2:
        avg_pb = result['sub_scores'].apply(lambda x: x.get('pb', 0)).mean()
        st.metric("平均PB", f"{avg_pb:.1f}")
    with col_m3:
        avg_roa = result['sub_scores'].apply(lambda x: x.get('roa', 0)).mean()
        st.metric("平均ROA", f"{avg_roa:.1f}%")

    display_df = result.copy()
    display_df['PB'] = display_df['sub_scores'].apply(lambda x: x.get('pb', 0))
    display_df['ROA'] = display_df['sub_scores'].apply(lambda x: x.get('roa', 0))
    display_df['PB分位'] = display_df['sub_scores'].apply(lambda x: x.get('pb_percentile', 0))
    display_df['负债率'] = display_df['sub_scores'].apply(lambda x: x.get('debt_ratio', 0))

    show_df = pd.DataFrame({
        '代码': display_df['ts_code'],
        '名称': display_df['name'],
        'PB': display_df['PB'].apply(lambda x: f"{x:.1f}"),
        'PB分位': display_df['PB分位'].apply(lambda x: f"{x:.0f}%"),
        'ROA': display_df['ROA'].apply(lambda x: f"{x:.1f}%"),
        '负债率': display_df['负债率'].apply(lambda x: f"{x:.0f}%"),
        '综合评分': display_df['score'].apply(lambda x: f"{x:.3f}"),
        '入选理由': display_df['signal_reason'],
    })

    st.subheader(f"📊 {date_str} PB-ROA价值 Top {len(show_df)}")
    st.dataframe(show_df, use_container_width=True, hide_index=True)


def display_index_enhance_strategy(date_str):
    """展示指数增强策略选股结果"""
    with st.spinner('正在运行指数增强策略...'):
        strategy = IndexEnhanceStrategy()
        result = strategy.run(trade_date=date_str, top_k=20)

    if result is None or result.empty:
        st.warning("指数增强策略未返回结果，请确认数据已同步。")
        return

    col_m1, col_m2, col_m3 = st.columns(3)
    with col_m1:
        st.metric("入选股票数", f"{len(result)}")
    with col_m2:
        avg_score = result['score'].mean()
        st.metric("平均评分", f"{avg_score:.3f}")
    with col_m3:
        industries = result['sub_scores'].apply(lambda x: x.get('industry', '')).unique()
        st.metric("覆盖行业", f"{len(industries)}个")

    display_df = result.copy()
    display_df['行业'] = display_df['sub_scores'].apply(lambda x: x.get('industry', ''))
    display_df['价值分'] = display_df['sub_scores'].apply(lambda x: x.get('value_score', 0))
    display_df['质量分'] = display_df['sub_scores'].apply(lambda x: x.get('quality_score', 0))
    display_df['动量分'] = display_df['sub_scores'].apply(lambda x: x.get('momentum_score', 0))
    display_df['反转分'] = display_df['sub_scores'].apply(lambda x: x.get('reversal_score', 0))

    show_df = pd.DataFrame({
        '代码': display_df['ts_code'],
        '名称': display_df['name'],
        '行业': display_df['行业'],
        '价值分': display_df['价值分'].apply(lambda x: f"{x:.3f}"),
        '质量分': display_df['质量分'].apply(lambda x: f"{x:.3f}"),
        '动量分': display_df['动量分'].apply(lambda x: f"{x:.3f}"),
        '反转分': display_df['反转分'].apply(lambda x: f"{x:.3f}"),
        '综合评分': display_df['score'].apply(lambda x: f"{x:.3f}"),
    })

    st.subheader(f"📈 {date_str} 指数增强 Top {len(show_df)}")
    st.dataframe(show_df, use_container_width=True, hide_index=True)


def display_etf_strategy_page():
    """展示ETF策略页面"""
    st.header("📊 ETF统一策略")

    mode = st.selectbox(
        "市场环境模式",
        ["自动检测", "进攻模式", "防御模式", "均衡模式"],
        index=0
    )

    if st.button("🚀 运行ETF策略", type="primary"):
        from src.strategy.etf_unified_strategy import ETFUnifiedStrategy
        with st.spinner('正在运行ETF统一策略...'):
            strategy = ETFUnifiedStrategy()
            result = strategy.run(top_n=10)

        if result is None or result.empty:
            st.warning("ETF策略未返回结果")
            return

        st.subheader(f"🏆 ETF Top {len(result)}")
        show_df = pd.DataFrame({
            '代码': result['code'],
            '名称': result['name'],
            '评分': result['score'].apply(lambda x: f"{x:.3f}"),
            '策略': result['strategies'],
            '建议': result.get('advice', '分批建仓'),
        })
        st.dataframe(show_df, use_container_width=True, hide_index=True)


def display_convertible_bond_page():
    """展示可转债策略页面"""
    st.header("📊 可转债策略")

    st.markdown("""
    **策略逻辑**：下有保底（债底） × 上不封顶（转股）
    - 到期收益率(YTM) > 0 → 保底安全
    - 转股溢价率 < 40% → 有弹性
    - 正股动量 > -5% → 正股不能太弱
    - 剩余规模 1-15亿 → 小盘弹性大
    """)

    if st.button("🚀 运行可转债策略", type="primary"):
        from src.strategy.convertible_bond_strategy import ConvertibleBondStrategy
        with st.spinner('正在运行可转债策略...'):
            strategy = ConvertibleBondStrategy()
            result = strategy.run(top_k=20)

        if result is None or result.empty:
            st.warning("可转债策略未返回结果")
            return

        st.subheader(f"🏆 可转债 Top {len(result)}")
        show_df = pd.DataFrame({
            '代码': result['ts_code'],
            '名称': result['name'],
            '评分': result['score'].apply(lambda x: f"{x:.3f}"),
            '入选理由': result['signal_reason'],
        })
        st.dataframe(show_df, use_container_width=True, hide_index=True)


# --- 3. 页面路由 ---
if main_page == "策略中心":
    from src.app.pages.strategy_center import render_strategy_center_page
    render_strategy_center_page()
    st.stop()

if main_page == "ETF策略":
    display_etf_strategy_page()
    st.stop()

if main_page == "可转债":
    display_convertible_bond_page()
    st.stop()

if main_page == "数据源状态":
    st.header("🔌 数据源状态")
    from src.collector.multi_source_adapter import ak, efinance, baostock
    sources = {
        "AKShare": ak,
        "eFinance": efinance,
        "Baostock": baostock,
    }
    try:
        import tushare as ts
        sources["Tushare"] = ts
    except ImportError:
        sources["Tushare"] = None

    cols = st.columns(len(sources))
    for i, (name, mod) in enumerate(sources.items()):
        with cols[i]:
            if mod is not None:
                st.success(f"✅ {name}")
                st.caption(f"版本: {getattr(mod, '__version__', 'N/A')}")
            else:
                st.error(f"❌ {name}")
                st.caption("未安装")

    st.divider()
    st.subheader("📊 数据覆盖矩阵")
    coverage = pd.DataFrame({
        "数据源": ["AKShare", "eFinance", "Baostock", "Tushare"],
        "股票列表": ["✅", "✅", "✅", "✅"],
        "日线行情": ["✅", "✅", "✅", "✅"],
        "实时行情": ["✅", "✅", "❌", "❌"],
        "ETF行情": ["✅", "✅", "❌", "✅"],
        "可转债": ["✅", "❌", "❌", "✅"],
        "指数成分": ["✅", "❌", "❌", "✅"],
        "板块概念": ["✅", "❌", "❌", "✅"],
        "北向资金": ["✅", "❌", "❌", "✅"],
        "龙虎榜": ["✅", "❌", "❌", "✅"],
        "宏观经济": ["✅", "❌", "❌", "✅"],
    })
    st.dataframe(coverage, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("🔄 数据同步")
    st.markdown("""
    **同步命令**：
    ```bash
    # 同步全部免费数据
    python scripts/sync_free_data.py

    # 仅同步特定数据
    python scripts/sync_free_data.py --etf        # ETF行情
    python scripts/sync_free_data.py --cb         # 可转债
    python scripts/sync_free_data.py --index      # 指数成分股
    python scripts/sync_free_data.py --concept    # 板块概念
    python scripts/sync_free_data.py --northbound # 北向资金
    ```
    """)

    st.stop()

# 如果选择了推送消息历史或自动交易管理，显示对应页面
if main_page == "推送消息历史":
    st.header("📨 推送消息历史记录")
    
    from src.utils.message_logger import MessageLogger
    msg_logger = MessageLogger()
    
    # 筛选选项
    col1, col2, col3 = st.columns(3)
    with col1:
        msg_type = st.selectbox(
            "消息类型",
            ["全部", "morning_push", "evening_push", "futures_etf", "etf_strategy"],
            index=0
        )
    with col2:
        days = st.selectbox("时间范围", [7, 30, 90, 365], index=1)
    with col3:
        limit = st.selectbox("显示数量", [50, 100, 200, 500], index=1)
    
    # 获取消息
    filter_type = None if msg_type == "全部" else msg_type
    start_date = (pd.Timestamp.now() - pd.Timedelta(days=days)).strftime('%Y-%m-%d')
    
    messages_df = msg_logger.get_messages(
        message_type=filter_type,
        limit=limit,
        start_date=start_date
    )
    
    if messages_df is not None and not messages_df.empty:
        # 统计信息
        stats = msg_logger.get_message_statistics()
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("总消息数", stats.get('total', 0))
        with col2:
            st.metric("成功率", f"{stats.get('success_rate', 0):.1f}%")
        with col3:
            st.metric("最近7天", stats.get('last_7_days', 0))
        with col4:
            st.metric("当前显示", len(messages_df))
        
        st.divider()
        
        # 消息列表
        for idx, row in messages_df.iterrows():
            with st.expander(f"{row['title']} - {row['send_time']}", expanded=False):
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.markdown(f"**类型**: {row['message_type']}")
                    st.markdown(f"**状态**: {'✅ 成功' if row['send_status'] == 'success' else '❌ 失败'}")
                with col2:
                    st.markdown(f"**时间**: {row['send_time']}")
                
                if row['send_status'] == 'failed' and row.get('error_message'):
                    st.error(f"错误信息: {row['error_message']}")
                
                st.markdown("**消息内容**:")
                st.markdown(row['content'])
    
    else:
        st.info("暂无消息记录")
    
    st.divider()
    
    # 消息统计图表
    if messages_df is not None and not messages_df.empty:
        st.subheader("📊 消息统计")
        
        # 按类型统计
        type_stats = messages_df.groupby('message_type').size()
        st.bar_chart(type_stats)
        
        # 按日期统计
        messages_df['date'] = pd.to_datetime(messages_df['send_time']).dt.date
        date_stats = messages_df.groupby('date').size()
        st.line_chart(date_stats)
    
    st.stop()

elif main_page == "自动交易管理":
    st.header("🤖 自动交易管理")
    
    from src.trading.auto_trader import AutoTrader
    from src.utils.config_loader import Config
    
    portfolio_config = Config.get('portfolio', {})
    auto_trade_enabled = portfolio_config.get('auto_trade_enabled', False)
    
    st.subheader("💰 资金配置")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("总资金", f"{portfolio_config.get('total_capital', 1000000):,.0f} 元")
    with col2:
        st.metric("股票资金", f"{portfolio_config.get('stock_capital', 500000):,.0f} 元")
    with col3:
        st.metric("ETF资金", f"{portfolio_config.get('etf_capital', 500000):,.0f} 元")
    
    st.divider()
    
    # 自动交易状态
    st.subheader("⚙️ 自动交易状态")
    status_color = "🟢" if auto_trade_enabled else "🔴"
    st.markdown(f"{status_color} **自动交易**: {'已启用' if auto_trade_enabled else '已禁用'}")
    
    if auto_trade_enabled:
        st.info("✅ 系统将根据选股结果自动执行买卖操作")
    else:
        st.warning("⚠️ 自动交易已禁用，需要手动执行交易")
    
    st.divider()
    
    # 交易汇总
    st.subheader("📊 交易汇总")
    try:
        trader = AutoTrader()
        summary = trader.get_trading_summary()
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("### 股票持仓")
            stock_summary = summary['stock']
            st.metric("持仓股数", stock_summary['stock_count'])
            st.metric("持仓市值", f"{stock_summary['total_value']:,.0f} 元")
            st.metric("浮动盈亏", f"{stock_summary['total_profit_loss']:+,.0f} 元 ({stock_summary['total_profit_loss_pct']*100:+.2f}%)")
            st.metric("持仓比例", f"{stock_summary['total_position_pct']*100:.1f}%")
        
        with col2:
            st.markdown("### ETF持仓")
            etf_summary = summary['etf']
            st.metric("持仓数量", etf_summary['stock_count'])
            st.metric("持仓市值", f"{etf_summary['total_value']:,.0f} 元")
            st.metric("浮动盈亏", f"{etf_summary['total_profit_loss']:+,.0f} 元 ({etf_summary['total_profit_loss_pct']*100:+.2f}%)")
            st.metric("持仓比例", f"{etf_summary['total_position_pct']*100:.1f}%")
        
    except Exception as e:
        st.error(f"获取交易汇总失败: {e}")
    
    st.stop()

# --- 3. 主界面布局（交易驾驶舱）---
st.title("🚀 QuantAgent 交易驾驶舱")

# 页面导航（原有导航）
page = st.sidebar.radio(
    "📑 导航",
    ["选股策略", "仓位管理", "回测分析"],
    index=0
)

# 如果选择仓位管理页面，渲染并退出
if page == "仓位管理":
    from src.app.pages.portfolio import render_portfolio_page
    render_portfolio_page()
    st.stop()

# 初始化stock_info表（如果不存在）
print("Skipping table initialization in dashboard to avoid connection conflicts")

# 使用列布局：左边日期，右边按钮
col1, col2, col3, col4 = st.columns([2, 1, 1, 1])

# 获取最新日期
temp_strategy = TopKStrategy(read_only=True)
try:
    latest_date_str = temp_strategy.get_latest_trade_date()
finally:
    temp_strategy.close()

with col1:
    if latest_date_str:
        selected_date = st.date_input(
            "📅 选择回测日期",
            value=pd.to_datetime(latest_date_str),
            max_value=pd.to_datetime(latest_date_str)
        )
        date_str = selected_date.strftime('%Y-%m-%d')
    else:
        st.warning("数据为空，请点击右侧按钮初始化 ->")
        date_str = None

with col2:
    # 策略选择器 (新增PB-ROA、可转债、指数增强)
    strategy_name = st.selectbox(
        "🎯 选择策略",
        options=[
            "TopK策略",
            "小市值+行业冥灯策略",
            "🤖 AI+事件混合策略",
            "📊 PB-ROA价值策略",
            "📈 指数增强策略",
        ],
        index=0
    )

with col3:
    # 回测周期选择器
    hold_days = st.selectbox(
        "📊 回测周期",
        options=[1, 3, 5, 10],
        index=2,  # 默认选择5天
        format_func=lambda x: f"{x}天"
    )

with col4:
    # 这就是你要找的按钮！放在了最显眼的地方
    st.write("")  # 占位，让按钮对齐
    st.write("")
    if st.button("🔄 立即全量更新", type="primary", width='stretch'):
        run_sync_task()

# --- 4. 数据展示区 ---
if date_str:
    # ========== 混合策略 ==========
    if strategy_name == "🤖 AI+事件混合策略":
        display_hybrid_strategy(date_str)

    # ========== PB-ROA价值策略 ==========
    elif strategy_name == "📊 PB-ROA价值策略":
        display_pb_roa_strategy(date_str)

    # ========== 指数增强策略 ==========
    elif strategy_name == "📈 指数增强策略":
        display_index_enhance_strategy(date_str)

    # ========== 传统策略 ==========
    elif strategy_name == "TopK策略":
        strategy = TopKStrategy(read_only=True)
        try:
            top_stocks = strategy.get_top_stocks(date_str, top_k=10)
            
            # 添加未来表现
            if top_stocks is not None and not top_stocks.empty:
                top_stocks = strategy.add_future_performance(top_stocks, date_str, hold_days=hold_days)
        finally:
            strategy.close()

        if top_stocks is not None and not top_stocks.empty:
            # TopK策略列重命名
            display_df = top_stocks.rename(columns={
                'ts_code': '代码',
                'name': '名称',
                'mom_20': '动量',
                'vol_20': '波动率',
                'rsi_14': 'RSI',
                'score': '综合得分',
                'future_return': f'{hold_days}日后表现'
            })
            
            # 添加说明文字
            st.caption("注：'持仓中'代表当前查看的是最新日期，未来收益尚未产生。请选择历史日期查看回测效果。")
            
            # 格式化收益率显示
            def format_return(value):
                if pd.isna(value):
                    return "持仓中"
                elif value > 0:
                    return f"+{value:.2f}%"
                else:
                    return f"{value:.2f}%"
            
            # 应用格式化
            if f'{hold_days}日后表现' in display_df.columns:
                display_df[f'{hold_days}日后表现'] = display_df[f'{hold_days}日后表现'].apply(format_return)
            
            # 交互式表格，带条件格式化
            def highlight_return(row):
                if f'{hold_days}日后表现' not in row.index:
                    return [''] * len(row)
                
                return_value = row[f'{hold_days}日后表现']
                if return_value == "持仓中":
                    return [''] * len(row)
                
                # 提取数值
                try:
                    value = float(return_value.replace('%', '').replace('+', ''))
                except:
                    return [''] * len(row)
                
                # A股习惯：盈利显示红色，亏损显示绿色
                if value > 0:
                    return ['background-color: #ffcccc'] * len(row)  # 红色背景
                elif value < 0:
                    return ['background-color: #ccffcc'] * len(row)  # 绿色背景
                else:
                    return [''] * len(row)
            
            # 应用样式
            styled_df = display_df.style.apply(highlight_return, axis=1)

            # --- 通用格式化 ---
            if 'trade_date' in display_df.columns:
                display_df['trade_date'] = pd.to_datetime(display_df['trade_date']).dt.strftime('%Y-%m-%d')
            
            mv_col = None
            if 'total_mv' in display_df.columns: mv_col = 'total_mv'
            elif '总市值(亿)' in display_df.columns: mv_col = '总市值(亿)'
            
            if mv_col:
                display_df[mv_col] = pd.to_numeric(display_df[mv_col], errors='coerce').fillna(0)
                def format_market_cap(x):
                    if x < 1000 or x == 0:
                        return "-"
                    else:
                        return f"{x/100000000:.2f}"
                display_df[mv_col] = display_df[mv_col].apply(format_market_cap)
                display_df = display_df.rename(columns={mv_col: '市值(亿)'})
            
            if 'pe_ttm' in display_df.columns:
                display_df['pe_ttm'] = pd.to_numeric(display_df['pe_ttm'], errors='coerce').fillna(0)
                display_df['pe_ttm'] = display_df['pe_ttm'].apply(lambda x: f"{x:.1f}" if x != 0 else "-")
            
            if '名称' in display_df.columns and '代码' in display_df.columns:
                from src.utils.db_utils import DBUtils
                for i, row in display_df.iterrows():
                    name = str(row['名称'])
                    code = str(row['代码'])
                    if name == code:
                        try:
                            result = DBUtils.query_df(f"SELECT name FROM stock_info WHERE ts_code = '{code}'")
                            if not result.empty and pd.notna(result.iloc[0]['name']):
                                display_df.at[i, '名称'] = result.iloc[0]['name']
                        except Exception:
                            pass

            st.subheader(f"🏆 {date_str} 优选 Top 10")
            st.dataframe(styled_df, width='stretch', hide_index=True)

            # 个股诊断
            st.divider()
            st.subheader("🔬 个股 AI 诊断")
            selected_code = st.selectbox("选择股票", display_df['代码'].tolist())
            target_row = display_df[display_df['代码'] == selected_code].iloc[0]
            st.info(f"🤖 快速分析: {generate_ai_comment(target_row)}")

            # LLM AI分析
            llm_client = LLMClient()
            if llm_client.is_available():
                if st.button("🧠 生成AI深度分析", key="generate_ai_analysis"):
                    with st.spinner('正在生成AI分析...'):
                        strategy = TopKStrategy(read_only=True)
                        try:
                            full_stock_data = strategy.get_stock_detail(selected_code, date_str)
                        finally:
                            strategy.close()
                        
                        if full_stock_data is not None:
                            ai_analysis = llm_client.generate_analysis(full_stock_data)
                            st.markdown("### 🤖 AI 深度分析")
                            st.write(ai_analysis)
                        else:
                            st.warning("无法获取股票详细数据")
            else:
                st.caption("💡 提示：配置Grok API Key后可使用AI深度分析功能")

        else:
            st.info("该日期无数据，可能是周末或节假日。")

    # ========== 小市值策略 ==========
    else:
        strategy = SmallCapJinxStrategy(read_only=True)
        try:
            top_stocks = strategy.get_top_stocks(date_str, top_k=10)
        finally:
            strategy.close()

        if top_stocks is not None and not top_stocks.empty:
            display_df = top_stocks.rename(columns={
                'ts_code': '代码',
                'name': '名称',
                'industry': '行业',
                'total_mv': '总市值(亿)',
                'close': '收盘价'
            })
            styled_df = display_df.style

            # --- 通用格式化 ---
            if 'trade_date' in display_df.columns:
                display_df['trade_date'] = pd.to_datetime(display_df['trade_date']).dt.strftime('%Y-%m-%d')
            
            mv_col = None
            if 'total_mv' in display_df.columns: mv_col = 'total_mv'
            elif '总市值(亿)' in display_df.columns: mv_col = '总市值(亿)'
            
            if mv_col:
                display_df[mv_col] = pd.to_numeric(display_df[mv_col], errors='coerce').fillna(0)
                def format_market_cap(x):
                    if x < 1000 or x == 0:
                        return "-"
                    else:
                        return f"{x/100000000:.2f}"
                display_df[mv_col] = display_df[mv_col].apply(format_market_cap)
                display_df = display_df.rename(columns={mv_col: '市值(亿)'})
            
            if 'pe_ttm' in display_df.columns:
                display_df['pe_ttm'] = pd.to_numeric(display_df['pe_ttm'], errors='coerce').fillna(0)
                display_df['pe_ttm'] = display_df['pe_ttm'].apply(lambda x: f"{x:.1f}" if x != 0 else "-")
            
            if '名称' in display_df.columns and '代码' in display_df.columns:
                from src.utils.db_utils import DBUtils
                for i, row in display_df.iterrows():
                    name = str(row['名称'])
                    code = str(row['代码'])
                    if name == code:
                        try:
                            result = DBUtils.query_df(f"SELECT name FROM stock_info WHERE ts_code = '{code}'")
                            if not result.empty and pd.notna(result.iloc[0]['name']):
                                display_df.at[i, '名称'] = result.iloc[0]['name']
                        except Exception:
                            pass

            st.subheader(f"🏆 {date_str} 优选 Top 10")
            st.dataframe(styled_df, width='stretch', hide_index=True)

            # 小市值策略的个股诊断
            st.divider()
            st.subheader("🔬 个股 AI 诊断")
            selected_code = st.selectbox("选择股票", display_df['代码'].tolist())
            target_row = display_df[display_df['代码'] == selected_code].iloc[0]
            industry = target_row.get('行业', '未知')
            market_cap = target_row.get('总市值(亿)', '未知')
            st.info(f"🤖 快速分析: 小市值股票 | 行业: {industry} | 总市值: {market_cap} 亿")
        else:
            st.info("该日期无数据，可能是周末或节假日。")

    # 行业择机（科技按渗透率、成熟按周期）- 选股策略下均展示
    st.divider()
    with st.expander("📐 行业择机（科技按渗透率 · 成熟按周期）", expanded=False):
        display_industry_timing(date_str)

# --- 5. 回测功能区 ---
st.divider()
st.subheader("📊 策略回测")

# 回测参数设置
col1, col2, col3, col4 = st.columns(4)

with col1:
    backtest_start = st.date_input("开始日期", value=pd.to_datetime('2026-01-01'))
with col2:
    backtest_end = st.date_input("结束日期", value=pd.to_datetime(latest_date_str) if latest_date_str else pd.to_datetime('2026-02-05'))
with col3:
    backtest_top_k = st.selectbox("选股数量", options=[5, 10, 15, 20], index=0)
with col4:
    backtest_hold_days = st.selectbox("持仓天数", options=[3, 5, 7, 10], index=1)

# 运行回测按钮
if st.button("🚀 运行回测", type="primary"):
    result = run_backtest(backtest_start, backtest_end, backtest_top_k, backtest_hold_days)
    
    if result:
        # 保存回测结果到session state
        st.session_state['backtest_result'] = result
        st.success("回测完成！")

# 显示回测结果
if 'backtest_result' in st.session_state:
    result = st.session_state['backtest_result']
    
    # 显示性能指标
    metrics = result['metrics']
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("累计收益率", f"{metrics['total_return']:.2f}%")
    with col2:
        st.metric("年化收益率", f"{metrics['annual_return_pct']:.2f}%")
    with col3:
        st.metric("最大回撤", f"{metrics['max_drawdown']:.2f}%")
    with col4:
        st.metric("夏普比率", f"{metrics['sharpe_ratio']:.2f}")
    
    # 显示更多指标
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("胜率", f"{metrics['win_rate']:.2f}%")
    with col2:
        st.metric("平均收益", f"{metrics['avg_return']:.2f}%")
    with col3:
        st.metric("收益标准差", f"{metrics['std_return']:.2f}%")
    
    # 显示累计收益曲线
    results_df = result['results']
    
    st.subheader("📈 累计收益曲线")
    
    # 创建图表
    chart_data = pd.DataFrame({
        '日期': results_df['trade_date'],
        '累计收益率': results_df['cumulative_return'] * 100
    })
    
    st.line_chart(chart_data.set_index('日期'))
    
    # 显示交易记录
    st.subheader("📋 交易记录")
    
    # 处理日期格式，只显示日期部分
    display_results = results_df.copy()
    if 'trade_date' in display_results.columns:
        display_results['trade_date'] = pd.to_datetime(display_results['trade_date']).dt.strftime('%Y-%m-%d')
    
    st.dataframe(
        display_results[['trade_date', 'portfolio_return', 'cumulative_return', 'num_stocks']].rename(columns={
            'trade_date': '交易日期',
            'portfolio_return': '组合收益率(%)',
            'cumulative_return': '累计收益率(%)',
            'num_stocks': '选股数量'
        }),
        width='stretch',
        hide_index=True
    )


else:
    # 原有的交易驾驶舱内容（保持原有逻辑）
    # 这里的内容已经在下面定义了，不需要修改
    pass
