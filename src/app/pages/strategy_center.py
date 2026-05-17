"""
策略中心页面 — 信号/宏观/因子IC/回测一体化视图
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', '..'))

import streamlit as st
import pandas as pd
import glob

from src.utils.db_utils import DBUtils

# ────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────
STRATEGY_LABELS = {
    "dividend":         "红利策略",
    "quant":            "量化策略",
    "small_cap":        "小盘策略",
    "cyclical":         "周期策略",
    "pb_roa":           "PB-ROA价值",
    "convertible_bond": "可转债",
    "index_enhance":    "指数增强",
}

# ────────────────────────────────────────────────────────────
# 缓存数据加载函数
# ────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_latest_signals():
    """读取 strategy_signals 表中最新一批信号"""
    try:
        dt_df = DBUtils.query_df("SELECT MAX(trade_date) AS dt FROM strategy_signals")
        if dt_df.empty or dt_df.iloc[0]["dt"] is None:
            return None, pd.DataFrame()
        latest_date = dt_df.iloc[0]["dt"]

        sql = """
            SELECT strategy, ts_code, name, score, signal_detail, rank_in_strategy
            FROM strategy_signals
            WHERE trade_date = ?
            ORDER BY strategy, rank_in_strategy
        """
        df = DBUtils.query_df(sql, params=(latest_date,))
        return latest_date, df
    except Exception:
        return None, pd.DataFrame()


@st.cache_data(ttl=300)
def load_macro_indicators():
    """读取 macro_indicators 表最新指标"""
    try:
        sql = """
            SELECT indicator, value, data_date
            FROM macro_indicators
            ORDER BY data_date DESC
        """
        return DBUtils.query_df(sql)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_factor_ic():
    """读取 factor_ic_log 表最新一次因子IC数据"""
    try:
        dt_df = DBUtils.query_df("SELECT MAX(calc_date) AS dt FROM factor_ic_log")
        if dt_df.empty or dt_df.iloc[0]["dt"] is None:
            return None, pd.DataFrame()
        latest_date = dt_df.iloc[0]["dt"]

        sql = """
            SELECT factor_name, ic_mean_60d, ic_ir, is_valid
            FROM factor_ic_log
            WHERE calc_date = ?
            ORDER BY ABS(ic_mean_60d) DESC
        """
        df = DBUtils.query_df(sql, params=(latest_date,))
        return latest_date, df
    except Exception:
        return None, pd.DataFrame()


@st.cache_data(ttl=300)
def load_backtest_results():
    """扫描 output/ 目录，读取最新的 backtest_ensemble_*.csv"""
    output_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', '..', '..', 'output')
    )
    pattern = os.path.join(output_dir, "backtest_ensemble_*.csv")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return None, pd.DataFrame()
    try:
        df = pd.read_csv(files[0])
        return os.path.basename(files[0]), df
    except Exception:
        return os.path.basename(files[0]), pd.DataFrame()


# ────────────────────────────────────────────────────────────
# Tab 渲染函数
# ────────────────────────────────────────────────────────────

def _render_tab_signals():
    """Tab1 — 最新信号"""
    latest_date, df = load_latest_signals()

    if latest_date is None or df.empty:
        st.warning("strategy_signals 表暂无数据，请先运行策略生成信号。")
        return

    st.caption(f"数据日期：{latest_date}")

    # 4个策略 metrics
    cols = st.columns(min(4, len(STRATEGY_LABELS)))
    for i, (key, label) in enumerate(STRATEGY_LABELS.items()):
        if i >= 4:
            break
        sub = df[df["strategy"] == key]
        if sub.empty:
            cols[i].metric(label, "无数据")
        else:
            cols[i].metric(label, f"{len(sub)} 只")
            cols[i].caption(
                f"最高 {sub['score'].max():.3f} / 最低 {sub['score'].min():.3f}"
            )

    st.divider()

    # 策略筛选下拉
    options = ["全部"] + [f"{v}（{k}）" for k, v in STRATEGY_LABELS.items()]
    selected = st.selectbox("选择策略", options, key="signal_strategy_select")

    if selected == "全部":
        filtered = df.copy()
    else:
        key = [k for k, v in STRATEGY_LABELS.items() if selected.startswith(v)][0]
        filtered = df[df["strategy"] == key].copy()

    if filtered.empty:
        st.info("该策略暂无信号。")
        return

    filtered = filtered.sort_values("score", ascending=False).reset_index(drop=True)
    filtered["排名"] = filtered.index + 1
    max_score = float(filtered["score"].max()) or 1.0

    show_df = filtered.rename(columns={
        "ts_code":          "代码",
        "name":             "名称",
        "strategy":         "策略",
        "score":            "评分",
        "signal_detail":    "入选理由",
        "rank_in_strategy": "策略内排名",
    })[["排名", "代码", "名称", "策略", "评分", "入选理由"]]

    st.dataframe(
        show_df,
        use_container_width=True,
        column_config={
            "评分": st.column_config.ProgressColumn(
                "评分", min_value=0, max_value=max_score, format="%.4f",
            ),
        },
        hide_index=True,
    )


def _render_tab_macro():
    """Tab2 — 宏观状态"""
    df = load_macro_indicators()

    # 宏观风险等级（延迟导入）
    risk_level = "NORMAL"
    risk_color = "green"
    try:
        from src.utils.macro_monitor import MacroMonitor
        state = MacroMonitor().assess()
        risk_level = state.level
        color_map = {
            "CRISIS": "red", "HIGH": "orange", "MEDIUM": "goldenrod", "NORMAL": "green"
        }
        risk_color = color_map.get(risk_level, "gray")
    except Exception as e:
        st.caption(f"MacroMonitor 加载失败：{e}")

    level_cn = {"CRISIS": "流动性危机", "HIGH": "高风险", "MEDIUM": "中风险", "NORMAL": "正常"}
    st.markdown(
        f"<h3>宏观风险等级：<span style='color:{risk_color}'>"
        f"{level_cn.get(risk_level, risk_level)}</span></h3>",
        unsafe_allow_html=True,
    )
    st.divider()

    if df.empty:
        st.warning("macro_indicators 表暂无数据，请运行 python scripts/sync_macro_data.py")
        return

    # 关键指标 metrics（取最新两期做趋势对比）
    KEY_INDICATORS = ["pmi", "ppi_yoy", "m1_yoy", "m2_yoy"]
    KEY_LABELS     = {"pmi": "PMI", "ppi_yoy": "PPI同比%", "m1_yoy": "M1增速%", "m2_yoy": "M2增速%"}
    metric_cols = st.columns(len(KEY_INDICATORS))
    for i, ind in enumerate(KEY_INDICATORS):
        sub = df[df["indicator"] == ind].sort_values("data_date", ascending=False)
        label = KEY_LABELS.get(ind, ind)
        if sub.empty:
            metric_cols[i].metric(label, "N/A")
            continue
        val = float(sub.iloc[0]["value"])
        period = str(sub.iloc[0]["data_date"])[:7]
        delta = None
        if len(sub) >= 2:
            delta = round(val - float(sub.iloc[1]["value"]), 3)
        metric_cols[i].metric(
            f"{label}（{period}）",
            f"{val:.2f}",
            delta=f"{delta:+.3f}" if delta is not None else None,
        )

    st.subheader("全量宏观指标")
    latest_df = (
        df.sort_values("data_date", ascending=False)
        .drop_duplicates(subset=["indicator"])
        .reset_index(drop=True)
    )
    st.dataframe(latest_df, use_container_width=True, hide_index=True)


def _render_tab_factor_ic():
    """Tab3 — 因子IC排名"""
    try:
        import plotly.graph_objects as go
    except ImportError:
        st.warning("需要安装 plotly：pip install plotly")
        return

    latest_date, df = load_factor_ic()

    if latest_date is None or df.empty:
        st.warning("factor_ic_log 表暂无数据，请先运行 python scripts/factor_ic_weekly.py")
        return

    st.caption(f"数据日期：{latest_date}")

    df_sorted = df.sort_values("ic_mean_60d", ascending=True)
    colors = [
        "green" if int(row["is_valid"] or 0) == 1 else "red"
        for _, row in df_sorted.iterrows()
    ]

    fig = go.Figure(go.Bar(
        x=df_sorted["ic_mean_60d"],
        y=df_sorted["factor_name"],
        orientation="h",
        marker_color=colors,
        text=[f"{v:.4f}" for v in df_sorted["ic_mean_60d"]],
        textposition="outside",
    ))
    fig.update_layout(
        title="因子 IC均值（60日）— 绿色=有效，红色=无效",
        xaxis_title="IC均值",
        height=max(400, len(df_sorted) * 28),
        margin=dict(l=160, r=60, t=50, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)

    show_df = df.rename(columns={
        "factor_name": "因子名", "ic_mean_60d": "IC均值(60d)",
        "ic_ir": "IC_IR", "is_valid": "是否有效",
    })
    st.dataframe(show_df, use_container_width=True, hide_index=True)


def _render_tab_backtest():
    """Tab4 — 策略回测"""
    filename, df = load_backtest_results()

    if filename is None:
        st.info(
            "未找到回测结果文件。\n\n"
            "策略运行积累一段历史信号后，执行：\n"
            "```\npython scripts/backtest_strategy_ensemble.py\n```"
        )
        return

    st.caption(f"回测文件：{filename}")

    if df.empty:
        st.warning("回测文件内容为空或解析失败。")
        return

    col_map = {
        "strategy": "策略", "annual_return": "年化收益",
        "sharpe": "Sharpe", "max_drawdown": "最大回撤",
        "win_rate": "胜率", "n_periods": "期数",
    }
    df_display = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    for pct_col in ["年化收益", "最大回撤", "胜率"]:
        if pct_col in df_display.columns:
            df_display[pct_col] = df_display[pct_col].apply(
                lambda x: f"{float(x)*100:.2f}%" if pd.notna(x) else "N/A"
            )
    if "Sharpe" in df_display.columns:
        df_display["Sharpe"] = df_display["Sharpe"].apply(
            lambda x: f"{float(x):.3f}" if pd.notna(x) else "N/A"
        )

    st.dataframe(df_display, use_container_width=True, hide_index=True)


# ────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────

def render_strategy_center_page():
    """渲染策略中心页面，供 dashboard.py 调用"""
    st.header("🎯 策略中心")

    tab1, tab2, tab3, tab4 = st.tabs(["最新信号", "宏观状态", "因子IC排名", "策略回测"])

    with tab1:
        _render_tab_signals()
    with tab2:
        _render_tab_macro()
    with tab3:
        _render_tab_factor_ic()
    with tab4:
        _render_tab_backtest()


if __name__ == "__main__":
    st.set_page_config(page_title="策略中心", layout="wide")
    render_strategy_center_page()
