"""元数据与图片域的能力端口客户端。"""

from __future__ import annotations

from typing import Optional, Union

from app.application.orchestration.ports.dispatch import CapabilityPorts
from app.domain.context import MediaInfo
from app.schemas.types import MediaImageType, MediaSource, MediaType


class MetadataPorts(CapabilityPorts):
    """元数据匹配、详情查询与图片获取的能力端口。"""

    def match_doubaninfo(
            self,
            name: str,
            imdbid: Optional[str] = None,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
            raise_exception: bool = False,
    ) -> Optional[dict]:
        """
        搜索和匹配豆瓣信息
        :param name: 标题
        :param imdbid: imdbid
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        return self._dispatch.unicast(
            "match_media",
            source=MediaSource.Douban,
            name=name,
            imdbid=imdbid,
            mtype=mtype,
            year=year,
            season=season,
            raise_exception=raise_exception,
        )

    async def async_match_doubaninfo(
            self,
            name: str,
            imdbid: Optional[str] = None,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
            raise_exception: bool = False,
    ) -> Optional[dict]:
        """
        搜索和匹配豆瓣信息（异步版本）
        :param name: 标题
        :param imdbid: imdbid
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        return await self._dispatch.async_unicast(
            "async_match_media",
            source=MediaSource.Douban,
            name=name,
            imdbid=imdbid,
            mtype=mtype,
            year=year,
            season=season,
            raise_exception=raise_exception,
        )

    def match_tmdbinfo(
            self,
            name: str,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        搜索和匹配TMDB信息
        :param name: 标题
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        """
        return self._dispatch.unicast(
            "match_media", source=MediaSource.TMDB, name=name, mtype=mtype, year=year, season=season
        )

    async def async_match_tmdbinfo(
            self,
            name: str,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        搜索和匹配TMDB信息（异步版本）
        :param name: 标题
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        """
        return await self._dispatch.async_unicast(
            "async_match_media", source=MediaSource.TMDB, name=name, mtype=mtype, year=year, season=season
        )

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        if mediainfo and mediainfo.type == MediaType.MUSIC:
            return mediainfo
        return self._dispatch.pipeline("obtain_images", mediainfo)

    async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片（异步版本）
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        if mediainfo and mediainfo.type == MediaType.MUSIC:
            return mediainfo
        return await self._dispatch.async_pipeline("async_obtain_images", mediainfo)

    def obtain_specific_image(
            self,
            mediaid: Union[str, int],
            mtype: MediaType,
            image_type: MediaImageType,
            image_prefix: Optional[str] = None,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[str]:
        """
        获取指定媒体信息图片，返回图片地址
        :param mediaid:     媒体ID
        :param mtype:       媒体类型
        :param image_type:  图片类型
        :param image_prefix: 图片前缀
        :param season:      季
        :param episode:     集
        """
        return self._dispatch.unicast(
            "obtain_specific_image",
            mediaid=mediaid,
            mtype=mtype,
            image_prefix=image_prefix,
            image_type=image_type,
            season=season,
            episode=episode,
        )

    def douban_info(
            self,
            doubanid: str,
            mtype: Optional[MediaType] = None,
            raise_exception: bool = False,
    ) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :return: 豆瓣信息
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        return self._dispatch.unicast(
            "media_detail",
            source=MediaSource.Douban,
            media_id=doubanid,
            mtype=mtype,
            raise_exception=raise_exception,
        )

    async def async_douban_info(
            self,
            doubanid: str,
            mtype: Optional[MediaType] = None,
            raise_exception: bool = False,
    ) -> Optional[dict]:
        """
        获取豆瓣信息（异步版本）
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :return: 豆瓣信息
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        return await self._dispatch.async_unicast(
            "async_media_detail",
            source=MediaSource.Douban,
            media_id=doubanid,
            mtype=mtype,
            raise_exception=raise_exception,
        )

    def tvdb_info(self, tvdbid: int) -> Optional[dict]:
        """
        获取TVDB信息
        :param tvdbid: int
        :return: TVDB信息
        """
        return self._dispatch.unicast("media_detail", source=MediaSource.TVDB, media_id=tvdbid)

    def tvdb_slug(self, tvdbid: int) -> Optional[str]:
        """
        获取TVDB剧集 slug（别名），用于构建 TheTvDb 直达链接。
        :param tvdbid: int
        :return: slug 字符串
        """
        return self._dispatch.unicast("tvdb_slug", tvdbid=tvdbid)

    def tmdb_info(
            self, tmdbid: int, mtype: MediaType, season: Optional[int] = None
    ) -> Optional[dict]:
        """
        获取TMDB信息
        :param tmdbid: int
        :param mtype:  媒体类型
        :param season: 季
        :return: TVDB信息
        """
        return self._dispatch.unicast(
            "media_detail", source=MediaSource.TMDB, media_id=tmdbid, mtype=mtype, season=season
        )

    async def async_tmdb_info(
            self, tmdbid: int, mtype: MediaType, season: Optional[int] = None
    ) -> Optional[dict]:
        """
        获取TMDB信息（异步版本）
        :param tmdbid: int
        :param mtype:  媒体类型
        :param season: 季
        :return: TVDB信息
        """
        return await self._dispatch.async_unicast(
            "async_media_detail", source=MediaSource.TMDB, media_id=tmdbid, mtype=mtype, season=season
        )

    def bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息
        :param bangumiid: int
        :return: Bangumi信息
        """
        return self._dispatch.unicast("media_detail", source=MediaSource.Bangumi, media_id=bangumiid)

    async def async_bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息（异步版本）
        :param bangumiid: int
        :return: Bangumi信息
        """
        return await self._dispatch.async_unicast(
            "async_media_detail", source=MediaSource.Bangumi, media_id=bangumiid
        )

    def metadata_img(
            self,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        return self._dispatch.unicast(
            "metadata_img", mediainfo=mediainfo, season=season, episode=episode
        )
