"""
仓位管理页面
"""
import streamlit as st
import pandas as pd
import sys
import os

# 添加项目根目录到路径
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from src.portfolio.position_manager import PositionManager
from src.utils.db_utils import DBUtils


def render_portfolio_page():
    """渲染仓位管理页面"""
    st.header("💼 仓位管理")
    
    # 初始化仓位管理器
    pm = PositionManager()
    
    # Tab切换
    tab1, tab2, tab3, tab4 = st.tabs(["持仓概况", "换仓决策", "交易历史", "仓位设置"])
    
    # ========== Tab 1: 持仓概况 ==========
    with tab1:
        render_positions_tab(pm)
    
    # ========== Tab 2: 换仓决策 ==========
    with tab2:
        render_holding_decision_tab()
    
    # ========== Tab 3: 交易历史 ==========
    with tab3:
        render_transactions_tab()
    
    # ========== Tab 4: 仓位设置 ==========
    with tab4:
        render_settings_tab(pm)


def render_holding_decision_tab():
    """渲染换仓决策Tab（HoldingManager）"""
    st.subheader("🔄 换仓决策")
    
    from src.portfolio.holding_manager import HoldingManager
    from datetime import datetime
    
    hm = HoldingManager()
    trade_date = datetime.now().strftime('%Y-%m-%d')
    
    # 获取最新选股结果作为新信号
    from src.utils.db_utils import DBUtils
    signals_df = DBUtils.query_df("""
        SELECT ts_code, name, score, strategy
        FROM strategy_signals
        WHERE trade_date = (SELECT MAX(trade_date) FROM strategy_signals)
        ORDER BY score DESC
    """)
    
    if signals_df.empty:
        st.info("暂无选股信号，请先运行策略生成信号")
        return
    
    st.caption(f"信号日期：{trade_date} | 候选股票：{len(signals_df)} 只")
    
    # 运行换仓决策
    decision = hm.decide(signals_df, trade_date=trade_date)
    
    # 显示决策摘要
    summary = decision.summary
    st.markdown(f"**{summary.get('date', '')}** | "
                f"买入 {summary.get('buy', 0)} 只 | "
                f"卖出 {summary.get('sell', 0)} 只 | "
                f"持有 {summary.get('hold', 0)} 只")
    
    # 买入列表
    if decision.buy_list:
        st.subheader("🟢 建议买入")
        buy_df = pd.DataFrame(decision.buy_list)
        st.dataframe(buy_df, use_container_width=True, hide_index=True)
    
    # 卖出列表
    if decision.sell_list:
        st.subheader("🔴 建议卖出")
        sell_df = pd.DataFrame(decision.sell_list)
        st.dataframe(sell_df, use_container_width=True, hide_index=True)
    
    # 持有列表
    if decision.hold_list:
        st.subheader("🔵 建议持有")
        hold_df = pd.DataFrame(decision.hold_list)
        if 'protected' in hold_df.columns:
            hold_df['保护期'] = hold_df['protected'].apply(lambda x: '✅ 是' if x else '')
        st.dataframe(hold_df, use_container_width=True, hide_index=True)
    
    # 强制止损
    if decision.forced_sell:
        st.subheader("🚨 强制止损")
        for item in decision.forced_sell:
            st.error(f"{item.get('name', '')} ({item.get('ts_code', '')}): "
                     f"亏损 {item.get('pnl_pct', 0)*100:.1f}%")


def render_positions_tab(pm: PositionManager):
    """渲染持仓概况Tab"""
    
    # 获取持仓汇总
    summary = pm.get_position_summary()
    
    # 资金概况
    st.subheader("📊 账户概况")
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        st.metric(
            "总资金", 
            f"{pm.total_capital:,.0f} 元",
            help="账户总资金"
        )
    
    with col2:
        st.metric(
            "持仓市值", 
            f"{summary['total_value']:,.0f} 元",
            help="当前持仓总市值"
        )
    
    with col3:
        pl_pct = summary['total_profit_loss_pct'] * 100
        st.metric(
            "浮动盈亏", 
            f"{summary['total_profit_loss']:+,.0f} 元",
            f"{pl_pct:+.2f}%",
            delta_color="normal" if summary['total_profit_loss'] >= 0 else "inverse"
        )
    
    with col4:
        st.metric(
            "当前仓位", 
            f"{summary['total_position_pct']*100:.1f}%",
            help="持仓市值占总资金比例"
        )
    
    with col5:
        st.metric(
            "剩余现金", 
            f"{summary['cash']:,.0f} 元",
            help="可用于新建仓的现金"
        )
    
    # 持仓明细
    st.divider()
    st.subheader("📋 持仓明细")
    
    if summary['stock_count'] == 0:
        st.info("当前无持仓")
        return
    
    positions_df = pm.get_current_positions()
    
    # 构建显示表格
    display_df = pd.DataFrame({
        '股票代码': positions_df['ts_code'],
        '股票名称': positions_df['name'],
        '持仓数量': positions_df['shares'].apply(lambda x: f"{int(x)}股"),
        '成本价': positions_df['avg_cost'].apply(lambda x: f"{x:.2f}"),
        '现价': positions_df['current_price'].apply(lambda x: f"{x:.2f}"),
        '市值': positions_df['market_value'].apply(lambda x: f"{x:,.0f}"),
        '盈亏': positions_df['profit_loss'].apply(lambda x: f"{x:+,.0f}"),
        '盈亏比例': positions_df['profit_loss_pct'].apply(lambda x: f"{x*100:+.2f}%"),
        '仓位占比': positions_df['position_pct'].apply(lambda x: f"{x*100:.1f}%"),
        '止损价': positions_df['stop_loss_price'].apply(lambda x: f"{x:.2f}"),
        '止盈价': positions_df['take_profit_price'].apply(lambda x: f"{x:.2f}"),
        '买入日期': positions_df['buy_date'],
    })
    
    # 行着色逻辑
    def highlight_row(row):
        pl = float(row['盈亏比例'].replace('%', ''))
        if pl > 0:
            return ['background-color: rgba(0, 200, 83, 0.15)'] * len(row)
        elif pl < 0:
            return ['background-color: rgba(255, 0, 0, 0.1)'] * len(row)
        return [''] * len(row)
    
    styled_df = display_df.style.apply(highlight_row, axis=1)
    st.dataframe(styled_df, use_container_width=True, hide_index=True)
    
    # 风控提醒
    st.divider()
    st.subheader("⚠️ 风控提醒")
    
    stop_loss_list, take_profit_list = pm.check_stop_loss_take_profit()
    
    col1, col2 = st.columns(2)
    
    with col1:
        if stop_loss_list:
            st.error(f"🚨 {len(stop_loss_list)} 只股票触发止损")
            for stock in stop_loss_list:
                st.write(f"- {stock['name']} ({stock['ts_code']})")
                st.write(f"  现价 {stock['current_price']:.2f} <= 止损价 {stock['stop_loss_price']:.2f}")
        else:
            st.success("✅ 无止损触发")
    
    with col2:
        if take_profit_list:
            st.warning(f"🎯 {len(take_profit_list)} 只股票触发止盈")
            for stock in take_profit_list:
                st.write(f"- {stock['name']} ({stock['ts_code']})")
                st.write(f"  现价 {stock['current_price']:.2f} >= 止盈价 {stock['take_profit_price']:.2f}")
        else:
            st.info("📊 无止盈触发")


def render_transactions_tab():
    """渲染交易历史Tab"""
    st.subheader("📜 交易历史")
    
    # 查询交易记录
    sql = "SELECT * FROM transactions ORDER BY trade_date DESC, id DESC LIMIT 100"
    transactions_df = DBUtils.query_df(sql)
    
    if transactions_df.empty:
        st.info("暂无交易记录")
        return
    
    # 构建显示表格
    display_df = pd.DataFrame({
        '交易日期': transactions_df['trade_date'],
        '操作': transactions_df['action'],
        '股票': transactions_df['name'],
        '代码': transactions_df['ts_code'],
        '价格': transactions_df['price'].apply(lambda x: f"{x:.2f}"),
        '数量': transactions_df['shares'].apply(lambda x: f"{int(x)}股"),
        '金额': transactions_df['amount'].apply(lambda x: f"{x:,.0f}"),
        '手续费': transactions_df['commission'].apply(lambda x: f"{x:.2f}"),
        '策略': transactions_df['strategy'],
        '备注': transactions_df['notes']
    })
    
    # 行着色
    def highlight_action(row):
        if row['操作'] == 'BUY':
            return ['background-color: rgba(0, 200, 83, 0.1)'] * len(row)
        elif row['操作'] == 'SELL':
            return ['background-color: rgba(255, 0, 0, 0.1)'] * len(row)
        return [''] * len(row)
    
    styled_df = display_df.style.apply(highlight_action, axis=1)
    st.dataframe(styled_df, use_container_width=True, hide_index=True)


def render_settings_tab(pm: PositionManager):
    """渲染仓位设置Tab"""
    st.subheader("⚙️ 仓位管理参数")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("### 资金管理")
        st.metric("总资金", f"{pm.total_capital:,.0f} 元")
        st.caption("💡 在 config/settings.yaml 中修改 `portfolio.total_capital`")
        
        st.markdown("### 仓位控制")
        st.metric("单只最大仓位", f"{pm.max_position_pct*100:.0f}%")
        st.metric("最大总仓位", f"{pm.max_total_position*100:.0f}%")
        st.caption("💡 在 config/settings.yaml 中修改 `portfolio.max_position_pct` 和 `portfolio.max_total_position`")
    
    with col2:
        st.markdown("### 风控参数")
        st.metric("止损比例", f"{pm.stop_loss_pct*100:.1f}%")
        st.metric("止盈比例", f"{pm.take_profit_pct*100:.1f}%")
        st.caption("💡 在 config/settings.yaml 中修改 `portfolio.stop_loss_pct` 和 `portfolio.take_profit_pct`")
        
        st.markdown("### 分配方法")
        from src.utils.config_loader import Config
        method = Config.get('portfolio.position_method') or 'tiered'
        method_names = {
            'equal': '等权分配',
            'proportional': '按评分比例',
            'tiered': '分层分配'
        }
        st.metric("当前方法", method_names.get(method, method))
        st.caption("💡 在 config/settings.yaml 中修改 `portfolio.position_method`")
    
    # 配置说明
    st.divider()
    st.markdown("""
    ### 📖 参数说明
    
    **资金管理**:
    - `total_capital`: 账户总资金（元）
    
    **仓位控制**:
    - `max_position_pct`: 单只股票最大仓位比例（防止过度集中）
    - `max_total_position`: 总资金最大使用比例（建议保留20%现金）
    
    **风控参数**:
    - `stop_loss_pct`: 止损比例（建议5-10%）
    - `take_profit_pct`: 止盈比例（建议15-30%）
    
    **分配方法**:
    - `equal`: 等权分配（所有股票平均分配仓位）
    - `proportional`: 按评分比例（评分越高仓位越大）
    - `tiered`: 分层分配（Top 30%高仓位，Middle 40%中仓位，Bottom 30%低仓位）
    
    """)


if __name__ == "__main__":
    st.set_page_config(page_title="仓位管理", page_icon="💼", layout="wide")
    render_portfolio_page()
