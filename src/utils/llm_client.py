import os
import time
import random
from openai import OpenAI
from loguru import logger
from src.utils.config_loader import Config

# LLM 调用全局参数
_LLM_TIMEOUT    = 60   # 单次 API 调用超时（秒）
_LLM_MAX_RETRY  = 3    # 最大重试次数
_LLM_RETRY_BASE = 1.0  # 指数退避基础（1→2→4 秒）

# 尝试导入 Anthropic SDK（可选）
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    logger.warning("Anthropic SDK not installed. Install with: pip install anthropic")

class LLMClient:
    """LLM客户端，支持Grok、Claude等模型"""
    
    def __init__(self):
        """初始化LLM客户端"""
        self.config = Config
        self.client = None
        self.model = None
        self.provider = None
        self.use_anthropic = False
        
        # 尝试初始化
        self._init_client()
    
    def _init_client(self):
        """初始化LLM客户端"""
        try:
            # 获取LLM配置
            llm_config = self.config.get('llm', {})
            provider = llm_config.get('provider', 'grok').lower()
            base_url = llm_config.get('base_url', 'https://api.groq.com/openai/v1')
            api_key = llm_config.get('api_key', '')
            model = llm_config.get('model', 'llama-3.1-8b-instant')
            
            # 检查API Key是否配置
            if not api_key or 'YOUR_API_KEY_HERE' in api_key:
                logger.warning("LLM API key not configured, LLM features will be disabled")
                return
            
            self.provider = provider
            self.model = model
            
            # 根据provider选择客户端
            if provider == 'claude' or provider == 'anthropic':
                if not ANTHROPIC_AVAILABLE:
                    logger.error("Anthropic SDK not installed. Install with: pip install anthropic")
                    return
                
                # 初始化Anthropic客户端
                self.client = anthropic.Anthropic(api_key=api_key)
                self.use_anthropic = True
                logger.info(f"Successfully initialized Anthropic client: Claude ({model})")
                
            else:
                # 初始化OpenAI兼容客户端（DeepSeek等）
                self.client = OpenAI(
                    api_key=api_key,
                    base_url=base_url
                )
                self.use_anthropic = False
                logger.info(f"Successfully initialized LLM client: {provider} ({model})")
            
        except Exception as e:
            logger.error(f"Failed to initialize LLM client: {e}")
            self.client = None
    
    def is_available(self):
        """检查LLM是否可用"""
        return self.client is not None
    
    def _call_llm_once(self, system_prompt, user_prompt, temperature=0.7, max_tokens=1000):
        """单次 LLM 调用（带超时），失败时抛异常"""
        if self.use_anthropic:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                timeout=_LLM_TIMEOUT,
            )
            return message.content[0].text
        else:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=_LLM_TIMEOUT,
            )
            return response.choices[0].message.content

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """去除 LLM 推理模型的 <think>...</think> 思维链标签"""
        if not text:
            return text
        import re
        cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        return cleaned.strip()

    def _call_llm(self, system_prompt, user_prompt, temperature=0.7, max_tokens=1000):
        """统一的LLM调用方法，支持OpenAI和Anthropic API。
        内置超时（60s）+ 指数退避重试（最多3次），全部失败返回 None。

        Args:
            system_prompt: 系统提示词
            user_prompt:   用户提示词
            temperature:   温度参数
            max_tokens:    最大token数

        Returns:
            LLM返回的文本内容（已去除 <think> 标签），或 None（不可用/全部重试失败）
        """
        if not self.is_available():
            return None

        for attempt in range(1, _LLM_MAX_RETRY + 1):
            try:
                text = self._call_llm_once(system_prompt, user_prompt, temperature, max_tokens)
                return self._strip_thinking(text)
            except Exception as e:
                if attempt < _LLM_MAX_RETRY:
                    wait = _LLM_RETRY_BASE * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    logger.warning(
                        f"[LLM] 调用失败 (第{attempt}/{_LLM_MAX_RETRY}次): {e}，"
                        f"{wait:.1f}s 后重试"
                    )
                    time.sleep(wait)
                else:
                    logger.error(f"[LLM] 全部{_LLM_MAX_RETRY}次重试失败: {e}")
                    return None
    
    def generate_analysis(self, stock_data, context=""):
        """生成股票分析
        
        Args:
            stock_data: 股票数据字典
            context: 额外上下文信息
            
        Returns:
            分析文本
        """
        if not self.is_available():
            return "LLM服务未配置，无法生成分析"
        
        try:
            # 构建提示词
            prompt = self._build_analysis_prompt(stock_data, context)
            
            # 调用LLM
            analysis = self._call_llm(
                system_prompt=(
                    "你是服务A股机构投资者的投资研究员，精通各行业估值方法"
                    "（银行PB-戈登、公用事业DDM、周期品EV/EBITDA、科技PEG+研发强度）。"
                    "分析结论须基于估值安全边际，给出可操作判断。"
                ),
                user_prompt=prompt,
                temperature=0.5,
                max_tokens=800
            )
            
            logger.info(f"Generated analysis for {stock_data.get('ts_code', 'Unknown')}")
            
            return analysis
            
        except Exception as e:
            logger.error(f"Failed to generate analysis: {e}")
            return f"生成分析失败: {e}"
    
    def _build_analysis_prompt(self, stock_data, context=""):
        """构建分析提示词
        
        Args:
            stock_data: 股票数据字典
            context: 额外上下文信息
            
        Returns:
            提示词
        """
        ts_code = stock_data.get('ts_code', 'Unknown')
        name = stock_data.get('name', 'Unknown')
        close = stock_data.get('close', 0)
        stop_loss = stock_data.get('stop_loss_price', 0)
        score = stock_data.get('score', 0)
        
        # 获取因子数据
        mom_20 = stock_data.get('mom_20', 0)
        vol_20 = stock_data.get('vol_20', 0)
        rsi_14 = stock_data.get('rsi_14', 0)
        atr_14 = stock_data.get('atr_14', 0)
        
        # 获取基本面数据
        pe_ttm = stock_data.get('pe_ttm', 0)
        pb = stock_data.get('pb', 0)
        total_mv = stock_data.get('total_mv', 0)
        roe = stock_data.get('roe', 0)
        
        # 构建提示词
        # 估值数据（可选）
        val_verdict = stock_data.get('val_verdict', '')
        val_upside  = stock_data.get('val_upside_pct')
        val_method  = stock_data.get('val_method', '')
        val_detail  = stock_data.get('val_detail', '')
        val_block   = ""
        if val_verdict and val_upside is not None:
            val_block = (
                f"\n估值结论（{val_method}法）：{val_verdict}，安全边际 {val_upside:+.1f}%"
                + (f"  [{val_detail}]" if val_detail else "")
            )

        prompt = f"""分析以下股票，给出投资判断：

标的：{name}（{ts_code}）  现价 {close:.2f}元  止损 {stop_loss:.2f}元  综合评分 {score:.3f}

基本面：PE={pe_ttm:.1f}x  PB={pb:.2f}x  ROE={roe:.1f}%  市值{total_mv/1e8:.0f}亿{val_block}

技术面：RSI={rsi_14:.0f}  20日动量={mom_20:+.1%}  波动率={vol_20:.1%}  ATR={atr_14:.2f}

{context}

请输出（200字内）：
## 估值判断
[当前是贵了还是便宜了，给出一句话理由]

## 核心风险
[2条具体风险，不要泛泛而谈]

## 操作建议
[买入/持有/减仓/观望] — [基于估值安全边际的具体理由]
"""
        
        return prompt
    
    def generate_stock_analysis_with_news(self, stock_data: dict, news_items: list) -> str:
        """
        基于个股新闻/公告 + 基本面 + 技术面，生成持仓点评。

        与旧版 generate_analysis 的区别：
          - 数据更完整（pe/roe/gpr 来自 stock_daily 最新行情）
          - 传入实际新闻/公告内容，LLM 可对具体事件做定性判断
          - 输出格式：影响判断(利好/利空/中性) + 具体理由 + 操作建议（持有/关注/减仓）

        Args:
            stock_data: {name, ts_code, close, today_change, pe_ttm, roe, gpr,
                         total_mv, rsi_14, mom_20, avg_cost, pl_pct, sector}
            news_items: [{'title','content','source','time','type'}, ...]

        Returns:
            str: 150字以内的点评文本
        """
        if not self.is_available():
            return ""
        if not news_items:
            return ""

        name     = stock_data.get('name', '')
        ts_code  = stock_data.get('ts_code', '')
        close    = stock_data.get('close', 0)
        change   = stock_data.get('today_change', 0)
        pe       = stock_data.get('pe_ttm', 0)
        roe      = stock_data.get('roe', 0)
        gpr      = stock_data.get('gpr', 0)
        mv_yi    = (stock_data.get('total_mv', 0) or 0) / 1e8
        rsi      = stock_data.get('rsi_14', 0)
        mom20    = stock_data.get('mom_20', 0)
        cost     = stock_data.get('avg_cost', 0)
        pl_pct   = stock_data.get('pl_pct', 0)
        sector   = stock_data.get('sector', '')

        # 构建新闻文本（最多8条，公告优先展示）
        notices = [n for n in news_items if n.get('type') == 'notice']
        others  = [n for n in news_items if n.get('type') != 'notice']
        ordered = notices + others
        news_text = ""
        for i, n in enumerate(ordered[:8], 1):
            tag = "【公告】" if n.get('type') == 'notice' else "【新闻】"
            t   = n.get('time', '')
            news_text += f"{i}. {tag}({t} {n['source']}) {n['title']}"
            if n.get('content'):
                news_text += f"  — {n['content'][:100]}"
            news_text += "\n"

        # 基本面摘要
        fund_parts = []
        if pe and pe > 0:   fund_parts.append(f"PE={pe:.1f}倍")
        if roe and roe > 0: fund_parts.append(f"ROE={roe:.1f}%")
        if gpr and gpr > 0: fund_parts.append(f"毛利率={gpr:.1f}%")
        if mv_yi > 0:       fund_parts.append(f"市值{mv_yi:.0f}亿")
        fund_str = "、".join(fund_parts) if fund_parts else "基本面数据暂缺"

        # 估值数据（可选，由调用方传入）
        val_verdict = stock_data.get('val_verdict', '')
        val_upside  = stock_data.get('val_upside_pct')
        val_method  = stock_data.get('val_method', '')
        val_detail  = stock_data.get('val_detail', '')
        itype       = stock_data.get('itype', '')

        if val_verdict and val_upside is not None:
            val_block = (
                f"\n**估值判断**（{val_method or itype}法）：{val_verdict}，"
                f"安全边际 {val_upside:+.1f}%"
                + (f"  {val_detail}" if val_detail else "")
            )
        else:
            val_block = ""

        system_prompt = (
            "你是服务A股机构投资者的投资研究员，精通各行业估值方法"
            "（银行PB-戈登、公用事业DDM、周期品EV/EBITDA、科技PEG+研发强度）。"
            "分析结论须基于估值安全边际，给出可操作判断，不废话，不预测大盘方向。"
        )

        user_prompt = f"""我持有 {name}（{ts_code}），请根据以下信息给出点评：

**持仓情况**：成本 {cost:.2f}元 / 现价 {close:.2f}元 / 今日{'+' if change>=0 else ''}{change:.1f}% / 浮盈{'+' if pl_pct>=0 else ''}{pl_pct:.1f}%
**板块**：{sector}
**基本面**：{fund_str}
**技术**：RSI={rsi:.0f}、20日动量={mom20:+.1f}%{val_block}

**最新新闻/公告（近24小时）**：
{news_text if news_text else "暂无相关新闻"}

请按以下格式回答（总计不超过150字）：
估值：[低估/合理/高估] — [一句话说明当前价格贵不贵，依据是估值结论或PE/PB水平]
影响：[利好/利空/中性] — [最重要的新闻对股价的影响]
建议：[持有/关注止损/考虑减仓/可适当加仓] — [理由，结合估值安全边际]"""

        try:
            result = self._call_llm(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=300,
            )
            return result.strip() if result else ""
        except Exception as e:
            logger.error(f"[LLM] 个股新闻分析失败 {ts_code}: {e}")
            return ""

    def generate_portfolio_commentary(self, portfolio_summary, market_sentiment=0):
        """生成组合点评
        
        Args:
            portfolio_summary: 组合摘要字典
            market_sentiment: 市场情绪指标
            
        Returns:
            组合点评文本
        """
        if not self.is_available():
            return "LLM服务未配置，无法生成组合点评"
        
        try:
            # 构建提示词
            prompt = self._build_portfolio_prompt(portfolio_summary, market_sentiment)
            
            # 调用LLM
            commentary = self._call_llm(
                system_prompt="你是一个专业的量化投资分析师，擅长分析投资组合并提供市场点评。",
                user_prompt=prompt,
                temperature=0.7,
                max_tokens=800
            )
            
            logger.info("Generated portfolio commentary")
            
            return commentary
            
        except Exception as e:
            logger.error(f"Failed to generate portfolio commentary: {e}")
            return f"生成组合点评失败: {e}"
    
    def _build_portfolio_prompt(self, portfolio_summary, market_sentiment):
        """构建组合点评提示词
        
        Args:
            portfolio_summary: 组合摘要字典
            market_sentiment: 市场情绪指标
            
        Returns:
            提示词
        """
        open_count = portfolio_summary.get('open_positions_count', 0)
        closed_count = portfolio_summary.get('closed_positions_count', 0)
        total_market_value = portfolio_summary.get('total_market_value', 0)
        total_cost = portfolio_summary.get('total_cost', 0)
        current_pnl = portfolio_summary.get('current_pnl', 0)
        current_pnl_pct = portfolio_summary.get('current_pnl_pct', 0)
        total_closed_pnl = portfolio_summary.get('total_closed_pnl', 0)
        total_pnl = portfolio_summary.get('total_pnl', 0)
        
        # 构建提示词
        prompt = f"""
请分析以下投资组合，并提供市场点评：

组合概况：
- 当前持仓数: {open_count}
- 已平仓数: {closed_count}
- 持仓市值: {total_market_value:.2f}元
- 持仓成本: {total_cost:.2f}元
- 当前盈亏: {current_pnl:.2f}元 ({current_pnl_pct:.2f}%)
- 已平仓盈亏: {total_closed_pnl:.2f}元
- 总盈亏: {total_pnl:.2f}元

市场环境：
- 市场情绪: {market_sentiment:.4f} ({'上涨' if market_sentiment > 0 else '下跌' if market_sentiment < 0 else '中性'})

请从以下角度进行分析：
1. 组合表现：当前盈亏情况、风险水平
2. 市场判断：根据市场情绪判断当前市场环境
3. 操作建议：是否需要调整仓位、止盈止损建议
4. 风险提示：潜在风险点

请用简洁、专业的语言回答，控制在400字以内。
"""
        
        return prompt
    
    def generate_market_commentary(self, market_data):
        """生成市场点评
        
        Args:
            market_data: 市场数据字典
            
        Returns:
            市场点评文本
        """
        if not self.is_available():
            return "LLM服务未配置，无法生成市场点评"
        
        try:
            # 构建提示词
            prompt = self._build_market_prompt(market_data)
            
            # 调用LLM
            commentary = self._call_llm(
                system_prompt="你是一个专业的量化投资分析师，擅长分析市场走势并提供投资建议。",
                user_prompt=prompt,
                temperature=0.7,
                max_tokens=800
            )
            
            logger.info("Generated market commentary")
            
            return commentary
            
        except Exception as e:
            logger.error(f"Failed to generate market commentary: {e}")
            return f"生成市场点评失败: {e}"
    
    def generate_etf_advice(self, industry_timing_summary: str, etf_by_industry: dict, holding_hint: str = "1周～3个月"):
        """基于行业择机与主题ETF列表，生成 Grok 买卖/选择建议（可结合模型知识或联网信息）。

        Args:
            industry_timing_summary: 行业择机结论摘要（如：当前周期、新兴/成熟行业及相对强度）
            etf_by_industry: 按行业推荐的主题ETF，格式 {"行业名": [{"code","name","涨跌幅",...}, ...]}
            holding_hint: 建议持有周期说明

        Returns:
            2～5 条简洁的 ETF 买卖/选择建议文本，失败时返回空字符串或错误说明
        """
        if not self.is_available():
            return ""

        try:
            lines = []
            for ind, lst in (etf_by_industry or {}).items():
                if not lst:
                    continue
                for x in lst[:2]:
                    name = x.get("name", "")
                    code = x.get("code", "")
                    pct = x.get("涨跌幅")
                    pct_str = f" 涨跌幅{pct:+.1f}%" if pct is not None else ""
                    lines.append(f"- {ind}: {name}({code}){pct_str}")
            etf_text = "\n".join(lines) if lines else "（暂无推荐列表）"

            prompt = f"""你是一位 A 股 ETF 与行业轮动顾问。请根据下面「行业择机结论」和「按行业推荐的主题 ETF 列表」，给出 2～4 条简洁的买卖/选择建议。

【行业择机结论】
{industry_timing_summary}

【按行业推荐的主题 ETF】
{etf_text}

【建议持有周期】
{holding_hint}

要求：
1. 明确建议可考虑买入/关注或暂时观望的行业或具体 ETF（写名称或代码）。
2. 若有近期政策、产业趋势或市场热点依据，可简要提及（若你掌握最新信息可结合）。
3. 每条 1～2 句话，总字数控制在 200 字以内。
4. 结尾加一句风险提示，如：仅供参考，不构成投资建议，注意波动风险。"""

            advice = self._call_llm(
                system_prompt="你是 A 股 ETF 与行业轮动顾问，回答简洁、专业，给出可操作的买卖/选择建议。",
                user_prompt=prompt,
                temperature=0.5,
                max_tokens=600
            )
            advice = (advice or "").strip()
            logger.info("Generated ETF advice from LLM")
            return advice
        except Exception as e:
            logger.error(f"Failed to generate ETF advice: {e}")
            return f"（Grok 建议暂时不可用: {str(e)[:80]}）"

    def _build_market_prompt(self, market_data):
        """构建市场点评提示词
        
        Args:
            market_data: 市场数据字典
            
        Returns:
            提示词
        """
        sentiment = market_data.get('sentiment', 0)
        avg_return = market_data.get('avg_return', 0)
        volatility = market_data.get('volatility', 0)
        
        # 构建提示词
        prompt = f"""
请分析以下市场数据，并提供市场点评：

市场概况：
- 市场情绪: {sentiment:.4f} ({'上涨' if sentiment > 0 else '下跌' if sentiment < 0 else '中性'})
- 平均涨跌幅: {avg_return:.2f}%
- 市场波动率: {volatility:.4f}

请从以下角度进行分析：
1. 市场趋势：当前市场是上涨、下跌还是震荡
2. 市场情绪：投资者情绪如何
3. 投资建议：当前市场环境下适合什么策略
4. 风险提示：需要注意的风险点

请用简洁、专业的语言回答，控制在300字以内。
"""
        
        return prompt

if __name__ == '__main__':
    # 测试代码
    client = LLMClient()
    
    if client.is_available():
        # 测试股票分析
        test_stock = {
            'ts_code': '600519.SH',
            'name': '贵州茅台',
            'close': 1800.00,
            'stop_loss_price': 1764.00,
            'score': 2.5,
            'mom_20': 0.15,
            'vol_20': -0.3,
            'rsi_14': 65.0,
            'atr_14': 30.0,
            'pe_ttm': 25.0,
            'pb': 8.5,
            'total_mv': 2000000000000,
            'roe': 30.0
        }
        
        analysis = client.generate_analysis(test_stock)
        print("股票分析:")
        print(analysis)
    else:
        print("LLM服务未配置，请先配置API Key")


# 模块级单例，避免每次 LLMClient() 都重新初始化连接
_llm_instance: "LLMClient | None" = None

def get_llm_client() -> "LLMClient":
    """获取全局唯一 LLMClient 实例（进程内复用）。"""
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = LLMClient()
    return _llm_instance
