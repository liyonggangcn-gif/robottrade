#!/usr/bin/env python3
"""
因子IC分析 + 12策略回测 - 基于现有1年DuckDB数据
"""
import duckdb
import pandas as pd
import numpy as np
import time
import json
import os
from datetime import datetime

DB_PATH = '/home/li/robottrade/data/quant_backtest.duckdb'
COST_RATE = 0.0003

def main():
    t0 = time.time()
    conn = duckdb.connect(DB_PATH)
    
    # Check data range
    r = conn.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM stock_daily").fetchone()
    print("Data range: %s to %s, %d rows" % (r[0], r[1], r[2]))
    
    # Load data
    start_date = '2025-04-03'
    end_date = '2026-04-03'
    load_start = '2025-02-01'
    
    print("\nLoading data...")
    daily = conn.execute("""
        SELECT sd.trade_date, sd.ts_code,
               COALESCE(NULLIF(si.name,''), sd.ts_code) AS name,
               sd.close, sd.high, sd.low, sd.vol, sd.amount,
               sd.pe_ttm, sd.roe, sd.gpr, sd.netprofit_yoy,
               sd.total_mv, si.industry
        FROM stock_daily sd
        LEFT JOIN stock_info si ON sd.ts_code = si.ts_code
        WHERE sd.trade_date >= '%s' AND sd.trade_date <= '%s'
          AND sd.close IS NOT NULL AND sd.close > 0
    """ % (load_start, end_date)).fetchdf()
    daily['trade_date'] = daily['trade_date'].astype(str).str.strip()
    for c in ['close','pe_ttm','roe','gpr','netprofit_yoy','total_mv','high','low','vol','amount']:
        daily[c] = pd.to_numeric(daily[c], errors='coerce')
    
    try:
        factors = conn.execute("""
            SELECT trade_date, ts_code, mom_20, rsi_14, macd_hist, bb_width,
                   vol_ratio, atr_14, kdj_k, kdj_d
            FROM stock_factors
            WHERE trade_date >= '%s' AND trade_date <= '%s'
        """ % (load_start, end_date)).fetchdf()
        factors['trade_date'] = factors['trade_date'].astype(str).str.strip()
    except:
        factors = pd.DataFrame(columns=['trade_date','ts_code'])
    
    conn.close()
    print("Daily: %d rows, %d stocks" % (len(daily), daily['ts_code'].nunique()))
    print("Factors: %d rows" % len(factors))
    
    # Merge
    df = daily.merge(factors, on=['trade_date','ts_code'], how='left')
    df = df.sort_values(['ts_code','trade_date'])
    
    # Compute technical factors
    print("Computing factors...")
    df['ret_1d'] = df.groupby('ts_code')['close'].pct_change()
    df['ret_5'] = df.groupby('ts_code')['close'].pct_change(5)
    df['ret_20'] = df.groupby('ts_code')['close'].pct_change(20)
    df['ret_60'] = df.groupby('ts_code')['close'].pct_change(60)
    df['vol_20'] = df.groupby('ts_code')['ret_1d'].transform(lambda x: x.rolling(20, min_periods=10).std())
    df['vol_ma5'] = df.groupby('ts_code')['vol'].transform(lambda x: x.rolling(5, min_periods=3).mean())
    df['vol_ma20'] = df.groupby('ts_code')['vol'].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df['vol_ratio'] = df['vol_ma5'] / df['vol_ma20'].clip(lower=1e-6)
    df['turnover'] = df['amount'] / df['total_mv'].clip(lower=1e6)
    df['max_high_60'] = df.groupby('ts_code')['high'].transform(lambda x: x.rolling(60, min_periods=20).max())
    df['drawdown'] = (df['close'] - df['max_high_60']) / df['max_high_60'].clip(lower=1e-6)
    
    trade_dates = [d for d in sorted(df['trade_date'].unique()) if start_date <= d <= end_date]
    print("Trading days: %d, Stocks: %d" % (len(trade_dates), df['ts_code'].nunique()))
    
    # Build sentiment
    clean = df[~df['name'].str.contains('ST|退|N', na=False)]
    clean = clean.sort_values(['ts_code','trade_date'])
    clean['ret'] = clean.groupby('ts_code')['close'].pct_change()
    mkt = clean.groupby('trade_date')['ret'].mean()
    sentiment = {}
    for d in trade_dates:
        r = mkt.get(d, 0)
        if pd.isna(r): sentiment[d] = 1.0
        elif r < -0.02: sentiment[d] = 0.0
        elif r < -0.01: sentiment[d] = 0.5
        elif r > 0.015: sentiment[d] = 1.2
        else: sentiment[d] = 1.0
    
    # ================================================================
    # Step 1: Factor IC Analysis
    # ================================================================
    print("\n" + "="*70)
    print("Step 1: 因子IC分析")
    print("="*70)
    
    bt = df[(df['trade_date'] >= start_date) & (df['trade_date'] <= end_date)].copy()
    bt['fwd_ret_5'] = bt.groupby('ts_code')['close'].transform(lambda x: x.pct_change(5).shift(-5))
    bt = bt.dropna(subset=['fwd_ret_5'])
    
    factor_cols = [
        'pe_ttm', 'roe', 'gpr', 'netprofit_yoy', 'total_mv',
        'mom_20', 'rsi_14', 'macd_hist', 'bb_width', 'vol_ratio',
        'atr_14', 'kdj_k', 'kdj_d',
        'ret_5', 'ret_20', 'ret_60', 'vol_20', 'turnover', 'drawdown'
    ]
    factor_names = {
        'pe_ttm': '市盈率TTM', 'roe': 'ROE', 'gpr': '毛利率',
        'netprofit_yoy': '净利润增速', 'total_mv': '总市值',
        'mom_20': '20日动量', 'rsi_14': 'RSI', 'macd_hist': 'MACD柱',
        'bb_width': '布林带宽', 'vol_ratio': '量比',
        'atr_14': 'ATR', 'kdj_k': 'KDJ-K', 'kdj_d': 'KDJ-D',
        'ret_5': '5日收益', 'ret_20': '20日收益', 'ret_60': '60日收益',
        'vol_20': '20日波动率', 'turnover': '换手率', 'drawdown': '回撤'
    }
    
    ic_results = {}
    for col in factor_cols:
        if col not in bt.columns:
            continue
        ics = []
        for date in trade_dates:
            day_data = bt[bt['trade_date'] == date].dropna(subset=[col, 'fwd_ret_5'])
            if len(day_data) < 50:
                continue
            ic = day_data[col].rank().corr(day_data['fwd_ret_5'].rank(), method='pearson')
            if not np.isnan(ic):
                ics.append(ic)
        
        if len(ics) > 10:
            ic_arr = np.array(ics)
            ic_results[col] = {
                'name': factor_names.get(col, col),
                'mean_ic': float(np.mean(ic_arr)),
                'abs_mean_ic': float(np.mean(np.abs(ic_arr))),
                'ic_std': float(np.std(ic_arr)),
                'icir': float(np.mean(ic_arr) / np.std(ic_arr)) if np.std(ic_arr) > 0 else 0,
                'ic_positive_rate': float(np.mean(ic_arr > 0)),
                'n_periods': len(ics),
            }
    
    sorted_ic = sorted(ic_results.items(), key=lambda x: abs(x[1]['mean_ic']), reverse=True)
    
    print("\n%-15s | %7s | %7s | %6s | %6s | %4s" % ('因子', '均值IC', '|IC|均值', 'ICIR', 'IC>0率', '期数'))
    print("-"*65)
    for col, data in sorted_ic:
        print("%-15s | %+7.4f | %7.4f | %6.3f | %5.1f%% | %4d" % (
            data['name'], data['mean_ic'], data['abs_mean_ic'],
            data['icir'], data['ic_positive_rate']*100, data['n_periods']))
    
    # ================================================================
    # Step 2: 12-Strategy Backtest
    # ================================================================
    print("\n" + "="*70)
    print("Step 2: 12策略回测")
    print("="*70)
    
    def score_hybrid(row):
        v = 0.0
        if not np.isnan(row.get('mom_20', np.nan)): v += 0.30 * np.clip(row['mom_20'], 0, 1)
        if not np.isnan(row.get('ret_20', np.nan)): v += 0.20 * np.clip((row['ret_20']+0.1)/0.5, 0, 1)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.20 * np.clip(row['roe']/20, 0, 1)
        if not np.isnan(row.get('pe_ttm', np.nan)) and 0 < row['pe_ttm'] < 50: v += 0.15 * (1 - row['pe_ttm']/50)
        if not np.isnan(row.get('total_mv', np.nan)) and row['total_mv'] > 0:
            mv = row['total_mv']/1e8; v += 0.15 * (1.0 if 20<=mv<=200 else (0.5 if mv<20 else 0.3))
        return v
    
    def score_value(row):
        v = 0.0
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.30 * np.clip(row['roe']/15, 0, 1)
        if not np.isnan(row.get('netprofit_yoy', np.nan)) and row['netprofit_yoy'] > 0: v += 0.25 * np.clip(row['netprofit_yoy']/50, 0, 1)
        if not np.isnan(row.get('gpr', np.nan)) and row['gpr'] > 0: v += 0.20 * np.clip(row['gpr']/40, 0, 1)
        if not np.isnan(row.get('pe_ttm', np.nan)) and 0 < row['pe_ttm'] < 50: v += 0.25 * (1 - row['pe_ttm']/50)
        return v
    
    def score_topk(row):
        v = 0.0
        if not np.isnan(row.get('mom_20', np.nan)): v += 0.25 * np.clip(row['mom_20'], 0, 1)
        if not np.isnan(row.get('ret_20', np.nan)): v += 0.20 * np.clip((row['ret_20']+0.1)/0.5, 0, 1)
        if not np.isnan(row.get('ret_5', np.nan)): v += 0.15 * np.clip((row['ret_5']+0.05)/0.2, 0, 1)
        if not np.isnan(row.get('vol_ratio', np.nan)) and row['vol_ratio'] > 0: v += 0.15 * np.clip(row['vol_ratio']/3, 0, 1)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.15 * np.clip(row['roe']/15, 0, 1)
        if not np.isnan(row.get('total_mv', np.nan)) and row['total_mv'] > 0:
            mv = row['total_mv']/1e8; v += 0.10 * (1.0 if mv<100 else 0.5)
        return v
    
    def score_dividend(row):
        v = 0.0
        if not np.isnan(row.get('pe_ttm', np.nan)) and 0 < row['pe_ttm'] < 30: v += 0.35 * (1 - row['pe_ttm']/30)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.25 * np.clip(row['roe']/15, 0, 1)
        if not np.isnan(row.get('vol_20', np.nan)): v += 0.25 * np.clip(1 - row['vol_20']/0.04, 0, 1)
        if not np.isnan(row.get('gpr', np.nan)) and row['gpr'] > 0: v += 0.15 * np.clip(row['gpr']/40, 0, 1)
        return v
    
    def score_quant(row):
        v = 0.0
        if not np.isnan(row.get('mom_20', np.nan)): v += 0.20 * np.clip(row['mom_20'], 0, 1)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.20 * np.clip(row['roe']/15, 0, 1)
        if not np.isnan(row.get('pe_ttm', np.nan)) and 0 < row['pe_ttm'] < 40: v += 0.15 * (1 - row['pe_ttm']/40)
        if not np.isnan(row.get('netprofit_yoy', np.nan)): v += 0.15 * np.clip((row['netprofit_yoy']+0.2)/1, 0, 1)
        if not np.isnan(row.get('vol_20', np.nan)): v += 0.15 * np.clip(1 - row['vol_20']/0.04, 0, 1)
        if not np.isnan(row.get('total_mv', np.nan)) and row['total_mv'] > 0:
            mv = row['total_mv']/1e8; v += 0.15 * np.clip(1 - np.log10(max(mv,1))/4, 0, 1)
        return v
    
    def score_small_cap(row):
        v = 0.0
        if not np.isnan(row.get('total_mv', np.nan)) and row['total_mv'] > 0:
            mv = row['total_mv']/1e8; v += 0.35 * np.clip(max(0, 1 - np.log10(max(mv,1))/3), 0, 1)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.25 * np.clip(row['roe']/15, 0, 1)
        if not np.isnan(row.get('ret_20', np.nan)): v += 0.20 * np.clip((row['ret_20']+0.1)/0.5, 0, 1)
        if not np.isnan(row.get('pe_ttm', np.nan)) and 0 < row['pe_ttm'] < 40: v += 0.20 * (1 - row['pe_ttm']/40)
        return v
    
    def score_small_cap_pure(row):
        v = 0.0
        if not np.isnan(row.get('total_mv', np.nan)) and row['total_mv'] > 0:
            mv = row['total_mv']/1e8; v += 0.60 * np.clip(max(0, 1 - np.log10(max(mv,1))/3), 0, 1)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.20 * np.clip(row['roe']/10, 0, 1)
        if not np.isnan(row.get('ret_5', np.nan)): v += 0.20 * np.clip((row['ret_5']+0.05)/0.15, 0, 1)
        return v
    
    def score_small_cap_jinx(row):
        v = 0.0
        if not np.isnan(row.get('total_mv', np.nan)) and row['total_mv'] > 0:
            mv = row['total_mv']/1e8; v += 0.30 * np.clip(max(0, 1 - np.log10(max(mv,1))/3), 0, 1)
        if not np.isnan(row.get('ret_20', np.nan)): v += 0.25 * np.clip((row['ret_20']+0.1)/0.5, 0, 1)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.20 * np.clip(row['roe']/12, 0, 1)
        if not np.isnan(row.get('vol_20', np.nan)): v += 0.15 * np.clip(1 - row['vol_20']/0.04, 0, 1)
        if not np.isnan(row.get('pe_ttm', np.nan)) and 0 < row['pe_ttm'] < 30: v += 0.10 * (1 - row['pe_ttm']/30)
        return v
    
    def score_cyclical(row):
        v = 0.0
        if not np.isnan(row.get('ret_60', np.nan)): v += 0.30 * np.clip((row['ret_60']+0.2)/0.8, 0, 1)
        if not np.isnan(row.get('ret_20', np.nan)): v += 0.25 * np.clip((row['ret_20']+0.1)/0.5, 0, 1)
        if not np.isnan(row.get('vol_ratio', np.nan)) and row['vol_ratio'] > 0: v += 0.20 * np.clip(row['vol_ratio']/3, 0, 1)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.15 * np.clip(row['roe']/15, 0, 1)
        if not np.isnan(row.get('pe_ttm', np.nan)) and 0 < row['pe_ttm'] < 30: v += 0.10 * (1 - row['pe_ttm']/30)
        return v
    
    def score_pb_roa(row):
        v = 0.0
        if not np.isnan(row.get('pe_ttm', np.nan)) and row['pe_ttm'] > 0: v += 0.35 * np.clip(max(0, 1 - row['pe_ttm']/30), 0, 1)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.35 * np.clip(row['roe']/20, 0, 1)
        if not np.isnan(row.get('gpr', np.nan)) and row['gpr'] > 0: v += 0.15 * np.clip(row['gpr']/40, 0, 1)
        if not np.isnan(row.get('netprofit_yoy', np.nan)): v += 0.15 * np.clip((row['netprofit_yoy']+0.1)/0.5, 0, 1)
        return v
    
    def score_convertible_bond(row):
        v = 0.0
        if not np.isnan(row.get('vol_20', np.nan)): v += 0.35 * np.clip(1 - row['vol_20']/0.04, 0, 1)
        if not np.isnan(row.get('pe_ttm', np.nan)) and 0 < row['pe_ttm'] < 30: v += 0.30 * (1 - row['pe_ttm']/30)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.20 * np.clip(row['roe']/12, 0, 1)
        if not np.isnan(row.get('gpr', np.nan)) and row['gpr'] > 0: v += 0.15 * np.clip(row['gpr']/40, 0, 1)
        return v
    
    def score_index_enhance(row):
        v = 0.0
        if not np.isnan(row.get('mom_20', np.nan)): v += 0.25 * np.clip(row['mom_20'], 0, 1)
        if not np.isnan(row.get('roe', np.nan)) and row['roe'] > 0: v += 0.25 * np.clip(row['roe']/15, 0, 1)
        if not np.isnan(row.get('ret_20', np.nan)): v += 0.20 * np.clip((row['ret_20']+0.05)/0.4, 0, 1)
        if not np.isnan(row.get('pe_ttm', np.nan)) and 0 < row['pe_ttm'] < 40: v += 0.15 * (1 - row['pe_ttm']/40)
        if not np.isnan(row.get('total_mv', np.nan)) and row['total_mv'] > 0:
            mv = row['total_mv']/1e8; v += 0.15 * np.clip(np.log10(max(mv,10))/4, 0, 1)
        return v
    
    STRATEGIES = {
        'hybrid': ('AI混合策略', score_hybrid),
        'value': ('价值策略', score_value),
        'topk': ('技术多因子策略', score_topk),
        'dividend': ('红利策略', score_dividend),
        'quant': ('量化多因子策略', score_quant),
        'small_cap': ('质量小市值策略', score_small_cap),
        'small_cap_pure': ('纯小市值策略', score_small_cap_pure),
        'small_cap_jinx': ('小市值Jinx择时', score_small_cap_jinx),
        'cyclical': ('周期轮动策略', score_cyclical),
        'pb_roa': ('PB-ROA价值策略', score_pb_roa),
        'convertible_bond': ('可转债策略', score_convertible_bond),
        'index_enhance': ('指数增强策略', score_index_enhance),
    }
    
    def run_one(scorer, df, trade_dates, rebalance, top_k, sentiment, cost_rate):
        rebal_dates = trade_dates[::rebalance]
        nav = [1.0]
        wins = loses = n_periods = 0
        
        for i, sel_date in enumerate(rebal_dates):
            sel_idx = i * rebalance
            exit_idx = sel_idx + rebalance
            if exit_idx >= len(trade_dates): break
            exit_date = trade_dates[exit_idx]
            
            day_data = df[df['trade_date'] == sel_date].copy()
            if len(day_data) == 0:
                nav.append(nav[-1]); continue
            
            scale = sentiment.get(sel_date, 1.0)
            eff_k = max(1, int(top_k * scale))
            
            day_data['score'] = day_data.apply(scorer, axis=1)
            picks = day_data.nlargest(eff_k, 'score')
            
            exit_data = df[(df['trade_date'] == exit_date) & (df['ts_code'].isin(picks['ts_code']))][['ts_code','close']]
            merged = picks[['ts_code','close']].merge(exit_data, on='ts_code', how='left', suffixes=('_buy','_sell'))
            merged['ret'] = (merged['close_sell'] - merged['close_buy']) / merged['close_buy']
            merged['ret'] = merged['ret'].fillna(0) - cost_rate
            
            port_ret = merged['ret'].mean()
            wins += int((merged['ret'] > 0).sum())
            loses += int((merged['ret'] <= 0).sum())
            n_periods += 1
            nav.append(nav[-1] * (1 + port_ret))
        
        return nav, wins, loses, n_periods
    
    results = {}
    for name, (label, scorer) in STRATEGIES.items():
        t1 = time.time()
        nav, w, l, n = run_one(scorer, df, trade_dates, 5, 10, sentiment, COST_RATE)
        
        if len(nav) < 2: continue
        total_ret = nav[-1]/nav[0] - 1
        annual_ret = (1 + total_ret) ** (252/len(trade_dates)) - 1
        rets = np.diff(nav)/np.array(nav[:-1])
        sharpe = np.mean(rets)/np.std(rets)*np.sqrt(len(rets)) if np.std(rets) > 0 else 0
        peak = nav[0]; max_dd = 0
        for v in nav:
            if v > peak: peak = v
            dd = (peak-v)/peak
            if dd > max_dd: max_dd = dd
        win_rate = w/max(w+l, 1)
        
        results[name] = {'label': label, 'total_ret': total_ret*100, 'annual_ret': annual_ret*100,
                         'max_dd': max_dd*100, 'sharpe': sharpe, 'win_rate': win_rate*100, 'n_periods': n}
        
        elapsed = time.time() - t1
        print('  %-15s (%5.1fs) | 收益: %+8.2f%% | 年化: %+8.2f%% | 回撤: %6.2f%% | 夏普: %6.2f | 胜率: %5.1f%%' % (
            label, elapsed, total_ret*100, annual_ret*100, max_dd*100, sharpe, win_rate*100))
    
    sorted_results = sorted(results.items(), key=lambda x: x[1]['sharpe'], reverse=True)
    
    print("\n" + "="*95)
    print("回测结果汇总 (%s ~ %s)" % (start_date, end_date))
    print("="*95)
    print("%-4s | %-15s | %9s | %9s | %7s | %6s | %6s | %4s" % (
        '排名', '策略', '总收益', '年化', '回撤', '夏普', '胜率', '期数'))
    print("-"*95)
    for i, (name, data) in enumerate(sorted_results):
        print("%-4d | %-15s | %+8.2f%% | %+8.2f%% | %6.2f%% | %6.2f | %5.1f%% | %4d" % (
            i+1, data['label'], data['total_ret'], data['annual_ret'],
            data['max_dd'], data['sharpe'], data['win_rate'], data['n_periods']))
    
    # Save results
    output = {
        'date_range': {'start': start_date, 'end': end_date},
        'factor_ic': {k: v for k, v in sorted_ic},
        'strategy_ranking': [{'rank': i+1, 'name': name, **data} for i, (name, data) in enumerate(sorted_results)],
        'total_time': time.time() - t0,
    }
    
    out_path = '/home/li/robottrade/output/backtest_12strategies_1y_ic.json'
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print("\n" + "="*60)
    print("总耗时: %.1fs" % (time.time()-t0))
    print("结果已保存: %s" % out_path)
    print("="*60)

if __name__ == '__main__':
    main()
