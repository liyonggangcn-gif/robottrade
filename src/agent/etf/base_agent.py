"""
BaseAgent — 所有 ETF Agent 的抽象基类
每个 Agent 只做一件事，run() 返回标准化 dict
"""
from __future__ import annotations
import logging
import time
import traceback
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict


class BaseAgent(ABC):
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self._last_run_at: str = ""
        self._last_elapsed: float = 0.0

    @abstractmethod
    def run(self, **kwargs) -> Dict[str, Any]:
        """执行 Agent 主逻辑，返回标准 dict"""

    def safe_run(self, **kwargs) -> Dict[str, Any]:
        """带异常捕获的执行入口，保证不崩溃"""
        t0 = time.time()
        try:
            result = self.run(**kwargs)
            self._last_elapsed = time.time() - t0
            self._last_run_at = datetime.now().isoformat(timespec="seconds")
            result.setdefault("ok", True)
            result.setdefault("agent", self.__class__.__name__)
            return result
        except Exception as e:
            self._last_elapsed = time.time() - t0
            self.logger.error(f"{self.__class__.__name__} failed: {e}")
            return {
                "ok": False,
                "agent": self.__class__.__name__,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }
