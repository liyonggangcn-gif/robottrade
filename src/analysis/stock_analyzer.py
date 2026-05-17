"""
个股深度分析引擎
生成五段式分析报告，按公司类型选择专属分析视角。

五段式结构：
  一、盈利逻辑    — 这公司靠什么赚钱，核心驱动因子当前状态
  二、未来展望    — 三情景（悲观/基准/乐观）+ 关键变量
  三、估值判断    — 类型专属估值方法 + 安全边际
  四、操作建议    — 买/不买/等 + 具体参数（建仓价/目标价/止损）
  五、跟踪要点    — 下次需关注什么，何时触发复审

触发时机（任一）：
  - PoolStrategy 发现买入信号
  - 财报发布
  - 用户手动调用
  - 重大新闻影响核心驱动因子
"""

import json
from datetime import date
from typing import Optional

from src.utils.db_utils import DBUtils
from src.utils.llm_client import LLMClient
from src.classifier.company_classifier import CompanyClassifier, TYPE_META
from src.valuation.valuation_engine import ValuationEngine
from src.universe.stock_pool import StockPool
from src.analysis.financial_fetcher import FinancialFetcher


# 各类型的分析视角指引（注入给 LLM 的上下文）
_TYPE_ANALYSIS_GUIDE = {
    "resource": """
分析视角：资源价格型公司，盈利主要由大宗商品价格驱动。
重点关注：① 商品价格历史分位（低位是买入机会）② 产能利用率 ③ 成本护城河
估值方法：PB + 商品价格周期位置（PE在周期底部会虚高，勿用PE判断）
陷阱提示：商品价格高位时PE最低，那反而是卖出时机。""",

    "brand": """
分析视角：品牌定价型公司，核心是定价权和消费者复购率。
重点关注：① PE历史分位（<25%是历史便宜区）② 渠道库存健康度 ③ 提价能力
估值方法：PE历史分位法（5年）+ 自由现金流收益率
陷阱提示：短期业绩波动不代表品牌逻辑破坏，区分一次性因素很重要。""",

    "growth": """
分析视角：成长渗透型公司，核心是行业渗透率和市场份额。
重点关注：① PEG < 1 是相对低估 ② 市占率是否稳定或提升 ③ 毛利率能否守住
估值方法：PEG法（成长期）→ PE分位（成熟期），随渗透率动态切换
陷阱提示：渗透率>60%后增速必然放缓，需提前识别周期转换。""",

    "rate_sensitive": """
分析视角：利率敏感型，盈利随利率/信用周期波动显著。
重点关注：① PB < 1 是便宜信号 ② 净息差趋势（NIM）③ 不良贷款率
估值方法：PB/ROE 法（PB < 历史35分位是低估）
陷阱提示：加息周期息差会持续收窄，PE看起来低但盈利还会继续恶化。""",

    "policy": """
分析视角：政策驱动型，盈利高度依赖政策支持和行业景气。
重点关注：① 政策信号（补贴/规划/招标节奏）② 行业排产景气度 ③ 竞争格局
估值方法：PE历史分位 + 政策周期位置（政策加码期可给予估值溢价）
陷阱提示：政策转向时杀估值很快，需关注政策持续性。""",

    "cashflow": """
分析视角：稳定现金流型，靠稳定流量×受监管费率赚钱。
重点关注：① 股息率 > 无风险利率1.5倍 ② 负债率可控 ③ 折旧完成后现金释放
估值方法：股息率法 + EV/EBITDA（PE因折旧影响会失真）
陷阱提示：利率上行时债券替代效应明显，股息率需与无风险利率动态比较。""",
}

# 操作建议模板（供 LLM 参考格式）
_REPORT_FORMAT = """
请严格按照以下格式输出，每段标题保持不变，内容简洁（总字数600字以内）：

【一、盈利逻辑】
（2-3句话描述这公司靠什么赚钱，当前核心驱动因子处于什么状态）

【二、未来展望】
悲观情景（概率xx%）：净利润增速约x%，原因...
基准情景（概率xx%）：净利润增速约x%，原因...
乐观情景（概率xx%）：净利润增速约x%，原因...
关键变量：...（1-2个最影响结果的变量）

【三、估值判断】
当前：PE=xx  PB=xx  历史分位xx%（5年）
结论：[低估/合理/高估]，安全边际约xx%
估值方法：（说明用了哪种方法）

【四、操作建议】
结论：[可建仓/暂观望/回避]
理由：（1句话）
建仓价：xx元以下  目标价：xx元  止损线：xx元

【五、跟踪要点】
下月关注：（1-2件具体的事）
触发复审：（什么情况出现时需要重新判断）
"""


class StockAnalyzer:
    """个股深度分析引擎"""

    def __init__(self):
        self.llm = LLMClient()
        self.classifier = CompanyClassifier()
        self.pool = StockPool()
        self.valuation_engine = ValuationEngine()
        self.financial_fetcher = FinancialFetcher()

    def analyze(self, ts_code: str, trigger: str = "manual",
                force_refresh: bool = False) -> dict:
        """
        生成个股深度分析报告。

        Args:
            ts_code:       股票代码，如 '600519.SH'
            trigger:       触发原因（manual/earnings/buy_signal/news）
            force_refresh: 是否强制重新生成（忽略缓存）

        Returns:
            {
              "ts_code": str,
              "company_name": str,
              "company_type": str,
              "report": str,         # 五段式 Markdown 报告
              "action": str,         # 可建仓/暂观望/回避
              "trigger": str,
              "generated_date": str,
            }
        """
        print(f"[StockAnalyzer] 开始分析 {ts_code} (trigger={trigger})")

        # 1. 基础信息
        info = self._get_stock_info(ts_code)
        company_type = info.get("company_type", "growth")
        company_name = info.get("name", ts_code)

        # 2. 财务数据
        print(f"[StockAnalyzer] 拉取财务数据...")
        fin_summary = self.financial_fetcher.get_summary(ts_code)

        # 3. 估值数据
        val = self.valuation_engine.get_latest(ts_code)

        # 4. 公司档案（池内研究笔记 + 驱动因子配置）
        profile = self.pool.get_profile(ts_code)

        # 5. 拼装 LLM 上下文
        context = self._build_context(ts_code, info, fin_summary, val, profile)

        # 6. 调用 LLM 生成报告
        print(f"[StockAnalyzer] 调用 LLM 生成报告...")
        report_text = self._call_llm(company_type, company_name, ts_code, context)

        # 7. 提取操作结论
        action = self._extract_action(report_text)

        # 8. 保存到研究日志
        result = {
            "ts_code": ts_code,
            "company_name": company_name,
            "company_type": company_type,
            "report": report_text,
            "action": action,
            "trigger": trigger,
            "generated_date": date.today().isoformat(),
        }
        self._save_to_log(ts_code, trigger, report_text, action)

        # 9. 更新档案的最后分析日期
        if self.pool.is_in_pool(ts_code):
            self.pool.update_profile(
                ts_code,
                last_analysis_date=date.today().isoformat(),
                research_notes=report_text[:500],  # 存摘要
            )

        print(f"[StockAnalyzer] 分析完成: {ts_code} {company_name} -> {action}")
        return result

    def analyze_pool_signals(self, pool_result: dict) -> list:
        """
        对 PoolStrategy 产生买入信号的股票批量分析。
        返回报告列表。
        """
        signals = pool_result.get("signals")
        if signals is None or signals.empty:
            return []

        reports = []
        for _, row in signals.iterrows():
            try:
                result = self.analyze(row["ts_code"], trigger="buy_signal")
                reports.append(result)
            except Exception as e:
                print(f"[StockAnalyzer] {row['ts_code']} 分析失败: {e}")
        return reports

    def format_report_for_dingtalk(self, result: dict) -> str:
        """格式化为钉钉 Markdown"""
        name = result["company_name"]
        ts_code = result["ts_code"]
        ctype_name = self.classifier.get_type_name(result["company_type"])
        trigger_map = {
            "buy_signal": "进入买入区",
            "earnings":   "财报发布",
            "news":       "重大新闻",
            "manual":     "手动触发",
        }
        trigger_label = trigger_map.get(result["trigger"], result["trigger"])
        action_icon = {"可建仓": "✅", "暂观望": "⏳", "回避": "❌"}.get(result["action"], "📊")

        lines = [
            f"### {action_icon} 个股分析：{name}（{ts_code[:6]}）",
            f"> {ctype_name} | 触发：{trigger_label} | {result['generated_date']}",
            "",
            result["report"],
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────────

    def _get_stock_info(self, ts_code: str) -> dict:
        df = DBUtils.query_df(
            "SELECT name, industry, pe_ttm, pb, total_mv FROM stock_info WHERE ts_code = ?",
            params=[ts_code],
        )
        if df.empty:
            return {"ts_code": ts_code, "name": ts_code, "company_type": "growth"}

        row = df.iloc[0].to_dict()
        row["ts_code"] = ts_code
        row["company_type"] = self.classifier.classify(ts_code, row.get("industry"))
        return row

    def _build_context(self, ts_code, info, fin_summary, val, profile) -> str:
        """将所有数据拼装成 LLM 上下文文本"""
        lines = []

        # 基础信息
        lines.append(f"公司：{info.get('name', ts_code)} ({ts_code})")
        lines.append(f"行业：{info.get('industry', 'N/A')}")
        lines.append(f"公司类型：{self.classifier.get_type_name(info.get('company_type', 'growth'))}")

        # 驱动因子（档案配置）
        if profile:
            drivers = profile.get("profit_drivers") or {}
            driver_list = drivers.get("drivers", []) if isinstance(drivers, dict) else []
            if driver_list:
                lines.append(f"核心驱动因子：{', '.join(driver_list)}")

            research_notes = profile.get("research_notes", "")
            if research_notes and len(research_notes) > 10:
                lines.append(f"历史研究笔记：{research_notes[:200]}")

        # 最新估值
        lines.append("\n【当前估值】")
        if val:
            pe = val.get("pe_ttm") or info.get("pe_ttm")
            pb = val.get("pb") or info.get("pb")
            pe_pct = val.get("pe_percentile_5y")
            pb_pct = val.get("pb_percentile_5y")
            peg = val.get("peg")
            dy = val.get("dividend_yield")
            margin = val.get("safety_margin")
            target = val.get("target_price_mid")
            signal = val.get("valuation_signal", "unknown")

            if pe:   lines.append(f"  PE(TTM)={pe:.1f}  PE历史5年分位={pe_pct:.0f}%" if pe_pct else f"  PE(TTM)={pe:.1f}")
            if pb:   lines.append(f"  PB={pb:.2f}  PB历史5年分位={pb_pct:.0f}%" if pb_pct else f"  PB={pb:.2f}")
            if peg:  lines.append(f"  PEG={peg:.2f}")
            if dy:   lines.append(f"  股息率(估算)={dy:.1f}%")
            if target: lines.append(f"  中性目标价={target:.0f}元")
            if margin: lines.append(f"  安全边际={margin*100:+.0f}%")
            lines.append(f"  估值信号：{signal}")
        else:
            pe = info.get("pe_ttm")
            pb = info.get("pb")
            if pe: lines.append(f"  PE(TTM)={pe:.1f}")
            if pb: lines.append(f"  PB={pb:.2f}")

        # 财务数据
        lines.append("\n【财务指标（近期）】")
        if fin_summary.get("available"):
            lines.append(f"  报告期：{fin_summary.get('report_date', '')}")
            lines.append(f"  ROE={fin_summary['roe']}  趋势：{fin_summary['roe_trend']}")
            lines.append(f"  毛利率={fin_summary['gross_margin']}  趋势：{fin_summary['margin_trend']}")
            lines.append(f"  净利润增速={fin_summary['net_profit_yoy']}  趋势：{fin_summary['profit_trend']}")
            lines.append(f"  营收增速={fin_summary['revenue_yoy']}")
            lines.append(f"  资产负债率={fin_summary['debt_ratio']}")
            cq = fin_summary.get("cashflow_quality")
            if cq != "N/A":
                lines.append(f"  经营现金流/净利润={cq}（>1优秀，<0.5警惕）")
            lines.append(f"  近4期ROE：{fin_summary['recent_roe']}")
            lines.append(f"  近4期净利润增速：{fin_summary['recent_profit_growth']}")
        else:
            lines.append(f"  {fin_summary.get('note', '暂无财务数据')}")

        return "\n".join(lines)

    def _call_llm(self, company_type: str, company_name: str,
                  ts_code: str, context: str) -> str:
        """调用 LLM 生成五段式报告"""

        type_guide = _TYPE_ANALYSIS_GUIDE.get(company_type, _TYPE_ANALYSIS_GUIDE["growth"])

        system_prompt = f"""你是一位专业的A股基本面分析师，专注价值投资。
核心投资逻辑：研究清楚公司未来能赚多少钱，在股价便宜时买入，高估时卖出。

当前分析的公司类型视角：{type_guide}

输出要求：
- 严格按照五段式模板输出
- 语言简洁实用，可直接用于投资决策
- 总字数控制在600字以内
- 数字要具体（增速%、PE倍数、目标价等）
- 不确定的内容用"N/A"或注明"需进一步核实"，不要编造数据"""

        user_prompt = f"""请分析以下股票并生成投资分析报告：

{context}

{_REPORT_FORMAT}"""

        try:
            report = self.llm._call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.5,
                max_tokens=1200,
            )
            return report if report else self._fallback_report(company_name, ts_code, context)
        except Exception as e:
            print(f"[StockAnalyzer] LLM 调用失败: {e}")
            return self._fallback_report(company_name, ts_code, context)

    def _fallback_report(self, name: str, ts_code: str, context: str) -> str:
        """LLM 不可用时生成规则化报告"""
        return f"""【一、盈利逻辑】
LLM服务暂不可用，以下为规则生成的数据摘要。

【二、未来展望】
需结合财务数据趋势人工判断。

【三、估值判断】
请参考上下文中的估值数据：
{context[:400]}

【四、操作建议】
结论：暂观望
建议先查看完整财务数据后再做判断。

【五、跟踪要点】
建议配置好 LLM API 后重新运行深度分析。"""

    def _extract_action(self, report_text: str) -> str:
        """从报告中提取操作结论"""
        if not report_text:
            return "暂观望"
        text = report_text.lower()
        if "可建仓" in report_text:  return "可建仓"
        if "回避" in report_text:    return "回避"
        return "暂观望"

    def _save_to_log(self, ts_code: str, trigger: str,
                     report: str, action: str):
        self.pool.add_research_log(
            ts_code,
            trigger_type=trigger,
            summary=report[:800],    # 截断存储
            action_suggestion=action,
        )
