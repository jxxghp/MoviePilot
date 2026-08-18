from typing import Any, List, Optional

from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.application.orchestration import ChainBase
from app.domain.context import MediaInfo, MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaSource, MediaType


class DoubanChain(ChainBase):
    """
    豆瓣处理链
    """

    music_source = MediaSource.DoubanMusic

    def search_music(self, meta: MetaMusic, limit: int = 20) -> list[MusicInfo]:
        """按音乐元数据搜索豆瓣音乐候选。"""
        result = self.unicast(
            "search_music",
            meta=meta,
            limit=limit,
            media_source=self.music_source,
        )
        return self._music_infos(result, limit=limit)

    async def async_search_music(
            self,
            meta: MetaMusic,
            limit: int = 20,
    ) -> list[MusicInfo]:
        """异步按音乐元数据搜索豆瓣音乐候选。"""
        result = await self.async_unicast(
            "search_music",
            meta=meta,
            limit=limit,
            media_source=self.music_source,
        )
        return self._music_infos(result, limit=limit)

    def recognize_music(
            self,
            meta: Optional[MetaMusic] = None,
            media_id: Optional[str] = None,
            cache: bool = True,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """按豆瓣音乐身份或音乐元数据识别标准音乐信息。"""
        normalized_id = self._normalize_music_id(media_id)
        result = self.unicast(
            "recognize_media",
            meta=meta,
            mtype=MediaType.MUSIC,
            media_source=self.music_source,
            media_id=normalized_id,
            cache=cache,
            music_type=music_type,
        )
        return self._music_info(result, media_id=normalized_id)

    async def async_recognize_music(
            self,
            meta: Optional[MetaMusic] = None,
            media_id: Optional[str] = None,
            cache: bool = True,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """异步按豆瓣音乐身份或音乐元数据识别标准音乐信息。"""
        normalized_id = self._normalize_music_id(media_id)
        result = await self.async_unicast(
            "async_recognize_media",
            meta=meta,
            mtype=MediaType.MUSIC,
            media_source=self.music_source,
            media_id=normalized_id,
            cache=cache,
            music_type=music_type,
        )
        return self._music_info(result, media_id=normalized_id)

    def get_music_album(self, media_id: str) -> Optional[MusicAlbumInfo]:
        """按豆瓣音乐专辑 ID 获取标准化专辑详情。"""
        normalized_id = self._normalize_music_id(media_id)
        if not normalized_id:
            return None
        result = self.unicast(
            "music_album",
            media_source=self.music_source,
            media_id=normalized_id,
        )
        return self._music_album(result, media_id=normalized_id)

    async def async_get_music_album(self, media_id: str) -> Optional[MusicAlbumInfo]:
        """异步按豆瓣音乐专辑 ID 获取标准化专辑详情。"""
        normalized_id = self._normalize_music_id(media_id)
        if not normalized_id:
            return None
        result = await self.async_unicast(
            "music_album",
            media_source=self.music_source,
            media_id=normalized_id,
        )
        return self._music_album(result, media_id=normalized_id)

    async def async_get_music_album_related(
            self,
            media_id: str,
            count: int = 24,
    ) -> list[MusicInfo]:
        """异步按豆瓣音乐专辑 ID 获取相关推荐。"""
        normalized_id = self._normalize_music_id(media_id)
        if not normalized_id:
            return []
        result = await self.async_unicast(
            "music_album_related",
            media_source=self.music_source,
            media_id=normalized_id,
            count=count,
        )
        return self._music_infos(result, limit=count)

    def music_discover(
            self,
            page: int = 1,
            count: int = 30,
            entity: str = MUSIC_ENTITY_ALBUM,
            mode: str = "chart",
            tags: str = "",
            sort: str = "U",
    ) -> list[MusicInfo]:
        """按豆瓣音乐官方榜单或标签浏览标准音乐条目。"""
        result = self.unicast(
            "music_discover",
            media_source=self.music_source,
            page=page,
            count=count,
            entity=entity,
            mode=mode,
            tags=tags,
            sort=sort,
        )
        return self._music_infos(result, limit=count)

    async def async_music_discover(
            self,
            page: int = 1,
            count: int = 30,
            entity: str = MUSIC_ENTITY_ALBUM,
            mode: str = "chart",
            tags: str = "",
            sort: str = "U",
    ) -> list[MusicInfo]:
        """异步按豆瓣音乐官方榜单或标签浏览标准音乐条目。"""
        result = await self.async_unicast(
            "music_discover",
            media_source=self.music_source,
            page=page,
            count=count,
            entity=entity,
            mode=mode,
            tags=tags,
            sort=sort,
        )
        return self._music_infos(result, limit=count)

    @staticmethod
    def _normalize_music_id(media_id: Optional[str]) -> Optional[str]:
        """清理豆瓣音乐原生 ID，空值和历史零哨兵按无身份处理。"""
        normalized = str(media_id).strip() if media_id is not None else ""
        return normalized if normalized and normalized != "0" else None

    @classmethod
    def _music_infos(cls, result: Any, limit: Optional[int] = None) -> list[MusicInfo]:
        """将模块或插件结果转换为豆瓣音乐候选列表。"""
        candidates = result if isinstance(result, list) else []
        infos = [
            item if isinstance(item, MusicInfo) else MusicInfo.from_dict(item)
            for item in candidates
            if isinstance(item, (MusicInfo, dict))
        ]
        infos = [info for info in infos if info.media_source == cls.music_source]
        return infos[:limit] if limit else infos

    @classmethod
    def _music_info(
            cls,
            result: Any,
            media_id: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """校验豆瓣音乐识别结果的来源与显式身份。"""
        if isinstance(result, MusicInfo):
            info = result
        elif isinstance(result, dict):
            info = MusicInfo.from_dict(result)
        else:
            return None
        if info.media_source and info.media_source != cls.music_source:
            return None
        if media_id and (
                info.media_source != cls.music_source
                or info.media_id != media_id
        ):
            return None
        return info

    @classmethod
    def _music_album(
            cls,
            result: Any,
            media_id: Optional[str] = None,
    ) -> Optional[MusicAlbumInfo]:
        """将模块或插件结果转换为豆瓣音乐专辑详情。"""
        if isinstance(result, MusicAlbumInfo):
            album = result
        elif isinstance(result, dict):
            album = MusicAlbumInfo.from_dict(result)
        else:
            return None
        if album.media_source != cls.music_source:
            return None
        if media_id and album.media_id != media_id:
            return None
        return album

    def person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        根据人物ID查询豆瓣人物详情
        :param person_id:  人物ID
        """
        return self.unicast("person_detail", source=MediaSource.Douban, person_id=person_id)

    def person_credits(self, person_id: int, page: Optional[int] = 1) -> List[MediaInfo]:
        """
        根据人物ID查询人物参演作品
        :param person_id:  人物ID
        :param page:  页码
        """
        return self.unicast("person_credits", source=MediaSource.Douban, person_id=person_id, page=page)

    def movie_top250(self, page: Optional[int] = 1, count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取豆瓣电影TOP250
        :param page:  页码
        :param count:  每页数量
        """
        return self.unicast("discover_board", source=MediaSource.Douban, board="movie_top250",
                               page=page, count=count)

    def movie_showing(self, page: Optional[int] = 1, count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取正在上映的电影
        """
        return self.unicast("discover_board", source=MediaSource.Douban, board="movie_showing",
                               page=page, count=count)

    def tv_weekly_chinese(self, page: Optional[int] = 1, count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取本周中国剧集榜
        """
        return self.unicast("discover_board", source=MediaSource.Douban, board="tv_weekly_chinese",
                               page=page, count=count)

    def tv_weekly_global(self, page: Optional[int] = 1, count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取本周全球剧集榜
        """
        return self.unicast("discover_board", source=MediaSource.Douban, board="tv_weekly_global",
                               page=page, count=count)

    def douban_discover(self, mtype: MediaType, sort: str, tags: str,
                        page: Optional[int] = 0, count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        发现豆瓣电影、剧集
        :param mtype:  媒体类型
        :param sort:  排序方式
        :param tags:  标签
        :param page:  页码
        :param count:  数量
        :return: 媒体信息列表
        """
        return self.unicast("discover", source=MediaSource.Douban, mtype=mtype, sort=sort, tags=tags,
                               page=page, count=count)

    def tv_animation(self, page: Optional[int] = 1, count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取动画剧集
        """
        return self.unicast("discover_board", source=MediaSource.Douban, board="tv_animation",
                               page=page, count=count)

    def movie_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取热门电影
        """
        return self.unicast("discover_board", source=MediaSource.Douban, board="movie_hot",
                               page=page, count=count)

    def tv_hot(self, page: Optional[int] = 1, count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取热门剧集
        """
        return self.unicast("discover_board", source=MediaSource.Douban, board="tv_hot",
                               page=page, count=count)

    def movie_credits(self, doubanid: str) -> Optional[List[_SchemaMediaPerson]]:
        """
        根据TMDBID查询电影演职人员
        :param doubanid:  豆瓣ID
        """
        return self.unicast("media_credits", source=MediaSource.Douban, media_id=doubanid,
                            mtype=MediaType.MOVIE)

    def tv_credits(self, doubanid: str) -> Optional[List[_SchemaMediaPerson]]:
        """
        根据TMDBID查询电视剧演职人员
        :param doubanid:  豆瓣ID
        """
        return self.unicast("media_credits", source=MediaSource.Douban, media_id=doubanid,
                            mtype=MediaType.TV)

    def movie_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电影
        :param doubanid:  豆瓣ID
        """
        return self.unicast("media_recommend", source=MediaSource.Douban, media_id=doubanid,
                            mtype=MediaType.MOVIE)

    def tv_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电视剧
        :param doubanid:  豆瓣ID
        """
        return self.unicast("media_recommend", source=MediaSource.Douban, media_id=doubanid,
                            mtype=MediaType.TV)

    async def async_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        根据人物ID查询豆瓣人物详情（异步版本）
        :param person_id:  人物ID
        """
        return await self.async_unicast("async_person_detail", source=MediaSource.Douban, person_id=person_id)

    async def async_person_credits(self, person_id: int, page: Optional[int] = 1) -> List[MediaInfo]:
        """
        根据人物ID查询人物参演作品（异步版本）
        :param person_id:  人物ID
        :param page:  页码
        """
        return await self.async_unicast(
            "async_person_credits", source=MediaSource.Douban, person_id=person_id, page=page
        )

    async def async_movie_top250(self, page: Optional[int] = 1,
                                 count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取豆瓣电影TOP250（异步版本）
        :param page:  页码
        :param count:  每页数量
        """
        return await self.async_unicast("async_discover_board", source=MediaSource.Douban,
                                           board="movie_top250", page=page, count=count)

    async def async_movie_showing(self, page: Optional[int] = 1,
                                  count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取正在上映的电影（异步版本）
        """
        return await self.async_unicast("async_discover_board", source=MediaSource.Douban,
                                           board="movie_showing", page=page, count=count)

    async def async_tv_weekly_chinese(self, page: Optional[int] = 1,
                                      count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取本周中国剧集榜（异步版本）
        """
        return await self.async_unicast("async_discover_board", source=MediaSource.Douban,
                                           board="tv_weekly_chinese", page=page, count=count)

    async def async_tv_weekly_global(self, page: Optional[int] = 1,
                                     count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取本周全球剧集榜（异步版本）
        """
        return await self.async_unicast("async_discover_board", source=MediaSource.Douban,
                                           board="tv_weekly_global", page=page, count=count)

    async def async_douban_discover(self, mtype: MediaType, sort: str, tags: str,
                                    page: Optional[int] = 0, count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        发现豆瓣电影、剧集（异步版本）
        :param mtype:  媒体类型
        :param sort:  排序方式
        :param tags:  标签
        :param page:  页码
        :param count:  数量
        :return: 媒体信息列表
        """
        return await self.async_unicast("async_discover", source=MediaSource.Douban, mtype=mtype,
                                           sort=sort, tags=tags, page=page, count=count)

    async def async_tv_animation(self, page: Optional[int] = 1,
                                 count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取动画剧集（异步版本）
        """
        return await self.async_unicast("async_discover_board", source=MediaSource.Douban,
                                           board="tv_animation", page=page, count=count)

    async def async_movie_hot(self, page: Optional[int] = 1,
                              count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取热门电影（异步版本）
        """
        return await self.async_unicast("async_discover_board", source=MediaSource.Douban,
                                           board="movie_hot", page=page, count=count)

    async def async_tv_hot(self, page: Optional[int] = 1,
                           count: Optional[int] = 30) -> Optional[List[MediaInfo]]:
        """
        获取热门剧集（异步版本）
        """
        return await self.async_unicast("async_discover_board", source=MediaSource.Douban,
                                           board="tv_hot", page=page, count=count)

    async def async_movie_credits(self, doubanid: str) -> Optional[List[_SchemaMediaPerson]]:
        """
        根据TMDBID查询电影演职人员（异步版本）
        :param doubanid:  豆瓣ID
        """
        return await self.async_unicast("async_media_credits", source=MediaSource.Douban, media_id=doubanid,
                                        mtype=MediaType.MOVIE)

    async def async_tv_credits(self, doubanid: str) -> Optional[List[_SchemaMediaPerson]]:
        """
        根据TMDBID查询电视剧演职人员（异步版本）
        :param doubanid:  豆瓣ID
        """
        return await self.async_unicast("async_media_credits", source=MediaSource.Douban, media_id=doubanid,
                                        mtype=MediaType.TV)

    async def async_movie_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电影（异步版本）
        :param doubanid:  豆瓣ID
        """
        return await self.async_unicast("async_media_recommend", source=MediaSource.Douban, media_id=doubanid,
                                        mtype=MediaType.MOVIE)

    async def async_tv_recommend(self, doubanid: str) -> List[MediaInfo]:
        """
        根据豆瓣ID查询推荐电视剧（异步版本）
        :param doubanid:  豆瓣ID
        """
        return await self.async_unicast("async_media_recommend", source=MediaSource.Douban, media_id=doubanid,
                                        mtype=MediaType.TV)
