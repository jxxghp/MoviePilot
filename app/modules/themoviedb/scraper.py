from pathlib import Path
from typing import Optional, Tuple
from xml.dom import minidom

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.schemas.types import MediaType
from app.utils.dom import DomUtils
from app.modules.themoviedb.tmdbapi import TmdbApi


class TmdbScraper:
    _meta_tmdb = None
    _img_tmdb = None

    @property
    def default_tmdb(self):
        """
        获取元数据TMDB Api
        """
        if not self._meta_tmdb:
            self._meta_tmdb = TmdbApi(language=settings.TMDB_LOCALE)
        return self._meta_tmdb

    def original_tmdb(self, mediainfo: Optional[MediaInfo] = None):
        """
        获取图片TMDB Api
        """
        if settings.TMDB_SCRAP_ORIGINAL_IMAGE and mediainfo:
            return TmdbApi(language=mediainfo.original_language)
        return self.default_tmdb


    def get_metadata_nfo(self, meta: MetaBase, mediainfo: MediaInfo,
                         season: Optional[int] = None, episode: Optional[int] = None) -> Optional[str]:
        """
        获取NFO文件内容文本
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if mediainfo.type == MediaType.MOVIE:
            # 电影元数据文件
            doc = self.__gen_movie_nfo_file(mediainfo=mediainfo)
        else:
            if season is not None:
                # 查询季信息
                if mediainfo.episode_group:
                    seasoninfo = self.default_tmdb.get_tv_group_detail(mediainfo.episode_group, season=season)
                else:
                    seasoninfo = self.default_tmdb.get_tv_season_detail(mediainfo.tmdb_id, season=season)
                if episode:
                    # 集元数据文件
                    episodeinfo = self.__get_episode_detail(seasoninfo, meta.begin_episode)
                    doc = self.__gen_tv_episode_nfo_file(episodeinfo=episodeinfo, tmdbid=mediainfo.tmdb_id,
                                                         season=season, episode=episode)
                else:
                    # 季元数据文件
                    doc = self.__gen_tv_season_nfo_file(seasoninfo=seasoninfo, season=season)
            else:
                # 电视剧元数据文件
                doc = self.__gen_tv_nfo_file(mediainfo=mediainfo)
        if doc:
            return doc.toprettyxml(indent="  ", encoding="utf-8") # noqa

        return None

    def get_metadata_img(self, mediainfo: MediaInfo, season: Optional[int] = None, episode: Optional[int] = None) -> dict:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        images = {}
        if season is not None:
            # 只需要集的图片
            if episode:
                # 集的图片
                if mediainfo.episode_group:
                    seasoninfo = self.original_tmdb(mediainfo).get_tv_group_detail(mediainfo.episode_group, season)
                else:
                    seasoninfo = self.original_tmdb(mediainfo).get_tv_season_detail(mediainfo.tmdb_id, season)
                if seasoninfo:
                    episodeinfo = self.__get_episode_detail(seasoninfo, episode)
                    if episodeinfo and episodeinfo.get("still_path"):
                        # TMDB集still图片
                        still_name = f"{episode}"
                        still_url = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{episodeinfo.get('still_path')}"
                        images[still_name] = still_url
            else:
                # 季的图片
                seasoninfo = self.original_tmdb(mediainfo).get_tv_season_detail(mediainfo.tmdb_id, season)
                if seasoninfo:
                    # TMDB季poster图片
                    poster_name, poster_url = self.get_season_poster(seasoninfo, season)
                    if poster_name and poster_url:
                        images[poster_name] = poster_url
            return images
        else:
            # 主媒体图片
            for attr_name, attr_value in vars(mediainfo).items():
                if attr_value \
                        and attr_name.endswith("_path") \
                        and attr_value \
                        and isinstance(attr_value, str) \
                        and attr_value.startswith("http"):
                    image_name = attr_name.replace("_path", "") + Path(attr_value).suffix
                    images[image_name] = attr_value
            return images

    @staticmethod
    def get_season_poster(seasoninfo: dict, season: int) -> Tuple[str, str]:
        """
        获取季的海报
        """
        # TMDB季poster图片
        sea_seq = str(season).rjust(2, '0')
        if seasoninfo.get("poster_path"):
            # 后缀
            ext = Path(seasoninfo.get('poster_path')).suffix
            # URL
            url = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{seasoninfo.get('poster_path')}"
            # S0海报格式不同
            if season == 0:
                image_name = f"season-specials-poster{ext}"
            else:
                image_name = f"season{sea_seq}-poster{ext}"
            return image_name, url
        return "", ""

    @staticmethod
    def __get_episode_detail(seasoninfo: dict, episode: int) -> dict:
        """
        根据季信息获取集的信息
        """
        for _episode_info in seasoninfo.get("episodes") or []:
            if _episode_info.get("episode_number") == episode:
                return _episode_info
        return {}

    @staticmethod
    def __gen_common_nfo(mediainfo: MediaInfo, doc: minidom.Document, root: minidom.Element):
        """
        生成公共NFO
        """
        # TMDB
        DomUtils.add_node(doc, root, "tmdbid", mediainfo.tmdb_id or "")
        uniqueid_tmdb = DomUtils.add_node(doc, root, "uniqueid", mediainfo.tmdb_id or "")
        uniqueid_tmdb.setAttribute("type", "tmdb")
        uniqueid_tmdb.setAttribute("default", "true")
        # TVDB
        if mediainfo.tvdb_id:
            DomUtils.add_node(doc, root, "tvdbid", str(mediainfo.tvdb_id))
            uniqueid_tvdb = DomUtils.add_node(doc, root, "uniqueid", str(mediainfo.tvdb_id))
            uniqueid_tvdb.setAttribute("type", "tvdb")
        # IMDB
        if mediainfo.imdb_id:
            DomUtils.add_node(doc, root, "imdbid", mediainfo.imdb_id)
            uniqueid_imdb = DomUtils.add_node(doc, root, "uniqueid", mediainfo.imdb_id)
            uniqueid_imdb.setAttribute("type", "imdb")
            uniqueid_imdb.setAttribute("default", "true")
            uniqueid_tmdb.setAttribute("default", "false")

        # 简介
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        # 导演
        for director in mediainfo.directors:
            xdirector = DomUtils.add_node(doc, root, "director", director.get("name") or "")
            xdirector.setAttribute("tmdbid", str(director.get("id") or ""))
        # 演员
        for actor in mediainfo.actors:
            # 获取中文名
            xactor = DomUtils.add_node(doc, root, "actor")
            DomUtils.add_node(doc, xactor, "name", actor.get("name") or "")
            DomUtils.add_node(doc, xactor, "type", "Actor")
            DomUtils.add_node(doc, xactor, "role", actor.get("character") or actor.get("role") or "")
            DomUtils.add_node(doc, xactor, "tmdbid", actor.get("id") or "")
            DomUtils.add_node(doc, xactor, "thumb",
                              f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{actor.get('profile_path')}")
            DomUtils.add_node(doc, xactor, "profile",
                              f"https://www.themoviedb.org/person/{actor.get('id')}")
        # 风格
        genres = mediainfo.genres or []
        for genre in genres:
            DomUtils.add_node(doc, root, "genre", genre.get("name") or "")
        # 评分
        DomUtils.add_node(doc, root, "rating", mediainfo.vote_average or "0")
        # 内容分级
        if content_rating := mediainfo.content_rating:
            DomUtils.add_node(doc, root, "mpaa", content_rating)

        return doc

    def __gen_movie_nfo_file(self, mediainfo: MediaInfo) -> minidom.Document:
        """
        生成电影的NFO描述文件
        :param mediainfo: 识别后的媒体信息
        """
        # 开始生成XML
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "movie")
        # 公共部分
        doc = self.__gen_common_nfo(mediainfo=mediainfo,
                                    doc=doc,
                                    root=root)
        # 标题
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        DomUtils.add_node(doc, root, "originaltitle", mediainfo.original_title or "")
        # 发布日期
        DomUtils.add_node(doc, root, "premiered", mediainfo.release_date or "")
        # 年份
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")
        return doc

    def __gen_tv_nfo_file(self, mediainfo: MediaInfo) -> minidom.Document:
        """
        生成电视剧的NFO描述文件
        :param mediainfo: 媒体信息
        """
        # 开始生成XML
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "tvshow")
        # 公共部分
        doc = self.__gen_common_nfo(mediainfo=mediainfo,
                                    doc=doc,
                                    root=root)
        # 标题
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        DomUtils.add_node(doc, root, "originaltitle", mediainfo.original_title or "")
        # 发布日期
        DomUtils.add_node(doc, root, "premiered", mediainfo.release_date or "")
        # 年份
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")
        DomUtils.add_node(doc, root, "season", "-1")
        DomUtils.add_node(doc, root, "episode", "-1")

        return doc

    @staticmethod
    def __gen_tv_season_nfo_file(seasoninfo: dict, season: int) -> minidom.Document:
        """
        生成电视剧季的NFO描述文件
        :param seasoninfo: TMDB季媒体信息
        :param season: 季号
        """
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "season")
        # 简介
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(seasoninfo.get("overview") or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(seasoninfo.get("overview") or ""))
        # 标题
        DomUtils.add_node(doc, root, "title", seasoninfo.get("name") or "季 %s" % season)
        # 发行日期
        DomUtils.add_node(doc, root, "premiered", seasoninfo.get("air_date") or "")
        DomUtils.add_node(doc, root, "releasedate", seasoninfo.get("air_date") or "")
        # 发行年份
        DomUtils.add_node(doc, root, "year",
                          seasoninfo.get("air_date")[:4] if seasoninfo.get("air_date") else "")
        # seasonnumber
        DomUtils.add_node(doc, root, "seasonnumber", str(season))
        return doc

    @staticmethod
    def __gen_tv_episode_nfo_file(tmdbid: int,
                                  episodeinfo: dict,
                                  season: int,
                                  episode: int) -> minidom.Document:
        """
        生成电视剧集的NFO描述文件
        :param tmdbid: TMDBID
        :param episodeinfo: 集TMDB元数据
        :param season: 季号
        :param episode: 集号
        """
        # 开始生成集的信息
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "episodedetails")
        # TMDBID
        uniqueid = DomUtils.add_node(doc, root, "uniqueid", str(episodeinfo.get("id")))
        uniqueid.setAttribute("type", "tmdb")
        uniqueid.setAttribute("default", "true")
        # tmdbid
        DomUtils.add_node(doc, root, "tmdbid", str(tmdbid))
        # 标题
        DomUtils.add_node(doc, root, "title", episodeinfo.get("name") or "第 %s 集" % episode)
        # 简介
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(episodeinfo.get("overview") or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(episodeinfo.get("overview") or ""))
        # 发布日期
        DomUtils.add_node(doc, root, "aired", episodeinfo.get("air_date") or "")
        # 年份
        DomUtils.add_node(doc, root, "year",
                          episodeinfo.get("air_date")[:4] if episodeinfo.get("air_date") else "")
        # 季
        DomUtils.add_node(doc, root, "season", str(season))
        # 集
        DomUtils.add_node(doc, root, "episode", str(episode))
        # 评分
        DomUtils.add_node(doc, root, "rating", episodeinfo.get("vote_average") or "0")
        # 导演
        directors = episodeinfo.get("crew") or []
        for director in directors:
            if director.get("known_for_department") == "Directing":
                xdirector = DomUtils.add_node(doc, root, "director", director.get("name") or "")
                xdirector.setAttribute("tmdbid", str(director.get("id") or ""))
        # 演员
        actors = episodeinfo.get("guest_stars") or []
        for actor in actors:
            if actor.get("known_for_department") == "Acting":
                xactor = DomUtils.add_node(doc, root, "actor")
                DomUtils.add_node(doc, xactor, "name", actor.get("name") or "")
                DomUtils.add_node(doc, xactor, "type", "Actor")
                DomUtils.add_node(doc, xactor, "tmdbid", actor.get("id") or "")
                DomUtils.add_node(doc, xactor, "thumb",
                                  f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{actor.get('profile_path')}")
                DomUtils.add_node(doc, xactor, "profile",
                                  f"https://www.themoviedb.org/person/{actor.get('id')}")
        return doc
