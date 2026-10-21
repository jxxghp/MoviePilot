"""
TMDB NFO 文档生成、季集编号映射与图片地址选择。
"""

from pathlib import Path
from typing import Any, Optional, Tuple, cast
from xml.dom import minidom

from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.foundation.dom import DomUtils
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MediaType


class TmdbScraper:
    """
    依据 TMDB 元数据生成 NFO 文档与待下载图片清单。

    季集详情和图片缺失时会补充查询 TMDB；本类返回文档内容及地址，不负责写入媒体目录。
    """
    _meta_tmdb = None
    _img_tmdb = None

    @property
    def default_tmdb(self) -> TmdbApi:
        """
        惰性创建并复用运行配置语言对应的 TMDB 客户端。

        :return: 元数据查询客户端
        """
        if not self._meta_tmdb:
            self._meta_tmdb = TmdbApi(language=get_runtime_setting('TMDB_LOCALE'))
        return self._meta_tmdb

    def original_tmdb(self, mediainfo: Optional[MediaInfo] = None) -> TmdbApi:
        """
        按刮削设置选择原语言或默认语言的图片查询客户端。

        :param mediainfo: 包含原始语言的媒体信息；缺失时使用默认客户端
        :return: 启用原语言图片且提供媒体信息时新建原语言客户端，否则复用默认客户端
        """
        if get_runtime_setting('TMDB_SCRAP_ORIGINAL_IMAGE') and mediainfo:
            return TmdbApi(language=mediainfo.original_language)
        return self.default_tmdb

    def get_metadata_nfo(
        self,
        meta: MetaBase,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[str]:
        """
        生成电影、电视剧、季或单集的 NFO XML 内容。

        单集内容按 meta.begin_episode 查询，输出季集编号取调用参数，支持整理后的编号映射。

        :param meta: 识别元数据；单集详情使用其 begin_episode 查询
        :param mediainfo: 包含 TMDB 标识、媒体类型及可选分集组的媒体信息
        :param season: 季号；None 时生成电影或整剧文档
        :param episode: 输出文档的单集编号；非零且提供季号时生成单集文档
        :return: 包含 UTF-8 编码声明的 XML 文本；未生成文档时返回 None
        """
        if mediainfo.type == MediaType.MOVIE:
            doc = self.__gen_movie_nfo_file(mediainfo=mediainfo)
        else:
            if season is not None:
                if mediainfo.episode_group:
                    seasoninfo = self.default_tmdb.get_tv_group_detail(
                        mediainfo.episode_group, season=season
                    )
                else:
                    if mediainfo.tmdb_id is None:
                        return None
                    seasoninfo = self.default_tmdb.get_tv_season_detail(
                        mediainfo.tmdb_id, season=season
                    )
                if episode:
                    tmdb_id = mediainfo.tmdb_id
                    if tmdb_id is None:
                        return None
                    episode_number = meta.begin_episode or episode
                    episodeinfo = self.__get_episode_detail(seasoninfo, episode_number)
                    doc = self.__gen_tv_episode_nfo_file(
                        episodeinfo=episodeinfo,
                        tmdbid=tmdb_id,
                        season=season,
                        episode=episode,
                    )
                else:
                    doc = self.__gen_tv_season_nfo_file(
                        seasoninfo=seasoninfo, season=season
                    )
            else:
                doc = self.__gen_tv_nfo_file(mediainfo=mediainfo)
        if doc:
            return doc.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")

        return None

    def get_metadata_img(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> dict[str, str]:
        """
        生成图片文件名到下载地址的映射。

        启用原语言图片时可覆盖默认语言路径。仅生成地址清单，不执行图片下载。

        :param mediainfo: 媒体信息；主海报或背景缺失时可能原地补充图片路径
        :param season: 需要季图时提供；None 表示主媒体图片
        :param episode: 非零且提供季号时查询单集截图
        :return: 文件名与图片地址的字典；没有图片时返回空字典
        """
        images: dict[str, str] = {}
        if season is not None:
            if episode:
                if mediainfo.episode_group:
                    seasoninfo = self.original_tmdb(mediainfo).get_tv_group_detail(
                        mediainfo.episode_group, season
                    )
                else:
                    tmdb_id = cast(int, mediainfo.tmdb_id)
                    seasoninfo = self.original_tmdb(mediainfo).get_tv_season_detail(
                        tmdb_id, season
                    )
                if seasoninfo:
                    episodeinfo = self.__get_episode_detail(seasoninfo, episode)
                    if still_path := episodeinfo.get("still_path"):
                        ext = Path(still_path).suffix
                        still_name = f"episode-thumb{ext}"
                        still_url = get_runtime_setting('TMDB_IMAGE_URL')(still_path)
                        images[still_name] = still_url
            else:
                if mediainfo.tmdb_id is None:
                    return images
                seasoninfo = self.original_tmdb(mediainfo).get_tv_season_detail(
                    mediainfo.tmdb_id, season
                )
                if seasoninfo:
                    poster_name, poster_url = self.get_season_poster(seasoninfo, season)
                    if poster_name and poster_url:
                        images[poster_name] = poster_url
        else:
            self.__ensure_main_images(mediainfo)
            for attr_name, attr_value in vars(mediainfo).items():
                if (
                    attr_value
                    and attr_name.endswith("_path")
                    and isinstance(attr_value, str)
                    and attr_value.startswith("http")
                ):
                    image_name = (
                        attr_name.replace("_path", "") + Path(attr_value).suffix
                    )
                    images[image_name] = attr_value

            if get_runtime_setting('TMDB_SCRAP_ORIGINAL_IMAGE') and mediainfo.tmdb_id is not None:
                _mediainfo = self.original_tmdb(mediainfo).get_info(
                    mediainfo.type, mediainfo.tmdb_id
                )
                if _mediainfo:
                    for attr_name, attr_value in _mediainfo.items():
                        if attr_name.endswith("_path") and attr_value is not None:
                            image_url = get_runtime_setting('TMDB_IMAGE_URL')(attr_value)
                            image_name = (
                                attr_name.replace("_path", "") + Path(image_url).suffix
                            )
                            images[image_name] = image_url
        return images

    def __ensure_main_images(self, mediainfo: MediaInfo) -> None:
        """
        主媒体图片缺失时从 TMDB images 接口回填，避免当前语言没有图时只生成 NFO。
        """
        if not mediainfo or not mediainfo.tmdb_id:
            return
        if mediainfo.poster_path and mediainfo.backdrop_path:
            return
        if mediainfo.type == MediaType.MOVIE:
            image_info = self.default_tmdb.get_movie_images(
                mediainfo.tmdb_id,
                original_language=mediainfo.original_language,
            )
        else:
            image_info = self.default_tmdb.get_tv_images(
                mediainfo.tmdb_id,
                original_language=mediainfo.original_language,
            )
        if not image_info:
            return
        if not mediainfo.poster_path:
            poster_path = self.__pick_best_image_path(image_info.get("posters"))
            if poster_path:
                mediainfo.poster_path = get_runtime_setting('TMDB_IMAGE_URL')(poster_path)
        if not mediainfo.backdrop_path:
            backdrop_path = self.__pick_best_image_path(image_info.get("backdrops"))
            if backdrop_path:
                mediainfo.backdrop_path = get_runtime_setting('TMDB_IMAGE_URL')(backdrop_path)

    @staticmethod
    def __pick_best_image_path(
        images: Optional[list[dict[str, Any]]]
    ) -> Optional[str]:
        """
        从 TMDB 图片列表中选择评分和投票数最高的一张。
        """
        if not images:
            return None
        images = sorted(
            images,
            key=lambda item: (
                item.get("vote_average") or 0,
                item.get("vote_count") or 0,
            ),
            reverse=True,
        )
        file_path = images[0].get("file_path")
        return file_path if isinstance(file_path, str) else None

    @staticmethod
    def get_season_poster(
        seasoninfo: dict[str, Any], season: int
    ) -> Tuple[str, str]:
        """
        生成季海报的 Kodi 文件名与完整下载地址。

        :param seasoninfo: 含 poster_path 的 TMDB 季信息
        :param season: 季号；0 使用 season-specials-poster 命名，其余至少补足两位
        :return: 海报文件名和地址二元组；无海报路径时为两个空字符串
        """
        sea_seq = str(season).rjust(2, "0")
        if poster_path := seasoninfo.get("poster_path"):
            ext = Path(poster_path).suffix
            url = get_runtime_setting('TMDB_IMAGE_URL')(poster_path)
            # S0海报格式不同
            if season == 0:
                image_name = f"season-specials-poster{ext}"
            else:
                image_name = f"season{sea_seq}-poster{ext}"
            return image_name, url
        return "", ""

    @staticmethod
    def __get_episode_detail(
        seasoninfo: dict[str, Any], episode: int
    ) -> dict[str, Any]:
        """
        根据季信息获取集的信息。
        """
        for _episode_info in seasoninfo.get("episodes") or []:
            if isinstance(_episode_info, dict) and _episode_info.get("episode_number") == episode:
                return cast(dict[str, Any], _episode_info)
        return {}

    @staticmethod
    def __normalize_tmdb_id(value: object) -> Optional[int]:
        """
        将外部集数据中的 TMDB ID 规范化为正整数。
        """
        try:
            normalized = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return normalized if normalized > 0 else None

    @staticmethod
    def __gen_common_nfo(
        mediainfo: MediaInfo, doc: minidom.Document, root: minidom.Element
    ) -> minidom.Document:
        """
        向现有文档追加媒体标识、演职员、分级和制作信息。

        存在 IMDb 标识时将其设为默认身份，否则默认使用 TMDB 标识；时长使用分钟。
        """
        DomUtils.add_node(doc, root, "tmdbid", mediainfo.tmdb_id or "")
        uniqueid_tmdb = DomUtils.add_node(
            doc, root, "uniqueid", mediainfo.tmdb_id or ""
        )
        uniqueid_tmdb.setAttribute("type", "tmdb")
        uniqueid_tmdb.setAttribute("default", "true")
        if mediainfo.tvdb_id:
            DomUtils.add_node(doc, root, "tvdbid", str(mediainfo.tvdb_id))
            uniqueid_tvdb = DomUtils.add_node(
                doc, root, "uniqueid", str(mediainfo.tvdb_id)
            )
            uniqueid_tvdb.setAttribute("type", "tvdb")
        if mediainfo.imdb_id:
            DomUtils.add_node(doc, root, "imdbid", mediainfo.imdb_id)
            uniqueid_imdb = DomUtils.add_node(doc, root, "uniqueid", mediainfo.imdb_id)
            uniqueid_imdb.setAttribute("type", "imdb")
            uniqueid_imdb.setAttribute("default", "true")
            uniqueid_tmdb.setAttribute("default", "false")

        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        for director in mediainfo.directors:
            xdirector = DomUtils.add_node(
                doc, root, "director", director.get("name") or ""
            )
            xdirector.setAttribute("tmdbid", str(director.get("id") or ""))
        for actor in mediainfo.actors:
            xactor = DomUtils.add_node(doc, root, "actor")
            DomUtils.add_node(doc, xactor, "name", actor.get("name") or "")
            DomUtils.add_node(doc, xactor, "type", "Actor")
            DomUtils.add_node(
                doc, xactor, "role", actor.get("character") or actor.get("role") or ""
            )
            DomUtils.add_node(doc, xactor, "tmdbid", actor.get("id") or "")
            if profile_path := actor.get("profile_path"):
                DomUtils.add_node(
                    doc, xactor, "thumb", get_runtime_setting('TMDB_IMAGE_URL')(profile_path)
                )
            DomUtils.add_node(
                doc,
                xactor,
                "profile",
                f"https://www.themoviedb.org/person/{actor.get('id')}",
            )
        genres = mediainfo.genres or []
        for genre in genres:
            DomUtils.add_node(doc, root, "genre", genre.get("name") or "")
        DomUtils.add_node(doc, root, "rating", mediainfo.vote_average or "0")
        if content_rating := mediainfo.content_rating:
            DomUtils.add_node(doc, root, "mpaa", content_rating)
        for company in mediainfo.production_companies or []:
            if company.get("name"):
                DomUtils.add_node(doc, root, "studio", company.get("name"))
        for country in mediainfo.production_countries or []:
            if country.get("name"):
                DomUtils.add_node(doc, root, "country", country.get("name"))
        if mediainfo.runtime:
            DomUtils.add_node(doc, root, "runtime", str(mediainfo.runtime))

        return doc

    def __gen_movie_nfo_file(self, mediainfo: MediaInfo) -> minidom.Document:
        """
        生成电影的NFO描述文件。

        :param mediainfo: 识别后的媒体信息
        """
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "movie")
        doc = self.__gen_common_nfo(mediainfo=mediainfo, doc=doc, root=root)
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        DomUtils.add_node(doc, root, "originaltitle", mediainfo.original_title or "")
        DomUtils.add_node(doc, root, "premiered", mediainfo.release_date or "")
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")
        return doc

    def __gen_tv_nfo_file(self, mediainfo: MediaInfo) -> minidom.Document:
        """
        生成电视剧的NFO描述文件。

        :param mediainfo: 媒体信息
        """
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "tvshow")
        doc = self.__gen_common_nfo(mediainfo=mediainfo, doc=doc, root=root)
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        DomUtils.add_node(doc, root, "originaltitle", mediainfo.original_title or "")
        DomUtils.add_node(doc, root, "premiered", mediainfo.release_date or "")
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")
        DomUtils.add_node(doc, root, "season", "-1")
        DomUtils.add_node(doc, root, "episode", "-1")

        return doc

    @staticmethod
    def __gen_tv_season_nfo_file(
        seasoninfo: dict[str, Any], season: int
    ) -> minidom.Document:
        """
        生成电视剧季的NFO描述文件。

        :param seasoninfo: TMDB季媒体信息
        :param season: 季号
        """
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "season")
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(seasoninfo.get("overview") or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(seasoninfo.get("overview") or ""))
        air_date = str(seasoninfo.get("air_date") or "")
        DomUtils.add_node(
            doc, root, "title", seasoninfo.get("name") or "季 %s" % season
        )
        DomUtils.add_node(doc, root, "premiered", seasoninfo.get("air_date") or "")
        DomUtils.add_node(doc, root, "releasedate", seasoninfo.get("air_date") or "")
        DomUtils.add_node(
            doc,
            root,
            "year",
            air_date[:4],
        )
        DomUtils.add_node(doc, root, "seasonnumber", str(season))
        return doc

    @staticmethod
    def __gen_tv_episode_nfo_file(
        tmdbid: int, episodeinfo: dict[str, Any], season: int, episode: int
    ) -> minidom.Document:
        """
        生成单集 NFO，并使用该单集自身的 TMDB 标识。

        :param tmdbid: 兼容旧接口的整剧标识，当前不写入单集文档
        :param episodeinfo: 含单集标识、简介和演职员的 TMDB 数据
        :param season: 输出文档的季号
        :param episode: 输出文档的集号
        :return: 单集 NFO 文档对象
        """
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "episodedetails")
        episode_tmdb_id = TmdbScraper.__normalize_tmdb_id(episodeinfo.get("id"))
        if episode_tmdb_id:
            uniqueid = DomUtils.add_node(doc, root, "uniqueid", str(episode_tmdb_id))
            uniqueid.setAttribute("type", "tmdb")
            uniqueid.setAttribute("default", "true")
            # 应与uniqueid一致，使用剧集id否则Jellyfin/Emby会覆盖上面的uniqueid。
            DomUtils.add_node(doc, root, "tmdbid", str(episode_tmdb_id))
        air_date = str(episodeinfo.get("air_date") or "")
        DomUtils.add_node(
            doc, root, "title", episodeinfo.get("name") or "第 %s 集" % episode
        )
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(episodeinfo.get("overview") or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(episodeinfo.get("overview") or ""))
        DomUtils.add_node(doc, root, "aired", episodeinfo.get("air_date") or "")
        DomUtils.add_node(
            doc,
            root,
            "year",
            air_date[:4],
        )
        DomUtils.add_node(doc, root, "season", str(season))
        DomUtils.add_node(doc, root, "episode", str(episode))
        DomUtils.add_node(doc, root, "rating", episodeinfo.get("vote_average") or "0")
        directors = episodeinfo.get("crew") or []
        for director in directors:
            if director.get("known_for_department") == "Directing":
                xdirector = DomUtils.add_node(
                    doc, root, "director", director.get("name") or ""
                )
                xdirector.setAttribute("tmdbid", str(director.get("id") or ""))
        actors = episodeinfo.get("guest_stars") or []
        for actor in actors:
            if actor.get("known_for_department") == "Acting":
                xactor = DomUtils.add_node(doc, root, "actor")
                DomUtils.add_node(doc, xactor, "name", actor.get("name") or "")
                DomUtils.add_node(doc, xactor, "type", "Actor")
                DomUtils.add_node(doc, xactor, "tmdbid", actor.get("id") or "")
                if profile_path := actor.get("profile_path"):
                    DomUtils.add_node(
                        doc, xactor, "thumb", get_runtime_setting('TMDB_IMAGE_URL')(profile_path)
                    )
                DomUtils.add_node(
                    doc,
                    xactor,
                    "profile",
                    f"https://www.themoviedb.org/person/{actor.get('id')}",
                )
        return doc
