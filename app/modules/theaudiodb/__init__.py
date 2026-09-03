from dataclasses import dataclass
from typing import Any, Optional, Tuple, Union

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.domain.context import (
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
)
from app.domain.media import is_media_source_selected
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.modules import _ModuleBase
from app.runtime.cache import cached
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
)


@dataclass(frozen=True, slots=True)
class _TheAudioDbRecognitionPlan:
    """描述 TheAudioDB 识别允许的详情或候选检索步骤。"""

    meta: Optional[MetaMusic]
    media_id: Optional[str]
    music_type: Optional[str]

    @property
    def search_recording(self) -> bool:
        """是否需要尝试单曲详情或候选。"""
        return bool(self.music_type != MUSIC_ENTITY_ALBUM)

    @property
    def search_album(self) -> bool:
        """是否允许在单曲未命中后继续尝试专辑。"""
        return bool(self.music_type != MUSIC_ENTITY_RECORDING)

    def require_meta(self) -> MetaMusic:
        """返回候选识别计划必有的音乐元数据。"""
        if self.meta is None:
            raise RuntimeError("TheAudioDB 候选识别计划缺少音乐元数据")
        return self.meta

    def require_media_id(self) -> str:
        """返回详情识别计划必有的原生 ID。"""
        if self.media_id is None:
            raise RuntimeError("TheAudioDB 详情识别计划缺少原生 ID")
        return self.media_id


@dataclass(frozen=True, slots=True)
class _TheAudioDbRequestPlan:
    """冻结 TheAudioDB 请求地址和参数，供同步异步传输共用。"""

    url: str
    params: dict[str, Any]


class TheAudioDbModule(_ModuleBase):
    """通过 TheAudioDB V1 API 提供音乐搜索、详情和手动识别能力。"""

    _source = MediaSource.TheAudioDB
    _base_url = "https://www.theaudiodb.com/api/v1/json"
    _detail_url = "https://www.theaudiodb.com"

    def init_module(self) -> None:
        """初始化无状态的 TheAudioDB 模块。"""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """TheAudioDB 使用环境配置中的 API Key，无独立启用开关。"""
        return None

    def stop(self) -> None:
        """停止模块；当前实现没有需要释放的持久资源。"""

    def test(self) -> Tuple[bool, str]:
        """测试 TheAudioDB 艺术家搜索接口连通性。"""
        result = self._request_json("search.php", {"s": "coldplay"})
        return (True, "") if result is not None else (False, "TheAudioDB 网络连接失败")

    @staticmethod
    def get_name() -> str:
        """返回模块展示名称。"""
        return "TheAudioDB"

    @staticmethod
    def get_music_source() -> MediaSource:
        """返回音乐识别使用的数据源标识。"""
        return TheAudioDbModule._source

    @staticmethod
    def get_type() -> ModuleType:
        """返回模块所属的媒体识别类型。"""
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """返回 TheAudioDB 模块子类型。"""
        return MediaRecognizeType.TheAudioDB

    @staticmethod
    def get_priority() -> int:
        """返回音乐识别优先级，位于默认 MusicBrainz 之后。"""
        return 1

    def search_music(
            self,
            meta: MetaMusic,
            limit: int = 20,
            media_source: Optional[MediaSourceSelection] = None,
    ) -> Optional[list[MusicInfo]]:
        """按请求来源搜索 TheAudioDB 单曲、专辑和艺术家。"""
        if not is_media_source_selected(media_source, self._source):
            return None
        normalized_limit = max(1, min(limit, 100))
        tracks = self._search_tracks(meta)
        albums = self._search_albums(meta)
        artists = self._search_artists(meta)
        return self._interleave_results(
            tracks,
            albums,
            artists,
            limit=normalized_limit,
        )

    def recognize_media(
            self,
            meta: MetaBase = None,
            mtype: MediaType = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            **kwargs,
    ) -> Optional[MusicInfo]:
        """仅响应显式 TheAudioDB 音乐请求，并返回带原生 ID 的标准音乐信息。"""
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
            meta: MetaBase = None,
            mtype: MediaType = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            **kwargs,
    ) -> Optional[MusicInfo]:
        """异步识别 TheAudioDB 音乐详情或按元数据匹配单曲。"""
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
        """统一完成来源、音乐类型和原生 ID 准入。"""
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
        """统一决定单曲未命中后是否继续查询专辑。"""
        return bool(not matched and plan.search_album)

    def recognize_music(
            self,
            media_source: MediaSource,
            media_id: str,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """按 TheAudioDB 原生 ID 和实体类型获取详情；空类型保留旧版探测顺序。"""
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
        """异步按 TheAudioDB 原生 ID 和实体类型获取详情。"""
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
        """统一校验详情来源并冻结实体探测顺序。"""
        if media_source != cls._source or not media_id:
            return None
        return _TheAudioDbRecognitionPlan(
            meta=None, media_id=str(media_id), music_type=music_type
        )

    @classmethod
    def _project_track_detail(
            cls, payload: Optional[dict[str, Any]]
    ) -> Optional[MusicInfo]:
        """把单曲详情响应投影为统一音乐信息。"""
        track = cls._first_entity(payload, "track", "tracks")
        return cls._track_to_info(track) if track else None

    @staticmethod
    def _project_album_result(
            album: Optional[MusicAlbumInfo],
    ) -> Optional[MusicInfo]:
        """把专辑候选统一投影到音乐识别返回类型。"""
        return album.to_music_info() if album else None

    async def _async_music_album(
            self,
            media_source: MediaSource,
            media_id: str,
    ) -> Optional[MusicAlbumInfo]:
        """异步按 TheAudioDB 专辑 ID 获取标准化专辑详情和曲目。"""
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
        """按 TheAudioDB 专辑 ID 获取标准化专辑详情和曲目。"""
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
        """把专辑详情响应投影为尚未装载曲目的专辑。"""
        item = cls._first_entity(payload, "album", "albums")
        return cls._album_to_info(item) if item else None

    @classmethod
    def _project_album_tracks(
            cls,
            album: MusicAlbumInfo,
            tracks_payload: Optional[dict[str, Any]],
    ) -> MusicAlbumInfo:
        """把曲目响应附加到已投影的 TheAudioDB 专辑。"""
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
        """按 TheAudioDB 艺术家 ID 获取标准化艺术家详情。"""
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
        """按 TheAudioDB 艺术家 ID 分页返回专辑列表。"""
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
        """按专辑主艺术家返回 TheAudioDB 同艺人专辑，供详情页关联浏览。"""
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
        """清除 TheAudioDB 请求缓存。"""
        self._request_json.cache_clear()

    def _search_tracks(self, meta: MetaMusic) -> list[MusicInfo]:
        """使用曲名和艺术家搜索 TheAudioDB 单曲。"""
        params = self._track_search_params(meta)
        if not params:
            return []
        payload = self._request_json("searchtrack.php", params)
        return self._project_track_search(payload)

    async def _async_search_tracks(self, meta: MetaMusic) -> list[MusicInfo]:
        """异步使用曲名和艺术家搜索 TheAudioDB 单曲。"""
        params = self._track_search_params(meta)
        if not params:
            return []
        payload = await self._async_request_json("searchtrack.php", params)
        return self._project_track_search(payload)

    @staticmethod
    def _track_search_params(meta: MetaMusic) -> Optional[dict[str, str]]:
        """从音乐元数据归一化单曲搜索参数。"""
        artist = meta.artists[0] if meta.artists else meta.album_artist
        if not meta.title or not artist:
            return None
        return {"t": meta.title, "s": artist}

    @classmethod
    def _project_track_search(
            cls, payload: Optional[dict[str, Any]]
    ) -> list[MusicInfo]:
        """把单曲搜索响应投影为统一候选列表。"""
        return [
            info
            for item in cls._entities(payload, "track", "tracks")
            if (info := cls._track_to_info(item))
        ]

    def _search_albums(self, meta: MetaMusic) -> list[MusicAlbumInfo]:
        """使用专辑名和艺术家搜索 TheAudioDB 专辑。"""
        params = self._album_search_params(meta)
        if not params:
            return []
        payload = self._request_json("searchalbum.php", params)
        return self._project_album_search(payload)

    async def _async_search_albums(
            self,
            meta: MetaMusic,
    ) -> list[MusicAlbumInfo]:
        """异步使用专辑名和艺术家搜索 TheAudioDB 专辑。"""
        params = self._album_search_params(meta)
        if not params:
            return []
        payload = await self._async_request_json("searchalbum.php", params)
        return self._project_album_search(payload)

    @staticmethod
    def _album_search_params(meta: MetaMusic) -> Optional[dict[str, str]]:
        """从音乐元数据归一化专辑搜索参数。"""
        album_name = meta.album or meta.title
        artist = meta.artists[0] if meta.artists else meta.album_artist
        if not album_name or not artist:
            return None
        return {"a": album_name, "s": artist}

    @classmethod
    def _project_album_search(
            cls, payload: Optional[dict[str, Any]]
    ) -> list[MusicAlbumInfo]:
        """把专辑搜索响应投影为统一候选列表。"""
        return [
            cls._album_to_info(item)
            for item in cls._entities(payload, "album", "albums")
        ]

    def _search_artists(self, meta: MetaMusic) -> list[MusicArtistInfo]:
        """使用艺术家线索搜索 TheAudioDB 艺术家。"""
        name = meta.artists[0] if meta.artists else meta.title
        if not name:
            return []
        payload = self._request_json("search.php", {"s": name})
        return [
            self._artist_to_info(item)
            for item in self._entities(payload, "artists", "artist")
        ]

    @classmethod
    def _select_track(
            cls,
            meta: MetaMusic,
            candidates: list[MusicInfo],
    ) -> Optional[MusicInfo]:
        """按曲名和可用艺术家线索选择可信单曲候选。"""
        for candidate in candidates:
            if not cls._same_text(meta.title, candidate.title):
                continue
            if meta.artists and not any(
                cls._same_text(expected, actual)
                for expected in meta.artists
                for actual in candidate.artists
            ):
                continue
            return candidate
        return None

    @classmethod
    def _select_album(
            cls,
            meta: MetaMusic,
            candidates: list[MusicAlbumInfo],
    ) -> Optional[MusicAlbumInfo]:
        """按专辑名和可用艺术家线索选择可信专辑候选。"""
        expected_title = meta.album or meta.title
        for candidate in candidates:
            if not cls._same_text(expected_title, candidate.title):
                continue
            if meta.artists and not any(
                cls._same_text(expected, actual)
                for expected in meta.artists
                for actual in candidate.artists
            ):
                continue
            return candidate
        return None

    @classmethod
    def _track_to_info(
            cls,
            item: dict[str, Any],
            album: Optional[MusicAlbumInfo] = None,
    ) -> Optional[MusicInfo]:
        """将 TheAudioDB 单曲响应转换为标准音乐信息。"""
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
            detail_link=f"{cls._detail_url}/track/{media_id}",
            raw_data={
                "musicbrainz_id": cls._text(item.get("strMusicBrainzID")),
                "musicbrainz_album_id": cls._text(item.get("strMusicBrainzAlbumID")),
            },
        )

    @classmethod
    def _album_to_info(cls, item: dict[str, Any]) -> MusicAlbumInfo:
        """将 TheAudioDB 专辑响应转换为标准专辑信息。"""
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
        """将 TheAudioDB 艺术家响应转换为标准艺术家信息。"""
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
        """交错合并三类搜索结果，避免单一实体占满候选列表。"""
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
        """统一校验并解析 TheAudioDB 响应，避免同步异步错误语义漂移。"""
        if response.status_code != 200:
            return None
        diagnostic = cls._response_diagnostic(response, endpoint)
        if getattr(response, "content", None) in (b"", ""):
            logger.warning(f"TheAudioDB 返回空响应：{diagnostic}")
            return None
        try:
            payload = response.json()
        except (TypeError, ValueError) as err:
            logger.warning(
                f"TheAudioDB 响应解析失败：{diagnostic}，错误：{str(err)}"
            )
            return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def _request_plan(
            cls,
            api_key: str,
            endpoint: str,
            params: Optional[dict[str, Any]],
    ) -> Optional[_TheAudioDbRequestPlan]:
        """校验 API Key 并构造同步异步共用的请求计划。"""
        normalized_key = str(api_key or "").strip()
        if not normalized_key:
            return None
        return _TheAudioDbRequestPlan(
            url=f"{cls._base_url}/{normalized_key}/{endpoint}",
            params=dict(params or {}),
        )

    @classmethod
    @cached(maxsize=get_runtime_setting('CONF').theaudiodb, ttl=get_runtime_setting('CONF').meta, skip_none=True)
    def _request_json(
            cls,
            endpoint: str,
            params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """请求 TheAudioDB V1 JSON 接口并统一处理错误响应。"""
        plan = cls._request_plan(
            get_runtime_setting('THEAUDIODB_API_KEY'), endpoint, params
        )
        if not plan:
            logger.warning("TheAudioDB API Key 未配置，跳过请求")
            return None
        response = RequestUtils(
            ua=get_runtime_setting('USER_AGENT'),
            proxies=get_runtime_setting('PROXY'),
            timeout=30,
        ).get_res(
            url=plan.url,
            params=plan.params,
        )
        if response is None:
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
        shared_key="_request_json",
    )
    async def _async_request_json(
            cls,
            endpoint: str,
            params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """异步请求 TheAudioDB V1 JSON 接口并统一处理错误响应。"""
        plan = cls._request_plan(
            get_runtime_setting('THEAUDIODB_API_KEY'), endpoint, params
        )
        if not plan:
            logger.warning("TheAudioDB API Key 未配置，跳过请求")
            return None
        response = await AsyncRequestUtils(
            ua=get_runtime_setting('USER_AGENT'),
            proxies=get_runtime_setting('PROXY'),
            timeout=30,
        ).get_res(
            url=plan.url,
            params=plan.params,
        )
        if response is None:
            return None
        try:
            return cls._response_payload(response, endpoint)
        finally:
            await response.aclose()

    @staticmethod
    def _response_diagnostic(response: Any, endpoint: str) -> str:
        """生成不包含 API Key 的 TheAudioDB 响应诊断摘要。"""
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
        """从兼容 V1/V2 命名的响应字段中提取实体列表。"""
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
        """返回响应中的首个实体。"""
        entities = cls._entities(payload, *keys)
        return entities[0] if entities else None

    @staticmethod
    def _text(value: Any) -> Optional[str]:
        """把外部响应值转换为去空白文本。"""
        text = str(value).strip() if value is not None else ""
        return text or None

    @classmethod
    def _first_text(cls, item: dict[str, Any], *keys: str) -> Optional[str]:
        """按优先级返回外部响应中的首个非空文本。"""
        return next((text for key in keys if (text := cls._text(item.get(key)))), None)

    @classmethod
    def _localized_text(cls, item: dict[str, Any], prefix: str) -> Optional[str]:
        """优先返回中文说明，不存在时回退到英文说明。"""
        return cls._first_text(item, f"{prefix}CN", f"{prefix}EN")

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        """将外部响应值安全转换为整数。"""
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_float(value: Any) -> float:
        """将外部评分安全转换为浮点数。"""
        try:
            return float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _split_text(cls, value: Any) -> list[str]:
        """把分号或斜线分隔的外部文本转换为去重列表。"""
        text = cls._text(value)
        if not text:
            return []
        return cls._unique_texts(text.replace("/", ";").split(";"))

    @classmethod
    def _unique_texts(cls, values: list[Any]) -> list[str]:
        """过滤空值并按大小写无关方式去重。"""
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

    @staticmethod
    def _same_text(left: Optional[str], right: Optional[str]) -> bool:
        """使用音乐元数据紧凑文本规则比较标题和艺术家。"""
        return bool(left and right and MetaMusic.compact_text(left) == MetaMusic.compact_text(right))
