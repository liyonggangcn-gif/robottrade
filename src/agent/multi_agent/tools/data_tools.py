"""
DataAgent 工具集

封装所有数据查询为 LangChain Tool，供 Agent 调用。
"""
import json
from datetime import datetime, timedelta
from typing import Optional, List, Any
from langchain_core.tools import tool

from src.utils.db_utils import DBUtils


@tool
def query_market_summary(trade_date: str) -> str:
    """查询市场概况（大盘指数、当日涨跌家数）

    Args:
        trade_date: 交易日期，格式 YYYY-MM-DD
    """
    try:
        df = DBUtils.query_df("""
            SELECT COUNT(*) as stock_count,
                   SUM(CASE WHEN prev_close IS NOT NULL AND sd.close > prev_close THEN 1 ELSE 0 END) as up_count,
                   SUM(CASE WHEN prev_close IS NOT NULL AND sd.close < prev_close THEN 1 ELSE 0 END) as down_count,
                   AVG((sd.close - prev_close) / prev_close * 100) as avg_pct
            FROM stock_daily sd
            LEFT JOIN (
                SELECT ts_code, close as prev_close
                FROM stock_daily
                WHERE trade_date = (
                    SELECT MAX(trade_date) FROM stock_daily WHERE trade_date < ?
                )
            ) prev ON sd.ts_code = prev.ts_code
            WHERE sd.trade_date = ?
        """, (trade_date, trade_date))
        if df.empty:
            return f"无 {trade_date} 市场数据"
        r = df.iloc[0]
        return (f"{trade_date} 市场概况："
                f"上涨 {int(r['up_count'])} 家 / 下跌 {int(r['down_count'])} 家，"
                f"涨停 {int(r['limit_up'])} 家 / 跌停 {int(r['limit_down'])} 家，"
                f"平均涨幅 {r['avg_pct']:.2f}%")
    except Exception as e:
        return f"查询失败: {e}"


@tool
def query_northbound_flow(days: int = 5) -> str:
    """查询北向资金流向（近N日）

    Args:
        days: 查询天数，默认5
    """
    try:
        df = DBUtils.query_df("""
            SELECT trade_date, north_net_inflow, north_acc_inflow
            FROM northbound_flow
            ORDER BY trade_date DESC
            LIMIT ?
        """, (days,))
        if df.empty:
            return "暂无北向资金数据"
        lines = []
        for _, row in df.iterrows():
            direction = "净流入" if row['north_net_inflow'] >= 0 else "净流出"
            lines.append(
                f"{row['trade_date']}: {abs(row['north_net_inflow']):.1f}亿 {direction}，累计 {row['north_acc_inflow']:.1f}亿"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"查询失败: {e}"


@tool
def query_stock_daily(
    ts_code: str,
    start_date: str,
    end_date: str
) -> str:
    """查询个股日线数据

    Args:
        ts_code: 股票代码，如 000001.SZ
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD
    """
    try:
        df = DBUtils.query_df("""
            SELECT trade_date, open, high, low, close, vol, amount,
                   pct_chg, pe_ttm, pb, total_mv
            FROM stock_daily
            WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT 30
        """, (ts_code, start_date, end_date))
        if df.empty:
            return f"无 {ts_code} 在 {start_date}~{end_date} 的数据"
        return df.to_string(index=False)
    except Exception as e:
        return f"查询失败: {e}"


@tool
def query_stock_factors(ts_code: str, trade_date: str) -> str:
    """查询个股技术因子（最新）

    Args:
        ts_code: 股票代码
        trade_date: 查询日期 YYYY-MM-DD
    """
    try:
        df = DBUtils.query_df("""
            SELECT * FROM stock_factors
            WHERE ts_code = ? AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 1
        """, (ts_code, trade_date))
        if df.empty:
            return f"无 {ts_code} 因子数据"
        row = df.iloc[0]
        factor_cols = [c for c in df.columns if c not in ('trade_date', 'ts_code')]
        lines = [f"{ts_code} 因子 ({row['trade_date']}):"]
        for col in factor_cols:
            v = row[col]
            if v is not None and str(v) != 'nan':
                lines.append(f"  {col}: {float(v):.4f}")
        return "\n".join(lines)
    except Exception as e:
        return f"查询失败: {e}"


@tool
def query_hot_concepts(trade_date: str, top_n: int = 10) -> str:
    """查询热门概念板块

    Args:
        trade_date: 交易日期 YYYY-MM-DD
        top_n: 返回前N个，默认10
    """
    try:
        prev_cte = f"""
            SELECT p1.ts_code, p1.trade_date, p1.close
            FROM stock_daily p1
            INNER JOIN (
                SELECT ts_code, MAX(trade_date) as max_date
                FROM stock_daily
                WHERE trade_date < '{trade_date}'
                GROUP BY ts_code
            ) p2 ON p1.ts_code COLLATE utf8mb4_general_ci = p2.ts_code COLLATE utf8mb4_general_ci
                AND p1.trade_date = p2.max_date
        """
        df = DBUtils.query_df(f"""
            SELECT sc.concept_name, COUNT(DISTINCT sc.ts_code) as stock_count,
                   AVG((sd.close - prev.close) / prev.close * 100) as avg_pct
            FROM stock_concepts sc
            JOIN stock_daily sd ON sc.ts_code COLLATE utf8mb4_general_ci = sd.ts_code COLLATE utf8mb4_general_ci
                AND sd.trade_date = '{trade_date}'
            LEFT JOIN ({prev_cte}) prev ON sd.ts_code COLLATE utf8mb4_general_ci = prev.ts_code COLLATE utf8mb4_general_ci
            WHERE prev.close > 0
            GROUP BY sc.concept_name
            HAVING COUNT(DISTINCT sc.ts_code) >= 3
            ORDER BY avg_pct DESC
            LIMIT {top_n}
        """)
        if df.empty:
            return f"无 {trade_date} 概念数据"
        lines = [f"热门概念（{trade_date}）:"]
        for i, (_, row) in enumerate(df.iterrows(), 1):
            lines.append(f"  {i}. {row['concept_name']}: {int(row['stock_count'])}只 平均涨幅 {row['avg_pct']:.2f}%")
        return "\n".join(lines)
    except Exception as e:
        return f"查询失败: {e}"


@tool
def query_daily_picks(trade_date: str, limit: int = 30) -> str:
    """查询量化选股结果

    Args:
        trade_date: 交易日期 YYYY-MM-DD（内部转为 YYYYMMDD）
        limit: 返回数量，默认30
    """
    try:
        date_compact = trade_date.replace('-', '')
        df = DBUtils.query_df("""
            SELECT ts_code, name, final_score, track, industry
            FROM daily_picks
            WHERE trade_date = ?
            ORDER BY final_score DESC
            LIMIT ?
        """, (date_compact, limit))
        if df.empty:
            prev = (datetime.strptime(trade_date, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y%m%d')
            df = DBUtils.query_df("""
                SELECT ts_code, name, final_score, track, industry
                FROM daily_picks
                WHERE trade_date = ?
                ORDER BY final_score DESC
                LIMIT ?
            """, (prev, limit))
            if df.empty:
                return "无可用选股结果"
        return df.to_string(index=False)
    except Exception as e:
        return f"查询失败: {e}"


@tool
def query_portfolio() -> str:
    """查询当前持仓"""
    try:
        df = DBUtils.query_df("""
            SELECT ts_code, name, volume, avg_cost, current_price,
                   profit_pct, holding_days
            FROM positions
            WHERE volume > 0
            ORDER BY profit_pct DESC
        """)
        if df.empty:
            return "当前无持仓"
        return df.to_string(index=False)
    except Exception as e:
        return f"查询失败: {e}"


@tool
def query_ai_predictions(trade_date: str, limit: int = 20) -> str:
    """查询AI预测评分

    Args:
        trade_date: 交易日期 YYYY-MM-DD（内部转为 YYYYMMDD）
        limit: 返回数量，默认20
    """
    try:
        date_compact = trade_date.replace('-', '')
        prev_cte = f"""
            SELECT p1.ts_code, p1.trade_date, p1.close
            FROM stock_daily p1
            INNER JOIN (
                SELECT ts_code, MAX(trade_date) as max_date
                FROM stock_daily
                WHERE trade_date < '{date_compact}'
                GROUP BY ts_code
            ) p2 ON p1.ts_code COLLATE utf8mb4_general_ci = p2.ts_code COLLATE utf8mb4_general_ci
                AND p1.trade_date = p2.max_date
        """
        df = DBUtils.query_df(f"""
            SELECT ap.ts_code, ap.ai_score, ap.pred_rank,
                   sd.close, sd.pe_ttm, sd.roe,
                   (sd.close - prev.close) / prev.close * 100 as pct_chg
            FROM ai_predictions ap
            LEFT JOIN stock_daily sd ON ap.ts_code COLLATE utf8mb4_general_ci = sd.ts_code COLLATE utf8mb4_general_ci
                AND sd.trade_date = '{date_compact}'
            LEFT JOIN ({prev_cte}) prev ON ap.ts_code COLLATE utf8mb4_general_ci = prev.ts_code COLLATE utf8mb4_general_ci
            WHERE ap.trade_date = '{date_compact}'
            ORDER BY ap.ai_score DESC
            LIMIT {limit}
        """)
        if df.empty:
            return "无AI预测数据"
        return df.to_string(index=False)
    except Exception as e:
        return f"查询失败: {e}"


def get_data_tools() -> List[Any]:
    """获取所有数据查询工具"""
    return [
        query_market_summary,
        query_northbound_flow,
        query_stock_daily,
        query_stock_factors,
        query_hot_concepts,
        query_daily_picks,
        query_portfolio,
        query_ai_predictions,
    ]
