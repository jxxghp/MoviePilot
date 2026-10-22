"""
Jellyfin 媒体服务器客户端，负责内容查询、图片、刷新及 Webhook 转换。
"""

import json
import posixpath
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from requests import Response

from app.adapters.network.http import RequestUtils
from app.application.mediaserver import MediaServerIdentityHelper, format_emby_family_item
from app.foundation.url import UrlUtils
from app.modules._base.mediaserver import emby_episode_numbers, emby_query_params
from app.runtime.cache import MemoryBackend
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerField, MediaServerItem, MediaServerQueryOptions
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import MediaSource, MediaType


def _optional_text(value: object) -> Optional[str]:
    """
    读取服务端可空文本字段，拒绝非文本值而不隐式转换标识或路径。
    """
    if value is None or isinstance(value, str):
        return value
    raise TypeError("Jellyfin 文本字段格式错误")


def _user_id(value: object) -> Optional[Union[str, int]]:
    """
    读取兼容字符串或整数的服务端用户标识，保留空值。
    """
    if value is None or isinstance(value, (str, int)):
        return value
    raise TypeError("Jellyfin 用户标识格式错误")


class Jellyfin:
    """
    适配 Jellyfin 的媒体库查询、同步和播放接口。
    """

    _host: Optional[str] = None
    _apikey: Optional[str] = None
    _playhost: Optional[str] = None
    _sync_libraries: List[str] = []
    user: Optional[Union[str, int]] = None
    # 删除后无法反查条目；缓存按服务器隔离，并限制容量和存活时间。
    _webhook_paths = MemoryBackend(maxsize=10000, ttl=30 * 24 * 60 * 60)
    _webhook_path_region = "jellyfin.webhook.paths"

    def __init__(self, host: Optional[str] = None, apikey: Optional[str] = None, play_host: Optional[str] = None,
                 sync_libraries: Optional[list[str]] = None, **kwargs: object) -> None:
        """
        保存服务配置，并查询用户、服务器身份及必要的媒体库路径。

        :param host: 服务根地址，缺少时不初始化连接
        :param apikey: 服务 API 密钥，缺少时不初始化连接
        :param play_host: 外网访问地址，用于播放跳转和远程图片链接
        :param sync_libraries: 允许同步的媒体库标识列表；空列表不限制，具体筛选由各查询接口处理
        :param kwargs: 兼容通用服务配置的额外参数，当前不读取
        """
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
        self.user = self.get_user(get_runtime_setting('SUPERUSER'))
        self.serverid = self.get_server_id()
        self._sync_libraries = sync_libraries or []

    def is_inactive(self) -> bool:
        """
        判断完整服务配置是否尚未找到可用的查询用户。

        :return: 地址及密钥均已配置但 user 为空时返回 True；配置缺失时返回 False
        """
        if not self._host or not self._apikey:
            return False
        return True if not self.user else False

    def reconnect(self) -> None:
        """
        重新查询用户并更新服务器标识。
        """
        self.user = self.get_user()
        self.serverid = self.get_server_id()

    def _request(self, headers: Optional[dict[str, str]] = None, **kwargs: Any) -> RequestUtils:
        """
        创建兼容不同 Jellyfin 版本鉴权方式的请求工具。
        """
        request_headers = dict(headers or {})
        if not any(str(name).lower() == "authorization" for name in request_headers):
            request_headers["Authorization"] = f'MediaBrowser Token="{self._apikey}"'
        if kwargs.get("accept_type") and "Accept" not in request_headers:
            request_headers["Accept"] = kwargs["accept_type"]
        if kwargs.get("content_type") and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = kwargs["content_type"]
        return RequestUtils(headers=request_headers, **kwargs)

    def get_jellyfin_folders(self) -> List[dict[str, Any]]:
        """
        读取服务端可选媒体库及其目录信息。

        :return: 服务端返回的目录记录列表；配置不完整或查询失败时返回空列表
        """
        if not self._host or not self._apikey:
            return []
        url = f"{self._host}Library/SelectableMediaFolders"
        params = {
            'api_key': self._apikey
        }
        try:
            res = self._request().get_res(url, params)
            if res:
                folders: object = res.json()
                if not isinstance(folders, list) or not all(isinstance(folder, dict) for folder in folders):
                    raise TypeError("Jellyfin 媒体库目录响应格式错误")
                return folders
            else:
                logger.error("Library/SelectableMediaFolders 未获取到返回数据")
                return []
        except Exception as e:
            logger.error("连接Library/SelectableMediaFolders 出错：" + str(e))
            return []

    def get_jellyfin_virtual_folders(self) -> List[dict[str, Any]]:
        """
        读取虚拟媒体库及其全部目录，优先使用共享路径。

        :return: 包含 Id、Name 和路径列表 Path 的字典列表；查询失败时返回空列表
        """
        if not self._host or not self._apikey:
            return []

        url = f"{self._host}Library/VirtualFolders"
        params = {
            'api_key': self._apikey
        }
        try:
            res = self._request().get_res(url, params)
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
                logger.error("Library/VirtualFolders 未获取到返回数据")
                return []
        except Exception as e:
            logger.error("连接Library/VirtualFolders 出错：" + str(e))
            return []

    def __get_jellyfin_librarys(self, username: Optional[str] = None) -> Optional[List[dict[str, Any]]]:
        """
        获取Jellyfin媒体库的信息。
        """
        if not self._host or not self._apikey:
            return None
        if username:
            user = self.get_user(username)
        else:
            user = self.user
        if not user:
            return None
        # 使用标准库路径拼接结合统一 URL 规整，避免 host 尾部斜杠缺失导致的寻址偏移。
        url = UrlUtils.combine_url(self._host, posixpath.join("Users", str(user), "Views"))
        if not url:
            return None
        params = {"api_key": self._apikey}
        try:
            res = self._request().get_res(url, params)
            if res:
                items = res.json().get("Items")
                return items if isinstance(items, list) else None
            else:
                logger.error("Users/Views 未获取到返回数据")
                return None
        except Exception as e:
            logger.error("连接Users/Views 出错：" + str(e))
            return None

    def get_librarys(
        self,
        username: Optional[str] = None,
        hidden: Optional[bool] = False,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        读取用户可见媒体库，并转换为统一媒体库模型。

        :param username: 用于限定可见内容的用户名，省略时使用当前实例的查询用户
        :param hidden: 是否隐藏未选入同步范围的媒体库；默认返回全部可见库
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 媒体库列表；配置不完整、用户不可用或查询失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        source_libraries = self.__get_jellyfin_librarys(username)
        if source_libraries is None:
            return None
        options = query or MediaServerQueryOptions()
        libraries = []
        for library in source_libraries:
            if (
                hidden
                and self._sync_libraries
                and "all" not in self._sync_libraries
                and library.get("Id") not in self._sync_libraries
            ):
                continue
            if library.get("CollectionType") == "movies":
                library_type = MediaType.MOVIE.value
                link = f"{self._playhost or self._host}web/index.html#!/movies.html?topParentId={library.get('Id')}"
            elif library.get("CollectionType") == "tvshows":
                library_type = MediaType.TV.value
                link = f"{self._playhost or self._host}web/index.html#!/tv.html?topParentId={library.get('Id')}"
            elif library.get("CollectionType") in ("music", "musicvideos"):
                library_type = MediaType.MUSIC.value
                link = f"{self._playhost or self._host}web/index.html#!/music.html?topParentId={library.get('Id')}"
            else:
                library_type = MediaType.UNKNOWN.value
                link = f"{self._playhost or self._host}web/index.html#!/library.html?topParentId={library.get('Id')}"
            image = (
                self.__get_local_image_by_id(library.get("Id")) if options.includes(MediaServerField.IMAGES) else None
            )
            libraries.append(
                _SchemaMediaServerLibrary(
                    server="jellyfin",
                    id=library.get("Id"),
                    name=library.get("Name"),
                    path=library.get("Path") if options.includes(MediaServerField.PATH) else None,
                    type=library_type,
                    item_count=self.get_items_count(
                        library.get("Id"),
                        include_item_types="MusicAlbum" if library_type == MediaType.MUSIC.value else "Movie,Series",
                    )
                    if options.includes(MediaServerField.COUNTS)
                    else None,
                    image=image,
                    link=link if options.includes(MediaServerField.LINK) else None,
                    server_type="jellyfin",
                )
            )
        return libraries

    def get_user_count(self) -> int:
        """
        查询媒体服务器的用户数量。

        :return: 服务端用户数量；配置不完整或请求失败时返回 0
        """
        if not self._host or not self._apikey:
            return 0
        url = f"{self._host}Users"
        params = {
            "api_key": self._apikey
        }
        try:
            res = self._request().get_res(url, params)
            if res:
                return len(res.json())
            else:
                logger.error("Users 未获取到返回数据")
                return 0
        except Exception as e:
            logger.error("连接Users出错：" + str(e))
            return 0

    def get_user(self, user_name: Optional[str] = None) -> Optional[Union[str, int]]:
        """
        选择用于查询可见媒体内容的用户。

        管理员优先选择可访问全部媒体库的账号；否则选择显式可访问媒体库最多的管理员。

        :param user_name: 优先匹配的用户名；未匹配到时改选管理员
        :return: 选中的用户标识；无可用用户或请求失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}Users"
        params = {
            "api_key": self._apikey
        }
        try:
            res = self._request().get_res(url, params)
            if res:
                users = res.json()
                # 先查询是否有与当前用户名称匹配的
                if user_name:
                    for user in users:
                        if user.get("Name") == user_name:
                            return _user_id(user.get("Id"))
                if user_name:
                    logger.warning(
                        f"未找到指定的 Jellyfin 用户账号 {user_name}，"
                        "将回退选择可用的超级管理员账号！"
                    )
                # 查询管理员，优先选择同时具备全库访问能力的账号，再回退到普通管理员。
                total_library_count = len(self.get_jellyfin_folders())
                best_admin_id = None
                best_admin_name = None
                best_admin_library_count = -1
                for user in users:
                    policy = user.get("Policy") or {}
                    if not policy.get("IsAdministrator"):
                        continue
                    if policy.get("EnableAllFolders"):
                        return _user_id(user.get("Id"))
                    else:
                        enabled_folders = policy.get('EnabledFolders') or []
                        current_count = len(enabled_folders)
                        if best_admin_id is None or current_count > best_admin_library_count:
                            best_admin_id = _user_id(user.get("Id"))
                            best_admin_name = user.get("Name")
                            best_admin_library_count = current_count
                if best_admin_id is None:
                    logger.warning("未找到可用的管理员账号，无法获取管理员用户，请检查Jellyfin用户及权限配置！")
                    return None
                logger.warning(
                    "未找到具备全库访问权限的管理员账号，"
                    f"回退使用显式访问范围为 {best_admin_library_count}/{total_library_count} "
                    f"个媒体库的管理员账号 {best_admin_name}！"
                )
                return best_admin_id
            else:
                logger.error("Users 未获取到返回数据")
        except Exception as e:
            logger.error("连接Users出错：" + str(e))
        return None

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """
        向媒体服务器验证指定用户的密码。

        :param username: 待认证的用户名
        :param password: 待认证的密码
        :return: 成功时返回访问令牌；配置不完整或认证失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}Users/authenticatebyname"
        try:
            res = self._request(headers={
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
                    return _optional_text(auth_token)
            else:
                logger.error("Users/AuthenticateByName 未获取到返回数据")
        except Exception as e:
            logger.error("连接Users/AuthenticateByName出错：" + str(e))
        return None

    def get_server_id(self) -> Optional[str]:
        """
        查询媒体服务器的稳定标识。

        :return: 服务器标识；配置不完整或查询失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}System/Info"
        params = {
            'api_key': self._apikey
        }
        try:
            res = self._request().get_res(url, params)
            if res:
                return _optional_text(res.json().get("Id"))
            else:
                logger.error("System/Info 未获取到返回数据")
        except Exception as e:
            logger.error("连接System/Info出错：" + str(e))
        return None

    def get_medias_count(self) -> _SchemaStatistic:
        """
        获得电影、电视剧、动漫媒体数量。

        优先遍历用户媒体库视图逐库统计：全局 `Items/Counts` 按数据库原始条目
        计数，同一影片在库内有多个版本/多个文件夹拷贝时会重复累计（#5915），
        而用户级 `Users/{user}/Items` 查询会折叠版本，与 Jellyfin 页面显示一致。
        仅在用户视图不可用或没有可识别媒体库时回退到 `Items/Counts`。

        :return: 电影、剧集、单集及音乐数量统计；未配置服务或查询失败时返回空统计
        """
        if not self._host or not self._apikey:
            return _SchemaStatistic()
        stat = self.__count_medias_by_librarys()
        if stat is not None:
            return stat
        url = f"{self._host}Items/Counts"
        params = {
            'api_key': self._apikey
        }
        try:
            res = self._request().get_res(url, params)
            if res:
                result = res.json()
                return _SchemaStatistic(
                    movie_count=result.get("MovieCount") or 0,
                    tv_count=result.get("SeriesCount") or 0,
                    episode_count=result.get("EpisodeCount") or 0,
                    music_count=result.get("MusicAlbumCount") or result.get("AlbumCount")
                    or result.get("SongCount") or 0,
                )
            else:
                logger.error("Items/Counts 未获取到返回数据")
                return _SchemaStatistic()
        except Exception as e:
            logger.error("连接Items/Counts出错：" + str(e))
        return _SchemaStatistic()

    def __count_medias_by_librarys(self) -> Optional[_SchemaStatistic]:
        """
        遍历用户媒体库视图逐库统计媒体数量。

        `Users/{user}/Views` 每个媒体库仅返回一条记录（库包含多个文件夹时
        也不会重复），按 `CollectionType` 分桶后用用户级条目查询累计。

        :return: 统计结果；用户、媒体库视图不可用或没有可识别媒体库时返回None（由调用方回退）
        """
        if not self.user:
            return None
        librarys = self.__get_jellyfin_librarys()
        if not librarys:
            return None
        movie_count = tv_count = episode_count = music_count = 0
        has_known_library = False
        for library in librarys:
            library_id = library.get("Id")
            if not library_id:
                continue
            collection_type = library.get("CollectionType")
            if collection_type == "movies":
                has_known_library = True
                movie_count += self.get_items_count(library_id, include_item_types="Movie") or 0
            elif collection_type == "tvshows":
                has_known_library = True
                tv_count += self.get_items_count(library_id, include_item_types="Series") or 0
                episode_count += self.get_items_count(library_id, include_item_types="Episode") or 0
            elif collection_type in ("music", "musicvideos"):
                has_known_library = True
                music_count += self.get_items_count(library_id, include_item_types="MusicAlbum") or 0
        return _SchemaStatistic(
            movie_count=movie_count, tv_count=tv_count,
            episode_count=episode_count, music_count=music_count,
        ) if has_known_library else None

    def __get_jellyfin_series_id_by_name(
        self, name: Optional[str], year: Optional[str], *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[str]:
        """
        根据名称查询Jellyfin中剧集的SeriesId。

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        if not self._host or not self._apikey or not self.user:
            return None
        url = f"{self._host}Users/{self.user}/Items"
        params = {
            "IncludeItemTypes": "Series",
            "Recursive": "true",
            "searchTerm": name,
            "Limit": 10,
            "api_key": self._apikey,
        }
        try:
            params.update(emby_query_params(query))
            res = self._request().get_res(url, params)
            if res:
                res_items = res.json().get("Items")
                if res_items:
                    for res_item in res_items:
                        if res_item.get("Name") == name and (
                            not year or str(res_item.get("ProductionYear")) == str(year)
                        ):
                            return _optional_text(res_item.get("Id"))
        except Exception as e:
            logger.error("连接Items出错：" + str(e))
            return None
        return ""

    def get_movies(
        self,
        title: str,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerItem]]:
        """
        搜索电影并核对标题、年份和来源身份。

        只检查服务端搜索返回的前 10 个候选。查询时展开合集成员，避免合集折叠隐藏已有电影。

        :param title: 必须精确匹配的电影标题
        :param year: 可选年份，空值表示不限年份
        :param media_source: 可选媒体来源，用于排除来源身份冲突
        :param media_id: 来源内的媒体标识，与 media_source 配合使用
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 匹配的统一电影条目；候选均被过滤时可能为空列表，服务端无候选或查询失败时返回 None
        """
        if not self._host or not self._apikey or not self.user:
            return None
        url = f"{self._host}Users/{self.user}/Items"
        params = {
            "IncludeItemTypes": "Movie",
            "Fields": "ProviderIds,OriginalTitle,ProductionYear,Path,UserDataPlayCount,UserDataLastPlayedDate,ParentId",
            "StartIndex": 0,
            "Recursive": "true",
            # 合集折叠会隐藏成员电影，影响已入库判断。
            "CollapseBoxSetItems": "false",
            "searchTerm": title,
            "Limit": 10,
            "api_key": self._apikey,
        }
        try:
            params.update(
                emby_query_params(query.with_fields(MediaServerField.IDENTITY) if query is not None else None)
            )
            res = self._request().get_res(url, params)
            if res:
                res_items = res.json().get("Items")
                if res_items:
                    ret_movies = []
                    for item in res_items:
                        if not item:
                            continue
                        mediaserver_item = self.__format_item_info(item)
                        if mediaserver_item:
                            if (
                                MediaServerIdentityHelper.is_compatible(mediaserver_item, media_source, media_id)
                                and mediaserver_item.title == title
                                and (not year or str(mediaserver_item.year) == str(year))
                            ):
                                ret_movies.append(mediaserver_item)
                    return ret_movies
        except Exception as e:
            logger.error("连接Items出错：" + str(e))
            return None
        return None

    def get_music(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> List[_SchemaMediaServerItem]:
        """
        按歌曲、艺术家和专辑关键词搜索音乐候选。

        此方法负责候选搜索与统一模型转换，不执行歌曲或专辑名称的精确匹配。

        :param title: 可选歌曲标题
        :param artist: 可选艺术家名称
        :param album: 可选专辑名称
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 最多 20 个专辑或歌曲候选；查询词为空、配置不完整或请求失败时返回空列表
        """
        if not self._host or not self._apikey or not self.user:
            return []
        search_term = " ".join(filter(None, [title, artist, album])).strip()
        if not search_term:
            return []
        url = f"{self._host}Users/{self.user}/Items"
        params = {
            "IncludeItemTypes": "MusicAlbum,Audio",
            "Fields": "Album,AlbumArtist,AlbumArtists,Artists,ArtistItems,ChildCount,"
            "ProviderIds,OriginalTitle,ProductionYear,Path,ParentId",
            "searchTerm": search_term,
            "Recursive": "true",
            "Limit": 20,
            "api_key": self._apikey,
        }
        try:
            params.update(emby_query_params(query))
            res = self._request().get_res(url, params)
            items = res.json().get("Items", []) if res and res.status_code == 200 else []
            return [item for item in (self.__format_item_info(item) for item in items) if item]
        except Exception as e:
            logger.debug(f"查询Jellyfin音乐出错：{e}")
            return []

    def get_tv_episodes(
        self,
        item_id: Optional[str] = None,
        title: Optional[str] = None,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Tuple[Optional[str], Optional[Dict[int, list[int]]]]:
        """
        查询电视剧身份及各季已经存在的集号。

        已有缓存标识失效时按标题及年份重新查找；仅统计服务端实际存在且具有季号、集号的条目。
        合并文件按 IndexNumber 至 IndexNumberEnd 的闭区间计入全部集数。

        :param item_id: Jellyfin 条目标识
        :param title: 缺少条目标识或缓存标识失效时用于回退搜索的剧名
        :param year: 回退搜索时限定的年份，空值表示不限
        :param media_source: 可选媒体来源，用于排除身份冲突
        :param media_id: 对应来源的媒体标识
        :param season: 目标季号；None 表示查询全部季
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 电视剧条目标识与“季号到集号列表”映射；配置缺失或异常返回两项 None，
            未找到、身份不兼容或响应无可用数据时返回 None 和空映射
        """
        if not self._host or not self._apikey or not self.user:
            return None, None
        cached_item_id = item_id
        if not item_id:
            item_id = self.__get_jellyfin_series_id_by_name(
                title, year, **MediaServerQueryOptions.as_kwargs(query)
            )
            if item_id is None:
                return None, None
            if not item_id:
                return None, {}
        # 校验媒体服务器返回的主身份是否与目标冲突
        item_info = self.get_iteminfo(
            item_id,
            **({"query": query.with_fields(MediaServerField.IDENTITY)} if query is not None else {}),
        )
        if not item_info and cached_item_id and title:
            # 媒体删除后重新入库会导致缓存ID失效，回退到标题搜索避免误判整部剧缺失。
            logger.warning(f"Jellyfin缓存的电视剧媒体ID {cached_item_id} 已失效，尝试按标题重新搜索：{title}")
            item_id = self.__get_jellyfin_series_id_by_name(
                title, year, **MediaServerQueryOptions.as_kwargs(query)
            )
            if item_id is None:
                return None, None
            if not item_id:
                return None, {}
            item_info = self.get_iteminfo(
                item_id,
                **({"query": query.with_fields(MediaServerField.IDENTITY)} if query is not None else {}),
            )
        if not item_info:
            return None, {}
        if not MediaServerIdentityHelper.is_compatible(item_info, media_source, media_id):
            return None, {}
        if season is None:
            season = None
        url = f"{self._host}Shows/{item_id}/Episodes"
        params = {"season": season, "userId": self.user, "isMissing": "false", "api_key": self._apikey}
        try:
            params.update(
                emby_query_params(
                    MediaServerQueryOptions(fields=frozenset()) if query is not None and query.fields is not None else None
                )
            )
            res_json = self._request().get_res(url, params)
            if res_json:
                tv_info = res_json.json()
                res_items = tv_info.get("Items")
                season_episodes: dict[int, list[int]] = {}
                for res_item in res_items:
                    season_index = res_item.get("ParentIndexNumber")
                    if season_index is None:
                        continue
                    if season is not None and season != season_index:
                        continue
                    episode_index = res_item.get("IndexNumber")
                    if episode_index is None:
                        continue
                    if not season_episodes.get(season_index):
                        season_episodes[season_index] = []
                    season_episodes[season_index].extend(emby_episode_numbers(res_item))
                return item_id, season_episodes
        except Exception as e:
            logger.error("连接Shows/Id/Episodes出错：" + str(e))
            return None, None
        return None, {}

    def get_season_episode_ids(
        self, item_id: str, season: int, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Dict[int, str]:
        """
        读取指定季的分集条目标识。

        合并文件覆盖的各个集号指向同一个条目标识。

        :param item_id: Jellyfin 条目标识
        :param season: 目标季号
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 集号到条目标识的映射；输入不完整、无分集或查询失败时返回空映射
        """
        if not item_id or not self._host or not self._apikey or not self.user:
            return {}
        try:
            url = f"{self._host}Shows/{item_id}/Episodes"
            params = {"season": season, "userId": self.user, "isMissing": "false", "api_key": self._apikey}
            params.update(
                emby_query_params(
                    MediaServerQueryOptions(fields=frozenset()) if query is not None and query.fields is not None else None
                )
            )
            res_json = self._request().get_res(url, params)
            if not res_json:
                return {}
            episode_ids: Dict[int, str] = {}
            for res_item in res_json.json().get("Items") or []:
                episode_index = res_item.get("IndexNumber")
                episode_id = res_item.get("Id")
                if episode_index is None or not episode_id:
                    continue
                for number in emby_episode_numbers(res_item):
                    episode_ids[number] = str(episode_id)
            return episode_ids
        except Exception as e:
            logger.error(f"获取 Jellyfin 季集条目 ID 出错：{str(e)}")
            return {}

    def get_remote_image_by_id(self, item_id: str, image_type: str) -> Optional[str]:
        """
        优先查询指定类型的 TMDB 图片，必要时回退媒体服务器图片。

        没有匹配图片时尝试外网媒体服务器地址。远程图片查询异常时仍尝试本地图片回退。

        :param item_id: Jellyfin 条目标识
        :param image_type: 服务端图片类型，如 Primary 或 Backdrop，需与响应中的 Type 一致
        :return: 匹配的图片 URL；回退失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}Items/{item_id}/RemoteImages"
        params = {"api_key": self._apikey}
        try:
            res = self._request(timeout=10).get_res(url, params)
            if res:
                images = res.json().get("Images") or []
                for image in images:
                    if image.get("ProviderName") == "TheMovieDb" and image.get("Type") == image_type:
                        return _optional_text(image.get("Url"))
                # TMDB 无匹配时回退本地图片
                logger.info(f"未找到 TMDB {image_type}，回退本地图片")
            else:
                logger.info("Items/RemoteImages 未获取到返回数据，采用本地图片")
        except Exception as e:
            logger.error("连接Items/Id/RemoteImages出错：" + str(e))
        return self.generate_image_link(item_id, image_type, True)

    def get_item_path_by_id(self, item_id: str) -> Optional[str]:
        """
        从条目的播放信息读取首个媒体源路径。

        :param item_id: Jellyfin 条目标识
        :return: 媒体路径；无媒体源、配置不完整或查询失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}Items/{item_id}/PlaybackInfo"
        params = {"api_key": self._apikey}
        try:
            res = self._request(timeout=10).get_res(url, params)
            if res:
                media_sources = res.json().get("MediaSources")
                if media_sources:
                    return _optional_text(media_sources[0].get("Path"))
            else:
                logger.error("Items/Id/PlaybackInfo 未获取到返回数据，不设置 Path")
                return None
        except Exception as e:
            logger.error("连接Items/Id/PlaybackInfo出错：" + str(e))
            return None
        return None

    def _get_webhook_item_path(self, message: dict[str, Any]) -> Optional[str]:
        """
        优先读取负载路径；删除事件仅使用已知路径，不反查已删除条目。

        入库及播放等非删除事件刷新缓存，删除时消费对应条目缓存。
        缓存仅在进程内有效；重启、过期或未观察过的条目可能仍无路径。
        """
        item_id = message.get("ItemId")
        path = message.get("Path")
        path = path if isinstance(path, str) and path.strip() else None
        if not item_id:
            return path
        key = f"{self._host}:{item_id}"
        if message.get("NotificationType") == "ItemDeleted":
            cached_path = self._webhook_paths.consume(key, region=self._webhook_path_region)
            path = path or cached_path
            if not path:
                logger.warning(f"Jellyfin 删除条目 {item_id} 的路径未知，不能据此定位文件版本")
            return path
        path = path or self.get_item_path_by_id(item_id)
        if path:
            self._webhook_paths.set(key, path, region=self._webhook_path_region)
        return path

    def generate_image_link(self, item_id: str, image_type: str, host_type: bool) -> Optional[str]:
        """
        探测条目或父剧集的本地图片 URL。

        即使选择内网地址，也要求已经配置外网播放地址。
        若祖先记录提供 ParentBackdropItemId，则改用父条目的图片。

        :param item_id: Jellyfin 条目标识
        :param image_type: 服务端图片类型，如 Backdrop 或 Primary
        :param host_type: True 使用外网播放地址，False 使用内部服务地址
        :return: 图片响应的最终 URL；缺少所需地址或请求失败时返回 None
        """
        if not self._playhost:
            logger.error("Jellyfin外网播放地址未能获取或为空")
            return None
        _parent_id = self.get_itemId_ancestors(item_id, 0, "ParentBackdropItemId")
        image_item_id = _parent_id or item_id

        _host = self._host
        if host_type:
            _host = self._playhost
        url = f"{_host}Items/{image_item_id}/Images/{image_type}"
        try:
            res = self._request().get_res(url)
            if res and res.status_code != 404:
                logger.info(f"影片图片链接:{res.url}")
                return _optional_text(res.url)
            else:
                logger.error("Items/Id/Images 未获取到返回数据或无该影片{}图片".format(image_type))
                return None
        except Exception as e:
            logger.error("连接Items/Id/Images出错：" + str(e))
            return None

    def get_itemId_ancestors(self, item_id: str, index: int, key: str) -> object:
        """
        读取指定祖先记录中的字段。

        :param item_id: Jellyfin 条目标识
        :param index: 服务端祖先列表中的索引
        :param key: 需要读取的祖先字段名称
        :return: 字段原始值；记录不存在、索引无效或请求失败时返回 None
        """
        url = f"{self._host}Items/{item_id}/Ancestors"
        params = {
            "api_key": self._apikey
        }
        try:
            res = self._request().get_res(url, params)
            if res:
                value: object = res.json()[index].get(key)
                return value
            else:
                logger.error("Items/Id/Ancestors 未获取到返回数据")
                return None
        except Exception as e:
            logger.error("连接Items/Id/Ancestors出错：" + str(e))
            return None

    def refresh_root_library(self) -> Optional[bool]:
        """
        请求 Jellyfin 刷新整个媒体库。

        :return: 请求被接受时返回 True；配置缺失或请求异常时返回 False，无有效响应时返回 None；不等待扫描完成
        """
        if not self._host or not self._apikey:
            return False
        url = f"{self._host}Library/Refresh"
        params = {
            "api_key": self._apikey
        }
        try:
            res = self._request().post_res(url, params=params)
            if res:
                return True
            else:
                logger.info("刷新媒体库失败，无法连接Jellyfin！")
        except Exception as e:
            logger.error("连接Library/Refresh出错：" + str(e))
            return False
        return None

    def get_webhook_message(self, body: Any) -> Optional[_SchemaWebhookEventInfo]:
        """
        解析 Jellyfin Webhook，并补全统一事件和媒体路径。

        播放和入库事件可补查媒体路径并缓存。删除事件只读取负载或既有缓存，
        不反查已删除条目，也不请求其图片。

        :param body: 包含 NotificationType 等字段的 JSON 请求体
        :return: 统一 Webhook 事件；请求体为空、JSON 无法解析或缺少事件类型时返回 None
        """
        if not body:
            return None
        try:
            message = json.loads(body)
        except Exception as e:
            logger.debug("解析Jellyfin Webhook报文出错：" + str(e))
            return None
        if not message:
            return None
        logger.debug(f"接收到jellyfin webhook：{message}")
        eventType = message.get('NotificationType')
        if not eventType:
            return None
        eventItem = _SchemaWebhookEventInfo(
            event=eventType,
            channel="jellyfin"
        )
        eventItem.item_id = message.get('ItemId')
        eventItem.media_source, eventItem.media_id = MediaServerIdentityHelper.from_provider_ids({
            key.removeprefix("Provider_"): value
            for key, value in message.items()
            if key.startswith("Provider_")
        })
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
            eventItem.item_type = "TV"
            eventItem.season_id = message.get('SeasonNumber')
            eventItem.episode_id = message.get('EpisodeNumber')
            eventItem.item_name = "%s %s%s %s" % (
                message.get('SeriesName'),
                "S" + str(eventItem.season_id),
                "E" + str(eventItem.episode_id),
                message.get('Name'))
        elif message.get("ItemType") == 'Audio':
            eventItem.item_type = "AUD"
            eventItem.item_name = message.get('Album')
            eventItem.overview = message.get('Name')
            eventItem.item_id = message.get('ItemId')
        else:
            eventItem.item_type = "MOV"
            eventItem.item_name = "%s %s" % (
                message.get('Name'), "(" + str(message.get('Year')) + ")")

        playback_position_ticks = message.get('PlaybackPositionTicks')
        runtime_ticks = message.get('RunTimeTicks')
        if playback_position_ticks is not None and runtime_ticks:
            eventItem.percentage = playback_position_ticks / runtime_ticks * 100

        if eventItem.item_id and eventType != "ItemDeleted":
            eventItem.image_url = self.get_remote_image_by_id(
                item_id=eventItem.item_id,
                image_type="Backdrop"
            )
        eventItem.item_path = self._get_webhook_item_path(message)

        eventItem.json_object = message

        return eventItem

    @staticmethod
    def __format_item_info(item: dict[str, Any]) -> Optional[_SchemaMediaServerItem]:
        """
        通过统一 Emby 系转换契约生成 Jellyfin 条目。
        """
        return format_emby_family_item(item, server="jellyfin")

    def get_iteminfo(
        self, itemid: str, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[_SchemaMediaServerItem]:
        """
        查询单个条目并转换为统一媒体模型。

        :param itemid: Jellyfin 条目标识
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 条目详情；标识为空、配置不完整、查询失败或类型无法转换时返回 None
        """
        if not itemid:
            return None
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}Users/{self.user}/Items/{itemid}"
        params: dict[str, Any] = {"api_key": self._apikey}
        projected = query is not None and query.fields is not None
        if projected:
            url = url.rsplit("/", 1)[0]
            params.update({"Ids": itemid, "Limit": 1, **emby_query_params(query)})
        try:
            res = self._request().get_res(url, params)
            if res and res.status_code == 200:
                payload = res.json()
                if projected:
                    payload = next(iter(payload.get("Items") or []), None)
                if payload is None:
                    return None
                return self.__format_item_info(payload)
        except Exception as e:
            logger.error(f"连接Users/{self.user}/Items/{itemid}：" + str(e))
        return None

    def get_items_count(self, parent: Optional[Union[str, int]],
                        include_item_types: str = "Movie,Series") -> Optional[int]:
        """
        递归统计指定媒体库内的可同步条目。

        :param parent: 媒体库标识
        :param include_item_types: 逗号分隔的服务端条目类型，默认 Movie,Series
        :return: 条目总数；输入或配置不完整、响应缺少计数或查询失败时返回 None
        """
        if not parent or not self._host or not self._apikey or not self.user:
            return None
        url = f"{self._host}Users/{self.user}/Items"
        params = {
            "ParentId": parent,
            "Recursive": "true",
            "CollapseBoxSetItems": "false",
            "IncludeItemTypes": include_item_types,
            "Limit": 0,
            "api_key": self._apikey,
        }
        try:
            res = self._request().get_res(url, params)
            if not res or res.status_code != 200:
                return None
            total_count = res.json().get("TotalRecordCount")
            return int(total_count) if total_count is not None else None
        except Exception as e:
            logger.error(f"查询媒体库 {parent} 的媒体总数出错：{str(e)}")
            return None

    def get_items(
        self,
        parent: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Generator[Optional[MediaServerItem], None, None]:
        """
        读取媒体库条目并递归展开目录。

        读取失败时向上抛出异常，防止同步调用方将失败误判为媒体库清空。
        Folder 和 BoxSet 都展开为成员，容器本身不作为媒体返回。

        :param parent: 媒体库标识，不接受空值
        :param start_index: 分页起始索引；不限制数量时不发送此参数
        :param limit: 本层请求数量；None 或 -1 不限制，递归子目录始终不分页
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :yields: 转换后的统一媒体条目
        :raises ValueError: 媒体库标识为空
        :raises ConnectionError: 配置不完整或读取媒体库失败
        """
        if not parent:
            raise ValueError("Jellyfin 媒体库 ID 不能为空")
        if not self._host or not self._apikey:
            raise ConnectionError("Jellyfin 服务器未连接")
        url = f"{self._host}Users/{self.user}/Items"
        params: dict[str, Union[str, int, None]] = {
            "ParentId": parent,
            "api_key": self._apikey,
            "Fields": "Album,AlbumArtist,AlbumArtists,Artists,ArtistItems,ChildCount,"
            "ProviderIds,OriginalTitle,ProductionYear,Path,UserDataPlayCount,"
            "UserDataLastPlayedDate,ParentId",
        }
        if limit is not None and limit != -1:
            params.update({"StartIndex": start_index, "Limit": limit})
        try:
            params.update(emby_query_params(query))
            res = self._request().get_res(url, params)
            if not res or res.status_code != 200:
                raise ConnectionError(
                    f"读取 Jellyfin 媒体库 {parent} 失败，状态码：{res.status_code if res is not None else '无响应'}"
                )
            items = res.json().get("Items") or []
            for item in items:
                if not item:
                    continue
                if "Folder" in item.get("Type") or item.get("Type") == "BoxSet":
                    for items in self.get_items(item.get("Id"), **MediaServerQueryOptions.as_kwargs(query)):
                        yield items
                elif item.get("Type") in ["Movie", "Series", "MusicAlbum"]:
                    yield self.__format_item_info(item)
        except Exception as e:
            logger.error(f"同步读取 Jellyfin 媒体库 {parent} 出错：{str(e)}")
            raise

    def get_data(self, url: str) -> Optional[Response]:
        """
        替换服务配置占位符后发送自定义 GET 请求。

        :param url: 请求 URL，其中 [HOST]、[APIKEY]、[USER] 替换为当前服务配置
        :return: 原始 HTTP 响应，由调用方读取和关闭；配置不完整或请求异常时返回 None
        """
        if not self._host or not self._apikey:
            return None
        url = url.replace("[HOST]", self._host or '') \
            .replace("[APIKEY]", self._apikey or '') \
            .replace("[USER]", _optional_text(self.user or '') or '')
        try:
            return self._request(accept_type="application/json").get_res(url=url)
        except Exception as e:
            logger.error("连接Jellyfin出错：" + str(e))
            return None

    def post_data(self, url: str, data: Optional[str] = None, headers: Optional[dict[str, str]] = None) -> Optional[Response]:
        """
        替换服务配置占位符后发送自定义 POST 请求。

        :param url: 请求 URL，其中 [HOST]、[APIKEY]、[USER] 替换为当前服务配置
        :param data: 发送的请求数据，原样传给请求工具
        :param headers: 可选请求头
        :return: 原始 HTTP 响应，由调用方读取和关闭；配置不完整或请求异常时返回 None
        """
        if not self._host or not self._apikey:
            return None
        url = url.replace("[HOST]", self._host or '') \
            .replace("[APIKEY]", self._apikey or '') \
            .replace("[USER]", _optional_text(self.user or '') or '')
        try:
            return self._request(
                headers=headers
            ).post_res(url=url, data=data)
        except Exception as e:
            logger.error("连接Jellyfin出错：" + str(e))
            return None

    def get_play_url(self, item_id: str) -> str:
        """
        构造媒体服务器网页中的条目播放链接。

        :param item_id: Jellyfin 条目标识
        :return: 优先使用外网播放地址的网页链接；只构造链接，不验证条目存在
        """
        return f"{self._playhost or self._host}web/index.html#!" \
            f"/details?id={item_id}&serverId={self.serverid}"

    def __get_local_image_by_id(self, item_id: Optional[Union[str, int]]) -> str:
        """
        构造服务内网的主封面图片地址。

        :param item_id: Jellyfin 条目标识
        :return: 主封面地址；服务配置不完整时返回空字符串
        """
        if not self._host or not self._apikey:
            return ""
        return "%sItems/%s/Images/Primary" % (self._host, item_id)

    def get_backdrop_url(self, item_id: str, image_tag: str, remote: Optional[bool] = False) -> str:
        """
        构造带 API 密钥和版本标签的背景图片地址。

        :param item_id: Jellyfin 条目标识
        :param image_tag: 图片版本标签，用于区分封面更新前后的缓存
        :param remote: 是否优先使用外网播放地址；False 使用服务地址
        :return: 图片 URL；配置、条目标识或图片标签缺失时返回空字符串
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

    def get_resume(
        self,
        num: Optional[int] = 12,
        username: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询用户的继续观看列表并按可同步媒体库路径筛选。

        :param num: 最多保留的条目数量，默认 12
        :param username: 用于限定可见内容的用户名，省略时使用当前实例的查询用户
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 电影或电视剧播放条目列表；配置不完整、用户不可用或请求失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        if username:
            user = self.get_user(username)
        else:
            user = self.user
        if not user:
            return None

        url = f"{self._host}Users/{user}/Items/Resume"
        params = {
            "Limit": 100,
            "MediaTypes": "Video",
            "Fields": "ProductionYear,Path",
            "api_key": self._apikey,
        }
        options = query or MediaServerQueryOptions()
        try:
            params.update(emby_query_params(query.with_fields(MediaServerField.PATH) if query is not None else None))
            res = self._request().get_res(url, params)
            if res:
                result = res.json().get("Items") or []
                ret_resume: list[_SchemaMediaServerPlayItem] = []
                # 用户媒体库文件夹列表（排除黑名单）
                library_folders = self.get_user_library_folders()
                for item in result:
                    if len(ret_resume) == num:
                        break
                    if item.get("Type") not in ["Movie", "Episode"]:
                        continue
                    item_path = item.get("Path")
                    if (
                        item_path
                        and library_folders
                        and not any(str(item_path).startswith(folder) for folder in library_folders)
                    ):
                        continue
                    item_type = MediaType.MOVIE.value if item.get("Type") == "Movie" else MediaType.TV.value
                    link = self.get_play_url(item.get("Id")) if options.includes(MediaServerField.LINK) else None
                    image = self._get_resume_image(item, query)
                    if item_type == MediaType.MOVIE.value:
                        title = item.get("Name")
                        subtitle = str(item.get("ProductionYear")) if item.get("ProductionYear") else None
                    else:
                        title = f"{item.get('SeriesName')}"
                        subtitle = f"S{item.get('ParentIndexNumber')}:{item.get('IndexNumber')} - {item.get('Name')}"
                    ret_resume.append(
                        _SchemaMediaServerPlayItem(
                            id=item.get("Id"),
                            title=title,
                            subtitle=subtitle,
                            type=item_type,
                            image=image,
                            link=link,
                            percent=item.get("UserData", {}).get("PlayedPercentage"),
                            server_type="jellyfin",
                        )
                    )
                return ret_resume
            else:
                logger.error("Users/Items/Resume 未获取到返回数据")
        except Exception as e:
            logger.error("连接Users/Items/Resume出错：" + str(e))
        return None

    def _get_resume_image(
        self,
        item: dict[str, Any],
        query: Optional[MediaServerQueryOptions],
    ) -> Optional[str]:
        """
        仅在请求图片时运行原有继续观看封面选择与探测逻辑。

        :param item: 继续观看接口返回的原始条目
        :param query: 可选内容投影；未指定时保留原有图片行为
        :return: 可用图片链接；调用方不需要图片时返回 None 且不探测图片
        """
        if query is not None and not query.includes(MediaServerField.IMAGES):
            return None
        image: Optional[str]
        if item.get("BackdropImageTags"):
            image = self.get_backdrop_url(item_id=item["Id"], image_tag=(item.get("BackdropImageTags") or [])[0])
        else:
            image = self.__get_local_image_by_id(item.get("Id"))
        # 小部分剧集无[xxx-S01E01-thumb.jpg]图片
        image_res = self._request().get_res(image)
        if not image_res or image_res.status_code == 404:
            image = self.generate_image_link(item["Id"], "Backdrop", False)
        return image

    def get_latest(
        self,
        num: Optional[int] = 20,
        username: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询用户最近入库的媒体条目。

        :param num: 最多保留的条目数量，默认 20；先查询 100 条再按库路径筛选
        :param username: 用于限定可见内容的用户名，省略时使用当前实例的查询用户
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 转换后的最新媒体列表；配置不完整、用户不可用或请求失败时返回 None
        """
        if not self._host or not self._apikey:
            return None
        if username:
            user = self.get_user(username)
        else:
            user = self.user
        if not user:
            return None
        url = f"{self._host}Users/{user}/Items/Latest"
        params = {
            "Limit": 100,
            "MediaTypes": "Video",
            "Fields": "ProductionYear,Path,BackdropImageTags",
            "api_key": self._apikey,
        }
        options = query or MediaServerQueryOptions()
        try:
            params.update(emby_query_params(query.with_fields(MediaServerField.PATH) if query is not None else None))
            res = self._request().get_res(url, params)
            if res:
                result = res.json() or []
                ret_latest: list[_SchemaMediaServerPlayItem] = []
                # 用户媒体库文件夹列表（排除黑名单）
                library_folders = self.get_user_library_folders()
                for item in result:
                    if len(ret_latest) == num:
                        break
                    if item.get("Type") not in ["Movie", "Series"]:
                        continue
                    item_path = item.get("Path")
                    if (
                        item_path
                        and library_folders
                        and not any(str(item_path).startswith(folder) for folder in library_folders)
                    ):
                        continue
                    item_type = MediaType.MOVIE.value if item.get("Type") == "Movie" else MediaType.TV.value
                    link = self.get_play_url(item.get("Id")) if options.includes(MediaServerField.LINK) else None
                    image = (
                        self.__get_local_image_by_id(item_id=item.get("Id"))
                        if options.includes(MediaServerField.IMAGES)
                        else None
                    )
                    ret_latest.append(
                        _SchemaMediaServerPlayItem(
                            id=item.get("Id"),
                            title=item.get("Name"),
                            subtitle=str(item.get("ProductionYear")) if item.get("ProductionYear") else None,
                            type=item_type,
                            image=image,
                            link=link,
                            BackdropImageTags=item.get("BackdropImageTags")
                            if options.includes(MediaServerField.IMAGES)
                            else None,
                            server_type="jellyfin",
                        )
                    )
                return ret_latest
            else:
                logger.error("Users/Items/Latest 未获取到返回数据")
        except Exception as e:
            logger.error("连接Users/Items/Latest出错：" + str(e))
        return None

    def get_user_library_folders(self) -> list[str]:
        """
        收集允许同步的媒体库目录路径。

        :return: 按同步库配置过滤后的路径列表；没有可用媒体库时返回空列表
        """
        if not self._host or not self._apikey:
            return []
        library_folders = []
        for library in self.get_jellyfin_virtual_folders() or []:
            if self._sync_libraries and library.get("Id") not in self._sync_libraries:
                continue
            paths = library.get("Path")
            if paths is None:
                raise TypeError("Jellyfin 媒体库路径列表为空")
            library_folders += [folder for folder in paths]
        return library_folders
