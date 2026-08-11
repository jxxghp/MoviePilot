import re
from typing import Any, List, Optional, Tuple, Union

import cn2an

from app import schemas
from app.core.config import settings
from app.core.context import MUSIC_ENTITY_ALBUM, MediaInfo, MusicAlbumInfo, MusicInfo
from app.core.meta import MetaBase, MetaMusic
from app.core.metainfo import MetaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.douban.apiv2 import DoubanApi
from app.modules.douban.scraper import DoubanScraper
from app.schemas import MediaPerson, APIRateLimitException
from app.schemas.types import MediaType, ModuleType, MediaRecognizeType
from app.utils.common import retry
from app.utils.http import RequestUtils
from app.utils.limit import rate_limit_exponential
from app.utils.media import is_media_source_enabled, is_media_source_selected
from app.utils.zhconv import convert as zhconv_convert


class DoubanModule(_ModuleBase):
    """提供豆瓣影视与豆瓣音乐元数据识别能力。"""

    _music_source = "doubanmusic"
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
    def get_music_source() -> str:
        """返回多源音乐识别使用的数据源标识。"""
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
            source: Optional[str] = None,
    ) -> Optional[List[MusicInfo]]:
        """按请求来源搜索豆瓣音乐专辑，并转换为统一音乐候选。"""
        if not is_media_source_selected(source, self._music_source):
            return None
        keyword = meta.album or meta.title
        if not keyword:
            return []
        result = self.doubanapi.music_search(keyword=keyword, count=max(1, min(limit, 100)))
        return self._build_music_search_results(result)

    def recognize_music(self, source: str, media_id: str) -> Optional[MusicInfo]:
        """按豆瓣音乐原生 ID 获取专辑或专辑内曲目详情。"""
        if source != self._music_source or not media_id:
            return None
        album_id, separator, track_id = str(media_id).partition(":")
        album = self.music_album(source, album_id)
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

    def music_album(self, source: str, media_id: str) -> Optional[MusicAlbumInfo]:
        """按豆瓣音乐专辑 ID 获取标准化专辑详情和曲目。"""
        if source != self._music_source or not media_id:
            return None
        info = self.doubanapi.music_detail(subject_id=str(media_id))
        return self._douban_music_to_album(info) if info else None

    def _recognize_music_media(
            self,
            meta: Optional[MetaMusic],
            source: Optional[str],
            mediaid: Optional[str],
    ) -> Optional[MusicInfo]:
        """执行豆瓣音乐详情识别或按专辑名称匹配。"""
        if source != self._music_source:
            return None
        resolved_media_id = mediaid or (meta.media_id if meta else None)
        if resolved_media_id:
            return self.recognize_music(source, str(resolved_media_id))
        if not meta:
            return None
        candidates = self.search_music(meta=meta, limit=20, source=source) or []
        expected_title = meta.album or meta.title
        for candidate in candidates:
            if not self._same_music_text(expected_title, candidate.title):
                continue
            if meta.artists and candidate.artists and not any(
                self._same_music_text(expected, actual)
                for expected in meta.artists
                for actual in candidate.artists
            ):
                continue
            if meta.album and meta.title:
                album = self.music_album(source, candidate.media_id)
                matched_track = self._select_douban_music_track(meta, album)
                if matched_track:
                    return matched_track
                continue
            return candidate
        return None

    async def _async_recognize_music_media(
            self,
            meta: Optional[MetaMusic],
            source: Optional[str],
            mediaid: Optional[str],
    ) -> Optional[MusicInfo]:
        """异步执行豆瓣音乐详情识别或按专辑名称匹配。"""
        if source != self._music_source:
            return None
        resolved_media_id = mediaid or (meta.media_id if meta else None)
        if resolved_media_id:
            album_id, separator, track_id = str(resolved_media_id).partition(":")
            info = await self.doubanapi.async_music_detail(subject_id=album_id)
            album = self._douban_music_to_album(info) if info else None
            if not album:
                return None
            if separator and track_id:
                return next(
                    (
                        track for track in album.tracks
                        if track.media_id == resolved_media_id
                        or str(track.track_number or "") == track_id
                    ),
                    None,
                )
            return album.to_music_info()
        if not meta:
            return None
        keyword = meta.album or meta.title
        if not keyword:
            return None
        result = await self.doubanapi.async_music_search(keyword=keyword, count=20)
        candidates = self._build_music_search_results(result)
        expected_title = meta.album or meta.title
        for candidate in candidates:
            if not self._same_music_text(expected_title, candidate.title):
                continue
            if meta.artists and candidate.artists and not any(
                self._same_music_text(expected, actual)
                for expected in meta.artists
                for actual in candidate.artists
            ):
                continue
            if meta.album and meta.title:
                info = await self.doubanapi.async_music_detail(
                    subject_id=str(candidate.media_id)
                )
                album = self._douban_music_to_album(info) if info else None
                matched_track = self._select_douban_music_track(meta, album)
                if matched_track:
                    return matched_track
                continue
            return candidate
        return None

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
    def _build_music_search_results(cls, result: Optional[dict]) -> List[MusicInfo]:
        """把豆瓣音乐搜索响应转换为专辑候选列表。"""
        items = (result or {}).get("items") or (result or {}).get("musics") or []
        candidates = []
        for item in items:
            if not isinstance(item, dict):
                continue
            target_type = str(item.get("target_type") or item.get("type") or "").casefold()
            target = item.get("target") if isinstance(item.get("target"), dict) else item
            type_name = str(target.get("type_name") or target.get("subtype") or "")
            if target_type and target_type not in {"music", "音乐"}:
                continue
            if type_name and type_name not in {"音乐", "music"}:
                continue
            media_id = cls._douban_music_text(
                target.get("id") or item.get("target_id") or item.get("id")
            )
            title = cls._douban_music_text(target.get("title") or target.get("name"))
            if not media_id or not title:
                continue
            artists = cls._douban_music_artists(target)
            release_date = cls._douban_music_date(target)
            cover_url = cls._douban_music_cover(target)
            candidate = MusicInfo(
                source=cls._music_source,
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
        attrs = info.get("attrs") if isinstance(info.get("attrs"), dict) else {}
        artists = cls._douban_music_artists(info)
        release_date = cls._douban_music_date(info)
        tags = [
            cls._douban_music_text(item.get("name") if isinstance(item, dict) else item)
            for item in (info.get("tags") or [])
        ]
        genres = [str(item) for item in info.get("genres") or [] if item]
        rating = info.get("rating") if isinstance(info.get("rating"), dict) else {}
        album = MusicAlbumInfo(
            source=cls._music_source,
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
        attrs = info.get("attrs") if isinstance(info.get("attrs"), dict) else {}
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
                source=cls._music_source,
                # 豆瓣歌曲没有独立 subject ID，使用专辑内绝对顺序避免多碟曲序重复。
                media_id=f"{album.media_id}:{index}",
                title=title,
                artists=artists,
                album=album.title,
                album_artist=album.artist or None,
                album_id=album.media_id,
                album_type=album.album_type,
                year=album.year,
                release_date=album.release_date,
                disc_number=disc_number,
                track_number=track_number,
                duration=duration,
                cover_url=cover_url,
                genres=list(album.genres),
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
        attrs = info.get("attrs") if isinstance(info.get("attrs"), dict) else {}
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
        attrs = info.get("attrs") if isinstance(info.get("attrs"), dict) else {}
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
        if not doubanid and not meta:
            return None

        if (
            meta
            and not doubanid
            and (kwargs.get("source") or settings.RECOGNIZE_SOURCE) != "douban"
        ):
            return None

        if doubanid:
            info = douban_info_func(
                doubanid=doubanid,
                mtype=mtype or (meta.type if meta else None),
            )
        elif not meta.name:
            logger.error("识别媒体信息时未提供元数据名称")
            return None
        else:
            if mtype:
                meta.type = mtype
            info = {}
            for name in self._prepare_search_names(meta):
                if meta.begin_season is not None:
                    logger.info(f"正在识别 {name} 第{meta.begin_season}季 ...")
                else:
                    logger.info(f"正在识别 {name} ...")
                match_info = match_doubaninfo_func(
                    name=name,
                    mtype=mtype or meta.type,
                    year=meta.year,
                    season=meta.begin_season,
                )
                if match_info:
                    info = douban_info_func(
                        doubanid=match_info.get("id"),
                        mtype=mtype or meta.type,
                    )
                    if info:
                        break

        if info:
            mediainfo = MediaInfo(douban_info=info)
            if meta:
                logger.info(f"{meta.name} 豆瓣识别结果：{mediainfo.type.value} "
                            f"{mediainfo.title_year} "
                            f"{mediainfo.douban_id}")
            else:
                logger.info(f"{doubanid} 豆瓣识别结果：{mediainfo.type.value} "
                            f"{mediainfo.title_year}")
            return mediainfo
        else:
            logger.info(f"{meta.name if meta else doubanid} 未匹配到豆瓣媒体信息")

        return None

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
        if not doubanid and not meta:
            return None

        if (
            meta
            and not doubanid
            and (kwargs.get("source") or settings.RECOGNIZE_SOURCE) != "douban"
        ):
            return None

        if doubanid:
            info = await async_douban_info_func(
                doubanid=doubanid,
                mtype=mtype or (meta.type if meta else None),
            )
        elif not meta.name:
            logger.error("识别媒体信息时未提供元数据名称")
            return None
        else:
            if mtype:
                meta.type = mtype
            info = {}
            for name in self._prepare_search_names(meta):
                if meta.begin_season is not None:
                    logger.info(f"正在识别 {name} 第{meta.begin_season}季 ...")
                else:
                    logger.info(f"正在识别 {name} ...")
                match_info = await async_match_doubaninfo_func(
                    name=name,
                    mtype=mtype or meta.type,
                    year=meta.year,
                    season=meta.begin_season,
                )
                if match_info:
                    info = await async_douban_info_func(
                        doubanid=match_info.get("id"),
                        mtype=mtype or meta.type,
                    )
                    if info:
                        break

        if info:
            mediainfo = MediaInfo(douban_info=info)
            if meta:
                logger.info(f"{meta.name} 豆瓣识别结果：{mediainfo.type.value} "
                            f"{mediainfo.title_year} "
                            f"{mediainfo.douban_id}")
            else:
                logger.info(f"{doubanid} 豆瓣识别结果：{mediainfo.type.value} "
                            f"{mediainfo.title_year}")
            return mediainfo
        else:
            logger.info(f"{meta.name if meta else doubanid} 未匹配到豆瓣媒体信息")

        return None

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        doubanid: Optional[str] = None,
                        **kwargs) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与doubanid配套
        :param doubanid: 豆瓣ID
        :return: 识别的媒体信息，包括剧集信息
        """
        source = kwargs.get("source")
        if source == self._music_source:
            return self._recognize_music_media(
                meta=meta if isinstance(meta, MetaMusic) else None,
                source=source,
                mediaid=kwargs.get("mediaid"),
            )
        # 音乐请求必须显式使用 doubanmusic，避免与影视豆瓣源混淆。
        if isinstance(meta, MetaMusic) or mtype == MediaType.MUSIC:
            return None
        return self._recognize_media_core(
            meta=meta,
            mtype=mtype,
            doubanid=doubanid,
            douban_info_func=self.douban_info,
            match_doubaninfo_func=self.match_doubaninfo,
            **kwargs
        )

    async def async_recognize_media(self, meta: MetaBase = None,
                                    mtype: MediaType = None,
                                    doubanid: Optional[str] = None,
                                    **kwargs) -> Optional[MediaInfo]:
        """
        识别媒体信息（异步版本）
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与doubanid配套
        :param doubanid: 豆瓣ID
        :return: 识别的媒体信息，包括剧集信息
        """
        source = kwargs.get("source")
        if source == self._music_source:
            return await self._async_recognize_music_media(
                meta=meta if isinstance(meta, MetaMusic) else None,
                source=source,
                mediaid=kwargs.get("mediaid"),
            )
        # 音乐请求必须显式使用 doubanmusic，避免与影视豆瓣源混淆。
        if isinstance(meta, MetaMusic) or mtype == MediaType.MUSIC:
            return None
        return await self._async_recognize_media_core(
            meta=meta,
            mtype=mtype,
            doubanid=doubanid,
            async_douban_info_func=self.async_douban_info,
            async_match_doubaninfo_func=self.async_match_doubaninfo,
            **kwargs
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

        def __douban_tv():
            """
            获取豆瓣剧集信息
            """
            info = self.doubanapi.tv_detail(doubanid)
            if info:
                if "subject_ip_rate_limit" in info.get("msg", ""):
                    msg = f"触发豆瓣IP速率限制，错误信息：{info} ..."
                    logger.warn(msg)
                    raise APIRateLimitException(msg)
                celebrities = self.doubanapi.tv_celebrities(doubanid)
                if celebrities:
                    info["directors"] = celebrities.get("directors")
                    info["actors"] = celebrities.get("actors")
            return info

        def __douban_movie():
            """
            获取豆瓣电影信息
            """
            info = self.doubanapi.movie_detail(doubanid)
            if info:
                if "subject_ip_rate_limit" in info.get("msg", ""):
                    msg = f"触发豆瓣IP速率限制，错误信息：{info} ..."
                    logger.warn(msg)
                    raise APIRateLimitException(msg)
                celebrities = self.doubanapi.movie_celebrities(doubanid)
                if celebrities:
                    info["directors"] = celebrities.get("directors")
                    info["actors"] = celebrities.get("actors")
            return info

        if not doubanid:
            return None
        logger.info(f"开始获取豆瓣信息：{doubanid} ...")
        if mtype == MediaType.TV:
            return __douban_tv()
        elif mtype == MediaType.MOVIE:
            return __douban_movie()
        else:
            return __douban_movie() or __douban_tv()

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

        async def __async_douban_tv():
            """
            获取豆瓣剧集信息（异步版本）
            """
            info = await self.doubanapi.async_tv_detail(doubanid)
            if info:
                if "subject_ip_rate_limit" in info.get("msg", ""):
                    msg = f"触发豆瓣IP速率限制，错误信息：{info} ..."
                    logger.warn(msg)
                    raise APIRateLimitException(msg)
                celebrities = await self.doubanapi.async_tv_celebrities(doubanid)
                if celebrities:
                    info["directors"] = celebrities.get("directors")
                    info["actors"] = celebrities.get("actors")
            return info

        async def __async_douban_movie():
            """
            获取豆瓣电影信息（异步版本）
            """
            info = await self.doubanapi.async_movie_detail(doubanid)
            if info:
                if "subject_ip_rate_limit" in info.get("msg", ""):
                    msg = f"触发豆瓣IP速率限制，错误信息：{info} ..."
                    logger.warn(msg)
                    raise APIRateLimitException(msg)
                celebrities = await self.doubanapi.async_movie_celebrities(doubanid)
                if celebrities:
                    info["directors"] = celebrities.get("directors")
                    info["actors"] = celebrities.get("actors")
            return info

        if not doubanid:
            return None
        logger.info(f"开始获取豆瓣信息：{doubanid} ...")
        if mtype == MediaType.TV:
            return await __async_douban_tv()
        elif mtype == MediaType.MOVIE:
            return await __async_douban_movie()
        else:
            movie_result = await __async_douban_movie()
            if movie_result:
                return movie_result
            return await __async_douban_tv()

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
        if infos and infos.get("items"):
            medias = [MediaInfo(douban_info=info) for info in infos.get("items")]
            return [media for media in medias if media.poster_path
                    and "movie_large.jpg" not in media.poster_path
                    and "tv_normal.png" not in media.poster_path
                    and "movie_large.jpg" not in media.poster_path
                    and "tv_normal.jpg" not in media.poster_path
                    and "tv_large.jpg" not in media.poster_path]
        return []

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
        if infos and infos.get("items"):
            medias = [MediaInfo(douban_info=info) for info in infos.get("items")]
            return [media for media in medias if media.poster_path
                    and "movie_large.jpg" not in media.poster_path
                    and "tv_normal.png" not in media.poster_path
                    and "movie_large.jpg" not in media.poster_path
                    and "tv_normal.jpg" not in media.poster_path
                    and "tv_large.jpg" not in media.poster_path]
        return []

    def movie_showing(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取正在上映的电影
        """
        infos = self.doubanapi.movie_showing(start=(page - 1) * count,
                                             count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    async def async_movie_showing(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取正在上映的电影（异步版本）
        """
        infos = await self.doubanapi.async_movie_showing(start=(page - 1) * count,
                                                         count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    def tv_weekly_chinese(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑国产剧
        """
        infos = self.doubanapi.tv_chinese_best_weekly(start=(page - 1) * count,
                                                      count=count)
        if infos:
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    async def async_tv_weekly_chinese(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑国产剧（异步版本）
        """
        infos = await self.doubanapi.async_tv_chinese_best_weekly(start=(page - 1) * count,
                                                                  count=count)
        if infos:
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    def tv_weekly_global(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑外国剧
        """
        infos = self.doubanapi.tv_global_best_weekly(start=(page - 1) * count,
                                                     count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    async def async_tv_weekly_global(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑外国剧（异步版本）
        """
        infos = await self.doubanapi.async_tv_global_best_weekly(start=(page - 1) * count,
                                                                 count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    def tv_animation(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣动画剧
        """
        infos = self.doubanapi.tv_animation(start=(page - 1) * count,
                                            count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    async def async_tv_animation(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣动画剧（异步版本）
        """
        infos = await self.doubanapi.async_tv_animation(start=(page - 1) * count,
                                                        count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    def movie_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门电影
        """
        infos = self.doubanapi.movie_hot_gaia(start=(page - 1) * count,
                                              count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    async def async_movie_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门电影（异步版本）
        """
        infos = await self.doubanapi.async_movie_hot_gaia(start=(page - 1) * count,
                                                          count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    def tv_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门剧集
        """
        infos = self.doubanapi.tv_hot(start=(page - 1) * count,
                                      count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    async def async_tv_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门剧集（异步版本）
        """
        infos = await self.doubanapi.async_tv_hot(start=(page - 1) * count,
                                                  count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    def search_medias(
        self, meta: MetaBase, source: Optional[str] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :param source: 请求级搜索数据源
        :return: 媒体信息
        """
        if not is_media_source_enabled(source, "douban"):
            return None
        if not meta.name:
            return []
        result = self.doubanapi.search(meta.name)
        if not result or not result.get("items"):
            return []
        # 返回数据
        return self._build_search_medias_result(meta, result.get("items"))

    async def async_search_medias(
        self, meta: MetaBase, source: Optional[str] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息（异步版本）
        :param meta:  识别的元数据
        :param source: 请求级搜索数据源
        :return: 媒体信息
        """
        if not is_media_source_enabled(source, "douban"):
            return None
        if not meta.name:
            return []
        result = await self.doubanapi.async_search(meta.name)
        if not result or not result.get("items"):
            return []
        # 返回数据
        return self._build_search_medias_result(meta, result.get("items"))

    def search_persons(
        self, name: str, source: Optional[str] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息
        :param name: 人物名称
        :param source: 请求级搜索数据源
        :return: 人物信息列表
        """
        if not is_media_source_enabled(source, "douban"):
            return None
        if not name:
            return []
        result = self.doubanapi.person_search(keyword=name)
        if result and result.get('items'):
            return [MediaPerson(source='douban', **{
                'id': item.get('target_id'),
                'name': item.get('target', {}).get('title'),
                'url': item.get('target', {}).get('url'),
                'images': item.get('target', {}).get('cover', {}),
                'avatar': (item.get('target', {}).get('cover_img', {}).get('url')
                           or '').replace("/l/public/", "/s/public/"),
            }) for item in result.get('items') if name in item.get('target', {}).get('title')]
        return []

    async def async_search_persons(
        self, name: str, source: Optional[str] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息（异步版本）
        :param name: 人物名称
        :param source: 请求级搜索数据源
        :return: 人物信息列表
        """
        if not is_media_source_enabled(source, "douban"):
            return None
        if not name:
            return []
        result = await self.doubanapi.async_person_search(keyword=name)
        if result and result.get('items'):
            return [MediaPerson(source='douban', **{
                'id': item.get('target_id'),
                'name': item.get('target', {}).get('title'),
                'url': item.get('target', {}).get('url'),
                'images': item.get('target', {}).get('cover', {}),
                'avatar': (item.get('target', {}).get('cover_img', {}).get('url')
                           or '').replace("/l/public/", "/s/public/"),
            }) for item in result.get('items') if name in item.get('target', {}).get('title')]
        return []

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
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    async def async_movie_top250(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣电影TOP250（异步版本）
        """
        infos = await self.doubanapi.async_movie_top250(start=(page - 1) * count,
                                                        count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    def metadata_nfo(self, mediainfo: MediaInfo, season: int = None, **kwargs) -> Optional[str]:
        """
        获取NFO文件内容文本
        :param mediainfo: 媒体信息
        :param season: 季号
        """
        if (mediainfo.scrape_source or settings.SCRAP_SOURCE) != "douban":
            return None
        return self.scraper.get_metadata_nfo(mediainfo=mediainfo, season=season)

    def metadata_img(self, mediainfo: MediaInfo, season: int = None, episode: int = None) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if (mediainfo.scrape_source or settings.SCRAP_SOURCE) != "douban":
            return None
        return self.scraper.get_metadata_img(mediainfo=mediainfo, season=season, episode=episode)

    @staticmethod
    def _validate_douban_obtain_images_params(mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        验证豆瓣 obtain_images 参数
        :param mediainfo: 媒体信息
        :return: None 表示不处理，MediaInfo 表示继续处理
        """
        if mediainfo.source != "douban" and settings.RECOGNIZE_SOURCE != "douban":
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

    def douban_movie_credits(self, doubanid: str) -> List[schemas.MediaPerson]:
        """
        根据豆瓣ID查询电影演职员表
        :param doubanid:  豆瓣ID
        """
        result = self.doubanapi.movie_celebrities(subject_id=doubanid)
        return self._process_celebrity_data(result)

    def douban_tv_credits(self, doubanid: str) -> List[schemas.MediaPerson]:
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

    def douban_person_detail(self, person_id: int) -> schemas.MediaPerson:
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
            return schemas.MediaPerson(source='douban', **{
                "id": detail.get("id"),
                "name": detail.get("title"),
                "avatar": image,
                "biography": detail.get("extra", {}).get("short_info"),
                "also_known_as": also_known_as,
            })
        return schemas.MediaPerson(source='douban')

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
    def _process_celebrity_data(result: dict) -> List[schemas.MediaPerson]:
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
            return [schemas.MediaPerson(source='douban', **doubaninfo) for doubaninfo in ret_list]
        return []

    async def async_douban_movie_credits(self, doubanid: str) -> List[schemas.MediaPerson]:
        """
        根据豆瓣ID查询电影演职员表（异步版本）
        :param doubanid:  豆瓣ID
        """
        result = await self.doubanapi.async_movie_celebrities(subject_id=doubanid)
        return self._process_celebrity_data(result)

    async def async_douban_tv_credits(self, doubanid: str) -> List[schemas.MediaPerson]:
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

    async def async_douban_person_detail(self, person_id: int) -> schemas.MediaPerson:
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
            return schemas.MediaPerson(source='douban', **{
                "id": detail.get("id"),
                "name": detail.get("title"),
                "avatar": image,
                "biography": detail.get("extra", {}).get("short_info"),
                "also_known_as": also_known_as,
            })
        return schemas.MediaPerson(source='douban')

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
