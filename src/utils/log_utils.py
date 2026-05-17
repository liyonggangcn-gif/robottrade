"""
统一日志工具：所有 scripts/ 脚本通过此模块初始化日志。

用法（在脚本开头）:
    from src.utils.log_utils import init_logger
    logger = init_logger("daily_alpha_run")

效果:
    - 同时输出到 控制台（stdout） 和 logs/<script_name>_YYYYMMDD.log
    - 日志文件按天轮转，保留 30 天
    - print() 也会被重定向到日志文件（通过 TeeWriter）
"""

import os
import sys
import logging
from datetime import datetime


def _get_logs_dir():
    """获取 logs 目录（项目根/logs），不存在则创建"""
    # 项目根目录：src/utils/log_utils.py -> ../../
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    logs_dir = os.path.join(project_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    return logs_dir


class TeeWriter:
    """同时写到原始 stdout/stderr 和日志文件"""

    def __init__(self, original, log_file):
        self.original = original
        self.log_file = log_file

    def write(self, text):
        if text:
            try:
                # 确保原始输出能处理UTF-8字符（Windows控制台编码问题）
                try:
                    self.original.write(text)
                except UnicodeEncodeError:
                    # 如果原始输出编码失败，尝试替换无法编码的字符
                    safe_text = text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
                    self.original.write(safe_text)
                
                # 日志文件始终使用UTF-8
                self.log_file.write(text)
                self.log_file.flush()
            except Exception as e:
                # 静默处理错误，避免影响主流程
                pass

    def flush(self):
        self.original.flush()
        try:
            self.log_file.flush()
        except Exception:
            pass

    @property
    def encoding(self):
        return getattr(self.original, 'encoding', 'utf-8')

    @property
    def buffer(self):
        return getattr(self.original, 'buffer', None)


def init_logger(script_name: str, level=logging.INFO) -> logging.Logger:
    """
    初始化日志：同时输出到控制台和 logs/<script_name>_YYYYMMDD.log

    Args:
        script_name: 脚本名称（不含 .py），作为日志文件名前缀
        level: 日志级别

    Returns:
        logging.Logger 实例
    """
    logs_dir = _get_logs_dir()
    today = datetime.now().strftime("%Y%m%d")
    log_path = os.path.join(logs_dir, f"{script_name}_{today}.log")

    # 创建 logger
    logger = logging.getLogger(script_name)
    logger.setLevel(level)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 日志格式
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 文件 handler（追加模式，UTF-8）
    fh = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 检测是否在交互式终端（cron 下 stdout 已重定向到日志文件，不是 TTY）
    is_tty = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()

    if is_tty:
        # 交互式：加控制台 handler + TeeWriter 把 print 也写到文件
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(level)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

        try:
            log_file = open(log_path, "a", encoding="utf-8")
            raw_stdout = sys.__stdout__ if sys.__stdout__ else sys.stdout
            raw_stderr = sys.__stderr__ if sys.__stderr__ else sys.stderr
            sys.stdout = TeeWriter(raw_stdout, log_file)
            sys.stderr = TeeWriter(raw_stderr, log_file)
        except Exception:
            pass
    else:
        # 非交互式（cron）：stdout/stderr 已由 shell 重定向到日志文件
        # 只保留 FileHandler，避免双写导致每行日志重复
        pass

    logger.info(f"===== {script_name} started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====")
    logger.info(f"Log file: {log_path}")

    return logger


def cleanup_old_logs(keep_days: int = 30):
    """清理超过 keep_days 天的旧日志"""
    import glob
    import time

    logs_dir = _get_logs_dir()
    cutoff = time.time() - keep_days * 86400

    for f in glob.glob(os.path.join(logs_dir, "*.log")):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
        except Exception:
            pass
