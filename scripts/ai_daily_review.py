#!/usr/bin/env python3
"""A股AI方向每日点评 + 技术分析"""
import sys
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.utils.notifier import DingTalkNotifier
import pandas as pd
import requests

API_KEY = os.environ.get('GROQ_API_KEY', '')

def get_stock_tech(ts_code):
    """获取股票技术分析"""
    # 获取60日数据
    df = DBUtils.query_df(f'''
        SELECT trade_date, close, vol FROM stock_daily 
        WHERE ts_code = '{ts_code}' 
        ORDER BY trade_date DESC LIMIT 60
    ''')
    
    if len(df) < 30:
        return None
    
    closes = df['close'].astype(float).values
    vols = df['vol'].astype(float).values
    
    # 计算均线
    ma5 = closes[:5].mean()
    ma10 = closes[:10].mean()
    ma20 = closes[:20].mean()
    ma60 = closes[:60].mean() if len(closes) >= 60 else ma20
    
    # MACD计算
    ema12 = closes[:12].mean()  # 简化
    ema26 = closes[:26].mean()
    diff = ema12 - ema26
    
    # 近期趋势判断
    trend = "上升" if ma5 > ma10 > ma20 else "震荡" if ma10 > ma20 else "下降"
    
    # 均线金叉/死叉
    if ma5 > ma10 and ma10 > ma20:
        signal = "金叉"  # 买入信号
    elif ma5 < ma10 and ma10 < ma20:
        signal = "死叉"  # 卖出信号
    else:
        signal = "震荡"
    
    # 成交量
    vol_ratio = vols[0] / vols[:5].mean() if vols[:5].mean() > 0 else 1
    vol_status = "放量" if vol_ratio > 1.2 else "缩量" if vol_ratio < 0.8 else "正常"
    
    # 支撑/阻力
    support = ma20
    resist = ma5
    
    return {
        'ma5': ma5,
        'ma10': ma10,
        'ma20': ma20,
        'trend': trend,
        'signal': signal,
        'vol': vol_status,
        'support': support,
        'resist': resist,
        'close': closes[0]
    }

def get_tech_recommendation(tech):
    """根据技术分析给出买卖建议"""
    signal = tech['signal']
    trend = tech['trend']
    vol = tech['vol']
    
    if signal == "金叉" and trend == "上升" and vol == "放量":
        return "强买", "短期看涨，均线多头排列"
    elif signal == "金叉":
        return "买入", "均线金叉，回调可买"
    elif signal == "死叉" or trend == "下降":
        return "卖出", "均线死叉，短期观望"
    elif trend == "震荡":
        return "持有", "震荡整理，观望为主"
    else:
        return "观望", "等待明确信号"

def get_market_data():
    data = {}
    try:
        df = DBUtils.query_df('SELECT COUNT(*) as cnt FROM stock_daily WHERE trade_date = CURDATE()')
        data['count'] = int(df.iloc[0]['cnt']) if not df.empty else 0
    except:
        data['count'] = 0
    return data

def get_ai_candidates():
    df_ai = DBUtils.query_df("SELECT DISTINCT ts_code FROM ai_stock_pool")
    ai_ts_codes = set(df_ai['ts_code'].tolist())
    
    trade_dates = DBUtils.query_df("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT 60")
    dates = trade_dates['trade_date'].tolist()
    
    data = []
    for ts_code in list(ai_ts_codes):
        df_stock = DBUtils.query_df(f'''
            SELECT trade_date, close FROM stock_daily 
            WHERE ts_code = '{ts_code}' AND trade_date IN ({','.join(['%s'] * min(60, len(dates)))}) 
            ORDER BY trade_date
        ''', tuple(dates[:60]))
        
        if len(df_stock) >= 40:
            closes = df_stock['close'].astype(float).values
            mom_20 = (closes[-1] / closes[-20] - 1) * 100 if closes[-20] > 0 else 0
            daily_returns = (closes[1:] / closes[:-1] - 1)
            positive_days = (daily_returns > 0).sum()
            quality = positive_days / (len(daily_returns) - 1) * 100 if len(daily_returns) > 1 else 50
            if mom_20 > -30 and quality > 30:
                data.append({'ts_code': ts_code, 'mom_20': mom_20, 'quality': quality})
    
    df = pd.DataFrame(data)
    df['score'] = df['mom_20'] * 0.6 + df['quality'] * 0.4
    df = df.sort_values('score', ascending=False).head(15)
    
    if not df.empty:
        names = []
        for ts in df['ts_code'].tolist():
            dfn = DBUtils.query_df(f"SELECT name FROM stock_info WHERE ts_code = '{ts}'")
            names.append(dfn.iloc[0]['name'] if not dfn.empty else '')
        df['name'] = names
    
    return df.head(10)

def grok_analysis(market_data, candidates, tech_data):
    cand_list = []
    for i, (_, r) in enumerate(candidates.iterrows()):
        cand_list.append(f"{i+1}. {r['ts_code']} {r['name']} 综合分:{r['score']:.1f}")
    cand_str = '\n'.join(cand_list)
    
    # 加入技术分析
    tech_str = ""
    for ts_code, row in tech_data.items():
        if row:
            action, reason = get_tech_recommendation(row)
            tech_str += f"- {ts_code}: {row['trend']}趋势/{row['signal']}/{row['vol']} → {action}\n"
    
    prompt = f"""你是A股AI方向资深分析师。

【市场数据】
- 今日上涨家数: 约{market_data.get('count', 0)//2}家（估算）

【候选股】（按量化策略排序）
{cand_str}

【技术分析】（已计算好）
{tech_str}

【限定方向】
1. 硬件算力（芯片、光模块、服务器）
2. 模型（大模型、AI软件）
3. 绿色电力（储能、光伏）

请做深度分析：

## 一、三个方向简报
每个方向用一句话总结（20字以内）
1. 硬件算力：
2. 模型：
3. 绿色电力：

## 二、候选股技术买卖点
对Top5股给出：
- 代码及名称
- 技术信号：金叉/死叉/震荡
- 趋势：上升/下降/震荡  
- 成交量：放量/缩量/正常
- 建议：买入/卖出/持有
- 买入点位（建议）：[当前价回调X%]
- 卖出点位（止盈）：[当前价上涨X%]
- 止损位：[当前价下跌X%]

格式：
| 代码 | 技术信号 | 趋势 | 成交量 | 建议 | 买 | 卖 | 止 |

## 三、最终推荐（按优先级排序3只）
每只给出：
- 代码+名称
- 推荐理由（30字）
- 买入点位建议
- 目标位
- 止损位

请用表格和清晰格式。"""

    try:
        response = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': f'Bearer {API_KEY}', 'Content-Type': 'application/json'},
            json={
                'model': 'llama-3.3-70b-versatile',
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': 1500,
                'temperature': 0.3
            },
            timeout=60
        )
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        return f"API错误: {response.status_code}"
    except Exception as e:
        return f"分析失败: {str(e)}"

def run_daily_review():
    print("=== A股AI方向每日点评(技术版) ===")
    
    print("获取市场数据...")
    market_data = get_market_data()
    
    print("获取候选股...")
    candidates = get_ai_candidates()
    print(f"候选股: {len(candidates)}只")
    
    # 技术分析
    print("计算技术指标...")
    tech_data = {}
    for ts in candidates['ts_code'].tolist()[:5]:
        tech = get_stock_tech(ts)
        if tech:
            action, reason = get_tech_recommendation(tech)
            tech_data[ts] = tech
            print(f"  {ts}: {tech['trend']}/{tech['signal']}/{tech['vol']} → {action}")
    
    print("Grok分析...")
    analysis = grok_analysis(market_data, candidates, tech_data)
    
    # 生成消息
    msg = f"## A股AI方向每日点评(技术版)\n\n"
    msg += analysis
    
    # 保存
    output_dir = os.path.join(PROJECT_ROOT, 'output')
    os.makedirs(output_dir, exist_ok=True)
    from datetime import datetime
    today = datetime.now().strftime('%Y%m%d')
    candidates.to_csv(f'{output_dir}/ai_review_candidates_{today}.csv', index=False, encoding='utf-8-sig')
    
    print("\n" + "="*50)
    print("分析结果预览:")
    print(analysis[:800])
    print("="*50)
    
    webhook = Config.get('notification.dingtalk.webhook')
    secret = Config.get('notification.dingtalk.secret_word', '提醒')
    notifier = DingTalkNotifier(webhook, secret_word=secret)
    result = notifier.send_message("A股AI每日点评(技术版)", msg)
    print(f"\n钉钉推送{'成功' if result else '失败'}")

if __name__ == '__main__':
    run_daily_review()