"""消息渠道回环进入宿主消息 API 的统一适配边界。"""

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

BackgroundSubmitter = Callable[..., object]


def build_message_ingress_url(source: str | None) -> str:
    """按当前运行配置构造安全编码的本地消息入口 URL。"""
    query = {"token": get_runtime_setting('API_TOKEN')}
    if source:
        query["source"] = source
    return (
        f"http://127.0.0.1:{get_runtime_setting('PORT')}/api/v1/message?"
        f"{urlencode(query)}"
    )


def forward_message_to_host(
    payload: Mapping[str, Any],
    source: str | None,
    *,
    timeout: float = 15,
) -> bool:
    """同步转发渠道 payload，统一判断本地入口是否确认接收。"""
    response = None
    try:
        response = RequestUtils(timeout=timeout).post_res(
            build_message_ingress_url(source),
            json=dict(payload),
        )
        if response is None:
            logger.error(f"转发渠道消息到本地入口失败：source={source or '-'} - 无响应")
            return False
        if response.status_code >= 400:
            logger.error(
                "转发渠道消息到本地入口失败："
                f"source={source or '-'} - HTTP {response.status_code}"
            )
            return False
        return True
    except Exception as error:
        logger.error(f"转发渠道消息到本地入口失败：source={source or '-'} - {error}")
        return False
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception as error:
                logger.debug(
                    f"释放本地消息入口响应失败：source={source or '-'} - {error}"
                )


async def async_forward_message_to_host(
    payload: Mapping[str, Any],
    source: str | None,
    *,
    timeout: float = 15,
) -> bool:
    """异步转发渠道 payload，供自有事件循环的消息 SDK 复用同一确认语义。"""
    response = None
    try:
        response = await AsyncRequestUtils(timeout=timeout).post_res(
            build_message_ingress_url(source),
            json=dict(payload),
        )
        if response is None:
            logger.error(f"转发渠道消息到本地入口失败：source={source or '-'} - 无响应")
            return False
        if response.status_code >= 400:
            logger.error(
                "转发渠道消息到本地入口失败："
                f"source={source or '-'} - HTTP {response.status_code}"
            )
            return False
        return True
    except Exception as error:
        logger.error(f"转发渠道消息到本地入口失败：source={source or '-'} - {error}")
        return False
    finally:
        if response is not None:
            try:
                await response.aclose()
            except Exception as error:
                logger.debug(
                    f"释放本地消息入口响应失败：source={source or '-'} - {error}"
                )


def submit_message_to_host(
    payload: Mapping[str, Any],
    source: str | None,
    *,
    submit: BackgroundSubmitter,
    timeout: float = 15,
) -> bool:
    """把同步回环转发提交给调用方注入的受管后台执行器。"""
    try:
        submit(
            forward_message_to_host,
            dict(payload),
            source,
            timeout=timeout,
        )
    except Exception as error:
        logger.error(f"提交渠道消息转发任务失败：source={source or '-'} - {error}")
        return False
    return True
