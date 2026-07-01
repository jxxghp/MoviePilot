from typing import Any

from pywebpush import WebPushException

# WNS 默认 TTL（秒）；ttl>0 时需配 X-WNS-Cache-Policy: cache
_WNS_DEFAULT_TTL = 86400


def is_webpush_subscription_gone(error: WebPushException) -> bool:
    """
    判断 WebPush 订阅是否已经在浏览器或推送服务侧失效。
    """
    response: Any = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None) or getattr(response, "status", None)
    return status_code in {404, 410}


def is_wns_endpoint(endpoint: str | None) -> bool:
    """
    判断是否为 Microsoft WNS（Edge/Windows）推送端点。
    """
    return bool(endpoint and "notify.windows.com" in endpoint)


def webpush_options_for_endpoint(endpoint: str | None) -> dict[str, Any]:
    """
    按推送服务返回 pywebpush 额外参数。

    WNS 要求 TTL 与 X-WNS-Cache-Policy 一致，否则返回 400。
    见 https://github.com/web-push-libs/pywebpush/issues/162
    """
    if not is_wns_endpoint(endpoint):
        return {}
    return {
        "ttl": _WNS_DEFAULT_TTL,
        "headers": {"X-WNS-Cache-Policy": "cache"},
    }
