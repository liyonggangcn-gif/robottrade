#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DuckDB 查询工具 — 用于替代 MySQL 上的重分析查询
DuckDB 文件位于 192.168.3.51:/home/li/robottrade/data/quant_backtest.duckdb
通过 NFS/SCP 同步到 192.168.3.22:/home/li/robottrade/data/quant_backtest.duckdb
"""
import os
import threading
import duckdb
import pandas as pd
import numpy as np

# DuckDB 文件路径（本地）
_DUCKDB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'data', 'quant_backtest.duckdb'
)

# 线程级 DuckDB 连接缓存
_duckdb_thread_local = threading.local()


def _get_duckdb_path():
    """获取 DuckDB 文件路径"""
    # 优先使用环境变量
    env_path = os.environ.get('DUCKDB_PATH')
    if env_path and os.path.exists(env_path):
        return env_path
    # 默认路径
    if os.path.exists(_DUCKDB_PATH):
        return _DUCKDB_PATH
    # 尝试常见路径
    for p in [
        '/home/li/robottrade/data/quant_backtest.duckdb',
        '/tmp/quant_backtest.duckdb',
    ]:
        if os.path.exists(p):
            return p
    return None


def _get_conn():
    """获取线程级 DuckDB 连接"""
    conn = getattr(_duckdb_thread_local, 'conn', None)
    if conn is None:
        path = _get_duckdb_path()
        if path is None:
            raise FileNotFoundError(
                "DuckDB file not found. Set DUCKDB_PATH env var or ensure "
                "/home/li/robottrade/data/quant_backtest.duckdb exists."
            )
        conn = duckdb.connect(path, read_only=True)
        _duckdb_thread_local.conn = conn
    return conn


def query_df(sql: str, params=None) -> pd.DataFrame:
    """执行 SQL 查询，返回 DataFrame"""
    conn = _get_conn()
    if params:
        return conn.execute(sql, params).fetchdf()
    return conn.execute(sql).fetchdf()


def query_one(sql: str, params=None):
    """执行 SQL 查询，返回单行"""
    conn = _get_conn()
    if params:
        return conn.execute(sql, params).fetchone()
    return conn.execute(sql).fetchone()


def query_value(sql: str, params=None):
    """执行 SQL 查询，返回单个值"""
    row = query_one(sql, params)
    if row:
        return row[0]
    return None


def is_available() -> bool:
    """检查 DuckDB 是否可用"""
    try:
        path = _get_duckdb_path()
        if path is None:
            return False
        conn = duckdb.connect(path, read_only=True)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return True
    except Exception:
        return False


def get_path() -> str:
    """获取 DuckDB 文件路径"""
    return _get_duckdb_path() or "not found"


def close_all():
    """关闭所有线程的 DuckDB 连接"""
    conn = getattr(_duckdb_thread_local, 'conn', None)
    if conn:
        conn.close()
        _duckdb_thread_local.conn = None
