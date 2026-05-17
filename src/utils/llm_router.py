#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM路由器
reasoner → DeepSeek-R1（复杂推理）
analyzer → DeepSeek-V3（快速分析）
"""
import time
from loguru import logger

from src.utils.config_loader import Config


class LLMRouter:
    """
    双模型路由器
    - reason()   → 推理模型（R1），用于最终决策综合
    - analyze()  → 分析模型（V3），用于单支股票分析
    - fast_query() → 快速问答
    """

    def __init__(self):
        self._reasoner_client = None
        self._reasoner_model = ''
        self._analyzer_client = None
        self._analyzer_model = ''

        self._init_clients()

    # ------------------------------------------------------------------ #
    #  初始化
    # ------------------------------------------------------------------ #
    def _init_clients(self):
        """读取配置并初始化 OpenAI 客户端"""
        try:
            from openai import OpenAI
        except ImportError:
            logger.warning("[Router] openai 包未安装，LLMRouter 不可用")
            return

        router_cfg = Config.get('llm_router') or {}
        fallback_cfg = Config.get('llm') or {}

        # --- 推理模型（reasoner / R1）---
        reasoner_cfg = router_cfg.get('reasoner') or {}
        if not reasoner_cfg:
            reasoner_cfg = fallback_cfg  # 降级到通用 llm 配置

        r_api_key = reasoner_cfg.get('api_key', '')
        r_base_url = reasoner_cfg.get('base_url', '')
        r_model = reasoner_cfg.get('model', 'deepseek-reasoner')

        if r_api_key:
            try:
                kwargs = {'api_key': r_api_key}
                if r_base_url:
                    kwargs['base_url'] = r_base_url
                self._reasoner_client = OpenAI(**kwargs)
                self._reasoner_model = r_model
                logger.info(f"[Router] 推理模型初始化: {r_model}  base_url={r_base_url or '(default)'}")
            except Exception as e:
                logger.error(f"[Router] 推理模型初始化失败: {e}")

        # --- 分析模型（analyzer / V3）---
        analyzer_cfg = router_cfg.get('analyzer') or {}
        if not analyzer_cfg:
            analyzer_cfg = fallback_cfg  # 降级到通用 llm 配置

        a_api_key = analyzer_cfg.get('api_key', r_api_key)   # 共享 key 也可以
        a_base_url = analyzer_cfg.get('base_url', r_base_url)
        a_model = analyzer_cfg.get('model', 'deepseek-chat')

        if a_api_key:
            try:
                kwargs = {'api_key': a_api_key}
                if a_base_url:
                    kwargs['base_url'] = a_base_url
                self._analyzer_client = OpenAI(**kwargs)
                self._analyzer_model = a_model
                logger.info(f"[Router] 分析模型初始化: {a_model}  base_url={a_base_url or '(default)'}")
            except Exception as e:
                logger.error(f"[Router] 分析模型初始化失败: {e}")

    # ------------------------------------------------------------------ #
    #  核心调用
    # ------------------------------------------------------------------ #
    def _call(self, client, model: str, system: str, user: str,
              temperature: float, max_tokens: int, timeout: float = 120.0) -> str:
        """统一调用逻辑，带超时 + 一次重试（指数退避）"""
        if client is None:
            logger.warning("[Router] 客户端未初始化，跳过调用")
            return ''

        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': user})

        for attempt in range(1, 3):  # 最多重试1次
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                content = resp.choices[0].message.content or ''
                return content.strip()
            except Exception as e:
                logger.warning(f"[Router] {model} 第{attempt}次调用失败: {e}")
                if attempt < 2:
                    time.sleep(5)   # 重试前等 5s，避免立即打压 API
                else:
                    logger.error(f"[Router] {model} 两次均失败，返回空字符串")
                    return ''
        return ''

    # ------------------------------------------------------------------ #
    #  公开接口
    # ------------------------------------------------------------------ #
    def reason(self, prompt: str, system: str = None, max_tokens: int = 4000) -> str:
        """
        调用推理模型（DeepSeek-R1）进行复杂综合判断
        适用场景：最终买卖决策、多因子综合推理
        """
        if system is None:
            system = (
                "你是专业的A股投资分析师，擅长复杂推理和综合判断。"
                "请给出清晰的推理过程和结论。"
            )
        logger.debug(f"[Router/R1] 调用推理模型  model={self._reasoner_model}  "
                     f"prompt_len={len(prompt)}")
        result = self._call(
            client=self._reasoner_client,
            model=self._reasoner_model,
            system=system,
            user=prompt,
            temperature=0.1,        # 推理需要一致性，低温度
            max_tokens=max_tokens,
            timeout=180.0,          # R1 推理链较长，允许 3 分钟
        )
        logger.debug(f"[Router/R1] 返回 {len(result)} 字符")
        return result

    def analyze(self, prompt: str, system: str = None, max_tokens: int = 2000, timeout: float = 60.0) -> str:
        """
        调用分析模型（DeepSeek-V3）进行财务/行业分析
        适用场景：单支股票分析、财报解读、行业对比
        """
        if system is None:
            system = (
                "你是A股金融数据分析师，精通财务报表解读、行业比较和估值分析。"
                "回答简洁专业。"
            )
        logger.debug(f"[Router/V3] 调用分析模型  model={self._analyzer_model}  "
                     f"prompt_len={len(prompt)}")
        result = self._call(
            client=self._analyzer_client,
            model=self._analyzer_model,
            system=system,
            user=prompt,
            temperature=0.3,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        logger.debug(f"[Router/V3] 返回 {len(result)} 字符")
        return result

    def fast_query(self, prompt: str, max_tokens: int = 500) -> str:
        """
        快速问答，用分析模型但更高温度（适合简短陈述性问答）
        """
        system = (
            "你是A股金融数据分析师，精通财务报表解读、行业比较和估值分析。"
            "回答简洁专业。"
        )
        logger.debug(f"[Router/V3-fast] prompt_len={len(prompt)}")
        result = self._call(
            client=self._analyzer_client,
            model=self._analyzer_model,
            system=system,
            user=prompt,
            temperature=0.5,
            max_tokens=max_tokens
        )
        return result

    def is_available(self) -> bool:
        """检查至少有一个模型客户端已初始化"""
        return self._reasoner_client is not None or self._analyzer_client is not None
