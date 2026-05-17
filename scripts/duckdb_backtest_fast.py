#!/usr/bin/env python3
"""快速回测 - 向量化版本"""
import duckdb
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta

DB_PATH = '/home/li/robottrade/data/quant_backtest.duckdb'

def main():
    t0 = time.time()
    conn = duckdb.connect(DB_PATH)
    
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
    load_start = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')
    
    # Get actual max date from DB
    actual_max = conn.execute('SELECT MAX(trade_date) FROM stock_daily').fetchone()[0]
    if actual_max:
        end_date = str(actual_max).strip()
        start_dt = datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=365)
        start_date = start_dt.strftime('%Y-%m-%d')
        load_start = (start_dt - timedelta(days=40)).strftime('%Y-%m-%d')
    
    print(f'加载数据 {load_start} ~ {end_date}...')
    daily = conn.execute(f"""
        SELECT sd.trade_date, sd.ts_code,
               COALESCE(NULLIF(si.name,''), sd.ts_code) AS name,
               sd.close, sd.pe_ttm, sd.roe, sd.gpr, sd.netprofit_yoy,
               COALESCE(si.total_mv, sd.total_mv, 0) AS total_mv,
               si.industry
        FROM stock_daily sd
        LEFT JOIN stock_info si ON sd.ts_code = si.ts_code
        WHERE sd.trade_date >= '{load_start}' AND sd.trade_date <= '{end_date}'
          AND sd.close IS NOT NULL AND sd.close > 0
    """).fetchdf()
    daily['trade_date'] = daily['trade_date'].astype(str).str.strip()
    for c in ['close','pe_ttm','roe','gpr','netprofit_yoy','total_mv']:
        daily[c] = pd.to_numeric(daily[c], errors='coerce')
    
    try:
        factors = conn.execute(f"""
            SELECT trade_date, ts_code, mom_20 FROM stock_factors
            WHERE trade_date >= '{load_start}' AND trade_date <= '{end_date}'
        """).fetchdf()
        factors['trade_date'] = factors['trade_date'].astype(str).str.strip()
    except:
        factors = pd.DataFrame(columns=['trade_date','ts_code','mom_20'])
    
    daily = daily.merge(factors, on=['trade_date','ts_code'], how='left')
    
    # Compute extra factors vectorized
    print('计算因子...')
    daily = daily.sort_values(['ts_code','trade_date'])
    daily['ret_1d'] = daily.groupby('ts_code')['close'].pct_change()
    daily['vol_20'] = daily.groupby('ts_code')['ret_1d'].transform(lambda x: x.rolling(20, min_periods=10).std())
    daily['ret_5'] = daily.groupby('ts_code')['close'].pct_change(5)
    daily['ret_20'] = daily.groupby('ts_code')['close'].pct_change(20)
    
    print(f'交易日: {daily["trade_date"].nunique()}, 股票: {daily["ts_code"].nunique()}')
    
    # Filter to backtest period
    bt = daily[(daily['trade_date'] >= start_date) & (daily['trade_date'] <= end_date)].copy()
    trade_dates = sorted(bt['trade_date'].unique())
    print(f'回测交易日: {len(trade_dates)}')
    
    # Market sentiment
    clean = daily[~daily['name'].str.contains('ST|退', na=False)]
    clean = clean.sort_values(['ts_code','trade_date'])
    clean['ret'] = clean.groupby('ts_code')['close'].pct_change()
    daily_mkt = clean.groupby('trade_date')['ret'].mean()
    sentiment = {}
    for d in trade_dates:
        r = daily_mkt.get(d, 0)
        if pd.isna(r): sentiment[d] = 1.0
        elif r < -0.02: sentiment[d] = 0.0
        elif r < -0.01: sentiment[d] = 0.5
        elif r > 0.015: sentiment[d] = 1.2
        else: sentiment[d] = 1.0
    
    strategies = {
        'A.技术动量': lambda df: (
            np.clip(df['mom_20'].fillna(0), 0, 1) * 0.35 +
            np.clip((df['ret_20'].fillna(0) + 0.1) / 0.5, 0, 1) * 0.25 +
            np.clip(df['roe'].fillna(0) / 20, 0, 1) * 0.10 +
            ((df['total_mv']/1e8).between(20, 200) * 0.15 + (df['total_mv']/1e8 < 20) * 0.10 + (df['total_mv']/1e8).between(200, 500) * 0.05)
        ),
        'B.价值质量': lambda df: (
            np.clip(df['roe'].fillna(0) / 15, 0, 1) * 0.30 +
            np.clip(df['netprofit_yoy'].fillna(0) / 50, 0, 1) * 0.25 +
            np.clip(df['gpr'].fillna(0) / 40, 0, 1) * 0.20 +
            np.clip(1 - df['pe_ttm'].fillna(99) / 50, 0, 1) * 0.25
        ),
        'C.低波动': lambda df: (
            np.clip(1 - df['vol_20'].fillna(0.05) / 0.05, 0, 1) * 0.50 +
            np.clip((df['ret_20'].fillna(0) + 0.05) / 0.3, 0, 1) * 0.30 +
            np.clip(df['roe'].fillna(0) / 15, 0, 1) * 0.20
        ),
        'D.短期反转': lambda df: (
            ((df['ret_5'].fillna(0) >= -0.15) & (df['ret_5'].fillna(0) <= -0.03)) * 0.50 +
            (df['ret_5'].fillna(0) < -0.15) * 0.30 +
            ((df['ret_5'].fillna(0) > -0.03) & (df['ret_5'].fillna(0) < 0)) * 0.30 +
            np.clip(df['roe'].fillna(0) / 15, 0, 1) * 0.30 +
            np.clip(1 - df['pe_ttm'].fillna(99) / 40, 0, 1) * 0.20
        ),
        'E.行业轮动': lambda df: (
            np.clip((df['ret_20'].fillna(0) + 0.1) / 0.4, 0, 1) * 0.40 +
            np.clip(df['roe'].fillna(0) / 15, 0, 1) * 0.30 +
            np.clip((df['ret_5'].fillna(0) + 0.05) / 0.2, 0, 1) * 0.30
        ),
        'F.回调企稳': lambda df: (
            np.clip(df['ret_20'].fillna(0) / 0.4 + 0.25, 0, 1) * 0.25 +
            np.clip(df['roe'].fillna(0) / 15, 0, 1) * 0.25 +
            np.clip((df['ret_5'].fillna(0) + 0.1) / 0.4, 0, 1) * 0.50
        ),
    }
    
    cost_rate = 0.0003
    top_k = 10
    rebalance = 5
    
    print('\n运行策略...')
    all_results = {}
    
    for label, scorer in strategies.items():
        t1 = time.time()
        nav = [1.0]
        bm_nav = [1.0]
        total_wins = 0
        total_loses = 0
        n_periods = 0
        
        rebal_dates = trade_dates[::rebalance]
        
        for i, sel_date in enumerate(rebal_dates):
            # Find the index of sel_date in trade_dates
            sel_idx = i * rebalance
            exit_idx = sel_idx + rebalance
            if exit_idx >= len(trade_dates):
                break
            exit_date = trade_dates[exit_idx]
            
            day_data = bt[bt['trade_date'] == sel_date].copy()
            if len(day_data) == 0:
                nav.append(nav[-1])
                bm_nav.append(bm_nav[-1])
                continue
            
            scale = sentiment.get(sel_date, 1.0)
            eff_k = max(1, int(top_k * scale))
            
            scores = scorer(day_data)
            day_data['score'] = scores
            picks = day_data.nlargest(eff_k, 'score')
            
            # Calculate returns
            exit_prices = bt[(bt['trade_date'] == exit_date) & (bt['ts_code'].isin(picks['ts_code']))][['ts_code','close']]
            merged = picks[['ts_code','close','name']].merge(exit_prices, on='ts_code', how='left', suffixes=('_buy','_sell'))
            merged['ret'] = (merged['close_sell'] - merged['close_buy']) / merged['close_buy']
            merged['ret'] = merged['ret'].fillna(0) - cost_rate
            
            port_ret = merged['ret'].mean()
            wins = (merged['ret'] > 0).sum()
            loses = (merged['ret'] <= 0).sum()
            total_wins += wins
            total_loses += loses
            n_periods += 1
            
            nav.append(nav[-1] * (1 + port_ret))
            
            # Benchmark
            bm_ret = daily_mkt.get(exit_date, 0)
            if pd.isna(bm_ret): bm_ret = 0
            bm_nav.append(bm_nav[-1] * (1 + bm_ret))
        
        total_ret = nav[-1] / nav[0] - 1
        bm_total = bm_nav[-1] / bm_nav[0] - 1
        n_days = len(trade_dates)
        annual_ret = (1 + total_ret) ** (252 / max(n_days, 1)) - 1
        
        rets = np.diff(nav) / np.array(nav[:-1])
        sharpe = np.mean(rets) / np.std(rets) * np.sqrt(len(rets)) if np.std(rets) > 0 else 0
        
        peak = nav[0]; max_dd = 0
        for v in nav:
            if v > peak: peak = v
            dd = (peak - v) / peak
            if dd > max_dd: max_dd = dd
        
        win_rate = total_wins / max(total_wins + total_loses, 1)
        
        elapsed = time.time() - t1
        print(f'  {label:15s} ({elapsed:.1f}s) | 收益: {total_ret:+7.2f}% | 年化: {annual_ret:+7.2f}% | 回撤: {max_dd:6.2f}% | 夏普: {sharpe:6.2f} | 胜率: {win_rate*100:5.1f}%')
        all_results[label] = {'total_ret': total_ret, 'annual_ret': annual_ret, 'max_dd': max_dd, 'sharpe': sharpe, 'win_rate': win_rate, 'bm_total': bm_total}
    
    print(f'\n{"="*90}')
    print(f'回测结果: {start_date} ~ {end_date} | {len(trade_dates)}交易日 | 换仓5日 | 选股10只')
    print(f'{"="*90}')
    for label, m in all_results.items():
        print(f'  {label:15s} | 收益: {m["total_ret"]:+7.2f}% | 年化: {m["annual_ret"]:+7.2f}% | 回撤: {m["max_dd"]:6.2f}% | 夏普: {m["sharpe"]:6.2f} | 胜率: {m["win_rate"]*100:5.1f}% | 基准: {m["bm_total"]:+6.2f}%')
    
    print(f'\n总耗时: {time.time()-t0:.1f}s')
    conn.close()

if __name__ == '__main__':
    main()
