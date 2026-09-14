from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, cast
from urllib.parse import urlsplit

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.runtime.cache import cached
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


@dataclass(frozen=True, slots=True)
class _BangumiRequestPlan:
    """冻结 Bangumi 请求地址、参数和结果字段，供同步与异步传输共用。"""

    url: str
    params: dict[str, Any]
    key: Optional[str] = None


class BangumiApi:
    """Bangumi API 客户端，统一同步与异步请求决策和结果投影。"""

    _urls = {
        "discover": "v0/subjects",
        "search": "search/subjects/%s?type=2",
        "calendar": "calendar",
        "detail": "v0/subjects/%s",
        "credits": "v0/subjects/%s/persons",
        "subjects": "v0/subjects/%s/subjects",
        "characters": "v0/subjects/%s/characters",
        "person_detail": "v0/persons/%s",
        "person_credits": "v0/persons/%s/subjects",
    }
    _base_url = "https://api.bgm.tv/"

    def __init__(
            self,
            base_url: Optional[str] = None,
    ) -> None:
        """初始化同步与异步 Bangumi 请求客户端及其代理地址快照。"""
        configured_base_url = (
            base_url if base_url is not None else (
                get_runtime_setting("BANGUMI_API_DOMAIN", "")
                if get_runtime_setting("BANGUMI_PROXY_ENABLE", False)
                else ""
            )
        )
        self._base_url = self._normalize_base_url(
            configured_base_url,
            default=self.__class__._base_url,
        )
        self._cache_token = self._base_url
        self._req = RequestUtils(
            ua=get_runtime_setting('NORMAL_USER_AGENT'),
            proxies=get_runtime_setting('PROXY'),
            use_session=True,
        )
        self._async_req = AsyncRequestUtils(
            ua=get_runtime_setting('NORMAL_USER_AGENT'),
            proxies=get_runtime_setting('PROXY'),
        )

    @property
    def base_url(self) -> str:
        """返回当前客户端使用的 Bangumi 数据请求地址。"""
        return self._base_url

    @staticmethod
    def _normalize_base_url(value: Any, default: str) -> str:
        """归一化 Bangumi 数据地址，兼容域名、完整 URL 和代理路径。"""
        raw_value = str(value or "").strip()
        if not raw_value:
            return default
        candidate = (
            raw_value
            if "://" in raw_value
            else f"https://{raw_value}"
        )
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return default
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            return default
        return f"{candidate.rstrip('/')}/"

    def _request_plan(
            self,
            path: str,
            key: Optional[str] = None,
            **params: Any,
    ) -> _BangumiRequestPlan:
        """构造同步与异步请求共同使用的不可变调用计划。"""
        return _BangumiRequestPlan(
            url=f"{self._base_url}{path}",
            params=dict(params),
            key=key,
        )

    @staticmethod
    def _project_response(
            status_code: Optional[int],
            payload: Any,
            key: Optional[str],
    ) -> Any:
        """按统一状态码与字段规则投影 Bangumi 响应。"""
        if status_code != 200:
            return None
        if key:
            return payload.get(key) if isinstance(payload, dict) else None
        return payload

    def _decode_response(self, response: Any, key: Optional[str]) -> Any:
        """解析 HTTP 响应，并把格式错误统一映射为空结果。"""
        if response is None:
            return None
        try:
            payload = response.json() if response.status_code == 200 else None
        except (TypeError, ValueError) as err:
            logger.warning(f"Bangumi 响应解析失败：{str(err)}")
            return None
        return self._project_response(response.status_code, payload, key)

    @cached(  # type: ignore[misc]  # 缓存装饰器的运行时签名由宿主统一维护
        maxsize=get_runtime_setting('CONF').bangumi,
        ttl=get_runtime_setting('CONF').meta,
        shared_key="get",
    )
    def _cached_invoke(
            self,
            url: str,
            key: Optional[str] = None,
            _cache_token: str = "",
            **kwargs: Any,
    ) -> Any:
        """执行带配置身份的同步 HTTP 请求，并复用统一响应解码。"""
        plan = self._request_plan(url, key=key, **kwargs)
        response = self._req.get_res(url=plan.url, params=plan.params)
        return self._decode_response(response, plan.key)

    def __invoke(
            self,
            url: str,
            key: Optional[str] = None,
            **kwargs: Any,
    ) -> Any:
        """执行同步 Bangumi 请求，并把当前代理配置纳入缓存键。"""
        return self._cached_invoke(
            url,
            key=key,
            _cache_token=self._cache_token,
            **kwargs,
        )

    @cached(  # type: ignore[misc]  # 缓存装饰器的运行时签名由宿主统一维护
        maxsize=get_runtime_setting('CONF').bangumi,
        ttl=get_runtime_setting('CONF').meta,
        shared_key="get",
    )
    async def _cached_async_invoke(
            self,
            url: str,
            key: Optional[str] = None,
            _cache_token: str = "",
            **kwargs: Any,
    ) -> Any:
        """执行带配置身份的异步 HTTP 请求，并复用统一响应解码。"""
        plan = self._request_plan(url, key=key, **kwargs)
        response = await self._async_req.get_res(url=plan.url, params=plan.params)
        return self._decode_response(response, plan.key)

    async def __async_invoke(
            self,
            url: str,
            key: Optional[str] = None,
            **kwargs: Any,
    ) -> Any:
        """执行异步 Bangumi 请求，并把当前代理配置纳入缓存键。"""
        return await self._cached_async_invoke(
            url,
            key=key,
            _cache_token=self._cache_token,
            **kwargs,
        )

    @staticmethod
    def _dated_params(**params: Any) -> dict[str, Any]:
        """为缓存键稳定附加当天日期，并保留调用方筛选参数。"""
        return {
            "_ts": datetime.strftime(datetime.now(), '%Y%m%d'),
            **params,
        }

    @staticmethod
    def _search_results(result: Any) -> list[dict[str, Any]]:
        """从旧版搜索响应中提取条目列表。"""
        return result.get("list") or [] if isinstance(result, dict) else []

    @staticmethod
    def _calendar_items(result: Any) -> list[dict[str, Any]]:
        """按星期顺序展开每日放送条目。"""
        return [
            item
            for weekday in result or []
            if isinstance(weekday, dict)
            for item in weekday.get("items") or []
        ]

    @staticmethod
    def _credit_people(result: Any) -> list[dict[str, Any]]:
        """把角色配音关系投影为带角色职业信息的人物列表。"""
        people: list[dict[str, Any]] = []
        for item in result or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            actors = item.get("actors") or []
            if not actors or not isinstance(actors[0], dict):
                continue
            actor = actors[0]
            actor.update({"career": [item.get("name")]})
            people.append(actor)
        return people

    @staticmethod
    def _list_result(result: Any) -> list[dict[str, Any]]:
        """把列表响应统一投影为空安全列表。"""
        return list(result) if isinstance(result, list) else []

    def search(self, name: str) -> list[dict[str, Any]]:
        """搜索媒体信息。"""
        return self._search_results(self.__invoke(f"search/subject/{name}"))

    async def async_search(self, name: str) -> list[dict[str, Any]]:
        """异步搜索媒体信息。"""
        return self._search_results(await self.__async_invoke(f"search/subject/{name}"))

    def calendar(self) -> list[dict[str, Any]]:
        """获取每日放送条目。"""
        result = self.__invoke(self._urls["calendar"], **self._dated_params())
        return self._calendar_items(result)

    async def async_calendar(self) -> list[dict[str, Any]]:
        """异步获取每日放送条目。"""
        result = await self.__async_invoke(
            self._urls["calendar"], **self._dated_params()
        )
        return self._calendar_items(result)

    def detail(self, bid: int) -> Optional[dict[str, Any]]:
        """获取番剧详情。"""
        return cast(
            Optional[dict[str, Any]],
            self.__invoke(self._urls["detail"] % bid, **self._dated_params()),
        )

    async def async_detail(self, bid: int) -> Optional[dict[str, Any]]:
        """异步获取番剧详情。"""
        return cast(
            Optional[dict[str, Any]],
            await self.__async_invoke(
                self._urls["detail"] % bid, **self._dated_params()
            ),
        )

    def credits(self, bid: int) -> list[dict[str, Any]]:
        """获取番剧配音人物。"""
        result = self.__invoke(self._urls["characters"] % bid, **self._dated_params())
        return self._credit_people(result)

    async def async_credits(self, bid: int) -> list[dict[str, Any]]:
        """异步获取番剧配音人物。"""
        result = await self.__async_invoke(
            self._urls["characters"] % bid, **self._dated_params()
        )
        return self._credit_people(result)

    def subjects(self, bid: int) -> Optional[list[dict[str, Any]]]:
        """获取关联条目信息。"""
        return cast(
            Optional[list[dict[str, Any]]],
            self.__invoke(self._urls["subjects"] % bid, **self._dated_params()),
        )

    async def async_subjects(self, bid: int) -> Optional[list[dict[str, Any]]]:
        """异步获取关联条目信息。"""
        return cast(
            Optional[list[dict[str, Any]]],
            await self.__async_invoke(
                self._urls["subjects"] % bid, **self._dated_params()
            ),
        )

    def person_detail(self, person_id: int) -> Optional[dict[str, Any]]:
        """获取人物详细信息。"""
        return cast(
            Optional[dict[str, Any]],
            self.__invoke(
                self._urls["person_detail"] % person_id, **self._dated_params()
            ),
        )

    async def async_person_detail(
            self, person_id: int
    ) -> Optional[dict[str, Any]]:
        """异步获取人物详细信息。"""
        return cast(
            Optional[dict[str, Any]],
            await self.__async_invoke(
                self._urls["person_detail"] % person_id, **self._dated_params()
            ),
        )

    def person_credits(self, person_id: int) -> list[dict[str, Any]]:
        """获取人物参演作品。"""
        result = self.__invoke(
            self._urls["person_credits"] % person_id, **self._dated_params()
        )
        return self._list_result(result)

    async def async_person_credits(self, person_id: int) -> list[dict[str, Any]]:
        """异步获取人物参演作品。"""
        result = await self.__async_invoke(
            self._urls["person_credits"] % person_id, **self._dated_params()
        )
        return self._list_result(result)

    def discover(self, **kwargs: Any) -> Optional[list[dict[str, Any]]]:
        """按筛选条件发现番剧。"""
        return cast(
            Optional[list[dict[str, Any]]],
            self.__invoke(
                self._urls["discover"],
                key="data",
                **self._dated_params(**kwargs),
            ),
        )

    async def async_discover(
            self, **kwargs: Any
    ) -> Optional[list[dict[str, Any]]]:
        """异步按筛选条件发现番剧。"""
        return cast(
            Optional[list[dict[str, Any]]],
            await self.__async_invoke(
                self._urls["discover"],
                key="data",
                **self._dated_params(**kwargs),
            ),
        )

    def clear_cache(self) -> None:
        """清除 Bangumi 请求缓存。"""
        self._cached_invoke.cache_clear()

    def close(self) -> None:
        """关闭 Bangumi 同步会话。"""
        self._req.close()
