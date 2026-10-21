"""
豆瓣影视与音乐识别、发现、人物评论查询及刮削模块。
"""

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple, Union, cast

import cn2an

from app.adapters.network.http import RequestUtils
from app.application.music.recognition import unique_music_match
from app.domain.context import (
    MediaInfo,
    MusicAlbumInfo,
    MusicInfo,
)
from app.domain.media import is_media_source_enabled, is_media_source_selected
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.domain.music import align_music_tracks, music_artist_matches, music_title_matches, music_version_matches
from app.foundation.text import convert as zhconv_convert
from app.modules import _ModuleBase
from app.modules._base.media import MediaAuxiliaryProviderMixin
from app.modules.douban.apiv2 import DoubanApi
from app.modules.douban.scraper import DoubanScraper
from app.runtime.execution import retry
from app.runtime.log import logger
from app.runtime.rate import rate_limit_exponential
from app.runtime.settings import get_runtime_setting
from app.schemas.comment import (
    MediaComment as _SchemaMediaComment,
)
from app.schemas.comment import (
    MediaCommentAuthor as _SchemaMediaCommentAuthor,
)
from app.schemas.comment import (
    MediaCommentPage as _SchemaMediaCommentPage,
)
from app.schemas.comment import (
    MediaReview as _SchemaMediaReview,
)
from app.schemas.comment import (
    MediaReviewPage as _SchemaMediaReviewPage,
)
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
    MusicEntityType,
)


@dataclass(frozen=True, slots=True)
class _DoubanMusicRecognitionPlan:
    """
    描述豆瓣音乐识别的显式身份或候选搜索范围。

    :param meta: 音乐元数据；纯身份详情查询时可为空
    :param media_id: 请求的豆瓣音乐原生标识，可包含冒号分隔的曲目标识
    :param album_id: 已解析的专辑标识；名称候选搜索时可为空
    :param track_id: 专辑内目标曲目标识；专辑查询时为空
    :param music_type: album 表示专辑，recording 表示录音；未指定时结合身份判断
    :param keyword: 名称候选搜索词；纯身份查询时可为空
    """

    meta: Optional[MetaMusic]
    media_id: Optional[str]
    album_id: Optional[str]
    track_id: Optional[str]
    music_type: Optional[str]
    keyword: Optional[str]

    def require_meta(self) -> MetaMusic:
        """
        返回候选搜索计划必有的音乐元数据。

        :return: 计划中的非空元数据
        :raises RuntimeError: 当前计划缺少该方法要求的数据
        """
        if self.meta is None:
            raise RuntimeError("豆瓣音乐候选识别计划缺少音乐元数据")
        return self.meta

    def require_album_id(self) -> str:
        """
        返回详情识别计划必有的专辑 ID。

        :return: 计划中的非空专辑标识
        :raises RuntimeError: 当前计划缺少该方法要求的数据
        """
        if self.album_id is None:
            raise RuntimeError("豆瓣音乐详情识别计划缺少专辑 ID")
        return self.album_id

    def require_keyword(self) -> str:
        """
        返回候选识别计划必有的搜索词。

        :return: 计划中的非空搜索词
        :raises RuntimeError: 当前计划缺少该方法要求的数据
        """
        if self.keyword is None:
            raise RuntimeError("豆瓣音乐候选识别计划缺少搜索词")
        return self.keyword


@dataclass(frozen=True, slots=True)
class _DoubanVideoRecognitionPlan:
    """
    描述豆瓣影视识别的原生 ID 或有序标题候选。

    :param meta: 影视元数据，按名称识别时必须存在
    :param media_type: 目标电影或电视剧类型，可为空
    :param douban_id: 显式豆瓣条目标识，可为空
    :param search_names: 按优先顺序去重的名称候选
    """

    meta: Optional[MetaBase]
    media_type: Optional[MediaType]
    douban_id: Optional[str]
    search_names: tuple[str, ...]

    def require_meta(self) -> MetaBase:
        """
        返回标题识别计划必有的影视元数据。

        :return: 计划中的非空元数据
        :raises RuntimeError: 当前计划缺少该方法要求的数据
        """
        if self.meta is None:
            raise RuntimeError("豆瓣影视标题识别计划缺少元数据")
        return self.meta


@dataclass(frozen=True, slots=True)
class _DoubanSearchPlan:
    """
    保存豆瓣搜索来源准入结果和有效查询文本。

    :param enabled: 本次来源选择是否允许豆瓣响应
    :param query: 去除空白后的有效查询文本；没有有效文本时为 None
    """

    enabled: bool
    query: Optional[str]


class DoubanModule(MediaAuxiliaryProviderMixin, _ModuleBase):
    """
    提供豆瓣影视与豆瓣音乐元数据识别能力。
    """

    auxiliary_media_source = MediaSource.Douban
    _music_source = MediaSource.DoubanMusic
    doubanapi: Optional[DoubanApi] = None
    scraper: Optional[DoubanScraper] = None

    def _require_api(self) -> DoubanApi:
        """
        取得模块激活时创建的豆瓣客户端，保留初始化前兼容属性为空的行为。

        :return: 已初始化的豆瓣客户端
        :raises RuntimeError: 模块尚未初始化
        """
        if self.doubanapi is None:
            raise RuntimeError("豆瓣模块尚未初始化")
        return self.doubanapi

    def _require_scraper(self) -> DoubanScraper:
        """
        取得模块初始化时创建的豆瓣刮削器。

        :return: 已初始化的豆瓣刮削器
        :raises RuntimeError: 模块尚未初始化
        """
        if self.scraper is None:
            raise RuntimeError("豆瓣刮削器尚未初始化")
        return self.scraper

    def init_module(self) -> None:
        """
        初始化豆瓣 API 客户端和 NFO 刮削器。
        """
        self.doubanapi = DoubanApi()
        self.scraper = DoubanScraper()

    def stop(self) -> None:
        """
        关闭豆瓣同步 HTTP 客户端。
        """
        self._require_api().close()

    def test(self) -> Tuple[bool, str]:
        """
        请求豆瓣电影首页，检查是否取得 HTTP 响应。

        :return: 成功标记与错误说明；取得任意响应即判定连通，不校验 HTTP 状态码
        """
        ret = RequestUtils().get_res("https://movie.douban.com/")
        if ret is None:
            return False, "豆瓣网络连接失败"
        return True, ""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        保留配置入口；豆瓣配置由运行设置管理，返回 None。
        """
        return None

    @staticmethod
    def get_name() -> str:
        """
        返回模块显示名称。
        """
        return "豆瓣"

    @staticmethod
    def get_music_source() -> MediaSource:
        """
        返回音乐识别使用的数据源标识。
        """
        return DoubanModule._music_source

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """
        获取模块子类型。
        """
        return MediaRecognizeType.Douban

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 2

    def search_music(
            self,
            meta: MetaMusic,
            limit: int = 20,
            media_source: Optional[MediaSourceSelection] = None,
            music_types: Optional[Iterable[MusicEntityType]] = None,
    ) -> Optional[List[MusicInfo]]:
        """
        按请求来源搜索豆瓣音乐专辑，并兼容统一音乐搜索的实体过滤参数。

        :param meta: 待识别或搜索的影视、音乐元数据
        :param limit: 请求结果上限，音乐搜索限制在 1 至 100 条
        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :param music_types: 兼容统一音乐搜索的实体类型参数，当前忽略，实际搜索专辑
        :return: 专辑候选列表；没有关键词或无匹配时为空列表，来源不适用时返回 None
        """
        del music_types
        if not is_media_source_selected(media_source, self._music_source):
            return None
        keyword = meta.album or meta.title
        if not keyword:
            return []
        result = self._require_api().music_search(keyword=keyword, count=max(1, min(limit, 100)))
        return self._build_music_search_results(result)

    def recognize_music(
            self,
            media_source: MediaSource,
            media_id: str,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """
        按豆瓣音乐原生 ID 和实体类型获取专辑或专辑内曲目详情。

        按计划选择显式身份或名称候选，影视识别准备阶段可能更新输入 meta 的类型。

        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :param media_id: 来源原生标识；音乐曲目使用“专辑标识:曲目标识”形式
        :param music_type: album 表示专辑，recording 表示录音；未指定时结合身份判断
        :return: 匹配的标准影视或音乐对象；来源、类型或标识不适用以及未命中时返回 None
        """
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
        """
        按豆瓣音乐专辑 ID 获取标准化专辑详情和曲目。

        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :param media_id: 来源原生标识；音乐曲目使用“专辑标识:曲目标识”形式
        :return: 包含专辑及曲目详情的对象；来源不适用或无详情时返回 None
        """
        if media_source != self._music_source or not media_id:
            return None
        info = self._require_api().music_detail(subject_id=str(media_id))
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
        """
        按官方新碟榜或标签交集浏览豆瓣音乐，并保留豆瓣条目原生身份。

        多标签仅在每个标签最多 300 条候选内求交集，不代表全部站点结果。

        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :param entity: 兼容统一音乐发现的实体类型参数，当前忽略，实际返回专辑
        :param mode: chart 使用新碟榜，其他值使用标签页
        :param tags: 逗号分隔的标签；音乐多标签按原生标识求交集，空值使用“流行”
        :param sort: 豆瓣排序代码，直接传递给对应接口
        :return: 豆瓣音乐专辑列表；无结果为空列表，来源不适用时返回 None
        """
        if media_source != self._music_source:
            return None
        del entity
        if mode == "chart":
            chart_items = self._build_music_search_results(self._require_api().music_chart())
            start = max(page - 1, 0) * max(1, count)
            return chart_items[start:start + max(1, count)]
        selected_tags = [tag.strip() for tag in str(tags or "").split(",") if tag.strip()]
        if not selected_tags:
            selected_tags = ["流行"]
        if len(selected_tags) == 1:
            result = self._require_api().music_tag(
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
                self._require_api().music_tag(tag=tag, start=0, count=scan_count, sort=sort)
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
        """
        按豆瓣音乐专辑 ID 返回相关推荐条目。

        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :param media_id: 来源原生标识；音乐曲目使用“专辑标识:曲目标识”形式
        :param count: 本次最多返回的条目数
        :return: 豆瓣音乐专辑列表；无结果为空列表，来源不适用时返回 None
        """
        if media_source != self._music_source or not media_id:
            return None
        result = self._require_api().music_recommendations(
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
        """
        执行豆瓣音乐详情识别或按专辑名称匹配。
        """
        plan = self._music_recognition_plan(
            meta, media_source, media_id, music_type
        )
        if not plan:
            return None
        if plan.media_id:
            info = self._require_api().music_detail(
                subject_id=plan.require_album_id()
            )
            return self._project_music_detail(plan, info)
        meta = plan.require_meta()
        result = self._require_api().music_search(
            keyword=plan.require_keyword(), count=20
        )
        candidates = self._matching_music_candidates(
            meta, self._build_music_search_results(result)
        )
        direct = [item for candidate in candidates if (item := self._direct_music_candidate(plan, candidate)) is not None]
        if direct:
            return unique_music_match(direct)
        tracks = []
        if plan.music_type != MUSIC_ENTITY_ALBUM and meta.album and meta.title:
            for candidate in candidates:
                info = self._require_api().music_detail(subject_id=str(candidate.media_id))
                album = self._douban_music_to_album(info) if info else None
                if album:
                    tracks.extend(album.tracks)
        return self._select_douban_music_track(meta, MusicAlbumInfo(tracks=tracks))

    async def _async_recognize_music_media(
            self,
            meta: Optional[MetaMusic],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """
        异步执行豆瓣音乐详情识别或按专辑名称匹配。
        """
        plan = self._music_recognition_plan(
            meta, media_source, media_id, music_type
        )
        if not plan:
            return None
        if plan.media_id:
            info = await self._require_api().async_music_detail(
                subject_id=plan.require_album_id()
            )
            return self._project_music_detail(plan, info)
        meta = plan.require_meta()
        result = await self._require_api().async_music_search(
            keyword=plan.require_keyword(), count=20
        )
        candidates = self._matching_music_candidates(
            meta, self._build_music_search_results(result)
        )
        direct = [item for candidate in candidates if (item := self._direct_music_candidate(plan, candidate)) is not None]
        if direct:
            return unique_music_match(direct)
        tracks = []
        if plan.music_type != MUSIC_ENTITY_ALBUM and meta.album and meta.title:
            for candidate in candidates:
                info = await self._require_api().async_music_detail(
                    subject_id=str(candidate.media_id)
                )
                album = self._douban_music_to_album(info) if info else None
                if album:
                    tracks.extend(album.tracks)
        return self._select_douban_music_track(meta, MusicAlbumInfo(tracks=tracks))

    @classmethod
    def _music_recognition_plan(
            cls,
            meta: Optional[MetaMusic],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            music_type: Optional[str],
    ) -> Optional[_DoubanMusicRecognitionPlan]:
        """
        统一完成豆瓣音乐来源、实体类型、ID 和搜索词准入。
        """
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
        """
        把豆瓣音乐详情按计划投影为专辑或指定曲目。
        """
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
        """
        按专辑名称与专辑署名筛选父级候选，录音版本留待实际目标确认。
        """
        expected_title = meta.album or meta.title
        expected_artists = [meta.album_artist] if meta.album_artist else meta.artists
        return [
            candidate
            for candidate in candidates
            if music_title_matches(candidate, expected_title, preserve_editions=True)
            and (
                not expected_artists
                or not candidate.artists
                or music_artist_matches(candidate, expected_artists)
            )
        ]

    @staticmethod
    def _direct_music_candidate(
            plan: _DoubanMusicRecognitionPlan,
            candidate: MusicInfo,
    ) -> Optional[MusicInfo]:
        """
        决定候选可直接返回，还是必须继续读取专辑曲目。
        """
        meta = plan.require_meta()
        if plan.music_type != MUSIC_ENTITY_ALBUM and (
            (meta.album and meta.title) or plan.music_type == MUSIC_ENTITY_RECORDING
        ):
            return None
        album_meta = MetaMusic(title=meta.album or meta.title, version=meta.version)
        return candidate if music_version_matches(candidate, album_meta) else None

    @classmethod
    def _select_douban_music_track(
            cls,
            meta: MetaMusic,
            album: Optional[MusicAlbumInfo],
    ) -> Optional[MusicInfo]:
        """
        跨候选专辑采用唯一曲名、署名、曲序和时长对位，不按返回顺序消除歧义。
        """
        candidates = list({(track.media_source, track.media_id or id(track)): track
                           for track in (album.tracks if album else [])
                           if music_version_matches(track, meta) and align_music_tracks([meta], [track])}.values())
        aligned = align_music_tracks([meta], candidates)
        if 0 in aligned:
            return candidates[aligned[0]]
        return unique_music_match(candidates)

    @classmethod
    def _build_music_search_results(
            cls,
            result: Optional[dict[str, Any] | list[Any]],
    ) -> List[MusicInfo]:
        """
        把豆瓣音乐搜索响应转换为专辑候选列表。
        """
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
        """
        把豆瓣音乐详情转换为标准专辑信息和曲目。
        """
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
        """
        从豆瓣新旧响应结构中提取专辑曲目。
        """
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
        """
        从豆瓣新旧响应结构中提取艺术家名称。
        """
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
        """
        提取搜索候选艺术家，缺少结构化字段时回退到卡片副标题首段。
        """
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
        """
        从豆瓣多种图片字段中提取清晰封面。
        """
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
        """
        从豆瓣新旧响应结构中提取首个发行日期。
        """
        attrs = cast(
            dict[str, Any],
            info.get("attrs") if isinstance(info.get("attrs"), dict) else {},
        )
        return cls._douban_music_first(info.get("pubdate") or attrs.get("pubdate"))

    @staticmethod
    def _clean_douban_track_title(value: Any) -> Optional[str]:
        """
        清理豆瓣旧接口曲目文本开头的序号。
        """
        text = str(value or "").strip()
        return re.sub(r"^\s*(?:\d+[\.、)]\s*)", "", text) or None

    @staticmethod
    def _douban_music_text(value: Any) -> Optional[str]:
        """
        把豆瓣外部响应值转换为去空白文本。
        """
        text = str(value).strip() if value is not None else ""
        return text or None

    @classmethod
    def _douban_music_first(cls, value: Any) -> Optional[str]:
        """
        从豆瓣列表或标量字段中提取首个文本。
        """
        if isinstance(value, list):
            return next((text for item in value if (text := cls._douban_music_text(item))), None)
        return cls._douban_music_text(value)

    @staticmethod
    def _douban_music_int(value: Any) -> Optional[int]:
        """
        将豆瓣外部响应值安全转换为整数。
        """
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _douban_music_float(value: Any) -> float:
        """
        将豆瓣外部评分安全转换为浮点数。
        """
        try:
            return float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _douban_music_year(cls, value: Any) -> Optional[int]:
        """
        从豆瓣年份或日期文本中提取四位年份。
        """
        text = cls._douban_music_text(value)
        return int(text[:4]) if text and text[:4].isdigit() else None

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
    def _build_search_medias_result(
            meta: MetaBase, items: Optional[List[dict[str, Any]]]
    ) -> List[MediaInfo]:
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
            target = item_obj.get("target")
            if not isinstance(target, dict):
                continue
            ret_medias.append(MediaInfo(douban_info=target))
        # 将搜索词中的季写入标题中
        if ret_medias and meta.begin_season is not None:
            # 小写数据转大写
            season_str = cn2an.an2cn(meta.begin_season, "low")
            for media in ret_medias:
                if media.type == MediaType.TV:
                    media.title = f"{media.title} 第{season_str}季"
                    media.season = meta.begin_season
        return ret_medias

    def _recognize_media_core(self, meta: Optional[MetaBase] = None,
                              mtype: Optional[MediaType] = None,
                              doubanid: Optional[str] = None,
                              douban_info_func: Any = None,
                              match_doubaninfo_func: Any = None,
                              **kwargs: Any) -> Optional[MediaInfo]:
        """
        识别媒体信息的核心逻辑。

        :param meta: 识别的元数据
        :param mtype: 识别的媒体类型，与doubanid配套
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

    async def _async_recognize_media_core(self, meta: Optional[MetaBase] = None,
                                          mtype: Optional[MediaType] = None,
                                          doubanid: Optional[str] = None,
                                          async_douban_info_func: Any = None,
                                          async_match_doubaninfo_func: Any = None,
                                          **kwargs: Any) -> Optional[MediaInfo]:
        """
        识别媒体信息的核心逻辑（异步版本）。

        :param meta: 识别的元数据
        :param mtype: 识别的媒体类型，与doubanid配套
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
        """
        统一完成豆瓣影视来源、类型、原生 ID 和搜索名称准入。
        """
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
        """
        应用豆瓣影视识别的旧 ABI 元数据回写并报告缺失标题。
        """
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
        """
        把豆瓣影视详情统一投影为媒体信息并记录识别结论。
        """
        label = plan.meta.name if plan.meta else plan.douban_id
        if not info:
            logger.info(f"{label} 未匹配到豆瓣媒体信息")
            return None
        mediainfo = MediaInfo(douban_info=info)
        if plan.meta:
            logger.info(
                f"{plan.meta.name} 豆瓣识别结果：{mediainfo.type.value if mediainfo.type else ''} "
                f"{mediainfo.title_year} {mediainfo.douban_id}"
            )
        else:
            logger.info(
                f"{plan.douban_id} 豆瓣识别结果：{mediainfo.type.value if mediainfo.type else ''} "
                f"{mediainfo.title_year}"
            )
        return mediainfo

    def recognize_media(self, meta: Optional[MetaBase] = None,
                        mtype: Optional[MediaType] = None,
                        media_source: Optional[MediaSource] = None,
                        media_id: Optional[str] = None,
                        **kwargs: Any) -> Optional[Union[MediaInfo, MusicInfo]]:
        """
        识别媒体信息。

        按计划选择显式身份或名称候选，影视识别准备阶段可能更新输入 meta 的类型。

        :param meta: 待识别或搜索的影视、音乐元数据
        :param mtype: 电影或电视剧类型；未指定时由识别计划决定查询顺序
        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :param media_id: 来源原生标识；音乐曲目使用“专辑标识:曲目标识”形式
        :param kwargs: 兼容统一模块入口的扩展参数；按识别入口需要转交，刮削入口当前忽略
        :return: 匹配的标准影视或音乐对象；来源、类型或标识不适用以及未命中时返回 None
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

    async def async_recognize_media(self, meta: Optional[MetaBase] = None,
                                    mtype: Optional[MediaType] = None,
                                    media_source: Optional[MediaSource] = None,
                                    media_id: Optional[str] = None,
                                    **kwargs: Any) -> Optional[Union[MediaInfo, MusicInfo]]:
        """
        识别媒体信息（异步版本）。

        按计划选择显式身份或名称候选，影视识别准备阶段可能更新输入 meta 的类型。

        :param meta: 待识别或搜索的影视、音乐元数据
        :param mtype: 电影或电视剧类型；未指定时由识别计划决定查询顺序
        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :param media_id: 来源原生标识；音乐曲目使用“专辑标识:曲目标识”形式
        :param kwargs: 兼容统一模块入口的扩展参数；按识别入口需要转交，刮削入口当前忽略
        :return: 匹配的标准影视或音乐对象；来源、类型或标识不适用以及未命中时返回 None
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
        """
        返回详情查询的唯一类型顺序，未知类型保持电影优先回退电视剧。
        """
        if mtype == MediaType.TV:
            return (MediaType.TV,)
        if mtype == MediaType.MOVIE:
            return (MediaType.MOVIE,)
        return MediaType.MOVIE, MediaType.TV

    @staticmethod
    def _classify_douban_detail(
            info: Optional[dict[str, Any]]
    ) -> str:
        """
        纯函数分类详情响应为未命中、速率限制或有效详情。
        """
        if not info:
            return "missing"
        if "subject_ip_rate_limit" in info.get("msg", ""):
            return "rate_limited"
        return "matched"

    @classmethod
    def _accept_douban_detail(
            cls, info: Optional[dict[str, Any]]
    ) -> bool:
        """
        应用共享响应分类，并把豆瓣速率限制转换为既有领域异常。
        """
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
        """
        把人物详情合并回原响应，保持现有对象身份和字段覆盖语义。
        """
        if celebrities:
            info["directors"] = celebrities.get("directors")
            info["actors"] = celebrities.get("actors")
        return info

    def _douban_detail(
            self, doubanid: str, mtype: MediaType
    ) -> Optional[dict[str, Any]]:
        """
        执行一次同步详情及人物查询，业务分类由共享状态机负责。
        """
        if mtype == MediaType.TV:
            info = self._require_api().tv_detail(doubanid)
            celebrity_loader = self._require_api().tv_celebrities
        else:
            info = self._require_api().movie_detail(doubanid)
            celebrity_loader = self._require_api().movie_celebrities
        if not self._accept_douban_detail(info):
            return None
        detail = cast(dict[str, Any], info)
        return self._merge_douban_celebrities(
            detail, celebrity_loader(doubanid)
        )

    async def _async_douban_detail(
            self, doubanid: str, mtype: MediaType
    ) -> Optional[dict[str, Any]]:
        """
        执行一次异步详情及人物查询，业务分类由共享状态机负责。
        """
        if mtype == MediaType.TV:
            info = await self._require_api().async_tv_detail(doubanid)
            celebrity_loader = self._require_api().async_tv_celebrities
        else:
            info = await self._require_api().async_movie_detail(doubanid)
            celebrity_loader = self._require_api().async_movie_celebrities
        if not self._accept_douban_detail(info):
            return None
        detail = cast(dict[str, Any], info)
        return self._merge_douban_celebrities(
            detail, await celebrity_loader(doubanid)
        )

    @rate_limit_exponential(source="douban_info")
    def douban_info(
            self, doubanid: str, mtype: Optional[MediaType] = None,
            raise_exception: bool = True,
    ) -> Optional[dict[str, Any]]:
        """
        获取豆瓣信息。

        身份详情未指定类型时先电影后电视剧；名称匹配优先使用 IMDb 标识，再按名称筛选。

        :param doubanid: 豆瓣影视条目原生标识
        :param mtype: 电影或电视剧类型；未指定时由识别计划决定查询顺序
        :param raise_exception: 是否将限流异常继续抛出，由限流装饰器解释
        :return: 豆瓣详情或匹配字典；未命中时返回空值
        """

        if not doubanid:
            return None
        logger.info(f"开始获取豆瓣信息：{doubanid} ...")
        for detail_type in self._douban_detail_order(mtype):
            if info := self._douban_detail(doubanid, detail_type):
                return info
        return None

    @rate_limit_exponential(source="douban_info")
    async def async_douban_info(
            self, doubanid: str, mtype: Optional[MediaType] = None,
            raise_exception: bool = True,
    ) -> Optional[dict[str, Any]]:
        """
        获取豆瓣信息（异步版本）。

        身份详情未指定类型时先电影后电视剧；名称匹配优先使用 IMDb 标识，再按名称筛选。

        :param doubanid: 豆瓣影视条目原生标识
        :param mtype: 电影或电视剧类型；未指定时由识别计划决定查询顺序
        :param raise_exception: 是否将限流异常继续抛出，由限流装饰器解释
        :return: 豆瓣详情或匹配字典；未命中时返回空值
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
        发现豆瓣电影、剧集。

        :param mtype: 电影或电视剧类型；未指定时由识别计划决定查询顺序
        :param sort: 豆瓣排序代码，直接传递给对应接口
        :param tags: 逗号分隔的标签；音乐多标签按原生标识求交集，空值使用“流行”
        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        logger.info(f"开始发现豆瓣 {mtype.value} ...")
        if mtype == MediaType.MOVIE:
            infos = self._require_api().movie_recommend(start=(page - 1) * count, count=count,
                                                   sort=sort, tags=tags)
        else:
            infos = self._require_api().tv_recommend(start=(page - 1) * count, count=count,
                                                sort=sort, tags=tags)
        return self._project_discover(infos)

    async def async_douban_discover(self, mtype: MediaType, sort: str, tags: str,
                                    page: int = 1, count: int = 30) -> Optional[List[MediaInfo]]:
        """
        发现豆瓣电影、剧集（异步版本）。

        :param mtype: 电影或电视剧类型；未指定时由识别计划决定查询顺序
        :param sort: 豆瓣排序代码，直接传递给对应接口
        :param tags: 逗号分隔的标签；音乐多标签按原生标识求交集，空值使用“流行”
        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        logger.info(f"开始发现豆瓣 {mtype.value} ...")
        if mtype == MediaType.MOVIE:
            infos = await self._require_api().async_movie_recommend(start=(page - 1) * count, count=count,
                                                               sort=sort, tags=tags)
        else:
            infos = await self._require_api().async_tv_recommend(start=(page - 1) * count, count=count,
                                                            sort=sort, tags=tags)
        return self._project_discover(infos)

    @staticmethod
    def _project_discover(infos: Optional[dict[str, Any]]) -> List[MediaInfo]:
        """
        过滤豆瓣发现页占位海报并投影为统一媒体列表。
        """
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
        获取正在上映的电影。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = self._require_api().movie_showing(start=(page - 1) * count,
                                             count=count)
        return self._project_collection(infos)

    async def async_movie_showing(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取正在上映的电影（异步版本）。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = await self._require_api().async_movie_showing(start=(page - 1) * count,
                                                         count=count)
        return self._project_collection(infos)

    def tv_weekly_chinese(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑国产剧。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = self._require_api().tv_chinese_best_weekly(start=(page - 1) * count,
                                                      count=count)
        return self._project_collection(infos)

    async def async_tv_weekly_chinese(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑国产剧（异步版本）。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = await self._require_api().async_tv_chinese_best_weekly(start=(page - 1) * count,
                                                                  count=count)
        return self._project_collection(infos)

    def tv_weekly_global(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑外国剧。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = self._require_api().tv_global_best_weekly(start=(page - 1) * count,
                                                     count=count)
        return self._project_collection(infos)

    async def async_tv_weekly_global(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑外国剧（异步版本）。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = await self._require_api().async_tv_global_best_weekly(start=(page - 1) * count,
                                                                 count=count)
        return self._project_collection(infos)

    def tv_animation(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣动画剧。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = self._require_api().tv_animation(start=(page - 1) * count,
                                            count=count)
        return self._project_collection(infos)

    async def async_tv_animation(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣动画剧（异步版本）。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = await self._require_api().async_tv_animation(start=(page - 1) * count,
                                                        count=count)
        return self._project_collection(infos)

    def movie_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门电影。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = self._require_api().movie_hot_gaia(start=(page - 1) * count,
                                              count=count)
        return self._project_collection(infos)

    async def async_movie_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门电影（异步版本）。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = await self._require_api().async_movie_hot_gaia(start=(page - 1) * count,
                                                          count=count)
        return self._project_collection(infos)

    def tv_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门剧集。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = self._require_api().tv_hot(start=(page - 1) * count,
                                      count=count)
        return self._project_collection(infos)

    async def async_tv_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门剧集（异步版本）。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = await self._require_api().async_tv_hot(start=(page - 1) * count,
                                                  count=count)
        return self._project_collection(infos)

    @staticmethod
    def _project_collection(
            infos: Optional[dict[str, Any]],
    ) -> List[MediaInfo]:
        """
        把豆瓣榜单或集合响应投影为统一媒体列表。
        """
        return [
            MediaInfo(douban_info=info)
            for info in (infos or {}).get("subject_collection_items") or []
        ]

    def search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息。

        :param meta: 待识别或搜索的影视、音乐元数据
        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :return: 标准媒体或人物列表；无结果时为空列表，来源不适用时返回 None
        """
        plan = self._search_plan(meta.name if meta else None, media_source)
        if not plan.enabled:
            return None
        if not plan.query:
            return []
        result = self._require_api().search(plan.query)
        return self._project_media_search(meta, result)

    async def async_search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息（异步版本）。

        :param meta: 待识别或搜索的影视、音乐元数据
        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :return: 标准媒体或人物列表；无结果时为空列表，来源不适用时返回 None
        """
        plan = self._search_plan(meta.name if meta else None, media_source)
        if not plan.enabled:
            return None
        if not plan.query:
            return []
        result = await self._require_api().async_search(plan.query)
        return self._project_media_search(meta, result)

    def search_persons(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息。

        :param name: 待搜索的标题或人物名称
        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :return: 标准媒体或人物列表；无结果时为空列表，来源不适用时返回 None
        """
        plan = self._search_plan(name, media_source)
        if not plan.enabled:
            return None
        if not plan.query:
            return []
        result = self._require_api().person_search(keyword=plan.query)
        return self._project_person_search(plan.query, result)

    async def async_search_persons(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息（异步版本）。

        :param name: 待搜索的标题或人物名称
        :param media_source: 本次请求允许的数据源；与对应豆瓣影视或音乐来源不符时不处理
        :return: 标准媒体或人物列表；无结果时为空列表，来源不适用时返回 None
        """
        plan = self._search_plan(name, media_source)
        if not plan.enabled:
            return None
        if not plan.query:
            return []
        result = await self._require_api().async_person_search(keyword=plan.query)
        return self._project_person_search(plan.query, result)

    @staticmethod
    def _search_plan(
            query: Optional[str],
            media_source: Optional[MediaSourceSelection],
    ) -> _DoubanSearchPlan:
        """
        统一决定豆瓣搜索是否响应并规范化查询文本。
        """
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
        """
        把豆瓣媒体搜索响应交给统一过滤与季信息投影。
        """
        return cls._build_search_medias_result(
            meta, (result or {}).get("items")
        )

    @staticmethod
    def _project_person_search(
            name: str,
            result: Optional[dict[str, Any]],
    ) -> List[MediaPerson]:
        """
        把豆瓣人物搜索响应过滤并投影为统一人物列表。

        :param name: 用于筛选人物标题的查询文本
        :param result: 豆瓣人物搜索响应
        :return: 图片尺寸已转换为 URL 的匹配人物列表
        """
        persons = []
        for item in (result or {}).get("items") or []:
            target = item.get("target") or {}
            title = target.get("title") or ""
            if name not in title:
                continue
            # 豆瓣尺寸图片可能包含宽高信息，统一模型只接收 URL；兼容已有字符串格式
            images = {
                size: image.get("url") if isinstance(image, dict) else image
                for size, image in (target.get("cover") or {}).items()
            }
            persons.append(MediaPerson(source="douban", **{
                "id": item.get("target_id"),
                "name": title,
                "url": target.get("url"),
                "images": images,
                "avatar": (target.get("cover_img", {}).get("url") or "").replace(
                    "/l/public/", "/s/public/"
                ),
            }))
        return persons

    @staticmethod
    def _process_imdbid_result(
            result: Optional[dict[str, Any]], imdbid: str,
    ) -> Optional[dict[str, Any]]:
        """
        处理IMDBID查询结果。

        :param result: IMDBID查询返回的结果
        :param imdbid: 用于关联查询结果的 IMDb 媒体标识
        :return: 处理后的结果，None表示无结果
        """
        if result:
            doubanid = result.get("id")
            if doubanid:
                if not str(doubanid).isdigit():
                    match = re.search(r"\d+", str(doubanid))
                    if not match:
                        return None
                    doubanid = match.group(0)
                    result["id"] = doubanid
                logger.info(f"{imdbid} 查询到豆瓣信息：{result.get('title')}")
                return result
            return None
        return None

    @staticmethod
    def _process_search_results(
            result: Optional[dict[str, Any]], name: str,
            mtype: Optional[MediaType] = None, year: Optional[str] = None,
            season: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        处理搜索结果并进行匹配。

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

        for item_obj in result.get("items") or []:
            type_name = item_obj.get("type_name")
            if type_name not in [MediaType.TV.value, MediaType.MOVIE.value]:
                continue
            if mtype and mtype.value != type_name:
                continue
            if mtype and mtype == MediaType.TV and season is None:
                season = 1
            item = item_obj.get("target")
            if not isinstance(item, dict):
                continue
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
    def match_doubaninfo(self, name: str, imdbid: Optional[str] = None,
                         mtype: Optional[MediaType] = None, year: Optional[str] = None, season: Optional[int] = None,
                         raise_exception: bool = False) -> dict[str, Any]:
        """
        搜索和匹配豆瓣信息。

        身份详情未指定类型时先电影后电视剧；名称匹配优先使用 IMDb 标识，再按名称筛选。

        :param name: 待搜索的标题或人物名称
        :param imdbid: 可选 IMDb 标识；提供时优先用于识别
        :param mtype: 电影或电视剧类型；未指定时由识别计划决定查询顺序
        :param year: 作品年份筛选，未指定时不限制
        :param season: 目标季号；None 表示整剧，0 可表示特别篇
        :param raise_exception: 是否将限流异常继续抛出，由限流装饰器解释
        :return: 豆瓣详情或匹配字典；未命中时返回空值
        """
        if imdbid:
            # 优先使用IMDBID查询
            logger.info(f"开始使用IMDBID {imdbid} 查询豆瓣信息 ...")
            result = self._require_api().imdbid(imdbid)
            processed_result = self._process_imdbid_result(result, imdbid)
            if processed_result:
                return processed_result

        logger.info(f"开始使用名称 {name} 匹配豆瓣信息 ...")
        result = self._require_api().search(f"{name} {year or ''}".strip())
        return self._process_search_results(result, name, mtype, year, season)

    @retry(Exception, 5, 3, 3, logger=logger)
    @rate_limit_exponential(source="match_doubaninfo")
    async def async_match_doubaninfo(self, name: str, imdbid: Optional[str] = None,
                                     mtype: Optional[MediaType] = None, year: Optional[str] = None, season: Optional[int] = None,
                                     raise_exception: bool = False) -> dict[str, Any]:
        """
        搜索和匹配豆瓣信息（异步版本）。

        身份详情未指定类型时先电影后电视剧；名称匹配优先使用 IMDb 标识，再按名称筛选。

        :param name: 待搜索的标题或人物名称
        :param imdbid: 可选 IMDb 标识；提供时优先用于识别
        :param mtype: 电影或电视剧类型；未指定时由识别计划决定查询顺序
        :param year: 作品年份筛选，未指定时不限制
        :param season: 目标季号；None 表示整剧，0 可表示特别篇
        :param raise_exception: 是否将限流异常继续抛出，由限流装饰器解释
        :return: 豆瓣详情或匹配字典；未命中时返回空值
        """
        if imdbid:
            # 优先使用IMDBID查询
            logger.info(f"开始使用IMDBID {imdbid} 查询豆瓣信息 ...")
            result = await self._require_api().async_imdbid(imdbid)
            processed_result = self._process_imdbid_result(result, imdbid)
            if processed_result:
                return processed_result

        logger.info(f"开始使用名称 {name} 匹配豆瓣信息 ...")
        result = await self._require_api().async_search(f"{name} {year or ''}".strip())
        return self._process_search_results(result, name, mtype, year, season)

    def movie_top250(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣电影TOP250。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = self._require_api().movie_top250(start=(page - 1) * count,
                                            count=count)
        return self._project_collection(infos)

    async def async_movie_top250(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣电影TOP250（异步版本）。

        :param page: 请求页码，从 1 开始
        :param count: 本次最多返回的条目数
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        infos = await self._require_api().async_movie_top250(start=(page - 1) * count,
                                                        count=count)
        return self._project_collection(infos)

    def metadata_nfo(
            self, mediainfo: MediaInfo, season: Optional[int] = None,
            **kwargs: Any,
    ) -> Optional[str]:
        """
        获取包含 UTF-8 编码声明的 NFO XML 文本。

        :param mediainfo: 已识别媒体信息，可带自定义刮削来源
        :param season: 目标季号；None 表示整剧，0 可表示特别篇
        :param kwargs: 兼容统一模块入口的扩展参数；按识别入口需要转交，刮削入口当前忽略
        :return: 包含 UTF-8 编码声明的 NFO XML 文本；未选择豆瓣刮削时返回 None
        """
        if (mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')) != "douban":
            return None
        return self._require_scraper().get_metadata_nfo(mediainfo=mediainfo, season=season)

    def metadata_img(
            self, mediainfo: MediaInfo, season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """
        获取图片名称和url。

        :param mediainfo: 已识别媒体信息，可带自定义刮削来源
        :param season: 目标季号；None 表示整剧，0 可表示特别篇
        :param episode: 目标集号；豆瓣刮削不提供单集图片
        :return: 主海报和背景图的文件名到地址映射；季集请求返回空字典，非豆瓣刮削返回 None
        """
        if (mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')) != "douban":
            return None
        return self._require_scraper().get_metadata_img(mediainfo=mediainfo, season=season, episode=episode)

    @staticmethod
    def _validate_douban_obtain_images_params(mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        仅在背景图已存在时短路图片补充流程。

        本辅助函数的 None 不能区分来源不符、身份缺失与确实缺图。

        :param mediainfo: 待补图媒体对象
        :return: 背景图已存在且来源满足条件时返回原对象；其他情况返回 None，交由调用方继续查询
        """
        if mediainfo.media_source != MediaSource.Douban and get_runtime_setting('RECOGNIZE_SOURCE') != "douban":
            return mediainfo
        if not mediainfo.douban_id:
            return mediainfo
        if mediainfo.backdrop_path:
            return mediainfo
        return None

    @staticmethod
    def _process_douban_images(
            mediainfo: MediaInfo, info: Optional[dict[str, Any]],
    ) -> MediaInfo:
        """
        处理豆瓣图片数据。

        :param mediainfo: 媒体信息
        :param info: 图片信息
        :return: 更新后的媒体信息
        """
        if not info:
            return mediainfo
        images = info.get("photos")
        if images:
            backdrop = images[0].get("image", {}).get("large") or {}
            if backdrop:
                mediainfo.backdrop_path = backdrop.get("url")
        return mediainfo

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片。

        :param mediainfo: 已识别媒体信息，可带自定义刮削来源
        :return: 原媒体对象，背景路径可能已原地补齐；没有可用剧照时保留原值
        """
        result = self._validate_douban_obtain_images_params(mediainfo)
        if result is not None:
            return result

        douban_id = mediainfo.douban_id
        if not douban_id:
            return mediainfo
        if mediainfo.type == MediaType.MOVIE:
            info = self._require_api().movie_photos(douban_id)
        else:
            info = self._require_api().tv_photos(douban_id)

        return self._process_douban_images(mediainfo, info)

    async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片（异步版本）。

        :param mediainfo: 已识别媒体信息，可带自定义刮削来源
        :return: 原媒体对象，背景路径可能已原地补齐；没有可用剧照时保留原值
        """
        result = self._validate_douban_obtain_images_params(mediainfo)
        if result is not None:
            return result

        douban_id = mediainfo.douban_id
        if not douban_id:
            return mediainfo
        if mediainfo.type == MediaType.MOVIE:
            info = await self._require_api().async_movie_photos(douban_id)
        else:
            info = await self._require_api().async_tv_photos(douban_id)

        return self._process_douban_images(mediainfo, info)

    def clear_cache(self) -> None:
        """
        清除缓存。
        """
        logger.info("开始清除豆瓣缓存 ...")
        self._require_api().clear_cache()
        logger.info("豆瓣缓存清除完成")

    def douban_movie_credits(self, doubanid: str) -> List[_SchemaMediaPerson]:
        """
        根据豆瓣ID查询电影演员列表。

        只转换响应中的 actors 字段，不包含导演等其他演职人员。

        :param doubanid: 豆瓣影视条目原生标识
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        result = self._require_api().movie_celebrities(subject_id=doubanid)
        return self._process_celebrity_data(result)

    def douban_tv_credits(self, doubanid: str) -> List[_SchemaMediaPerson]:
        """
        根据豆瓣ID查询电视剧演员列表。

        只转换响应中的 actors 字段，不包含导演等其他演职人员。

        :param doubanid: 豆瓣影视条目原生标识
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        result = self._require_api().tv_celebrities(subject_id=doubanid)
        return self._process_celebrity_data(result)

    def douban_movie_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电影。

        :param doubanid: 豆瓣影视条目原生标识
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        recommend = self._require_api().movie_recommendations(subject_id=doubanid)
        return self._process_recommendations(recommend)

    def douban_tv_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电视剧。

        :param doubanid: 豆瓣影视条目原生标识
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        recommend = self._require_api().tv_recommendations(subject_id=doubanid)
        return self._process_recommendations(recommend)

    @staticmethod
    def _process_recommendations(
            recommend: Optional[Union[dict[str, Any], list[dict[str, Any]]]],
    ) -> List[MediaInfo]:
        """
        将豆瓣推荐列表或携带 items 的响应投影为媒体列表。

        :param recommend: 推荐接口原生列表或兼容的分页字典；无响应时为 None
        :return: 保持原顺序的媒体列表，跳过非字典条目且不修改缓存响应
        """
        items = (recommend.get("items") or []) if isinstance(recommend, dict) else (recommend or [])
        return [MediaInfo(douban_info=info) for info in items if isinstance(info, dict)]

    @staticmethod
    def _process_comment_author(doubaninfo: Optional[dict[str, Any]]) -> Optional[_SchemaMediaCommentAuthor]:
        """
        把豆瓣用户信息投影为评论作者。

        :param doubaninfo: 豆瓣用户信息
        :return: 评论作者，缺少用户信息时返回 None
        """
        if not doubaninfo:
            return None
        return _SchemaMediaCommentAuthor(
            id=str(doubaninfo.get("id")) if doubaninfo.get("id") else None,
            name=doubaninfo.get("name"),
            avatar=doubaninfo.get("avatar"),
            url=doubaninfo.get("url"),
        )

    @staticmethod
    def _process_comment_rating(doubaninfo: Optional[dict[str, Any]]) -> Optional[float]:
        """
        提取豆瓣五分制评分。

        :param doubaninfo: 豆瓣评分信息
        :return: 五星制评分，未评分时返回 None
        """
        if not doubaninfo:
            return None
        value = doubaninfo.get("value")
        return float(value) if value else None

    def _process_interests(self, result: Optional[dict[str, Any]]) -> _SchemaMediaCommentPage:
        """
        把豆瓣短评响应投影为分页结果。

        :param result: 豆瓣短评响应
        :return: 短评分页结果
        """
        if not result:
            return _SchemaMediaCommentPage()
        items = [
            _SchemaMediaComment(
                id=str(item.get("id")) if item.get("id") else None,
                comment=item.get("comment"),
                rating=self._process_comment_rating(item.get("rating")),
                vote_count=item.get("vote_count"),
                create_time=item.get("create_time"),
                author=self._process_comment_author(item.get("user")),
                url=item.get("sharing_url"),
            )
            for item in result.get("interests") or []
        ]
        return _SchemaMediaCommentPage(
            start=result.get("start") or 0,
            count=result.get("count") or len(items),
            total=result.get("total") or len(items),
            items=items,
        )

    def _process_reviews(self, result: Optional[dict[str, Any]]) -> _SchemaMediaReviewPage:
        """
        把豆瓣影评响应投影为分页结果。

        :param result: 豆瓣影评响应
        :return: 影评分页结果
        """
        if not result:
            return _SchemaMediaReviewPage()
        items = [
            _SchemaMediaReview(
                id=str(item.get("id")) if item.get("id") else None,
                title=item.get("title"),
                abstract=item.get("abstract"),
                rating=self._process_comment_rating(item.get("rating")),
                useful_count=item.get("useful_count"),
                comments_count=item.get("comments_count"),
                create_time=item.get("create_time"),
                author=self._process_comment_author(item.get("user")),
                url=item.get("url") or item.get("sharing_url"),
            )
            for item in result.get("reviews") or []
        ]
        return _SchemaMediaReviewPage(
            start=result.get("start") or 0,
            count=result.get("count") or len(items),
            total=result.get("total") or len(items),
            items=items,
        )

    def douban_movie_interests(self, doubanid: str, start: int = 0, count: int = 20) -> _SchemaMediaCommentPage:
        """
        根据豆瓣ID查询电影短评。

        :param doubanid: 豆瓣影视条目原生标识
        :param start: 从 0 开始的条目偏移量
        :param count: 本次最多返回的条目数
        :return: 短评分页对象；没有响应时返回空分页对象
        """
        result = self._require_api().movie_interests(subject_id=doubanid, start=start, count=count)
        return self._process_interests(result)

    def douban_tv_interests(self, doubanid: str, start: int = 0, count: int = 20) -> _SchemaMediaCommentPage:
        """
        根据豆瓣ID查询电视剧短评。

        :param doubanid: 豆瓣影视条目原生标识
        :param start: 从 0 开始的条目偏移量
        :param count: 本次最多返回的条目数
        :return: 短评分页对象；没有响应时返回空分页对象
        """
        result = self._require_api().tv_interests(subject_id=doubanid, start=start, count=count)
        return self._process_interests(result)

    def douban_movie_reviews(self, doubanid: str, start: int = 0, count: int = 20) -> _SchemaMediaReviewPage:
        """
        根据豆瓣ID查询电影影评。

        :param doubanid: 豆瓣影视条目原生标识
        :param start: 从 0 开始的条目偏移量
        :param count: 本次最多返回的条目数
        :return: 影评分页对象；没有响应时返回空分页对象
        """
        result = self._require_api().movie_reviews(subject_id=doubanid, start=start, count=count)
        return self._process_reviews(result)

    def douban_tv_reviews(self, doubanid: str, start: int = 0, count: int = 20) -> _SchemaMediaReviewPage:
        """
        根据豆瓣ID查询电视剧影评。

        :param doubanid: 豆瓣影视条目原生标识
        :param start: 从 0 开始的条目偏移量
        :param count: 本次最多返回的条目数
        :return: 影评分页对象；没有响应时返回空分页对象
        """
        result = self._require_api().tv_reviews(subject_id=doubanid, start=start, count=count)
        return self._process_reviews(result)

    def douban_person_detail(self, person_id: int) -> _SchemaMediaPerson:
        """
        获取人物详细信息。

        :param person_id: 豆瓣人物原生标识
        :return: 豆瓣人物对象；没有详情时返回仅带豆瓣来源的空人物对象
        """
        detail = self._require_api().person_detail(person_id)
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
        按豆瓣人物标识查询参演作品。

        先查询人物所属作品集合，再按每页 20 条读取集合内容。

        :param person_id: 豆瓣人物原生标识
        :param page: 请求页码，从 1 开始
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        personinfo = self._require_api().person_detail(person_id)
        if not personinfo:
            return []
        collection_id = None
        for module in personinfo.get("modules") or []:
            if module.get("type") == "work_collections":
                collection_id = module.get("payload", {}).get("id")
        if collection_id:
            collections = self._require_api().person_work(subject_id=collection_id, start=(page - 1) * 20, count=20)
            if collections:
                works = collections.get("works") or []
                return [MediaInfo(douban_info=work.get("subject")) for work in works]
        return []

    @staticmethod
    def _process_celebrity_data(
            result: Optional[dict[str, Any]],
    ) -> List[_SchemaMediaPerson]:
        """
        处理演职员表数据的公共方法。

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
        根据豆瓣ID查询电影演员列表（异步版本）。

        只转换响应中的 actors 字段，不包含导演等其他演职人员。

        :param doubanid: 豆瓣影视条目原生标识
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        result = await self._require_api().async_movie_celebrities(subject_id=doubanid)
        return self._process_celebrity_data(result)

    async def async_douban_tv_credits(self, doubanid: str) -> List[_SchemaMediaPerson]:
        """
        根据豆瓣ID查询电视剧演员列表（异步版本）。

        只转换响应中的 actors 字段，不包含导演等其他演职人员。

        :param doubanid: 豆瓣影视条目原生标识
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        result = await self._require_api().async_tv_celebrities(subject_id=doubanid)
        return self._process_celebrity_data(result)

    async def async_douban_movie_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电影（异步版本）。

        :param doubanid: 豆瓣影视条目原生标识
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        recommend = await self._require_api().async_movie_recommendations(subject_id=doubanid)
        return self._process_recommendations(recommend)

    async def async_douban_tv_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电视剧（异步版本）。

        :param doubanid: 豆瓣影视条目原生标识
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        recommend = await self._require_api().async_tv_recommendations(subject_id=doubanid)
        return self._process_recommendations(recommend)

    async def async_douban_movie_interests(self, doubanid: str, start: int = 0,
                                           count: int = 20) -> _SchemaMediaCommentPage:
        """
        根据豆瓣ID查询电影短评（异步版本）。

        :param doubanid: 豆瓣影视条目原生标识
        :param start: 从 0 开始的条目偏移量
        :param count: 本次最多返回的条目数
        :return: 短评分页对象；没有响应时返回空分页对象
        """
        result = await self._require_api().async_movie_interests(subject_id=doubanid, start=start, count=count)
        return self._process_interests(result)

    async def async_douban_tv_interests(self, doubanid: str, start: int = 0,
                                        count: int = 20) -> _SchemaMediaCommentPage:
        """
        根据豆瓣ID查询电视剧短评（异步版本）。

        :param doubanid: 豆瓣影视条目原生标识
        :param start: 从 0 开始的条目偏移量
        :param count: 本次最多返回的条目数
        :return: 短评分页对象；没有响应时返回空分页对象
        """
        result = await self._require_api().async_tv_interests(subject_id=doubanid, start=start, count=count)
        return self._process_interests(result)

    async def async_douban_movie_reviews(self, doubanid: str, start: int = 0,
                                         count: int = 20) -> _SchemaMediaReviewPage:
        """
        根据豆瓣ID查询电影影评（异步版本）。

        :param doubanid: 豆瓣影视条目原生标识
        :param start: 从 0 开始的条目偏移量
        :param count: 本次最多返回的条目数
        :return: 影评分页对象；没有响应时返回空分页对象
        """
        result = await self._require_api().async_movie_reviews(subject_id=doubanid, start=start, count=count)
        return self._process_reviews(result)

    async def async_douban_tv_reviews(self, doubanid: str, start: int = 0,
                                      count: int = 20) -> _SchemaMediaReviewPage:
        """
        根据豆瓣ID查询电视剧影评（异步版本）。

        :param doubanid: 豆瓣影视条目原生标识
        :param start: 从 0 开始的条目偏移量
        :param count: 本次最多返回的条目数
        :return: 影评分页对象；没有响应时返回空分页对象
        """
        result = await self._require_api().async_tv_reviews(subject_id=doubanid, start=start, count=count)
        return self._process_reviews(result)

    async def async_douban_person_detail(self, person_id: int) -> _SchemaMediaPerson:
        """
        获取人物详细信息（异步版本）。

        :param person_id: 豆瓣人物原生标识
        :return: 豆瓣人物对象；没有详情时返回仅带豆瓣来源的空人物对象
        """
        detail = await self._require_api().async_person_detail(person_id)
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
        按豆瓣人物标识查询参演作品。

        先查询人物所属作品集合，再按每页 20 条读取集合内容。

        :param person_id: 豆瓣人物原生标识
        :param page: 请求页码，从 1 开始
        :return: 当前页或本次查询的标准对象列表；无结果时为空列表
        """
        personinfo = await self._require_api().async_person_detail(person_id)
        if not personinfo:
            return []
        collection_id = None
        for module in personinfo.get("modules") or []:
            if module.get("type") == "work_collections":
                collection_id = module.get("payload", {}).get("id")
        if collection_id:
            collections = await self._require_api().async_person_work(subject_id=collection_id, start=(page - 1) * 20,
                                                                 count=20)
            if collections:
                works = collections.get("works") or []
                return [MediaInfo(douban_info=work.get("subject")) for work in works]
        return []
