#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
因子有效性检验（IC Analysis）
检验各技术/基本面因子对未来5日收益的预测能力

用法：python scripts/factor_ic_test.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, date

from src.utils.db_utils import DBUtils

# ──────────────────────────────────────────────
# 参数配置
# ──────────────────────────────────────────────
START_DATE  = '2024-03-01'
END_DATE    = '2026-03-17'
FWD_DAYS    = 5          # 预测未来 N 日收益
N_QUINTILE  = 5          # 分层组数
MIN_STOCKS  = 30         # 每日截面最少股票数，否则跳过
ICIR_GOOD   = 0.5        # ICIR 阈值：稳定
ICIR_GREAT  = 1.0        # ICIR 阈值：很好
MEAN_IC_SIG = 0.02       # mean_IC 有效门槛
HIT_RATE_SIG = 0.55      # hit_rate 有效门槛

FACTOR_LABELS = {
    'mom_5':         '近5日涨幅',
    'mom_20':        '近20日涨幅',
    'mom_60':        '近60日涨幅',
    'rsi_14':        '14日RSI',
    'vol_ratio':     '成交量比(当日/20日均量)',
    'atr_pct':       'ATR%',
    'pe_inv':        '1/PE(便宜度)',
    'roe':           'ROE',
    'pb_inv':        '1/PB(便宜度)',
    'price_pos_52w': '52周价格位置[0,1]',
    'bb_pos':        '布林带位置',
}

# ──────────────────────────────────────────────
# 1. 拉取数据
# ──────────────────────────────────────────────

def load_stock_pool():
    """从 stock_pool 取活跃股票列表"""
    sql = "SELECT ts_code FROM stock_pool WHERE is_active = 1"
    try:
        df = DBUtils.query_df(sql)
        codes = df['ts_code'].tolist()
        print(f"[数据] 股票池：{len(codes)} 只活跃股票")
        return codes
    except Exception as e:
        print(f"[警告] 读取 stock_pool 失败 ({e})，使用全市场股票")
        return None


def load_price_data(codes=None):
    """
    一次拉全量数据，时间范围宽于分析区间以保证 rolling 计算稳定
    拉取 START_DATE 前 90 天到 END_DATE
    """
    # rolling 最大窗口 = 52 周 = 约 260 交易日，留 90 天做缓冲（降低内存）
    # 因为最长动量 mom_60 + fwd_ret_5 需要往前看 ~60 + 5 天
    # 52周高低 price_pos_52w 需要 ~250 天，故需更长的预热期
    import pandas as pd
    start_ext = (pd.to_datetime(START_DATE) - pd.DateOffset(days=400)).strftime('%Y%m%d')
    start_ext_dash = (pd.to_datetime(START_DATE) - pd.DateOffset(days=400)).strftime('%Y-%m-%d')
    end_dash = END_DATE

    if codes:
        placeholders = ','.join(['%s'] * len(codes))
        sql = f"""
            SELECT trade_date, ts_code, open, high, low, close, vol, amount,
                   pe_ttm, total_mv, roe, pb
            FROM stock_daily
            WHERE trade_date >= %s AND trade_date <= %s
              AND ts_code IN ({placeholders})
            ORDER BY ts_code, trade_date
        """
        params = [start_ext_dash, end_dash] + codes
    else:
        sql = """
            SELECT trade_date, ts_code, open, high, low, close, vol, amount,
                   pe_ttm, total_mv, roe, pb
            FROM stock_daily
            WHERE trade_date >= %s AND trade_date <= %s
            ORDER BY ts_code, trade_date
        """
        params = [start_ext_dash, end_dash]

    print(f"[数据] 正在从 MySQL 拉取价格数据（{start_ext_dash} ~ {end_dash}）...")
    df = DBUtils.query_df(sql, params)
    print(f"[数据] 拉取完成，共 {len(df):,} 行，{df['ts_code'].nunique()} 只股票")

    # 类型转换
    for col in ['open', 'high', 'low', 'close', 'vol', 'amount', 'pe_ttm', 'total_mv', 'roe', 'pb']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df.sort_values(['ts_code', 'trade_date'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ──────────────────────────────────────────────
# 2. 计算因子
# ──────────────────────────────────────────────

def _calc_rsi(series, period=14):
    """计算 RSI"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_factors(df):
    """按股票分组，用 pandas rolling 计算所有因子，返回带因子列的 DataFrame"""
    print("[因子] 开始计算因子（按股票分组 rolling）...")

    results = []
    codes = df['ts_code'].unique()
    total = len(codes)

    for i, code in enumerate(codes):
        if (i + 1) % 50 == 0 or (i + 1) == total:
            print(f"  进度: {i+1}/{total} 只股票", end='\r')

        g = df[df['ts_code'] == code].copy().reset_index(drop=True)
        c = g['close']
        h = g['high']
        lo = g['low']
        v  = g['vol']

        # 动量
        g['mom_5']  = c.pct_change(5)
        g['mom_20'] = c.pct_change(20)
        g['mom_60'] = c.pct_change(60)

        # RSI_14
        g['rsi_14'] = _calc_rsi(c, 14)

        # 成交量比
        vol_ma20 = v.rolling(20, min_periods=10).mean()
        g['vol_ratio'] = v / vol_ma20.replace(0, np.nan)

        # ATR%
        tr = pd.concat([
            h - lo,
            (h - c.shift(1)).abs(),
            (lo - c.shift(1)).abs()
        ], axis=1).max(axis=1)
        atr14 = tr.rolling(14, min_periods=7).mean()
        g['atr_pct'] = atr14 / c.replace(0, np.nan)

        # 估值类
        g['pe_inv'] = np.where(g['pe_ttm'] > 0, 1.0 / g['pe_ttm'], np.nan)
        g['roe']    = g['roe']  # 直接用原始值
        # pb_inv: 优先用 pb 列，没有时用 total_mv/close 近似（流通市值/价格≈股本，不精确但可用）
        if 'pb' in g.columns and g['pb'].notna().sum() > 0:
            g['pb_inv'] = np.where(g['pb'] > 0, 1.0 / g['pb'], np.nan)
        else:
            # total_mv 单位通常是万元，close 单位元，pb ≈ total_mv*10000 / (close * 总股本)
            # 这里只能做相对排序用，取 close/total_mv 作为代理（值越大越便宜）
            g['pb_inv'] = c / g['total_mv'].replace(0, np.nan)

        # 52周价格位置
        roll_252_max = c.rolling(252, min_periods=60).max()
        roll_252_min = c.rolling(252, min_periods=60).min()
        denom_52 = (roll_252_max - roll_252_min).replace(0, np.nan)
        g['price_pos_52w'] = (c - roll_252_min) / denom_52

        # 布林带位置 (20日, 2σ)
        ma20  = c.rolling(20, min_periods=10).mean()
        std20 = c.rolling(20, min_periods=10).std()
        bb_upper = ma20 + 2 * std20
        bb_lower = ma20 - 2 * std20
        denom_bb = (bb_upper - bb_lower).replace(0, np.nan)
        g['bb_pos'] = (c - bb_lower) / denom_bb

        # 前向收益（T+5）
        g['fwd_ret_5'] = c.shift(-FWD_DAYS) / c - 1

        results.append(g)

    print(f"\n[因子] 计算完成，共 {total} 只股票")
    out = pd.concat(results, ignore_index=True)
    return out


# ──────────────────────────────────────────────
# 3. IC 计算
# ──────────────────────────────────────────────

def calc_ic_series(factor_df, factors):
    """
    每日截面计算 Rank IC（Spearman 相关系数）
    只在分析时间段内计算
    """
    start_ts = pd.to_datetime(START_DATE)
    end_ts   = pd.to_datetime(END_DATE)

    # 过滤到分析区间（去掉因子计算所需的预热期数据，也去掉末尾没有 fwd_ret_5 的行）
    mask = (factor_df['trade_date'] >= start_ts) & (factor_df['trade_date'] <= end_ts)
    sub  = factor_df[mask].copy()

    print(f"[IC] 分析区间内共 {len(sub):,} 行，{sub['trade_date'].nunique()} 个交易日")

    ic_records = {f: [] for f in factors}
    dates_used = []

    for dt, grp in sub.groupby('trade_date'):
        valid = grp[['fwd_ret_5'] + factors].dropna(how='any')
        if len(valid) < MIN_STOCKS:
            continue

        dates_used.append(dt)
        ret_rank = valid['fwd_ret_5'].rank()

        for f in factors:
            f_rank = valid[f].rank()
            corr, _ = stats.spearmanr(f_rank, ret_rank)
            ic_records[f].append(corr)

    print(f"[IC] 有效交易日: {len(dates_used)} 天")
    ic_df = pd.DataFrame(ic_records, index=dates_used)
    ic_df.index.name = 'trade_date'
    return ic_df


# ──────────────────────────────────────────────
# 4. 汇总统计
# ──────────────────────────────────────────────

def summarize_ic(ic_df, factors):
    rows = []
    n = len(ic_df)
    for f in factors:
        series = ic_df[f].dropna()
        if len(series) == 0:
            continue
        mean_ic = series.mean()
        ic_std  = series.std()
        icir    = mean_ic / ic_std if ic_std > 0 else np.nan
        hit     = (series > 0).sum() / len(series)
        t_stat  = mean_ic / (ic_std / np.sqrt(len(series))) if ic_std > 0 else np.nan
        rows.append({
            'factor':      f,
            'label':       FACTOR_LABELS.get(f, f),
            'mean_IC':     round(mean_ic, 5),
            'IC_std':      round(ic_std,  5),
            'ICIR':        round(icir,    4) if not np.isnan(icir) else np.nan,
            'hit_rate':    round(hit,     4),
            't_stat':      round(t_stat,  3) if not np.isnan(t_stat) else np.nan,
            'valid_days':  int(len(series)),
        })

    summary = pd.DataFrame(rows).sort_values('ICIR', ascending=False, key=lambda x: x.abs())
    summary.reset_index(drop=True, inplace=True)
    return summary


# ──────────────────────────────────────────────
# 5. 因子分层回测
# ──────────────────────────────────────────────

def quintile_analysis(factor_df, factors):
    """每日按因子值分 N 组，计算各组平均 fwd_ret_5"""
    start_ts = pd.to_datetime(START_DATE)
    end_ts   = pd.to_datetime(END_DATE)
    mask     = (factor_df['trade_date'] >= start_ts) & (factor_df['trade_date'] <= end_ts)
    sub      = factor_df[mask].copy()

    quintile_rows = []

    for f in factors:
        q_returns = {q: [] for q in range(1, N_QUINTILE + 1)}

        for dt, grp in sub.groupby('trade_date'):
            valid = grp[[f, 'fwd_ret_5']].dropna()
            if len(valid) < MIN_STOCKS:
                continue
            valid = valid.copy()
            valid['q'] = pd.qcut(valid[f].rank(method='first'),
                                 q=N_QUINTILE,
                                 labels=range(1, N_QUINTILE + 1))
            for q in range(1, N_QUINTILE + 1):
                grp_ret = valid.loc[valid['q'] == q, 'fwd_ret_5'].mean()
                q_returns[q].append(grp_ret)

        row = {'factor': f, 'label': FACTOR_LABELS.get(f, f)}
        for q in range(1, N_QUINTILE + 1):
            arr = np.array(q_returns[q])
            row[f'Q{q}_avg_ret'] = round(arr.mean() * 100, 4) if len(arr) > 0 else np.nan
        if f'Q{N_QUINTILE}_avg_ret' in row and 'Q1_avg_ret' in row:
            row['Q5_minus_Q1'] = round(
                row[f'Q{N_QUINTILE}_avg_ret'] - row['Q1_avg_ret'], 4
            )
        quintile_rows.append(row)

    return pd.DataFrame(quintile_rows)


# ──────────────────────────────────────────────
# 6. 结论建议
# ──────────────────────────────────────────────

def build_conclusion(summary_df, quintile_df):
    """自动标注每个因子是否值得保留，并给出建议"""
    merged = summary_df.merge(quintile_df[['factor', 'Q5_minus_Q1']], on='factor', how='left')

    def verdict(row):
        icir = row['ICIR'] if not pd.isna(row['ICIR']) else 0
        hit  = row['hit_rate'] if not pd.isna(row['hit_rate']) else 0
        qs   = row.get('Q5_minus_Q1', 0) or 0

        if abs(icir) >= ICIR_GREAT and hit >= HIT_RATE_SIG:
            return '★★★ 强有效，强烈建议保留'
        elif abs(icir) >= ICIR_GOOD and hit >= HIT_RATE_SIG:
            return '★★  稳定有效，建议保留'
        elif abs(row['mean_IC']) >= MEAN_IC_SIG:
            return '★   弱有效，可纳入参考'
        else:
            return '✗   无效，建议剔除'

    merged['verdict'] = merged.apply(verdict, axis=1)
    merged['direction'] = merged['mean_IC'].apply(
        lambda x: '正向(越大越涨)' if x > 0 else '反向(越大越跌)'
    )
    return merged


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

def main():
    t0 = datetime.now()
    print("=" * 60)
    print("  因子有效性检验（IC Analysis）")
    print(f"  分析区间: {START_DATE} ~ {END_DATE}")
    print(f"  目标: T+{FWD_DAYS} 日收益率")
    print("=" * 60)

    # ── 1. 拉数据
    codes = load_stock_pool()
    price_df = load_price_data(codes)

    if price_df.empty:
        print("[错误] 未拉到任何数据，退出")
        return

    # ── 2. 计算因子
    factor_df = compute_factors(price_df)

    factors = list(FACTOR_LABELS.keys())

    # ── 3. IC 时序
    print("[IC] 开始逐日计算截面 Rank IC...")
    ic_df = calc_ic_series(factor_df, factors)

    # ── 4. 汇总统计
    print("[IC] 计算汇总统计...")
    summary = summarize_ic(ic_df, factors)

    # ── 5. 分层回测
    print("[分层] 开始因子分层回测（Quintile Analysis）...")
    quintile = quintile_analysis(factor_df, factors)

    # ── 6. 结论
    conclusion = build_conclusion(summary, quintile)

    # ── 打印结果
    print("\n" + "=" * 70)
    print("  因子 IC 汇总（按 |ICIR| 降序排列）")
    print("=" * 70)
    cols_show = ['factor', 'label', 'mean_IC', 'IC_std', 'ICIR', 'hit_rate', 't_stat', 'valid_days']
    print(summary[cols_show].to_string(index=False))

    print("\n" + "=" * 70)
    print("  因子分层回测（各组平均 T+5 收益率 %）")
    print("=" * 70)
    q_cols = ['factor', 'label'] + [f'Q{q}_avg_ret' for q in range(1, N_QUINTILE+1)] + ['Q5_minus_Q1']
    print(quintile[q_cols].to_string(index=False))

    print("\n" + "=" * 70)
    print("  综合评价与建议")
    print("=" * 70)
    verdict_cols = ['factor', 'label', 'ICIR', 'hit_rate', 'Q5_minus_Q1', 'direction', 'verdict']
    print(conclusion[verdict_cols].to_string(index=False))

    # ── 7. 输出 CSV
    os.makedirs('output', exist_ok=True)
    today_str = date.today().strftime('%Y%m%d')
    out_path  = f'output/factor_ic_report_{today_str}.csv'

    # 合并 IC 时序、分层回测、汇总三个表到一个 CSV（分 sheet 模拟用分隔行）
    # 写成多段 CSV，方便用 Excel 直接打开
    with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
        f.write('# == 因子IC汇总 ==\n')
        conclusion[verdict_cols + ['mean_IC', 'IC_std', 't_stat', 'valid_days']].to_csv(f, index=False)
        f.write('\n# == 因子分层回测 ==\n')
        quintile[q_cols].to_csv(f, index=False)
        f.write('\n# == IC日时序（每行一个交易日） ==\n')
        ic_df.reset_index().to_csv(f, index=False)

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\n[完成] 报告已输出: {os.path.abspath(out_path)}")
    print(f"[完成] 总耗时: {elapsed:.1f} 秒")

    # ── 简明结论
    print("\n" + "=" * 70)
    print("  快速结论")
    print("=" * 70)
    keep   = conclusion[conclusion['verdict'].str.startswith('★')]
    remove = conclusion[~conclusion['verdict'].str.startswith('★')]
    if len(keep):
        print(f"  建议保留 ({len(keep)} 个):", ', '.join(keep['factor'].tolist()))
    if len(remove):
        print(f"  建议剔除 ({len(remove)} 个):", ', '.join(remove['factor'].tolist()))


if __name__ == '__main__':
    main()
