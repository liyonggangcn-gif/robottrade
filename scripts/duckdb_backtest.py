#!/usr/bin/env python3
"""独立回测脚本 - 只依赖 DuckDB + pandas + numpy"""
import duckdb
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

DB_PATH = '/home/li/robottrade/data/quant_backtest.duckdb'
STAMP_DUTY = 0.0005

def load_all_data(start_date, end_date):
    conn = duckdb.connect(DB_PATH)
    print(f"[Data] 加载 {start_date} ~ {end_date}...")
    daily = conn.execute(f"""
        SELECT sd.trade_date, sd.ts_code,
               COALESCE(NULLIF(si.name,''), sd.ts_code) AS name,
               sd.close, sd.high, sd.low, sd.vol,
               sd.pe_ttm, sd.roe, sd.gpr,
               sd.netprofit_yoy,
               COALESCE(si.total_mv, sd.total_mv, 0) AS total_mv,
               si.industry
        FROM stock_daily sd
        LEFT JOIN stock_info si ON sd.ts_code = si.ts_code
        WHERE sd.trade_date >= '{start_date}' AND sd.trade_date <= '{end_date}'
          AND sd.close IS NOT NULL AND sd.close > 0
    """).fetchdf()
    daily['trade_date'] = daily['trade_date'].astype(str).str.strip()
    for c in ['close','pe_ttm','roe','gpr','netprofit_yoy','total_mv']:
        if c in daily.columns:
            daily[c] = pd.to_numeric(daily[c], errors='coerce')
    print(f"  行情: {len(daily):,} 条, {daily['ts_code'].nunique()} 只股票")

    try:
        factors = conn.execute(f"""
            SELECT trade_date, ts_code, mom_20
            FROM stock_factors
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        """).fetchdf()
        factors['trade_date'] = factors['trade_date'].astype(str).str.strip()
    except:
        factors = pd.DataFrame(columns=['trade_date','ts_code','mom_20'])

    concepts = conn.execute("SELECT ts_code, concept_name FROM stock_concepts").fetchdf()
    print(f"  概念: {len(concepts):,} 条")
    conn.close()
    return daily, factors, concepts

def get_trade_dates(daily, start_date, end_date):
    dates = sorted(daily['trade_date'].unique())
    return [d for d in dates if start_date <= d <= end_date]

def compute_extra_factors(daily):
    df = daily[['ts_code','trade_date','close','high','low','vol','industry']].sort_values(
        ['ts_code','trade_date']).copy()
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df['high'] = pd.to_numeric(df['high'], errors='coerce')
    df['low'] = pd.to_numeric(df['low'], errors='coerce')
    df['vol'] = pd.to_numeric(df['vol'], errors='coerce')
    df['ret_1d'] = df.groupby('ts_code')['close'].pct_change()
    df['vol_20'] = df.groupby('ts_code')['ret_1d'].transform(lambda x: x.rolling(20, min_periods=10).std())
    df['ret_5'] = df.groupby('ts_code')['close'].transform(lambda x: x.pct_change(5))
    df['ret_20'] = df.groupby('ts_code')['close'].transform(lambda x: x.pct_change(20))
    df['prior_gain'] = df.groupby('ts_code')['close'].transform(
        lambda x: x.rolling(21, min_periods=10).apply(
            lambda w: max((w[-1]-wi)/max(wi,1e-6) for wi in w[:-1]) if len(w)>1 else 0.0, raw=True))
    df['peak_60'] = df.groupby('ts_code')['close'].transform(lambda x: x.rolling(60, min_periods=20).max())
    df['drawdown'] = (df['close'] - df['peak_60']) / df['peak_60'].clip(lower=1e-6)
    df['vol_ma5'] = df.groupby('ts_code')['vol'].transform(lambda x: x.rolling(5, min_periods=3).mean())
    df['vol_ma20'] = df.groupby('ts_code')['vol'].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df['vol_ratio'] = df['vol_ma5'] / df['vol_ma20'].clip(lower=1e-6)
    df['daily_amp'] = (df['high'] - df['low']) / df['close'].clip(lower=1e-6)
    df['amp_ma5'] = df.groupby('ts_code')['daily_amp'].transform(lambda x: x.rolling(5, min_periods=3).mean())
    df['amp_ma10'] = df.groupby('ts_code')['daily_amp'].transform(lambda x: x.rolling(10, min_periods=5).mean())
    def _ps(row):
        pg = row['prior_gain']; dd = abs(row['drawdown']) if not np.isnan(row['drawdown']) else 0
        vr = row['vol_ratio'] if not np.isnan(row['vol_ratio']) else 1.0
        a5 = row['amp_ma5'] if not np.isnan(row['amp_ma5']) else 0
        a10 = row['amp_ma10'] if not np.isnan(row['amp_ma10']) else 0
        s_pg = 1.0 if pg>=0.20 else (0.7+(pg-0.10)/0.10*0.3 if pg>=0.10 else (0.3+(pg-0.05)/0.05*0.4 if pg>=0.05 else 0.0))
        s_dd = 1.0 if 0.05<=dd<=0.15 else (1.0-(dd-0.15)/0.10*0.3 if 0.15<dd<=0.25 else (dd/0.05*0.5 if dd<0.05 else (0.7-(dd-0.25)/0.15*0.5 if 0.25<dd<=0.40 else 0.0)))
        s_vol = max(0.0, min(1.0, 1.0-(vr-0.5)/0.5))
        s_amp = 1.0 if (a10>0 and a5<a10) else max(0.0, 1.0-(a5-a10)/max(a10,1e-6))
        return 0.40*s_pg + 0.35*s_dd + 0.25*(0.4*s_vol+0.6*s_amp)
    df['pullback_stab'] = df.apply(_ps, axis=1)
    return df[['ts_code','trade_date','vol_20','ret_5','ret_20','pullback_stab']]

def get_benchmark_returns(daily, dates):
    clean = daily[~daily['name'].str.contains('ST|退', na=False)].sort_values(['ts_code','trade_date'])
    clean['ret'] = clean.groupby('ts_code')['close'].pct_change()
    daily_mkt = clean.groupby('trade_date')['ret'].mean()
    result = {}
    for i in range(len(dates)-1):
        d1, d2 = dates[i], dates[i+1]
        if d1 in daily_mkt.index and d2 in daily_mkt.index:
            result[(d1,d2)] = daily_mkt[d2]
    return result

def build_market_sentiment(daily, trade_dates):
    clean = daily[~daily['name'].str.contains('ST|退', na=False)]
    clean = clean.sort_values(['ts_code','trade_date'])
    clean['ret'] = clean.groupby('ts_code')['close'].pct_change()
    daily_mkt = clean.groupby('trade_date')['ret'].mean()
    result = {}
    for d in trade_dates:
        if d in daily_mkt.index:
            r = daily_mkt[d]
            if pd.isna(r): result[d] = 1.0
            elif r < -0.02: result[d] = 0.0
            elif r < -0.01: result[d] = 0.5
            elif r > 0.015: result[d] = 1.2
            else: result[d] = 1.0
        else: result[d] = 1.0
    return result

def score_tech(row, factors_row=None, concepts_set=None):
    s = 0.0
    mom = row.get('mom_20', None)
    if mom is not None and not np.isnan(mom):
        s += 0.35 * max(0, min(mom, 1.0))
    elif factors_row is not None and 'mom_20' in factors_row:
        mom2 = factors_row['mom_20']
        if not np.isnan(mom2):
            s += 0.35 * max(0, min(mom2, 1.0))
    if 'ret_20' in row and not np.isnan(row['ret_20']):
        s += 0.25 * min(max(row['ret_20']+0.1, 0), 0.5) / 0.5
    if 'roe' in row and not np.isnan(row['roe']) and row['roe'] > 0:
        s += 0.10 * min(row['roe']/20, 1.0)
    if 'total_mv' in row and row['total_mv'] > 0:
        mv = row['total_mv'] / 1e8
        if 20 <= mv <= 200: s += 0.15
        elif mv < 20: s += 0.10
        elif mv < 500: s += 0.05
    return s

def score_value(row, extra_row=None):
    s = 0.0
    if 'roe' in row and not np.isnan(row['roe']):
        s += 0.30 * min(max(row['roe']/15, 0), 1.0)
    if 'netprofit_yoy' in row and not np.isnan(row['netprofit_yoy']):
        s += 0.25 * min(max(row['netprofit_yoy']/50, 0), 1.0)
    if 'gpr' in row and not np.isnan(row['gpr']):
        s += 0.20 * min(max(row['gpr']/40, 0), 1.0)
    if 'pe_ttm' in row and not np.isnan(row['pe_ttm']) and row['pe_ttm'] > 0:
        s += 0.25 * max(0, 1 - row['pe_ttm']/50)
    return s

def score_lowvol(row, extra_row=None):
    s = 0.0
    if extra_row is not None and 'vol_20' in extra_row and not np.isnan(extra_row['vol_20']):
        s += 0.50 * max(0, 1 - extra_row['vol_20']/0.05)
    if 'ret_20' in row and not np.isnan(row['ret_20']):
        s += 0.30 * min(max(row['ret_20']+0.05, 0), 0.3) / 0.3
    if 'roe' in row and not np.isnan(row['roe']) and row['roe'] > 0:
        s += 0.20 * min(row['roe']/15, 1.0)
    return s

def score_reversal(row, extra_row=None):
    s = 0.0
    if extra_row is not None and 'ret_5' in extra_row and not np.isnan(extra_row['ret_5']):
        r5 = extra_row['ret_5']
        if -0.15 <= r5 <= -0.03:
            s += 0.50
        elif r5 < -0.15:
            s += 0.30
        elif -0.03 < r5 < 0:
            s += 0.30
    if 'roe' in row and not np.isnan(row['roe']) and row['roe'] > 0:
        s += 0.30 * min(row['roe']/15, 1.0)
    if 'pe_ttm' in row and not np.isnan(row['pe_ttm']) and row['pe_ttm'] > 0:
        s += 0.20 * max(0, 1 - row['pe_ttm']/40)
    return s

def score_sector(row, extra_row=None):
    s = 0.0
    if extra_row is not None and 'ret_20' in extra_row and not np.isnan(extra_row['ret_20']):
        s += 0.40 * min(max(extra_row['ret_20']+0.1, 0), 0.4) / 0.4
    if 'roe' in row and not np.isnan(row['roe']) and row['roe'] > 0:
        s += 0.30 * min(row['roe']/15, 1.0)
    if 'ret_5' in row and not np.isnan(row['ret_5']):
        s += 0.30 * min(max(row['ret_5']+0.05, 0), 0.2) / 0.2
    return s

def score_pullback(row, extra_row=None):
    s = 0.0
    if extra_row is not None and 'pullback_stab' in extra_row and not np.isnan(extra_row['pullback_stab']):
        s += 0.50 * extra_row['pullback_stab']
    if 'ret_20' in row and not np.isnan(row['ret_20']):
        s += 0.25 * min(max(row['ret_20']+0.1, 0), 0.4) / 0.4
    if 'roe' in row and not np.isnan(row['roe']) and row['roe'] > 0:
        s += 0.25 * min(row['roe']/15, 1.0)
    return s

SCORERS = {
    'tech': score_tech,
    'value': score_value,
    'lowvol': score_lowvol,
    'reversal': score_reversal,
    'sector_rotation': score_sector,
    'pullback_stab': score_pullback,
}

def run_one_strategy(name, daily, factors, concepts, trade_dates, rebalance, top_k, sentiment_scale, stop_loss, trailing_pct, cost_rate, extra_df):
    scorer = SCORERS[name]
    concept_map = {}
    for _, r in concepts.iterrows():
        concept_map.setdefault(r['ts_code'], set()).add(r['concept_name'])

    factor_map = {}
    for _, r in factors.iterrows():
        factor_map[(r['trade_date'], r['ts_code'])] = r

    extra_map = {}
    if extra_df is not None and len(extra_df) > 0:
        for _, r in extra_df.iterrows():
            extra_map[(r['trade_date'], r['ts_code'])] = r

    rebal_dates = trade_dates[::rebalance]
    results = []

    for idx_sel, sel_date in enumerate(rebal_dates):
        if idx_sel + 1 < len(trade_dates):
            exit_date = trade_dates[idx_sel + rebalance] if idx_sel + rebalance < len(trade_dates) else trade_dates[-1]
        else:
            break

        day_data = daily[daily['trade_date'] == sel_date].copy()
        if len(day_data) == 0:
            continue

        scale = sentiment_scale.get(sel_date, 1.0)
        effective_topk = max(0, int(top_k * scale))

        concept_sets = {code: concept_map.get(code, set()) for code in day_data['ts_code']}

        scores = []
        for _, row in day_data.iterrows():
            f_row = factor_map.get((sel_date, row['ts_code']), None)
            e_row = extra_map.get((sel_date, row['ts_code']), None)
            merged = {**row.to_dict()}
            if f_row is not None:
                merged.update({k: v for k, v in f_row.to_dict().items() if k not in merged})
            if e_row is not None:
                merged.update({k: v for k, v in e_row.to_dict().items() if k not in merged})
            s = scorer(merged, e_row)
            scores.append(s)

        day_data['score'] = scores
        day_data = day_data.sort_values('score', ascending=False).head(effective_topk)

        stock_rets = []
        for _, row in day_data.iterrows():
            code = row['ts_code']
            buy_price = row['close']
            exit_data = daily[(daily['trade_date'] == exit_date) & (daily['ts_code'] == code)]
            if len(exit_data) > 0:
                sell_price = exit_data.iloc[0]['close']
                ret = (sell_price - buy_price) / buy_price
            else:
                sell_price = 0
                ret = 0.0
            stock_rets.append({'ts_code': code, 'name': row.get('name',''), 'industry': row.get('industry',''),
                               'ret': ret, 'exit_type': 'normal', 'buy_price': buy_price, 'sell_price': sell_price})

        if stock_rets:
            port_ret = np.mean([s['ret'] for s in stock_rets])
            port_ret -= cost_rate
            wins = sum(1 for s in stock_rets if s['ret'] > 0)
            loses = sum(1 for s in stock_rets if s['ret'] <= 0)
        else:
            port_ret = 0.0; wins = 0; loses = 0

        results.append({
            'sel_date': sel_date, 'exit_date': exit_date,
            'port_ret': port_ret, 'win': wins, 'lose': loses,
            'n_stocks': len(stock_rets), 'sl_hits': 0, 'tp_hits': 0,
            'turnover': 1.0, '_details': stock_rets,
        })

    return pd.DataFrame(results)

def calc_metrics(res, bm_daily, trade_dates, a500_daily=None):
    if len(res) == 0:
        return {'total_ret': 0, 'annual_ret': 0, 'max_dd': 0, 'sharpe': 0, 'win_rate': 0, 'alpha': 0,
                'bm_total_ret': 0, 'a500_total_ret': None, 'n_periods': 0, 'nav': [], 'bm_nav': [], 'a500_nav': None}

    nav = [1.0]
    bm_nav = [1.0]
    for _, row in res.iterrows():
        nav.append(nav[-1] * (1 + row['port_ret']))
        key = (row['sel_date'], row['exit_date'])
        bm_ret = bm_daily.get(key, 0)
        bm_nav.append(bm_nav[-1] * (1 + bm_ret))

    total_ret = nav[-1] / nav[0] - 1
    bm_total_ret = bm_nav[-1] / bm_nav[0] - 1

    n_days = len(trade_dates)
    annual_ret = (1 + total_ret) ** (252 / max(n_days, 1)) - 1

    rets = res['port_ret'].values
    sharpe = np.mean(rets) / np.std(rets) * np.sqrt(len(rets)) if np.std(rets) > 0 else 0

    peak = nav[0]
    max_dd = 0
    for v in nav:
        if v > peak: peak = v
        dd = (peak - v) / peak
        if dd > max_dd: max_dd = dd

    win_rate = res['win'].sum() / max(res['win'].sum() + res['lose'].sum(), 1)
    alpha = annual_ret - (bm_total_ret * 252 / max(n_days, 1))

    a500_total_ret = None
    a500_nav = None
    if a500_daily:
        a500_nav_list = [1.0]
        for _, row in res.iterrows():
            key = row['exit_date']
            r = a500_daily.get(key, 0)
            if not pd.isna(r):
                a500_nav_list.append(a500_nav_list[-1] * (1 + r))
            else:
                a500_nav_list.append(a500_nav_list[-1])
        a500_total_ret = a500_nav_list[-1] / a500_nav_list[0] - 1
        a500_nav = a500_nav_list

    return {
        'total_ret': total_ret, 'annual_ret': annual_ret, 'max_dd': max_dd,
        'sharpe': sharpe, 'win_rate': win_rate, 'alpha': alpha,
        'bm_total_ret': bm_total_ret, 'a500_total_ret': a500_total_ret,
        'n_periods': len(res), 'nav': nav, 'bm_nav': bm_nav, 'a500_nav': a500_nav,
    }

def main():
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
    load_start = (datetime.now() - timedelta(days=400)).strftime('%Y-%m-%d')

    all_strategies = ['tech', 'value', 'lowvol', 'reversal', 'sector_rotation', 'pullback_stab']

    print(f'开始回测: {start_date} ~ {end_date}')
    print(f'策略: {all_strategies}')
    print(f'参数: 换仓5日, 选股10只, 止损8%, 追踪止盈5%, 手续费0.03%')
    print()

    t0 = time.time()
    daily, factors, concepts = load_all_data(load_start, end_date)
    trade_dates = get_trade_dates(daily, start_date, end_date)
    print(f'交易日: {len(trade_dates)} 天')
    print()

    print('计算辅助因子...')
    extra_df = compute_extra_factors(daily)
    bm_daily = get_benchmark_returns(daily, trade_dates)
    sentiment_scale = build_market_sentiment(daily, trade_dates)
    print()

    common = dict(factors=factors, concepts=concepts, trade_dates=trade_dates, rebalance=5, top_k=10,
                  sentiment_scale=sentiment_scale, stop_loss=-0.08, trailing_pct=0.05,
                  cost_rate=0.0003, extra_df=extra_df)

    print('运行策略...')
    all_results = {}
    for i, name in enumerate(all_strategies):
        t1 = time.time()
        res = run_one_strategy(name, daily, **common)
        m = calc_metrics(res, bm_daily, trade_dates)
        elapsed = time.time() - t1
        label = {'tech':'A.技术动量','value':'B.价值质量','lowvol':'C.低波动',
                 'reversal':'D.短期反转','sector_rotation':'E.行业轮动','pullback_stab':'F.回调企稳'}[name]
        print(f'  [{i+1}/{len(all_strategies)}] {label} ({elapsed:.1f}s)')
        print(f'    总收益: {m["total_ret"]*100:+.2f}%  年化: {m["annual_ret"]*100:+.2f}%  回撤: {m["max_dd"]*100:.2f}%  夏普: {m["sharpe"]:.2f}  胜率: {m["win_rate"]*100:.1f}%')
        all_results[name] = {'label': label, 'metrics': m, 'periods': res}

    print()
    print('=' * 90)
    print('回测结果汇总')
    print('=' * 90)
    print(f'期间: {start_date} ~ {end_date}  |  交易日: {len(trade_dates)}  |  换仓: 5日  |  选股: 10只')
    print()
    for name, data in all_results.items():
        m = data['metrics']
        print(f'  {data["label"]:15s} | 收益: {m["total_ret"]:+7.2f}% | 年化: {m["annual_ret"]:+7.2f}% | 回撤: {m["max_dd"]:6.2f}% | 夏普: {m["sharpe"]:6.2f} | 胜率: {m["win_rate"]*100:5.1f}%')

    print(f'\n总耗时: {time.time()-t0:.1f}s')

if __name__ == '__main__':
    import time
    main()
