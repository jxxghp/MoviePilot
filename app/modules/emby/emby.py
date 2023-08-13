import json
import re
from pathlib import Path
from typing import List, Optional, Union, Dict, Generator

from requests import Response

from app.core.config import settings
from app.log import logger
from app.schemas import RefreshMediaItem, WebhookEventInfo
from app.schemas.types import MediaType
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.string import StringUtils


class Emby(metaclass=Singleton):

    def __init__(self):
        self._host = settings.EMBY_HOST
        if self._host:
            if not self._host.endswith("/"):
                self._host += "/"
            if not self._host.startswith("http"):
                self._host = "http://" + self._host
        self._apikey = settings.EMBY_API_KEY
        self._user = self.get_user()
        self._folders = self.get_emby_folders()

    def get_emby_folders(self) -> List[dict]:
        """
        获取Emby媒体库路径列表
        """
        if not self._host or not self._apikey:
            return []
        req_url = "%semby/Library/SelectableMediaFolders?api_key=%s" % (self._host, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                return res.json()
            else:
                logger.error(f"Library/SelectableMediaFolders 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接Library/SelectableMediaFolders 出错：" + str(e))
            return []

    def __get_emby_librarys(self) -> List[dict]:
        """
        获取Emby媒体库列表
        """
        if not self._host or not self._apikey:
            return []
        req_url = f"{self._host}emby/Users/{self._user}/Views?api_key={self._apikey}"
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                return res.json().get("Items")
            else:
                logger.error(f"User/Views 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接User/Views 出错：" + str(e))
            return []

    def get_librarys(self):
        """
        获取媒体服务器所有媒体库列表
        """
        if not self._host or not self._apikey:
            return []
        libraries = []
        for library in self.__get_emby_librarys() or []:
            match library.get("CollectionType"):
                case "movies":
                    library_type = MediaType.MOVIE.value
                case "tvshows":
                    library_type = MediaType.TV.value
                case _:
                    continue
            libraries.append({
                "id": library.get("Id"),
                "name": library.get("Name"),
                "path": library.get("Path"),
                "type": library_type
            })
        return libraries

    def get_user(self, user_name: str = None) -> Optional[Union[str, int]]:
        """
        获得管理员用户
        """
        if not self._host or not self._apikey:
            return None
        req_url = "%sUsers?api_key=%s" % (self._host, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                users = res.json()
                # 先查询是否有与当前用户名称匹配的
                if user_name:
                    for user in users:
                        if user.get("Name") == user_name:
                            return user.get("Id")
                # 查询管理员
                for user in users:
                    if user.get("Policy", {}).get("IsAdministrator"):
                        return user.get("Id")
            else:
                logger.error(f"Users 未获取到返回数据")
        except Exception as e:
            logger.error(f"连接Users出错：" + str(e))
        return None

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """
        用户认证
        :param username: 用户名
        :param password: 密码
        :return: 认证token
        """
        if not self._host or not self._apikey:
            return None
        req_url = "%semby/Users/AuthenticateByName" % self._host
        try:
            res = RequestUtils(headers={
                'X-Emby-Authorization': f'MediaBrowser Client="MoviePilot", '
                                        f'Device="requests", '
                                        f'DeviceId="1", '
                                        f'Version="1.0.0", '
                                        f'Token="{self._apikey}"',
                'Content-Type': 'application/json',
                "Accept": "application/json"
            }).post_res(
                url=req_url,
                data=json.dumps({
                    "Username": username,
                    "Pw": password
                })
            )
            if res:
                auth_token = res.json().get("AccessToken")
                if auth_token:
                    logger.info(f"用户 {username} Emby认证成功")
                    return auth_token
            else:
                logger.error(f"Users/AuthenticateByName 未获取到返回数据")
        except Exception as e:
            logger.error(f"连接Users/AuthenticateByName出错：" + str(e))
        return None

    def get_server_id(self) -> Optional[str]:
        """
        获得服务器信息
        """
        if not self._host or not self._apikey:
            return None
        req_url = "%sSystem/Info?api_key=%s" % (self._host, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                return res.json().get("Id")
            else:
                logger.error(f"System/Info 未获取到返回数据")
        except Exception as e:

            logger.error(f"连接System/Info出错：" + str(e))
        return None

    def get_user_count(self) -> int:
        """
        获得用户数量
        """
        if not self._host or not self._apikey:
            return 0
        req_url = "%semby/Users/Query?api_key=%s" % (self._host, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                return res.json().get("TotalRecordCount")
            else:
                logger.error(f"Users/Query 未获取到返回数据")
                return 0
        except Exception as e:
            logger.error(f"连接Users/Query出错：" + str(e))
            return 0

    def get_activity_log(self, num: int = 30) -> List[dict]:
        """
        获取Emby活动记录
        """
        if not self._host or not self._apikey:
            return []
        req_url = "%semby/System/ActivityLog/Entries?api_key=%s&" % (self._host, self._apikey)
        ret_array = []
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                ret_json = res.json()
                items = ret_json.get('Items')
                for item in items:
                    if item.get("Type") == "AuthenticationSucceeded":
                        event_type = "LG"
                        event_date = StringUtils.get_time(item.get("Date"))
                        event_str = "%s, %s" % (item.get("Name"), item.get("ShortOverview"))
                        activity = {"type": event_type, "event": event_str, "date": event_date}
                        ret_array.append(activity)
                    if item.get("Type") in ["VideoPlayback", "VideoPlaybackStopped"]:
                        event_type = "PL"
                        event_date = StringUtils.get_time(item.get("Date"))
                        event_str = item.get("Name")
                        activity = {"type": event_type, "event": event_str, "date": event_date}
                        ret_array.append(activity)
            else:
                logger.error(f"System/ActivityLog/Entries 未获取到返回数据")
                return []
        except Exception as e:

            logger.error(f"连接System/ActivityLog/Entries出错：" + str(e))
            return []
        return ret_array[:num]

    def get_medias_count(self) -> dict:
        """
        获得电影、电视剧、动漫媒体数量
        :return: MovieCount SeriesCount SongCount
        """
        if not self._host or not self._apikey:
            return {}
        req_url = "%semby/Items/Counts?api_key=%s" % (self._host, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                return res.json()
            else:
                logger.error(f"Items/Counts 未获取到返回数据")
                return {}
        except Exception as e:
            logger.error(f"连接Items/Counts出错：" + str(e))
            return {}

    def __get_emby_series_id_by_name(self, name: str, year: str) -> Optional[str]:
        """
        根据名称查询Emby中剧集的SeriesId
        :param name: 标题
        :param year: 年份
        :return: None 表示连不通，""表示未找到，找到返回ID
        """
        if not self._host or not self._apikey:
            return None
        req_url = "%semby/Items?IncludeItemTypes=Series&Fields=ProductionYear&StartIndex=0&Recursive=true&SearchTerm=%s&Limit=10&IncludeSearchTypes=false&api_key=%s" % (
            self._host, name, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                res_items = res.json().get("Items")
                if res_items:
                    for res_item in res_items:
                        if res_item.get('Name') == name and (
                                not year or str(res_item.get('ProductionYear')) == str(year)):
                            return res_item.get('Id')
        except Exception as e:
            logger.error(f"连接Items出错：" + str(e))
            return None
        return ""

    def get_movies(self, title: str, year: str = None) -> Optional[List[dict]]:
        """
        根据标题和年份，检查电影是否在Emby中存在，存在则返回列表
        :param title: 标题
        :param year: 年份，可以为空，为空时不按年份过滤
        :return: 含title、year属性的字典列表
        """
        if not self._host or not self._apikey:
            return None
        req_url = "%semby/Items?IncludeItemTypes=Movie&Fields=ProductionYear&StartIndex=0" \
                  "&Recursive=true&SearchTerm=%s&Limit=10&IncludeSearchTypes=false&api_key=%s" % (
                      self._host, title, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                res_items = res.json().get("Items")
                if res_items:
                    ret_movies = []
                    for res_item in res_items:
                        if res_item.get('Name') == title and (
                                not year or str(res_item.get('ProductionYear')) == str(year)):
                            ret_movies.append(
                                {'title': res_item.get('Name'), 'year': str(res_item.get('ProductionYear'))})
                            return ret_movies
        except Exception as e:
            logger.error(f"连接Items出错：" + str(e))
            return None
        return []

    def get_tv_episodes(self,
                        item_ids: List[str] = [],
                        title: str = None,
                        year: str = None,
                        tmdb_id: int = None,
                        season: int = None) -> Optional[Dict[int, list]]:
        """
        根据标题和年份和季，返回Emby中的剧集列表
        :param item_ids: Emby中的ID列表
        :param title: 标题
        :param year: 年份
        :param tmdb_id: TMDBID
        :param season: 季
        :return: 每一季的已有集数
        """
        if not self._host or not self._apikey:
            return None
        item_id_by_name = ''
        season_episodes = {}
        if not season:
            season = ""
        # 电视剧
        if not item_ids:
            item_id_by_name = self.__get_emby_series_id_by_name(title, year)
            if item_id_by_name is None:
                return None
            if not item_id_by_name:
                return {}
        if item_id_by_name:
            item_ids.append(item_id_by_name)
        for item_id in item_ids:
            # 验证tmdbid是否相同
            item_tmdbid = self.get_iteminfo(item_id).get("ProviderIds", {}).get("Tmdb")
            if tmdb_id and item_tmdbid:
                if str(tmdb_id) != str(item_tmdbid):
                    continue
            # /Shows/Id/Episodes 查集的信息
            try:
                req_url = "%semby/Shows/%s/Episodes?Season=%s&IsMissing=false&api_key=%s" % (
                    self._host, item_id, season, self._apikey)
                res_json = RequestUtils().get_res(req_url)
                if res_json:
                    res_items = res_json.json().get("Items")

                    for res_item in res_items:
                        season_index = res_item.get("ParentIndexNumber")
                        if not season_index:
                            continue
                        if season and season != season_index:
                            continue
                        episode_index = res_item.get("IndexNumber")
                        if not episode_index:
                            continue
                        if season_index not in season_episodes:
                            season_episodes[season_index] = []
                        season_episodes[season_index].append(episode_index)
            except Exception as e:
                logger.error(f"连接Shows/Id/Episodes出错：" + str(e))
                return None
        return season_episodes

    def get_remote_image_by_id(self, item_id: str, image_type: str) -> Optional[str]:
        """
        根据ItemId从Emby查询TMDB的图片地址
        :param item_id: 在Emby中的ID
        :param image_type: 图片的类弄地，poster或者backdrop等
        :return: 图片对应在TMDB中的URL
        """
        if not self._host or not self._apikey:
            return None
        req_url = "%semby/Items/%s/RemoteImages?api_key=%s" % (self._host, item_id, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                images = res.json().get("Images")
                for image in images:
                    if image.get("ProviderName") == "TheMovieDb" and image.get("Type") == image_type:
                        return image.get("Url")
            else:
                logger.error(f"Items/RemoteImages 未获取到返回数据")
                return None
        except Exception as e:
            logger.error(f"连接Items/Id/RemoteImages出错：" + str(e))
            return None
        return None

    def __refresh_emby_library_by_id(self, item_id: str) -> bool:
        """
        通知Emby刷新一个项目的媒体库
        """
        if not self._host or not self._apikey:
            return False
        req_url = "%semby/Items/%s/Refresh?Recursive=true&api_key=%s" % (self._host, item_id, self._apikey)
        try:
            res = RequestUtils().post_res(req_url)
            if res:
                return True
            else:
                logger.info(f"刷新媒体库对象 {item_id} 失败，无法连接Emby！")
        except Exception as e:
            logger.error(f"连接Items/Id/Refresh出错：" + str(e))
            return False
        return False

    def refresh_root_library(self) -> bool:
        """
        通知Emby刷新整个媒体库
        """
        if not self._host or not self._apikey:
            return False
        req_url = "%semby/Library/Refresh?api_key=%s" % (self._host, self._apikey)
        try:
            res = RequestUtils().post_res(req_url)
            if res:
                return True
            else:
                logger.info(f"刷新媒体库失败，无法连接Emby！")
        except Exception as e:
            logger.error(f"连接Library/Refresh出错：" + str(e))
            return False
        return False

    def refresh_library_by_items(self, items: List[RefreshMediaItem]) -> bool:
        """
        按类型、名称、年份来刷新媒体库
        :param items: 已识别的需要刷新媒体库的媒体信息列表
        """
        if not items:
            return False
        # 收集要刷新的媒体库信息
        logger.info(f"开始刷新Emby媒体库...")
        library_ids = []
        for item in items:
            library_id = self.__get_emby_library_id_by_item(item)
            if library_id and library_id not in library_ids:
                library_ids.append(library_id)
        # 开始刷新媒体库
        if "/" in library_ids:
            return self.refresh_root_library()
        for library_id in library_ids:
            if library_id != "/":
                return self.__refresh_emby_library_by_id(library_id)
        logger.info(f"Emby媒体库刷新完成")

    def __get_emby_library_id_by_item(self, item: RefreshMediaItem) -> Optional[str]:
        """
        根据媒体信息查询在哪个媒体库，返回要刷新的位置的ID
        :param item: {title, year, type, category, target_path}
        """
        if not item.title or not item.year or not item.type:
            return None
        if item.type != MediaType.MOVIE.value:
            item_id = self.__get_emby_series_id_by_name(item.title, item.year)
            if item_id:
                # 存在电视剧，则直接刷新这个电视剧就行
                return item_id
        else:
            if self.get_movies(item.title, item.year):
                # 已存在，不用刷新
                return None
        # 查找需要刷新的媒体库ID
        item_path = Path(item.target_path)
        for folder in self._folders:
            # 找同级路径最多的媒体库（要求容器内映射路径与实际一致）
            max_comm_path = ""
            match_num = 0
            match_id = None
            # 匹配子目录
            for subfolder in folder.get("SubFolders"):
                try:
                    # 查询最大公共路径
                    subfolder_path = Path(subfolder.get("Path"))
                    item_path_parents = list(item_path.parents)
                    subfolder_path_parents = list(subfolder_path.parents)
                    common_path = next(p1 for p1, p2 in zip(reversed(item_path_parents),
                                                            reversed(subfolder_path_parents)
                                                            ) if p1 == p2)
                    if len(common_path) > len(max_comm_path):
                        max_comm_path = common_path
                        match_id = subfolder.get("Id")
                        match_num += 1
                except StopIteration:
                    continue
                except Exception as err:
                    print(str(err))
            # 检查匹配情况
            if match_id:
                return match_id if match_num == 1 else folder.get("Id")
            # 如果找不到，只要路径中有分类目录名就命中
            for subfolder in folder.get("SubFolders"):
                if subfolder.get("Path") and re.search(r"[/\\]%s" % item.category,
                                                       subfolder.get("Path")):
                    return folder.get("Id")
        # 刷新根目录
        return "/"

    def get_iteminfo(self, itemid: str) -> dict:
        """
        获取单个项目详情
        """
        if not itemid:
            return {}
        if not self._host or not self._apikey:
            return {}
        req_url = "%semby/Users/%s/Items/%s?api_key=%s" % (self._host, self._user, itemid, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res and res.status_code == 200:
                return res.json()
        except Exception as e:
            logger.error(f"连接Items/Id出错：" + str(e))
            return {}

    def get_items(self, parent: str) -> Generator:
        """
        获取媒体服务器所有媒体库列表
        """
        if not parent:
            yield {}
        if not self._host or not self._apikey:
            yield {}
        req_url = "%semby/Users/%s/Items?ParentId=%s&api_key=%s" % (self._host, self._user, parent, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res and res.status_code == 200:
                results = res.json().get("Items") or []
                for result in results:
                    if not result:
                        continue
                    if result.get("Type") in ["Movie", "Series"]:
                        item_info = self.get_iteminfo(result.get("Id"))
                        yield {"id": result.get("Id"),
                               "library": item_info.get("ParentId"),
                               "type": item_info.get("Type"),
                               "title": item_info.get("Name"),
                               "original_title": item_info.get("OriginalTitle"),
                               "year": item_info.get("ProductionYear"),
                               "tmdbid": item_info.get("ProviderIds", {}).get("Tmdb"),
                               "imdbid": item_info.get("ProviderIds", {}).get("Imdb"),
                               "tvdbid": item_info.get("ProviderIds", {}).get("Tvdb"),
                               "path": item_info.get("Path"),
                               "json": str(item_info)}
                    elif "Folder" in result.get("Type"):
                        for item in self.get_items(parent=result.get('Id')):
                            yield item
        except Exception as e:
            logger.error(f"连接Users/Items出错：" + str(e))
        yield {}

    def get_webhook_message(self, message_str: str) -> WebhookEventInfo:
        """
        解析Emby Webhook报文
        """
        message = json.loads(message_str)
        eventItem = WebhookEventInfo(event=message.get('Event', ''), channel="emby")
        if message.get('Item'):
            if message.get('Item', {}).get('Type') == 'Episode':
                eventItem.item_type = "TV"
                if message.get('Item', {}).get('SeriesName') \
                        and message.get('Item', {}).get('ParentIndexNumber') \
                        and message.get('Item', {}).get('IndexNumber'):
                    eventItem.item_name = "%s %s%s %s" % (
                        message.get('Item', {}).get('SeriesName'),
                        "S" + str(message.get('Item', {}).get('ParentIndexNumber')),
                        "E" + str(message.get('Item', {}).get('IndexNumber')),
                        message.get('Item', {}).get('Name'))
                else:
                    eventItem.item_name = message.get('Item', {}).get('Name')
                eventItem.item_id = message.get('Item', {}).get('SeriesId')
                eventItem.season_id = message.get('Item', {}).get('ParentIndexNumber')
                eventItem.episode_id = message.get('Item', {}).get('IndexNumber')
            elif message.get('Item', {}).get('Type') == 'Audio':
                eventItem.item_type = "AUD"
                album = message.get('Item', {}).get('Album')
                file_name = message.get('Item', {}).get('FileName')
                eventItem.item_name = album
                eventItem.overview = file_name
                eventItem.item_id = message.get('Item', {}).get('AlbumId')
            else:
                eventItem.item_type = "MOV"
                eventItem.item_name = "%s %s" % (
                    message.get('Item', {}).get('Name'), "(" + str(message.get('Item', {}).get('ProductionYear')) + ")")
                eventItem.item_path = message.get('Item', {}).get('Path')
                eventItem.item_id = message.get('Item', {}).get('Id')

            eventItem.tmdb_id = message.get('Item', {}).get('ProviderIds', {}).get('Tmdb')
            if message.get('Item', {}).get('Overview') and len(message.get('Item', {}).get('Overview')) > 100:
                eventItem.overview = str(message.get('Item', {}).get('Overview'))[:100] + "..."
            else:
                eventItem.overview = message.get('Item', {}).get('Overview')
            eventItem.percentage = message.get('TranscodingInfo', {}).get('CompletionPercentage')
            if not eventItem.percentage:
                if message.get('PlaybackInfo', {}).get('PositionTicks'):
                    eventItem.percentage = message.get('PlaybackInfo', {}).get('PositionTicks') / \
                                           message.get('Item', {}).get('RunTimeTicks') * 100
        if message.get('Session'):
            eventItem.ip = message.get('Session').get('RemoteEndPoint')
            eventItem.device_name = message.get('Session').get('DeviceName')
            eventItem.client = message.get('Session').get('Client')
        if message.get("User"):
            eventItem.user_name = message.get("User").get('Name')
        if message.get("item_isvirtual"):
            eventItem.item_isvirtual = message.get("item_isvirtual")
            eventItem.item_type = message.get("item_type")
            eventItem.item_name = message.get("item_name")
            eventItem.item_path = message.get("item_path")
            eventItem.tmdb_id = message.get("tmdb_id")
            eventItem.season_id = message.get("season_id")
            eventItem.episode_id = message.get("episode_id")

        # 获取消息图片
        if eventItem.item_id:
            # 根据返回的item_id去调用媒体服务器获取
            eventItem.image_url = self.get_remote_image_by_id(item_id=eventItem.item_id,
                                                              image_type="Backdrop")

        return eventItem

    def get_data(self, url: str) -> Optional[Response]:
        """
        自定义URL从媒体服务器获取数据，其中{HOST}、{APIKEY}、{USER}会被替换成实际的值
        :param url: 请求地址
        """
        if not self._host or not self._apikey:
            return None
        url = url.replace("{HOST}", self._host)\
            .replace("{APIKEY}", self._apikey)\
            .replace("{USER}", self._user)
        try:
            return RequestUtils().get_res(url=url)
        except Exception as e:
            logger.error(f"连接Emby出错：" + str(e))
            return None
