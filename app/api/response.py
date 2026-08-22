import inspect
from functools import wraps
from typing import Any, Callable

from fastapi import APIRouter
from fastapi.datastructures import DefaultPlaceholder
from fastapi.dependencies.utils import get_typed_signature
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, get_typed_return_annotation
from starlette.responses import Response as StarletteResponse

from app.schemas.common import JsonData
from app.schemas.response import Response, ValidationIssue


ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    400: {"model": Response[None], "description": "请求错误"},
    401: {"model": Response[None], "description": "未认证"},
    403: {"model": Response[None], "description": "无权限"},
    404: {"model": Response[None], "description": "资源不存在"},
    409: {"model": Response[None], "description": "资源冲突"},
    422: {
        "model": Response[list[ValidationIssue]],
        "description": "请求参数校验失败",
    },
    500: {"model": Response[None], "description": "服务器内部错误"},
}
RAW_RESPONSE_OPENAPI_KEY = "x-moviepilot-raw-response"


class ResponseAPIRoute(APIRoute):
    """为普通 JSON 接口统一声明并生成 ``Response[T]`` 响应。"""

    def __init__(
        self,
        path: str,
        endpoint: Callable[..., Any],
        **kwargs: Any,
    ) -> None:
        """根据原始响应模型决定是否包装接口定义及运行时返回值。"""
        response_model = kwargs.get("response_model")
        response_class = kwargs.get("response_class", JSONResponse)
        status_code = kwargs.get("status_code")
        openapi_extra = kwargs.get("openapi_extra") or {}
        force_raw = bool(openapi_extra.get(RAW_RESPONSE_OPENAPI_KEY))

        if isinstance(response_model, DefaultPlaceholder):
            inferred_model = get_typed_return_annotation(endpoint)
            if self._is_native_response_model(inferred_model):
                response_model = None
            else:
                response_model = inferred_model or JsonData
        if response_model is Any:
            response_model = JsonData
        if response_model is Response:
            response_model = Response[JsonData]
            kwargs["response_model"] = response_model

        should_wrap = self._should_wrap_response(
            response_model=response_model,
            response_class=response_class,
            status_code=status_code,
            force_raw=force_raw,
        )
        if should_wrap:
            kwargs["response_model"] = Response[response_model]
            endpoint = self._wrap_endpoint(endpoint)

        kwargs["responses"] = self._merge_error_responses(
            kwargs.get("responses")
        )

        super().__init__(path=path, endpoint=endpoint, **kwargs)

    @staticmethod
    def _should_wrap_response(
        response_model: Any,
        response_class: Any,
        status_code: int | None,
        force_raw: bool,
    ) -> bool:
        """判断当前路由是否属于需要统一封装的普通 JSON 接口。"""
        if force_raw or response_model is None or status_code in {204, 304}:
            return False

        resolved_response_class = (
            response_class.value
            if isinstance(response_class, DefaultPlaceholder)
            else response_class
        )
        try:
            if not issubclass(resolved_response_class, JSONResponse):
                return False
        except TypeError:
            return False

        return not ResponseAPIRoute._is_response_model(response_model)

    @staticmethod
    def _is_response_model(response_model: Any) -> bool:
        """判断声明模型是否已经是统一响应模型。"""
        try:
            return issubclass(response_model, Response)
        except TypeError:
            return False

    @staticmethod
    def _is_native_response_model(response_model: Any) -> bool:
        """判断返回注解是否声明为 Starlette 原生响应。"""
        try:
            return issubclass(response_model, StarletteResponse)
        except TypeError:
            return False

    @staticmethod
    def _merge_error_responses(
        responses: dict[int | str, dict[str, Any]] | None,
    ) -> dict[int | str, dict[str, Any]]:
        """补齐统一错误模型，并保留端点已经显式声明的响应。"""
        merged_responses: dict[int | str, dict[str, Any]] = dict(ERROR_RESPONSES)
        merged_responses.update(responses or {})
        return merged_responses

    @staticmethod
    def _resolved_signature(endpoint: Callable[..., Any]) -> inspect.Signature:
        """按端点自身模块名字空间解析出的参数签名。

        FastAPI 用 ``call.__globals__`` 求值延后注解，而包装函数的 globals 属于本模块，
        端点模块里 ``from __future__ import annotations`` 写下的注解会在这里求值失败，
        被静默保留成未解析的 ``ForwardRef``——错误直到生成 OpenAPI 才抛出。
        先按端点自身名字空间解析好，再挂到包装函数上，取值来源就不再依赖包装位置。

        :param endpoint: 原始端点函数
        :return: 参数注解已解析、返回注解保持原样的签名
        """
        signature = inspect.signature(endpoint)
        return signature.replace(
            parameters=list(get_typed_signature(endpoint).parameters.values())
        )

    @staticmethod
    def _wrap_endpoint(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        """包装端点返回值，同时保持原函数签名供 FastAPI 注入依赖。"""
        signature = ResponseAPIRoute._resolved_signature(endpoint)
        if inspect.iscoroutinefunction(endpoint):

            @wraps(endpoint)
            async def async_endpoint(*args: Any, **kwargs: Any) -> Any:
                """异步调用端点并封装普通业务数据。"""
                result = await endpoint(*args, **kwargs)
                return ResponseAPIRoute._wrap_result(result)

            async_endpoint.__signature__ = signature  # type: ignore[attr-defined]
            return async_endpoint

        @wraps(endpoint)
        def sync_endpoint(*args: Any, **kwargs: Any) -> Any:
            """同步调用端点并封装普通业务数据。"""
            result = endpoint(*args, **kwargs)
            return ResponseAPIRoute._wrap_result(result)

        sync_endpoint.__signature__ = signature  # type: ignore[attr-defined]
        return sync_endpoint

    @staticmethod
    def _wrap_result(result: Any) -> Any:
        """保留已封装或原生响应，其余结果写入统一响应的数据区域。"""
        if isinstance(result, (Response, StarletteResponse)):
            return result
        return Response(success=True, data=result)


class ResponseAPIRouter(APIRouter):
    """默认使用统一响应路由类的 API 路由器。"""

    def __init__(self, **kwargs: Any) -> None:
        """初始化路由器并允许调用方显式覆盖路由类。"""
        kwargs.setdefault("route_class", ResponseAPIRoute)
        super().__init__(**kwargs)
