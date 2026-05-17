import sys
sys.path.insert(0, '.')
from src.utils.db_utils import DBUtils
import pandas as pd
from datetime import datetime

print("=" * 70)
print("选股回测详细分析")
print("=" * 70)

# 获取所有选股记录
picks = DBUtils.query_df('''
    SELECT trade_date, ts_code, name, track, final_score
    FROM daily_picks
    ORDER BY trade_date DESC, final_score DESC
''')

# 转换日期格式
def parse_date(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"

# 按策略分组分析
strategy_results = {}
all_results = []

for _, row in picks.iterrows():
    ts_code = row['ts_code']
    buy_date = parse_date(row['trade_date'])
    trade_date_str = row['trade_date']
    
    # 买入价格
    df_buy = DBUtils.query_df('''
        SELECT close FROM stock_daily 
        WHERE ts_code = %s AND trade_date = %s
    ''', (ts_code, buy_date))
    if df_buy.empty:
        continue
    buy_price = df_buy.iloc[0]['close']
    
    # 当前价格
    df_now = DBUtils.query_df('''
        SELECT close FROM stock_daily 
        WHERE ts_code = %s
        ORDER BY trade_date DESC LIMIT 1
    ''', (ts_code,))
    if df_now.empty:
        continue
    now_price = df_now.iloc[0]['close']
    
    ret = (now_price - buy_price) / buy_price * 100
    
    record = {
        'ts_code': ts_code,
        'name': row['name'],
        'track': row['track'],
        'buy_date': buy_date,
        'trade_date_str': trade_date_str,
        'buy_price': buy_price,
        'now_price': now_price,
        'return_pct': ret,
        'score': row['final_score']
    }
    all_results.append(record)
    
    # 按策略汇总
    track = row['track']
    if track not in strategy_results:
        strategy_results[track] = []
    strategy_results[track].append(record)

# 转DataFrame
df_all = pd.DataFrame(all_results)

print("\n" + "=" * 70)
print("一、各策略表现")
print("=" * 70)

for track, stocks in strategy_results.items():
    df = pd.DataFrame(stocks)
    if len(df) == 0:
        continue
    
    win = (df['return_pct'] > 0).sum()
    total = len(df)
    avg = df['return_pct'].mean()
    top = df['return_pct'].max()
    bottom = df['return_pct'].min()
    
    print(f"\n【{track}】共{total}只:")
    print(f"  平均收益: {avg:+.2f}%  胜率: {win/total*100:.1f}%")
    print(f"  最高: {top:+.2f}%  最低: {bottom:+.2f}%")

print("\n" + "=" * 70)
print("二、所有股票详情（按盈亏排序）")
print("=" * 70)

# 按盈亏排序
df_sorted = df_all.sort_values('return_pct', ascending=False)

# 盈利的
print("\n【盈利股票】")
winners = df_sorted[df_sorted['return_pct'] > 0]
for _, r in winners.head(15).iterrows():
    print(f"  {r['ts_code']} {r['name'][:8]:8s} {r['track']:15s} 买{r['buy_price']:6.2f} -> 现{r['now_price']:6.2f} = {r['return_pct']:+.2f}% score={r['score']:.2f}")

# 亏损的  
print("\n【亏损股票】")
losers = df_sorted[df_sorted['return_pct'] <= 0]
for _, r in losers.sort_values('return_pct').head(10).iterrows():
    print(f"  {r['ts_code']} {r['name'][:8]:8s} {r['track']:15s} 买{r['buy_price']:6.2f} -> 现{r['now_price']:6.2f} = {r['return_pct']:+.2f}% score={r['score']:.2f}")

# 汇总
print("\n" + "=" * 70)
print("三、汇总统计")
print("=" * 70)

total = len(df_all)
win = len(winners)
loss = len(losers)
avg = df_all['return_pct'].mean()
median = df_all['return_pct'].median()

print(f"总股票数: {total}")
print(f"盈利: {win}只 ({win/total*100:.1f}%)")
print(f"亏损: {loss}只 ({loss/total*100:.1f}%)")
print(f"平均收益: {avg:+.2f}%")
print(f"中位数: {median:+.2f}%")

# 多策略对比
print("\n【策略收益对比】")
for track, stocks in strategy_results.items():
    df = pd.DataFrame(stocks)
    if len(df) >= 3:
        avg = df['return_pct'].mean()
        print(f"  {track:15s}: {avg:+.2f}% ({len(df)}只)")

print("\n" + "=" * 70)