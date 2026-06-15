import json
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union, Dict, Generator, Tuple, Any

from requests import Response

from app import schemas
from app.log import logger
from app.schemas import MediaServerItem
from app.schemas.types import MediaType
from app.utils.http import RequestUtils
from app.utils.url import UrlUtils


DEFAULT_ITEMS_PAGE_SIZE = 100


class ZSpace:
    _host: Optional[str] = None
    _playhost: Optional[str] = None
    _apikey: Optional[str] = None
    _sync_libraries: List[str] = []
    user: Optional[Union[str, int]] = None
    _username: Optional[str] = None
    _password: Optional[str] = None

    def __init__(self, host: Optional[str] = None, username: Optional[str] = None,
                 password: Optional[str] = None, play_host: Optional[str] = None,
                 sync_libraries: list = None, **kwargs):
        if not host or not username or not password:
            logger.error("极影视服务器配置不完整！")
            return
        self._host = host
        if self._host:
            self._host = UrlUtils.standardize_base_url(self._host)
        self._playhost = play_host
        if self._playhost:
            self._playhost = UrlUtils.standardize_base_url(self._playhost)
        self._username = username
        self._password = password
        self._sync_libraries = sync_libraries or []
        self.user = None
        self.folders = []
        self.serverid = None
        if not self.reconnect():
            logger.error(f"请检查极影视服务端地址 {host}")

    @staticmethod
    def __get_client_authorization() -> str:
        """
        构造客户端标识头。

        极影视兼容 Emby 登录接口时，需要携带客户端、设备和版本信息，
        这里统一复用一份固定头，避免各接口散落重复字符串。
        """
        return 'MediaBrowser Client="MoviePilot", Device="requests", DeviceId="1", Version="1.0.0"'

    @staticmethod
    def __get_user_authorization(user_id: Union[str, int]) -> str:
        """
        构造用户态授权头。

        保留这个组装函数，便于后续需要显式传递用户态 Authorization 头时复用。
        当前极影视兼容 Emby 实测主要依赖 `X-Emby-Token`，这里不默认附带该头，
        避免部分实现把非 GUID 的用户 ID 校验为非法格式。
        """
        return f'Emby UserId="{user_id}", Client="MoviePilot", Device="requests", DeviceId="1", Version="1.0.0"'

    def __request_utils(self,
                        timeout: Optional[int] = None,
                        include_token: bool = True,
                        headers: Optional[dict] = None) -> RequestUtils:
        """
        统一构造极影视请求客户端。

        极影视这里使用用户名密码登录获取 AccessToken，后续 API 请求优先通过
        `X-Emby-Token` 请求头传递 token，而不是把登录 token 当成静态 API Key。
        """
        request_headers = {
            "X-Emby-Authorization": self.__get_client_authorization()
        }
        if include_token and self._apikey:
            request_headers["X-Emby-Token"] = self._apikey
        if headers:
            request_headers.update(headers)
        return RequestUtils(headers=request_headers, timeout=timeout)

    def is_inactive(self) -> bool:
        """
        判断是否需要重连
        """
        if not self._host or not self._username or not self._password:
            return False
        if not self._apikey or not self.user:
            return True
        current_user = self.__get_current_user()
        if not current_user:
            return True
        self.user = current_user.get("Id") or self.user
        return False

    def reconnect(self) -> bool:
        """
        重连
        """
        token, user_id = self.__login(self._username, self._password)
        if not token:
            self._apikey = None
            self.user = None
            self.folders = []
            self.serverid = None
            return False
        self._apikey = token
        self.user = user_id or self.__get_current_user_id()
        if not self.user:
            self._apikey = None
            self.folders = []
            self.serverid = None
            return False
        self.folders = self.get_library_folders()
        self.serverid = self.get_server_id()
        return True

    def get_library_folders(self) -> List[dict]:
        """
        获取极影视媒体库路径列表。

        极影视当前 Emby 兼容层（`System/Info` 返回 ServerVersion=4.7.0.0，
        对齐 Emby Server 4.7 协议）未实现 `Library/SelectableMediaFolders`
        （实测 404）。此处先尝试标准端点；不可用时退化为 `Users/{uid}/Views`
        的返回（仅有 Id/Name，**没有 SubFolders/Path**）——下游按子目录路径
        匹配库 ID 的逻辑在该服务端上无法工作，会回退到整库刷新分支。
        """
        if not self._host or not self._apikey:
            return []
        url = f"{self._host}emby/Library/SelectableMediaFolders"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                return res.json()
            logger.debug("Library/SelectableMediaFolders 未获取到返回数据，回退到 Users/{uid}/Views")
        except Exception as e:
            logger.debug(f"连接Library/SelectableMediaFolders 出错：{e}，回退到 Users/{{uid}}/Views")
        return self.__get_library_views() or []

    def get_virtual_folders(self) -> List[dict]:
        """
        获取极影视媒体库所有路径列表（包含共享路径）。

        极影视当前 Emby 兼容层（`System/Info` 返回 ServerVersion=4.7.0.0，
        对齐 Emby Server 4.7 协议）未实现 `Library/VirtualFolders/Query`
        （实测 404），且 `Users/{uid}/Views` 与 `Users/{uid}/Items` 返回里
        `Path` 均为空字符串，因此该端点不可用时仅能给出"库 Id/Name + 空路径
        列表"。下游 `get_user_library_folders()` 会跳过空 Path 的库，等价于
        对所有库都不做路径前缀过滤——这是当前可用的最小可工作策略。
        """
        if not self._host or not self._apikey:
            return []
        url = f"{self._host}emby/Library/VirtualFolders/Query"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                library_items = res.json().get("Items")
                libraries = []
                for library_item in library_items or []:
                    library_id = library_item.get('ItemId')
                    library_name = library_item.get('Name')
                    path_infos = library_item.get('LibraryOptions', {}).get('PathInfos')
                    library_paths = []
                    for path in path_infos or []:
                        if path.get('NetworkPath'):
                            library_paths.append(path.get('NetworkPath'))
                        else:
                            library_paths.append(path.get('Path'))

                    if library_name and library_paths:
                        libraries.append({
                            'Id': library_id,
                            'Name': library_name,
                            'Path': library_paths
                        })
                return libraries
            logger.debug("Library/VirtualFolders/Query 未获取到返回数据，回退到 Users/{uid}/Views（路径列表为空）")
        except Exception as e:
            logger.debug(f"连接Library/VirtualFolders/Query 出错：{e}，回退到 Users/{{uid}}/Views（路径列表为空）")
        libraries = []
        for view in self.__get_library_views() or []:
            view_id = view.get("Id")
            view_name = view.get("Name")
            if view_id and view_name:
                libraries.append({
                    'Id': view_id,
                    'Name': view_name,
                    'Path': []
                })
        return libraries

    def __get_library_views(self, username: Optional[str] = None) -> List[dict]:
        """
        获取极影视媒体库列表
        """
        if not self._host or not self._apikey:
            return []
        if username:
            user = self.get_user(username)
        else:
            user = self.user
        if not user:
            return []
        url = f"{self._host}emby/Users/{user}/Views"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                return res.json().get("Items")
            else:
                logger.error("Users/Views 未获取到返回数据")
                return []
        except Exception as e:
            logger.error(f"连接Users/Views 出错：{e}")
            return []

    def get_librarys(self, username: Optional[str] = None, hidden: Optional[bool] = False) -> List[
        schemas.MediaServerLibrary]:
        """
        获取媒体服务器所有媒体库列表
        """
        if not self._host or not self._apikey:
            return []
        libraries = []
        for library in self.__get_library_views(username) or []:
            if hidden and self._sync_libraries and "all" not in self._sync_libraries \
                    and library.get("Id") not in self._sync_libraries:
                continue
            if library.get("CollectionType") == "movies":
                library_type = MediaType.MOVIE.value
            elif library.get("CollectionType") == "tvshows":
                library_type = MediaType.TV.value
            else:
                library_type = MediaType.UNKNOWN.value
            image = self.__get_local_image_by_id(library.get("Id"))
            libraries.append(
                schemas.MediaServerLibrary(
                    server="zspace",
                    id=library.get("Id"),
                    name=library.get("Name"),
                    path=library.get("Path"),
                    type=library_type,
                    image=image,
                    link=f'{self._playhost or self._host}web/index.html'
                         f'#!/videos?serverId={self.serverid}&parentId={library.get("Id")}',
                    server_type="zspace"
                )
            )
        return libraries

    def get_user(self, user_name: Optional[str] = None) -> Optional[Union[str, int]]:
        """
        获取可用用户ID，优先按用户名匹配，失败时回退当前登录用户。

        极影视当前 Emby 兼容层（`System/Info` 返回 ServerVersion=4.7.0.0，
        对齐 Emby Server 4.7 协议）暂未实现 `Users` 全量列表端点（实测 404），
        所以多用户名匹配在该服务端不可用；这里按"端点可用→按名匹配 / 端点不可用→
        当前登录用户兜底"的顺序处理，避免主日志反复刷 ERROR。
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/Users"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                users = res.json()
                if isinstance(users, list):
                    if user_name:
                        for user in users:
                            if user.get("Name") == user_name:
                                return user.get("Id")
                    for user in users:
                        if user.get("Policy", {}).get("IsAdministrator"):
                            return user.get("Id")
                    for user in users:
                        if user.get("Id"):
                            return user.get("Id")
                else:
                    logger.debug("Users 返回数据格式错误，回退到当前登录用户")
            else:
                # 极影视未实现该端点会走到这里，仅 debug 级即可，避免污染主日志
                logger.debug("Users 未获取到返回数据，可能服务端未实现该端点，回退到当前登录用户")
        except Exception as e:
            logger.debug(f"连接Users出错：{e}，回退到当前登录用户")
        return self.__get_current_user_id() or self.user

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """
        用户认证
        :param username: 用户名
        :param password: 密码
        :return: 认证token
        """
        token, _ = self.__login(username, password)
        if token:
            logger.info(f"用户 {username} 极影视认证成功")
        return token

    def get_server_id(self) -> Optional[str]:
        """
        获得服务器信息
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/System/Info"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                return res.json().get("Id")
            else:
                logger.error("System/Info 未获取到返回数据")
        except Exception as e:
            logger.error(f"连接System/Info出错：{e}")
        return None

    def get_user_count(self) -> int:
        """
        获得用户数量。

        极影视当前 Emby 兼容层（`System/Info` 返回 ServerVersion=4.7.0.0，
        对齐 Emby Server 4.7 协议）的 `Users/Query` 端点会把路径段 "Query"
        当作 mediaUid 校验，返回 400 "invalid mediaUid format"；此时无法
        从服务端拿到真实用户数，退化为：已登录则至少有 1 个用户。
        """
        if not self._host or not self._apikey:
            return 0
        url = f"{self._host}emby/Users/Query"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                count = res.json().get("TotalRecordCount")
                if count:
                    return count
            else:
                # 极影视未实现该端点会走到这里，降为 debug 避免主日志误报
                logger.debug("Users/Query 未获取到返回数据，可能服务端未实现该端点，回退到登录用户兜底")
        except Exception as e:
            logger.debug(f"连接Users/Query出错：{e}，回退到登录用户兜底")
        return 1 if self.user else 0

    def get_medias_count(self) -> schemas.Statistic:
        """
        获得电影、电视剧、动漫媒体数量。

        极影视当前 Emby 兼容层（`System/Info` 返回 ServerVersion=4.7.0.0，
        对齐 Emby Server 4.7 协议）相关接口实测：
        - `Items/Counts` → 401 "无权限"；`Users/{uid}/Items/Counts` → 500，
          两条标准聚合路径都拿不到全局统计。
        - `Users/{uid}/Items` 的 `IncludeItemTypes` 参数：
          - 在带 `ParentId` 时**完全被忽略**（同一库无论传 Movie/Series/
            Episode/Folder/Audio，TotalRecordCount 都等于该库总条数）。
          - 在不带 `ParentId` 的全局层面，单类型 `Movie` / `Series` 过滤
            生效；`Episode` 与多类型逗号组合（如 `Movie,Series`）不生效。
        - Views 返回的 `CollectionType` 在该服务端恒为 null，无法直接
          区分库类型。
        因为还要遵循 `_sync_libraries` 选中过滤，不能用"两次全局过滤请求"
        的简化方案（全局聚合无 ParentId 入参）。降级流程：
        1. 先尝试标准 `Items/Counts`；
        2. 失败则遍历**被选中**的媒体库视图，按 `Users/{uid}/Items
           ?ParentId=...&Limit=0` 拿单库 TotalRecordCount，再通过
           `Limit=1` 采样首条目 `Type` 兜底分桶到 movie / tv；
        3. 集数维度在该服务端无法可靠拿到，统一计 0。
        :return: MovieCount SeriesCount EpisodeCount
        """
        if not self._host or not self._apikey:
            return schemas.Statistic()
        url = f"{self._host}emby/Items/Counts"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                result = res.json()
                return schemas.Statistic(
                    movie_count=result.get("MovieCount") or 0,
                    tv_count=result.get("SeriesCount") or 0,
                    episode_count=result.get("EpisodeCount") or 0
                )
            logger.debug("Items/Counts 未获取到返回数据，回退到按媒体库累计 TotalRecordCount")
        except Exception as e:
            logger.debug(f"连接Items/Counts出错：{e}，回退到按媒体库累计 TotalRecordCount")
        return self.__count_medias_by_views()

    def __count_medias_by_views(self) -> schemas.Statistic:
        """
        通过遍历媒体库视图累计条目数，兜底实现 `get_medias_count`。

        - 仅统计 `_sync_libraries` 选中的库；空集或包含 `"all"` 时视为
          全部，与 `get_librarys` / `get_user_library_folders` 的过滤语义
          保持一致。
        - 对每个被选中的 View 调 `Users/{uid}/Items?ParentId=<libId>
          &Recursive=true&Limit=0` 只拿 `TotalRecordCount`，再按视图类型
          分桶到 movie / tv。极影视当前 Emby 兼容层返回的 View 中
          `CollectionType` 恒为 null，无法直接判定库类型，因此优先用
          `CollectionType`；缺失时用 `Limit=1` 采样一条目，按返回条目的
          `Type`（Movie / Series / Episode）兜底分类。
        - 集数维度在该服务端无法可靠拿到（见 `get_medias_count` 注释），
          统一计为 0。
        """
        if not self._host or not self._apikey or not self.user:
            return schemas.Statistic()
        # 与 get_librarys / get_user_library_folders 保持一致的选中库过滤：
        # _sync_libraries 为空或包含 "all" 视为全部库。
        sync_all = (not self._sync_libraries) or ("all" in self._sync_libraries)
        stat = schemas.Statistic()
        for view in self.__get_library_views() or []:
            view_id = view.get("Id")
            if not view_id:
                continue
            if not sync_all and view_id not in self._sync_libraries:
                continue
            total, bucket = self.__count_view(view_id, view.get("CollectionType"))
            if not total:
                continue
            if bucket == "movies":
                stat.movie_count = (stat.movie_count or 0) + total
            elif bucket == "tvshows":
                stat.tv_count = (stat.tv_count or 0) + total
        return stat

    def __count_view(self, view_id: str,
                     collection_type: Optional[str]) -> Tuple[int, Optional[str]]:
        """
        返回单个媒体库视图的 (TotalRecordCount, 桶名)。桶名取 `movies`/`tvshows`，
        无法判定时返回 None。CollectionType 缺失时采样首个 Item 的 `Type` 决定。
        """
        count_url = f"{self._host}emby/Users/{self.user}/Items"
        try:
            res = self.__request_utils().get_res(
                count_url,
                params={"ParentId": view_id, "Recursive": "true", "Limit": 0}
            )
            if not res:
                return 0, None
            total = res.json().get("TotalRecordCount") or 0
        except Exception as e:
            logger.debug(f"按媒体库 {view_id} 统计 TotalRecordCount 出错：{e}")
            return 0, None
        if not total:
            return 0, None
        if collection_type == "movies":
            return total, "movies"
        if collection_type == "tvshows":
            return total, "tvshows"
        # CollectionType 缺失时采样一条目按 Type 兜底分类
        try:
            res = self.__request_utils().get_res(
                count_url,
                params={"ParentId": view_id, "Recursive": "true", "Limit": 1}
            )
            items = (res.json().get("Items") if res else None) or []
            sample_type = items[0].get("Type") if items else None
        except Exception as e:
            logger.debug(f"采样媒体库 {view_id} 首条目类型出错：{e}")
            sample_type = None
        if sample_type == "Movie":
            return total, "movies"
        if sample_type in ("Series", "Episode", "Season"):
            return total, "tvshows"
        return total, None

    def __get_series_id_by_name(self, name: str, year: str) -> Optional[str]:
        """
        根据名称查询极影视中剧集的 SeriesId
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
            "IncludeSearchTypes": "false"
        }
        try:
            res = self.__request_utils().get_res(url, params=params)
            if res:
                res_items = res.json().get("Items")
                if res_items:
                    for res_item in res_items:
                        if res_item.get('Name') == name and (
                                not year or str(res_item.get('ProductionYear')) == str(year)):
                            return res_item.get('Id')
        except Exception as e:
            logger.error(f"连接Items出错：{e}")
            return None
        return ""

    def get_movies(self,
                   title: str,
                   year: Optional[str] = None,
                   tmdb_id: Optional[int] = None) -> Optional[List[schemas.MediaServerItem]]:
        """
        根据标题和年份，检查电影是否在极影视中存在，存在则返回列表
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
            "IncludeSearchTypes": "false"
        }
        try:
            res = self.__request_utils().get_res(url, params=params)
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
            logger.error(f"连接Items出错：{e}")
            return None
        return []

    def get_tv_episodes(self,
                        item_id: Optional[str] = None,
                        title: Optional[str] = None,
                        year: Optional[str] = None,
                        tmdb_id: Optional[int] = None,
                        season: Optional[int] = None
                        ) -> Tuple[Optional[str], Optional[Dict[int, List[int]]]]:
        """
        根据标题和年份和季，返回极影视中的剧集列表
        :param item_id: 极影视中的ID
        :param title: 标题
        :param year: 年份
        :param tmdb_id: TMDBID
        :param season: 季
        :return: 每一季的已有集数
        """
        if not self._host or not self._apikey:
            return None, None
        cached_item_id = item_id
        if not item_id:
            item_id = self.__get_series_id_by_name(title, year)
            if item_id is None:
                return None, None
            if not item_id:
                return None, {}
        item_info = self.get_iteminfo(item_id)
        if not item_info and cached_item_id and title:
            logger.warning(f"极影视缓存的电视剧媒体ID {cached_item_id} 已失效，尝试按标题重新搜索：{title}")
            item_id = self.__get_series_id_by_name(title, year)
            if item_id is None:
                return None, None
            if not item_id:
                return None, {}
            item_info = self.get_iteminfo(item_id)
        if not item_info:
            return None, {}
        if item_info and tmdb_id and item_info.tmdbid:
            if str(tmdb_id) != str(item_info.tmdbid):
                return None, {}
        if season is None:
            season = None
        try:
            url = f"{self._host}emby/Shows/{item_id}/Episodes"
            params = {
                "Season": season,
                "IsMissing": "false"
            }
            res_json = self.__request_utils().get_res(url, params=params)
            if res_json:
                tv_item = res_json.json()
                res_items = tv_item.get("Items")
                season_episodes = {}
                for res_item in res_items or []:
                    season_index = res_item.get("ParentIndexNumber")
                    if season_index is None:
                        continue
                    if season is not None and season != season_index:
                        continue
                    episode_index = res_item.get("IndexNumber")
                    if episode_index is None:
                        continue
                    if season_index not in season_episodes:
                        season_episodes[season_index] = []
                    season_episodes[season_index].append(episode_index)
                return item_id, season_episodes
        except Exception as e:
            logger.error(f"连接Shows/Id/Episodes出错：{e}")
            return None, None
        return None, {}

    def get_remote_image_by_id(self, item_id: str, image_type: str) -> Optional[str]:
        """
        根据ItemId从极影视查询TMDB的图片地址
        :param item_id: 在极影视中的ID
        :param image_type: 图片类型，poster或者backdrop等
        :return: 图片对应在TMDB中的URL
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/Items/{item_id}/RemoteImages"
        try:
            res = self.__request_utils(timeout=10).get_res(url)
            if res:
                images = res.json().get("Images")
                if images:
                    for image in images:
                        if image.get("ProviderName") == "TheMovieDb" and image.get("Type") == image_type:
                            return image.get("Url")
            logger.info("Items/RemoteImages 未获取到返回数据，采用本地图片")
            return self.generate_external_image_link(item_id, image_type)
        except Exception as e:
            logger.error(f"连接Items/Id/RemoteImages出错：{e}")
        return None

    def generate_external_image_link(self, item_id: str, image_type: str) -> Optional[str]:
        """
        根据ItemId和imageType查询本地对应图片
        :param item_id: 在极影视中的ID
        :param image_type: 图片类型，如Backdrop、Primary
        :return: 图片对应在外网播放器中的URL
        """
        if not self._playhost:
            logger.error("极影视外网播放地址未能获取或为空")
            return None

        url = f"{self._playhost}emby/Items/{item_id}/Images/{image_type}"
        try:
            res = self.__request_utils().get_res(url)
            if res and res.status_code != 404:
                logger.info(f"影片图片链接:{res.url}")
                return res.url
            else:
                logger.info(f"Items/Id/Images 未获取到返回数据或无该影片{image_type}图片")
                return None
        except Exception as e:
            logger.error(f"连接Items/Id/Images出错：{e}")
            return None

    def __refresh_library_by_id(self, item_id: str) -> bool:
        """
        通知极影视刷新一个项目的媒体库。

        极影视当前 Emby 兼容层（`System/Info` 返回 ServerVersion=4.7.0.0，
        对齐 Emby Server 4.7 协议）未实现 `Items/{id}/Refresh`（实测 404），
        因此该方法目前必然失败。保留实现以便兼容层补齐后自动复用，并把端点
        缺失的常态情况降级为 debug，避免污染主日志。
        """
        if not self._host or not self._apikey:
            return False
        url = f"{self._host}emby/Items/{item_id}/Refresh"
        params = {
            "Recursive": "true"
        }
        try:
            res = self.__request_utils().post_res(url, params=params)
            if res:
                return True
            logger.debug(f"刷新媒体库对象 {item_id} 未生效，极影视当前 Emby 兼容层未实现该端点")
        except Exception as e:
            logger.debug(f"连接Items/Id/Refresh出错：{e}（极影视当前 Emby 兼容层未实现该端点）")
        return False

    def refresh_root_library(self) -> bool:
        """
        通知极影视刷新整个媒体库。

        与 `__refresh_library_by_id` 同源：极影视当前 Emby 兼容层未实现
        `Library/Refresh`（实测 404）。保留实现并把常态失败降为 debug。
        """
        if not self._host or not self._apikey:
            return False
        url = f"{self._host}emby/Library/Refresh"
        try:
            res = self.__request_utils().post_res(url)
            if res:
                return True
            logger.debug("刷新媒体库未生效，极影视当前 Emby 兼容层未实现 Library/Refresh")
        except Exception as e:
            logger.debug(f"连接Library/Refresh出错：{e}（极影视当前 Emby 兼容层未实现该端点）")
        return False

    def refresh_library_by_items(self, items: List[schemas.RefreshMediaItem]) -> Optional[bool]:
        """
        按类型、名称、年份来刷新媒体库
        :param items: 已识别的需要刷新媒体库的媒体信息列表
        """
        if not items:
            return False
        logger.info("开始刷新极影视媒体库...")
        library_ids = []
        for item in items:
            library_id = self.__get_library_id_by_item(item)
            if library_id and library_id not in library_ids:
                library_ids.append(library_id)
        if "/" in library_ids:
            return self.refresh_root_library()
        for library_id in library_ids:
            if library_id != "/":
                return self.__refresh_library_by_id(library_id)
        logger.info("极影视媒体库刷新完成")
        return True

    def __get_library_id_by_item(self, item: schemas.RefreshMediaItem) -> Optional[str]:
        """
        根据媒体信息查询在哪个媒体库，返回要刷新的位置的ID
        :param item: {title, year, type, category, target_path}
        """
        if not item.title or not item.year or not item.type:
            return None
        if item.type != MediaType.MOVIE.value:
            item_id = self.__get_series_id_by_name(item.title, item.year)
            if item_id:
                return item_id
        else:
            if self.get_movies(item.title, item.year):
                return None
        item_path = Path(item.target_path)
        for folder in self.folders:
            for subfolder in folder.get("SubFolders") or []:
                try:
                    subfolder_path = Path(subfolder.get("Path"))
                    if item_path.is_relative_to(subfolder_path):
                        return folder.get("Id")
                except Exception as err:
                    logger.debug(f"匹配子目录出错：{err} - {traceback.format_exc()}")
        for folder in self.folders:
            for subfolder in folder.get("SubFolders") or []:
                if subfolder.get("Path") and re.search(r"[/\\]%s" % item.category,
                                                       subfolder.get("Path")):
                    return folder.get("Id")
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
                server="zspace",
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
        if not self._host or not self._apikey or not self.user:
            return None
        url = f"{self._host}emby/Users/{self.user}/Items/{itemid}"
        try:
            res = self.__request_utils().get_res(url)
            if res and res.status_code == 200:
                iteminfo = self.__format_item_info(res.json())
                return iteminfo
        except Exception as e:
            logger.error(f"连接/Users/{self.user}/Items/{itemid}出错：{e}")
        return None

    def get_items(self, parent: Union[str, int], start_index: Optional[int] = 0,
                  limit: Optional[int] = -1) -> Generator[MediaServerItem | None | Any, Any, None]:
        """
        获取媒体服务器项目列表，支持分页和不分页逻辑，默认不分页获取所有数据

        :param parent: 媒体库ID，用于标识要获取的媒体库
        :param start_index: 起始索引，用于分页获取数据。默认为 0，即从第一个项目开始获取
        :param limit: 每次请求的最大项目数，用于分页。如果为 None 或 -1，则表示一次性获取所有数据，默认为 -1

        :return: 返回一个生成器对象，用于逐步获取媒体服务器中的项目
        """
        if not parent or not self._host or not self._apikey or not self.user:
            return None
        url = f"{self._host}emby/Users/{self.user}/Items"
        fetch_all = limit is None or limit == -1
        page_size = DEFAULT_ITEMS_PAGE_SIZE if fetch_all else limit
        current_start_index = max(start_index or 0, 0)
        while True:
            params = {
                "ParentId": parent,
                "Recursive": "true",
                "StartIndex": current_start_index,
                "Limit": page_size,
                "Fields": "ProviderIds,OriginalTitle,ProductionYear,Path,"
                          "UserDataPlayCount,UserDataLastPlayedDate,ParentId"
            }
            try:
                res = self.__request_utils().get_res(url, params=params)
                if not res or res.status_code != 200:
                    return None
                result = res.json() or {}
                items = result.get("Items") or []
                for item in items:
                    if not item:
                        continue
                    if item.get("Type") == "BoxSet" and item.get("Id"):
                        for sub_item in self.get_items(parent=item.get("Id")):
                            if sub_item:
                                yield sub_item
                        continue
                    if item.get("Type") not in ["Movie", "Series"]:
                        continue
                    provider_ids = item.get("ProviderIds") or {}
                    needs_detail = (
                        not provider_ids.get("Tmdb")
                        or not item.get("ProductionYear")
                        or not item.get("Path")
                    )
                    if needs_detail and item.get("Id"):
                        detail_item = self.get_iteminfo(item.get("Id"))
                        if detail_item:
                            yield detail_item
                            continue
                    yield self.__format_item_info(item)
            except Exception as e:
                logger.error(f"连接Users/Items出错：{e}")
                return None

            if not fetch_all:
                break
            current_start_index += len(items)
            total_count = result.get("TotalRecordCount")
            if not items or (
                    total_count is not None and current_start_index >= total_count
            ) or (
                    total_count is None and len(items) < page_size
            ):
                break
        return None

    def get_webhook_message(self, form: Any, args: dict) -> Optional[schemas.WebhookEventInfo]:
        """
        解析极影视 Webhook 报文
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
            logger.debug(f"解析极影视 webhook报文出错：{e}")
            return None
        event_type = message.get('Event')
        if not event_type:
            return None
        logger.debug(f"接收到极影视 webhook：{message}")
        event_item = schemas.WebhookEventInfo(event=event_type, channel="zspace")
        if message.get('Item'):
            event_item.media_type = message.get('Item', {}).get('Type')
            if message.get('Item', {}).get('Type') == 'Episode' \
                    or message.get('Item', {}).get('Type') == 'Series' \
                    or message.get('Item', {}).get('Type') == 'Season':
                event_item.item_type = "TV"
                if message.get('Item', {}).get('SeriesName') \
                        and message.get('Item', {}).get('ParentIndexNumber') \
                        and message.get('Item', {}).get('IndexNumber'):
                    event_item.item_name = "%s %s%s %s" % (
                        message.get('Item', {}).get('SeriesName'),
                        "S" + str(message.get('Item', {}).get('ParentIndexNumber')),
                        "E" + str(message.get('Item', {}).get('IndexNumber')),
                        message.get('Item', {}).get('Name'))
                elif message.get('Item', {}).get('SeriesName'):
                    event_item.item_name = "%s %s" % (
                        message.get('Item', {}).get('SeriesName'),
                        message.get('Item', {}).get('Name'))
                else:
                    event_item.item_name = message.get('Item', {}).get('Name')
                event_item.item_id = message.get('Item', {}).get('SeriesId')
                event_item.season_id = message.get('Item', {}).get('ParentIndexNumber')
                event_item.episode_id = message.get('Item', {}).get('IndexNumber')
            elif message.get('Item', {}).get('Type') == 'Audio':
                event_item.item_type = "AUD"
                album = message.get('Item', {}).get('Album')
                file_name = message.get('Item', {}).get('FileName')
                event_item.item_name = album
                event_item.overview = file_name
                event_item.item_id = message.get('Item', {}).get('AlbumId')
            else:
                event_item.item_type = "MOV"
                event_item.item_name = "%s %s" % (
                    message.get('Item', {}).get('Name'), "(" + str(message.get('Item', {}).get('ProductionYear')) + ")")
                event_item.item_id = message.get('Item', {}).get('Id')

            event_item.item_path = message.get('Item', {}).get('Path')
            event_item.tmdb_id = message.get('Item', {}).get('ProviderIds', {}).get('Tmdb')
            if message.get('Item', {}).get('Overview') and len(message.get('Item', {}).get('Overview')) > 100:
                event_item.overview = str(message.get('Item', {}).get('Overview'))[:100] + "..."
            else:
                event_item.overview = message.get('Item', {}).get('Overview')
            event_item.percentage = message.get('TranscodingInfo', {}).get('CompletionPercentage')
            if not event_item.percentage:
                if message.get('PlaybackInfo', {}).get('PositionTicks') and message.get('Item', {}).get('RunTimeTicks'):
                    event_item.percentage = message.get('PlaybackInfo', {}).get('PositionTicks') / \
                                            message.get('Item', {}).get('RunTimeTicks') * 100
        if message.get('Session'):
            event_item.ip = message.get('Session').get('RemoteEndPoint')
            event_item.device_name = message.get('Session').get('DeviceName')
            event_item.client = message.get('Session').get('Client')
        if message.get("User"):
            event_item.user_name = message.get("User").get('Name')
        if message.get("item_isvirtual"):
            event_item.item_isvirtual = message.get("item_isvirtual")
            event_item.item_type = message.get("item_type")
            event_item.item_name = message.get("item_name")
            event_item.item_path = message.get("item_path")
            event_item.tmdb_id = message.get("tmdb_id")
            event_item.season_id = message.get("season_id")
            event_item.episode_id = message.get("episode_id")

        if event_item.item_id:
            event_item.image_url = self.get_remote_image_by_id(item_id=event_item.item_id,
                                                               image_type="Backdrop")

        event_item.json_object = message

        return event_item

    def get_data(self, url: str) -> Optional[Response]:
        """
        自定义URL从媒体服务器获取数据，其中[HOST]、[APIKEY]、[USER]会被替换成实际的值。
        极影视这里的 [APIKEY] 实际替换为登录返回的 AccessToken，以兼容现有占位符。
        :param url: 请求地址
        """
        if not self._host or not self._apikey:
            return None
        url = url.replace("[HOST]", self._host or '') \
            .replace("[APIKEY]", self._apikey or '') \
            .replace("[USER]", self.user or '')
        try:
            return self.__request_utils(headers={"Content-Type": "application/json"}).get_res(url=url)
        except Exception as e:
            logger.error(f"连接极影视出错：{e}")
            return None

    def post_data(self, url: str, data: Optional[str] = None, headers: dict = None) -> Optional[Response]:
        """
        自定义URL从媒体服务器获取数据，其中[HOST]、[APIKEY]、[USER]会被替换成实际的值。
        极影视这里的 [APIKEY] 实际替换为登录返回的 AccessToken，以兼容现有占位符。
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
            return self.__request_utils(headers=headers).post_res(url=url, data=data)
        except Exception as e:
            logger.error(f"连接极影视出错：{e}")
            return None

    def get_play_url(self, item_id: str) -> str:
        """
        拼装媒体播放链接
        :param item_id: 媒体的ID
        """
        return f"{self._playhost or self._host}web/index.html#!" \
               f"/item?id={item_id}&context=home&serverId={self.serverid}"

    def get_backdrop_url(self, item_id: str, image_tag: str, remote: Optional[bool] = False) -> str:
        """
        获取极影视的Backdrop图片地址
        :param item_id: 在极影视中的ID
        :param image_tag: 图片的tag
        :param remote: 是否远程使用，TG微信等客户端调用应为True
        """
        if not self._host or not self._apikey:
            return ""
        if not image_tag or not item_id:
            return ""
        if remote:
            host_url = self._playhost or self._host
        else:
            host_url = self._host
        return f"{host_url}emby/Items/{item_id}/" \
               f"Images/Backdrop?tag={image_tag}&api_key={self._apikey}"

    def __get_local_image_by_id(self, item_id: str) -> str:
        """
        根据ItemId从媒体服务器查询本地图片地址
        :param item_id: 在极影视中的ID
        """
        if not self._host or not self._apikey:
            return ""
        return f"{self._host}emby/Items/{item_id}/Images/Primary?api_key={self._apikey}"

    def get_resume(self, num: Optional[int] = 12, username: Optional[str] = None) -> Optional[
        List[schemas.MediaServerPlayItem]]:
        """
        获得继续观看
        """
        if not self._host or not self._apikey:
            return None
        if username:
            user = self.get_user(username) or self.user
        else:
            user = self.user
        if not user:
            return []
        url = f"{self._host}emby/Users/{user}/Items/Resume"
        params = {
            "Limit": 100,
            "MediaTypes": "Video",
            "Fields": "ProductionYear,Path"
        }
        try:
            res = self.__request_utils().get_res(url, params=params)
            if res:
                result = res.json().get("Items") or []
                ret_resume = []
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
                        subtitle = str(item.get("ProductionYear")) if item.get("ProductionYear") else None
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
                        percent=item.get("UserData", {}).get("PlayedPercentage"),
                        server_type='zspace'
                    ))
                return ret_resume
            else:
                logger.error("Users/Items/Resume 未获取到返回数据")
        except Exception as e:
            logger.error(f"连接Users/Items/Resume出错：{e}")
        return []

    def get_latest(self, num: Optional[int] = 20, username: Optional[str] = None) -> Optional[
        List[schemas.MediaServerPlayItem]]:
        """
        获得最近更新。

        极影视当前 Emby 兼容层（`System/Info` 返回 ServerVersion=4.7.0.0，
        对齐 Emby Server 4.7 协议）的 `Users/{uid}/Items/Latest` 端点实测
        返回 500（疑似服务端 panic），无法使用。降级为以 `DateCreated` 倒序
        查询 `Users/{uid}/Items`，语义上近似"最近添加"。
        """
        if not self._host or not self._apikey:
            return None
        if username:
            user = self.get_user(username) or self.user
        else:
            user = self.user
        if not user:
            return []
        url = f"{self._host}emby/Users/{user}/Items"
        params = {
            "Recursive": "true",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "IncludeItemTypes": "Movie,Series",
            "Limit": 100,
            "Fields": "ProductionYear,Path,BackdropImageTags"
        }
        try:
            res = self.__request_utils().get_res(url, params=params)
            if res:
                # 兼容两种返回形态：原 Latest 返回裸数组，新接口返回 {Items, TotalRecordCount}
                payload = res.json()
                if isinstance(payload, dict):
                    result = payload.get("Items") or []
                else:
                    result = payload or []
                ret_latest = []
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
                        subtitle=str(item.get("ProductionYear")) if item.get("ProductionYear") else None,
                        type=item_type,
                        image=image,
                        link=link,
                        BackdropImageTags=item.get("BackdropImageTags"),
                        server_type='zspace'
                    ))
                return ret_latest
            else:
                logger.debug("Users/Items?SortBy=DateCreated 未获取到返回数据")
        except Exception as e:
            logger.error(f"连接 Users/Items（DateCreated 排序）出错：{e}")
        return []

    def get_user_library_folders(self):
        """
        获取极影视媒体库文件夹列表（排除黑名单）
        """
        if not self._host or not self._apikey:
            return []
        library_folders = []
        for library in self.get_virtual_folders() or []:
            if self._sync_libraries and library.get("Id") not in self._sync_libraries:
                continue
            library_folders += [folder for folder in library.get("Path")]
        return library_folders

    def __login(self, username: Optional[str], password: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """
        使用用户名密码登录极影视，返回访问令牌和用户ID
        """
        if not self._host or not username or not password:
            return None, None
        url = f"{self._host}emby/Users/AuthenticateByName"
        try:
            res = self.__request_utils(
                include_token=False,
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json'
                }
            ).post_res(
                url=url,
                data=json.dumps({
                    "Username": username,
                    "Pw": password
                })
            )
            if res:
                result = res.json() or {}
                auth_token = result.get("AccessToken")
                user_id = result.get("User", {}).get("Id")
                if auth_token:
                    return auth_token, user_id
            else:
                logger.error("Users/AuthenticateByName 未获取到返回数据")
        except Exception as e:
            logger.error(f"连接Users/AuthenticateByName出错：{e}")
        return None, None

    def __get_current_user(self) -> Optional[dict]:
        """
        获取当前登录用户信息。

        极影视当前 Emby 兼容层（`System/Info` 返回 ServerVersion=4.7.0.0，
        对齐 Emby Server 4.7 协议）将 `Users/{seg}` 中的 {seg} 严格按 mediaUid 校验，
        非 GUID 的 `Users/Me` 会被直接判定为 "invalid mediaUid format" 返回 400，
        因此只走精确的 `Users/{userId}` 路径，避免污染日志且少一次无效请求。
        """
        if not self._host or not self._apikey or not self.user:
            return None
        url = f"{self._host}emby/Users/{self.user}"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                result = res.json()
                if isinstance(result, dict):
                    return result
        except Exception as e:
            logger.error(f"连接 {url} 出错：{e}")
        return None

    def __get_current_user_id(self) -> Optional[str]:
        """
        获取当前登录用户ID
        """
        current_user = self.__get_current_user()
        if current_user:
            current_user_id = current_user.get("Id")
            if current_user_id:
                self.user = current_user_id
                return current_user_id
        return None
