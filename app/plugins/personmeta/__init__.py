import copy
import datetime
import json
import threading
import time
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional

import pytz
import zhconv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.modules.plex import Plex
from app.plugins import _PluginBase
from app.schemas import MediaInfo, MediaServerItem
from app.schemas.types import EventType, MediaType
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

    def init_plugin(self, config: dict = None):
        self.tmdbchain = TmdbChain(self.db)
        self.mschain = MediaServerChain(self.db)
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._delay = config.get("delay") or 0

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
            "delay": self._delay
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
                                    'md': 6
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
                                    'md': 6
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
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "",
            "delay": 30
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
        if not mediainfo:
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
        self.__update_item(server=existsinfo.server, item=iteminfo, mediainfo=mediainfo)

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

    def __update_item(self, server: str, item: MediaServerItem, mediainfo: MediaInfo = None):
        """
        更新媒体服务器中的条目
        """
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
        # 处理媒体项中的人物信息
        if iteminfo.get("People"):
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
            for people in iteminfo["People"]:
                if not people.get("Name"):
                    continue
                if StringUtils.is_chinese(people.get("Name")):
                    continue
                info = self.__update_people(server=server, people=people)
                if info:
                    peoples.append(info)
                else:
                    peoples.append(people)
            # 保存媒体项信息
            if peoples:
                iteminfo["People"] = peoples
                self.set_iteminfo(server=server, itemid=item.item_id, iteminfo=iteminfo)
        # 处理季和集人物
        if iteminfo.get("Type") and "Series" in iteminfo["Type"]:
            # 获取季媒体项
            seasons = self.get_items(server=server, parentid=item.item_id, mtype="Season")
            if not seasons:
                logger.warn(f"{item.title} 未找到季媒体项")
                return
            for season in seasons["Items"]:
                # 如果是Jellyfin，更新季的人物，Emby/Plex季没有人物
                if server == "jellyfin":
                    seasoninfo = self.get_iteminfo(server=server, itemid=season.get("Id"))
                    if not seasoninfo:
                        logger.warn(f"{item.title} 未找到季媒体项：{season.get('Id')}")
                        continue
                    # 更新季媒体项人物
                    peoples = []
                    if seasoninfo.get("People"):
                        for people in seasoninfo["People"]:
                            if not people.get("Name"):
                                continue
                            if StringUtils.is_chinese(people.get("Name")):
                                continue
                            # 更新人物信息
                            info = self.__update_people(server=server, people=people)
                            if info:
                                peoples.append(info)
                            else:
                                peoples.append(people)
                        # 保存季媒体项信息
                        if peoples:
                            seasoninfo["People"] = peoples
                            self.set_iteminfo(server=server, itemid=season.get("Id"), iteminfo=seasoninfo)
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
                    # 更新集媒体项人物
                    if episodeinfo.get("People"):
                        peoples = []
                        for people in episodeinfo["People"]:
                            if not people.get("Name"):
                                continue
                            if StringUtils.is_chinese(people.get("Name")):
                                continue
                            # 更新人物信息
                            info = self.__update_people(server=server, people=people)
                            if info:
                                peoples.append(info)
                            else:
                                peoples.append(people)
                        # 保存集媒体项信息
                        if peoples:
                            episodeinfo["People"] = peoples
                            self.set_iteminfo(server=server, itemid=episode.get("Id"), iteminfo=episodeinfo)

    def __update_people(self, server: str, people: dict) -> Optional[dict]:
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
                logger.debug(f"未找到人物 {people.get('Id')} 的信息")
                return None
            # 获取人物的TMDBID
            person_tmdbid, person_imdbid = __get_peopleid(personinfo)
            if not person_tmdbid:
                logger.warn(f"未找到人物 {people.get('Id')} 的tmdbid")
                return None
            # 是否更新标志
            updated = False
            # 查询人物TMDB详情
            person_tmdbinfo = self.tmdbchain.person_detail(int(person_tmdbid))
            if person_tmdbinfo:
                cn_name = self.__get_chinese_name(person_tmdbinfo)
                if cn_name:
                    updated = True
                    # 更新中文名
                    personinfo["Name"] = cn_name
                    ret_people["Name"] = cn_name
                    if "Name" not in personinfo["LockedFields"]:
                        personinfo["LockedFields"].append("Name")
                    # 更新中文描述
                    biography = person_tmdbinfo.get("biography")
                    if StringUtils.is_chinese(biography):
                        personinfo["Overview"] = biography
                        if "Overview" not in personinfo["LockedFields"]:
                            personinfo["LockedFields"].append("Overview")
                    # 更新人物图片
                    profile_path = f"https://image.tmdb.org/t/p/original{person_tmdbinfo.get('profile_path')}"
                    if profile_path:
                        logger.info(f"更新人物 {people.get('Id')} 的图片：{profile_path}")
                        self.set_item_image(server=server, itemid=people.get("Id"), imageurl=profile_path)
            # 更新人物信息
            if updated:
                logger.info(f"更新人物 {people.get('Id')} 的信息：{personinfo}")
                ret = self.set_iteminfo(server=server, itemid=people.get("Id"), iteminfo=personinfo)
                if ret:
                    return ret_people
        except Exception as err:
            logger.error(f"更新人物信息失败：{err}")
        return None

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
                logger.error(f"获取Emby媒体项详情失败：{err}")
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
                logger.error(f"获取Jellyfin媒体项详情失败：{err}")
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
                logger.error(f"获取Plex媒体项详情失败：{err}")
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
                logger.error(f"获取Emby媒体的所有子媒体项失败：{err}")
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
                logger.error(f"获取Jellyfin媒体的所有子媒体项失败：{err}")
            return {}

        def __get_plex_items(t: str) -> dict:
            """
            获得Plex媒体的所有子媒体项
            """
            items = {}
            try:
                plex = Plex().get_plex()
                items['Items'] = []
                if parentid:
                    if mtype and 'Season' in t:
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
                    elif mtype and 'Episode' in t:
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
                logger.error(f"获取Plex媒体的所有子媒体项失败：{err}")
            return {}

        if server == "emby":
            return __get_emby_items()
        elif server == "jellyfin":
            return __get_jellyfin_items()
        else:
            return __get_plex_items(mtype)

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
                    data=json.dumps(iteminfo)
                )
                if res and res.status_code in [200, 204]:
                    return True
                else:
                    logger.error(f"更新Emby媒体项详情失败，错误码：{res.status_code}")
                    return False
            except Exception as err:
                logger.error(f"更新Emby媒体项详情失败：{err}")
            return False

        def __set_jellyfin_iteminfo():
            """
            更新Jellyfin媒体项详情
            """
            try:
                res = Jellyfin().post_data(
                    url=f'[HOST]Items/{itemid}?api_key=[APIKEY]',
                    data=json.dumps(iteminfo)
                )
                if res and res.status_code in [200, 204]:
                    return True
                else:
                    logger.error(f"更新Jellyfin媒体项详情失败，错误码：{res.status_code}")
                    return False
            except Exception as err:
                logger.error(f"更新Jellyfin媒体项详情失败：{err}")
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
                logger.error(f"更新Plex媒体项详情失败：{err}")
            return False

        if server == "emby":
            return __set_emby_iteminfo()
        elif server == "jellyfin":
            return __set_jellyfin_iteminfo()
        else:
            return __set_plex_iteminfo()

    @staticmethod
    def set_item_image(server: str, itemid: str, imageurl: str):
        """
        更新媒体项图片
        """

        def __set_emby_item_image():
            """
            更新Emby媒体项图片
            """
            try:
                url = f'[HOST]emby/Items/{itemid}/Images/Primary/0/Url?api_key=[APIKEY]'
                data = json.dumps({'Url': imageurl})
                res = Emby().post_data(url=url, data=data)
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
            """
            try:
                plexitem = Plex().get_plex().library.fetchItem(ekey=itemid)
                plexitem.uploadPoster(url=imageurl)
                return True
            except Exception as err:
                logger.error(f"更新Plex媒体项图片失败：{err}")
            return False

        if server == "emby":
            return __set_emby_item_image()
        elif server == "jellyfin":
            return __set_jellyfin_item_image()
        else:
            return __set_plex_item_image()

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
