#!/usr/bin/env python3
"""
热点板块检测器 - 详细版本
通过涨停板数据判断热门板块，支持多维度分析
"""
from src.utils.db_utils import DBUtils
from typing import List, Dict, Any


def get_hot_sectors_detailed(trade_date: str) -> Dict[str, Any]:
    """
    获取详细的热门板块分析
    
    Returns:
        {
            'limitup_count': int,           # 涨停总数
            'concept_sectors': list,        # 热门概念板块（Top10）
            'industry_sectors': list,        # 热门行业板块（Top10）
            'early_limitup': list,           # 早盘涨停（9:30-10:00）
            'mid_limitup': list,            # 午盘涨停（10:00-14:00）
            'close_limitup': list,          # 尾盘涨停（14:00-15:00）
            'consec_limitup': list,         # 连续涨停股
            'sector_details': list,        # 板块详情（含涨停股明细）
        }
    """
    # 1. 获取今日行情数据
    today_df = DBUtils.query_df(
        "SELECT ts_code, close, vol FROM stock_daily WHERE trade_date = %s",
        (trade_date,)
    )
    if today_df.empty:
        return {'error': 'No data for date', 'limitup_count': 0}
    
    # 2. 获取昨日收盘价
    prev_df = DBUtils.query_df("""
        SELECT t1.ts_code, t1.close as prev_close
        FROM stock_daily t1
        INNER JOIN (
            SELECT ts_code, MAX(trade_date) as max_dt
            FROM stock_daily WHERE trade_date < %s
            GROUP BY ts_code
        ) t2 ON t1.ts_code = t2.ts_code AND t1.trade_date = t2.max_dt
    """, (trade_date,))
    if prev_df.empty:
        return {'error': 'No prev data', 'limitup_count': 0}
    
    # 3. 计算涨幅
    today_df = today_df.rename(columns={'close': 'close'})
    merged = today_df.merge(prev_df, on='ts_code', how='inner')
    merged = merged[merged['prev_close'] > 0]
    merged['pct_chg'] = (merged['close'] - merged['prev_close']) / merged['prev_close'] * 100
    
    # 4. 获取行业和概念信息
    info_df = DBUtils.query_df(
        "SELECT ts_code, industry FROM stock_info WHERE industry IS NOT NULL AND industry != ''"
    )
    merged = merged.merge(info_df, on='ts_code', how='inner')
    
    # 5. 获取概念映射
    concept_df = DBUtils.query_df("SELECT ts_code, concept_name FROM stock_concepts")
    concept_agg = concept_df.groupby('ts_code')['concept_name'].apply(list).reset_index()
    concept_agg = concept_agg.rename(columns={'concept_name': 'concepts'})
    
    merged = merged.merge(concept_agg, on='ts_code', how='left')
    merged['concepts'] = merged['concepts'].apply(lambda x: x if isinstance(x, list) else [])
    
    # 6. 筛选涨停股 (涨幅 >= 9.9%)
    limitup = merged[merged['pct_chg'] >= 9.9].copy()
    
    result = {
        'limitup_count': len(limitup),
        'limitup_stocks': [],
    }
    
    if limitup.empty:
        result['concept_sectors'] = []
        result['industry_sectors'] = []
        return result
    
    # 7. 统计概念板块（更细粒度）
    concept_list = []
    for _, row in limitup.iterrows():
        for conc in row['concepts']:
            concept_list.append(conc)
    
    if concept_list:
        from collections import Counter
        concept_counts = Counter(concept_list)
        top_concepts = sorted(concept_counts.items(), key=lambda x: -x[1])[:10]
        result['concept_sectors'] = [{'name': c[0], 'count': c[1]} for c in top_concepts]
    else:
        result['concept_sectors'] = []
    
    # 8. 统计行业板块
    industry_counts = limitup.groupby('industry').size().reset_index(name='count')
    industry_counts = industry_counts.sort_values('count', ascending=False)
    result['industry_sectors'] = [
        {'name': row['industry'], 'count': int(row['count'])}
        for _, row in industry_counts.head(10).iterrows()
    ]
    
    # 9. 涨停股明细（含概念）
    result['limitup_stocks'] = [
        {
            'ts_code': row['ts_code'],
            'pct_chg': round(row['pct_chg'], 2),
            'industry': row['industry'],
            'concepts': row['concepts'][:3] if row['concepts'] else [],  # 只保留前3个概念
        }
        for _, row in limitup.iterrows()
    ]
    
    # 10. 按概念板块聚合涨停股
    concept_stocks = {}
    for stock in result['limitup_stocks']:
        for conc in stock['concepts']:
            if conc not in concept_stocks:
                concept_stocks[conc] = []
            concept_stocks[conc].append(stock['ts_code'])
    
    result['concept_details'] = [
        {'name': name, 'count': len(stocks), 'stocks': stocks[:5]}
        for name, stocks in sorted(concept_stocks.items(), key=lambda x: -len(x[1]))[:10]
    ]
    
    return result


def get_hot_sectors_simple(trade_date: str, top_n: int = 10) -> List[str]:
    """简化版：只返回热门概念板块列表"""
    result = get_hot_sectors_detailed(trade_date)
    if 'concept_sectors' in result:
        return [c['name'] for c in result['concept_sectors'][:top_n]]
    return []


if __name__ == '__main__':
    # 自动获取最新交易日期
    latest_df = DBUtils.query_df("SELECT MAX(trade_date) as latest FROM stock_daily")
    trade_date = latest_df.iloc[0]['latest']
    print("=== 热门板块详细分析 ({}) ===".format(trade_date))
    result = get_hot_sectors_detailed(trade_date)
    print("涨停数量:", result['limitup_count'])
    
    print("\n--- 热门概念板块 ---")
    for item in result.get('concept_sectors', [])[:5]:
        print(f"  {item['name']}: {item['count']}只涨停")
    
    print("\n--- 热门行业板块 ---")
    for item in result.get('industry_sectors', [])[:5]:
        print(f"  {item['name']}: {item['count']}只涨停")
    
    print("\n--- 涨停股明细(前10) ---")
    for stock in result.get('limitup_stocks', [])[:10]:
        print(f"  {stock['ts_code']}: {stock['pct_chg']}% | {stock['concepts'][:2]}")