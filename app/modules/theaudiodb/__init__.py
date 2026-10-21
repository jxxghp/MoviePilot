"""
TheAudioDB 音乐搜索、身份匹配和详情转换，统一同步与异步请求语义。
"""

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple, Union, cast

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.application.music.observation import music_request_timeout, report_music_recognition
from app.application.music.recognition import (
    async_enrich_music_artist_aliases,
    enrich_music_artist_aliases,
    unique_music_match,
)
from app.domain.context import (
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
)
from app.domain.media import is_media_source_selected
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic, music_credit_values
from app.domain.music import (
    music_album_artist_evidence_matches,
    music_album_year_matches,
    music_artist_evidence_matches,
    music_credit_conflicts,
    music_isrc_matches,
    music_query_artists,
    music_release_year_matches,
    music_title_matches,
    music_version_matches,
)
from app.modules import _ModuleBase
from app.runtime.cache import CacheControls, cached
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaRecognizeType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
    ModuleType,
    MusicEntityType,
)


@dataclass(frozen=True, slots=True)
class _TheAudioDbRecognitionPlan:
    """
    TheAudioDB 识别允许使用的身份线索与实体范围。

    :param meta: 候选搜索所需的音乐线索，仅查询详情时可为 None
    :param media_id: 明确的来源标识，None 表示使用候选搜索
    :param music_type: recording 限定单曲、album 限定专辑；None 按先单曲后专辑的顺序尝试
    """

    meta: Optional[MetaMusic]
    media_id: Optional[str]
    music_type: Optional[str]

    @property
    def search_recording(self) -> bool:
        """
        判断计划是否允许查询单曲。

        :return: 实体范围不是 album 时为 True
        """
        return bool(self.music_type != MUSIC_ENTITY_ALBUM)

    @property
    def search_album(self) -> bool:
        """
        判断单曲未匹配时是否允许查询专辑。

        :return: 实体范围不是 recording 时为 True
        """
        return bool(self.music_type != MUSIC_ENTITY_RECORDING)

    def require_meta(self) -> MetaMusic:
        """
        读取候选搜索必需的音乐线索。

        :return: 计划保存的音乐线索
        :raises RuntimeError: 计划未提供 meta，调用方选错了执行分支
        """
        if self.meta is None:
            raise RuntimeError("TheAudioDB 候选识别计划缺少音乐元数据")
        return self.meta

    def require_media_id(self) -> str:
        """
        读取详情查询必需的来源标识。

        :return: 计划保存的原生标识
        :raises RuntimeError: 计划未提供 media_id，调用方选错了执行分支
        """
        if self.media_id is None:
            raise RuntimeError("TheAudioDB 详情识别计划缺少原生 ID")
        return self.media_id


@dataclass(frozen=True, slots=True)
class _TheAudioDbRequestPlan:
    """
    同步与异步查询共用的 TheAudioDB 请求计划。

    :param url: 包含应用密钥的完整接口地址，不应写入公开日志
    :param params: 接口查询参数的独立副本
    """

    url: str
    params: dict[str, Any]


class TheAudioDbModule(_ModuleBase):
    """
    通过 TheAudioDB V1 API 提供音乐搜索、详情和手动识别能力。
    """

    _source = MediaSource.TheAudioDB
    _base_url = "https://www.theaudiodb.com/api/v1/json"
    _detail_url = "https://www.theaudiodb.com"

    def init_module(self) -> None:
        """
        初始化无状态的 TheAudioDB 模块。
        """

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        TheAudioDB 使用环境配置中的 API Key，无独立启用开关。
        """
        return None

    def stop(self) -> None:
        """
        停止模块；当前实现没有需要释放的持久资源。
        """

    def test(self) -> Tuple[bool, str]:
        """
        测试 TheAudioDB 艺术家搜索接口连通性。
        """
        result = self._request_json("search.php", {"s": "coldplay"})
        return (True, "") if result is not None else (False, "TheAudioDB 网络连接失败")

    @staticmethod
    def get_name() -> str:
        """
        返回模块展示名称。
        """
        return "TheAudioDB"

    @staticmethod
    def get_music_source() -> MediaSource:
        """
        返回音乐识别使用的数据源标识。
        """
        return TheAudioDbModule._source

    @staticmethod
    def get_type() -> ModuleType:
        """
        返回模块所属的媒体识别类型。
        """
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """
        返回 TheAudioDB 模块子类型。
        """
        return MediaRecognizeType.TheAudioDB

    @staticmethod
    def get_priority() -> int:
        """
        返回音乐识别优先级，位于默认 MusicBrainz 之后。
        """
        return 1

    def search_music(
            self,
            meta: MetaMusic,
            limit: int = 20,
            media_source: Optional[MediaSourceSelection] = None,
            music_types: Optional[Iterable[MusicEntityType]] = None,
    ) -> Optional[list[MusicInfo]]:
        """
        按实体范围搜索 TheAudioDB 单曲、专辑和艺术家，并交错合并候选。

        :param meta: 搜索使用的音乐线索，也可指定默认实体类型
        :param limit: 最多返回的结果数量，限制在 1 至 100
        :param media_source: 来源选择条件，未包含 TheAudioDB 时不响应
        :param music_types: 优先使用的实体范围；空值使用线索类型，线索未指定时查询全部三类
        :return: 统一音乐候选列表；来源未选中时为 None，无候选或请求失败时为空列表
        """
        if not is_media_source_selected(media_source, self._source):
            return None
        normalized_limit = max(1, min(limit, 100))
        selected: set[str] = set(music_types) if music_types else ({meta.music_type} if meta.music_type else {"recording", "album", "artist"})
        tracks = self._search_tracks(meta) if "recording" in selected else []
        albums = self._search_albums(meta) if "album" in selected else []
        artists = self._search_artists(meta) if "artist" in selected else []
        return self._interleave_results(
            tracks,
            albums,
            artists,
            limit=normalized_limit,
        )

    def recognize_media(
            self,
            meta: Optional[MetaBase] = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            **kwargs: Any,
    ) -> Optional[MusicInfo]:
        """
        响应明确选择 TheAudioDB 的音乐识别请求。

        有明确身份时查询详情，否则先筛选单曲，再按实体范围决定是否回退专辑。

        :param meta: 音乐线索；仅按明确身份查询时可省略
        :param mtype: 没有 MetaMusic 线索时必须显式为音乐类型
        :param media_source: 请求来源，必须为 TheAudioDB 才执行查询
        :param media_id: 原生标识，优先于线索中的标识
        :param kwargs: music_type 可将识别限定为 recording 或 album，其他参数不使用
        :return: 确认的音乐结果；来源或类型不符、无匹配或请求失败时返回 None
        """
        plan = self._recognition_plan(
            meta=meta,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            music_type=kwargs.get("music_type"),
        )
        if not plan:
            return None
        if plan.media_id:
            return self.recognize_music(
                self._source, plan.require_media_id(), music_type=plan.music_type
            )
        plan_meta = plan.require_meta()
        matched: Optional[MusicInfo] = None
        if plan.search_recording:
            matched = self._select_track(plan_meta, self._search_tracks(plan_meta))
            if matched:
                return matched
        if not self._should_search_album(plan, matched):
            return None
        album = self._select_album(plan_meta, self._search_albums(plan_meta))
        return self._project_album_result(album)

    async def async_recognize_media(
            self,
            meta: Optional[MetaBase] = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            **kwargs: Any,
    ) -> Optional[MusicInfo]:
        """
        异步响应明确选择 TheAudioDB 的音乐识别请求。

        有明确身份时查询详情，否则先筛选单曲，再按实体范围决定是否回退专辑。

        :param meta: 音乐线索；仅按明确身份查询时可省略
        :param mtype: 没有 MetaMusic 线索时必须显式为音乐类型
        :param media_source: 请求来源，必须为 TheAudioDB 才执行查询
        :param media_id: 原生标识，优先于线索中的标识
        :param kwargs: music_type 可将识别限定为 recording 或 album，其他参数不使用
        :return: 确认的音乐结果；来源或类型不符、无匹配或请求失败时返回 None
        """
        plan = self._recognition_plan(
            meta=meta,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            music_type=kwargs.get("music_type"),
        )
        if not plan:
            return None
        if plan.media_id:
            return await self.async_recognize_music(
                self._source,
                plan.require_media_id(),
                music_type=plan.music_type,
            )
        plan_meta = plan.require_meta()
        matched: Optional[MusicInfo] = None
        if plan.search_recording:
            matched = self._select_track(
                plan_meta,
                await self._async_search_tracks(plan_meta),
            )
            if matched:
                return matched
        if not self._should_search_album(plan, matched):
            return None
        album = self._select_album(
            plan_meta,
            await self._async_search_albums(plan_meta),
        )
        return self._project_album_result(album)

    @classmethod
    def _recognition_plan(
            cls,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            music_type: Optional[str],
    ) -> Optional[_TheAudioDbRecognitionPlan]:
        """
        统一完成来源、音乐类型和原生 ID 准入。
        """
        if media_source != cls._source:
            return None
        if not isinstance(meta, MetaMusic):
            if mtype != MediaType.MUSIC or not media_id:
                return None
            return _TheAudioDbRecognitionPlan(
                meta=None, media_id=str(media_id), music_type=music_type
            )
        return _TheAudioDbRecognitionPlan(
            meta=meta,
            media_id=str(media_id or meta.media_id) if media_id or meta.media_id else None,
            music_type=music_type,
        )

    @staticmethod
    def _should_search_album(
            plan: _TheAudioDbRecognitionPlan,
            matched: Optional[MusicInfo],
    ) -> bool:
        """
        统一决定单曲未命中后是否继续查询专辑。
        """
        return bool(not matched and plan.search_album)

    def recognize_music(
            self,
            media_source: MediaSource,
            media_id: str,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """
        按原生标识查询单曲或专辑详情。

        :param media_source: 请求来源，必须为 TheAudioDB 才执行查询
        :param media_id: TheAudioDB 单曲或专辑标识
        :param music_type: recording 仅查询单曲，album 仅查询专辑，省略时先单曲后专辑
        :return: 统一音乐结果；来源不符、标识缺失、无结果或查询失败时返回 None
        """
        plan = self._detail_plan(media_source, media_id, music_type)
        if not plan:
            return None
        result: Optional[MusicInfo] = None
        if plan.search_recording:
            payload = self._request_json(
                "track.php", {"h": plan.require_media_id()}
            )
            result = self._project_track_detail(payload)
            if result:
                return result
        if not self._should_search_album(plan, result):
            return None
        album = self.music_album(self._source, plan.require_media_id())
        return self._project_album_result(album)

    async def async_recognize_music(
            self,
            media_source: MediaSource,
            media_id: str,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """
        异步按原生标识查询单曲或专辑详情。

        :param media_source: 请求来源，必须为 TheAudioDB 才执行查询
        :param media_id: TheAudioDB 单曲或专辑标识
        :param music_type: recording 仅查询单曲，album 仅查询专辑，省略时先单曲后专辑
        :return: 统一音乐结果；来源不符、标识缺失、无结果或查询失败时返回 None
        """
        plan = self._detail_plan(media_source, media_id, music_type)
        if not plan:
            return None
        result: Optional[MusicInfo] = None
        if plan.search_recording:
            payload = await self._async_request_json(
                "track.php", {"h": plan.require_media_id()}
            )
            result = self._project_track_detail(payload)
            if result:
                return result
        if not self._should_search_album(plan, result):
            return None
        album = await self._async_music_album(
            self._source, plan.require_media_id()
        )
        return self._project_album_result(album)

    @classmethod
    def _detail_plan(
            cls,
            media_source: MediaSource,
            media_id: str,
            music_type: Optional[str],
    ) -> Optional[_TheAudioDbRecognitionPlan]:
        """
        统一校验详情来源并冻结实体探测顺序。
        """
        if media_source != cls._source or not media_id:
            return None
        return _TheAudioDbRecognitionPlan(
            meta=None, media_id=str(media_id), music_type=music_type
        )

    @classmethod
    def _project_track_detail(
            cls, payload: Optional[dict[str, Any]]
    ) -> Optional[MusicInfo]:
        """
        把单曲详情响应投影为统一音乐信息。
        """
        track = cls._first_entity(payload, "track", "tracks")
        return cls._track_to_info(track) if track else None

    @staticmethod
    def _project_album_result(
            album: Optional[MusicAlbumInfo],
    ) -> Optional[MusicInfo]:
        """
        把专辑候选统一投影到音乐识别返回类型。
        """
        return album.to_music_info() if album else None

    async def _async_music_album(
            self,
            media_source: MediaSource,
            media_id: str,
    ) -> Optional[MusicAlbumInfo]:
        """
        异步按 TheAudioDB 专辑 ID 获取标准化专辑详情和曲目。
        """
        if not self._detail_plan(media_source, media_id, MUSIC_ENTITY_ALBUM):
            return None
        payload = await self._async_request_json("album.php", {"m": media_id})
        album = self._project_album_header(payload)
        if not album:
            return None
        tracks_payload = await self._async_request_json("track.php", {"m": media_id})
        return self._project_album_tracks(album, tracks_payload)

    def music_album(
            self,
            media_source: MediaSource,
            media_id: str,
    ) -> Optional[MusicAlbumInfo]:
        """
        按专辑标识读取详情并附加可用曲目。

        :param media_source: 请求来源，必须为 TheAudioDB 才执行查询
        :param media_id: TheAudioDB 专辑标识
        :return: 专辑模型；来源不符、标识缺失或专辑详情不可用时为 None，曲目请求失败时保留空曲目专辑
        """
        if not self._detail_plan(media_source, media_id, MUSIC_ENTITY_ALBUM):
            return None
        payload = self._request_json("album.php", {"m": media_id})
        album = self._project_album_header(payload)
        if not album:
            return None
        tracks_payload = self._request_json("track.php", {"m": media_id})
        return self._project_album_tracks(album, tracks_payload)

    @classmethod
    def _project_album_header(
            cls,
            payload: Optional[dict[str, Any]],
    ) -> Optional[MusicAlbumInfo]:
        """
        把专辑详情响应投影为尚未装载曲目的专辑。
        """
        item = cls._first_entity(payload, "album", "albums")
        return cls._album_to_info(item) if item else None

    @classmethod
    def _project_album_tracks(
            cls,
            album: MusicAlbumInfo,
            tracks_payload: Optional[dict[str, Any]],
    ) -> MusicAlbumInfo:
        """
        把曲目响应附加到已投影的 TheAudioDB 专辑。
        """
        album.tracks = [
            info
            for track in cls._entities(tracks_payload, "track", "tracks")
            if (info := cls._track_to_info(track, album=album))
        ]
        return album

    def music_artist(
            self,
            media_source: MediaSource,
            media_id: str,
    ) -> Optional[MusicArtistInfo]:
        """
        按艺术家标识读取详情。

        :param media_source: 请求来源，必须为 TheAudioDB 才执行查询
        :param media_id: TheAudioDB 艺术家标识
        :return: 艺术家模型；来源不符、标识缺失或无可用结果时返回 None
        """
        if media_source != self._source or not media_id:
            return None
        payload = self._request_json("artist.php", {"i": media_id})
        item = self._first_entity(payload, "artists", "artist")
        return self._artist_to_info(item) if item else None

    def music_artist_albums(
            self,
            media_source: MediaSource,
            media_id: str,
            page: int = 1,
            count: int = 30,
            album_type: Optional[str] = None,
    ) -> list[MusicInfo]:
        """
        读取艺术家的专辑，按可选类型筛选后在本地分页。

        :param media_source: 请求来源，必须为 TheAudioDB 才执行查询
        :param media_id: TheAudioDB 艺术家标识
        :param page: 从 1 开始的页码，非正值按第一页处理
        :param count: 每页数量，至少为 1
        :param album_type: 可选专辑类型，忽略大小写进行精确匹配
        :return: 统一音乐列表；来源不符、标识缺失或无结果时为空列表
        """
        if media_source != self._source or not media_id:
            return []
        payload = self._request_json("album.php", {"i": media_id})
        albums = [self._album_to_info(item) for item in self._entities(payload, "album", "albums")]
        if album_type:
            normalized_type = album_type.casefold()
            albums = [
                album for album in albums
                if (album.album_type or "").casefold() == normalized_type
            ]
        start = max(page - 1, 0) * max(1, count)
        return [album.to_music_info() for album in albums[start:start + max(1, count)]]

    def music_album_related(
            self,
            media_source: MediaSource,
            media_id: str,
            count: int = 24,
    ) -> Optional[list[MusicInfo]]:
        """
        读取专辑主艺术家的其他专辑，排除当前专辑。

        :param media_source: 请求来源，必须为 TheAudioDB 才执行查询
        :param media_id: TheAudioDB 专辑标识
        :param count: 最多返回的数量，至少为 1
        :return: 关联专辑列表；来源不符或标识缺失时为 None，无艺术家或结果时为空列表
        """
        if media_source != self._source or not media_id:
            return None
        payload = self._request_json("album.php", {"m": media_id})
        album_item = self._first_entity(payload, "album", "albums")
        artist_id = self._text((album_item or {}).get("idArtist"))
        if not artist_id:
            return []
        albums_payload = self._request_json("album.php", {"i": artist_id})
        albums = [
            self._album_to_info(item).to_music_info()
            for item in self._entities(albums_payload, "album", "albums")
            if self._text(item.get("idAlbum") or item.get("id")) != str(media_id)
        ]
        return albums[:max(1, count)]

    def clear_cache(self) -> None:
        """
        清除 TheAudioDB 请求缓存。
        """
        cast(CacheControls, self._request_json).cache_clear()

    def _search_tracks(self, meta: MetaMusic) -> list[MusicInfo]:
        """
        使用曲名和艺术家搜索 TheAudioDB 单曲。
        """
        params = self._track_search_params(meta)
        if not params:
            return []
        payload = self._request_json("searchtrack.php", params)
        candidates = self._project_track_search(payload)
        enrich_music_artist_aliases(((meta, candidate) for candidate in candidates), self._source, self._artist_aliases)
        return candidates

    async def _async_search_tracks(self, meta: MetaMusic) -> list[MusicInfo]:
        """
        异步使用曲名和艺术家搜索 TheAudioDB 单曲。
        """
        params = self._track_search_params(meta)
        if not params:
            return []
        payload = await self._async_request_json("searchtrack.php", params)
        candidates = self._project_track_search(payload)
        await async_enrich_music_artist_aliases(((meta, candidate) for candidate in candidates), self._source, self._async_artist_aliases)
        return candidates

    @staticmethod
    def _track_search_params(meta: MetaMusic) -> Optional[dict[str, str]]:
        """
        从音乐元数据归一化单曲搜索参数。
        """
        artist = next(iter(music_query_artists(meta)), None) or meta.album_artist
        if not meta.title or not artist:
            return None
        return {"t": meta.title, "s": artist}

    @classmethod
    def _project_track_search(
            cls, payload: Optional[dict[str, Any]]
    ) -> list[MusicInfo]:
        """
        把单曲搜索响应投影为统一候选列表。
        """
        return [
            info
            for item in cls._entities(payload, "track", "tracks")
            if (info := cls._track_to_info(item))
        ]

    def _search_albums(self, meta: MetaMusic) -> list[MusicAlbumInfo]:
        """
        使用专辑名和艺术家搜索 TheAudioDB 专辑。
        """
        params = self._album_search_params(meta)
        if not params:
            return []
        payload = self._request_json("searchalbum.php", params)
        candidates = self._project_album_search(payload)
        enrich_music_artist_aliases(((meta, candidate) for candidate in candidates), self._source, self._artist_aliases)
        return candidates

    async def _async_search_albums(
            self,
            meta: MetaMusic,
    ) -> list[MusicAlbumInfo]:
        """
        异步使用专辑名和艺术家搜索 TheAudioDB 专辑。
        """
        params = self._album_search_params(meta)
        if not params:
            return []
        payload = await self._async_request_json("searchalbum.php", params)
        candidates = self._project_album_search(payload)
        await async_enrich_music_artist_aliases(((meta, candidate) for candidate in candidates), self._source, self._async_artist_aliases)
        return candidates

    @staticmethod
    def _album_search_params(meta: MetaMusic) -> Optional[dict[str, str]]:
        """
        专辑查询优先使用专辑署名，不把合辑中的单曲表演者当作专辑艺人。
        """
        album_name = meta.album or meta.title
        artist = meta.album_artist or (meta.artists[0] if meta.artists else None)
        if not album_name or not artist:
            return None
        return {"a": album_name, "s": artist}

    @classmethod
    def _project_album_search(
            cls, payload: Optional[dict[str, Any]]
    ) -> list[MusicAlbumInfo]:
        """
        把专辑搜索响应投影为统一候选列表。
        """
        return [
            cls._album_to_info(item)
            for item in cls._entities(payload, "album", "albums")
        ]

    def _search_artists(self, meta: MetaMusic) -> list[MusicArtistInfo]:
        """
        使用艺术家线索搜索 TheAudioDB 艺术家。
        """
        name = meta.artists[0] if meta.artists else meta.title
        if not name:
            return []
        payload = self._request_json("search.php", {"s": name})
        return [
            self._artist_to_info(item)
            for item in self._entities(payload, "artists", "artist")
        ]

    @classmethod
    def _artist_alias_values(cls, payload: Optional[dict[str, Any]], artist_id: str) -> list[str]:
        """
        只消费精确Artist ID响应中的真实名字与别名，拒绝同名但不同身份的结果。
        """
        names = []
        for item in cls._entities(payload, "artists", "artist"):
            artist = cls._artist_to_info(item)
            if artist.media_id == artist_id:
                names.extend([artist.name, *artist.aliases])
        return cls._unique_texts(names)

    def _artist_aliases(self, artist_id: str) -> list[str]:
        """
        通过已有缓存及HTTP预算查询一个确定的艺人身份。
        """
        return self._artist_alias_values(self._request_json("artist.php", {"i": artist_id}), artist_id)

    async def _async_artist_aliases(self, artist_id: str) -> list[str]:
        """
        异步按同一身份补证，沿用与同步来源共享的缓存。
        """
        return self._artist_alias_values(await self._async_request_json("artist.php", {"i": artist_id}), artist_id)

    @staticmethod
    def _select_track(
            meta: MetaMusic,
            candidates: list[MusicInfo],
    ) -> Optional[MusicInfo]:
        """
        复用统一音乐证据确认单曲，明确 ISRC 优先于名称候选。
        """
        candidates = [candidate for candidate in candidates if not music_credit_conflicts(candidate, meta)]
        identities = [candidate for candidate in candidates if music_isrc_matches(candidate, meta)]
        if identities:
            return unique_music_match(identities)
        matches = []
        for candidate in candidates:
            if not music_title_matches(candidate, meta.title, preserve_editions=True) or not music_version_matches(candidate, meta):
                continue
            if (meta.artists or any(music_credit_values(meta).values())) and not music_artist_evidence_matches(candidate, meta):
                continue
            if not music_release_year_matches(candidate, meta):
                continue
            matches.append(candidate)
        return unique_music_match(matches)

    @staticmethod
    def _select_album(
            meta: MetaMusic,
            candidates: list[MusicAlbumInfo],
    ) -> Optional[MusicAlbumInfo]:
        """
        按专辑名、署名和版本确认；普通年份可缺省，明确发行年仍须核验。
        """
        expected_title = meta.album or meta.title
        expected_artists = [meta.album_artist] if meta.album_artist else meta.artists
        album_meta = MetaMusic(title=expected_title, version=meta.version)
        matches = []
        for candidate in candidates:
            music = candidate.to_music_info()
            if not music_title_matches(music, expected_title, preserve_editions=True) or not music_version_matches(music, album_meta):
                continue
            if (expected_artists or any(music_credit_values(meta).values())) and not music_album_artist_evidence_matches(candidate, meta):
                continue
            if not music_album_year_matches(music, meta):
                continue
            matches.append(candidate)
        return unique_music_match(matches)

    @classmethod
    def _track_to_info(
            cls,
            item: dict[str, Any],
            album: Optional[MusicAlbumInfo] = None,
    ) -> Optional[MusicInfo]:
        """
        将 TheAudioDB 单曲响应转换为标准音乐信息。
        """
        media_id = cls._text(item.get("idTrack") or item.get("id"))
        title = cls._text(item.get("strTrack") or item.get("name"))
        if not media_id or not title:
            return None
        artist = cls._text(item.get("strArtist"))
        artist_id = cls._text(item.get("idArtist"))
        duration_ms = cls._optional_int(item.get("intDuration"))
        genres = cls._unique_texts([item.get("strGenre"), item.get("strStyle")])
        return MusicInfo(
            media_source=cls._source,
            media_id=media_id,
            title=title,
            artists=[artist] if artist else list(album.artists if album else []),
            artist_ids=[artist_id] if artist_id else list(album.artist_ids if album else []),
            album=cls._text(item.get("strAlbum")) or (album.title if album else None),
            album_artist=artist or (album.artist if album else None),
            album_id=cls._text(item.get("idAlbum")) or (album.media_id if album else None),
            year=album.year if album else None,
            release_date=album.release_date if album else None,
            disc_number=cls._optional_int(item.get("intCD")),
            track_number=cls._optional_int(item.get("intTrackNumber")),
            duration=duration_ms // 1000 if duration_ms else None,
            isrc=cls._text(item.get("strISRC")),
            cover_url=cls._first_text(
                item,
                "strTrackThumb",
                "strTrack3DCase",
            ) or (album.cover_url if album else None),
            lyrics=cls._text(item.get("strTrackLyrics")),
            metadata_category=" / ".join(genres),
            genres=genres,
            names=cls._unique_texts([title, item.get("strTrackAlternate")]),
            title_aliases=cls._unique_texts([item.get("strTrackAlternate")]),
            detail_link=f"{cls._detail_url}/track/{media_id}",
            raw_data={
                "musicbrainz_id": cls._text(item.get("strMusicBrainzID")),
                "musicbrainz_album_id": cls._text(item.get("strMusicBrainzAlbumID")),
            },
        )

    @classmethod
    def _album_to_info(cls, item: dict[str, Any]) -> MusicAlbumInfo:
        """
        将 TheAudioDB 专辑响应转换为标准专辑信息。
        """
        media_id = cls._text(item.get("idAlbum") or item.get("id"))
        title = cls._text(item.get("strAlbum") or item.get("name"))
        artist = cls._text(item.get("strArtist"))
        artist_id = cls._text(item.get("idArtist"))
        genres = cls._unique_texts([item.get("strGenre"), item.get("strStyle")])
        release_date = cls._text(item.get("strReleaseDate"))
        if not release_date:
            release_date = cls._text(item.get("intYearReleased"))
        return MusicAlbumInfo(
            media_source=cls._source,
            media_id=media_id,
            title=title,
            artists=[artist] if artist else [],
            artist_ids=[artist_id] if artist_id else [],
            album_type=cls._text(item.get("strReleaseFormat")),
            release_date=release_date,
            cover_url=cls._first_text(
                item,
                "strAlbumThumbHQ",
                "strAlbumThumb",
                "strAlbum3DCase",
                "strAlbumCDart",
            ),
            genres=genres,
            tags=cls._unique_texts([item.get("strMood"), item.get("strStyle")]),
            rating=cls._optional_float(item.get("intScore")),
            rating_votes=cls._optional_int(item.get("intScoreVotes")),
            detail_link=f"{cls._detail_url}/album/{media_id}" if media_id else None,
            raw_data={"description": cls._localized_text(item, "strDescription")},
        )

    @classmethod
    def _artist_to_info(cls, item: dict[str, Any]) -> MusicArtistInfo:
        """
        将 TheAudioDB 艺术家响应转换为标准艺术家信息。
        """
        media_id = cls._text(item.get("idArtist") or item.get("id"))
        name = cls._text(item.get("strArtist") or item.get("name"))
        links = {}
        website = cls._text(item.get("strWebsite"))
        if website:
            links["official homepage"] = website
        return MusicArtistInfo(
            media_source=cls._source,
            media_id=media_id,
            name=name,
            disambiguation=cls._text(item.get("strArtistAlternate")),
            artist_type=cls._text(item.get("strStyle")),
            gender=cls._text(item.get("strGender")),
            country=cls._text(item.get("strCountry")),
            begin_date=cls._text(item.get("intFormedYear") or item.get("intBornYear")),
            end_date=cls._text(item.get("intDiedYear") or item.get("strDisbanded")),
            ended=bool(item.get("intDiedYear") or item.get("strDisbanded")),
            genres=cls._unique_texts([item.get("strGenre"), item.get("strStyle")]),
            aliases=cls._split_text(item.get("strArtistAlternate")),
            image_url=cls._first_text(
                item,
                "strArtistThumb",
                "strArtistFanart",
                "strArtistWideThumb",
                "strArtistCutout",
            ),
            detail_link=f"{cls._detail_url}/artist/{media_id}" if media_id else None,
            external_links=links,
            raw_data={
                "musicbrainz_id": cls._text(item.get("strMusicBrainzID")),
                "biography": cls._localized_text(item, "strBiography"),
            },
        )

    @staticmethod
    def _interleave_results(
            tracks: list[MusicInfo],
            albums: list[MusicAlbumInfo],
            artists: list[MusicArtistInfo],
            limit: int,
    ) -> list[MusicInfo]:
        """
        交错合并三类搜索结果，避免单一实体占满候选列表。
        """
        groups = [tracks, [item.to_music_info() for item in albums], [item.to_music_info() for item in artists]]
        results: list[MusicInfo] = []
        for index in range(max((len(group) for group in groups), default=0)):
            for group in groups:
                if index < len(group):
                    results.append(group[index])
                    if len(results) >= limit:
                        return results
        return results

    @classmethod
    def _response_payload(
            cls, response: Any, endpoint: str
    ) -> Optional[dict[str, Any]]:
        """
        统一校验并解析 TheAudioDB 响应，避免同步异步错误语义漂移。
        """
        if response.status_code != 200:
            report_music_recognition("service_error", "TheAudioDB 服务暂时不可用")
            return None
        diagnostic = cls._response_diagnostic(response, endpoint)
        if getattr(response, "content", None) in (b"", ""):
            logger.warning(f"TheAudioDB 返回空响应：{diagnostic}")
            report_music_recognition("service_error", "TheAudioDB 返回空响应")
            return None
        try:
            payload = response.json()
        except (TypeError, ValueError) as err:
            logger.warning(
                f"TheAudioDB 响应解析失败：{diagnostic}，错误：{str(err)}"
            )
            report_music_recognition("service_error", "TheAudioDB 返回了无效响应")
            return None
        if not isinstance(payload, dict) or payload.get("error") or payload.get("success") is False:
            report_music_recognition("service_error", "TheAudioDB 返回了无效响应")
            return None
        return payload

    @classmethod
    def _request_plan(
            cls,
            api_key: str,
            endpoint: str,
            params: Optional[dict[str, Any]],
    ) -> Optional[_TheAudioDbRequestPlan]:
        """
        校验 API Key 并构造同步异步共用的请求计划。
        """
        normalized_key = str(api_key or "").strip()
        if not normalized_key:
            return None
        return _TheAudioDbRequestPlan(
            url=f"{cls._base_url}/{normalized_key}/{endpoint}",
            params=dict(params or {}),
        )

    @classmethod
    @cached(maxsize=get_runtime_setting('CONF').theaudiodb, ttl=get_runtime_setting('CONF').meta, skip_none=True,
            shared_key="music_http_v2", empty_ttl=300, empty_if=lambda payload: isinstance(payload, dict) and not any(payload.values()))
    def _request_json(
            cls,
            endpoint: str,
            params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        请求 TheAudioDB V1 JSON 接口并统一处理错误响应。
        """
        plan = cls._request_plan(
            get_runtime_setting('THEAUDIODB_API_KEY'), endpoint, params
        )
        if not plan:
            logger.warning("TheAudioDB API Key 未配置，跳过请求")
            return None
        timeout = music_request_timeout(maximum=30)
        if timeout is None:
            return None
        response = RequestUtils(
            ua=get_runtime_setting('USER_AGENT'),
            proxies=get_runtime_setting('PROXY'),
            timeout=30,
        ).get_res(
            url=plan.url,
            params=plan.params,
            timeout=timeout,
        )
        if response is None:
            report_music_recognition("service_error", "TheAudioDB 连接失败")
            return None
        try:
            return cls._response_payload(response, endpoint)
        finally:
            response.close()

    @classmethod
    @cached(
        maxsize=get_runtime_setting('CONF').theaudiodb,
        ttl=get_runtime_setting('CONF').meta,
        skip_none=True,
        shared_key="music_http_v2",
        empty_ttl=300,
        empty_if=lambda payload: isinstance(payload, dict) and not any(payload.values()),
    )
    async def _async_request_json(
            cls,
            endpoint: str,
            params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        异步请求 TheAudioDB V1 JSON 接口并统一处理错误响应。
        """
        plan = cls._request_plan(
            get_runtime_setting('THEAUDIODB_API_KEY'), endpoint, params
        )
        if not plan:
            logger.warning("TheAudioDB API Key 未配置，跳过请求")
            return None
        timeout = music_request_timeout(maximum=30)
        if timeout is None:
            return None
        response = await AsyncRequestUtils(
            ua=get_runtime_setting('USER_AGENT'),
            proxies=get_runtime_setting('PROXY'),
            timeout=30,
        ).get_res(
            url=plan.url,
            params=plan.params,
            timeout=timeout,
        )
        if response is None:
            report_music_recognition("service_error", "TheAudioDB 连接失败")
            return None
        try:
            return cls._response_payload(response, endpoint)
        finally:
            await response.aclose()

    @staticmethod
    def _response_diagnostic(response: Any, endpoint: str) -> str:
        """
        生成不包含 API Key 的 TheAudioDB 响应诊断摘要。
        """
        headers = getattr(response, "headers", {}) or {}
        content_type = headers.get("Content-Type", "") if hasattr(headers, "get") else ""
        body = str(getattr(response, "text", "") or "").replace("\n", " ")[:200]
        return (
            f"endpoint={endpoint}, HTTP={getattr(response, 'status_code', '')}, "
            f"Content-Type={content_type}, body={body!r}"
        )

    @staticmethod
    def _entities(
            payload: Optional[dict[str, Any]],
            *keys: str,
    ) -> list[dict[str, Any]]:
        """
        从兼容 V1/V2 命名的响应字段中提取实体列表。
        """
        if not payload:
            return []
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return [value]
        return []

    @classmethod
    def _first_entity(
            cls,
            payload: Optional[dict[str, Any]],
            *keys: str,
    ) -> Optional[dict[str, Any]]:
        """
        返回响应中的首个实体。
        """
        entities = cls._entities(payload, *keys)
        return entities[0] if entities else None

    @staticmethod
    def _text(value: Any) -> Optional[str]:
        """
        把外部响应值转换为去空白文本。
        """
        text = str(value).strip() if value is not None else ""
        return text or None

    @classmethod
    def _first_text(cls, item: dict[str, Any], *keys: str) -> Optional[str]:
        """
        按优先级返回外部响应中的首个非空文本。
        """
        return next((text for key in keys if (text := cls._text(item.get(key)))), None)

    @classmethod
    def _localized_text(cls, item: dict[str, Any], prefix: str) -> Optional[str]:
        """
        优先返回中文说明，不存在时回退到英文说明。
        """
        return cls._first_text(item, f"{prefix}CN", f"{prefix}EN")

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        """
        将外部响应值安全转换为整数。
        """
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_float(value: Any) -> float:
        """
        将外部评分安全转换为浮点数。
        """
        try:
            return float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _split_text(cls, value: Any) -> list[str]:
        """
        把分号或斜线分隔的外部文本转换为去重列表。
        """
        text = cls._text(value)
        if not text:
            return []
        return cls._unique_texts(text.replace("/", ";").split(";"))

    @classmethod
    def _unique_texts(cls, values: list[Any]) -> list[str]:
        """
        过滤空值并按大小写无关方式去重。
        """
        results = []
        seen = set()
        for value in values:
            text = cls._text(value)
            identity = text.casefold() if text else ""
            if not text or identity in seen:
                continue
            seen.add(identity)
            results.append(text)
        return results
