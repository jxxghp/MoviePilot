import base64
import copy
import datetime
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional

import pytz
import zhconv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from requests import RequestException

from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.meta import MetaBase
from app.log import logger
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.modules.plex import Plex
from app.plugins import _PluginBase
from app.schemas import MediaInfo, MediaServerItem
from app.schemas.types import EventType, MediaType
from app.utils.common import retry
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class PersonMeta(_PluginBase):
    # 插件名称
    plugin_name = "演职人员刮削"
    # 插件描述
    plugin_desc = "刮削演职人员图片以及中文名称。"
    # 插件图标
    plugin_icon = "actor.png"
    # 主题色
    plugin_color = "#E66E72"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "jxxghp"
    # 作者主页
    author_url = "https://github.com/jxxghp"
    # 插件配置项ID前缀
    plugin_config_prefix = "personmeta_"
    # 加载顺序
    plugin_order = 24
    # 可使用的用户级别
    auth_level = 1

    # 退出事件
    _event = threading.Event()

    # 私有属性
    _scheduler = None
    tmdbchain = None
    mschain = None
    _enabled = False
    _onlyonce = False
    _cron = None
    _delay = 0
    _type = "all"
    _remove_nozh = False

    def init_plugin(self, config: dict = None):
        self.tmdbchain = TmdbChain()
        self.mschain = MediaServerChain()
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._type = config.get("type") or "all"
            self._delay = config.get("delay") or 0
            self._remove_nozh = config.get("remove_nozh") or False

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._cron or self._onlyonce:
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.scrap_library,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="演职人员刮削")
                        logger.info(f"演职人员刮削服务启动，周期：{self._cron}")
                    except Exception as e:
                        logger.error(f"演职人员刮削服务启动失败，错误信息：{str(e)}")
                        self.systemmessage.put(f"演职人员刮削服务启动失败，错误信息：{str(e)}")
                if self._onlyonce:
                    self._scheduler.add_job(func=self.scrap_library, trigger='date',
                                            run_date=datetime.datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                            )
                    logger.info(f"演职人员刮削服务启动，立即运行一次")
                    # 关闭一次性开关
                    self._onlyonce = False
                    # 保存配置
                    self.__update_config()

            if self._scheduler.get_jobs():
                # 启动服务
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "type": self._type,
            "delay": self._delay,
            "remove_nozh": self._remove_nozh
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '媒体库扫描周期',
                                            'placeholder': '5位cron表达式'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'delay',
                                            'label': '入库延迟时间（秒）',
                                            'placeholder': '30'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'type',
                                            'label': '刮削条件',
                                            'items': [
                                                {'title': '全部', 'value': 'all'},
                                                {'title': '演员非中文', 'value': 'name'},
                                                {'title': '角色非中文', 'value': 'role'},
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'remove_nozh',
                                            'label': '删除非中文演员',
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "",
            "type": "all",
            "delay": 30,
            "remove_nozh": False
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.TransferComplete)
    def scrap_rt(self, event: Event):
        """
        根据事件实时刮削演员信息
        """
        if not self._enabled:
            return
        # 事件数据
        mediainfo: MediaInfo = event.event_data.get("mediainfo")
        meta: MetaBase = event.event_data.get("meta")
        if not mediainfo or not meta:
            return
        # 延迟
        if self._delay:
            time.sleep(int(self._delay))
        # 查询媒体服务器中的条目
        existsinfo = self.chain.media_exists(mediainfo=mediainfo)
        if not existsinfo or not existsinfo.itemid:
            logger.warn(f"演职人员刮削 {mediainfo.title_year} 在媒体库中不存在")
            return
        # 查询条目详情
        iteminfo = self.mschain.iteminfo(server=existsinfo.server, item_id=existsinfo.itemid)
        if not iteminfo:
            logger.warn(f"演职人员刮削 {mediainfo.title_year} 条目详情获取失败")
            return
        # 刮削演职人员信息
        self.__update_item(server=existsinfo.server, item=iteminfo,
                           mediainfo=mediainfo, season=meta.begin_season)

    def scrap_library(self):
        """
        扫描整个媒体库，刮削演员信息
        """
        # 所有媒体服务器
        if not settings.MEDIASERVER:
            return
        for server in settings.MEDIASERVER.split(","):
            # 扫描所有媒体库
            logger.info(f"开始刮削服务器 {server} 的演员信息 ...")
            for library in self.mschain.librarys(server):
                logger.info(f"开始刮削媒体库 {library.name} 的演员信息 ...")
                for item in self.mschain.items(server, library.id):
                    if not item:
                        continue
                    if not item.item_id:
                        continue
                    if "Series" not in item.item_type \
                            and "Movie" not in item.item_type:
                        continue
                    if self._event.is_set():
                        logger.info(f"演职人员刮削服务停止")
                        return
                    # 处理条目
                    logger.info(f"开始刮削 {item.title} 的演员信息 ...")
                    self.__update_item(server=server, item=item)
                    logger.info(f"{item.title} 的演员信息刮削完成")
                logger.info(f"媒体库 {library.name} 的演员信息刮削完成")
            logger.info(f"服务器 {server} 的演员信息刮削完成")

    def __update_peoples(self, server: str, itemid: str, iteminfo: dict, douban_actors):
        # 处理媒体项中的人物信息
        """
        "People": [
            {
              "Name": "丹尼尔·克雷格",
              "Id": "33625",
              "Role": "James Bond",
              "Type": "Actor",
              "PrimaryImageTag": "bef4f764540f10577f804201d8d27918"
            }
        ]
        """
        peoples = []
        # 更新当前媒体项人物
        for people in iteminfo["People"] or []:
            if self._event.is_set():
                logger.info(f"演职人员刮削服务停止")
                return
            if not people.get("Name"):
                continue
            if StringUtils.is_chinese(people.get("Name")) \
                    and StringUtils.is_chinese(people.get("Role")):
                peoples.append(people)
                continue
            info = self.__update_people(server=server, people=people,
                                        douban_actors=douban_actors)
            if info:
                peoples.append(info)
            elif not self._remove_nozh:
                peoples.append(people)
        # 保存媒体项信息
        if peoples:
            iteminfo["People"] = peoples
            self.set_iteminfo(server=server, itemid=itemid, iteminfo=iteminfo)

    def __update_item(self, server: str, item: MediaServerItem,
                      mediainfo: MediaInfo = None, season: int = None):
        """
        更新媒体服务器中的条目
        """

        def __need_trans_actor(_item):
            """
            是否需要处理人物信息
            """
            if self._type == "name":
                # 是否需要处理人物名称
                _peoples = [x for x in _item.get("People", []) if
                            (x.get("Name") and not StringUtils.is_chinese(x.get("Name")))]
            elif self._type == "role":
                # 是否需要处理人物角色
                _peoples = [x for x in _item.get("People", []) if
                            (x.get("Role") and not StringUtils.is_chinese(x.get("Role")))]
            else:
                _peoples = [x for x in _item.get("People", []) if
                            (x.get("Name") and not StringUtils.is_chinese(x.get("Name")))
                            or (x.get("Role") and not StringUtils.is_chinese(x.get("Role")))]
            if _peoples:
                return True
            return False

        # 识别媒体信息
        if not mediainfo:
            if not item.tmdbid:
                logger.warn(f"{item.title} 未找到tmdbid，无法识别媒体信息")
                return
            mtype = MediaType.TV if item.item_type in ['Series', 'show'] else MediaType.MOVIE
            mediainfo = self.chain.recognize_media(mtype=mtype, tmdbid=item.tmdbid)
            if not mediainfo:
                logger.warn(f"{item.title} 未识别到媒体信息")
                return

        # 获取媒体项
        iteminfo = self.get_iteminfo(server=server, itemid=item.item_id)
        if not iteminfo:
            logger.warn(f"{item.title} 未找到媒体项")
            return

        if __need_trans_actor(iteminfo):
            # 获取豆瓣演员信息
            logger.info(f"开始获取 {item.title} 的豆瓣演员信息 ...")
            douban_actors = self.__get_douban_actors(mediainfo=mediainfo, season=season)
            self.__update_peoples(server=server, itemid=item.item_id, iteminfo=iteminfo, douban_actors=douban_actors)
        else:
            logger.info(f"{item.title} 的人物信息已是中文，无需更新")

        # 处理季和集人物
        if iteminfo.get("Type") and "Series" in iteminfo["Type"]:
            # 获取季媒体项
            seasons = self.get_items(server=server, parentid=item.item_id, mtype="Season")
            if not seasons:
                logger.warn(f"{item.title} 未找到季媒体项")
                return
            for season in seasons["Items"]:
                # 获取豆瓣演员信息
                season_actors = self.__get_douban_actors(mediainfo=mediainfo, season=season.get("IndexNumber"))
                # 如果是Jellyfin，更新季的人物，Emby/Plex季没有人物
                if server == "jellyfin":
                    seasoninfo = self.get_iteminfo(server=server, itemid=season.get("Id"))
                    if not seasoninfo:
                        logger.warn(f"{item.title} 未找到季媒体项：{season.get('Id')}")
                        continue

                    if __need_trans_actor(seasoninfo):
                        # 更新季媒体项人物
                        self.__update_peoples(server=server, itemid=season.get("Id"), iteminfo=seasoninfo,
                                              douban_actors=season_actors)
                        logger.info(f"季 {seasoninfo.get('Id')} 的人物信息更新完成")
                    else:
                        logger.info(f"季 {seasoninfo.get('Id')} 的人物信息已是中文，无需更新")
                # 获取集媒体项
                episodes = self.get_items(server=server, parentid=season.get("Id"), mtype="Episode")
                if not episodes:
                    logger.warn(f"{item.title} 未找到集媒体项")
                    continue
                # 更新集媒体项人物
                for episode in episodes["Items"]:
                    # 获取集媒体项详情
                    episodeinfo = self.get_iteminfo(server=server, itemid=episode.get("Id"))
                    if not episodeinfo:
                        logger.warn(f"{item.title} 未找到集媒体项：{episode.get('Id')}")
                        continue
                    if __need_trans_actor(episodeinfo):
                        # 更新集媒体项人物
                        self.__update_peoples(server=server, itemid=episode.get("Id"), iteminfo=episodeinfo,
                                              douban_actors=season_actors)
                        logger.info(f"集 {episodeinfo.get('Id')} 的人物信息更新完成")
                    else:
                        logger.info(f"集 {episodeinfo.get('Id')} 的人物信息已是中文，无需更新")

    def __update_people(self, server: str, people: dict, douban_actors: list = None) -> Optional[dict]:
        """
        更新人物信息，返回替换后的人物信息
        """

        def __get_peopleid(p: dict) -> Tuple[Optional[str], Optional[str]]:
            """
            获取人物的TMDBID、IMDBID
            """
            if not p.get("ProviderIds"):
                return None, None
            peopletmdbid, peopleimdbid = None, None
            if "Tmdb" in p["ProviderIds"]:
                peopletmdbid = p["ProviderIds"]["Tmdb"]
            if "tmdb" in p["ProviderIds"]:
                peopletmdbid = p["ProviderIds"]["tmdb"]
            if "Imdb" in p["ProviderIds"]:
                peopleimdbid = p["ProviderIds"]["Imdb"]
            if "imdb" in p["ProviderIds"]:
                peopleimdbid = p["ProviderIds"]["imdb"]
            return peopletmdbid, peopleimdbid

        # 返回的人物信息
        ret_people = copy.deepcopy(people)

        try:
            # 查询媒体库人物详情
            personinfo = self.get_iteminfo(server=server, itemid=people.get("Id"))
            if not personinfo:
                logger.debug(f"未找到人物 {people.get('Name')} 的信息")
                return None

            # 是否更新标志
            updated_name = False
            updated_overview = False
            update_character = False
            profile_path = None

            # 从TMDB信息中更新人物信息
            person_tmdbid, person_imdbid = __get_peopleid(personinfo)
            if person_tmdbid:
                person_tmdbinfo = self.tmdbchain.person_detail(int(person_tmdbid))
                if person_tmdbinfo:
                    cn_name = self.__get_chinese_name(person_tmdbinfo)
                    if cn_name:
                        # 更新中文名
                        logger.debug(f"{people.get('Name')} 从TMDB获取到中文名：{cn_name}")
                        personinfo["Name"] = cn_name
                        ret_people["Name"] = cn_name
                        updated_name = True
                        # 更新中文描述
                        biography = person_tmdbinfo.get("biography")
                        if biography and StringUtils.is_chinese(biography):
                            logger.debug(f"{people.get('Name')} 从TMDB获取到中文描述")
                            personinfo["Overview"] = biography
                            updated_overview = True
                        # 图片
                        profile_path = person_tmdbinfo.get('profile_path')
                        if profile_path:
                            logger.debug(f"{people.get('Name')} 从TMDB获取到图片：{profile_path}")
                            profile_path = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{profile_path}"

            # 从豆瓣信息中更新人物信息
            """
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
            """
            if douban_actors and (not updated_name
                                  or not updated_overview
                                  or not update_character):
                # 从豆瓣演员中匹配中文名称、角色和简介
                for douban_actor in douban_actors:
                    if douban_actor.get("latin_name") == people.get("Name") \
                            or douban_actor.get("name") == people.get("Name"):
                        # 名称
                        if not updated_name:
                            logger.debug(f"{people.get('Name')} 从豆瓣中获取到中文名：{douban_actor.get('name')}")
                            personinfo["Name"] = douban_actor.get("name")
                            ret_people["Name"] = douban_actor.get("name")
                            updated_name = True
                        # 描述
                        if not updated_overview:
                            if douban_actor.get("title"):
                                logger.debug(f"{people.get('Name')} 从豆瓣中获取到中文描述：{douban_actor.get('title')}")
                                personinfo["Overview"] = douban_actor.get("title")
                                updated_overview = True
                        # 饰演角色
                        if not update_character:
                            if douban_actor.get("character"):
                                # "饰 詹姆斯·邦德 James Bond 007"
                                character = re.sub(r"饰\s+", "",
                                                   douban_actor.get("character"))
                                character = re.sub("演员", "",
                                                   character)
                                if character:
                                    logger.debug(f"{people.get('Name')} 从豆瓣中获取到饰演角色：{character}")
                                    ret_people["Role"] = character
                                    update_character = True
                        # 图片
                        if not profile_path:
                            avatar = douban_actor.get("avatar") or {}
                            if avatar.get("large"):
                                logger.debug(f"{people.get('Name')} 从豆瓣中获取到图片：{avatar.get('large')}")
                                profile_path = avatar.get("large")
                        break

            # 更新人物图片
            if profile_path:
                logger.debug(f"更新人物 {people.get('Name')} 的图片：{profile_path}")
                self.set_item_image(server=server, itemid=people.get("Id"), imageurl=profile_path)

            # 锁定人物信息
            if updated_name:
                if "Name" not in personinfo["LockedFields"]:
                    personinfo["LockedFields"].append("Name")
            if updated_overview:
                if "Overview" not in personinfo["LockedFields"]:
                    personinfo["LockedFields"].append("Overview")

            # 更新人物信息
            if updated_name or updated_overview or update_character:
                logger.debug(f"更新人物 {people.get('Name')} 的信息：{personinfo}")
                ret = self.set_iteminfo(server=server, itemid=people.get("Id"), iteminfo=personinfo)
                if ret:
                    return ret_people
            else:
                logger.debug(f"人物 {people.get('Name')} 未找到中文数据")
        except Exception as err:
            logger.error(f"更新人物信息失败：{str(err)}")
        return None

    def __get_douban_actors(self, mediainfo: MediaInfo, season: int = None) -> List[dict]:
        """
        获取豆瓣演员信息
        """
        # 随机休眠 3-10 秒
        sleep_time = 3 + int(time.time()) % 7
        logger.debug(f"随机休眠 {sleep_time}秒 ...")
        time.sleep(sleep_time)
        # 匹配豆瓣信息
        doubaninfo = self.chain.match_doubaninfo(name=mediainfo.title,
                                                 imdbid=mediainfo.imdb_id,
                                                 mtype=mediainfo.type.value,
                                                 year=mediainfo.year,
                                                 season=season)
        # 豆瓣演员
        if doubaninfo:
            doubanitem = self.chain.douban_info(doubaninfo.get("id")) or {}
            return (doubanitem.get("actors") or []) + (doubanitem.get("directors") or [])
        else:
            logger.debug(f"未找到豆瓣信息：{mediainfo.title_year}")
        return []

    @staticmethod
    def get_iteminfo(server: str, itemid: str) -> dict:
        """
        获得媒体项详情
        """

        def __get_emby_iteminfo() -> dict:
            """
            获得Emby媒体项详情
            """
            try:
                url = f'[HOST]emby/Users/[USER]/Items/{itemid}?' \
                      f'Fields=ChannelMappingInfo&api_key=[APIKEY]'
                res = Emby().get_data(url=url)
                if res:
                    return res.json()
            except Exception as err:
                logger.error(f"获取Emby媒体项详情失败：{str(err)}")
            return {}

        def __get_jellyfin_iteminfo() -> dict:
            """
            获得Jellyfin媒体项详情
            """
            try:
                url = f'[HOST]Users/[USER]/Items/{itemid}?Fields=ChannelMappingInfo&api_key=[APIKEY]'
                res = Jellyfin().get_data(url=url)
                if res:
                    result = res.json()
                    if result:
                        result['FileName'] = Path(result['Path']).name
                    return result
            except Exception as err:
                logger.error(f"获取Jellyfin媒体项详情失败：{str(err)}")
            return {}

        def __get_plex_iteminfo() -> dict:
            """
            获得Plex媒体项详情
            """
            iteminfo = {}
            try:
                plexitem = Plex().get_plex().library.fetchItem(ekey=itemid)
                if 'movie' in plexitem.METADATA_TYPE:
                    iteminfo['Type'] = 'Movie'
                    iteminfo['IsFolder'] = False
                elif 'episode' in plexitem.METADATA_TYPE:
                    iteminfo['Type'] = 'Series'
                    iteminfo['IsFolder'] = False
                    if 'show' in plexitem.TYPE:
                        iteminfo['ChildCount'] = plexitem.childCount
                iteminfo['Name'] = plexitem.title
                iteminfo['Id'] = plexitem.key
                iteminfo['ProductionYear'] = plexitem.year
                iteminfo['ProviderIds'] = {}
                for guid in plexitem.guids:
                    idlist = str(guid.id).split(sep='://')
                    if len(idlist) < 2:
                        continue
                    iteminfo['ProviderIds'][idlist[0]] = idlist[1]
                for location in plexitem.locations:
                    iteminfo['Path'] = location
                    iteminfo['FileName'] = Path(location).name
                iteminfo['Overview'] = plexitem.summary
                iteminfo['CommunityRating'] = plexitem.audienceRating
                return iteminfo
            except Exception as err:
                logger.error(f"获取Plex媒体项详情失败：{str(err)}")
            return {}

        if server == "emby":
            return __get_emby_iteminfo()
        elif server == "jellyfin":
            return __get_jellyfin_iteminfo()
        else:
            return __get_plex_iteminfo()

    @staticmethod
    def get_items(server: str, parentid: str, mtype: str = None) -> dict:
        """
        获得媒体的所有子媒体项
        """
        pass

        def __get_emby_items() -> dict:
            """
            获得Emby媒体的所有子媒体项
            """
            try:
                if parentid:
                    url = f'[HOST]emby/Users/[USER]/Items?ParentId={parentid}&api_key=[APIKEY]'
                else:
                    url = '[HOST]emby/Users/[USER]/Items?api_key=[APIKEY]'
                res = Emby().get_data(url=url)
                if res:
                    return res.json()
            except Exception as err:
                logger.error(f"获取Emby媒体的所有子媒体项失败：{str(err)}")
            return {}

        def __get_jellyfin_items() -> dict:
            """
            获得Jellyfin媒体的所有子媒体项
            """
            try:
                if parentid:
                    url = f'[HOST]Users/[USER]/Items?ParentId={parentid}&api_key=[APIKEY]'
                else:
                    url = '[HOST]Users/[USER]/Items?api_key=[APIKEY]'
                res = Jellyfin().get_data(url=url)
                if res:
                    return res.json()
            except Exception as err:
                logger.error(f"获取Jellyfin媒体的所有子媒体项失败：{str(err)}")
            return {}

        def __get_plex_items() -> dict:
            """
            获得Plex媒体的所有子媒体项
            """
            items = {}
            try:
                plex = Plex().get_plex()
                items['Items'] = []
                if parentid:
                    if mtype and 'Season' in mtype:
                        plexitem = plex.library.fetchItem(ekey=parentid)
                        items['Items'] = []
                        for season in plexitem.seasons():
                            item = {
                                'Name': season.title,
                                'Id': season.key,
                                'IndexNumber': season.seasonNumber,
                                'Overview': season.summary
                            }
                            items['Items'].append(item)
                    elif mtype and 'Episode' in mtype:
                        plexitem = plex.library.fetchItem(ekey=parentid)
                        items['Items'] = []
                        for episode in plexitem.episodes():
                            item = {
                                'Name': episode.title,
                                'Id': episode.key,
                                'IndexNumber': episode.episodeNumber,
                                'Overview': episode.summary,
                                'CommunityRating': episode.audienceRating
                            }
                            items['Items'].append(item)
                    else:
                        plexitems = plex.library.sectionByID(sectionID=parentid)
                        for plexitem in plexitems.all():
                            item = {}
                            if 'movie' in plexitem.METADATA_TYPE:
                                item['Type'] = 'Movie'
                                item['IsFolder'] = False
                            elif 'episode' in plexitem.METADATA_TYPE:
                                item['Type'] = 'Series'
                                item['IsFolder'] = False
                            item['Name'] = plexitem.title
                            item['Id'] = plexitem.key
                            items['Items'].append(item)
                else:
                    plexitems = plex.library.sections()
                    for plexitem in plexitems:
                        item = {}
                        if 'Directory' in plexitem.TAG:
                            item['Type'] = 'Folder'
                            item['IsFolder'] = True
                        elif 'movie' in plexitem.METADATA_TYPE:
                            item['Type'] = 'Movie'
                            item['IsFolder'] = False
                        elif 'episode' in plexitem.METADATA_TYPE:
                            item['Type'] = 'Series'
                            item['IsFolder'] = False
                        item['Name'] = plexitem.title
                        item['Id'] = plexitem.key
                        items['Items'].append(item)
                return items
            except Exception as err:
                logger.error(f"获取Plex媒体的所有子媒体项失败：{str(err)}")
            return {}

        if server == "emby":
            return __get_emby_items()
        elif server == "jellyfin":
            return __get_jellyfin_items()
        else:
            return __get_plex_items()

    @staticmethod
    def set_iteminfo(server: str, itemid: str, iteminfo: dict):
        """
        更新媒体项详情
        """

        def __set_emby_iteminfo():
            """
            更新Emby媒体项详情
            """
            try:
                res = Emby().post_data(
                    url=f'[HOST]emby/Items/{itemid}?api_key=[APIKEY]&reqformat=json',
                    data=json.dumps(iteminfo),
                    headers={
                        "Content-Type": "application/json"
                    }
                )
                if res and res.status_code in [200, 204]:
                    return True
                else:
                    logger.error(f"更新Emby媒体项详情失败，错误码：{res.status_code}")
                    return False
            except Exception as err:
                logger.error(f"更新Emby媒体项详情失败：{str(err)}")
            return False

        def __set_jellyfin_iteminfo():
            """
            更新Jellyfin媒体项详情
            """
            try:
                res = Jellyfin().post_data(
                    url=f'[HOST]Items/{itemid}?api_key=[APIKEY]',
                    data=json.dumps(iteminfo),
                    headers={
                        "Content-Type": "application/json"
                    }
                )
                if res and res.status_code in [200, 204]:
                    return True
                else:
                    logger.error(f"更新Jellyfin媒体项详情失败，错误码：{res.status_code}")
                    return False
            except Exception as err:
                logger.error(f"更新Jellyfin媒体项详情失败：{str(err)}")
            return False

        def __set_plex_iteminfo():
            """
            更新Plex媒体项详情
            """
            try:
                plexitem = Plex().get_plex().library.fetchItem(ekey=itemid)
                if 'CommunityRating' in iteminfo:
                    edits = {
                        'audienceRating.value': iteminfo['CommunityRating'],
                        'audienceRating.locked': 1
                    }
                    plexitem.edit(**edits)
                plexitem.editTitle(iteminfo['Name']).editSummary(iteminfo['Overview']).reload()
                return True
            except Exception as err:
                logger.error(f"更新Plex媒体项详情失败：{str(err)}")
            return False

        if server == "emby":
            return __set_emby_iteminfo()
        elif server == "jellyfin":
            return __set_jellyfin_iteminfo()
        else:
            return __set_plex_iteminfo()

    @staticmethod
    @retry(RequestException, logger=logger)
    def set_item_image(server: str, itemid: str, imageurl: str):
        """
        更新媒体项图片
        """

        def __download_image():
            """
            下载图片
            """
            try:
                if "doubanio.com" in imageurl:
                    r = RequestUtils(headers={
                        'Referer': "https://movie.douban.com/"
                    }, ua=settings.USER_AGENT).get_res(url=imageurl, raise_exception=True)
                else:
                    r = RequestUtils().get_res(url=imageurl, raise_exception=True)
                if r:
                    return base64.b64encode(r.content).decode()
                else:
                    logger.warn(f"{imageurl} 图片下载失败，请检查网络连通性")
            except Exception as err:
                logger.error(f"下载图片失败：{str(err)}")
            return None

        def __set_emby_item_image(_base64: str):
            """
            更新Emby媒体项图片
            """
            try:
                url = f'[HOST]emby/Items/{itemid}/Images/Primary?api_key=[APIKEY]'
                res = Emby().post_data(
                    url=url,
                    data=_base64,
                    headers={
                        "Content-Type": "image/png"
                    }
                )
                if res and res.status_code in [200, 204]:
                    return True
                else:
                    logger.error(f"更新Emby媒体项图片失败，错误码：{res.status_code}")
                    return False
            except Exception as result:
                logger.error(f"更新Emby媒体项图片失败：{result}")
            return False

        def __set_jellyfin_item_image():
            """
            更新Jellyfin媒体项图片
            # FIXME 改为预下载图片
            """
            try:
                url = f'[HOST]Items/{itemid}/RemoteImages/Download?' \
                      f'Type=Primary&ImageUrl={imageurl}&ProviderName=TheMovieDb&api_key=[APIKEY]'
                res = Jellyfin().post_data(url=url)
                if res and res.status_code in [200, 204]:
                    return True
                else:
                    logger.error(f"更新Jellyfin媒体项图片失败，错误码：{res.status_code}")
                    return False
            except Exception as err:
                logger.error(f"更新Jellyfin媒体项图片失败：{err}")
            return False

        def __set_plex_item_image():
            """
            更新Plex媒体项图片
            # FIXME 改为预下载图片
            """
            try:
                plexitem = Plex().get_plex().library.fetchItem(ekey=itemid)
                plexitem.uploadPoster(url=imageurl)
                return True
            except Exception as err:
                logger.error(f"更新Plex媒体项图片失败：{err}")
            return False

        if server == "emby":
            # 下载图片获取base64
            image_base64 = __download_image()
            if image_base64:
                return __set_emby_item_image(image_base64)
        elif server == "jellyfin":
            return __set_jellyfin_item_image()
        else:
            return __set_plex_item_image()
        return None

    @staticmethod
    def __get_chinese_name(personinfo: dict) -> str:
        """
        获取TMDB别名中的中文名
        """
        try:
            also_known_as = personinfo.get("also_known_as") or []
            if also_known_as:
                for name in also_known_as:
                    if name and StringUtils.is_chinese(name):
                        # 使用cn2an将繁体转化为简体
                        return zhconv.convert(name, "zh-hans")
        except Exception as err:
            logger.error(f"获取人物中文名失败：{err}")
        return ""

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))
