import inspect
import json
from functools import wraps
from typing import Annotated, Any, Awaitable, Callable, Optional, get_args, get_origin

from fastapi import APIRouter, Depends, Query, Request
from fastapi.datastructures import DefaultPlaceholder
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
COLLECTION_PAGINATION_OPENAPI_KEY = "x-moviepilot-compatible-pagination"
COLLECTION_TOTAL_OPENAPI_KEY = "x-moviepilot-exact-total"
COLLECTION_TOTAL_HEADER = "X-Total-Count"
COLLECTION_RESULT_HEADER = "X-Result-Count"
COLLECTION_PAGE_HEADER = "X-Page"
COLLECTION_PAGE_SIZE_HEADER = "X-Page-Size"
COLLECTION_DEFAULT_PAGE_SIZE = 50
COLLECTION_MAX_PAGE_SIZE = 200
_COLLECTION_WINDOW_PARAMETERS = frozenset(
    {"page", "count", "limit", "offset", "page_size", "max_results"}
)
_COLLECTION_RESPONSE_HEADERS = {
    COLLECTION_RESULT_HEADER: {
        "description": "Number of collection items serialized in this response body.",
        "schema": {"type": "integer", "minimum": 0},
    },
    COLLECTION_PAGE_HEADER: {
        "description": "Effective one-based page when request pagination is active.",
        "schema": {"type": "integer", "minimum": 1},
    },
    COLLECTION_PAGE_SIZE_HEADER: {
        "description": "Effective page size when request pagination is active.",
        "schema": {"type": "integer", "minimum": 1},
    },
}


def _optional_collection_pagination(
    page: Annotated[
        Optional[int],
        Query(
            ge=1,
            description=(
                "Optional one-based page for a legacy full-list endpoint. Omit both page and "
                "count to keep the original unpaginated full result."
            ),
        ),
    ] = None,
    count: Annotated[
        Optional[int],
        Query(
            ge=1,
            le=COLLECTION_MAX_PAGE_SIZE,
            description=(
                "Optional page size for a legacy full-list endpoint. Supplying page or count "
                f"activates pagination; an omitted count then uses {COLLECTION_DEFAULT_PAGE_SIZE}."
            ),
        ),
    ] = None,
) -> None:
    """校验兼容分页参数；实际切片由统一响应路由在序列化后执行。"""
    del page, count


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
        force_collection_pagination = bool(
            openapi_extra.get(COLLECTION_PAGINATION_OPENAPI_KEY)
        )
        endpoint_reports_collection_total = bool(
            openapi_extra.get(COLLECTION_TOTAL_OPENAPI_KEY)
        )

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

        methods = {
            str(method).upper()
            for method in (kwargs.get("methods") or [])
        }
        endpoint_parameters = set(inspect.signature(endpoint).parameters)
        collection_response = self._is_collection_response_model(response_model)
        collection_window_parameters = endpoint_parameters & _COLLECTION_WINDOW_PARAMETERS
        optional_collection_pagination = bool(
            collection_response
            and ("GET" in methods or force_collection_pagination)
            and not collection_window_parameters
        )
        if optional_collection_pagination:
            dependencies = list(kwargs.get("dependencies") or [])
            dependencies.append(Depends(_optional_collection_pagination))
            kwargs["dependencies"] = dependencies
        if collection_response:
            kwargs["responses"] = self._merge_collection_response_headers(
                kwargs.get("responses"),
                include_total=(
                    optional_collection_pagination
                    or endpoint_reports_collection_total
                ),
            )

        self._collection_response = collection_response
        self._optional_collection_pagination = optional_collection_pagination
        self._collection_parameter_defaults = self._parameter_defaults(
            endpoint,
            collection_window_parameters,
        )

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

    def get_route_handler(
        self,
    ) -> Callable[[Request], Awaitable[StarletteResponse]]:
        """在标准端点序列化后附加兼容列表分页与数量元数据。"""
        original_handler = super().get_route_handler()
        if not self._collection_response:
            return original_handler

        async def collection_handler(request: Request) -> StarletteResponse:
            """保留列表响应体形状，并在显式请求时执行兼容切片。"""
            response = await original_handler(request)
            return self._apply_collection_contract(request, response)

        return collection_handler

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
    def _is_collection_response_model(response_model: Any) -> bool:
        """判断响应模型的业务数据是否为列表。"""
        if response_model is None:
            return False
        generic_metadata = getattr(
            response_model,
            "__pydantic_generic_metadata__",
            None,
        )
        if isinstance(generic_metadata, dict):
            arguments = generic_metadata.get("args") or ()
            if arguments:
                response_model = arguments[0]
        if get_origin(response_model) is list:
            return True
        if isinstance(response_model, type):
            model_fields = getattr(response_model, "model_fields", None)
            root_field = model_fields.get("root") if isinstance(model_fields, dict) else None
            if root_field is not None and get_origin(root_field.annotation) is list:
                return True
        return get_origin(response_model) in {list, tuple} and bool(get_args(response_model))

    @staticmethod
    def _parameter_defaults(
        endpoint: Callable[..., Any],
        parameter_names: set[str],
    ) -> dict[str, Any]:
        """读取原生分页或限量参数的端点默认值，供响应元数据复用。"""
        signature = inspect.signature(endpoint)
        defaults: dict[str, Any] = {}
        for name in parameter_names:
            parameter = signature.parameters.get(name)
            if parameter is None or parameter.default is inspect.Parameter.empty:
                continue
            defaults[name] = parameter.default
        return defaults

    @staticmethod
    def _merge_collection_response_headers(
        responses: dict[int | str, dict[str, Any]] | None,
        *,
        include_total: bool,
    ) -> dict[int | str, dict[str, Any]]:
        """为列表响应声明兼容数量头，并保留端点既有成功响应定义。"""
        merged = dict(responses or {})
        success_key: int | str = 200 if "200" not in merged else "200"
        success_response = dict(merged.get(success_key) or {})
        headers = dict(success_response.get("headers") or {})
        headers.update(_COLLECTION_RESPONSE_HEADERS)
        if include_total:
            headers[COLLECTION_TOTAL_HEADER] = {
                "description": (
                    "Exact collection size before optional compatibility pagination. This header "
                    "is omitted when an upstream-native page or limit does not expose a total."
                ),
                "schema": {"type": "integer", "minimum": 0},
            }
        success_response["headers"] = headers
        merged[success_key] = success_response
        return merged

    def _apply_collection_contract(
        self,
        request: Request,
        response: StarletteResponse,
    ) -> StarletteResponse:
        """对已序列化列表应用显式分页，同时通过响应头报告数量元数据。"""
        content_type = response.headers.get("content-type", "").lower()
        body = getattr(response, "body", None)
        if (
            response.status_code >= 400
            or body is None
            or not ("application/json" in content_type or "+json" in content_type)
        ):
            return response
        try:
            payload = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return response
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return response

        total_count = len(items)
        page: Optional[int] = None
        page_size: Optional[int] = None
        if self._optional_collection_pagination and (
            "page" in request.query_params or "count" in request.query_params
        ):
            page = int(request.query_params.get("page", "1"))
            page_size = int(
                request.query_params.get(
                    "count",
                    str(COLLECTION_DEFAULT_PAGE_SIZE),
                )
            )
            start = (page - 1) * page_size
            paged_items = items[start : start + page_size]
            if isinstance(payload, dict):
                payload["data"] = paged_items
            else:
                payload = paged_items
            items = paged_items
            response.body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            response.headers["content-length"] = str(len(response.body))
        elif not self._optional_collection_pagination:
            page = self._request_int_value(request, "page")
            page_size = self._request_int_value(request, "count")

        if self._optional_collection_pagination:
            response.headers[COLLECTION_TOTAL_HEADER] = str(total_count)
        response.headers[COLLECTION_RESULT_HEADER] = str(len(items))
        if page is not None:
            response.headers[COLLECTION_PAGE_HEADER] = str(page)
        if page_size is not None:
            response.headers[COLLECTION_PAGE_SIZE_HEADER] = str(page_size)
        return response

    def _request_int_value(
        self,
        request: Request,
        name: str,
    ) -> Optional[int]:
        """读取请求显式值或端点默认整数值，无法解释时不写分页头。"""
        raw_value: Any = request.query_params.get(name)
        if raw_value is None:
            raw_value = self._collection_parameter_defaults.get(name)
        if raw_value is None or isinstance(raw_value, bool):
            return None
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _merge_error_responses(
        responses: dict[int | str, dict[str, Any]] | None,
    ) -> dict[int | str, dict[str, Any]]:
        """补齐统一错误模型，并保留端点已经显式声明的响应。"""
        merged_responses: dict[int | str, dict[str, Any]] = dict(ERROR_RESPONSES)
        merged_responses.update(responses or {})
        return merged_responses

    @staticmethod
    def _wrap_endpoint(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        """包装端点返回值，同时保持原函数签名供 FastAPI 注入依赖。"""
        if inspect.iscoroutinefunction(endpoint):

            @wraps(endpoint)
            async def async_endpoint(*args: Any, **kwargs: Any) -> Any:
                """异步调用端点并封装普通业务数据。"""
                result = await endpoint(*args, **kwargs)
                return ResponseAPIRoute._wrap_result(result)

            return async_endpoint

        @wraps(endpoint)
        def sync_endpoint(*args: Any, **kwargs: Any) -> Any:
            """同步调用端点并封装普通业务数据。"""
            result = endpoint(*args, **kwargs)
            return ResponseAPIRoute._wrap_result(result)

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
