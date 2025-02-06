import json
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union, Dict, Generator, Tuple

from requests import Response

from app import schemas
from app.core.config import settings
from app.log import logger
from app.schemas.types import MediaType
from app.utils.http import RequestUtils
from app.utils.url import UrlUtils


class Emby:
    _host: str = None
    _playhost: str = None
    _apikey: str = None
    _sync_libraries: List[str] = []
    user: Optional[Union[str, int]] = None

    def __init__(self, host: str = None, apikey: str = None, play_host: str = None,
                 sync_libraries: list = None, **kwargs):
        if not host or not apikey:
            logger.error("Emby服务器配置不完整！")
            return
        self._host = host
        if self._host:
            self._host = UrlUtils.standardize_base_url(self._host)
        self._playhost = play_host
        if self._playhost:
            self._playhost = UrlUtils.standardize_base_url(self._playhost)
        self._apikey = apikey
        self.user = self.get_user(settings.SUPERUSER)
        self.folders = self.get_emby_folders()
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
        self.folders = self.get_emby_folders()

    def get_emby_folders(self) -> List[dict]:
        """
        获取Emby媒体库路径列表
        """
        if not self._host or not self._apikey:
            return []
        url = f"{self._host}emby/Library/SelectableMediaFolders"
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

    def get_emby_virtual_folders(self) -> List[dict]:
        """
        获取Emby媒体库所有路径列表（包含共享路径）
        """
        if not self._host or not self._apikey:
            return []
        url = f"{self._host}emby/Library/VirtualFolders/Query"
        params = {
            'api_key': self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
            if res:
                library_items = res.json().get("Items")
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
                logger.error(f"Library/VirtualFolders/Query 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接Library/VirtualFolders/Query 出错：" + str(e))
            return []

    def __get_emby_librarys(self, username: str = None) -> List[dict]:
        """
        获取Emby媒体库列表
        """
        if not self._host or not self._apikey:
            return []
        if username:
            user = self.get_user(username)
        else:
            user = self.user
        url = f"{self._host}emby/Users/{user}/Views"
        params = {"api_key": self._apikey}
        try:
            res = RequestUtils().get_res(url, params)
            if res:
                return res.json().get("Items")
            else:
                logger.error(f"User/Views 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接User/Views 出错：" + str(e))
            return []

    def get_librarys(self, username: str = None, hidden: bool = False) -> List[schemas.MediaServerLibrary]:
        """
        获取媒体服务器所有媒体库列表
        """
        if not self._host or not self._apikey:
            return []
        libraries = []
        for library in self.__get_emby_librarys(username) or []:
            if hidden and self._sync_libraries and "all" not in self._sync_libraries \
                    and library.get("Id") not in self._sync_libraries:
                continue
            match library.get("CollectionType"):
                case "movies":
                    library_type = MediaType.MOVIE.value
                case "tvshows":
                    library_type = MediaType.TV.value
                case _:
                    library_type = MediaType.UNKNOWN.value
            image = self.__get_local_image_by_id(library.get("Id"))
            libraries.append(
                schemas.MediaServerLibrary(
                    server="emby",
                    id=library.get("Id"),
                    name=library.get("Name"),
                    path=library.get("Path"),
                    type=library_type,
                    image=image,
                    link=f'{self._playhost or self._host}web/index.html'
                         f'#!/videos?serverId={self.serverid}&parentId={library.get("Id")}'
                )
            )
        return libraries

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
        :return: 认证token
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/Users/AuthenticateByName"
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

    def get_user_count(self) -> int:
        """
        获得用户数量
        """
        if not self._host or not self._apikey:
            return 0
        url = f"{self._host}emby/Users/Query"
        params = {
            'api_key': self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
            if res:
                return res.json().get("TotalRecordCount")
            else:
                logger.error(f"Users/Query 未获取到返回数据")
                return 0
        except Exception as e:
            logger.error(f"连接Users/Query出错：" + str(e))
            return 0

    def get_medias_count(self) -> schemas.Statistic:
        """
        获得电影、电视剧、动漫媒体数量
        :return: MovieCount SeriesCount SongCount
        """
        if not self._host or not self._apikey:
            return schemas.Statistic()
        url = f"{self._host}emby/Items/Counts"
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

    def __get_emby_series_id_by_name(self, name: str, year: str) -> Optional[str]:
        """
        根据名称查询Emby中剧集的SeriesId
        :param name: 标题
        :param year: 年份
        :return: None 表示连不通，""表示未找到，找到返回ID
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/Items"
        params = {
            "IncludeItemTypes": "Series",
            "Fields": "ProductionYear",
            "StartIndex": 0,
            "Recursive": "true",
            "SearchTerm": name,
            "Limit": 10,
            "IncludeSearchTypes": "false",
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
        根据标题和年份，检查电影是否在Emby中存在，存在则返回列表
        :param title: 标题
        :param year: 年份，可以为空，为空时不按年份过滤
        :param tmdb_id: TMDB ID
        :return: 含title、year属性的字典列表
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/Items"
        params = {
            "IncludeItemTypes": "Movie",
            "Fields": "ProviderIds,OriginalTitle,ProductionYear,Path,UserDataPlayCount,UserDataLastPlayedDate,ParentId",
            "StartIndex": 0,
            "Recursive": "true",
            "SearchTerm": title,
            "Limit": 10,
            "IncludeSearchTypes": "false",
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
                        season: int = None
                        ) -> Tuple[Optional[str], Optional[Dict[int, List[int]]]]:
        """
        根据标题和年份和季，返回Emby中的剧集列表
        :param item_id: Emby中的ID
        :param title: 标题
        :param year: 年份
        :param tmdb_id: TMDBID
        :param season: 季
        :return: 每一季的已有集数
        """
        if not self._host or not self._apikey:
            return None, None
        # 电视剧
        if not item_id:
            item_id = self.__get_emby_series_id_by_name(title, year)
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
        # 查集的信息
        if not season:
            season = ""
        try:
            url = f"{self._host}emby/Shows/{item_id}/Episodes"
            params = {
                "Season": season,
                "IsMissing": "false",
                "api_key": self._apikey
            }
            res_json = RequestUtils().get_res(url, params)
            if res_json:
                tv_item = res_json.json()
                res_items = tv_item.get("Items")
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
                    if season_index not in season_episodes:
                        season_episodes[season_index] = []
                    season_episodes[season_index].append(episode_index)
                # 返回
                return item_id, season_episodes
        except Exception as e:
            logger.error(f"连接Shows/Id/Episodes出错：" + str(e))
            return None, None
        return None, {}

    def get_remote_image_by_id(self, item_id: str, image_type: str) -> Optional[str]:
        """
        根据ItemId从Emby查询TMDB的图片地址
        :param item_id: 在Emby中的ID
        :param image_type: 图片的类弄地，poster或者backdrop等
        :return: 图片对应在TMDB中的URL
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/Items/{item_id}/RemoteImages"
        params = {
            "api_key": self._apikey
        }
        try:
            res = RequestUtils(timeout=10).get_res(url, params)
            if res:
                images = res.json().get("Images")
                if images:
                    for image in images:
                        if image.get("ProviderName") == "TheMovieDb" and image.get("Type") == image_type:
                            return image.get("Url")
            # 数据为空
            logger.info(f"Items/RemoteImages 未获取到返回数据，采用本地图片")
            return self.generate_external_image_link(item_id, image_type)
        except Exception as e:
            logger.error(f"连接Items/Id/RemoteImages出错：" + str(e))
        return None

    def generate_external_image_link(self, item_id: str, image_type: str) -> Optional[str]:
        """
        根据ItemId和imageType查询本地对应图片
        :param item_id: 在Emby中的ID
        :param image_type: 图片类型，如Backdrop、Primary
        :return: 图片对应在外网播放器中的URL
        """
        if not self._playhost:
            logger.error("Emby外网播放地址未能获取或为空")
            return None

        url = f"{self._playhost}Items/{item_id}/Images/{image_type}"
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

    def __refresh_emby_library_by_id(self, item_id: str) -> bool:
        """
        通知Emby刷新一个项目的媒体库
        """
        if not self._host or not self._apikey:
            return False
        url = f"{self._host}emby/Items/{item_id}/Refresh"
        params = {
            "Recursive": "true",
            "api_key": self._apikey
        }
        try:
            res = RequestUtils().post_res(url, params=params)
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
        url = f"{self._host}emby/Library/Refresh"
        params = {
            "api_key": self._apikey
        }
        try:
            res = RequestUtils().post_res(url, params=params)
            if res:
                return True
            else:
                logger.info(f"刷新媒体库失败，无法连接Emby！")
        except Exception as e:
            logger.error(f"连接Library/Refresh出错：" + str(e))
            return False
        return False

    def refresh_library_by_items(self, items: List[schemas.RefreshMediaItem]) -> bool:
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

    def __get_emby_library_id_by_item(self, item: schemas.RefreshMediaItem) -> Optional[str]:
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
        # 匹配子目录
        for folder in self.folders:
            for subfolder in folder.get("SubFolders"):
                try:
                    # 匹配子目录
                    subfolder_path = Path(subfolder.get("Path"))
                    if item_path.is_relative_to(subfolder_path):
                        return folder.get("Id")
                except Exception as err:
                    logger.debug(f"匹配子目录出错：{err} - {traceback.format_exc()}")
        # 如果找不到，只要路径中有分类目录名就命中
        for folder in self.folders:
            for subfolder in folder.get("SubFolders"):
                if subfolder.get("Path") and re.search(r"[/\\]%s" % item.category,
                                                       subfolder.get("Path")):
                    return folder.get("Id")
        # 刷新根目录
        return "/"

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
                server="emby",
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
        url = f"{self._host}emby/Users/{self.user}/Items/{itemid}"
        params = {
            "api_key": self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
            if res and res.status_code == 200:
                iteminfo = self.__format_item_info(res.json())
                return iteminfo
        except Exception as e:
            logger.error(f"连接/Users/{self.user}/Items/{itemid}出错：" + str(e))
        return None

    def get_items(self, parent: Union[str, int], start_index: int = 0, limit: Optional[int] = -1) \
            -> Optional[Generator]:
        """
        获取媒体服务器项目列表，支持分页和不分页逻辑，默认不分页获取所有数据

        :param parent: 媒体库ID，用于标识要获取的媒体库
        :param start_index: 起始索引，用于分页获取数据。默认为 0，即从第一个项目开始获取
        :param limit: 每次请求的最大项目数，用于分页。如果为 None 或 -1，则表示一次性获取所有数据，默认为 -1

        :return: 返回一个生成器对象，用于逐步获取媒体服务器中的项目
        """
        if not parent or not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/Users/{self.user}/Items"
        params = {
            "ParentId": parent,
            "api_key": self._apikey,
            "Fields": "ProviderIds,OriginalTitle,ProductionYear,Path,UserDataPlayCount,UserDataLastPlayedDate,ParentId"
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
                    for items in self.get_items(parent=item.get('Id')):
                        yield items
                elif item.get("Type") in ["Movie", "Series"]:
                    yield self.__format_item_info(item)

        except Exception as e:
            logger.error(f"连接Users/Items出错：" + str(e))

    def get_webhook_message(self, form: any, args: dict) -> Optional[schemas.WebhookEventInfo]:
        """
        解析Emby Webhook报文
        电影：
        {
          "Title": "admin 在 Microsoft Edge Windows 上停止播放 蜘蛛侠：纵横宇宙",
          "Date": "2023-08-19T00:49:07.8523469Z",
          "Event": "playback.stop",
          "User": {
            "Name": "admin",
            "Id": "e6a9dd89fd954d689870e7e0e3e72947"
          },
          "Item": {
            "Name": "蜘蛛侠：纵横宇宙",
            "OriginalTitle": "Spider-Man: Across the Spider-Verse",
            "ServerId": "f40a5bd0c6b64051bdbed00580fa1118",
            "Id": "240270",
            "DateCreated": "2023-06-21T21:01:27.0000000Z",
            "Container": "mp4",
            "SortName": "蜘蛛侠：纵横宇宙",
            "PremiereDate": "2023-05-30T16:00:00.0000000Z",
            "ExternalUrls": [
              {
                "Name": "IMDb",
                "Url": "https://www.imdb.com/title/tt9362722"
              },
              {
                "Name": "TheMovieDb",
                "Url": "https://www.themoviedb.org/movie/569094"
              },
              {
                "Name": "Trakt",
                "Url": "https://trakt.tv/search/tmdb/569094?id_type=movie"
              }
            ],
            "Path": "\\\\10.10.10.10\\Video\\电影\\动画电影\\蜘蛛侠：纵横宇宙 (2023)\\蜘蛛侠：纵横宇宙 (2023).mp4",
            "OfficialRating": "PG",
            "Overview": "讲述了新生代蜘蛛侠迈尔斯（沙梅克·摩尔 Shameik Moore 配音）携手蜘蛛格温（海莉·斯坦菲尔德 Hailee Steinfeld 配音），穿越多元宇宙踏上更宏大的冒险征程的故事。面临每个蜘蛛侠都会失去至亲的宿命，迈尔斯誓言打破命运魔咒，找到属于自己的英雄之路。而这个决定和蜘蛛侠2099（奥斯卡·伊萨克 Oscar Is aac 配音）所领军的蜘蛛联盟产生了极大冲突，一场以一敌百的蜘蛛侠大内战即将拉响！",
            "Taglines": [],
            "Genres": [
              "动作",
              "冒险",
              "动画",
              "科幻"
            ],
            "CommunityRating": 8.7,
            "RunTimeTicks": 80439590000,
            "Size": 3170164641,
            "FileName": "蜘蛛侠：纵横宇宙 (2023).mp4",
            "Bitrate": 3152840,
            "PlayAccess": "Full",
            "ProductionYear": 2023,
            "RemoteTrailers": [
              {
                "Url": "https://www.youtube.com/watch?v=BbXJ3_AQE_o"
              },
              {
                "Url": "https://www.youtube.com/watch?v=cqGjhVJWtEg"
              },
              {
                "Url": "https://www.youtube.com/watch?v=shW9i6k8cB0"
              },
              {
                "Url": "https://www.youtube.com/watch?v=Etv-L2JKCWk"
              },
              {
                "Url": "https://www.youtube.com/watch?v=yFrxzaBLDQM"
              }
            ],
            "ProviderIds": {
              "Tmdb": "569094",
              "Imdb": "tt9362722"
            },
            "IsFolder": false,
            "ParentId": "240253",
            "Type": "Movie",
            "Studios": [
              {
                "Name": "Columbia Pictures",
                "Id": 1252
              },
              {
                "Name": "Sony Pictures Animation",
                "Id": 1814
              },
              {
                "Name": "Lord Miller",
                "Id": 240307
              },
              {
                "Name": "Pascal Pictures",
                "Id": 60101
              },
              {
                "Name": "Arad Productions",
                "Id": 67372
              }
            ],
            "GenreItems": [
              {
                "Name": "动作",
                "Id": 767
              },
              {
                "Name": "冒险",
                "Id": 818
              },
              {
                "Name": "动画",
                "Id": 1382
              },
              {
                "Name": "科幻",
                "Id": 709
              }
            ],
            "TagItems": [],
            "PrimaryImageAspectRatio": 0.7012622720897616,
            "ImageTags": {
              "Primary": "c080830ff3c964a775dd0b011b675a29",
              "Art": "a418b990ca0df95838884b5951883ad5",
              "Logo": "1782310274c108e85d02d2b0b1c7249c",
              "Thumb": "29d499a96b7da07cd1cf37edb58507a8",
              "Banner": "bec236365d57f7f646d8fda16fce2ecb",
              "Disc": "3e32d87be8655f52bcf43bd34ee94c2b"
            },
            "BackdropImageTags": [
              "13acab1246c95a6fbdee22cf65edf3f0"
            ],
            "MediaType": "Video",
            "Width": 1920,
            "Height": 820
          },
          "Server": {
            "Name": "PN41",
            "Id": "f40a5bd0c6b64051bdbed00580fa1118",
            "Version": "4.7.13.0"
          },
          "Session": {
            "RemoteEndPoint": "10.10.10.253",
            "Client": "Emby Web",
            "DeviceName": "Microsoft Edge Windows",
            "DeviceId": "30239450-1748-4855-9799-de3544fc2744",
            "ApplicationVersion": "4.7.13.0",
            "Id": "c336b028b893558b333d1a49238b7db1"
          },
          "PlaybackInfo": {
            "PlayedToCompletion": false,
            "PositionTicks": 17431791950,
            "PlaylistIndex": 0,
            "PlaylistLength": 1
          }
        }

        电视剧：
        {
          "Title": "admin 在 Microsoft Edge Windows 上开始播放 长风渡 - S1, Ep11 - 第 11 集",
          "Date": "2023-08-19T00:52:20.5200050Z",
          "Event": "playback.start",
          "User": {
            "Name": "admin",
            "Id": "e6a9dd89fd954d689870e7e0e3e72947"
          },
          "Item": {
            "Name": "第 11 集",
            "ServerId": "f40a5bd0c6b64051bdbed00580fa1118",
            "Id": "240252",
            "DateCreated": "2023-06-21T10:51:06.0000000Z",
            "Container": "mp4",
            "SortName": "第 11 集",
            "PremiereDate": "2023-06-20T16:00:00.0000000Z",
            "ExternalUrls": [
              {
                "Name": "Trakt",
                "Url": "https://trakt.tv/search/tmdb/4533239?id_type=episode"
              }
            ],
            "Path": "\\\\10.10.10.10\\Video\\电视剧\\国产剧\\长风渡 (2023)\\Season 1\\长风渡 - S01E11 - 第 11 集.mp4",
            "Taglines": [],
            "Genres": [],
            "RunTimeTicks": 28021450000,
            "Size": 707122056,
            "FileName": "长风渡 - S01E11 - 第 11 集.mp4",
            "Bitrate": 2018802,
            "PlayAccess": "Full",
            "ProductionYear": 2023,
            "IndexNumber": 11,
            "ParentIndexNumber": 1,
            "RemoteTrailers": [],
            "ProviderIds": {
              "Tmdb": "4533239"
            },
            "IsFolder": false,
            "ParentId": "240203",
            "Type": "Episode",
            "Studios": [],
            "GenreItems": [],
            "TagItems": [],
            "ParentLogoItemId": "240202",
            "ParentBackdropItemId": "240202",
            "ParentBackdropImageTags": [
              "7dd568c67721c1f184b281001ced2f8e"
            ],
            "SeriesName": "长风渡",
            "SeriesId": "240202",
            "SeasonId": "240203",
            "PrimaryImageAspectRatio": 2.4,
            "SeriesPrimaryImageTag": "e91c822173e9bcbf7a0efa7d1c16f6bd",
            "SeasonName": "季 1",
            "ImageTags": {
              "Primary": "d6bf1d76150cd86fdff746e4353569ee"
            },
            "BackdropImageTags": [],
            "ParentLogoImageTag": "51cf6b2661c3c9cef3796abafd6a1694",
            "MediaType": "Video",
            "Width": 1920,
            "Height": 800
          },
          "Server": {
            "Name": "PN41",
            "Id": "f40a5bd0c6b64051bdbed00580fa1118",
            "Version": "4.7.13.0"
          },
          "Session": {
            "RemoteEndPoint": "10.10.10.253",
            "Client": "Emby Web",
            "DeviceName": "Microsoft Edge Windows",
            "DeviceId": "30239450-1748-4855-9799-de3544fc2744",
            "ApplicationVersion": "4.7.13.0",
            "Id": "c336b028b893558b333d1a49238b7db1"
          },
          "PlaybackInfo": {
            "PositionTicks": 14256663550,
            "PlaylistIndex": 10,
            "PlaylistLength": 40
          }
        }
        """
        if not form and not args:
            return None
        try:
            if form and form.get("data"):
                result = form.get("data")
            else:
                result = json.dumps(dict(args))
            message = json.loads(result)
        except Exception as e:
            logger.debug(f"解析emby webhook报文出错：" + str(e))
            return None
        eventType = message.get('Event')
        if not eventType:
            return None
        logger.debug(f"接收到emby webhook：{message}")
        eventItem = schemas.WebhookEventInfo(event=eventType, channel="emby")
        if message.get('Item'):
            eventItem.media_type = message.get('Item', {}).get('Type')
            if message.get('Item', {}).get('Type') == 'Episode' \
                    or message.get('Item', {}).get('Type') == 'Series' \
                    or message.get('Item', {}).get('Type') == 'Season':
                eventItem.item_type = "TV"
                if message.get('Item', {}).get('SeriesName') \
                        and message.get('Item', {}).get('ParentIndexNumber') \
                        and message.get('Item', {}).get('IndexNumber'):
                    eventItem.item_name = "%s %s%s %s" % (
                        message.get('Item', {}).get('SeriesName'),
                        "S" + str(message.get('Item', {}).get('ParentIndexNumber')),
                        "E" + str(message.get('Item', {}).get('IndexNumber')),
                        message.get('Item', {}).get('Name'))
                elif message.get('Item', {}).get('SeriesName'):
                    eventItem.item_name = "%s %s" % (
                        message.get('Item', {}).get('SeriesName'),
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
                eventItem.item_id = message.get('Item', {}).get('Id')

            eventItem.item_path = message.get('Item', {}).get('Path')
            eventItem.tmdb_id = message.get('Item', {}).get('ProviderIds', {}).get('Tmdb')
            if message.get('Item', {}).get('Overview') and len(message.get('Item', {}).get('Overview')) > 100:
                eventItem.overview = str(message.get('Item', {}).get('Overview'))[:100] + "..."
            else:
                eventItem.overview = message.get('Item', {}).get('Overview')
            eventItem.percentage = message.get('TranscodingInfo', {}).get('CompletionPercentage')
            if not eventItem.percentage:
                if message.get('PlaybackInfo', {}).get('PositionTicks') and message.get('Item', {}).get('RunTimeTicks'):
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
        自定义URL从媒体服务器获取数据，其中[HOST]、[APIKEY]、[USER]会被替换成实际的值
        :param url: 请求地址
        """
        if not self._host or not self._apikey:
            return None
        url = url.replace("[HOST]", self._host or '') \
            .replace("[APIKEY]", self._apikey or '') \
            .replace("[USER]", self.user or '')
        try:
            return RequestUtils(content_type="application/json").get_res(url=url)
        except Exception as e:
            logger.error(f"连接Emby出错：" + str(e))
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
                headers=headers,
            ).post_res(url=url, data=data)
        except Exception as e:
            logger.error(f"连接Emby出错：" + str(e))
            return None

    def get_play_url(self, item_id: str) -> str:
        """
        拼装媒体播放链接
        :param item_id: 媒体的的ID
        """
        return f"{self._playhost or self._host}web/index.html#!" \
               f"/item?id={item_id}&context=home&serverId={self.serverid}"

    def get_backdrop_url(self, item_id: str, image_tag: str, remote: bool = False) -> str:
        """
        获取Emby的Backdrop图片地址
        :param: item_id: 在Emby中的ID
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

    def __get_local_image_by_id(self, item_id: str) -> str:
        """
        根据ItemId从媒体服务器查询本地图片地址
        :param: item_id: 在Emby中的ID
        :param: remote 是否远程使用，TG微信等客户端调用应为True
        :param: inner 是否NT内部调用，为True是会使用NT中转
        """
        if not self._host or not self._apikey:
            return ""
        return "%sItems/%s/Images/Primary" % (self._host, item_id)

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
                    if item_type == MediaType.MOVIE.value:
                        title = item.get("Name")
                        subtitle = item.get("ProductionYear")
                    else:
                        title = f'{item.get("SeriesName")}'
                        subtitle = f'S{item.get("ParentIndexNumber")}:{item.get("IndexNumber")} - {item.get("Name")}'
                    if item_type == MediaType.MOVIE.value:
                        if item.get("BackdropImageTags"):
                            image = self.get_backdrop_url(item_id=item.get("Id"),
                                                          image_tag=item.get("BackdropImageTags")[0])
                        else:
                            image = self.__get_local_image_by_id(item.get("Id"))
                    else:
                        image = self.get_backdrop_url(item_id=item.get("SeriesId"),
                                                      image_tag=item.get("SeriesPrimaryImageTag"))
                        if not image:
                            image = self.__get_local_image_by_id(item.get("SeriesId"))
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

    def get_latest(self, num: int = 20, username: str = None) -> Optional[List[schemas.MediaServerPlayItem]]:
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
            "api_key": self._apikey
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
        获取Emby媒体库文件夹列表（排除黑名单）
        """
        if not self._host or not self._apikey:
            return []
        library_folders = []
        for library in self.get_emby_virtual_folders() or []:
            if self._sync_libraries and library.get("Id") not in self._sync_libraries:
                continue
            library_folders += [folder for folder in library.get("Path")]
        return library_folders
