import re
from pathlib import Path
from typing import List, Optional, Tuple, Union

import cn2an

from app import schemas
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.log import logger
from app.modules import _ModuleBase
from app.modules.douban.apiv2 import DoubanApi
from app.modules.douban.douban_cache import DoubanCache
from app.modules.douban.scraper import DoubanScraper
from app.schemas import MediaPerson
from app.schemas.types import MediaType
from app.utils.common import retry
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils


class DoubanModule(_ModuleBase):
    doubanapi: DoubanApi = None
    scraper: DoubanScraper = None
    cache: DoubanCache = None

    def init_module(self) -> None:
        self.doubanapi = DoubanApi()
        self.scraper = DoubanScraper()
        self.cache = DoubanCache()

    def stop(self):
        self.doubanapi.close()

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        ret = RequestUtils().get_res("https://movie.douban.com/")
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接豆瓣，错误码：{ret.status_code}"
        return False, "豆瓣网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def get_name() -> str:
        return "豆瓣"

    def recognize_media(self, meta: MetaBase = None,
                        mtype: MediaType = None,
                        doubanid: str = None,
                        cache: bool = True,
                        **kwargs) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与doubanid配套
        :param doubanid: 豆瓣ID
        :param cache:    是否使用缓存
        :return: 识别的媒体信息，包括剧集信息
        """
        if not doubanid and not meta:
            return None

        if meta and not doubanid \
                and settings.RECOGNIZE_SOURCE != "douban":
            return None

        if not meta:
            # 未提供元数据时，直接查询豆瓣信息，不使用缓存
            cache_info = {}
        elif not meta.name:
            logger.error("识别媒体信息时未提供元数据名称")
            return None
        else:
            # 读取缓存
            if mtype:
                meta.type = mtype
            if doubanid:
                meta.doubanid = doubanid
            cache_info = self.cache.get(meta)

        # 识别豆瓣信息
        if not cache_info or not cache:
            # 缓存没有或者强制不使用缓存
            if doubanid:
                # 直接查询详情
                info = self.douban_info(doubanid=doubanid, mtype=mtype or meta.type)
            elif meta:
                info = {}
                # 使用中英文名分别识别，去重去空，但要保持顺序
                names = list(dict.fromkeys([k for k in [meta.cn_name, meta.en_name] if k]))
                for name in names:
                    if meta.begin_season:
                        logger.info(f"正在识别 {name} 第{meta.begin_season}季 ...")
                    else:
                        logger.info(f"正在识别 {name} ...")
                    # 匹配豆瓣信息
                    match_info = self.match_doubaninfo(name=name,
                                                       mtype=mtype or meta.type,
                                                       year=meta.year,
                                                       season=meta.begin_season)
                    if match_info:
                        # 匹配到豆瓣信息
                        info = self.douban_info(
                            doubanid=match_info.get("id"),
                            mtype=mtype or meta.type
                        )
                        if info:
                            break
            else:
                logger.error("识别媒体信息时未提供元数据或豆瓣ID")
                return None

            # 保存到缓存
            if meta and cache:
                self.cache.update(meta, info)
        else:
            # 使用缓存信息
            if cache_info.get("title"):
                logger.info(f"{meta.name} 使用豆瓣识别缓存：{cache_info.get('title')}")
                info = self.douban_info(mtype=cache_info.get("type"),
                                        doubanid=cache_info.get("id"))
            else:
                logger.info(f"{meta.name} 使用豆瓣识别缓存：无法识别")
                info = None

        if info:
            # 赋值TMDB信息并返回
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

    def douban_info(self, doubanid: str, mtype: MediaType = None) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :param mtype:    媒体类型
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

    def movie_showing(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取正在上映的电影
        """
        infos = self.doubanapi.movie_showing(start=(page - 1) * count,
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

    def tv_weekly_global(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣本周口碑外国剧
        """
        infos = self.doubanapi.tv_global_best_weekly(start=(page - 1) * count,
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

    def movie_hot(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣热门电影
        """
        infos = self.doubanapi.movie_hot_gaia(start=(page - 1) * count,
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

    def search_medias(self, meta: MetaBase) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :reutrn: 媒体信息
        """
        if settings.SEARCH_SOURCE and "douban" not in settings.SEARCH_SOURCE:
            return None
        if not meta.name:
            return []
        result = self.doubanapi.search(meta.name)
        if not result or not result.get("items"):
            return []
        # 返回数据
        ret_medias = []
        for item_obj in result.get("items"):
            if meta.type and meta.type != MediaType.UNKNOWN and meta.type.value != item_obj.get("type_name"):
                continue
            if item_obj.get("type_name") not in (MediaType.TV.value, MediaType.MOVIE.value):
                continue
            if meta.name not in item_obj.get("target", {}).get("title"):
                continue
            ret_medias.append(MediaInfo(douban_info=item_obj.get("target")))
        # 将搜索词中的季写入标题中
        if ret_medias and meta.begin_season:
            # 小写数据转大写
            season_str = cn2an.an2cn(meta.begin_season, "low")
            for media in ret_medias:
                if media.type == MediaType.TV:
                    media.title = f"{media.title} 第{season_str}季"
                    media.season = meta.begin_season
        return ret_medias

    def search_persons(self, name: str) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息
        """
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

    @retry(Exception, 5, 3, 3, logger=logger)
    def match_doubaninfo(self, name: str, imdbid: str = None,
                         mtype: MediaType = None, year: str = None, season: int = None) -> dict:
        """
        搜索和匹配豆瓣信息
        :param name:  名称
        :param imdbid:  IMDB ID
        :param mtype:  类型
        :param year:  年份
        :param season:  季号
        """
        if imdbid:
            # 优先使用IMDBID查询
            logger.info(f"开始使用IMDBID {imdbid} 查询豆瓣信息 ...")
            result = self.doubanapi.imdbid(imdbid)
            if result:
                doubanid = result.get("id")
                if doubanid and not str(doubanid).isdigit():
                    doubanid = re.search(r"\d+", doubanid).group(0)
                    result["id"] = doubanid
                logger.info(f"{imdbid} 查询到豆瓣信息：{result.get('title')}")
                return result
        # 搜索
        logger.info(f"开始使用名称 {name} 匹配豆瓣信息 ...")
        result = self.doubanapi.search(f"{name} {year or ''}".strip())
        if not result or not result.get("items"):
            logger.warn(f"未找到 {name} 的豆瓣信息")
            return {}
        # 触发rate limit
        if "search_access_rate_limit" in result.values():
            logger.warn(f"触发豆瓣API速率限制 错误信息 {result} ...")
            raise Exception("触发豆瓣API速率限制")
        for item_obj in result.get("items"):
            type_name = item_obj.get("type_name")
            if type_name not in [MediaType.TV.value, MediaType.MOVIE.value]:
                continue
            if mtype and mtype.value != type_name:
                continue
            if mtype and mtype == MediaType.TV and not season:
                season = 1
            item = item_obj.get("target")
            title = item.get("title")
            if not title:
                continue
            meta = MetaInfo(title)
            if type_name == MediaType.TV.value:
                meta.type = MediaType.TV
                meta.begin_season = meta.begin_season or 1
            if meta.name == name \
                    and ((not season and not meta.begin_season) or meta.begin_season == season) \
                    and (not year or item.get('year') == year):
                logger.info(f"{name} 匹配到豆瓣信息：{item.get('id')} {item.get('title')}")
                return item
        return {}

    def movie_top250(self, page: int = 1, count: int = 30) -> List[MediaInfo]:
        """
        获取豆瓣电影TOP250
        """
        infos = self.doubanapi.movie_top250(start=(page - 1) * count,
                                            count=count)
        if infos and infos.get("subject_collection_items"):
            return [MediaInfo(douban_info=info) for info in infos.get("subject_collection_items")]
        return []

    def scrape_metadata(self, path: Path, mediainfo: MediaInfo, transfer_type: str,
                        metainfo: MetaBase = None, force_nfo: bool = False, force_img: bool = False) -> None:
        """
        刮削元数据
        :param path: 媒体文件路径
        :param mediainfo:  识别的媒体信息
        :param transfer_type: 传输类型
        :param metainfo: 源文件的识别元数据
        :param force_nfo: 是否强制刮削nfo
        :param force_img: 是否强制刮削图片
        :return: 成功或失败
        """

        def __get_mediainfo(_meta: MetaBase, _mediainfo: MediaInfo) -> Optional[MediaInfo]:
            """
            获取豆瓣媒体信息
            """
            if not _meta.name:
                return None
            # 查询豆瓣详情
            if not _mediainfo.douban_id:
                # 根据TMDB名称查询豆瓣数据
                _doubaninfo = self.match_doubaninfo(name=_mediainfo.title,
                                                    imdbid=_mediainfo.imdb_id,
                                                    mtype=_mediainfo.type,
                                                    year=_mediainfo.year)
                if not _doubaninfo:
                    logger.warn(f"未找到 {_mediainfo.title} 的豆瓣信息")
                    return None
                _doubaninfo = self.douban_info(doubanid=_doubaninfo.get("id"), mtype=_mediainfo.type)
            else:
                _doubaninfo = self.douban_info(doubanid=_mediainfo.douban_id,
                                               mtype=_mediainfo.type)
            if not _doubaninfo:
                logger(f"未获取到 {_mediainfo.douban_id} 的豆瓣媒体信息，无法刮削！")
                return None
            # 豆瓣媒体信息
            _doubanmedia = MediaInfo(douban_info=_doubaninfo)
            # 补充图片
            self.obtain_images(_doubanmedia)
            return _doubanmedia

        if settings.SCRAP_SOURCE != "douban":
            return None
        if SystemUtils.is_bluray_dir(path):
            # 蓝光原盘
            logger.info(f"开始刮削蓝光原盘：{path} ...")
            # 优先使用传入metainfo
            meta = metainfo or MetaInfo(path.name)
            # 刮削路径
            scrape_path = path / path.name
            # 媒体信息
            doubanmedia = __get_mediainfo(_meta=meta, _mediainfo=mediainfo)
            if not doubanmedia:
                return
            # 刮削
            self.scraper.gen_scraper_files(meta=meta,
                                           mediainfo=doubanmedia,
                                           file_path=scrape_path,
                                           transfer_type=transfer_type,
                                           force_nfo=force_nfo,
                                           force_img=force_img)
        elif path.is_file():
            # 刮削单个文件
            logger.info(f"开始刮削媒体库文件：{path} ...")
            # 优先使用传入metainfo
            meta = metainfo or MetaInfoPath(path)
            # 媒体信息
            doubanmedia = __get_mediainfo(_meta=meta, _mediainfo=mediainfo)
            if not doubanmedia:
                return
            # 刮削
            self.scraper.gen_scraper_files(meta=meta,
                                           mediainfo=doubanmedia,
                                           file_path=path,
                                           transfer_type=transfer_type,
                                           force_nfo=force_nfo,
                                           force_img=force_img)
        else:
            # 目录下的所有文件
            for file in SystemUtils.list_files(path, settings.RMT_MEDIAEXT):
                if not file:
                    continue
                logger.info(f"开始刮削媒体库文件：{file} ...")
                try:
                    meta = MetaInfoPath(file)
                    # 豆瓣媒体信息
                    doubanmedia = __get_mediainfo(_meta=meta, _mediainfo=mediainfo)
                    if not doubanmedia:
                        return
                    # 刮削
                    self.scraper.gen_scraper_files(meta=meta,
                                                   mediainfo=doubanmedia,
                                                   file_path=file,
                                                   transfer_type=transfer_type,
                                                   force_nfo=force_nfo,
                                                   force_img=force_img)
                except Exception as e:
                    logger.error(f"刮削文件 {file} 失败，原因：{str(e)}")
        logger.info(f"{path} 刮削完成")

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        if settings.RECOGNIZE_SOURCE != "douban":
            return None
        if not mediainfo.douban_id:
            return None
        if mediainfo.backdrop_path:
            # 没有图片缺失
            return mediainfo
        # 调用图片接口
        if not mediainfo.backdrop_path:
            if mediainfo.type == MediaType.MOVIE:
                info = self.doubanapi.movie_photos(mediainfo.douban_id)
            else:
                info = self.doubanapi.tv_photos(mediainfo.douban_id)
            if not info:
                return mediainfo
            images = info.get("photos")
            # 背景图
            if images:
                backdrop = images[0].get("image", {}).get("large") or {}
                if backdrop:
                    mediainfo.backdrop_path = backdrop.get("url")
        return mediainfo

    def clear_cache(self):
        """
        清除缓存
        """
        logger.info("开始清除豆瓣缓存 ...")
        self.doubanapi.clear_cache()
        self.cache.clear()
        logger.info("豆瓣缓存清除完成")

    def douban_movie_credits(self, doubanid: str) -> List[schemas.MediaPerson]:
        """
        根据TMDBID查询电影演职员表
        :param doubanid:  豆瓣ID
        """
        result = self.doubanapi.movie_celebrities(subject_id=doubanid)
        if not result:
            return []
        ret_list = result.get("actors") or []
        if ret_list:
            # 更新豆瓣演员信息中的ID，从URI中提取'douban://douban.com/celebrity/1316132?subject_id=27503705' subject_id
            for doubaninfo in ret_list:
                doubaninfo['id'] = doubaninfo.get('uri', '').split('?subject_id=')[-1]
            return [schemas.MediaPerson(source='douban', **doubaninfo) for doubaninfo in ret_list]
        return []

    def douban_tv_credits(self, doubanid: str) -> List[schemas.MediaPerson]:
        """
        根据TMDBID查询电视剧演职员表
        :param doubanid:  豆瓣ID
        """
        result = self.doubanapi.tv_celebrities(subject_id=doubanid)
        if not result:
            return []
        ret_list = result.get("actors") or []
        if ret_list:
            # 更新豆瓣演员信息中的ID，从URI中提取'douban://douban.com/celebrity/1316132?subject_id=27503705' subject_id
            for doubaninfo in ret_list:
                doubaninfo['id'] = doubaninfo.get('uri', '').split('?subject_id=')[-1]
            return [schemas.MediaPerson(source='douban', **doubaninfo) for doubaninfo in ret_list]
        return []

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
