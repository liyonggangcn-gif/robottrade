"""
网络工具：代理自适应

若当前环境设置了代理但代理连不上，自动取消代理并重试，
使后续请求（akshare、requests 等）可直连。

同时提供 patch_requests_no_proxy()，强制 requests 不使用代理，
解决 Tushare 等库在 127.0.0.1:7890 超时的问题。
"""

import os

# 常见代理环境变量（小写+大写）
PROXY_ENV_KEYS = [
    "http_proxy", "https_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
]


def patch_requests_no_proxy():
    """
    强制 requests.post/get 不使用代理（Monkey-patch）。
    在 import tushare 或任何使用 requests 的库之前调用。
    """
    try:
        import requests as _req
    except ImportError:
        return
    if getattr(_req, "_tushare_no_proxy_patched", False):
        return
    _orig_post = _req.post
    _orig_get = _req.get

    def _no_proxy_request(method_func, *args, **kwargs):
        if "proxies" not in kwargs:
            kwargs["proxies"] = {"http": "", "https": ""}
        return method_func(*args, **kwargs)

    _req.post = lambda *a, **kw: _no_proxy_request(_orig_post, *a, **kw)
    _req.get = lambda *a, **kw: _no_proxy_request(_orig_get, *a, **kw)
    _req._tushare_no_proxy_patched = True


def clear_proxy_env():
    """清除所有代理相关环境变量，使后续请求直连"""
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)


def is_proxy_related_error(exc):
    """判断异常是否与代理/连接相关"""
    if exc is None:
        return False
    msg = str(exc).lower()
    err_type = type(exc).__name__
    if "proxy" in msg or "ProxyError" in err_type:
        return True
    if "connection" in msg or "connect" in msg:
        return True
    if "remotedisconnected" in msg or "remote end closed" in msg:
        return True
    if "timeout" in msg or "timed out" in msg:
        return True
    return False


def ensure_proxy_adaptive(test_url="https://www.baidu.com", timeout=8):
    """
    代理自适应：先按当前环境请求；若因代理/连接失败则清除代理后重试一次。

    - 若首次成功，不修改环境。
    - 若首次失败且为代理/连接类错误，清除代理并重试；重试成功则后续请求将直连。

    Returns:
        bool: 是否最终可连通（True 表示后续请求可正常发）
    """
    try:
        import requests
    except ImportError:
        return True  # 无 requests 时不做检测，交给调用方

    try:
        resp = requests.get(test_url, timeout=timeout)
        resp.raise_for_status()
        return True
    except Exception as e:
        if not is_proxy_related_error(e):
            return False
        clear_proxy_env()
        try:
            resp = requests.get(test_url, timeout=timeout)
            resp.raise_for_status()
            print("[network] 代理不可用，已自动取消代理并改用直连")
            return True
        except Exception as e2:
            print(f"[network] 直连也失败: {e2}")
            return False
