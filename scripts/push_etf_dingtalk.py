"""
将最新 ETF 策略结果推送到钉钉（读取 output/etf_picks_YYYYMMDD.csv）。
"""
import sys
import os
import io
import glob

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 初始化日志
from src.utils.log_utils import init_logger
logger = init_logger("push_etf_dingtalk")

import pandas as pd
from datetime import datetime
from src.utils.config_loader import Config
from src.utils.notifier import DingTalkNotifier


def main():
    # 找最新的 CSV
    output_dir = os.path.join(os.path.dirname(__file__), '..', 'output')
    csv_files = sorted(glob.glob(os.path.join(output_dir, 'etf_picks_*.csv')))
    if not csv_files:
        print("[ERROR] 未找到 ETF 结果 CSV 文件")
        # 尝试发送错误通知
        try:
            notif_cfg = Config.get('notification') or {}
            ding = notif_cfg.get('dingtalk') or {}
            webhook = ding.get('webhook', '')
            secret = ding.get('secret_word', '提醒')
            if webhook:
                from src.utils.notifier import DingTalkNotifier
                notifier = DingTalkNotifier(webhook, secret)
                notifier.send_message("ETF推送错误", "未找到ETF结果文件，请检查数据同步情况。")
        except:
            pass
        sys.exit(1)
    csv_path = csv_files[-1]
    print(f"[INFO] 读取: {csv_path}")

    df = pd.read_csv(csv_path)
    if df.empty:
        print("[ERROR] CSV 为空")
        sys.exit(1)

    # 构造钉钉消息
    cfg = Config.get('etf_selector') or {}
    hold_hint = cfg.get('holding_period_hint', '1周～3个月')
    today = datetime.now().strftime('%Y-%m-%d %H:%M')

    title = '提醒：A股主题ETF策略推荐'
    content = '**A股主题ETF策略推荐**\n\n'
    content += f'日期 {today}\n'
    content += f'建议持有周期 {hold_hint}\n'
    content += f'入选ETF {len(df)}只\n\n'

    sig_icon = {
        '重点关注': '★',
        '积极配置': '▲',
        '适度配置': '●',
        '观望': '○',
        '回避': '×',
    }

    # 按评分降序
    df = df.sort_values('score', ascending=False)

    current_ind = ''
    for _, row in df.iterrows():
        ind = str(row.get('industry', ''))
        if ind != current_ind:
            content += f'\n**{ind}**\n'
            current_ind = ind
        icon = sig_icon.get(str(row.get('signal', '')), '')
        name = row.get('name', '')
        code = row.get('code', '')
        score = int(row.get('score', 0))
        strategy = row.get('strategy', '')
        pct = row.get('涨跌幅', None)
        pct_str = f' {pct:+.1f}%' if pd.notna(pct) else ''
        amount = row.get('成交额_万', 0)
        content += f'{icon}{name}({code}){pct_str} 成交{amount:.0f}万 评分{score} {strategy}\n'

    content += '\n---\n'
    content += '★重点关注(渗透率破壁期) ▲积极配置(高速期/周期匹配) ●适度配置 ○观望 ×回避\n'
    content += '策略基于渗透率阶段+经济周期理论，适合中长期持有。\n'
    content += '仅供参考，理性决策，注意风险。'

    print("\n===== 钉钉消息预览 =====")
    print(content)
    print("========================\n")

    # 发送钉钉
    notif_cfg = Config.get('notification') or {}
    ding = notif_cfg.get('dingtalk') or {}
    webhook = ding.get('webhook', '')
    secret = ding.get('secret_word', '提醒')

    if not webhook:
        print("[ERROR] 钉钉 webhook 未配置")
        sys.exit(1)

    notifier = DingTalkNotifier(webhook, secret)
    ok = notifier.send_message(title, content, message_type='etf_strategy')
    print(f"发送结果: {'成功' if ok else '失败'}")


if __name__ == '__main__':
    main()
