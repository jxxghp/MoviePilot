"""
搜索、换票和种子 HTTP 请求共用的站点流控策略。
"""

import asyncio
import math
import time
from collections.abc import Mapping
from typing import Optional

from app.application.site.query import get_configured_site_query_service
from app.domain import site as site_rules
from app.domain import torrent as torrent_rules
from app.runtime.rate import RequestRateLimiter
from app.schemas.exception import RateLimitExceededException

_site_requests = RequestRateLimiter()


def site_request_config(url: str, site_id: Optional[int] = None) -> dict[str, object]:
    """
    以站点 ID 读取流控配置，使网页、API 和临时下载主机共享同一额度。

    :param url: 无站点 ID 时用于匹配配置的请求地址
    :param site_id: 资源所属站点 ID，优先于下载主机匹配
    :return: 不含认证凭据的站点流控快照；未保存的地址只使用域名隔离冷却
    """
    query = get_configured_site_query_service()
    domain = site_rules.extract_domain(url)
    site = query.get_sync(site_id) if site_id is not None else query.get_by_domain_sync(domain)
    if site is None:
        return {"id": site_id, "domain": domain}
    return site.model_dump(include={"id", "domain", "limit_seconds", "limit_interval", "limit_count"})


def _site_key(site: Mapping[str, object]) -> str:
    """
    返回站点的稳定流控键，已保存站点始终按 ID 隔离。
    """
    site_id = site.get("id")
    return f"id:{site_id}" if site_id is not None else str(site.get("domain") or site.get("url") or site.get("name"))


def _positive_number(value: object) -> float:
    """
    忽略无效或非有限的流控配置，避免生成无法结束的等待。
    """
    if not isinstance(value, (str, int, float)):
        return 0.0
    try:
        number = float(value or 0)
        return number if math.isfinite(number) and number > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _request_delay(site: Mapping[str, object]) -> float:
    """
    原子申请一次站点请求，远端冷却期间直接返回原始原因。
    """
    delay, reason = _site_requests.acquire(
        _site_key(site), interval=_positive_number(site.get("limit_seconds")),
        window=_positive_number(site.get("limit_interval")), count=int(_positive_number(site.get("limit_count"))),
    )
    if reason:
        raise RateLimitExceededException(reason)
    return delay


def wait_for_site_request(site: Mapping[str, object]) -> None:
    """
    在同步请求前等待配置的间隔和次数窗口，唤醒后重新检查许可。

    :param site: 本次请求所属站点的流控配置
    :raises RateLimitExceededException: 站点仍处于远端明确要求的冷却期
    """
    while delay := _request_delay(site):
        time.sleep(delay)


async def async_wait_for_site_request(site: Mapping[str, object]) -> None:
    """
    异步等待同一站点额度，取消等待不会提前消耗请求次数。

    :param site: 本次请求所属站点的流控配置
    :raises RateLimitExceededException: 站点仍处于远端明确要求的冷却期
    """
    while delay := _request_delay(site):
        await asyncio.sleep(delay)


def defer_site_request(site: Mapping[str, object], reason: str) -> None:
    """
    将远端限流应用于同站点后续搜索、换票和取种子请求。

    :param site: 返回限流响应的站点配置
    :param reason: 已识别的原始限流提示
    """
    seconds = torrent_rules.rate_limit_cooldown(reason)
    if seconds is not None:
        _site_requests.defer(_site_key(site), seconds, reason)


def site_request_cooldown(site: Mapping[str, object]) -> float:
    """
    返回该站点的远端冷却余量，供订阅调度安排下次访问。

    :param site: 订阅正在访问的站点配置
    :return: 冷却剩余秒数，没有远端冷却时为 0
    """
    return _site_requests.cooldown(_site_key(site))
