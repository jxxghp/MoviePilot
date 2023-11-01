import json
from typing import List, Union, Optional, Dict, Generator, Tuple

from requests import Response

from app import schemas
from app.core.config import settings
from app.log import logger
from app.schemas import MediaType
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton


class Jellyfin(metaclass=Singleton):

    def __init__(self):
        self._host = settings.JELLYFIN_HOST
        if self._host:
            if not self._host.endswith("/"):
                self._host += "/"
            if not self._host.startswith("http"):
                self._host = "http://" + self._host
        self._apikey = settings.JELLYFIN_API_KEY
        self.user = self.get_user(settings.SUPERUSER)
        self.serverid = self.get_server_id()

    def is_inactive(self) -> bool:
        """
        判断是否需要重连
        """
        if not self._host or not self._apikey:
            return False
        return True if not self.user else False

    def reconnect(self):
        """
        重连
        """
        self.user = self.get_user()
        self.serverid = self.get_server_id()

    def __get_jellyfin_librarys(self) -> List[dict]:
        """
        获取Jellyfin媒体库的信息
        """
        if not self._host or not self._apikey:
            return []
        req_url = f"{self._host}Users/{self.user}/Views?api_key={self._apikey}"
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                return res.json().get("Items")
            else:
                logger.error(f"Users/Views 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接Users/Views 出错：" + str(e))
            return []

    def get_librarys(self):
        """
        获取媒体服务器所有媒体库列表
        """
        if not self._host or not self._apikey:
            return []
        libraries = []
        for library in self.__get_jellyfin_librarys() or []:
            match library.get("CollectionType"):
                case "movies":
                    library_type = MediaType.MOVIE.value
                case "tvshows":
                    library_type = MediaType.TV.value
                case _:
                    continue
            libraries.append(
                schemas.MediaServerLibrary(
                    server="jellyfin",
                    id=library.get("Id"),
                    name=library.get("Name"),
                    path=library.get("Path"),
                    type=library_type
                ))
        return libraries

    def get_user_count(self) -> int:
        """
        获得用户数量
        """
        if not self._host or not self._apikey:
            return 0
        req_url = "%sUsers?api_key=%s" % (self._host, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                return len(res.json())
            else:
                logger.error(f"Users 未获取到返回数据")
                return 0
        except Exception as e:
            logger.error(f"连接Users出错：" + str(e))
            return 0

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
        :return: 认证成功返回token，否则返回None
        """
        if not self._host or not self._apikey:
            return None
        req_url = "%sUsers/authenticatebyname" % self._host
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
                    logger.info(f"用户 {username} Jellyfin认证成功")
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

    def get_medias_count(self) -> schemas.Statistic:
        """
        获得电影、电视剧、动漫媒体数量
        :return: MovieCount SeriesCount SongCount
        """
        if not self._host or not self._apikey:
            return schemas.Statistic()
        req_url = "%sItems/Counts?api_key=%s" % (self._host, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                result = res.json()
                return schemas.Statistic(
                    movie_count=result.get("MovieCount") or 0,
                    tv_count=result.get("SeriesCount") or 0,
                    episode_count=result.get("EpisodeCount") or 0
                )
            else:
                logger.error(f"Items/Counts 未获取到返回数据")
                return schemas.Statistic()
        except Exception as e:
            logger.error(f"连接Items/Counts出错：" + str(e))
        return schemas.Statistic()

    def __get_jellyfin_series_id_by_name(self, name: str, year: str) -> Optional[str]:
        """
        根据名称查询Jellyfin中剧集的SeriesId
        """
        if not self._host or not self._apikey or not self.user:
            return None
        req_url = ("%sUsers/%s/Items?"
                   "api_key=%s&searchTerm=%s&IncludeItemTypes=Series&Limit=10&Recursive=true") % (
                      self._host, self.user, self._apikey, name)
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

    def get_movies(self,
                   title: str,
                   year: str = None,
                   tmdb_id: int = None) -> Optional[List[schemas.MediaServerItem]]:
        """
        根据标题和年份，检查电影是否在Jellyfin中存在，存在则返回列表
        :param title: 标题
        :param year: 年份，为空则不过滤
        :param tmdb_id: TMDB ID
        :return: 含title、year属性的字典列表
        """
        if not self._host or not self._apikey or not self.user:
            return None
        req_url = ("%sUsers/%s/Items?"
                   "api_key=%s&searchTerm=%s&IncludeItemTypes=Movie&Limit=10&Recursive=true") % (
                      self._host, self.user, self._apikey, title)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                res_items = res.json().get("Items")
                if res_items:
                    ret_movies = []
                    for item in res_items:
                        item_tmdbid = item.get("ProviderIds", {}).get("Tmdb")
                        mediaserver_item = schemas.MediaServerItem(
                            server="jellyfin",
                            library=item.get("ParentId"),
                            item_id=item.get("Id"),
                            item_type=item.get("Type"),
                            title=item.get("Name"),
                            original_title=item.get("OriginalTitle"),
                            year=item.get("ProductionYear"),
                            tmdbid=int(item_tmdbid) if item_tmdbid else None,
                            imdbid=item.get("ProviderIds", {}).get("Imdb"),
                            tvdbid=item.get("ProviderIds", {}).get("Tvdb"),
                            path=item.get("Path")
                        )
                        if tmdb_id and item_tmdbid:
                            if str(item_tmdbid) != str(tmdb_id):
                                continue
                            else:
                                ret_movies.append(mediaserver_item)
                                continue
                        if mediaserver_item.title == title and (
                                not year or str(mediaserver_item.year) == str(year)):
                            ret_movies.append(mediaserver_item)
                    return ret_movies
        except Exception as e:
            logger.error(f"连接Items出错：" + str(e))
            return None
        return []

    def get_tv_episodes(self,
                        item_id: str = None,
                        title: str = None,
                        year: str = None,
                        tmdb_id: int = None,
                        season: int = None) -> Tuple[Optional[str], Optional[Dict[int, list]]]:
        """
        根据标题和年份和季，返回Jellyfin中的剧集列表
        :param item_id: Jellyfin中的Id
        :param title: 标题
        :param year: 年份
        :param tmdb_id: TMDBID
        :param season: 季
        :return: 集号的列表
        """
        if not self._host or not self._apikey or not self.user:
            return None, None
        # 查TVID
        if not item_id:
            item_id = self.__get_jellyfin_series_id_by_name(title, year)
            if item_id is None:
                return None, None
            if not item_id:
                return None, {}
        # 验证tmdbid是否相同
        item_info = self.get_iteminfo(item_id)
        if item_info:
            if tmdb_id and item_info.tmdbid:
                if str(tmdb_id) != str(item_info.tmdbid):
                    return None, {}
        if not season:
            season = ""
        try:
            req_url = "%sShows/%s/Episodes?season=%s&&userId=%s&isMissing=false&api_key=%s" % (
                self._host, item_id, season, self.user, self._apikey)
            res_json = RequestUtils().get_res(req_url)
            if res_json:
                tv_info = res_json.json()
                res_items = tv_info.get("Items")
                # 返回的季集信息
                season_episodes = {}
                for res_item in res_items:
                    season_index = res_item.get("ParentIndexNumber")
                    if not season_index:
                        continue
                    if season and season != season_index:
                        continue
                    episode_index = res_item.get("IndexNumber")
                    if not episode_index:
                        continue
                    if not season_episodes.get(season_index):
                        season_episodes[season_index] = []
                    season_episodes[season_index].append(episode_index)
                return tv_info.get('Id'), season_episodes
        except Exception as e:
            logger.error(f"连接Shows/Id/Episodes出错：" + str(e))
            return None, None
        return None, {}

    def get_remote_image_by_id(self, item_id: str, image_type: str) -> Optional[str]:
        """
        根据ItemId从Jellyfin查询TMDB图片地址
        :param item_id: 在Emby中的ID
        :param image_type: 图片的类弄地，poster或者backdrop等
        :return: 图片对应在TMDB中的URL
        """
        if not self._host or not self._apikey:
            return None
        req_url = "%sItems/%s/RemoteImages?api_key=%s" % (self._host, item_id, self._apikey)
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

    def refresh_root_library(self) -> bool:
        """
        通知Jellyfin刷新整个媒体库
        """
        if not self._host or not self._apikey:
            return False
        req_url = "%sLibrary/Refresh?api_key=%s" % (self._host, self._apikey)
        try:
            res = RequestUtils().post_res(req_url)
            if res:
                return True
            else:
                logger.info(f"刷新媒体库失败，无法连接Jellyfin！")
        except Exception as e:
            logger.error(f"连接Library/Refresh出错：" + str(e))
            return False

    def get_webhook_message(self, body: any) -> Optional[schemas.WebhookEventInfo]:
        """
        解析Jellyfin报文
        {
          "ServerId": "d79d3a6261614419a114595a585xxxxx",
          "ServerName": "nyanmisaka-jellyfin1",
          "ServerVersion": "10.8.10",
          "ServerUrl": "http://xxxxxxxx:8098",
          "NotificationType": "PlaybackStart",
          "Timestamp": "2023-09-10T08:35:25.3996506+00:00",
          "UtcTimestamp": "2023-09-10T08:35:25.3996527Z",
          "Name": "慕灼华逃婚离开",
          "Overview": "慕灼华假装在读书，她害怕大娘子说她不务正业。",
          "Tagline": "",
          "ItemId": "4b92551344f53b560fb55cd6700xxxxx",
          "ItemType": "Episode",
          "RunTimeTicks": 27074985984,
          "RunTime": "00:45:07",
          "Year": 2023,
          "SeriesName": "灼灼风流",
          "SeasonNumber": 1,
          "SeasonNumber00": "01",
          "SeasonNumber000": "001",
          "EpisodeNumber": 1,
          "EpisodeNumber00": "01",
          "EpisodeNumber000": "001",
          "Provider_tmdb": "229210",
          "Video_0_Title": "4K HEVC SDR",
          "Video_0_Type": "Video",
          "Video_0_Codec": "hevc",
          "Video_0_Profile": "Main",
          "Video_0_Level": 150,
          "Video_0_Height": 2160,
          "Video_0_Width": 3840,
          "Video_0_AspectRatio": "16:9",
          "Video_0_Interlaced": false,
          "Video_0_FrameRate": 25,
          "Video_0_VideoRange": "SDR",
          "Video_0_ColorSpace": "bt709",
          "Video_0_ColorTransfer": "bt709",
          "Video_0_ColorPrimaries": "bt709",
          "Video_0_PixelFormat": "yuv420p",
          "Video_0_RefFrames": 1,
          "Audio_0_Title": "AAC - Stereo - Default",
          "Audio_0_Type": "Audio",
          "Audio_0_Language": "und",
          "Audio_0_Codec": "aac",
          "Audio_0_Channels": 2,
          "Audio_0_Bitrate": 125360,
          "Audio_0_SampleRate": 48000,
          "Audio_0_Default": true,
          "PlaybackPositionTicks": 1000000,
          "PlaybackPosition": "00:00:00",
          "MediaSourceId": "4b92551344f53b560fb55cd6700ebc86",
          "IsPaused": false,
          "IsAutomated": false,
          "DeviceId": "TW96aWxsxxxxxjA",
          "DeviceName": "Edge Chromium",
          "ClientName": "Jellyfin Web",
          "NotificationUsername": "Jeaven",
          "UserId": "9783d2432b0d40a8a716b6aa46xxxxx"
        }
        """
        if not body:
            return None
        try:
            message = json.loads(body)
        except Exception as e:
            logger.debug(f"解析Jellyfin Webhook报文出错：" + str(e))
            return None
        if not message:
            return None
        logger.debug(f"接收到jellyfin webhook：{message}")
        eventType = message.get('NotificationType')
        if not eventType:
            return None
        eventItem = schemas.WebhookEventInfo(
            event=eventType,
            channel="jellyfin"
        )
        eventItem.item_id = message.get('ItemId')
        eventItem.tmdb_id = message.get('Provider_tmdb')
        eventItem.overview = message.get('Overview')
        eventItem.device_name = message.get('DeviceName')
        eventItem.user_name = message.get('NotificationUsername')
        eventItem.client = message.get('ClientName')
        eventItem.media_type = message.get('ItemType')
        if message.get("ItemType") == "Episode" \
                or message.get("ItemType") == "Series" \
                or message.get("ItemType") == "Season":
            # 剧集
            eventItem.item_type = "TV"
            eventItem.season_id = message.get('SeasonNumber')
            eventItem.episode_id = message.get('EpisodeNumber')
            eventItem.item_name = "%s %s%s %s" % (
                message.get('SeriesName'),
                "S" + str(eventItem.season_id),
                "E" + str(eventItem.episode_id),
                message.get('Name'))
        else:
            # 电影
            eventItem.item_type = "MOV"
            eventItem.item_name = "%s %s" % (
                message.get('Name'), "(" + str(message.get('Year')) + ")")

        # 获取消息图片
        if eventItem.item_id:
            # 根据返回的item_id去调用媒体服务器获取
            eventItem.image_url = self.get_remote_image_by_id(
                item_id=eventItem.item_id,
                image_type="Backdrop"
            )

        return eventItem

    def get_iteminfo(self, itemid: str) -> Optional[schemas.MediaServerItem]:
        """
        获取单个项目详情
        """
        if not itemid:
            return None
        if not self._host or not self._apikey:
            return None
        req_url = "%sUsers/%s/Items/%s?api_key=%s" % (
            self._host, self.user, itemid, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res and res.status_code == 200:
                item = res.json()
                tmdbid = item.get("ProviderIds", {}).get("Tmdb")
                return schemas.MediaServerItem(
                    server="jellyfin",
                    library=item.get("ParentId"),
                    item_id=item.get("Id"),
                    item_type=item.get("Type"),
                    title=item.get("Name"),
                    original_title=item.get("OriginalTitle"),
                    year=item.get("ProductionYear"),
                    tmdbid=int(tmdbid) if tmdbid else None,
                    imdbid=item.get("ProviderIds", {}).get("Imdb"),
                    tvdbid=item.get("ProviderIds", {}).get("Tvdb"),
                    path=item.get("Path")
                )
        except Exception as e:
            logger.error(f"连接Users/Items出错：" + str(e))
        return None

    def get_items(self, parent: str) -> Generator:
        """
        获取媒体服务器所有媒体库列表
        """
        if not parent:
            yield None
        if not self._host or not self._apikey:
            yield None
        req_url = "%sUsers/%s/Items?parentId=%s&api_key=%s" % (self._host, self.user, parent, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res and res.status_code == 200:
                results = res.json().get("Items") or []
                for result in results:
                    if not result:
                        continue
                    if result.get("Type") in ["Movie", "Series"]:
                        yield self.get_iteminfo(result.get("Id"))
                    elif "Folder" in result.get("Type"):
                        for item in self.get_items(result.get("Id")):
                            yield item
        except Exception as e:
            logger.error(f"连接Users/Items出错：" + str(e))
        yield None

    def get_data(self, url: str) -> Optional[Response]:
        """
        自定义URL从媒体服务器获取数据，其中[HOST]、[APIKEY]、[USER]会被替换成实际的值
        :param url: 请求地址
        """
        if not self._host or not self._apikey:
            return None
        url = url.replace("[HOST]", self._host) \
            .replace("[APIKEY]", self._apikey) \
            .replace("[USER]", self.user)
        try:
            return RequestUtils(accept_type="application/json").get_res(url=url)
        except Exception as e:
            logger.error(f"连接Jellyfin出错：" + str(e))
            return None

    def post_data(self, url: str, data: str = None, headers: dict = None) -> Optional[Response]:
        """
        自定义URL从媒体服务器获取数据，其中[HOST]、[APIKEY]、[USER]会被替换成实际的值
        :param url: 请求地址
        :param data: 请求数据
        :param headers: 请求头
        """
        if not self._host or not self._apikey:
            return None
        url = url.replace("[HOST]", self._host) \
            .replace("[APIKEY]", self._apikey) \
            .replace("[USER]", self.user)
        try:
            return RequestUtils(
                headers=headers
            ).post_res(url=url, data=data)
        except Exception as e:
            logger.error(f"连接Jellyfin出错：" + str(e))
            return None
