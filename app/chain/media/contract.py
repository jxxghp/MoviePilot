"""MediaChain owner 的静态组合合同。"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ClassVar, Optional, Protocol, TypeVar, Union

from app.chain.base import ChainBase
from app.chain.media.cache import AlbumDirectoryCache
from app.domain.context import MediaInfo, MusicAlbumInfo, MusicArtistInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.schemas.category import ClassificationSelection
from app.schemas.types import (
    ChainEventType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
)

_RecognitionCallback = Callable[[], Optional[MediaInfo]]
_AsyncRecognitionCallback = Callable[[], Awaitable[Optional[MediaInfo]]]
_ClassificationSubjectT = TypeVar(
    "_ClassificationSubjectT",
    MediaInfo,
    MusicInfo,
    MusicAlbumInfo,
    MusicArtistInfo,
)
_RecognitionPredicate = Callable[[Optional[MediaInfo]], bool]
_MediaPayload = dict[str, Any]


class _ProjectionEventManagerPort(Protocol):
    """声明身份投影 owner 使用的最小事件发送合同。"""

    def send_event(
        self,
        event_type: ChainEventType,
        data: object,
    ) -> Optional[object]:
        """同步发送身份投影事件。"""
        ...

    async def async_send_event(
        self,
        event_type: ChainEventType,
        data: object,
    ) -> Optional[object]:
        """异步发送身份投影事件。"""
        ...


if TYPE_CHECKING:

    class _MediaOwnerBase:
        """声明各媒体 owner 组合后可依赖的精确静态合同。"""

        _video_primary_source: ClassVar[MediaSource]
        _music_primary_source: ClassVar[MediaSource]
        _album_dir_cache: ClassVar[AlbumDirectoryCache]
        _album_match_min_files: ClassVar[int]
        _music_simplified_text_fields: ClassVar[tuple[str, ...]]
        _music_simplified_list_fields: ClassVar[tuple[str, ...]]
        eventmanager: _ProjectionEventManagerPort

        def run_module(
            self,
            method: str,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """通过运行时分发器同步调用模块能力。"""
            ...

        async def async_run_module(
            self,
            method: str,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """通过运行时分发器异步调用模块能力。"""
            ...

        def _finalize_recognition_result(
            self,
            mediainfo: Optional[_ClassificationSubjectT],
            *,
            effective_override: ClassificationSelection | None = None,
            refresh: bool = False,
        ) -> Optional[_ClassificationSubjectT]:
            """通过注入的应用服务分类一个完整识别结果。"""
            ...

        async def _async_finalize_recognition_result(
            self,
            mediainfo: Optional[_ClassificationSubjectT],
            *,
            effective_override: ClassificationSelection | None = None,
            refresh: bool = False,
        ) -> Optional[_ClassificationSubjectT]:
            """通过注入的应用服务异步补充并分类完整识别结果。"""
            ...

        def recognize_media(
            self,
            meta: Optional[MetaBase] = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            episode_group: Optional[str] = None,
            cache: bool = True,
            share_meta: Optional[MetaBase] = None,
            music_type: Optional[str] = None,
        ) -> Optional[MediaInfo]:
            """同步执行稳定媒体识别入口。"""
            ...

        async def async_recognize_media(
            self,
            meta: Optional[MetaBase] = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            episode_group: Optional[str] = None,
            cache: bool = True,
            share_meta: Optional[MetaBase] = None,
            music_type: Optional[str] = None,
        ) -> Optional[MediaInfo]:
            """异步执行稳定媒体识别入口。"""
            ...

        def recognize_music_from_source(
            self,
            media_source: MediaSource,
            meta: Optional[MetaMusic] = None,
            media_id: Optional[str] = None,
            cache: bool = True,
            music_type: Optional[str] = None,
        ) -> Optional[MusicInfo]:
            """同步按固定来源识别音乐实体。"""
            ...

        async def async_recognize_music_from_source(
            self,
            media_source: MediaSource,
            meta: Optional[MetaMusic] = None,
            media_id: Optional[str] = None,
            cache: bool = True,
            music_type: Optional[str] = None,
        ) -> Optional[MusicInfo]:
            """异步按固定来源识别音乐实体。"""
            ...

        @classmethod
        def _simplify_recognized_music_info(cls, info: MusicInfo) -> MusicInfo:
            """按配置返回简体化音乐信息。"""
            ...

        @classmethod
        def _simplify_recognized_music_mapping(
            cls,
            matched: dict[str, MusicInfo],
        ) -> dict[str, MusicInfo]:
            """按配置返回简体化音乐目录映射。"""
            ...

        def recognize_music_album_directory(
            self,
            path: Union[str, Path],
        ) -> dict[str, MusicInfo]:
            """同步识别音乐专辑目录。"""
            ...

        async def async_recognize_music_album_directory(
            self,
            path: Union[str, Path],
        ) -> dict[str, MusicInfo]:
            """异步识别音乐专辑目录。"""
            ...

        def select_recognize_source(
            self,
            log_name: str,
            log_context: str,
            native_fn: _RecognitionCallback,
            plugin_fn: _RecognitionCallback,
            is_recognized: Optional[_RecognitionPredicate] = None,
            plugin_event: ChainEventType = ChainEventType.NameRecognize,
        ) -> Optional[MediaInfo]:
            """同步选择原生或插件识别结果。"""
            ...

        async def async_select_recognize_source(
            self,
            log_name: str,
            log_context: str,
            native_fn: _AsyncRecognitionCallback,
            plugin_fn: _AsyncRecognitionCallback,
            is_recognized: Optional[_RecognitionPredicate] = None,
            plugin_event: ChainEventType = ChainEventType.NameRecognize,
        ) -> Optional[MediaInfo]:
            """异步选择原生或插件识别结果。"""
            ...

        def recognize_help(
            self,
            title: str,
            org_meta: MetaBase,
            share_meta: Optional[MetaBase] = None,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
        ) -> Optional[MediaInfo]:
            """同步请求插件辅助识别。"""
            ...

        async def async_recognize_help(
            self,
            title: str,
            org_meta: MetaBase,
            share_meta: Optional[MetaBase] = None,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
        ) -> Optional[MediaInfo]:
            """异步请求插件辅助识别。"""
            ...

        def _recognize_with_fallback_by_meta(
            self,
            metainfo: MetaBase,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            obtain_images: bool = False,
            music_type: Optional[str] = None,
        ) -> Optional[MediaInfo]:
            """同步识别元数据并在需要时回退插件。"""
            ...

        async def _async_recognize_with_fallback_by_meta(
            self,
            metainfo: MetaBase,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            obtain_images: bool = False,
            music_type: Optional[str] = None,
        ) -> Optional[MediaInfo]:
            """异步识别元数据并在需要时回退插件。"""
            ...

        def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
            """同步补充影视图片。"""
            ...

        async def async_obtain_images(
            self,
            mediainfo: MediaInfo,
        ) -> Optional[MediaInfo]:
            """异步补充影视图片。"""
            ...

        def search_medias(
            self,
            meta: MetaBase,
            media_source: Optional[MediaSourceSelection] = None,
        ) -> Optional[list[MediaInfo]]:
            """同步搜索媒体候选。"""
            ...

        async def async_search_medias(
            self,
            meta: MetaBase,
            media_source: Optional[MediaSourceSelection] = None,
        ) -> Optional[list[MediaInfo]]:
            """异步搜索媒体候选。"""
            ...

        def douban_info(
            self,
            doubanid: str,
            mtype: Optional[MediaType] = None,
            raise_exception: bool = False,
        ) -> Optional[_MediaPayload]:
            """同步读取豆瓣详情。"""
            ...

        async def async_douban_info(
            self,
            doubanid: str,
            mtype: Optional[MediaType] = None,
            raise_exception: bool = False,
        ) -> Optional[_MediaPayload]:
            """异步读取豆瓣详情。"""
            ...

        def tmdb_info(
            self,
            tmdbid: int,
            mtype: MediaType,
            season: Optional[int] = None,
        ) -> Optional[_MediaPayload]:
            """同步读取 TMDB 详情。"""
            ...

        async def async_tmdb_info(
            self,
            tmdbid: int,
            mtype: MediaType,
            season: Optional[int] = None,
        ) -> Optional[_MediaPayload]:
            """异步读取 TMDB 详情。"""
            ...

        def bangumi_info(self, bangumiid: int) -> Optional[_MediaPayload]:
            """同步读取 Bangumi 详情。"""
            ...

        async def async_bangumi_info(
            self,
            bangumiid: int,
        ) -> Optional[_MediaPayload]:
            """异步读取 Bangumi 详情。"""
            ...

        def match_doubaninfo(
            self,
            name: str,
            imdbid: Optional[str] = None,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
            raise_exception: bool = False,
        ) -> Optional[_MediaPayload]:
            """同步匹配豆瓣详情。"""
            ...

        async def async_match_doubaninfo(
            self,
            name: str,
            imdbid: Optional[str] = None,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
            raise_exception: bool = False,
        ) -> Optional[_MediaPayload]:
            """异步匹配豆瓣详情。"""
            ...

        def match_tmdbinfo(
            self,
            name: str,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
        ) -> Optional[_MediaPayload]:
            """同步匹配 TMDB 详情。"""
            ...

        async def async_match_tmdbinfo(
            self,
            name: str,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
        ) -> Optional[_MediaPayload]:
            """异步匹配 TMDB 详情。"""
            ...

else:
    _MediaOwnerBase = ChainBase
