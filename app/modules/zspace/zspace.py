"""
极影视 Emby 兼容客户端，负责会话认证、媒体检索及接口缺失时的回退。
"""

import json
import re
import traceback
from pathlib import Path
from typing import Any, Dict, Generator, List, Mapping, Optional, Tuple, Union

from requests import Response

from app.adapters.network.http import RequestUtils
from app.application.mediaserver import MediaServerIdentityHelper, format_emby_family_item
from app.foundation.url import UrlUtils
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import RefreshMediaItem as _SchemaRefreshMediaItem
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import MediaSource, MediaType

# 全量遍历时每次读取的原始条目数量
DEFAULT_ITEMS_PAGE_SIZE = 100


def _optional_text(value: object) -> Optional[str]:
    """
    读取服务端可空文本字段，不将非法类型隐式转成文本。
    """
    if value is None or isinstance(value, str):
        return value
    raise TypeError("极影视文本字段格式错误")


def _user_id(value: object) -> Optional[Union[str, int]]:
    """
    读取兼容字符串和整数的用户标识，保留空值。
    """
    if value is None or isinstance(value, (str, int)):
        return value
    raise TypeError("极影视用户标识格式错误")


def _library_items(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """
    校验完整枚举的列表字段，非法结构不能代表成功空库。
    """
    items = result.get("Items")
    if not isinstance(items, list):
        raise ConnectionError("极影视媒体库列表响应格式错误")
    return items


class ZSpace:
    """
    通过极影视的 Emby 兼容接口提供媒体检索、登录和播放信息。

    用户名和密码用于获取会话令牌；接口缺失时使用对应的兼容回退。
    """
    _host: Optional[str] = None
    _playhost: Optional[str] = None
    _apikey: Optional[str] = None
    _sync_libraries: List[str] = []
    user: Optional[Union[str, int]] = None
    _username: Optional[str] = None
    _password: Optional[str] = None

    def __init__(self, host: Optional[str] = None, username: Optional[str] = None,
                 password: Optional[str] = None, play_host: Optional[str] = None,
                 sync_libraries: Optional[list[str]] = None, **kwargs: object) -> None:
        """
        保存连接配置并登录，随后缓存媒体库路径和服务器标识。

        地址或账号密码缺失时提前结束初始化；登录失败记录日志，不抛出连接异常。

        :param host: 极影视服务地址
        :param username: 登录账号
        :param password: 登录密码
        :param play_host: 外网播放地址，未提供时部分链接使用服务地址
        :param sync_libraries: 参与同步的库标识，空列表表示全部
        :param kwargs: 通用配置中的额外参数，当前不读取
        """
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
        self.folders: List[dict[str, Any]] = []
        self.serverid: Optional[str] = None
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
                        headers: Optional[dict[str, str]] = None) -> RequestUtils:
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
        检查现有令牌和用户是否需要重新登录。

        令牌与用户标识齐全时查询当前用户，并用返回值更新缓存的用户标识。

        :return: 有完整连接配置且令牌或用户不可用时为 True；配置不完整时为 False
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
        重新登录并更新令牌、用户、媒体库路径和服务器标识。

        :return: 获得令牌和用户标识时为 True；失败时清除缓存的登录与库信息并返回 False
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

    def get_library_folders(self) -> List[dict[str, Any]]:
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
                folders: object = res.json()
                if not isinstance(folders, list) or not all(isinstance(folder, dict) for folder in folders):
                    raise TypeError("极影视媒体库目录响应格式错误")
                return folders
            logger.debug("Library/SelectableMediaFolders 未获取到返回数据，回退到 Users/{uid}/Views")
        except Exception as e:
            logger.debug(f"连接Library/SelectableMediaFolders 出错：{e}，回退到 Users/{{uid}}/Views")
        return self.__get_library_views() or []

    def get_virtual_folders(self) -> List[dict[str, Any]]:
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

    def __get_library_views(self, username: Optional[str] = None) -> Optional[List[dict[str, Any]]]:
        """
        获取极影视媒体库列表。
        """
        if not self._host or not self._apikey:
            return None
        if username:
            user = self.get_user(username)
        else:
            user = self.user
        if not user:
            return None
        url = f"{self._host}emby/Users/{user}/Views"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                items = res.json().get("Items")
                return items if isinstance(items, list) else None
            else:
                logger.error("Users/Views 未获取到返回数据")
                return None
        except Exception as e:
            logger.error(f"连接Users/Views 出错：{e}")
            return None

    def get_librarys(
        self,
        username: Optional[str] = None,
        hidden: Optional[bool] = False,
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        查询可见媒体库并补充统一类型、数量和图片链接。

        :param username: 目标用户名，空值使用当前用户；查找失败按用户接口规则回退
        :param hidden: 是否隐藏未选入同步范围的库，默认不隐藏
        :return: 统一媒体库列表；配置或用户不可用、查询失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        source_libraries = self.__get_library_views(username)
        if source_libraries is None:
            return None
        libraries = []
        for library in source_libraries:
            if hidden and self._sync_libraries and "all" not in self._sync_libraries \
                    and library.get("Id") not in self._sync_libraries:
                continue
            if library.get("CollectionType") == "movies":
                library_type = MediaType.MOVIE.value
            elif library.get("CollectionType") == "tvshows":
                library_type = MediaType.TV.value
            elif library.get("CollectionType") in ("music", "musicvideos", "audio"):
                library_type = MediaType.MUSIC.value
            else:
                library_type = MediaType.UNKNOWN.value
            image = self.__get_local_image_by_id(library.get("Id"))
            libraries.append(
                _SchemaMediaServerLibrary(
                    server="zspace",
                    id=library.get("Id"),
                    name=library.get("Name"),
                    path=library.get("Path"),
                    type=library_type,
                    item_count=self.get_items_count(library.get("Id")),
                    image=image,
                    link=f'{self._playhost or self._host}web/index.html'
                    f'#!/videos?serverId={self.serverid}&parentId={library.get("Id")}',
                    server_type="zspace"
                )
            )
        return libraries

    def get_user(self, user_name: Optional[str] = None) -> Optional[Union[str, int]]:
        """
        按用户名、管理员、首个有标识的用户依次选择可用账号。

        已知极影视兼容版本未实现 Users 列表接口，回退使用当前用户避免依赖该端点。

        :param user_name: 优先匹配的用户名；空值直接尝试管理员和其他用户
        :return: 用户标识；全量用户接口无可用结果时回退当前登录用户，仍不可用时返回 None
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
                                return _user_id(user.get("Id"))
                    for user in users:
                        if user.get("Policy", {}).get("IsAdministrator"):
                            return _user_id(user.get("Id"))
                    for user in users:
                        if user.get("Id"):
                            return _user_id(user.get("Id"))
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
        使用指定账号获取独立访问令牌，不替换当前客户端登录状态。

        :param username: 登录账号
        :param password: 登录密码
        :return: 登录令牌；配置不完整或认证失败时返回 None
        """
        token, _ = self.__login(username, password)
        if token:
            logger.info(f"用户 {username} 极影视认证成功")
        return token

    def get_server_id(self) -> Optional[str]:
        """
        查询极影视服务器标识，用于构造网页链接。

        :return: 服务器标识；未配置、请求失败或响应缺少标识时返回 None
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/System/Info"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                return _optional_text(res.json().get("Id"))
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
                    if not isinstance(count, int):
                        raise TypeError("极影视用户数量格式错误")
                    return count
            else:
                # 极影视未实现该端点会走到这里，降为 debug 避免主日志误报
                logger.debug("Users/Query 未获取到返回数据，可能服务端未实现该端点，回退到登录用户兜底")
        except Exception as e:
            logger.debug(f"连接Users/Query出错：{e}，回退到登录用户兜底")
        return 1 if self.user else 0

    def get_medias_count(self) -> _SchemaStatistic:
        """
        查询媒体数量，聚合接口不可用时改按所选媒体库估算。

        标准 Items/Counts 成功时直接使用全局统计。失败后仅统计同步范围内的库，
        使用库类型或首个条目类型区分电影与电视剧；该回退无法可靠统计分集和音乐。
        极影视兼容接口可能忽略带 ParentId 的类型过滤，因此回退统计使用库的总条数。

        :return: 电影、电视剧、分集和音乐数量；无法获取的数量为 0
        """
        if not self._host or not self._apikey:
            return _SchemaStatistic()
        url = f"{self._host}emby/Items/Counts"
        try:
            res = self.__request_utils().get_res(url)
            if res:
                result = res.json()
                return _SchemaStatistic(
                    movie_count=result.get("MovieCount") or 0,
                    tv_count=result.get("SeriesCount") or 0,
                    episode_count=result.get("EpisodeCount") or 0,
                    music_count=result.get("MusicAlbumCount") or result.get("AlbumCount")
                    or result.get("SongCount") or 0,
                )
            logger.debug("Items/Counts 未获取到返回数据，回退到按媒体库累计 TotalRecordCount")
        except Exception as e:
            logger.debug(f"连接Items/Counts出错：{e}，回退到按媒体库累计 TotalRecordCount")
        return self.__count_medias_by_views()

    def __count_medias_by_views(self) -> _SchemaStatistic:
        """
        按所选媒体库累计电影和电视剧数量，作为聚合接口的回退。

        同步列表为空或包含 all 时统计全部库；优先使用库类型，缺失时采样首条记录。

        :return: 估算统计，分集和音乐保持为 0
        """
        if not self._host or not self._apikey or not self.user:
            return _SchemaStatistic()
        # 统计与 get_librarys 使用相同的同步范围：
        # _sync_libraries 为空或包含 "all" 视为全部库。
        sync_all = (not self._sync_libraries) or ("all" in self._sync_libraries)
        stat = _SchemaStatistic()
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
        查询单个媒体库总数，并通过库类型或首条记录确定分类。

        :param view_id: 媒体库视图标识
        :param collection_type: 服务端库类型，缺失或未知时采样首条记录
        :return: 总条数和 movies 或 tvshows 分类；数量查询失败时为 0 和 None，分类未知时保留总数
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

    def __get_series_id_by_name(self, name: Optional[str], year: Optional[Union[str, int]]) -> Optional[str]:
        """
        在最多 10 条电视剧搜索结果中匹配标题和年份。

        :param name: 必须精确匹配的剧名
        :param year: 可选年份，空值不限制
        :return: 匹配的标识；未配置或捕获异常时返回 None，无匹配或无有效响应时返回空字符串
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
                            return _optional_text(res_item.get('Id'))
        except Exception as e:
            logger.error(f"连接Items出错：{e}")
            return None
        return ""

    def get_movies(self,
                   title: str,
                   year: Optional[Union[str, int]] = None,
                   media_source: Optional[MediaSource] = None,
                   media_id: Optional[str] = None) -> Optional[List[_SchemaMediaServerItem]]:
        """
        在最多 10 条电影搜索结果中筛选标题、年份和来源身份。

        :param title: 必须精确匹配的标题
        :param year: 可选年份，空值不限制
        :param media_source: 目标媒体来源
        :param media_id: 目标来源的原生标识
        :return: 统一电影条目列表；未配置或捕获异常时返回 None，无匹配或无有效响应时为空列表
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
                            if MediaServerIdentityHelper.is_compatible(
                                    mediaserver_item, media_source, media_id
                            ) and \
                                    mediaserver_item.title == title and \
                                    (not year or str(mediaserver_item.year) == str(year)):
                                ret_movies.append(mediaserver_item)
                    return ret_movies
        except Exception as e:
            logger.error(f"连接Items出错：{e}")
            return None
        return []

    def get_music(
            self,
            title: Optional[str] = None,
            artist: Optional[str] = None,
            album: Optional[str] = None,
    ) -> List[_SchemaMediaServerItem]:
        """
        组合歌曲、艺术家和专辑名称搜索最多 20 条音乐记录。

        :param title: 歌曲名称，可省略
        :param artist: 艺术家名称，可省略
        :param album: 专辑名称，可省略
        :return: 转换成功的专辑或歌曲列表；无关键词、未配置或查询失败时为空列表
        """
        if not self._host or not self._apikey:
            return []
        query = " ".join(filter(None, [title, artist, album])).strip()
        if not query:
            return []
        url = f"{self._host}emby/Items"
        params = {
            "IncludeItemTypes": "MusicAlbum,Audio",
            "Fields": "Album,AlbumArtist,AlbumArtists,Artists,ArtistItems,ChildCount,"
                      "ProviderIds,OriginalTitle,ProductionYear,Path,ParentId",
            "SearchTerm": query,
            "Recursive": "true",
            "Limit": 20,
        }
        try:
            res = self.__request_utils().get_res(url, params=params)
            items = res.json().get("Items", []) if res else []
            return [item for item in (self.__format_item_info(item) for item in items) if item]
        except Exception as e:
            logger.debug(f"查询极影视音乐出错：{e}")
            return []

    def get_tv_episodes(self,
                        item_id: Optional[str] = None,
                        title: Optional[str] = None,
                        year: Optional[Union[str, int]] = None,
                        media_source: Optional[MediaSource] = None,
                        media_id: Optional[str] = None,
                        season: Optional[int] = None
                        ) -> Tuple[Optional[str], Optional[Dict[int, List[int]]]]:
        """
        定位电视剧并读取指定季或全部季的已有集号。

        :param item_id: 极影视条目标识
        :param title: 缓存标识失效或未提供时用于回退搜索的剧名
        :param year: 回退搜索使用的可选年份
        :param media_source: 用于检查身份兼容性的媒体来源
        :param media_id: 媒体来源的原生标识
        :param season: 目标季号，None 表示全部季
        :return: 电视剧标识与季集映射；未配置或捕获请求异常时为两项 None，
            未找到、身份冲突或无有效响应时为 None 与空映射
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
        if not MediaServerIdentityHelper.is_compatible(item_info, media_source, media_id):
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
                season_episodes: Dict[int, List[int]] = {}
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
        查询 TMDB 原始图片链接，无匹配时探测外网播放地址的图片。

        :param item_id: 极影视条目标识
        :param image_type: 极影视图片类型，如 Backdrop 或 Primary，按原值匹配
        :return: 图片 URL；未配置、捕获异常或回退探测失败时返回 None
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
                            return _optional_text(image.get("Url"))
            logger.info("Items/RemoteImages 未获取到返回数据，采用本地图片")
            return self.generate_external_image_link(item_id, image_type)
        except Exception as e:
            logger.error(f"连接Items/Id/RemoteImages出错：{e}")
        return None

    def generate_external_image_link(self, item_id: str, image_type: str) -> Optional[str]:
        """
        请求外网播放地址上的图片，返回重定向后的链接。

        :param item_id: 极影视条目标识
        :param image_type: 图片类型，如 Backdrop 或 Primary
        :return: 最终图片 URL；未配置外网地址、图片不存在或请求失败时返回 None
        """
        if not self._playhost:
            logger.error("极影视外网播放地址未能获取或为空")
            return None

        url = f"{self._playhost}emby/Items/{item_id}/Images/{image_type}"
        try:
            res = self.__request_utils().get_res(url)
            if res and res.status_code != 404:
                logger.info(f"影片图片链接:{res.url}")
                return _optional_text(res.url)
            else:
                logger.info(f"Items/Id/Images 未获取到返回数据或无该影片{image_type}图片")
                return None
        except Exception as e:
            logger.error(f"连接Items/Id/Images出错：{e}")
            return None

    def __refresh_library_by_id(self, item_id: str) -> bool:
        """
        提交单个媒体条目或媒体库的递归扫描请求。

        已知极影视兼容版本未实现此端点；保留请求以支持后续实现，失败记录调试日志。

        :param item_id: 极影视条目标识
        :return: 收到成功响应时为 True，否则为 False
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
        提交全部媒体库的扫描请求。

        已知极影视兼容版本未实现 Library/Refresh，此时只记录调试日志并返回失败。

        :return: 收到成功响应时为 True，否则为 False
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

    def refresh_library_by_items(self, items: List[_SchemaRefreshMediaItem]) -> Optional[bool]:
        """
        按媒体条目确定刷新范围，不能定位媒体库时刷新全库。

        同一库的请求会去重；任意条目需要全库刷新时仅提交全库请求。

        :param items: 已识别的媒体及目标路径列表
        :return: 空输入返回 False；选定的扫描请求均成功时返回 True，
            非空输入未产生需扫描标识时也返回 True
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
        success = True
        for library_id in library_ids:
            if library_id != "/":
                refreshed = self.__refresh_library_by_id(library_id)
                success = bool(refreshed) and success
        logger.info("极影视媒体库刷新完成")
        return success

    def __get_library_id_by_item(self, item: _SchemaRefreshMediaItem) -> Optional[str]:
        """
        依据已入库剧集、目标路径和分类目录确定刷新位置。

        :param item: 包含媒体类型、标题、年份、分类和目标路径的刷新请求
        :return: 条目或媒体库标识；无法匹配时返回 / 表示全库，
            类型或路径缺失、电影已经存在时返回 None 表示不需要刷新
        """
        if not item.type or not item.target_path:
            return None
        if item.type == MediaType.TV and item.title and item.year:
            item_id = self.__get_series_id_by_name(item.title, item.year)
            if item_id:
                return item_id
        elif item.type == MediaType.MOVIE and item.title and item.year:
            if self.get_movies(item.title, item.year):
                return None
        item_path = Path(item.target_path)
        for folder in self.folders:
            for subfolder in folder.get("SubFolders") or []:
                try:
                    subfolder_path = Path(subfolder.get("Path"))
                    if item_path.is_relative_to(subfolder_path):
                        return _optional_text(folder.get("Id"))
                except Exception as err:
                    logger.debug(f"匹配子目录出错：{err} - {traceback.format_exc()}")
        if item.category:
            for folder in self.folders:
                for subfolder in folder.get("SubFolders") or []:
                    if subfolder.get("Path") and re.search(
                            r"[/\\]%s" % re.escape(item.category), subfolder.get("Path")
                    ):
                        return _optional_text(folder.get("Id"))
        return "/"

    @staticmethod
    def __format_item_info(item: Mapping[str, object]) -> Optional[_SchemaMediaServerItem]:
        """
        通过统一 Emby 系转换契约生成极影视条目。
        """
        return format_emby_family_item(item, server="zspace")

    def get_iteminfo(self, itemid: str) -> Optional[_SchemaMediaServerItem]:
        """
        查询当前用户可见的单条媒体详情。

        :param itemid: 极影视条目标识
        :return: 统一媒体模型；标识或登录信息缺失、请求或转换失败时返回 None
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

    def get_items_count(self, parent: Optional[Union[str, int]]) -> Optional[int]:
        """
        获取指定媒体库的媒体条目总数。

        极影视当前兼容层会忽略条目类型过滤，因此以递归查询返回的
        TotalRecordCount 作为同步进度的总数。

        :param parent: 媒体库ID
        :return: 媒体条目总数，查询失败时返回None
        """
        if not parent or not self._host or not self._apikey or not self.user:
            return None
        url = f"{self._host}emby/Users/{self.user}/Items"
        try:
            res = self.__request_utils().get_res(
                url,
                params={"ParentId": parent, "Recursive": "true", "Limit": 0},
            )
            if not res or res.status_code != 200:
                return None
            total_count = res.json().get("TotalRecordCount")
            return int(total_count) if total_count is not None else None
        except Exception as e:
            logger.error(f"查询媒体库 {parent} 的媒体总数出错：{e}")
            return None

    def get_items(self, parent: Union[str, int], start_index: Optional[int] = 0,
                  limit: Optional[int] = -1) -> Generator[MediaServerItem | None, None, None]:
        """
        分页遍历媒体库，展开合集并补查缺少身份或路径的条目。

        仅产出电影、电视剧和音乐专辑。合集递归展开，因此产出数量可能超过单页限制。
        未配置或任意分页请求失败会抛出异常，调用方不得清理未读到的缓存。

        :param parent: 媒体库或合集标识
        :param start_index: 零基起始偏移，空值或负数按 0 处理
        :param limit: None 或 -1 按每页 100 条读完整个库；其他值只请求一页，原值传给服务端
        :yields: 统一媒体条目；底层转换未成功时可能为 None
        :raises ConnectionError: 服务未连接、分页失败或列表响应结构错误
        """
        if not parent or not self._host or not self._apikey or not self.user:
            raise ConnectionError("极影视服务尚未连接或媒体库标识为空")
        url = f"{self._host}emby/Users/{self.user}/Items"
        fetch_all = limit is None or limit == -1
        page_size = DEFAULT_ITEMS_PAGE_SIZE if limit is None or limit == -1 else limit
        current_start_index = max(start_index or 0, 0)
        while True:
            params = {
                "ParentId": parent,
                "Recursive": "true",
                "StartIndex": current_start_index,
                "Limit": page_size,
                "Fields": "Album,AlbumArtist,AlbumArtists,Artists,ArtistItems,ChildCount,"
                          "ProviderIds,OriginalTitle,ProductionYear,Path,"
                          "UserDataPlayCount,UserDataLastPlayedDate,ParentId"
            }
            try:
                res = self.__request_utils().get_res(url, params=params)
                if not res or res.status_code != 200:
                    raise ConnectionError("读取极影视媒体库失败")
                result = res.json() or {}
                items = _library_items(result)
                for item in items:
                    if not item:
                        continue
                    item_id = item.get("Id")
                    if item.get("Type") == "BoxSet" and item_id:
                        for sub_item in self.get_items(parent=item_id):
                            if sub_item:
                                yield sub_item
                        continue
                    if item.get("Type") not in ["Movie", "Series", "MusicAlbum"]:
                        continue
                    if item.get("Type") == "MusicAlbum":
                        yield self.__format_item_info(item)
                        continue
                    provider_ids = item.get("ProviderIds") or {}
                    needs_detail = (
                        not provider_ids.get("Tmdb")
                        or not item.get("ProductionYear")
                        or not item.get("Path")
                    )
                    if needs_detail and item_id:
                        detail_item = self.get_iteminfo(item_id)
                        if detail_item:
                            yield detail_item
                            continue
                    yield self.__format_item_info(item)
            except Exception as e:
                logger.error(f"连接Users/Items出错：{e}")
                raise

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

    def get_webhook_message(self, form: Any, args: Mapping[str, object]) -> Optional[_SchemaWebhookEventInfo]:
        """
        解析兼容 Emby 的极影视通知，并补充媒体身份和图片。

        :param form: 包含 JSON 字符串 data 字段的表单，优先于查询参数
        :param args: 没有表单 data 时作为通知内容的查询参数
        :return: 统一事件对象；输入为空、JSON 解析失败或缺少 Event 时返回 None
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
        event_item = _SchemaWebhookEventInfo(event=event_type, channel="zspace")
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
            event_item.media_source, event_item.media_id = MediaServerIdentityHelper.from_provider_ids(
                message.get('Item', {}).get('ProviderIds')
            )
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
            event_item.media_source, event_item.media_id = MediaServerIdentityHelper.from_provider_ids({
                "tmdb_id": message.get("tmdb_id"),
            })
            event_item.season_id = message.get("season_id")
            event_item.episode_id = message.get("episode_id")

        if event_item.item_id:
            event_item.image_url = self.get_remote_image_by_id(item_id=event_item.item_id,
                                                               image_type="Backdrop")

        event_item.json_object = message

        return event_item

    def get_data(self, url: str) -> Optional[Response]:
        """
        替换连接占位符并发送自定义 GET 请求。

        [APIKEY] 替换为登录返回的会话令牌，同时请求头仍携带当前用户令牌。

        :param url: 请求地址，支持 [HOST]、[APIKEY]、[USER] 占位符
        :return: 原始 HTTP 响应；未配置或请求异常时返回 None
        """
        if not self._host or not self._apikey:
            return None
        user = self.user or ''
        if not isinstance(user, str):
            raise TypeError(f"replace() argument 2 must be str, not {type(user).__name__}")
        url = url.replace("[HOST]", self._host or '') \
            .replace("[APIKEY]", self._apikey or '') \
            .replace("[USER]", user)
        try:
            return self.__request_utils(headers={"Content-Type": "application/json"}).get_res(url=url)
        except Exception as e:
            logger.error(f"连接极影视出错：{e}")
            return None

    def post_data(self, url: str, data: Optional[str] = None, headers: Optional[dict[str, str]] = None) -> Optional[Response]:
        """
        替换连接占位符并发送自定义 POST 请求。

        [APIKEY] 替换为登录返回的会话令牌，同时请求头仍携带当前用户令牌。

        :param url: 请求地址，支持 [HOST]、[APIKEY]、[USER] 占位符
        :param data: 请求正文，原样发送
        :param headers: 额外请求头，可覆盖默认头
        :return: 原始 HTTP 响应；未配置或请求异常时返回 None
        """
        if not self._host or not self._apikey:
            return None
        user = self.user or ''
        if not isinstance(user, str):
            raise TypeError(f"replace() argument 2 must be str, not {type(user).__name__}")
        url = url.replace("[HOST]", self._host or '') \
            .replace("[APIKEY]", self._apikey or '') \
            .replace("[USER]", user)
        try:
            return self.__request_utils(headers=headers).post_res(url=url, data=data)
        except Exception as e:
            logger.error(f"连接极影视出错：{e}")
            return None

    def get_play_url(self, item_id: str) -> str:
        """
        构造媒体条目的网页播放链接。

        :param item_id: 极影视条目标识
        :return: 优先使用外网播放地址的链接，不验证配置或条目是否可用
        """
        return f"{self._playhost or self._host}web/index.html#!" \
            f"/item?id={item_id}&context=home&serverId={self.serverid}"

    def get_backdrop_url(self, item_id: str, image_tag: str, remote: Optional[bool] = False) -> str:
        """
        构造带登录令牌的背景图片链接。

        :param item_id: 极影视条目标识
        :param image_tag: 背景图片标签，空值不生成链接
        :param remote: 是否优先使用外网播放地址
        :return: 图片 URL；登录配置、条目标识或图片标签缺失时返回空字符串
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

    def __get_local_image_by_id(self, item_id: object) -> str:
        """
        根据ItemId从媒体服务器查询本地图片地址。

        :param item_id: 在极影视中的ID
        """
        if not self._host or not self._apikey:
            return ""
        return f"{self._host}emby/Items/{item_id}/Images/Primary?api_key={self._apikey}"

    def get_resume(self, num: Optional[int] = 12, username: Optional[str] = None) -> Optional[
            List[_SchemaMediaServerPlayItem]]:
        """
        查询继续观看的电影和分集。

        从服务端读取最多 100 条，在本地按类型和可用的库路径筛选。
        库路径缺失时不执行路径过滤。

        :param num: 最多返回的数量，0 返回空列表，None 或负值不截断已读取结果
        :param username: 目标用户名，未提供或查找失败时使用当前用户
        :return: 统一播放条目列表；登录信息不可用或请求失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        if username:
            user = self.get_user(username) or self.user
        else:
            user = self.user
        if not user:
            return None
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
                ret_resume: List[_SchemaMediaServerPlayItem] = []
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
                    ret_resume.append(_SchemaMediaServerPlayItem(
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
        return None

    def get_latest(self, num: Optional[int] = 20, username: Optional[str] = None) -> Optional[
            List[_SchemaMediaServerPlayItem]]:
        """
        按入库时间降序查询最新电影和电视剧。

        从服务端读取最多 100 条，在本地按类型和可用的库路径筛选。
        库路径缺失时不执行路径过滤。

        :param num: 最多返回的数量，0 返回空列表，None 或负值不截断已读取结果
        :param username: 目标用户名，未提供或查找失败时使用当前用户
        :return: 统一播放条目列表；登录信息不可用或请求失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        if username:
            user = self.get_user(username) or self.user
        else:
            user = self.user
        if not user:
            return None
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
                ret_latest: List[_SchemaMediaServerPlayItem] = []
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
                    ret_latest.append(_SchemaMediaServerPlayItem(
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
        return None

    def get_user_library_folders(self) -> List[str]:
        """
        汇总同步列表中明确选中的媒体库路径。

        同步列表为空时包含全部库；非空时只按库标识逐项匹配，all 在本方法没有特殊含义。

        :return: 路径列表；未配置或接口未提供路径时为空列表
        """
        if not self._host or not self._apikey:
            return []
        library_folders = []
        for library in self.get_virtual_folders() or []:
            if self._sync_libraries and library.get("Id") not in self._sync_libraries:
                continue
            paths = library.get("Path")
            if paths is None:
                raise TypeError("'NoneType' object is not iterable")
            library_folders += [folder for folder in paths]
        return library_folders

    def __login(self, username: Optional[str], password: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """
        使用用户名密码登录极影视，返回访问令牌和用户ID。
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

    def __get_current_user(self) -> Optional[dict[str, Any]]:
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

    def __get_current_user_id(self) -> Optional[Union[str, int]]:
        """
        获取当前登录用户ID。
        """
        current_user = self.__get_current_user()
        if current_user:
            current_user_id = _user_id(current_user.get("Id"))
            if current_user_id:
                self.user = current_user_id
                return current_user_id
        return None
