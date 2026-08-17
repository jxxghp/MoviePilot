"""搜索参数与结果缓存的应用服务。"""

from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.schemas.media import parse_media_key, resolve_media_identity
from app.schemas.types import MediaSource, MediaType


def stringify_sites(sites: Optional[List[int]]) -> str:
    """将站点 ID 列表转换为前端可复用的逗号分隔值。"""
    return ",".join(str(site) for site in sites) if sites else ""


def normalize_search_params(
        params: Optional[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """把搜索缓存归一为前端重新搜索使用的稳定字段。"""
    if not isinstance(params, dict):
        return None

    media_source, media_id = resolve_media_identity(
        media_source=params.get("media_source"),
        media_id=params.get("media_id"),
    )
    keyword = str(params.get("keyword") or "")
    if not media_source and keyword:
        media_source, media_id = parse_media_key(keyword)
        if media_source and media_id:
            keyword = ""

    normalized = {
        "keyword": keyword,
        "media_source": str(media_source) if media_source else "",
        "media_id": media_id or "",
        "type": str(params.get("type") or ""),
        "area": str(params.get("area") or ""),
        "title": str(params.get("title") or ""),
        "year": str(params.get("year") or ""),
        "season": str(params["season"]) if params.get("season") is not None else "",
        "episode": str(params.get("episode") or ""),
        "sites": str(params.get("sites") or ""),
        "result_type": str(params.get("result_type") or "torrent"),
    }
    if params.get("music_type"):
        normalized["music_type"] = str(params["music_type"])
    return normalized if normalized["keyword"] or media_id else None


class SearchStateService:
    """通过注入的缓存端口保存和读取搜索状态。"""

    def __init__(
        self,
        save_cache: Callable[[Any, str], None],
        load_cache: Callable[[str], Any],
        async_save_cache: Callable[[Any, str], Awaitable[None]],
        async_load_cache: Callable[[str], Awaitable[Any]],
        params_key: str,
        result_key: str,
        subtitle_result_key: str,
    ) -> None:
        """保存缓存端口和兼容缓存键。"""
        self._save_cache = save_cache
        self._load_cache = load_cache
        self._async_save_cache = async_save_cache
        self._async_load_cache = async_load_cache
        self._params_key = params_key
        self._result_key = result_key
        self._subtitle_result_key = subtitle_result_key

    @staticmethod
    def build_params(
        *,
        keyword: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        mtype: Optional[MediaType] = None,
        area: Optional[str] = "title",
        title: Optional[str] = None,
        year: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        sites: Optional[List[int]] = None,
        music_type: Optional[str] = None,
        result_type: Optional[str] = "torrent",
    ) -> Optional[Dict[str, str]]:
        """把公开搜索参数构造成可持久化的兼容字典。"""
        return normalize_search_params(
            {
                "keyword": keyword,
                "media_source": media_source,
                "media_id": media_id,
                "type": mtype.value if isinstance(mtype, MediaType) else mtype,
                "area": area,
                "title": title,
                "year": year,
                "season": season,
                "episode": episode,
                "sites": stringify_sites(sites),
                "music_type": music_type,
                "result_type": result_type or "torrent",
            }
        )

    def save_params(self, **kwargs: Any) -> None:
        """同步保存最后一次有效搜索参数。"""
        params = self.build_params(**kwargs)
        if params:
            self._save_cache(params, self._params_key)

    async def async_save_params(self, **kwargs: Any) -> None:
        """异步保存最后一次有效搜索参数。"""
        params = self.build_params(**kwargs)
        if params:
            await self._async_save_cache(params, self._params_key)

    def load_params(self) -> Optional[Dict[str, str]]:
        """同步读取并归一化最后一次搜索参数。"""
        return normalize_search_params(self._load_cache(self._params_key))

    async def async_load_params(self) -> Optional[Dict[str, str]]:
        """异步读取并归一化最后一次搜索参数。"""
        return normalize_search_params(await self._async_load_cache(self._params_key))

    def load_results(self) -> Any:
        """同步读取最后一次资源搜索结果。"""
        return self._load_cache(self._result_key)

    async def async_load_results(self) -> Any:
        """异步读取最后一次资源搜索结果。"""
        return await self._async_load_cache(self._result_key)

    async def async_load_subtitle_results(self) -> Any:
        """异步读取最后一次字幕搜索结果。"""
        return await self._async_load_cache(self._subtitle_result_key)
