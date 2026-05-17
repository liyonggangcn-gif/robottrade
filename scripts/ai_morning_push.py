import sys
import os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.utils.db_utils import DBUtils
from src.utils.config_loader import Config
from src.utils.notifier import DingTalkNotifier
from src.utils.llm_client import LLMClient
import pandas as pd

def get_stock_info(ts_code):
    try:
        df = DBUtils.query_df('''
            SELECT name, industry
            FROM stock_info WHERE ts_code = %s
        ''', (ts_code,))
        if df.empty:
            return {'name': '', 'industry': ''}
        return {'name': df.iloc[0].get('name', ''), 'industry': df.iloc[0].get('industry', '')}
    except:
        return {'name': '', 'industry': ''}

def analyze_company(ts_code, name):
    """用Grok分析公司在AI领域的护城河"""
    prompt = f"""请分析{name}({ts_code})在AI领域的竞争优势和护城河：

1. 主营业务和AI相关产品？
2. 在AI产业链中的位置？
3. 技术壁垒有多深？
4. 请用30字以内简洁回答。"""

    try:
        import requests
        api_key = os.environ.get('GROQ_API_KEY', '')
        response = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': 'llama-3.3-70b-versatile', 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 60},
            timeout=30
        )
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content'].strip()[:60]
        return f"API错误: {response.status_code}"
    except Exception as e:
        return f"分析失败: {str(e)[:20]}"

def run_ai_push():
    print("=== AI动量+质量选股+DeepSeek分析 ===")
    
    df_ai = DBUtils.query_df("SELECT DISTINCT ts_code FROM ai_stock_pool")
    ai_ts_codes = set(df_ai['ts_code'].tolist())
    if not ai_ts_codes:
        return
    print(f"AI成分股: {len(ai_ts_codes)}只")
    
    trade_dates = DBUtils.query_df("SELECT DISTINCT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT 60")
    dates = trade_dates['trade_date'].tolist()
    if len(dates) < 40:
        return
    
    # 计算动量+质量
    data = []
    for ts_code in list(ai_ts_codes):
        df_stock = DBUtils.query_df('''
            SELECT trade_date, close FROM stock_daily 
            WHERE ts_code = %s AND trade_date IN (''' + ','.join(['%s'] * min(60, len(dates))) + ''')
            ORDER BY trade_date
        ''', (ts_code, *dates[:60]))
        
        if len(df_stock) >= 40:
            closes = df_stock['close'].astype(float).values
            mom_20 = (closes[-1] / closes[-20] - 1) * 100 if closes[-20] > 0 else 0
            daily_returns = (closes[1:] / closes[:-1] - 1)
            positive_days = (daily_returns > 0).sum()
            quality = positive_days / (len(daily_returns) - 1) * 100 if len(daily_returns) > 1 else 50
            if mom_20 > -50 and quality > 30:
                data.append({'ts_code': ts_code, 'mom_20': mom_20, 'quality': quality})
    
    df = pd.DataFrame(data)
    df['score'] = df['mom_20'] * 0.6 + df['quality'] * 0.4
    df = df.sort_values('score', ascending=False).head(5)
    
    # 获取名称
    if not df.empty:
        names, inds = [], []
        for ts in df['ts_code'].tolist():
            info = get_stock_info(ts)
            names.append(info.get('name', ''))
            inds.append(info.get('industry', '')[:8])
        df['name'] = names
        df['industry'] = inds
    
    # DeepSeek分析
    print("DeepSeek分析中...")
    analysis = {}
    for _, r in df.iterrows():
        ts = r['ts_code']
        name = r['name']
        print(f"  分析 {name}...")
        analysis[ts] = analyze_company(ts, name)
    
    # 生成消息
    lines = ["## AI动量+质量 Top5 + DeepSeek点评", ""]
    lines.append("**公式: 20日动量×0.6 + 质量×0.4**")
    lines.append("")
    
    for i, (_, r) in enumerate(df.iterrows(), 1):
        code = r['ts_code']
        name = r.get('name', '')[:6]
        ind = r.get('industry', '')[:8]
        anal = analysis.get(code, '')
        
        lines.append(f"**{i}. {code} {name}** ({ind})")
        lines.append(f"   动量: {r['mom_20']:+.1f}% | 质量: {r['quality']:.0f}% | 综合: {r['score']:.1f}")
        if anal:
            lines.append(f"   分析: {anal[:60]}")
        lines.append("")
    
    msg = '\n'.join(lines)
    print(msg[:300])
    
    # 发送钉钉
    webhook = Config.get('notification.dingtalk.webhook')
    secret = Config.get('notification.dingtalk.secret_word', '提醒')
    notifier = DingTalkNotifier(webhook, secret_word=secret)
    result = notifier.send_message("AI动量+DeepSeek分析", msg)
    print(f"推送{'成功' if result else '失败'}")

if __name__ == '__main__':
    run_ai_push()