#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from scripts.backtest_3months import run_backtest_api

end_date = datetime.now().strftime('%Y-%m-%d')
start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')

all_strategies = ['tech', 'value', 'lowvol', 'reversal', 'sector_rotation', 'pullback_stab']

def cb(pct, msg):
    print(f'  [{pct}%] {msg}')

print(f'开始回测: {start_date} ~ {end_date}')
print(f'策略: {all_strategies}')
print()

result = run_backtest_api(
    start_date=start_date,
    end_date=end_date,
    rebalance=5,
    top_k=10,
    stop_loss=0.08,
    trailing=0.05,
    cost=0.0003,
    strategy_names=all_strategies,
    progress_cb=cb
)

if 'error' in result:
    print('错误: ' + result['error'])
else:
    print()
    print('=' * 80)
    print('回测结果汇总')
    print('=' * 80)
    print('期间: ' + result['start_date'] + ' ~ ' + result['end_date'])
    print('交易日: ' + str(result['n_trade_days']) + '天, 换仓周期: ' + str(result['rebalance']) + '日, 选股: ' + str(result['top_k']) + '只')
    print('止损: ' + str(result['stop_loss']*100) + '%, 止盈追踪: ' + str(result['trailing']*100) + '%, 手续费: ' + str(result['cost']*100) + '%')
    print()

    bm = result.get('benchmark', {})
    print('基准总收益: ' + str(bm.get('bm_total_ret', 'N/A')) + '%, 中证500: ' + str(bm.get('a500_total_ret', 'N/A')) + '%')
    print()

    for name, data in result['strategies'].items():
        m = data['metrics']
        label = data['label']
        print('--- ' + label + ' (' + name + ') ---')
        print('  总收益: ' + str(m['total_ret']) + '%  |  年化: ' + str(m['annual_ret']) + '%  |  最大回撤: ' + str(m['max_dd']) + '%')
        print('  夏普: ' + str(m['sharpe']) + '  |  胜率: ' + str(m['win_rate']) + '%  |  Alpha: ' + str(m['alpha']) + '%')
        print('  期数: ' + str(m['n_periods']) + '  |  基准收益: ' + str(m['bm_total_ret']) + '%')
        a500 = m.get('a500_total_ret')
        if a500 is not None:
            print('  中证500: ' + str(a500) + '%')
        print()
