import sys
sys.path.insert(0, '.')
import pandas as pd
from datetime import datetime
from src.strategy.center import StrategyCenter
from src.utils.notifier import DingTalkNotifier
from src.utils.config_loader import Config
from src.utils.db_utils import DBUtils

# 获取最新有数据的交易日
from src.utils.db_utils import DBUtils
latest_date = DBUtils.query_df('SELECT MAX(trade_date) as d FROM stock_daily').iloc[0]['d']
trade_date = latest_date
trade_date_compact = trade_date.replace('-', '')
print(f"日期: {trade_date}")

webhook = Config.get('notification.dingtalk.webhook')
secret = Config.get('notification.dingtalk.secret_word', '提醒')
notifier = DingTalkNotifier(webhook, secret_word=secret)

sc = StrategyCenter()
strategies = sc.available_strategies()
print(f"策略数量: {len(strategies)}")
print(f"策略: {strategies}")

results = {}
for name in strategies:
    print(f"\n=== 运行: {name} ===")
    try:
        df = sc.run([name], trade_date, top_k=10)
        if df is not None and len(df) > 0:
            results[name] = df
            print(f"  选出 {len(df)} 只")
    except Exception as e:
        print(f"  错误: {e}")
        import traceback
        traceback.print_exc()

print(f"\n=== 推送 {len(results)} 个策略 ===")
for name, df in results.items():
    title = f"【{name}】策略选股 {trade_date}"
    score_col = 'final_score' if 'final_score' in df.columns else 'score'
    lines = [f"### {name}\n", "| 代码 | 名称 | 分数 |", "|------|------|------|"]
    for _, row in df.head(10).iterrows():
        code = row['ts_code']
        name_col = row.get('name', code)
        score = row.get(score_col, 0)
        lines.append(f"| {code} | {name_col[:6]} | {score:.3f} |")
    content = "\n".join(lines)
    print(f"发送: {name}")
    notifier.send_message(title, content)

print(f"\n=== 保存到数据库 ===")
DBUtils.execute("""
    CREATE TABLE IF NOT EXISTS daily_picks (
        id INT AUTO_INCREMENT PRIMARY KEY,
        trade_date VARCHAR(8) NOT NULL,
        ts_code VARCHAR(20) NOT NULL,
        name VARCHAR(100),
        final_score FLOAT,
        ai_score FLOAT,
        event_score FLOAT,
        fund_score FLOAT,
        track VARCHAR(20),
        concept TEXT,
        industry VARCHAR(100),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

for name, df in results.items():
    score_col = 'final_score' if 'final_score' in df.columns else 'score'
    DBUtils.execute("DELETE FROM daily_picks WHERE trade_date = %s AND track = %s", 
                   (trade_date_compact, name))
    for _, row in df.head(10).iterrows():
        try:
            DBUtils.execute("""
                INSERT INTO daily_picks (trade_date, ts_code, name, final_score, track)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                trade_date_compact,
                row['ts_code'],
                row.get('name', row['ts_code']),
                row.get(score_col, 0),
                name
            ))
        except Exception as e:
            print(f"  写入失败: {e}")
    print(f"  已保存 {name}: {len(df)} 条")

print("\n完成!")