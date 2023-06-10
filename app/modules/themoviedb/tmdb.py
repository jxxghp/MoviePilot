import traceback
from functools import lru_cache
from typing import Optional, Tuple, List

import zhconv
from lxml import etree
from tmdbv3api import TMDb, Search, Movie, TV, Season, Episode
from tmdbv3api.exceptions import TMDbException

from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.types import MediaType


class TmdbHelper:
    """
    TMDB识别匹配
    """

    tmdb: TMDb = None
    search: Search = None
    movie: Movie = None
    tv: TV = None

    def __init__(self):
        # TMDB主体
        self.tmdb = TMDb()
        # 域名
        self.tmdb.domain = settings.TMDB_API_DOMAIN
        # 开启缓存
        self.tmdb.cache = True
        # 缓存大小
        self.tmdb.REQUEST_CACHE_MAXSIZE = 256
        # APIKEY
        self.tmdb.api_key = settings.TMDB_API_KEY
        # 语种
        self.tmdb.language = 'zh'
        # 代理
        self.tmdb.proxies = settings.PROXY
        # 调试模式
        self.tmdb.debug = False
        # 查询对象
        self.search = Search()
        self.movie = Movie()
        self.tv = TV()
        self.season = Season()
        self.episode = Episode()

    def search_multiis(self, title: str) -> List[dict]:
        """
        同时查询模糊匹配的电影、电视剧TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        multis = self.search.multi({"query": title}) or []
        for multi in multis:
            if multi.get("media_type") in ["movie", "tv"]:
                multi['media_type'] = MediaType.MOVIE if multi.get("media_type") == "movie" else MediaType.TV
                ret_infos.append(multi)
        return ret_infos

    def search_movies(self, title: str, year: str) -> List[dict]:
        """
        查询模糊匹配的所有电影TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        if year:
            movies = self.search.movies({"query": title, "year": year}) or []
        else:
            movies = self.search.movies({"query": title}) or []
        for movie in movies:
            if title in movie.get("title"):
                movie['media_type'] = MediaType.MOVIE
                ret_infos.append(movie)
        return ret_infos

    def search_tv_tmdbinfos(self, title: str, year: str) -> List[dict]:
        """
        查询模糊匹配的所有电视剧TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        if year:
            tvs = self.search.tv_shows({"query": title, "first_air_date_year": year}) or []
        else:
            tvs = self.search.tv_shows({"query": title}) or []
        for tv in tvs:
            if title in tv.get("name"):
                tv['media_type'] = MediaType.TV
                ret_infos.append(tv)
        return ret_infos

    @staticmethod
    def __compare_names(file_name: str, tmdb_names: list) -> bool:
        """
        比较文件名是否匹配，忽略大小写和特殊字符
        :param file_name: 识别的文件名或者种子名
        :param tmdb_names: TMDB返回的译名
        :return: True or False
        """
        if not file_name or not tmdb_names:
            return False
        if not isinstance(tmdb_names, list):
            tmdb_names = [tmdb_names]
        file_name = StringUtils.clear_special_chars(file_name).upper()
        for tmdb_name in tmdb_names:
            tmdb_name = StringUtils.clear_special_chars(tmdb_name).strip().upper()
            if file_name == tmdb_name:
                return True
        return False

    def __get_names(self, mtype: MediaType, tmdb_id: int) -> Tuple[Optional[dict], List[str]]:
        """
        搜索tmdb中所有的标题和译名，用于名称匹配
        :param mtype: 类型：电影、电视剧、动漫
        :param tmdb_id: TMDB的ID
        :return: 所有译名的清单
        """
        if not mtype or not tmdb_id:
            return {}, []
        ret_names = []
        tmdb_info = self.get_info(mtype=mtype, tmdbid=tmdb_id)
        if not tmdb_info:
            return tmdb_info, []
        if mtype == MediaType.MOVIE:
            alternative_titles = tmdb_info.get("alternative_titles", {}).get("titles", [])
            for alternative_title in alternative_titles:
                title = alternative_title.get("title")
                if title and title not in ret_names:
                    ret_names.append(title)
            translations = tmdb_info.get("translations", {}).get("translations", [])
            for translation in translations:
                title = translation.get("data", {}).get("title")
                if title and title not in ret_names:
                    ret_names.append(title)
        else:
            alternative_titles = tmdb_info.get("alternative_titles", {}).get("results", [])
            for alternative_title in alternative_titles:
                name = alternative_title.get("title")
                if name and name not in ret_names:
                    ret_names.append(name)
            translations = tmdb_info.get("translations", {}).get("translations", [])
            for translation in translations:
                name = translation.get("data", {}).get("name")
                if name and name not in ret_names:
                    ret_names.append(name)
        return tmdb_info, ret_names

    def match(self, name: str,
              mtype: MediaType,
              year: str = None,
              season_year: str = None,
              season_number: int = None) -> Optional[dict]:
        """
        搜索tmdb中的媒体信息，匹配返回一条尽可能正确的信息
        :param name: 剑索的名称
        :param mtype: 类型：电影、电视剧
        :param year: 年份，如要是季集需要是首播年份(first_air_date)
        :param season_year: 当前季集年份
        :param season_number: 季集，整数
        :return: TMDB的INFO，同时会将mtype赋值到media_type中
        """
        if not self.search:
            return None
        if not name:
            return None
        # TMDB搜索
        info = {}
        if mtype == MediaType.MOVIE:
            year_range = [year]
            if year:
                year_range.append(str(int(year) + 1))
                year_range.append(str(int(year) - 1))
            for year in year_range:
                logger.debug(
                    f"正在识别{mtype.value}：{name}, 年份={year} ...")
                info = self.__search_movie_by_name(name, year)
                if info:
                    info['media_type'] = MediaType.MOVIE
                    logger.info("%s 识别到 电影：TMDBID=%s, 名称=%s, 上映日期=%s" % (
                        name,
                        info.get('id'),
                        info.get('title'),
                        info.get('release_date')))
                    break
        else:
            # 有当前季和当前季集年份，使用精确匹配
            if season_year and season_number:
                logger.debug(
                    f"正在识别{mtype.value}：{name}, 季集={season_number}, 季集年份={season_year} ...")
                info = self.__search_tv_by_season(name,
                                                  season_year,
                                                  season_number)
            if not info:
                logger.debug(
                    f"正在识别{mtype.value}：{name}, 年份={year} ...")
                info = self.__search_tv_by_name(name,
                                                year)
            if info:
                info['media_type'] = MediaType.TV
                logger.info("%s 识别到 电视剧：TMDBID=%s, 名称=%s, 首播日期=%s" % (
                    name,
                    info.get('id'),
                    info.get('name'),
                    info.get('first_air_date')))
        # 返回
        if not info:
            logger.info("%s 以年份 %s 在TMDB中未找到%s信息!" % (
                name, year, mtype.value if mtype else ""))
        return info

    def __search_movie_by_name(self, name: str, year: str) -> Optional[dict]:
        """
        根据名称查询电影TMDB匹配
        :param name: 识别的文件名或种子名
        :param year: 电影上映日期
        :return: 匹配的媒体信息
        """
        try:
            if year:
                movies = self.search.movies({"query": name, "year": year})
            else:
                movies = self.search.movies({"query": name})
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{err}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{e}")
            print(traceback.print_exc())
            return None
        logger.debug(f"API返回：{str(self.search.total_results)}")
        if len(movies) == 0:
            logger.debug(f"{name} 未找到相关电影信息!")
            return {}
        else:
            info = {}
            if year:
                for movie in movies:
                    if movie.get('release_date'):
                        if self.__compare_names(name, movie.get('title')) \
                                and movie.get('release_date')[0:4] == str(year):
                            return movie
                        if self.__compare_names(name, movie.get('original_title')) \
                                and movie.get('release_date')[0:4] == str(year):
                            return movie
            else:
                for movie in movies:
                    if self.__compare_names(name, movie.get('title')) \
                            or self.__compare_names(name, movie.get('original_title')):
                        return movie
            if not info:
                index = 0
                for movie in movies:
                    if year:
                        if not movie.get('release_date'):
                            continue
                        if movie.get('release_date')[0:4] != str(year):
                            continue
                        index += 1
                        info, names = self.__get_names(MediaType.MOVIE, movie.get("id"))
                        if self.__compare_names(name, names):
                            return info
                    else:
                        index += 1
                        info, names = self.__get_names(MediaType.MOVIE, movie.get("id"))
                        if self.__compare_names(name, names):
                            return info
                    if index > 5:
                        break
        return {}

    def __search_tv_by_name(self, name: str, year: str) -> Optional[dict]:
        """
        根据名称查询电视剧TMDB匹配
        :param name: 识别的文件名或者种子名
        :param year: 电视剧的首播年份
        :return: 匹配的媒体信息
        """
        try:
            if year:
                tvs = self.search.tv_shows({"query": name, "first_air_date_year": year})
            else:
                tvs = self.search.tv_shows({"query": name})
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{err}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{e}")
            print(traceback.print_exc())
            return None
        logger.debug(f"API返回：{str(self.search.total_results)}")
        if len(tvs) == 0:
            logger.debug(f"{name} 未找到相关剧集信息!")
            return {}
        else:
            info = {}
            if year:
                for tv in tvs:
                    if tv.get('first_air_date'):
                        if self.__compare_names(name, tv.get('name')) \
                                and tv.get('first_air_date')[0:4] == str(year):
                            return tv
                        if self.__compare_names(name, tv.get('original_name')) \
                                and tv.get('first_air_date')[0:4] == str(year):
                            return tv
            else:
                for tv in tvs:
                    if self.__compare_names(name, tv.get('name')) \
                            or self.__compare_names(name, tv.get('original_name')):
                        return tv
            if not info:
                index = 0
                for tv in tvs:
                    if year:
                        if not tv.get('first_air_date'):
                            continue
                        if tv.get('first_air_date')[0:4] != str(year):
                            continue
                        index += 1
                        info, names = self.__get_names(MediaType.TV, tv.get("id"))
                        if self.__compare_names(name, names):
                            return info
                    else:
                        index += 1
                        info, names = self.__get_names(MediaType.TV, tv.get("id"))
                        if self.__compare_names(name, names):
                            return info
                    if index > 5:
                        break
        return {}

    def __search_tv_by_season(self, name: str, season_year: str, season_number: int) -> Optional[dict]:
        """
        根据电视剧的名称和季的年份及序号匹配TMDB
        :param name: 识别的文件名或者种子名
        :param season_year: 季的年份
        :param season_number: 季序号
        :return: 匹配的媒体信息
        """

        def __season_match(tv_info: dict, _season_year: str) -> bool:
            if not tv_info:
                return False
            try:
                seasons = self.__get_tv_seasons(tv_info)
                for season, season_info in seasons.items():
                    if season_info.get("air_date"):
                        if season_info.get("air_date")[0:4] == str(_season_year) \
                                and season == int(season_number):
                            return True
            except Exception as e1:
                logger.error(f"连接TMDB出错：{e1}")
                print(traceback.print_exc())
                return False
            return False

        try:
            tvs = self.search.tv_shows({"query": name})
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{err}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{e}")
            print(traceback.print_exc())
            return None

        if len(tvs) == 0:
            logger.debug("%s 未找到季%s相关信息!" % (name, season_number))
            return {}
        else:
            for tv in tvs:
                if (self.__compare_names(name, tv.get('name'))
                    or self.__compare_names(name, tv.get('original_name'))) \
                        and (tv.get('first_air_date') and tv.get('first_air_date')[0:4] == str(season_year)):
                    return tv

            for tv in tvs[:5]:
                info, names = self.__get_names(MediaType.TV, tv.get("id"))
                if not self.__compare_names(name, names):
                    continue
                if __season_match(tv_info=info, _season_year=season_year):
                    return info
        return {}

    @staticmethod
    def __get_tv_seasons(tv_info: dict) -> Optional[dict]:
        """
        查询TMDB电视剧的所有季
        :param tv_info: TMDB 的季信息
        :return: 包括每季集数的字典
        """
        """
        "seasons": [
            {
              "air_date": "2006-01-08",
              "episode_count": 11,
              "id": 3722,
              "name": "特别篇",
              "overview": "",
              "poster_path": "/snQYndfsEr3Sto2jOmkmsQuUXAQ.jpg",
              "season_number": 0
            },
            {
              "air_date": "2005-03-27",
              "episode_count": 9,
              "id": 3718,
              "name": "第 1 季",
              "overview": "",
              "poster_path": "/foM4ImvUXPrD2NvtkHyixq5vhPx.jpg",
              "season_number": 1
            }
        ]
        """
        if not tv_info:
            return {}
        ret_seasons = {}
        for season_info in tv_info.get("seasons") or []:
            if not season_info.get("season_number"):
                continue
            ret_seasons[season_info.get("season_number")] = season_info
        return ret_seasons

    def search_multi(self, name: str) -> Optional[dict]:
        """
        根据名称同时查询电影和电视剧，不带年份
        :param name: 识别的文件名或种子名
        :return: 匹配的媒体信息
        """
        try:
            multis = self.search.multi({"query": name}) or []
        except TMDbException as err:
            logger.error(f"连接TMDB出错：{err}")
            return None
        except Exception as e:
            logger.error(f"连接TMDB出错：{e}")
            print(traceback.print_exc())
            return None
        logger.debug(f"API返回：{str(self.search.total_results)}")
        if len(multis) == 0:
            logger.debug(f"{name} 未找到相关媒体息!")
            return {}
        else:
            info = {}
            for multi in multis:
                if multi.get("media_type") == "movie":
                    if self.__compare_names(name, multi.get('title')) \
                            or self.__compare_names(name, multi.get('original_title')):
                        info = multi
                elif multi.get("media_type") == "tv":
                    if self.__compare_names(name, multi.get('name')) \
                            or self.__compare_names(name, multi.get('original_name')):
                        info = multi
            if not info:
                for multi in multis[:5]:
                    if multi.get("media_type") == "movie":
                        movie_info, names = self.__get_names(MediaType.MOVIE, multi.get("id"))
                        if self.__compare_names(name, names):
                            info = movie_info
                    elif multi.get("media_type") == "tv":
                        tv_info, names = self.__get_names(MediaType.TV, multi.get("id"))
                        if self.__compare_names(name, names):
                            info = tv_info
        # 返回
        if info:
            info['media_type'] = MediaType.MOVIE if info.get('media_type') in ['movie',
                                                                               MediaType.MOVIE] else MediaType.TV
        else:
            logger.info("%s 在TMDB中未找到媒体信息!" % name)
        return info

    @lru_cache(maxsize=128)
    def search_web(self, name: str, mtype: MediaType) -> Optional[dict]:
        """
        搜索TMDB网站，直接抓取结果，结果只有一条时才返回
        :param name: 名称
        :param mtype: 媒体类型
        """
        if not name:
            return None
        if StringUtils.is_chinese(name):
            return {}
        logger.info("正在从TheDbMovie网站查询：%s ..." % name)
        tmdb_url = "https://www.themoviedb.org/search?query=%s" % name
        res = RequestUtils(timeout=5).get_res(url=tmdb_url)
        if res and res.status_code == 200:
            html_text = res.text
            if not html_text:
                return None
            try:
                tmdb_links = []
                html = etree.HTML(html_text)
                if mtype == MediaType.TV:
                    links = html.xpath("//a[@data-id and @data-media-type='tv']/@href")
                else:
                    links = html.xpath("//a[@data-id]/@href")
                for link in links:
                    if not link or (not link.startswith("/tv") and not link.startswith("/movie")):
                        continue
                    if link not in tmdb_links:
                        tmdb_links.append(link)
                if len(tmdb_links) == 1:
                    tmdbinfo = self.get_info(
                        mtype=MediaType.TV if tmdb_links[0].startswith("/tv") else MediaType.MOVIE,
                        tmdbid=tmdb_links[0].split("/")[-1])
                    if tmdbinfo:
                        if mtype == MediaType.TV and tmdbinfo.get('media_type') != MediaType.TV:
                            return {}
                        if tmdbinfo.get('media_type') == MediaType.MOVIE:
                            logger.info("%s 从WEB识别到 电影：TMDBID=%s, 名称=%s, 上映日期=%s" % (
                                name,
                                tmdbinfo.get('id'),
                                tmdbinfo.get('title'),
                                tmdbinfo.get('release_date')))
                        else:
                            logger.info("%s 从WEB识别到 电视剧：TMDBID=%s, 名称=%s, 首播日期=%s" % (
                                name,
                                tmdbinfo.get('id'),
                                tmdbinfo.get('name'),
                                tmdbinfo.get('first_air_date')))
                    return tmdbinfo
                elif len(tmdb_links) > 1:
                    logger.info("%s TMDB网站返回数据过多：%s" % (name, len(tmdb_links)))
                else:
                    logger.info("%s TMDB网站未查询到媒体信息！" % name)
            except Exception as err:
                print(str(err))
                return None
        return None

    def get_info(self,
                 mtype: MediaType,
                 tmdbid: int) -> dict:
        """
        给定TMDB号，查询一条媒体信息
        :param mtype: 类型：电影、电视剧、动漫，为空时都查（此时用不上年份）
        :param tmdbid: TMDB的ID，有tmdbid时优先使用tmdbid，否则使用年份和标题
        """

        def __get_genre_ids(genres: list) -> list:
            """
            从TMDB详情中获取genre_id列表
            """
            if not genres:
                return []
            genre_ids = []
            for genre in genres:
                genre_ids.append(genre.get('id'))
            return genre_ids

        # 设置语言
        if mtype == MediaType.MOVIE:
            tmdb_info = self.__get_movie_detail(tmdbid)
            if tmdb_info:
                tmdb_info['media_type'] = MediaType.MOVIE
        else:
            tmdb_info = self.__get_tv_detail(tmdbid)
            if tmdb_info:
                tmdb_info['media_type'] = MediaType.TV
        if tmdb_info:
            # 转换genreid
            tmdb_info['genre_ids'] = __get_genre_ids(tmdb_info.get('genres'))
            # 转换中文标题
            self.__update_tmdbinfo_cn_title(tmdb_info)

        return tmdb_info

    @staticmethod
    def __update_tmdbinfo_cn_title(tmdb_info: dict):
        """
        更新TMDB信息中的中文名称
        """

        def __get_tmdb_chinese_title(tmdbinfo):
            """
            从别名中获取中文标题
            """
            if not tmdbinfo:
                return None
            if tmdbinfo.get("media_type") == MediaType.MOVIE:
                alternative_titles = tmdbinfo.get("alternative_titles", {}).get("titles", [])
            else:
                alternative_titles = tmdbinfo.get("alternative_titles", {}).get("results", [])
            for alternative_title in alternative_titles:
                iso_3166_1 = alternative_title.get("iso_3166_1")
                if iso_3166_1 == "CN":
                    title = alternative_title.get("title")
                    if title and StringUtils.is_chinese(title) \
                            and zhconv.convert(title, "zh-hans") == title:
                        return title
            return tmdbinfo.get("title") if tmdbinfo.get("media_type") == MediaType.MOVIE else tmdbinfo.get("name")

        # 查找中文名
        org_title = tmdb_info.get("title") \
            if tmdb_info.get("media_type") == MediaType.MOVIE \
            else tmdb_info.get("name")
        if not StringUtils.is_chinese(org_title):
            cn_title = __get_tmdb_chinese_title(tmdb_info)
            if cn_title and cn_title != org_title:
                if tmdb_info.get("media_type") == MediaType.MOVIE:
                    tmdb_info['title'] = cn_title
                else:
                    tmdb_info['name'] = cn_title

    def __get_movie_detail(self,
                           tmdbid: int,
                           append_to_response: str = "images,"
                                                     "credits,"
                                                     "alternative_titles,"
                                                     "translations,"
                                                     "external_ids") -> Optional[dict]:
        """
        获取电影的详情
        :param tmdbid: TMDB ID
        :return: TMDB信息
        """
        """
        {
          "adult": false,
          "backdrop_path": "/r9PkFnRUIthgBp2JZZzD380MWZy.jpg",
          "belongs_to_collection": {
            "id": 94602,
            "name": "穿靴子的猫（系列）",
            "poster_path": "/anHwj9IupRoRZZ98WTBvHpTiE6A.jpg",
            "backdrop_path": "/feU1DWV5zMWxXUHJyAIk3dHRQ9c.jpg"
          },
          "budget": 90000000,
          "genres": [
            {
              "id": 16,
              "name": "动画"
            },
            {
              "id": 28,
              "name": "动作"
            },
            {
              "id": 12,
              "name": "冒险"
            },
            {
              "id": 35,
              "name": "喜剧"
            },
            {
              "id": 10751,
              "name": "家庭"
            },
            {
              "id": 14,
              "name": "奇幻"
            }
          ],
          "homepage": "",
          "id": 315162,
          "imdb_id": "tt3915174",
          "original_language": "en",
          "original_title": "Puss in Boots: The Last Wish",
          "overview": "时隔11年，臭屁自大又爱卖萌的猫大侠回来了！如今的猫大侠（安东尼奥·班德拉斯 配音），依旧幽默潇洒又不拘小节、数次“花式送命”后，九条命如今只剩一条，于是不得不请求自己的老搭档兼“宿敌”——迷人的软爪妞（萨尔玛·海耶克 配音）来施以援手来恢复自己的九条生命。",
          "popularity": 8842.129,
          "poster_path": "/rnn30OlNPiC3IOoWHKoKARGsBRK.jpg",
          "production_companies": [
            {
              "id": 33,
              "logo_path": "/8lvHyhjr8oUKOOy2dKXoALWKdp0.png",
              "name": "Universal Pictures",
              "origin_country": "US"
            },
            {
              "id": 521,
              "logo_path": "/kP7t6RwGz2AvvTkvnI1uteEwHet.png",
              "name": "DreamWorks Animation",
              "origin_country": "US"
            }
          ],
          "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "United States of America"
            }
          ],
          "release_date": "2022-12-07",
          "revenue": 260725470,
          "runtime": 102,
          "spoken_languages": [
            {
              "english_name": "English",
              "iso_639_1": "en",
              "name": "English"
            },
            {
              "english_name": "Spanish",
              "iso_639_1": "es",
              "name": "Español"
            }
          ],
          "status": "Released",
          "tagline": "",
          "title": "穿靴子的猫2",
          "video": false,
          "vote_average": 8.614,
          "vote_count": 2291
        }
        """
        if not self.movie:
            return {}
        try:
            logger.info("正在查询TMDB电影：%s ..." % tmdbid)
            tmdbinfo = self.movie.details(tmdbid, append_to_response)
            if tmdbinfo:
                logger.info(f"{tmdbid} 查询结果：{tmdbinfo.get('title')}")
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return None

    def __get_tv_detail(self,
                        tmdbid: int,
                        append_to_response: str = "images,"
                                                  "credits,"
                                                  "alternative_titles,"
                                                  "translations,"
                                                  "external_ids") -> Optional[dict]:
        """
        获取电视剧的详情
        :param tmdbid: TMDB ID
        :return: TMDB信息
        """
        """
        {
          "adult": false,
          "backdrop_path": "/uDgy6hyPd82kOHh6I95FLtLnj6p.jpg",
          "created_by": [
            {
              "id": 35796,
              "credit_id": "5e84f06a3344c600153f6a57",
              "name": "Craig Mazin",
              "gender": 2,
              "profile_path": "/uEhna6qcMuyU5TP7irpTUZ2ZsZc.jpg"
            },
            {
              "id": 1295692,
              "credit_id": "5e84f03598f1f10016a985c0",
              "name": "Neil Druckmann",
              "gender": 2,
              "profile_path": "/bVUsM4aYiHbeSYE1xAw2H5Z1ANU.jpg"
            }
          ],
          "episode_run_time": [],
          "first_air_date": "2023-01-15",
          "genres": [
            {
              "id": 18,
              "name": "剧情"
            },
            {
              "id": 10765,
              "name": "Sci-Fi & Fantasy"
            },
            {
              "id": 10759,
              "name": "动作冒险"
            }
          ],
          "homepage": "https://www.hbo.com/the-last-of-us",
          "id": 100088,
          "in_production": true,
          "languages": [
            "en"
          ],
          "last_air_date": "2023-01-15",
          "last_episode_to_air": {
            "air_date": "2023-01-15",
            "episode_number": 1,
            "id": 2181581,
            "name": "当你迷失在黑暗中",
            "overview": "在一场全球性的流行病摧毁了文明之后，一个顽强的幸存者负责照顾一个 14 岁的小女孩，她可能是人类最后的希望。",
            "production_code": "",
            "runtime": 81,
            "season_number": 1,
            "show_id": 100088,
            "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
            "vote_average": 8,
            "vote_count": 33
          },
          "name": "最后生还者",
          "next_episode_to_air": {
            "air_date": "2023-01-22",
            "episode_number": 2,
            "id": 4071039,
            "name": "虫草变异菌",
            "overview": "",
            "production_code": "",
            "runtime": 55,
            "season_number": 1,
            "show_id": 100088,
            "still_path": "/jkUtYTmeap6EvkHI4n0j5IRFrIr.jpg",
            "vote_average": 10,
            "vote_count": 1
          },
          "networks": [
            {
              "id": 49,
              "name": "HBO",
              "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png",
              "origin_country": "US"
            }
          ],
          "number_of_episodes": 9,
          "number_of_seasons": 1,
          "origin_country": [
            "US"
          ],
          "original_language": "en",
          "original_name": "The Last of Us",
          "overview": "不明真菌疫情肆虐之后的美国，被真菌感染的人都变成了可怕的怪物，乔尔（Joel）为了换回武器答应将小女孩儿艾莉（Ellie）送到指定地点，由此开始了两人穿越美国的漫漫旅程。",
          "popularity": 5585.639,
          "poster_path": "/nOY3VBFO0VnlN9nlRombnMTztyh.jpg",
          "production_companies": [
            {
              "id": 3268,
              "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png",
              "name": "HBO",
              "origin_country": "US"
            },
            {
              "id": 11073,
              "logo_path": "/aCbASRcI1MI7DXjPbSW9Fcv9uGR.png",
              "name": "Sony Pictures Television Studios",
              "origin_country": "US"
            },
            {
              "id": 23217,
              "logo_path": "/kXBZdQigEf6QiTLzo6TFLAa7jKD.png",
              "name": "Naughty Dog",
              "origin_country": "US"
            },
            {
              "id": 115241,
              "logo_path": null,
              "name": "The Mighty Mint",
              "origin_country": "US"
            },
            {
              "id": 119645,
              "logo_path": null,
              "name": "Word Games",
              "origin_country": "US"
            },
            {
              "id": 125281,
              "logo_path": "/3hV8pyxzAJgEjiSYVv1WZ0ZYayp.png",
              "name": "PlayStation Productions",
              "origin_country": "US"
            }
          ],
          "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "United States of America"
            }
          ],
          "seasons": [
            {
              "air_date": "2023-01-15",
              "episode_count": 9,
              "id": 144593,
              "name": "第 1 季",
              "overview": "",
              "poster_path": "/aUQKIpZZ31KWbpdHMCmaV76u78T.jpg",
              "season_number": 1
            }
          ],
          "spoken_languages": [
            {
              "english_name": "English",
              "iso_639_1": "en",
              "name": "English"
            }
          ],
          "status": "Returning Series",
          "tagline": "",
          "type": "Scripted",
          "vote_average": 8.924,
          "vote_count": 601
        }
        """
        if not self.tv:
            return {}
        try:
            logger.info("正在查询TMDB电视剧：%s ..." % tmdbid)
            tmdbinfo = self.tv.details(tmdbid, append_to_response)
            if tmdbinfo:
                logger.info(f"{tmdbid} 查询结果：{tmdbinfo.get('name')}")
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return None

    def get_tv_season_detail(self, tmdbid: int, season: int):
        """
        获取电视剧季的详情
        :param tmdbid: TMDB ID
        :param season: 季，数字
        :return: TMDB信息
        """
        """
        {
          "_id": "5e614cd3357c00001631a6ef",
          "air_date": "2023-01-15",
          "episodes": [
            {
              "air_date": "2023-01-15",
              "episode_number": 1,
              "id": 2181581,
              "name": "当你迷失在黑暗中",
              "overview": "在一场全球性的流行病摧毁了文明之后，一个顽强的幸存者负责照顾一个 14 岁的小女孩，她可能是人类最后的希望。",
              "production_code": "",
              "runtime": 81,
              "season_number": 1,
              "show_id": 100088,
              "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
              "vote_average": 8,
              "vote_count": 33,
              "crew": [
                {
                  "job": "Writer",
                  "department": "Writing",
                  "credit_id": "619c370063536a00619a08ee",
                  "adult": false,
                  "gender": 2,
                  "id": 35796,
                  "known_for_department": "Writing",
                  "name": "Craig Mazin",
                  "original_name": "Craig Mazin",
                  "popularity": 15.211,
                  "profile_path": "/uEhna6qcMuyU5TP7irpTUZ2ZsZc.jpg"
                },
              ],
              "guest_stars": [
                {
                  "character": "Marlene",
                  "credit_id": "63c4ca5e5f2b8d00aed539fc",
                  "order": 500,
                  "adult": false,
                  "gender": 1,
                  "id": 1253388,
                  "known_for_department": "Acting",
                  "name": "Merle Dandridge",
                  "original_name": "Merle Dandridge",
                  "popularity": 21.679,
                  "profile_path": "/lKwHdTtDf6NGw5dUrSXxbfkZLEk.jpg"
                }
              ]
            },
          ],
          "name": "第 1 季",
          "overview": "",
          "id": 144593,
          "poster_path": "/aUQKIpZZ31KWbpdHMCmaV76u78T.jpg",
          "season_number": 1
        }
        """
        if not self.season:
            return {}
        try:
            logger.info("正在查询TMDB电视剧：%s，季：%s ..." % (tmdbid, season))
            tmdbinfo = self.season.details(tmdbid, season)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return {}

    def get_tv_episode_detail(self, tmdbid: int, season: int, episode: int):
        """
        获取电视剧集的详情
        :param tmdbid: TMDB ID
        :param season: 季，数字
        :param episode: 集，数字
        """
        if not self.episode:
            return {}
        try:
            logger.info("正在查询TMDB集图片：%s，季：%s，集：%s ..." % (tmdbid, season, episode))
            tmdbinfo = self.episode.details(tmdbid, season, episode)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return {}
