"""
每周小市值选股推送 — 周一 8:35 执行

三策略对比：
  策略A（JQ原版）：ROE>10% + 市值最小100取×2 + 空仓月12/1/4/8 + 搅屎棍过滤，5只
  策略B（评分加权）：市值65%+动量15%+质量10%+MACD7%+52w3%，20只
  策略C（无空仓+创业板）：策略A去除空仓月 + 包含创业板，1年回测+101.9%，5只

市场冰冷检测：搅屎棍行业（银行/煤炭/钢铁）+ 存量市场 → 推送警告，不推荐买入

结果保存：output/small_cap_weekly_YYYYMMDD.txt（供 openclaw/飞书查询）

用法：
  python scripts/weekly_small_cap_push.py          # 直接推钉钉
  python scripts/weekly_small_cap_push.py --dry-run # 只打印，不发钉钉
  python scripts/weekly_small_cap_push.py --top 20  # 策略B输出前N只
"""
import sys
import os
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from src.utils.log_utils import init_logger
logger = init_logger("weekly_small_cap_push")

from src.utils.config_loader import Config
from src.utils.notifier import NotifierFactory
from src.strategy.small_cap_strategy import SmallCapStrategy
from src.strategy.small_cap_pure import PureSmallCapStrategy
from src.utils.db_utils import DBUtils
import pandas as pd
import numpy as np


OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'output')

# 搅屎棍行业（银行暴涨 = 存量市场信号）
JSG_INDUSTRIES = {'银行', '煤炭', '钢铁'}


# ──────────────────────────────────────────
# 市场冰冷检测
# ──────────────────────────────────────────

def _check_market_cold(trade_date: str) -> tuple[bool, str]:
    """
    检测市场是否冰冷（搅屎棍暴涨 = 存量博弈，不适合小市值）

    返回：(is_cold, reason_str)
    判断逻辑：
      1. 计算全市场成交额 20日MA斜率，<=10% 为存量
      2. 统计各行业 20日收益，top行业是搅屎棍（银行/煤炭/钢铁）→ 冰冷
    """
    try:
        # 市场环境（复用 SmallCapStrategy）
        strategy = SmallCapStrategy.__new__(SmallCapStrategy)
        market_env = SmallCapStrategy._calc_market_env(strategy, trade_date)

        if market_env != '存量':
            return False, f"市场环境={market_env}"

        # 查各行业近20日平均涨跌
        date_30_ago = (datetime.strptime(trade_date, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
        sql = """
        SELECT si.industry,
               AVG(sd.pct_chg) AS avg_pct
        FROM stock_daily sd
        JOIN stock_info si ON sd.ts_code = si.ts_code
        WHERE sd.trade_date >= ?
          AND sd.trade_date <= ?
          AND si.industry IS NOT NULL AND si.industry != ''
        GROUP BY si.industry
        ORDER BY avg_pct DESC
        LIMIT 5
        """
        df = DBUtils.query_df(sql, params=(date_30_ago, trade_date))
        if df.empty:
            return True, f"市场环境={market_env}，无行业数据"

        top_industries = df['industry'].tolist()
        jsg_hit = [ind for ind in top_industries[:3] if any(j in ind for j in JSG_INDUSTRIES)]

        if jsg_hit:
            reason = f"市场存量 + 搅屎棍行业领涨（{'/'.join(jsg_hit)}），不适合买小市值"
            return True, reason

        return True, f"市场存量（成交萎缩），建议轻仓"

    except Exception as e:
        logger.warning(f"市场冰冷检测失败: {e}")
        return False, "检测失败，按正常流程"


# ──────────────────────────────────────────
# 消息构建
# ──────────────────────────────────────────

def _industry_summary(df: pd.DataFrame, top_n: int = 5) -> str:
    if 'industry' not in df.columns or df['industry'].isna().all():
        return ""
    counts = df['industry'].fillna('未知').value_counts().head(top_n)
    return "  |  ".join(f"{ind}({n}只)" for ind, n in counts.items())


def _format_stock_line(i: int, row: pd.Series, show_score: bool = True) -> list:
    name  = str(row.get('name', ''))[:5]
    code  = str(row.get('ts_code', ''))[:6]
    mv_yi = float(row.get('total_mv', 0) or 0) / 10000
    roe   = float(row.get('roe', 0) or 0)
    ind   = str(row.get('industry', ''))[:8]
    score = float(row.get('score', 0))

    line = f"{i+1}. **{name}** ({code})  市值{mv_yi:.0f}亿"
    if show_score:
        line += f"  评分{score:.3f}"
    if roe:
        line += f"  ROE={roe:.1f}%"
    if ind:
        line += f"  [{ind}]"
    return [line]


def build_cold_message(reason: str, trade_date: str) -> tuple[str, str]:
    """市场冰冷时的警告推送"""
    today_str = datetime.now().strftime('%m月%d日')
    weekday_cn = {'Monday':'一','Tuesday':'二','Wednesday':'三','Thursday':'四','Friday':'五'}
    weekday = weekday_cn.get(datetime.now().strftime('%A'), '')
    title = f"❄️ 小市值本周空仓 {today_str}" + (f" 周{weekday}" if weekday else "")

    content = "\n".join([
        f"### ❄️ 本周小市值策略空仓  {today_str}",
        f"> 数据日期: {trade_date}\n",
        "---\n",
        f"**原因**: {reason}\n",
        "**操作建议**:",
        "- 策略A（JQ原版）：本周不建仓",
        "- 策略C（无空仓+创业板）：仍会选股，请谨慎参考",
        "- 策略B（评分加权）：可轻仓观察，止损 -5%",
        "- 等待市场环境转为增量后再介入\n",
        "---",
        "> 本提示仅供参考，不构成投资建议",
    ])
    return title, content


def build_message(df_pure: pd.DataFrame, df_scored: pd.DataFrame,
                  trade_date: str, market_note: str = '',
                  df_pure_c: pd.DataFrame = None) -> tuple[str, str]:
    today_str = datetime.now().strftime('%m月%d日')
    weekday_cn = {'Monday':'一','Tuesday':'二','Wednesday':'三','Thursday':'四','Friday':'五'}
    weekday = weekday_cn.get(datetime.now().strftime('%A'), '')
    title = f"📊 小市值三策略 {today_str}" + (f" 周{weekday}" if weekday else "")

    lines = [
        f"### 📊 小市值三策略对比  {today_str}",
        f"> 数据日期: {trade_date}\n",
    ]
    if market_note:
        lines.append(f"> ⚠️ {market_note}\n")
    lines.append("---\n")

    # 策略A
    lines.append(f"**🔹 策略A｜JQ原版** — {len(df_pure)} 只\n")
    lines.append("> ROE>10% + 市值最小×2 + 空仓月1/4/8/12 + 搅屎棍过滤\n")
    if df_pure.empty:
        lines.append("_本周空仓（空仓期或市场信号）_\n")
    else:
        for i, row in df_pure.iterrows():
            lines += _format_stock_line(i, row, show_score=False)
        lines.append("")

    # 策略C（无空仓+创业板）
    if df_pure_c is not None:
        lines.append(f"**🔶 策略C｜无空仓+创业板** — {len(df_pure_c)} 只\n")
        lines.append("> 策略A去除空仓月 + 包含创业板300 | 1年回测+101.9%\n")
        if df_pure_c.empty:
            lines.append("_本周无选股结果_\n")
        else:
            for i, row in df_pure_c.iterrows():
                lines += _format_stock_line(i, row, show_score=False)
            lines.append("")

    # 策略B
    lines.append(f"**🔸 策略B｜评分加权** — {len(df_scored)} 只\n")
    lines.append("> 市值65%+动量15%+质量10%+MACD7%+52w3%\n")
    ind_summary = _industry_summary(df_scored)
    if ind_summary:
        lines.append(f"> 行业: {ind_summary}\n")
    for i, row in df_scored.iterrows():
        lines += _format_stock_line(i, row, show_score=True)
    lines.append("")

    lines += [
        "---",
        "⚠️ 三策略对比观察中，策略C为实验性（1年回测，需持续验证）",
        "建议持有周期：2~4周，止损 -8%，单票 ≤10%",
        "> 本推荐仅供参考，不构成投资建议",
    ]
    return title, "\n".join(lines)


def _save_result(content: str, trade_date: str):
    """保存结果到 output/small_cap_weekly_YYYYMMDD.txt，供飞书/openclaw查询"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    date_str = trade_date.replace('-', '') if trade_date else datetime.now().strftime('%Y%m%d')
    path = os.path.join(OUTPUT_DIR, f"small_cap_weekly_{date_str}.txt")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(content)
    print(f"[OK] 结果已保存: {path}")
    return path


# ──────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────

def run(top_k: int = 20, dry_run: bool = False) -> bool:
    print("=" * 60)
    print("  小市值双策略推送")
    print("=" * 60)

    # 先获取交易日期
    trade_date = (pd.Timestamp.now() - pd.Timedelta(days=1)).strftime('%Y-%m-%d')

    # 市场冰冷检测
    is_cold, cold_reason = _check_market_cold(trade_date)
    print(f"\n[市场环境] {cold_reason}")

    if is_cold and '搅屎棍' in cold_reason:
        # 搅屎棍暴涨 → 发空仓警告
        title, content = build_cold_message(cold_reason, trade_date)
        _save_result(content, trade_date)

        if dry_run:
            print("\n[DRY-RUN] ===== 市场冰冷警告 =====")
            print(f"标题: {title}")
            print(content)
            print("[DRY-RUN] ===========================\n")
            return True

        _send_dingtalk(title, content)
        return True

    # 正常选股流程
    market_note = cold_reason if is_cold else ''

    print("\n[策略A] JQ原版（top 5）...")
    pure = PureSmallCapStrategy()
    df_pure = pure.run(top_k=5)
    if df_pure is None or df_pure.empty:
        print("[WARN] 策略A无结果（空仓期或无符合股票）")
        df_pure = pd.DataFrame()

    print("\n[策略C] 无空仓+创业板（top 5）...")
    pure_c = PureSmallCapStrategy(include_300=True, empty_months=set())
    df_pure_c = pure_c.run(top_k=5)
    if df_pure_c is None or df_pure_c.empty:
        print("[WARN] 策略C无结果")
        df_pure_c = pd.DataFrame()

    print(f"\n[策略B] 评分加权（top {top_k}）...")
    scored = SmallCapStrategy()
    df_scored = scored.run(top_k=top_k)
    if df_scored is None or df_scored.empty:
        print("[WARN] 策略B无结果")
        df_scored = pd.DataFrame()

    for df in [df_pure, df_pure_c, df_scored]:
        if not df.empty:
            trade_date = str(df.iloc[0].get('trade_date', trade_date))
            break

    print(f"\n[OK] 策略A={len(df_pure)}只  策略C={len(df_pure_c)}只  策略B={len(df_scored)}只  日期={trade_date}")

    title, content = build_message(df_pure, df_scored, trade_date, market_note, df_pure_c=df_pure_c)
    _save_result(content, trade_date)

    if dry_run:
        print("\n[DRY-RUN] ===== 推送内容预览 =====")
        print(f"标题: {title}")
        print(content)
        print("[DRY-RUN] ===========================\n")
        return True

    return _send_dingtalk(title, content)


def _send_dingtalk(title: str, content: str) -> bool:
    notif_cfg = Config.get('notification') or {}
    if not notif_cfg.get('enabled', False):
        print("[WARN] 通知功能未启用")
        return False
    ding_cfg = notif_cfg.get('dingtalk') or {}
    webhook = ding_cfg.get('webhook', '')
    secret  = ding_cfg.get('secret_word', '提醒')
    if not webhook:
        print("[ERROR] 钉钉 webhook 未配置")
        return False
    notifier = NotifierFactory.create_notifier('dingtalk', webhook_url=webhook, secret_word=secret)
    success = notifier.send_message(title, content, message_type='morning_push')
    print("[OK] 推送成功" if success else "[ERROR] 推送失败")
    return success


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='小市值双策略钉钉推送')
    parser.add_argument('--top',     type=int,  default=20,    help='策略B输出数量（默认20）')
    parser.add_argument('--dry-run', action='store_true',      help='只打印，不发钉钉')
    args = parser.parse_args()
    print(f"\n[INFO] 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    ok = run(top_k=args.top, dry_run=args.dry_run)
    sys.exit(0 if ok else 1)
