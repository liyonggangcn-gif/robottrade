"""
A股主题ETF挑选：按流动性+规模+行业关键词筛选，可与行业择机联动。

用法:
    python scripts/run_etf_selector.py              # 使用 config 默认行业列表
    python scripts/run_etf_selector.py --industry  # 与行业择机联动（需请求行业接口）
"""

import sys
import os
import argparse

# 先清代理，避免东方财富/akshare 请求走代理失败
for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
    os.environ.pop(_k, None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 初始化日志
from src.utils.log_utils import init_logger
logger = init_logger("run_etf_selector")

from src.analysis.etf_selector import run, run_with_industry_timing
from src.utils.config_loader import Config


def main():
    parser = argparse.ArgumentParser(description="A股主题ETF挑选")
    parser.add_argument("--industry", action="store_true", help="与行业择机联动（拉取行业择机结果再匹配ETF）")
    parser.add_argument("--top", type=int, default=2, help="每个行业最多几只ETF，默认2")
    parser.add_argument("--dry-run", action="store_true", help="不请求接口，用模拟数据展示输出格式（网络异常时可用）")
    parser.add_argument("--llm", action="store_true", help="调用 Grok 生成 ETF 买卖/选择建议")
    args = parser.parse_args()

    if args.dry_run:
        _run_dry_run(hold_hint=(Config.get("etf_selector") or {}).get("holding_period_hint", "1周～3个月"))
        return

    if args.industry:
        from src.analysis.industry_timing import IndustryTiming
        timing = IndustryTiming()
        industry_data = timing.run_split(max_industries=40, emerging_top=6, mature_top=6)
        result = run_with_industry_timing(industry_data, top_per_industry=args.top)
    else:
        result = run(industry_names=None, top_per_industry=args.top)

    if result.get("error"):
        print(f"[ERROR] {result['error']}")
        sys.exit(1)

    cfg = Config.get("etf_selector") or {}
    hold_hint = cfg.get("holding_period_hint", "1周～3个月")
    print("\n=== A股主题ETF挑选结果 ===\n")
    print(f"建议持有周期: {hold_hint}")
    print(f"全市场ETF数量: {result.get('etf_all_count', 0)}")
    print(f"通过流动性+规模过滤后: {result.get('etf_filtered_count', 0)}\n")

    by_ind = result.get("by_industry") or {}
    if not by_ind:
        print("未匹配到任何行业主题ETF，可检查 config etf_selector.industry_keywords 或行业名。")
        return

    for ind, etf_list in by_ind.items():
        print(f"【{ind}】")
        for x in etf_list:
            pct = x.get("涨跌幅")
            pct_str = f" {pct:+.1f}%" if pct is not None else ""
            signal = x.get("signal", "")
            score = x.get("score")
            strategy = x.get("strategy", "")
            score_str = f"  评分{score}" if score is not None else ""
            signal_str = f" [{signal}]" if signal else ""
            strategy_str = f"  {strategy}" if strategy else ""
            print(f"  {x.get('name', '')} ({x.get('code', '')}){pct_str}  成交额{x.get('成交额_万', 0):.0f}万  规模{x.get('总市值_亿', 0):.1f}亿{score_str}{signal_str}{strategy_str}")
        print()

    if args.llm and by_ind:
        cfg = Config.get("etf_selector") or {}
        if cfg.get("use_llm_advice", True):
            try:
                from src.utils.llm_client import LLMClient
                client = LLMClient()
                if client.is_available():
                    summary = "当前根据主题ETF列表与行业匹配结果生成建议（未接入行业择机时可结合模型知识）。"
                    hold = (cfg.get("holding_period_hint") or "1周～3个月")
                    advice = client.generate_etf_advice(summary, by_ind, hold)
                    if advice:
                        print("【Grok 建议】")
                        print(advice)
                else:
                    print("[WARN] LLM 未配置或不可用，跳过 Grok 建议")
            except Exception as e:
                print(f"[WARN] Grok 建议失败: {e}")
    print("=== 结束 ===")


def _run_dry_run(hold_hint="1周～3个月"):
    """用模拟数据跑一遍，展示输出格式（不请求东方财富）。"""
    result = {
        "by_industry": {
            "电子": [
                {"code": "512480", "name": "半导体ETF", "涨跌幅": 1.2, "成交额_万": 12000, "总市值_亿": 85.2, "score": 4, "signal": "重点关注", "strategy": "渗透率·破壁期 RS+5.2%"},
                {"code": "515000", "name": "科技ETF", "涨跌幅": 0.8, "成交额_万": 8000, "总市值_亿": 62.1, "score": 3, "signal": "积极配置", "strategy": "渗透率·高速期 RS+2.1%"},
            ],
            "银行": [
                {"code": "512800", "name": "银行ETF", "涨跌幅": -0.3, "成交额_万": 5000, "总市值_亿": 120.5, "score": 3, "signal": "积极配置", "strategy": "周期·早周期匹配 季+3.5% 年+8.2%"},
            ],
            "医药生物": [
                {"code": "512010", "name": "医药ETF", "涨跌幅": 0.5, "成交额_万": 3500, "总市值_亿": 45.0, "score": 0, "signal": "观望", "strategy": "周期·中周期不匹配 季-1.2% 年+2.0%"},
            ],
        },
        "etf_all_count": 600,
        "etf_filtered_count": 320,
        "error": None,
    }
    print("\n=== A股主题ETF挑选结果（dry-run 模拟）===\n")
    print(f"建议持有周期: {hold_hint}")
    print(f"全市场ETF数量: {result.get('etf_all_count', 0)}")
    print(f"通过流动性+规模过滤后: {result.get('etf_filtered_count', 0)}\n")
    for ind, etf_list in result["by_industry"].items():
        print(f"【{ind}】")
        for x in etf_list:
            pct = x.get("涨跌幅")
            pct_str = f" {pct:+.1f}%" if pct is not None else ""
            signal = x.get("signal", "")
            score = x.get("score")
            strategy = x.get("strategy", "")
            score_str = f"  评分{score}" if score is not None else ""
            signal_str = f" [{signal}]" if signal else ""
            strategy_str = f"  {strategy}" if strategy else ""
            print(f"  {x.get('name', '')} ({x.get('code', '')}){pct_str}  成交额{x.get('成交额_万', 0):.0f}万  规模{x.get('总市值_亿', 0):.1f}亿{score_str}{signal_str}{strategy_str}")
        print()
    print("=== 结束（真实运行请去掉 --dry-run，并确保网络/代理可访问东方财富）===")


if __name__ == "__main__":
    main()
