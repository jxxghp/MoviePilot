import json
from typing import List, Union, Optional, Dict, Generator, Tuple

from requests import Response

from app import schemas
from app.core.config import settings
from app.log import logger
from app.schemas import MediaType
from app.utils.http import RequestUtils


class Jellyfin:

    def __init__(self):
        self._host = settings.JELLYFIN_HOST
        if self._host:
            self._host = RequestUtils.standardize_base_url(self._host)
        self._playhost = settings.JELLYFIN_PLAY_HOST
        if self._playhost:
            self._playhost = RequestUtils.standardize_base_url(self._playhost)
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

    def get_jellyfin_folders(self) -> List[dict]:
        """
        获取Jellyfin媒体库路径列表
        """
        if not self._host or not self._apikey:
            return []
        req_url = "%sLibrary/SelectableMediaFolders?api_key=%s" % (self._host, self._apikey)
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

    def get_jellyfin_virtual_folders(self) -> List[dict]:
        """
        获取Jellyfin媒体库所有路径列表（包含共享路径）
        """
        if not self._host or not self._apikey:
            return []
        req_url = "%sLibrary/VirtualFolders?api_key=%s" % (self._host, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                library_items = res.json()
                librarys = []
                for library_item in library_items:
                    library_name = library_item.get('Name')
                    pathInfos = library_item.get('LibraryOptions', {}).get('PathInfos')
                    library_paths = []
                    for path in pathInfos:
                        if path.get('NetworkPath'):
                            library_paths.append(path.get('NetworkPath'))
                        else:
                            library_paths.append(path.get('Path'))

                    if library_name and library_paths:
                        librarys.append({
                            'Name': library_name,
                            'Path': library_paths
                        })
                return librarys
            else:
                logger.error(f"Library/VirtualFolders 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接Library/VirtualFolders 出错：" + str(e))
            return []

    def __get_jellyfin_librarys(self, username: str = None) -> List[dict]:
        """
        获取Jellyfin媒体库的信息
        """
        if not self._host or not self._apikey:
            return []
        if username:
            user = self.get_user(username)
        else:
            user = self.user
        req_url = f"{self._host}Users/{user}/Views?api_key={self._apikey}"
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

    def get_librarys(self, username: str = None) -> List[schemas.MediaServerLibrary]:
        """
        获取媒体服务器所有媒体库列表
        """
        if not self._host or not self._apikey:
            return []
        libraries = []
        black_list = (settings.MEDIASERVER_SYNC_BLACKLIST or '').split(",")
        for library in self.__get_jellyfin_librarys(username) or []:
            if library.get("Name") in black_list:
                continue
            match library.get("CollectionType"):
                case "movies":
                    library_type = MediaType.MOVIE.value
                case "tvshows":
                    library_type = MediaType.TV.value
                case _:
                    continue
            image = self.__get_local_image_by_id(library.get("Id"))
            link = f"{self._playhost or self._host}web/index.html#!" \
                   f"/movies.html?topParentId={library.get('Id')}" \
                if library_type == MediaType.MOVIE.value \
                else f"{self._playhost or self._host}web/index.html#!" \
                     f"/tv.html?topParentId={library.get('Id')}"
            libraries.append(
                schemas.MediaServerLibrary(
                    server="jellyfin",
                    id=library.get("Id"),
                    name=library.get("Name"),
                    path=library.get("Path"),
                    type=library_type,
                    image=image,
                    link=link
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
                return item_id, season_episodes
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
            res = RequestUtils(timeout=10).get_res(req_url)
            if res:
                images = res.json().get("Images")
                for image in images:
                    if image.get("ProviderName") == "TheMovieDb" and image.get("Type") == image_type:
                        return image.get("Url")
                # return images[0].get("Url") # 首选无则返回第一张
            else:
                logger.info(f"Items/RemoteImages 未获取到返回数据，采用本地图片")
                return self.generate_image_link(item_id, image_type, True)
        except Exception as e:
            logger.error(f"连接Items/Id/RemoteImages出错：" + str(e))
            return None
        return None

    def generate_image_link(self, item_id: str, image_type: str, host_type: bool) -> Optional[str]:
        """
        根据ItemId和imageType查询本地对应图片
        :param item_id: 在Jellyfin中的ID
        :param image_type: 图片类型，如Backdrop、Primary
        :param host_type: True为外网链接, False为内网链接
        :return: 图片对应在host_type的播放器中的URL
        """
        if not self._playhost:
            logger.error("Jellyfin外网播放地址未能获取或为空")
            return None
        # 检测是否为TV
        _parent_id = self.get_itemId_ancestors(item_id, 0, "ParentBackdropItemId")
        if _parent_id:
            item_id = _parent_id

        _host = self._host
        if host_type:
            _host = self._playhost
        req_url = "%sItems/%s/Images/%s" % (_host, item_id, image_type)
        try:
            res = RequestUtils().get_res(req_url)
            if res and res.status_code != 404:
                logger.info(f"影片图片链接:{res.url}")
                return res.url
            else:
                logger.error("Items/Id/Images 未获取到返回数据或无该影片{}图片".format(image_type))
                return None
        except Exception as e:
            logger.error(f"连接Items/Id/Images出错：" + str(e))
            return None

    def get_itemId_ancestors(self, item_id: str, index: int, key: str) -> Optional[Union[str, list, int, dict, bool]]:
        """
        获得itemId的父item
        :param item_id: 在Jellyfin中剧集的ID (S01E02的E02的item_id)
        :param index: 第几个json对象
        :param key: 需要得到父item中的键值对
        :return key对应类型的值
        """
        req_url = "%sItems/%s/Ancestors?api_key=%s" % (self._host, item_id, self._apikey)
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                return res.json()[index].get(key)
            else:
                logger.error(f"Items/Id/Ancestors 未获取到返回数据")
                return None
        except Exception as e:
            logger.error(f"连接Items/Id/Ancestors出错：" + str(e))
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
        eventItem.item_favorite = message.get('Favorite')
        eventItem.save_reason = message.get('SaveReason')
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

        playback_position_ticks = message.get('PlaybackPositionTicks')
        runtime_ticks = message.get('RunTimeTicks')
        if playback_position_ticks is not None and runtime_ticks is not None:
            eventItem.percentage = playback_position_ticks / runtime_ticks * 100

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
        url = url.replace("[HOST]", self._host or '') \
            .replace("[APIKEY]", self._apikey or '') \
            .replace("[USER]", self.user or '')
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
        url = url.replace("[HOST]", self._host or '') \
            .replace("[APIKEY]", self._apikey or '') \
            .replace("[USER]", self.user or '')
        try:
            return RequestUtils(
                headers=headers
            ).post_res(url=url, data=data)
        except Exception as e:
            logger.error(f"连接Jellyfin出错：" + str(e))
            return None

    def get_play_url(self, item_id: str) -> str:
        """
        拼装媒体播放链接
        :param item_id: 媒体的的ID
        """
        return f"{self._playhost or self._host}web/index.html#!" \
               f"/details?id={item_id}&serverId={self.serverid}"

    def __get_local_image_by_id(self, item_id: str) -> str:
        """
        根据ItemId从媒体服务器查询有声书图片地址
        :param: item_id: 在Emby中的ID
        :param: remote 是否远程使用，TG微信等客户端调用应为True
        :param: inner 是否NT内部调用，为True是会使用NT中转
        """
        if not self._host or not self._apikey:
            return ""
        return "%sItems/%s/Images/Primary" % (self._host, item_id)

    def __get_backdrop_url(self, item_id: str, image_tag: str) -> str:
        """
        获取Backdrop图片地址
        :param: item_id: 在Emby中的ID
        :param: image_tag: 图片的tag
        :param: remote 是否远程使用，TG微信等客户端调用应为True
        :param: inner 是否NT内部调用，为True是会使用NT中转
        """
        if not self._host or not self._apikey:
            return ""
        if not image_tag or not item_id:
            return ""
        return f"{self._host}Items/{item_id}/" \
               f"Images/Backdrop?tag={image_tag}&fillWidth=666&api_key={self._apikey}"

    def get_resume(self, num: int = 12, username: str = None) -> Optional[List[schemas.MediaServerPlayItem]]:
        """
        获得继续观看
        """
        if not self._host or not self._apikey:
            return None
        if username:
            user = self.get_user(username)
        else:
            user = self.user
        req_url = (f"{self._host}Users/{user}/Items/Resume?"
                   f"Limit=100&MediaTypes=Video&api_key={self._apikey}&Fields=ProductionYear,Path")
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                result = res.json().get("Items") or []
                ret_resume = []
                # 用户媒体库文件夹列表（排除黑名单）
                library_folders = self.get_user_library_folders()
                for item in result:
                    if len(ret_resume) == num:
                        break
                    if item.get("Type") not in ["Movie", "Episode"]:
                        continue
                    item_path = item.get("Path")
                    if item_path and library_folders and not any(
                            str(item_path).startswith(folder) for folder in library_folders):
                        continue
                    item_type = MediaType.MOVIE.value if item.get("Type") == "Movie" else MediaType.TV.value
                    link = self.get_play_url(item.get("Id"))
                    if item.get("BackdropImageTags"):
                        image = self.__get_backdrop_url(item_id=item.get("Id"),
                                                        image_tag=item.get("BackdropImageTags")[0])
                    else:
                        image = self.__get_local_image_by_id(item.get("Id"))
                    # 小部分剧集无[xxx-S01E01-thumb.jpg]图片
                    image_res = RequestUtils().get_res(image)
                    if not image_res or image_res.status_code == 404:
                        image = self.generate_image_link(item.get("Id"), "Backdrop", False)
                    if item_type == MediaType.MOVIE.value:
                        title = item.get("Name")
                        subtitle = item.get("ProductionYear")
                    else:
                        title = f'{item.get("SeriesName")}'
                        subtitle = f'S{item.get("ParentIndexNumber")}:{item.get("IndexNumber")} - {item.get("Name")}'
                    ret_resume.append(schemas.MediaServerPlayItem(
                        id=item.get("Id"),
                        title=title,
                        subtitle=subtitle,
                        type=item_type,
                        image=image,
                        link=link,
                        percent=item.get("UserData", {}).get("PlayedPercentage")
                    ))
                return ret_resume
            else:
                logger.error(f"Users/Items/Resume 未获取到返回数据")
        except Exception as e:
            logger.error(f"连接Users/Items/Resume出错：" + str(e))
        return []

    def get_latest(self, num=20, username: str = None) -> Optional[List[schemas.MediaServerPlayItem]]:
        """
        获得最近更新
        """
        if not self._host or not self._apikey:
            return None
        if username:
            user = self.get_user(username)
        else:
            user = self.user
        req_url = (f"{self._host}Users/{user}/Items/Latest?"
                   f"Limit=100&MediaTypes=Video&api_key={self._apikey}&Fields=ProductionYear,Path")
        try:
            res = RequestUtils().get_res(req_url)
            if res:
                result = res.json() or []
                ret_latest = []
                # 用户媒体库文件夹列表（排除黑名单）
                library_folders = self.get_user_library_folders()
                for item in result:
                    if len(ret_latest) == num:
                        break
                    if item.get("Type") not in ["Movie", "Series"]:
                        continue
                    item_path = item.get("Path")
                    if item_path and library_folders and not any(
                            str(item_path).startswith(folder) for folder in library_folders):
                        continue
                    item_type = MediaType.MOVIE.value if item.get("Type") == "Movie" else MediaType.TV.value
                    link = self.get_play_url(item.get("Id"))
                    image = self.__get_local_image_by_id(item_id=item.get("Id"))
                    ret_latest.append(schemas.MediaServerPlayItem(
                        id=item.get("Id"),
                        title=item.get("Name"),
                        subtitle=item.get("ProductionYear"),
                        type=item_type,
                        image=image,
                        link=link
                    ))
                return ret_latest
            else:
                logger.error(f"Users/Items/Latest 未获取到返回数据")
        except Exception as e:
            logger.error(f"连接Users/Items/Latest出错：" + str(e))
        return []

    def get_user_library_folders(self):
        """
        获取Emby媒体库文件夹列表（排除黑名单）
        """
        if not self._host or not self._apikey:
            return []
        library_folders = []
        black_list = (settings.MEDIASERVER_SYNC_BLACKLIST or '').split(",")
        for library in self.get_jellyfin_virtual_folders() or []:
            if library.get("Name") in black_list:
                continue
            library_folders += [folder for folder in library.get("Path")]
        return library_folders
