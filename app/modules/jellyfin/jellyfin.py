import json
from datetime import datetime
from typing import List, Union, Optional, Dict, Generator, Tuple, Any

from requests import Response

from app import schemas
from app.core.config import settings
from app.log import logger
from app.schemas import MediaType
from app.utils.http import RequestUtils
from app.utils.url import UrlUtils
from schemas import MediaServerItem


class Jellyfin:
    _host: str = None
    _apikey: str = None
    _playhost: str = None
    _sync_libraries: List[str] = []
    user: Optional[Union[str, int]] = None

    def __init__(self, host: str = None, apikey: str = None, play_host: str = None,
                 sync_libraries: list = None, **kwargs):
        if not host or not apikey:
            logger.error("Jellyfin服务器配置不完整！！")
            return
        self._host = host
        if self._host:
            self._host = UrlUtils.standardize_base_url(self._host)
        self._playhost = play_host
        if self._playhost:
            self._playhost = UrlUtils.standardize_base_url(self._playhost)
        self._apikey = apikey
        self.user = self.get_user(settings.SUPERUSER)
        self.serverid = self.get_server_id()
        self._sync_libraries = sync_libraries or []

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
        url = f"{self._host}Library/SelectableMediaFolders"
        params = {
            'api_key': self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
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

        url = f"{self._host}Library/VirtualFolders"
        params = {
            'api_key': self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
            if res:
                library_items = res.json()
                librarys = []
                for library_item in library_items:
                    library_id = library_item.get('ItemId')
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
                            'Id': library_id,
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
        url = f"{self._host}Users/{user}/Views"
        params = {"api_key": self._apikey}
        try:
            res = RequestUtils().get_res(url, params)
            if res:
                return res.json().get("Items")
            else:
                logger.error(f"Users/Views 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接Users/Views 出错：" + str(e))
            return []

    def get_librarys(self, username: str = None, hidden: bool = False) -> List[schemas.MediaServerLibrary]:
        """
        获取媒体服务器所有媒体库列表
        """
        if not self._host or not self._apikey:
            return []
        libraries = []
        for library in self.__get_jellyfin_librarys(username) or []:
            if hidden and self._sync_libraries and "all" not in self._sync_libraries \
                    and library.get("Id") not in self._sync_libraries:
                continue
            if library.get("CollectionType") == "movies":
                library_type = MediaType.MOVIE.value
                link = f"{self._playhost or self._host}web/index.html#!" \
                       f"/movies.html?topParentId={library.get('Id')}"
            elif library.get("CollectionType") == "tvshows":
                library_type = MediaType.TV.value
                link = f"{self._playhost or self._host}web/index.html#!" \
                       f"/tv.html?topParentId={library.get('Id')}"
            else:
                library_type = MediaType.UNKNOWN.value
                link = f"{self._playhost or self._host}web/index.html#!" \
                       f"/library.html?topParentId={library.get('Id')}"
            image = self.__get_local_image_by_id(library.get("Id"))
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
        url = f"{self._host}Users"
        params = {
            "api_key": self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
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
        url = f"{self._host}Users"
        params = {
            "api_key": self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
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
        url = f"{self._host}Users/authenticatebyname"
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
                url=url,
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
        url = f"{self._host}System/Info"
        params = {
            'api_key': self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
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
        url = f"{self._host}Items/Counts"
        params = {
            'api_key': self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
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
        url = f"{self._host}Users/{self.user}/Items"
        params = {
            "IncludeItemTypes": "Series",
            "Recursive": "true",
            "searchTerm": name,
            "Limit": 10,
            "api_key": self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
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
        url = f"{self._host}Users/{self.user}/Items"
        params = {
            "IncludeItemTypes": "Movie",
            "Fields": "ProviderIds,OriginalTitle,ProductionYear,Path,UserDataPlayCount,UserDataLastPlayedDate,ParentId",
            "StartIndex": 0,
            "Recursive": "true",
            "searchTerm": title,
            "Limit": 10,
            "api_key": self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
            if res:
                res_items = res.json().get("Items")
                if res_items:
                    ret_movies = []
                    for item in res_items:
                        if not item:
                            continue
                        mediaserver_item = self.__format_item_info(item)
                        if mediaserver_item:
                            if (not tmdb_id or mediaserver_item.tmdbid == tmdb_id) and \
                                    mediaserver_item.title == title and \
                                    (not year or str(mediaserver_item.year) == str(year)):
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
            season = None
        url = f"{self._host}Shows/{item_id}/Episodes"
        params = {
            "season": season,
            "userId": self.user,
            "isMissing": "false",
            "api_key": self._apikey
        }
        try:
            res_json = RequestUtils().get_res(url, params)
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
        :param item_id: 在Jellyfin中的ID
        :param image_type: 图片的类弄地，poster或者backdrop等
        :return: 图片对应在TMDB中的URL
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}Items/{item_id}/RemoteImages"
        params = {"api_key": self._apikey}
        try:
            res = RequestUtils(timeout=10).get_res(url, params)
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

    def get_item_path_by_id(self, item_id: str) -> Optional[str]:
        """
        根据ItemId查询所在的Path
        :param item_id: 在Jellyfin中的ID
        :return: Path
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}Items/{item_id}/PlaybackInfo"
        params = {"api_key": self._apikey}
        try:
            res = RequestUtils(timeout=10).get_res(url, params)
            if res:
                media_sources = res.json().get("MediaSources")
                if media_sources:
                    return media_sources[0].get("Path")
            else:
                logger.error("Items/Id/PlaybackInfo 未获取到返回数据，不设置 Path")
                return None
        except Exception as e:
            logger.error("连接Items/Id/PlaybackInfo出错：" + str(e))
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
        url = f"{_host}Items/{item_id}/Images/{image_type}"
        try:
            res = RequestUtils().get_res(url)
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
        url = f"{self._host}Items/{item_id}/Ancestors"
        params = {
            "api_key": self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
            if res:
                return res.json()[index].get(key)
            else:
                logger.error(f"Items/Id/Ancestors 未获取到返回数据")
                return None
        except Exception as e:
            logger.error(f"连接Items/Id/Ancestors出错：" + str(e))
            return None

    def refresh_root_library(self) -> Optional[bool]:
        """
        通知Jellyfin刷新整个媒体库
        """
        if not self._host or not self._apikey:
            return False
        url = f"{self._host}Library/Refresh"
        params = {
            "api_key": self._apikey
        }
        try:
            res = RequestUtils().post_res(url, params=params)
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
        elif message.get("ItemType") == 'Audio':
            # 音乐
            eventItem.item_type = "AUD"
            eventItem.item_name = message.get('Album')
            eventItem.overview = message.get('Name')
            eventItem.item_id = message.get('ItemId')
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
            # jellyfin 的 webhook 不含 item_path，需要单独获取
            eventItem.item_path = self.get_item_path_by_id(eventItem.item_id)

        return eventItem

    @staticmethod
    def __format_item_info(item) -> Optional[schemas.MediaServerItem]:
        """
        格式化item
        """
        try:
            user_data = item.get("UserData", {})
            if not user_data:
                user_state = None
            else:
                resume = item.get("UserData", {}).get("PlaybackPositionTicks") and item.get("UserData", {}).get(
                    "PlaybackPositionTicks") > 0
                last_played_date = item.get("UserData", {}).get("LastPlayedDate")
                if last_played_date is not None and "." in last_played_date:
                    last_played_date = last_played_date.split(".")[0]
                user_state = schemas.MediaServerItemUserState(
                    played=item.get("UserData", {}).get("Played"),
                    resume=resume,
                    last_played_date=datetime.strptime(last_played_date, "%Y-%m-%dT%H:%M:%S").strftime(
                        "%Y-%m-%d %H:%M:%S") if last_played_date else None,
                    play_count=item.get("UserData", {}).get("PlayCount"),
                    percentage=item.get("UserData", {}).get("PlayedPercentage"),
                )
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
                path=item.get("Path"),
                user_state=user_state

            )
        except Exception as e:
            logger.error(e)
        return None

    def get_iteminfo(self, itemid: str) -> Optional[schemas.MediaServerItem]:
        """
        获取单个项目详情
        """
        if not itemid:
            return None
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}Users/{self.user}/Items/{itemid}"
        params = {
            "api_key": self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
            if res and res.status_code == 200:
                return self.__format_item_info(res.json())
        except Exception as e:
            logger.error(f"连接Users/{self.user}/Items/{itemid}：" + str(e))
        return None

    def get_items(self, parent: Union[str, int], start_index: int = 0, limit: Optional[int] = -1) \
            -> Generator[MediaServerItem | None | Any, Any, None]:
        """
        获取媒体服务器项目列表，支持分页和不分页逻辑，默认不分页获取所有数据

        :param parent: 媒体库ID，用于标识要获取的媒体库
        :param start_index: 起始索引，用于分页获取数据。默认为 0，即从第一个项目开始获取
        :param limit: 每次请求的最大项目数，用于分页。如果为 None 或 -1，则表示一次性获取所有数据，默认为 -1

        :return: 返回一个生成器对象，用于逐步获取媒体服务器中的项目
        """
        if not parent or not self._host or not self._apikey:
            return None
        url = f"{self._host}Users/{self.user}/Items"
        params = {
            "ParentId": parent,
            "api_key": self._apikey,
            "Fields": "ProviderIds,OriginalTitle,ProductionYear,Path,UserDataPlayCount,UserDataLastPlayedDate,ParentId",
        }
        if limit is not None and limit != -1:
            params.update({
                "StartIndex": start_index,
                "Limit": limit
            })
        try:
            res = RequestUtils().get_res(url, params)
            if not res or res.status_code != 200:
                return None
            items = res.json().get("Items") or []
            for item in items:
                if not item:
                    continue
                if "Folder" in item.get("Type"):
                    for items in self.get_items(item.get("Id")):
                        yield items
                elif item.get("Type") in ["Movie", "Series"]:
                    yield self.__format_item_info(item)
        except Exception as e:
            logger.error(f"连接Users/Items出错：" + str(e))

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
        :param: item_id: 在Jellyfin中的ID
        :param: remote 是否远程使用，TG微信等客户端调用应为True
        :param: inner 是否NT内部调用，为True是会使用NT中转
        """
        if not self._host or not self._apikey:
            return ""
        return "%sItems/%s/Images/Primary" % (self._host, item_id)

    def get_backdrop_url(self, item_id: str, image_tag: str, remote: bool = False) -> str:
        """
        获取Backdrop图片地址
        :param: item_id: 在Jellyfin中的ID
        :param: image_tag: 图片的tag
        :param: remote 是否远程使用，TG微信等客户端调用应为True
        """
        if not self._host or not self._apikey:
            return ""
        if not image_tag or not item_id:
            return ""
        if remote:
            host_url = self._playhost or self._host
        else:
            host_url = self._host
        return f"{host_url}Items/{item_id}/" \
               f"Images/Backdrop?tag={image_tag}&api_key={self._apikey}"

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

        url = f"{self._host}Users/{user}/Items/Resume"
        params = {
            "Limit": 100,
            "MediaTypes": "Video",
            "Fields": "ProductionYear,Path",
            "api_key": self._apikey,
        }
        try:
            res = RequestUtils().get_res(url, params)
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
                        image = self.get_backdrop_url(item_id=item.get("Id"),
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
        url = f"{self._host}Users/{user}/Items/Latest"
        params = {
            "Limit": 100,
            "MediaTypes": "Video",
            "Fields": "ProductionYear,Path,BackdropImageTags",
            "api_key": self._apikey,
        }
        try:
            res = RequestUtils().get_res(url, params)
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
                        link=link,
                        BackdropImageTags=item.get("BackdropImageTags")
                    ))
                return ret_latest
            else:
                logger.error(f"Users/Items/Latest 未获取到返回数据")
        except Exception as e:
            logger.error(f"连接Users/Items/Latest出错：" + str(e))
        return []

    def get_user_library_folders(self):
        """
        获取Jellyfin媒体库文件夹列表（排除黑名单）
        """
        if not self._host or not self._apikey:
            return []
        library_folders = []
        for library in self.get_jellyfin_virtual_folders() or []:
            if self._sync_libraries and library.get("Id") not in self._sync_libraries:
                continue
            library_folders += [folder for folder in library.get("Path")]
        return library_folders
