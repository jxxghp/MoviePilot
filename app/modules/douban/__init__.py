import re
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, Union, cast

import cn2an

from app.adapters.network.http import RequestUtils
from app.domain.context import (
    MediaInfo,
    MusicAlbumInfo,
    MusicInfo,
)
from app.domain.media import is_media_source_enabled, is_media_source_selected
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.foundation.text import convert as zhconv_convert
from app.modules import _ModuleBase
from app.modules._base.media import MediaAuxiliaryProviderMixin
from app.modules.douban.apiv2 import DoubanApi
from app.modules.douban.scraper import DoubanScraper
from app.runtime.execution import retry
from app.runtime.log import logger
from app.runtime.rate import rate_limit_exponential
from app.runtime.settings import get_runtime_setting
from app.schemas.context import MediaPerson
from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.exception import APIRateLimitException
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
class _DoubanMusicRecognitionPlan:
    """描述豆瓣音乐识别的显式身份或候选搜索范围。"""

    meta: Optional[MetaMusic]
    media_id: Optional[str]
    album_id: Optional[str]
    track_id: Optional[str]
    music_type: Optional[str]
    keyword: Optional[str]

    def require_meta(self) -> MetaMusic:
        """返回候选搜索计划必有的音乐元数据。"""
        if self.meta is None:
            raise RuntimeError("豆瓣音乐候选识别计划缺少音乐元数据")
        return self.meta

    def require_album_id(self) -> str:
        """返回详情识别计划必有的专辑 ID。"""
        if self.album_id is None:
            raise RuntimeError("豆瓣音乐详情识别计划缺少专辑 ID")
        return self.album_id

    def require_keyword(self) -> str:
        """返回候选识别计划必有的搜索词。"""
        if self.keyword is None:
            raise RuntimeError("豆瓣音乐候选识别计划缺少搜索词")
        return self.keyword


@dataclass(frozen=True, slots=True)
class _DoubanVideoRecognitionPlan:
    """描述豆瓣影视识别的原生 ID 或有序标题候选。"""

    meta: Optional[MetaBase]
    media_type: Optional[MediaType]
    douban_id: Optional[str]
    search_names: tuple[str, ...]

    def require_meta(self) -> MetaBase:
        """返回标题识别计划必有的影视元数据。"""
        if self.meta is None:
            raise RuntimeError("豆瓣影视标题识别计划缺少元数据")
        return self.meta


@dataclass(frozen=True, slots=True)
class _DoubanSearchPlan:
    """保存豆瓣搜索来源准入结果和有效查询文本。"""

    enabled: bool
    query: Optional[str]


class DoubanModule(MediaAuxiliaryProviderMixin, _ModuleBase):
    """提供豆瓣影视与豆瓣音乐元数据识别能力。"""

    auxiliary_media_source = MediaSource.Douban
    _music_source = MediaSource.DoubanMusic
    doubanapi: DoubanApi = None
    scraper: DoubanScraper = None

    def init_module(self) -> None:
        self.doubanapi = DoubanApi()
        self.scraper = DoubanScraper()

    def stop(self):
        self.doubanapi.close()

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        ret = RequestUtils().get_res("https://movie.douban.com/")
        if ret is None:
            return False, "豆瓣网络连接失败"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def get_name() -> str:
        return "豆瓣"

    @staticmethod
    def get_music_source() -> MediaSource:
        """返回音乐识别使用的数据源标识。"""
        return DoubanModule._music_source

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """
        获取模块子类型
        """
        return MediaRecognizeType.Douban

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 2

    def search_music(
            self,
            meta: MetaMusic,
            limit: int = 20,
            media_source: Optional[MediaSourceSelection] = None,
    ) -> Optional[List[MusicInfo]]:
        """按请求来源搜索豆瓣音乐专辑，并转换为统一音乐候选。"""
        if not is_media_source_selected(media_source, self._music_source):
            return None
        keyword = meta.album or meta.title
        if not keyword:
            return []
        result = self.doubanapi.music_search(keyword=keyword, count=max(1, min(limit, 100)))
        return self._build_music_search_results(result)

    def recognize_music(
            self,
            media_source: MediaSource,
            media_id: str,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """按豆瓣音乐原生 ID 和实体类型获取专辑或专辑内曲目详情。"""
        if media_source != self._music_source or not media_id:
            return None
        album_id, separator, track_id = str(media_id).partition(":")
        if music_type == MUSIC_ENTITY_RECORDING and not separator:
            return None
        if music_type == MUSIC_ENTITY_ALBUM and separator:
            return None
        album = self.music_album(media_source, album_id)
        if not album:
            return None
        if separator and track_id:
            return next(
                (
                    track for track in album.tracks
                    if track.media_id == media_id or str(track.track_number or "") == track_id
                ),
                None,
            )
        return album.to_music_info()

    def music_album(
            self,
            media_source: MediaSource,
            media_id: str,
    ) -> Optional[MusicAlbumInfo]:
        """按豆瓣音乐专辑 ID 获取标准化专辑详情和曲目。"""
        if media_source != self._music_source or not media_id:
            return None
        info = self.doubanapi.music_detail(subject_id=str(media_id))
        return self._douban_music_to_album(info) if info else None

    def music_discover(
            self,
            media_source: MediaSource,
            page: int = 1,
            count: int = 30,
            entity: str = MUSIC_ENTITY_ALBUM,
            mode: str = "chart",
            tags: str = "",
            sort: str = "U",
    ) -> Optional[List[MusicInfo]]:
        """按官方新碟榜或标签交集浏览豆瓣音乐，并保留豆瓣条目原生身份。"""
        if media_source != self._music_source:
            return None
        del entity
        if mode == "chart":
            chart_items = self._build_music_search_results(self.doubanapi.music_chart())
            start = max(page - 1, 0) * max(1, count)
            return chart_items[start:start + max(1, count)]
        selected_tags = [tag.strip() for tag in str(tags or "").split(",") if tag.strip()]
        if not selected_tags:
            selected_tags = ["流行"]
        if len(selected_tags) == 1:
            result = self.doubanapi.music_tag(
                tag=selected_tags[0],
                start=max(page - 1, 0) * max(1, count),
                count=max(1, count),
                sort=sort,
            )
            return self._build_music_search_results(result)

        # 豆瓣官网的多标签 URL 会按完整文本标签匹配；分别读取后按原生 ID
        # 求交集，才能实现风格与地区的真实组合筛选。
        scan_count = min(max(page * count * 4, 100), 300)
        tag_results = [
            self._build_music_search_results(
                self.doubanapi.music_tag(tag=tag, start=0, count=scan_count, sort=sort)
            )
            for tag in selected_tags
        ]
        if not tag_results:
            return []
        shared_ids = set(item.media_id for item in tag_results[0])
        for items in tag_results[1:]:
            shared_ids.intersection_update(item.media_id for item in items)
        matched = [item for item in tag_results[0] if item.media_id in shared_ids]
        start = max(page - 1, 0) * max(1, count)
        return matched[start:start + max(1, count)]

    def music_album_related(
            self,
            media_source: MediaSource,
            media_id: str,
            count: int = 24,
    ) -> Optional[List[MusicInfo]]:
        """按豆瓣音乐专辑 ID 返回相关推荐条目。"""
        if media_source != self._music_source or not media_id:
            return None
        result = self.doubanapi.music_recommendations(
            subject_id=str(media_id),
            start=0,
            count=max(1, count),
        )
        return self._build_music_search_results(result)

    def _recognize_music_media(
            self,
            meta: Optional[MetaMusic],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """执行豆瓣音乐详情识别或按专辑名称匹配。"""
        plan = self._music_recognition_plan(
            meta, media_source, media_id, music_type
        )
        if not plan:
            return None
        if plan.media_id:
            info = self.doubanapi.music_detail(
                subject_id=plan.require_album_id()
            )
            return self._project_music_detail(plan, info)
        meta = plan.require_meta()
        result = self.doubanapi.music_search(
            keyword=plan.require_keyword(), count=20
        )
        candidates = self._matching_music_candidates(
            meta, self._build_music_search_results(result)
        )
        for candidate in candidates:
            direct_result = self._direct_music_candidate(plan, candidate)
            if direct_result is not None:
                return direct_result
            if meta.album and meta.title:
                info = self.doubanapi.music_detail(subject_id=str(candidate.media_id))
                album = self._douban_music_to_album(info) if info else None
                matched_track = self._select_douban_music_track(meta, album)
                if matched_track:
                    return matched_track
        return None

    async def _async_recognize_music_media(
            self,
            meta: Optional[MetaMusic],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """异步执行豆瓣音乐详情识别或按专辑名称匹配。"""
        plan = self._music_recognition_plan(
            meta, media_source, media_id, music_type
        )
        if not plan:
            return None
        if plan.media_id:
            info = await self.doubanapi.async_music_detail(
                subject_id=plan.require_album_id()
            )
            return self._project_music_detail(plan, info)
        meta = plan.require_meta()
        result = await self.doubanapi.async_music_search(
            keyword=plan.require_keyword(), count=20
        )
        candidates = self._matching_music_candidates(
            meta, self._build_music_search_results(result)
        )
        for candidate in candidates:
            direct_result = self._direct_music_candidate(plan, candidate)
            if direct_result is not None:
                return direct_result
            if meta.album and meta.title:
                info = await self.doubanapi.async_music_detail(
                    subject_id=str(candidate.media_id)
                )
                album = self._douban_music_to_album(info) if info else None
                matched_track = self._select_douban_music_track(meta, album)
                if matched_track:
                    return matched_track
        return None

    @classmethod
    def _music_recognition_plan(
            cls,
            meta: Optional[MetaMusic],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            music_type: Optional[str],
    ) -> Optional[_DoubanMusicRecognitionPlan]:
        """统一完成豆瓣音乐来源、实体类型、ID 和搜索词准入。"""
        if media_source != cls._music_source:
            return None
        resolved_media_id = media_id or (meta.media_id if meta else None)
        if resolved_media_id:
            normalized_id = str(resolved_media_id)
            album_id, separator, track_id = normalized_id.partition(":")
            if music_type == MUSIC_ENTITY_RECORDING and not separator:
                return None
            if music_type == MUSIC_ENTITY_ALBUM and separator:
                return None
            return _DoubanMusicRecognitionPlan(
                meta=meta,
                media_id=normalized_id,
                album_id=album_id,
                track_id=track_id if separator else None,
                music_type=music_type,
                keyword=None,
            )
        keyword = (meta.album or meta.title) if meta else None
        if not meta or not keyword:
            return None
        return _DoubanMusicRecognitionPlan(
            meta=meta,
            media_id=None,
            album_id=None,
            track_id=None,
            music_type=music_type,
            keyword=keyword,
        )

    @classmethod
    def _project_music_detail(
            cls,
            plan: _DoubanMusicRecognitionPlan,
            info: Optional[dict[str, Any]],
    ) -> Optional[MusicInfo]:
        """把豆瓣音乐详情按计划投影为专辑或指定曲目。"""
        album = cls._douban_music_to_album(info) if info else None
        if not album:
            return None
        if plan.track_id:
            return next(
                (
                    track
                    for track in album.tracks
                    if track.media_id == plan.media_id
                    or str(track.track_number or "") == plan.track_id
                ),
                None,
            )
        return album.to_music_info()

    @classmethod
    def _matching_music_candidates(
            cls,
            meta: MetaMusic,
            candidates: List[MusicInfo],
    ) -> List[MusicInfo]:
        """按专辑标题和可用艺术家线索过滤豆瓣音乐候选。"""
        expected_title = meta.album or meta.title
        return [
            candidate
            for candidate in candidates
            if cls._same_music_text(expected_title, candidate.title)
            and (
                not meta.artists
                or not candidate.artists
                or any(
                    cls._same_music_text(expected, actual)
                    for expected in meta.artists
                    for actual in candidate.artists
                )
            )
        ]

    @staticmethod
    def _direct_music_candidate(
            plan: _DoubanMusicRecognitionPlan,
            candidate: MusicInfo,
    ) -> Optional[MusicInfo]:
        """决定候选可直接返回，还是必须继续读取专辑曲目。"""
        meta = plan.require_meta()
        if plan.music_type == MUSIC_ENTITY_ALBUM:
            return candidate
        if meta.album and meta.title:
            return None
        if plan.music_type == MUSIC_ENTITY_RECORDING:
            return None
        return candidate

    @classmethod
    def _select_douban_music_track(
            cls,
            meta: MetaMusic,
            album: Optional[MusicAlbumInfo],
    ) -> Optional[MusicInfo]:
        """从豆瓣专辑曲目中选择与本地曲名、艺术家及曲序最一致的音轨。"""
        if not album:
            return None
        candidates = [
            track for track in album.tracks
            if cls._same_music_text(meta.title, track.title)
        ]
        if meta.artists:
            candidates = [
                track for track in candidates
                if any(
                    cls._same_music_text(expected, actual)
                    for expected in meta.artists
                    for actual in track.artists
                )
            ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda track: (
                bool(meta.track_number and track.track_number == meta.track_number),
                -abs((meta.duration or track.duration or 0) - (track.duration or meta.duration or 0)),
            ),
            reverse=True,
        )
        return candidates[0]

    @classmethod
    def _build_music_search_results(
            cls,
            result: Optional[dict | list],
    ) -> List[MusicInfo]:
        """把豆瓣音乐搜索响应转换为专辑候选列表。"""
        payload = result or {}
        if isinstance(payload, list):
            items = payload
        else:
            items = (
                payload.get("subject_collection_items")
                or payload.get("recommendations")
                or payload.get("subjects")
                or payload.get("items")
                or payload.get("musics")
                or []
            )
        candidates = []
        for item in items:
            if not isinstance(item, dict):
                continue
            target_type = str(item.get("target_type") or "").casefold()
            if isinstance(item.get("target"), dict):
                target = item["target"]
            elif isinstance(item.get("subject"), dict):
                target = item["subject"]
            else:
                target = item
            type_name = str(target.get("type_name") or target.get("subtype") or "")
            target_subject_type = str(target.get("type") or "").casefold()
            if target_type and target_type not in {"music", "音乐", "subject"}:
                continue
            if target_subject_type and target_subject_type not in {"music", "音乐"}:
                continue
            if type_name and type_name not in {"音乐", "music"}:
                continue
            media_id = cls._douban_music_text(
                target.get("id") or item.get("target_id") or item.get("id")
            )
            title = cls._douban_music_text(target.get("title") or target.get("name"))
            if not media_id or not title:
                continue
            artists = cls._douban_music_search_artists(target)
            release_date = cls._douban_music_date(target)
            cover_url = cls._douban_music_cover(target)
            candidate = MusicInfo(
                media_source=cls._music_source,
                media_id=media_id,
                music_type=MUSIC_ENTITY_ALBUM,
                title=title,
                artists=artists,
                album=title,
                album_artist=" / ".join(artists) or None,
                album_id=media_id,
                year=cls._douban_music_year(target.get("year") or release_date),
                release_date=release_date,
                cover_url=cover_url,
                names=[title],
                detail_link=f"https://music.douban.com/subject/{media_id}/",
                raw_data={
                    "rating": cls._douban_music_float(target["rating"].get("value")),
                    "rating_votes": cls._douban_music_int(target["rating"].get("count")),
                } if isinstance(target.get("rating"), dict) else {},
            )
            candidates.append(candidate)
        return candidates

    @classmethod
    def _douban_music_to_album(cls, info: dict[str, Any]) -> Optional[MusicAlbumInfo]:
        """把豆瓣音乐详情转换为标准专辑信息和曲目。"""
        media_id = cls._douban_music_text(info.get("id") or info.get("subject_id"))
        title = cls._douban_music_text(info.get("title") or info.get("name"))
        if not media_id or not title:
            return None
        attrs = cast(
            dict[str, Any],
            info.get("attrs") if isinstance(info.get("attrs"), dict) else {},
        )
        artists = cls._douban_music_artists(info)
        release_date = cls._douban_music_date(info)
        tags = [
            cls._douban_music_text(item.get("name") if isinstance(item, dict) else item)
            for item in (info.get("tags") or [])
        ]
        genres = [str(item) for item in info.get("genres") or [] if item]
        rating = cast(
            dict[str, Any],
            info.get("rating") if isinstance(info.get("rating"), dict) else {},
        )
        album = MusicAlbumInfo(
            media_source=cls._music_source,
            media_id=media_id,
            title=title,
            artists=artists,
            album_type=cls._douban_music_first(
                info.get("media") or attrs.get("media")
            ) or "Album",
            release_date=release_date,
            cover_url=cls._douban_music_cover(info),
            genres=genres,
            tags=[item for item in tags if item],
            rating=cls._douban_music_float(rating.get("value") or rating.get("average")),
            rating_votes=cls._douban_music_int(
                rating.get("count") or rating.get("numRaters") or info.get("ratings_count")
            ),
            detail_link=f"https://music.douban.com/subject/{media_id}/",
            raw_data={
                "overview": cls._douban_music_text(info.get("intro") or info.get("summary")),
                "publisher": cls._douban_music_first(
                    info.get("publisher") or attrs.get("publisher")
                ),
            },
        )
        album.tracks = cls._douban_music_tracks(info, album)
        return album

    @classmethod
    def _douban_music_tracks(
            cls,
            info: dict[str, Any],
            album: MusicAlbumInfo,
    ) -> List[MusicInfo]:
        """从豆瓣新旧响应结构中提取专辑曲目。"""
        attrs = cast(
            dict[str, Any],
            info.get("attrs") if isinstance(info.get("attrs"), dict) else {},
        )
        # Frodo 当前音乐详情使用 songs；tracks/attrs.tracks 兼容旧接口响应。
        tracks = info.get("songs") or info.get("tracks") or attrs.get("tracks") or []
        if isinstance(tracks, str):
            tracks = tracks.splitlines()
        elif not isinstance(tracks, list):
            tracks = []
        elif len(tracks) == 1 and isinstance(tracks[0], str) and "\n" in tracks[0]:
            tracks = tracks[0].splitlines()
        results = []
        for index, item in enumerate(tracks, start=1):
            if isinstance(item, dict):
                title = cls._douban_music_text(item.get("title") or item.get("name"))
                track_number = cls._douban_music_int(item.get("track_number") or item.get("position")) or index
                duration = cls._douban_music_int(item.get("duration"))
                duration = duration if duration and duration > 0 else None
                disc_number = cls._douban_music_int(
                    item.get("disc_number") or item.get("disc")
                )
                artists = cls._douban_music_artists(item) or list(album.artists)
                cover_url = cls._douban_music_text(item.get("cover_url")) or album.cover_url
                raw_data = {
                    key: value
                    for key, value in {
                        "apple_album_id": item.get("apple_album_id"),
                        "apple_track_id": item.get("apple_track_id"),
                        "preview_url": item.get("preview_url"),
                    }.items()
                    if value not in (None, "")
                }
            else:
                title = cls._clean_douban_track_title(item)
                track_number = index
                duration = None
                disc_number = None
                artists = list(album.artists)
                cover_url = album.cover_url
                raw_data = {}
            if not title:
                continue
            results.append(MusicInfo(
                media_source=cls._music_source,
                # 豆瓣歌曲没有独立 subject ID，使用专辑内绝对顺序避免多碟曲序重复。
                media_id=f"{album.media_id}:{index}",
                title=title,
                artists=artists,
                album=album.title,
                album_artist=album.artist or None,
                album_id=album.media_id,
                album_type=album.album_type,
                secondary_types=list(album.secondary_types),
                year=album.year,
                release_date=album.release_date,
                disc_number=disc_number,
                track_number=track_number,
                duration=duration,
                cover_url=cover_url,
                metadata_category=album.metadata_category,
                genres=list(album.genres),
                tags=list(album.tags),
                artist_country=album.artist_country,
                release_status=album.release_status,
                names=[title],
                detail_link=album.detail_link,
                raw_data=raw_data,
            ))
        for track in results:
            track.total_tracks = len(results)
        return results

    @classmethod
    def _douban_music_artists(cls, info: dict[str, Any]) -> List[str]:
        """从豆瓣新旧响应结构中提取艺术家名称。"""
        attrs = cast(
            dict[str, Any],
            info.get("attrs") if isinstance(info.get("attrs"), dict) else {},
        )
        values = (
            info.get("artists")
            or info.get("artist_names")
            or info.get("author")
            or info.get("singer")
            or attrs.get("singer")
            or []
        )
        if isinstance(values, str):
            values = [values]
        artists = []
        seen = set()
        for item in values:
            value = item.get("name") if isinstance(item, dict) else item
            text = cls._douban_music_text(value)
            identity = MetaMusic.compact_text(text) if text else ""
            if not text or identity in seen:
                continue
            seen.add(identity)
            artists.append(text)
        return artists

    @classmethod
    def _douban_music_search_artists(cls, info: dict[str, Any]) -> List[str]:
        """提取搜索候选艺术家，缺少结构化字段时回退到卡片副标题首段。"""
        artists = cls._douban_music_artists(info)
        if artists:
            return artists
        subtitle = cls._douban_music_text(info.get("card_subtitle"))
        if not subtitle:
            return []
        artist = re.split(r"\s+/\s+", subtitle, maxsplit=1)[0].strip()
        if not artist or re.fullmatch(r"\d{4}(?:-\d{1,2}(?:-\d{1,2})?)?", artist):
            return []
        return [artist]

    @classmethod
    def _douban_music_cover(cls, info: dict[str, Any]) -> Optional[str]:
        """从豆瓣多种图片字段中提取清晰封面。"""
        pic = info.get("pic") if isinstance(info.get("pic"), dict) else {}
        cover = info.get("cover") if isinstance(info.get("cover"), dict) else {}
        cover_img = info.get("cover_img") if isinstance(info.get("cover_img"), dict) else {}
        return next(
            (
                text for value in [
                    pic.get("large"),
                    cover_img.get("url"),
                    cover.get("large"),
                    cover.get("normal"),
                    cover.get("url"),
                    info.get("cover_url"),
                    info.get("image"),
                ]
                if (text := cls._douban_music_text(value))
            ),
            None,
        )

    @classmethod
    def _douban_music_date(cls, info: dict[str, Any]) -> Optional[str]:
        """从豆瓣新旧响应结构中提取首个发行日期。"""
        attrs = cast(
            dict[str, Any],
            info.get("attrs") if isinstance(info.get("attrs"), dict) else {},
        )
        return cls._douban_music_first(info.get("pubdate") or attrs.get("pubdate"))

    @staticmethod
    def _clean_douban_track_title(value: Any) -> Optional[str]:
        """清理豆瓣旧接口曲目文本开头的序号。"""
        text = str(value or "").strip()
        return re.sub(r"^\s*(?:\d+[\.、)]\s*)", "", text) or None

    @staticmethod
    def _douban_music_text(value: Any) -> Optional[str]:
        """把豆瓣外部响应值转换为去空白文本。"""
        text = str(value).strip() if value is not None else ""
        return text or None

    @classmethod
    def _douban_music_first(cls, value: Any) -> Optional[str]:
        """从豆瓣列表或标量字段中提取首个文本。"""
        if isinstance(value, list):
            return next((text for item in value if (text := cls._douban_music_text(item))), None)
        return cls._douban_music_text(value)

    @staticmethod
    def _douban_music_int(value: Any) -> Optional[int]:
        """将豆瓣外部响应值安全转换为整数。"""
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _douban_music_float(value: Any) -> float:
        """将豆瓣外部评分安全转换为浮点数。"""
        try:
            return float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _douban_music_year(cls, value: Any) -> Optional[int]:
        """从豆瓣年份或日期文本中提取四位年份。"""
        text = cls._douban_music_text(value)
        return int(text[:4]) if text and text[:4].isdigit() else None

    @staticmethod
    def _same_music_text(left: Optional[str], right: Optional[str]) -> bool:
        """使用音乐元数据紧凑文本规则比较豆瓣候选。"""
        return bool(left and right and MetaMusic.compact_text(left) == MetaMusic.compact_text(right))

    @staticmethod
    def _prepare_search_names(meta: MetaBase) -> List[str]:
        """
        准备搜索名称列表，保留中英文名称分别识别且按顺序去重的历史行为。
        """
        # 简体名称
        zh_name = zhconv_convert(meta.cn_name, "zh-hans") if meta.cn_name else None
        # 使用中英文名分别识别，去重去空，但要保持顺序
        return list(dict.fromkeys([k for k in [meta.cn_name, zh_name, meta.en_name] if k]))

    @staticmethod
    def _build_search_medias_result(meta: MetaBase, items: Optional[List[dict]]) -> List[MediaInfo]:
        """
        构建豆瓣搜索结果，并沿用原有的类型、标题包含和季信息处理规则。
        """
        if not items:
            return []
        ret_medias = []
        for item_obj in items:
            if meta.type and meta.type != MediaType.UNKNOWN and meta.type.value != item_obj.get("type_name"):
                continue
            if item_obj.get("type_name") not in (MediaType.TV.value, MediaType.MOVIE.value):
                continue
            if meta.name not in item_obj.get("target", {}).get("title"):
                continue
            ret_medias.append(MediaInfo(douban_info=item_obj.get("target")))
        # 将搜索词中的季写入标题中
        if ret_medias and meta.begin_season is not None:
            # 小写数据转大写
            season_str = cn2an.an2cn(meta.begin_season, "low")
            for media in ret_medias:
                if media.type == MediaType.TV:
                    media.title = f"{media.title} 第{season_str}季"
                    media.season = meta.begin_season
        return ret_medias

    def _recognize_media_core(self, meta: MetaBase = None,
                              mtype: MediaType = None,
                              doubanid: Optional[str] = None,
                              douban_info_func=None,
                              match_doubaninfo_func=None,
                              **kwargs) -> Optional[MediaInfo]:
        """
        识别媒体信息的核心逻辑
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与doubanid配套
        :param doubanid: 豆瓣ID
        :param douban_info_func: 获取豆瓣信息的函数
        :param match_doubaninfo_func: 匹配豆瓣信息的函数
        :return: 识别的媒体信息，包括剧集信息
        """
        plan = self._video_recognition_plan(
            meta=meta,
            mtype=mtype,
            douban_id=doubanid,
            effective_source=(
                kwargs.get("media_source")
                or get_runtime_setting('RECOGNIZE_SOURCE')
            ),
        )
        if not plan:
            return None
        if not self._prepare_video_plan(plan):
            return None
        if plan.douban_id:
            info = douban_info_func(
                doubanid=plan.douban_id,
                mtype=plan.media_type,
            )
        else:
            info = {}
            plan_meta = plan.require_meta()
            for name in plan.search_names:
                if plan_meta.begin_season is not None:
                    logger.info(f"正在识别 {name} 第{plan_meta.begin_season}季 ...")
                else:
                    logger.info(f"正在识别 {name} ...")
                match_info = match_doubaninfo_func(
                    name=name,
                    mtype=plan.media_type,
                    year=plan_meta.year,
                    season=plan_meta.begin_season,
                )
                if match_info:
                    info = douban_info_func(
                        doubanid=match_info.get("id"),
                        mtype=plan.media_type,
                    )
                    if info:
                        break
        return self._project_video_recognition(plan, info)

    async def _async_recognize_media_core(self, meta: MetaBase = None,
                                          mtype: MediaType = None,
                                          doubanid: Optional[str] = None,
                                          async_douban_info_func=None,
                                          async_match_doubaninfo_func=None,
                                          **kwargs) -> Optional[MediaInfo]:
        """
        识别媒体信息的核心逻辑（异步版本）
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与doubanid配套
        :param doubanid: 豆瓣ID
        :param async_douban_info_func: 获取豆瓣信息的异步函数
        :param async_match_doubaninfo_func: 匹配豆瓣信息的异步函数
        :return: 识别的媒体信息，包括剧集信息
        """
        plan = self._video_recognition_plan(
            meta=meta,
            mtype=mtype,
            douban_id=doubanid,
            effective_source=(
                kwargs.get("media_source")
                or get_runtime_setting('RECOGNIZE_SOURCE')
            ),
        )
        if not plan:
            return None
        if not self._prepare_video_plan(plan):
            return None
        if plan.douban_id:
            info = await async_douban_info_func(
                doubanid=plan.douban_id,
                mtype=plan.media_type,
            )
        else:
            info = {}
            plan_meta = plan.require_meta()
            for name in plan.search_names:
                if plan_meta.begin_season is not None:
                    logger.info(f"正在识别 {name} 第{plan_meta.begin_season}季 ...")
                else:
                    logger.info(f"正在识别 {name} ...")
                match_info = await async_match_doubaninfo_func(
                    name=name,
                    mtype=plan.media_type,
                    year=plan_meta.year,
                    season=plan_meta.begin_season,
                )
                if match_info:
                    info = await async_douban_info_func(
                        doubanid=match_info.get("id"),
                        mtype=plan.media_type,
                    )
                    if info:
                        break
        return self._project_video_recognition(plan, info)

    @classmethod
    def _video_recognition_plan(
            cls,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            douban_id: Optional[str],
            effective_source: Optional[Union[MediaSource, str]],
    ) -> Optional[_DoubanVideoRecognitionPlan]:
        """统一完成豆瓣影视来源、类型、原生 ID 和搜索名称准入。"""
        if not douban_id and not meta:
            return None
        if meta and not douban_id and effective_source != MediaSource.Douban:
            return None
        media_type = mtype or (meta.type if meta else None)
        if douban_id:
            return _DoubanVideoRecognitionPlan(
                meta=meta,
                media_type=media_type,
                douban_id=str(douban_id),
                search_names=(),
            )
        plan_meta = meta
        if plan_meta is None:
            raise RuntimeError("豆瓣影视标题识别计划缺少元数据")
        return _DoubanVideoRecognitionPlan(
            meta=plan_meta,
            media_type=media_type,
            douban_id=None,
            search_names=(
                tuple(cls._prepare_search_names(plan_meta)) if plan_meta.name else ()
            ),
        )

    @staticmethod
    def _prepare_video_plan(plan: _DoubanVideoRecognitionPlan) -> bool:
        """应用豆瓣影视识别的旧 ABI 元数据回写并报告缺失标题。"""
        if plan.douban_id:
            return True
        if not plan.search_names:
            logger.error("识别媒体信息时未提供元数据名称")
            return False
        if plan.meta and plan.media_type and plan.meta.type != plan.media_type:
            # 显式类型历史上会回写解析元数据，保留该可观察行为。
            plan.meta.type = plan.media_type
        return True

    @staticmethod
    def _project_video_recognition(
            plan: _DoubanVideoRecognitionPlan,
            info: Optional[dict[str, Any]],
    ) -> Optional[MediaInfo]:
        """把豆瓣影视详情统一投影为媒体信息并记录识别结论。"""
        label = plan.meta.name if plan.meta else plan.douban_id
        if not info:
            logger.info(f"{label} 未匹配到豆瓣媒体信息")
            return None
        mediainfo = MediaInfo(douban_info=info)
        if plan.meta:
            logger.info(
                f"{plan.meta.name} 豆瓣识别结果：{mediainfo.type.value} "
                f"{mediainfo.title_year} {mediainfo.douban_id}"
            )
        else:
            logger.info(
                f"{plan.douban_id} 豆瓣识别结果：{mediainfo.type.value} "
                f"{mediainfo.title_year}"
            )
        return mediainfo

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        media_source: Optional[MediaSource] = None,
                        media_id: Optional[str] = None,
                        **kwargs) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型
        :param media_source: 媒体来源
        :param media_id: 媒体来源原生ID
        :return: 识别的媒体信息，包括剧集信息
        """
        if media_source == self._music_source:
            return self._recognize_music_media(
                meta=meta if isinstance(meta, MetaMusic) else None,
                media_source=media_source,
                media_id=media_id,
                music_type=kwargs.get("music_type"),
            )
        # 音乐请求必须显式使用 doubanmusic，避免与影视豆瓣源混淆。
        if isinstance(meta, MetaMusic) or mtype == MediaType.MUSIC:
            return None
        if media_source and media_source != MediaSource.Douban:
            return None
        doubanid = str(media_id) if media_id is not None else None
        return self._recognize_media_core(
            meta=meta,
            mtype=mtype,
            doubanid=doubanid,
            douban_info_func=self.douban_info,
            match_doubaninfo_func=self.match_doubaninfo,
            media_source=media_source,
            **kwargs
        )

    async def async_recognize_media(self, meta: MetaBase = None,
                                    mtype: MediaType = None,
                                    media_source: Optional[MediaSource] = None,
                                    media_id: Optional[str] = None,
                                    **kwargs) -> Optional[MediaInfo]:
        """
        识别媒体信息（异步版本）
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型
        :param media_source: 媒体来源
        :param media_id: 媒体来源原生ID
        :return: 识别的媒体信息，包括剧集信息
        """
        if media_source == self._music_source:
            return await self._async_recognize_music_media(
                meta=meta if isinstance(meta, MetaMusic) else None,
                media_source=media_source,
                media_id=media_id,
                music_type=kwargs.get("music_type"),
            )
        # 音乐请求必须显式使用 doubanmusic，避免与影视豆瓣源混淆。
        if isinstance(meta, MetaMusic) or mtype == MediaType.MUSIC:
            return None
        if media_source and media_source != MediaSource.Douban:
            return None
        doubanid = str(media_id) if media_id is not None else None
        return await self._async_recognize_media_core(
            meta=meta,
            mtype=mtype,
            doubanid=doubanid,
            async_douban_info_func=self.async_douban_info,
            async_match_doubaninfo_func=self.async_match_doubaninfo,
            media_source=media_source,
            **kwargs
        )

    @staticmethod
    def _douban_detail_order(mtype: Optional[MediaType]) -> Tuple[MediaType, ...]:
        """返回详情查询的唯一类型顺序，未知类型保持电影优先回退电视剧。"""
        if mtype == MediaType.TV:
            return (MediaType.TV,)
        if mtype == MediaType.MOVIE:
            return (MediaType.MOVIE,)
        return MediaType.MOVIE, MediaType.TV

    @staticmethod
    def _classify_douban_detail(
            info: Optional[dict[str, Any]]
    ) -> str:
        """纯函数分类详情响应为未命中、速率限制或有效详情。"""
        if not info:
            return "missing"
        if "subject_ip_rate_limit" in info.get("msg", ""):
            return "rate_limited"
        return "matched"

    @classmethod
    def _accept_douban_detail(
            cls, info: Optional[dict[str, Any]]
    ) -> bool:
        """应用共享响应分类，并把豆瓣速率限制转换为既有领域异常。"""
        state = cls._classify_douban_detail(info)
        if state == "rate_limited":
            msg = f"触发豆瓣IP速率限制，错误信息：{info} ..."
            logger.warning(msg)
            raise APIRateLimitException(msg)
        return state == "matched"

    @staticmethod
    def _merge_douban_celebrities(
            info: dict[str, Any],
            celebrities: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """把人物详情合并回原响应，保持现有对象身份和字段覆盖语义。"""
        if celebrities:
            info["directors"] = celebrities.get("directors")
            info["actors"] = celebrities.get("actors")
        return info

    def _douban_detail(
            self, doubanid: str, mtype: MediaType
    ) -> Optional[dict[str, Any]]:
        """执行一次同步详情及人物查询，业务分类由共享状态机负责。"""
        if mtype == MediaType.TV:
            info = self.doubanapi.tv_detail(doubanid)
            celebrity_loader = self.doubanapi.tv_celebrities
        else:
            info = self.doubanapi.movie_detail(doubanid)
            celebrity_loader = self.doubanapi.movie_celebrities
        if not self._accept_douban_detail(info):
            return None
        detail = cast(dict[str, Any], info)
        return self._merge_douban_celebrities(
            detail, celebrity_loader(doubanid)
        )

    async def _async_douban_detail(
            self, doubanid: str, mtype: MediaType
    ) -> Optional[dict[str, Any]]:
        """执行一次异步详情及人物查询，业务分类由共享状态机负责。"""
        if mtype == MediaType.TV:
            info = await self.doubanapi.async_tv_detail(doubanid)
            celebrity_loader = self.doubanapi.async_tv_celebrities
        else:
            info = await self.doubanapi.async_movie_detail(doubanid)
            celebrity_loader = self.doubanapi.async_movie_celebrities
        if not self._accept_douban_detail(info):
            return None
        detail = cast(dict[str, Any], info)
        return self._merge_douban_celebrities(
            detail, await celebrity_loader(doubanid)
        )

    @rate_limit_exponential(source="douban_info")
    def douban_info(self, doubanid: str, mtype: MediaType = None, raise_exception: bool = True) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :param mtype:    媒体类型
        :param raise_exception: 触发速率限制时是否抛出异常
        :return: 豆瓣信息
        """
        """
        {
          "rating": {
            "count": 287365,
            "max": 10,
            "star_count": 3.5,
            "value": 6.6
          },
          "lineticket_url": "",
          "controversy_reason": "",
          "pubdate": [
            "2021-10-29(中国大陆)"
          ],
          "last_episode_number": null,
          "interest_control_info": null,
          "pic": {
            "large": "https://img9.doubanio.com/view/photo/m_ratio_poster/public/p2707553644.webp",
            "normal": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2707553644.webp"
          },
          "vendor_count": 6,
          "body_bg_color": "f4f5f9",
          "is_tv": false,
          "head_info": null,
          "album_no_interact": false,
          "ticket_price_info": "",
          "webisode_count": 0,
          "year": "2021",
          "card_subtitle": "2021 / 英国 美国 / 动作 惊悚 冒险 / 凯瑞·福永 / 丹尼尔·克雷格 蕾雅·赛杜",
          "forum_info": null,
          "webisode": null,
          "id": "20276229",
          "gallery_topic_count": 0,
          "languages": [
            "英语",
            "法语",
            "意大利语",
            "俄语",
            "西班牙语"
          ],
          "genres": [
            "动作",
            "惊悚",
            "冒险"
          ],
          "review_count": 926,
          "title": "007：无暇赴死",
          "intro": "世界局势波诡云谲，再度出山的邦德（丹尼尔·克雷格 饰）面临有史以来空前的危机，传奇特工007的故事在本片中达到高潮。新老角色集结亮相，蕾雅·赛杜回归，二度饰演邦女郎玛德琳。系列最恐怖反派萨芬（拉米·马雷克 饰）重磅登场，毫不留情地展示了自己狠辣的一面，不仅揭开了玛德琳身上隐藏的秘密，还酝酿着危及数百万人性命的阴谋，幽灵党的身影也似乎再次浮出水面。半路杀出的新00号特工（拉什纳·林奇 饰）与神秘女子（安娜·德·阿玛斯 饰）看似与邦德同阵作战，但其真实目的依然成谜。关乎邦德生死的新仇旧怨接踵而至，暗潮汹涌之下他能否拯救世界？",
          "interest_cmt_earlier_tip_title": "发布于上映前",
          "has_linewatch": true,
          "ugc_tabs": [
            {
              "source": "reviews",
              "type": "review",
              "title": "影评"
            },
            {
              "source": "forum_topics",
              "type": "forum",
              "title": "讨论"
            }
          ],
          "forum_topic_count": 857,
          "ticket_promo_text": "",
          "webview_info": {},
          "is_released": true,
          "actors": [
            {
              "name": "丹尼尔·克雷格",
              "roles": [
                "演员",
                "制片人",
                "配音"
              ],
              "title": "丹尼尔·克雷格（同名）英国,英格兰,柴郡,切斯特影视演员",
              "url": "https://movie.douban.com/celebrity/1025175/",
              "user": null,
              "character": "饰 詹姆斯·邦德 James Bond 007",
              "uri": "douban://douban.com/celebrity/1025175?subject_id=27230907",
              "avatar": {
                "large": "https://qnmob3.doubanio.com/view/celebrity/raw/public/p42588.jpg?imageView2/2/q/80/w/600/h/3000/format/webp",
                "normal": "https://qnmob3.doubanio.com/view/celebrity/raw/public/p42588.jpg?imageView2/2/q/80/w/200/h/300/format/webp"
              },
              "sharing_url": "https://www.douban.com/doubanapp/dispatch?uri=/celebrity/1025175/",
              "type": "celebrity",
              "id": "1025175",
              "latin_name": "Daniel Craig"
            }
          ],
          "interest": null,
          "vendor_icons": [
            "https://img9.doubanio.com/f/frodo/fbc90f355fc45d5d2056e0d88c697f9414b56b44/pics/vendors/tencent.png",
            "https://img2.doubanio.com/f/frodo/8286b9b5240f35c7e59e1b1768cd2ccf0467cde5/pics/vendors/migu_video.png",
            "https://img9.doubanio.com/f/frodo/88a62f5e0cf9981c910e60f4421c3e66aac2c9bc/pics/vendors/bilibili.png"
          ],
          "episodes_count": 0,
          "color_scheme": {
            "is_dark": true,
            "primary_color_light": "868ca5",
            "_base_color": [
              0.6333333333333333,
              0.18867924528301885,
              0.20784313725490197
            ],
            "secondary_color": "f4f5f9",
            "_avg_color": [
              0.059523809523809625,
              0.09790209790209795,
              0.5607843137254902
            ],
            "primary_color_dark": "676c7f"
          },
          "type": "movie",
          "null_rating_reason": "",
          "linewatches": [
            {
              "url": "http://v.youku.com/v_show/id_XNTIwMzM2NDg5Mg==.html?tpa=dW5pb25faWQ9MzAwMDA4XzEwMDAwMl8wMl8wMQ&refer=esfhz_operation.xuka.xj_00003036_000000_FNZfau_19010900",
              "source": {
                "literal": "youku",
                "pic": "https://img1.doubanio.com/img/files/file-1432869267.png",
                "name": "优酷视频"
              },
              "source_uri": "youku://play?vid=XNTIwMzM2NDg5Mg==&source=douban&refer=esfhz_operation.xuka.xj_00003036_000000_FNZfau_19010900",
              "free": false
            },
          ],
          "info_url": "https://www.douban.com/doubanapp//h5/movie/20276229/desc",
          "tags": [],
          "durations": [
            "163分钟"
          ],
          "comment_count": 97204,
          "cover": {
            "description": "",
            "author": {
              "loc": {
                "id": "108288",
                "name": "北京",
                "uid": "beijing"
              },
              "kind": "user",
              "name": "雨落下",
              "reg_time": "2020-08-11 16:22:48",
              "url": "https://www.douban.com/people/221011676/",
              "uri": "douban://douban.com/user/221011676",
              "id": "221011676",
              "avatar_side_icon_type": 3,
              "avatar_side_icon_id": "234",
              "avatar": "https://img2.doubanio.com/icon/up221011676-2.jpg",
              "is_club": false,
              "type": "user",
              "avatar_side_icon": "https://img2.doubanio.com/view/files/raw/file-1683625971.png",
              "uid": "221011676"
            },
            "url": "https://movie.douban.com/photos/photo/2707553644/",
            "image": {
              "large": {
                "url": "https://img9.doubanio.com/view/photo/l/public/p2707553644.webp",
                "width": 1082,
                "height": 1600,
                "size": 0
              },
              "raw": null,
              "small": {
                "url": "https://img9.doubanio.com/view/photo/s/public/p2707553644.webp",
                "width": 405,
                "height": 600,
                "size": 0
              },
              "normal": {
                "url": "https://img9.doubanio.com/view/photo/m/public/p2707553644.webp",
                "width": 405,
                "height": 600,
                "size": 0
              },
              "is_animated": false
            },
            "uri": "douban://douban.com/photo/2707553644",
            "create_time": "2021-10-26 15:05:01",
            "position": 0,
            "owner_uri": "douban://douban.com/movie/20276229",
            "type": "photo",
            "id": "2707553644",
            "sharing_url": "https://www.douban.com/doubanapp/dispatch?uri=/photo/2707553644/"
          },
          "cover_url": "https://img9.doubanio.com/view/photo/m_ratio_poster/public/p2707553644.webp",
          "restrictive_icon_url": "",
          "header_bg_color": "676c7f",
          "is_douban_intro": false,
          "ticket_vendor_icons": [
            "https://img9.doubanio.com/view/dale-online/dale_ad/public/0589a62f2f2d7c2.jpg"
          ],
          "honor_infos": [],
          "sharing_url": "https://movie.douban.com/subject/20276229/",
          "subject_collections": [],
          "wechat_timeline_share": "screenshot",
          "countries": [
            "英国",
            "美国"
          ],
          "url": "https://movie.douban.com/subject/20276229/",
          "release_date": null,
          "original_title": "No Time to Die",
          "uri": "douban://douban.com/movie/20276229",
          "pre_playable_date": null,
          "episodes_info": "",
          "subtype": "movie",
          "directors": [
            {
              "name": "凯瑞·福永",
              "roles": [
                "导演",
                "制片人",
                "编剧",
                "摄影",
                "演员"
              ],
              "title": "凯瑞·福永（同名）美国,加利福尼亚州,奥克兰影视演员",
              "url": "https://movie.douban.com/celebrity/1009531/",
              "user": null,
              "character": "导演",
              "uri": "douban://douban.com/celebrity/1009531?subject_id=27215222",
              "avatar": {
                "large": "https://qnmob3.doubanio.com/view/celebrity/raw/public/p1392285899.57.jpg?imageView2/2/q/80/w/600/h/3000/format/webp",
                "normal": "https://qnmob3.doubanio.com/view/celebrity/raw/public/p1392285899.57.jpg?imageView2/2/q/80/w/200/h/300/format/webp"
              },
              "sharing_url": "https://www.douban.com/doubanapp/dispatch?uri=/celebrity/1009531/",
              "type": "celebrity",
              "id": "1009531",
              "latin_name": "Cary Fukunaga"
            }
          ],
          "is_show": false,
          "in_blacklist": false,
          "pre_release_desc": "",
          "video": null,
          "aka": [
            "007：生死有时(港)",
            "007：生死交战(台)",
            "007：间不容死",
            "邦德25",
            "007：没空去死(豆友译名)",
            "James Bond 25",
            "Never Dream of Dying",
            "Shatterhand"
          ],
          "is_restrictive": false,
          "trailer": {
            "sharing_url": "https://www.douban.com/doubanapp/dispatch?uri=/movie/20276229/trailer%3Ftrailer_id%3D282585%26trailer_type%3DA",
            "video_url": "https://vt1.doubanio.com/202310011325/3b1f5827e91dde7826dc20930380dfc2/view/movie/M/402820585.mp4",
            "title": "中国预告片：终极决战版 (中文字幕)",
            "uri": "douban://douban.com/movie/20276229/trailer?trailer_id=282585&trailer_type=A",
            "cover_url": "https://img1.doubanio.com/img/trailer/medium/2712944408.jpg",
            "term_num": 0,
            "n_comments": 21,
            "create_time": "2021-11-01",
            "subject_title": "007：无暇赴死",
            "file_size": 10520074,
            "runtime": "00:42",
            "type": "A",
            "id": "282585",
            "desc": ""
          },
          "interest_cmt_earlier_tip_desc": "该短评的发布时间早于公开上映时间，作者可能通过其他渠道提前观看，请谨慎参考。其评分将不计入总评分。"
        }
        """

        if not doubanid:
            return None
        logger.info(f"开始获取豆瓣信息：{doubanid} ...")
        for detail_type in self._douban_detail_order(mtype):
            if info := self._douban_detail(doubanid, detail_type):
                return info
        return None

    @rate_limit_exponential(source="douban_info")
    async def async_douban_info(self, doubanid: str, mtype: MediaType = None,
                                raise_exception: bool = True) -> Optional[dict]:
        """
        获取豆瓣信息（异步版本）
        :param doubanid: 豆瓣ID
        :param mtype:    媒体类型
        :param raise_exception: 触发速率限制时是否抛出异常
        :return: 豆瓣信息
        """

        if not doubanid:
            return None
        logger.info(f"开始获取豆瓣信息：{doubanid} ...")
        for detail_type in self._douban_detail_order(mtype):
            if info := await self._async_douban_detail(doubanid, detail_type):
                return info
        return None

    def douban_discover(self, mtype: MediaType, sort: str, tags: str,
                        page: int = 1, count: int = 30) -> Optional[List[MediaInfo]]:
        """
        发现豆瓣电影、剧集
        :param mtype:  媒体类型
        :param sort:  排序方式
        :param tags:  标签
        :param page:  页码
        :param count:  数量
        :return: 媒体信息列表
        """
        logger.info(f"开始发现豆瓣 {mtype.value} ...")
        if mtype == MediaType.MOVIE:
            infos = self.doubanapi.movie_recommend(start=(page - 1) * count, count=count,
                                                   sort=sort, tags=tags)
        else:
            infos = self.doubanapi.tv_recommend(start=(page - 1) * count, count=count,
                                                sort=sort, tags=tags)
        return self._project_discover(infos)

    async def async_douban_discover(self, mtype: MediaType, sort: str, tags: str,
                                    page: int = 1, count: int = 30) -> Optional[List[MediaInfo]]:
        """
        发现豆瓣电影、剧集（异步版本）
        :param mtype:  媒体类型
        :param sort:  排序方式
        :param tags:  标签
        :param page:  页码
        :param count:  数量
        :return: 媒体信息列表
        """
        logger.info(f"开始发现豆瓣 {mtype.value} ...")
        if mtype == MediaType.MOVIE:
            infos = await self.doubanapi.async_movie_recommend(start=(page - 1) * count, count=count,
                                                               sort=sort, tags=tags)
        else:
            infos = await self.doubanapi.async_tv_recommend(start=(page - 1) * count, count=count,
                                                            sort=sort, tags=tags)
        return self._project_discover(infos)

    @staticmethod
    def _project_discover(infos: Optional[dict[str, Any]]) -> List[MediaInfo]:
        """过滤豆瓣发现页占位海报并投影为统一媒体列表。"""
        medias = [
            MediaInfo(douban_info=info)
            for info in (infos or {}).get("items") or []
        ]
        invalid_posters = (
            "movie_large.jpg",
            "tv_normal.png",
            "tv_normal.jpg",
            "tv_large.jpg",
        )
        return [
            media
            for media in medias
            if media.poster_path
            and not any(marker in media.poster_path for marker in invalid_posters)
        ]

    def movie_showing(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取正在上映的电影
        """
        infos = self.doubanapi.movie_showing(start=(page - 1) * count,
                                             count=count)
        return self._project_collection(infos)

    async def async_movie_showing(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取正在上映的电影（异步版本）
        """
        infos = await self.doubanapi.async_movie_showing(start=(page - 1) * count,
                                                         count=count)
        return self._project_collection(infos)

    def tv_weekly_chinese(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑国产剧
        """
        infos = self.doubanapi.tv_chinese_best_weekly(start=(page - 1) * count,
                                                      count=count)
        return self._project_collection(infos)

    async def async_tv_weekly_chinese(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑国产剧（异步版本）
        """
        infos = await self.doubanapi.async_tv_chinese_best_weekly(start=(page - 1) * count,
                                                                  count=count)
        return self._project_collection(infos)

    def tv_weekly_global(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑外国剧
        """
        infos = self.doubanapi.tv_global_best_weekly(start=(page - 1) * count,
                                                     count=count)
        return self._project_collection(infos)

    async def async_tv_weekly_global(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑外国剧（异步版本）
        """
        infos = await self.doubanapi.async_tv_global_best_weekly(start=(page - 1) * count,
                                                                 count=count)
        return self._project_collection(infos)

    def tv_animation(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣动画剧
        """
        infos = self.doubanapi.tv_animation(start=(page - 1) * count,
                                            count=count)
        return self._project_collection(infos)

    async def async_tv_animation(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣动画剧（异步版本）
        """
        infos = await self.doubanapi.async_tv_animation(start=(page - 1) * count,
                                                        count=count)
        return self._project_collection(infos)

    def movie_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门电影
        """
        infos = self.doubanapi.movie_hot_gaia(start=(page - 1) * count,
                                              count=count)
        return self._project_collection(infos)

    async def async_movie_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门电影（异步版本）
        """
        infos = await self.doubanapi.async_movie_hot_gaia(start=(page - 1) * count,
                                                          count=count)
        return self._project_collection(infos)

    def tv_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门剧集
        """
        infos = self.doubanapi.tv_hot(start=(page - 1) * count,
                                      count=count)
        return self._project_collection(infos)

    async def async_tv_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门剧集（异步版本）
        """
        infos = await self.doubanapi.async_tv_hot(start=(page - 1) * count,
                                                  count=count)
        return self._project_collection(infos)

    @staticmethod
    def _project_collection(
            infos: Optional[dict[str, Any]],
    ) -> List[MediaInfo]:
        """把豆瓣榜单或集合响应投影为统一媒体列表。"""
        return [
            MediaInfo(douban_info=info)
            for info in (infos or {}).get("subject_collection_items") or []
        ]

    def search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息
        """
        plan = self._search_plan(meta.name if meta else None, media_source)
        if not plan.enabled:
            return None
        if not plan.query:
            return []
        result = self.doubanapi.search(plan.query)
        return self._project_media_search(meta, result)

    async def async_search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息（异步版本）
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息
        """
        plan = self._search_plan(meta.name if meta else None, media_source)
        if not plan.enabled:
            return None
        if not plan.query:
            return []
        result = await self.doubanapi.async_search(plan.query)
        return self._project_media_search(meta, result)

    def search_persons(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息
        :param name: 人物名称
        :param media_source: 请求级搜索数据源
        :return: 人物信息列表
        """
        plan = self._search_plan(name, media_source)
        if not plan.enabled:
            return None
        if not plan.query:
            return []
        result = self.doubanapi.person_search(keyword=plan.query)
        return self._project_person_search(plan.query, result)

    async def async_search_persons(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息（异步版本）
        :param name: 人物名称
        :param media_source: 请求级搜索数据源
        :return: 人物信息列表
        """
        plan = self._search_plan(name, media_source)
        if not plan.enabled:
            return None
        if not plan.query:
            return []
        result = await self.doubanapi.async_person_search(keyword=plan.query)
        return self._project_person_search(plan.query, result)

    @staticmethod
    def _search_plan(
            query: Optional[str],
            media_source: Optional[MediaSourceSelection],
    ) -> _DoubanSearchPlan:
        """统一决定豆瓣搜索是否响应并规范化查询文本。"""
        enabled = is_media_source_enabled(media_source, MediaSource.Douban)
        return _DoubanSearchPlan(
            enabled=enabled,
            query=str(query).strip() if enabled and query else None,
        )

    @classmethod
    def _project_media_search(
            cls,
            meta: MetaBase,
            result: Optional[dict[str, Any]],
    ) -> List[MediaInfo]:
        """把豆瓣媒体搜索响应交给统一过滤与季信息投影。"""
        return cls._build_search_medias_result(
            meta, (result or {}).get("items")
        )

    @staticmethod
    def _project_person_search(
            name: str,
            result: Optional[dict[str, Any]],
    ) -> List[MediaPerson]:
        """把豆瓣人物搜索响应过滤并投影为统一人物列表。"""
        persons = []
        for item in (result or {}).get("items") or []:
            target = item.get("target") or {}
            title = target.get("title") or ""
            if name not in title:
                continue
            persons.append(MediaPerson(source="douban", **{
                "id": item.get("target_id"),
                "name": title,
                "url": target.get("url"),
                "images": target.get("cover", {}),
                "avatar": (target.get("cover_img", {}).get("url") or "").replace(
                    "/l/public/", "/s/public/"
                ),
            }))
        return persons

    @staticmethod
    def _process_imdbid_result(result: dict, imdbid: str) -> Optional[dict]:
        """
        处理IMDBID查询结果
        :param result: IMDBID查询返回的结果
        :param imdbid: IMDB ID
        :return: 处理后的结果，None表示无结果
        """
        if result:
            doubanid = result.get("id")
            if doubanid:
                if not str(doubanid).isdigit():
                    doubanid = re.search(r"\d+", doubanid).group(0)
                    result["id"] = doubanid
                logger.info(f"{imdbid} 查询到豆瓣信息：{result.get('title')}")
                return result
            return None
        return None

    @staticmethod
    def _process_search_results(result: dict, name: str, mtype: MediaType = None,
                                year: str = None, season: int = None) -> dict:
        """
        处理搜索结果并进行匹配
        :param result: 搜索返回的结果
        :param name: 搜索名称
        :param mtype: 媒体类型
        :param year: 年份
        :param season: 季号
        :return: 匹配到的豆瓣信息
        """
        if not result:
            logger.warn(f"未找到 {name} 的豆瓣信息")
            return {}

        # 触发rate limit检查
        if "search_access_rate_limit" in result.values():
            msg = f"触发豆瓣API速率限制，错误信息：{result} ..."
            logger.warn(msg)
            raise APIRateLimitException(msg)

        if not result.get("items"):
            logger.warn(f"未找到 {name} 的豆瓣信息")
            return {}

        for item_obj in result.get("items"):
            type_name = item_obj.get("type_name")
            if type_name not in [MediaType.TV.value, MediaType.MOVIE.value]:
                continue
            if mtype and mtype.value != type_name:
                continue
            if mtype and mtype == MediaType.TV and season is None:
                season = 1
            item = item_obj.get("target")
            title = item.get("title")
            if not title:
                continue
            meta = MetaInfo(title)
            if type_name == MediaType.TV.value:
                meta.type = MediaType.TV
                meta.begin_season = meta.begin_season if meta.begin_season is not None else 1
            if meta.name == name \
                    and ((season is None and meta.begin_season is None) or meta.begin_season == season) \
                    and (not year or item.get('year') == year):
                logger.info(f"{name} 匹配到豆瓣信息：{item.get('id')} {item.get('title')}")
                return item
        return {}

    @retry(Exception, 5, 3, 3, logger=logger)
    @rate_limit_exponential(source="match_doubaninfo")
    def match_doubaninfo(self, name: str, imdbid: str = None,
                         mtype: MediaType = None, year: str = None, season: int = None,
                         raise_exception: bool = False) -> dict:
        """
        搜索和匹配豆瓣信息
        :param name:  名称
        :param imdbid:  IMDB ID
        :param mtype:  类型
        :param year:  年份
        :param season:  季号
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        if imdbid:
            # 优先使用IMDBID查询
            logger.info(f"开始使用IMDBID {imdbid} 查询豆瓣信息 ...")
            result = self.doubanapi.imdbid(imdbid)
            processed_result = self._process_imdbid_result(result, imdbid)
            if processed_result:
                return processed_result

        # 搜索
        logger.info(f"开始使用名称 {name} 匹配豆瓣信息 ...")
        result = self.doubanapi.search(f"{name} {year or ''}".strip())
        return self._process_search_results(result, name, mtype, year, season)

    @retry(Exception, 5, 3, 3, logger=logger)
    @rate_limit_exponential(source="match_doubaninfo")
    async def async_match_doubaninfo(self, name: str, imdbid: str = None,
                                     mtype: MediaType = None, year: str = None, season: int = None,
                                     raise_exception: bool = False) -> dict:
        """
        搜索和匹配豆瓣信息（异步版本）
        :param name:  名称
        :param imdbid:  IMDB ID
        :param mtype:  类型
        :param year:  年份
        :param season:  季号
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        if imdbid:
            # 优先使用IMDBID查询
            logger.info(f"开始使用IMDBID {imdbid} 查询豆瓣信息 ...")
            result = await self.doubanapi.async_imdbid(imdbid)
            processed_result = self._process_imdbid_result(result, imdbid)
            if processed_result:
                return processed_result

        # 搜索
        logger.info(f"开始使用名称 {name} 匹配豆瓣信息 ...")
        result = await self.doubanapi.async_search(f"{name} {year or ''}".strip())
        return self._process_search_results(result, name, mtype, year, season)

    def movie_top250(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣电影TOP250
        """
        infos = self.doubanapi.movie_top250(start=(page - 1) * count,
                                            count=count)
        return self._project_collection(infos)

    async def async_movie_top250(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣电影TOP250（异步版本）
        """
        infos = await self.doubanapi.async_movie_top250(start=(page - 1) * count,
                                                        count=count)
        return self._project_collection(infos)

    def metadata_nfo(self, mediainfo: MediaInfo, season: int = None, **kwargs) -> Optional[str]:
        """
        获取NFO文件内容文本
        :param mediainfo: 媒体信息
        :param season: 季号
        """
        if (mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')) != "douban":
            return None
        return self.scraper.get_metadata_nfo(mediainfo=mediainfo, season=season)

    def metadata_img(self, mediainfo: MediaInfo, season: int = None, episode: int = None) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if (mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')) != "douban":
            return None
        return self.scraper.get_metadata_img(mediainfo=mediainfo, season=season, episode=episode)

    @staticmethod
    def _validate_douban_obtain_images_params(mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        验证豆瓣 obtain_images 参数
        :param mediainfo: 媒体信息
        :return: None 表示不处理，MediaInfo 表示继续处理
        """
        if mediainfo.media_source != MediaSource.Douban and get_runtime_setting('RECOGNIZE_SOURCE') != "douban":
            return None
        if not mediainfo.douban_id:
            return None
        if mediainfo.backdrop_path:
            # 没有图片缺失
            return mediainfo
        return None

    @staticmethod
    def _process_douban_images(mediainfo: MediaInfo, info: dict) -> MediaInfo:
        """
        处理豆瓣图片数据
        :param mediainfo: 媒体信息
        :param info: 图片信息
        :return: 更新后的媒体信息
        """
        if not info:
            return mediainfo
        images = info.get("photos")
        # 背景图
        if images:
            backdrop = images[0].get("image", {}).get("large") or {}
            if backdrop:
                mediainfo.backdrop_path = backdrop.get("url")
        return mediainfo

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        # 验证参数
        result = self._validate_douban_obtain_images_params(mediainfo)
        if result is not None:
            return result

        # 调用图片接口
        if mediainfo.type == MediaType.MOVIE:
            info = self.doubanapi.movie_photos(mediainfo.douban_id)
        else:
            info = self.doubanapi.tv_photos(mediainfo.douban_id)

        # 处理图片数据
        return self._process_douban_images(mediainfo, info)

    async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片（异步版本）
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        # 验证参数
        result = self._validate_douban_obtain_images_params(mediainfo)
        if result is not None:
            return result

        # 调用图片接口
        if mediainfo.type == MediaType.MOVIE:
            info = await self.doubanapi.async_movie_photos(mediainfo.douban_id)
        else:
            info = await self.doubanapi.async_tv_photos(mediainfo.douban_id)

        # 处理图片数据
        return self._process_douban_images(mediainfo, info)

    def clear_cache(self):
        """
        清除缓存
        """
        logger.info("开始清除豆瓣缓存 ...")
        self.doubanapi.clear_cache()
        logger.info("豆瓣缓存清除完成")

    def douban_movie_credits(self, doubanid: str) -> List[_SchemaMediaPerson]:
        """
        根据豆瓣ID查询电影演职员表
        :param doubanid:  豆瓣ID
        """
        result = self.doubanapi.movie_celebrities(subject_id=doubanid)
        return self._process_celebrity_data(result)

    def douban_tv_credits(self, doubanid: str) -> List[_SchemaMediaPerson]:
        """
        根据豆瓣ID查询电视剧演职员表
        :param doubanid:  豆瓣ID
        """
        result = self.doubanapi.tv_celebrities(subject_id=doubanid)
        return self._process_celebrity_data(result)

    def douban_movie_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电影
        :param doubanid:  豆瓣ID
        """
        recommend = self.doubanapi.movie_recommendations(subject_id=doubanid)
        if recommend:
            return [MediaInfo(douban_info=info) for info in recommend]
        return []

    def douban_tv_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电视剧
        :param doubanid:  豆瓣ID
        """
        recommend = self.doubanapi.tv_recommendations(subject_id=doubanid)
        if recommend:
            return [MediaInfo(douban_info=info) for info in recommend]
        return []

    def douban_person_detail(self, person_id: int) -> _SchemaMediaPerson:
        """
        获取人物详细信息
        :param person_id:  豆瓣人物ID
        """
        detail = self.doubanapi.person_detail(person_id)
        if detail:
            also_known_as = []
            infos = detail.get("extra", {}).get("info")
            if infos:
                also_known_as = ["：".join(info) for info in infos]
            image = detail.get("cover_img", {}).get("url")
            if image:
                image = image.replace("/l/public/", "/s/public/")
            return _SchemaMediaPerson(source='douban', **{
                "id": detail.get("id"),
                "name": detail.get("title"),
                "avatar": image,
                "biography": detail.get("extra", {}).get("short_info"),
                "also_known_as": also_known_as,
            })
        return _SchemaMediaPerson(source='douban')

    def douban_person_credits(self, person_id: int, page: int = 1) -> List[MediaInfo]:
        """
        根据TMDBID查询人物参演作品
        :param person_id:  人物ID
        :param page:  页码
        """
        # 获取人物参演作品集
        personinfo = self.doubanapi.person_detail(person_id)
        if not personinfo:
            return []
        collection_id = None
        for module in personinfo.get("modules"):
            if module.get("type") == "work_collections":
                collection_id = module.get("payload", {}).get("id")
        # 查询作品集内容
        if collection_id:
            collections = self.doubanapi.person_work(subject_id=collection_id, start=(page - 1) * 20, count=20)
            if collections:
                works = collections.get("works")
                return [MediaInfo(douban_info=work.get("subject")) for work in works]
        return []

    @staticmethod
    def _process_celebrity_data(result: dict) -> List[_SchemaMediaPerson]:
        """
        处理演职员表数据的公共方法
        :param result: API返回的演职员表数据
        :return: 处理后的演员列表
        """
        if not result:
            return []
        ret_list = result.get("actors") or []
        if ret_list:
            # 更新豆瓣演员信息中的ID，从URI中提取'douban://douban.com/celebrity/1316132?subject_id=27503705' subject_id
            for doubaninfo in ret_list:
                doubaninfo['id'] = doubaninfo.get('uri', '').split('?subject_id=')[-1]
            return [_SchemaMediaPerson(source='douban', **doubaninfo) for doubaninfo in ret_list]
        return []

    async def async_douban_movie_credits(self, doubanid: str) -> List[_SchemaMediaPerson]:
        """
        根据豆瓣ID查询电影演职员表（异步版本）
        :param doubanid:  豆瓣ID
        """
        result = await self.doubanapi.async_movie_celebrities(subject_id=doubanid)
        return self._process_celebrity_data(result)

    async def async_douban_tv_credits(self, doubanid: str) -> List[_SchemaMediaPerson]:
        """
        根据豆瓣ID查询电视剧演职员表（异步版本）
        :param doubanid:  豆瓣ID
        """
        result = await self.doubanapi.async_tv_celebrities(subject_id=doubanid)
        return self._process_celebrity_data(result)

    async def async_douban_movie_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电影（异步版本）
        :param doubanid:  豆瓣ID
        """
        recommend = await self.doubanapi.async_movie_recommendations(subject_id=doubanid)
        if recommend:
            return [MediaInfo(douban_info=info) for info in recommend]
        return []

    async def async_douban_tv_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电视剧（异步版本）
        :param doubanid:  豆瓣ID
        """
        recommend = await self.doubanapi.async_tv_recommendations(subject_id=doubanid)
        if recommend:
            return [MediaInfo(douban_info=info) for info in recommend]
        return []

    async def async_douban_person_detail(self, person_id: int) -> _SchemaMediaPerson:
        """
        获取人物详细信息（异步版本）
        :param person_id:  豆瓣人物ID
        """
        detail = await self.doubanapi.async_person_detail(person_id)
        if detail:
            also_known_as = []
            infos = detail.get("extra", {}).get("info")
            if infos:
                also_known_as = ["：".join(info) for info in infos]
            image = detail.get("cover_img", {}).get("url")
            if image:
                image = image.replace("/l/public/", "/s/public/")
            return _SchemaMediaPerson(source='douban', **{
                "id": detail.get("id"),
                "name": detail.get("title"),
                "avatar": image,
                "biography": detail.get("extra", {}).get("short_info"),
                "also_known_as": also_known_as,
            })
        return _SchemaMediaPerson(source='douban')

    async def async_douban_person_credits(self, person_id: int, page: int = 1) -> List[MediaInfo]:
        """
        根据豆瓣ID查询人物参演作品（异步版本）
        :param person_id:  人物ID
        :param page:  页码
        """
        # 获取人物参演作品集
        personinfo = await self.doubanapi.async_person_detail(person_id)
        if not personinfo:
            return []
        collection_id = None
        for module in personinfo.get("modules"):
            if module.get("type") == "work_collections":
                collection_id = module.get("payload", {}).get("id")
        # 查询作品集内容
        if collection_id:
            collections = await self.doubanapi.async_person_work(subject_id=collection_id, start=(page - 1) * 20,
                                                                 count=20)
            if collections:
                works = collections.get("works")
                return [MediaInfo(douban_info=work.get("subject")) for work in works]
        return []
