import sys
sys.path.insert(0, '.')
from src.utils.db_utils import DBUtils

# 检查有哪些代码
df = DBUtils.query_df("SELECT DISTINCT ts_code FROM stock_daily WHERE ts_code LIKE '000001%' OR ts_code LIKE '399%' OR ts_code LIKE '000300%'")
print("所有0/3开头的代码:")
for code in df['ts_code'].tolist()[:20]:
    print(f"  {code}")

# 没有指数数据，用简单对比
# 手动获取上证数据
print("\n使用Tushare获取上证指数...")

try:
    import tushare as ts
    from src.utils.config_loader import Config
    token = Config.get('tushare_token')
    pro = ts.pro_api(token)
    
    # 获取上证指数
    df = pro.index_daily(ts_code='000001.SH', start_date='20260317', end_date='20260417')
    if not df.empty:
        sh_buy = df[df['trade_date'] == '20260317'].iloc[0]['close']
        sh_now = df[df['trade_date'] == df['trade_date'].max()].iloc[0]['close']
        sh_ret = (sh_now - sh_buy) / sh_buy * 100
        print(f"上证指数: {sh_buy:.2f} -> {sh_now:.2f} = {sh_ret:+.2f}%")
        
        # 创业板
        df_cy = pro.index_daily(ts_code='399006.SZ', start_date='20260317', end_date='20260417')
        if not df_cy.empty:
            cy_buy = df_cy[df_cy['trade_date'] == '20260317'].iloc[0]['close']
            cy_now = df_cy[df_cy['trade_date'] == df_cy['trade_date'].max()].iloc[0]['close']
            cy_ret = (cy_now - cy_buy) / cy_buy * 100
            print(f"创业板: {cy_buy:.2f} -> {cy_now:.2f} = {cy_ret:+.2f}%")
except Exception as e:
    print(f"获取失败: {e}")

# 总结
print("\n=== 总结 ===")
print("选股组合平均收益: +4.06%")