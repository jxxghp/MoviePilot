"""
Emby 媒体服务器客户端，负责内容查询、图片、刷新及 Webhook 转换。
"""

import json
import re
import traceback
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from requests import Response

from app.adapters.network.http import RequestUtils
from app.application.mediaserver import MediaServerIdentityHelper, format_emby_family_item
from app.foundation.url import UrlUtils
from app.modules._base.mediaserver import emby_episode_numbers, emby_query_params
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerField, MediaServerItem, MediaServerQueryOptions
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import RefreshMediaItem as _SchemaRefreshMediaItem
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import MediaSource, MediaType


def _optional_text(value: object) -> Optional[str]:
    """
    读取服务端可空文本字段，拒绝非文本值而不隐式转换标识或路径。
    """
    if value is None or isinstance(value, str):
        return value
    raise TypeError("Emby 文本字段格式错误")


def _user_id(value: object) -> Optional[Union[str, int]]:
    """
    读取兼容字符串或整数的服务端用户标识，保留空值。
    """
    if value is None or isinstance(value, (str, int)):
        return value
    raise TypeError("Emby 用户标识格式错误")


class Emby:
    """
    Emby媒体服务器客户端。
    """

    _host: Optional[str] = None
    _playhost: Optional[str] = None
    _apikey: Optional[str] = None
    _sync_libraries: List[str] = []
    user: Optional[Union[str, int]] = None
    _username: Optional[str] = None

    def __init__(self, host: Optional[str] = None, apikey: Optional[str] = None, play_host: Optional[str] = None,
                 username: Optional[str] = None, sync_libraries: Optional[list[str]] = None, **kwargs: object) -> None:
        """
        保存服务配置，并查询用户、服务器身份及必要的媒体库路径。

        :param host: 服务根地址，缺少时不初始化连接
        :param apikey: 服务 API 密钥，缺少时不初始化连接
        :param play_host: 外网访问地址，用于播放跳转和远程图片链接
        :param username: 优先使用的查询用户名；省略时先匹配 MoviePilot 管理员用户名
        :param sync_libraries: 允许同步的媒体库标识列表；空列表不限制，具体筛选由各查询接口处理
        :param kwargs: 兼容通用服务配置的额外参数，当前不读取
        """
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
        self._username = username
        self.user = self.get_user(username or get_runtime_setting('SUPERUSER'))
        self.folders = self.get_emby_folders()
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
        重新查询用户并更新媒体库路径缓存。
        """
        self.user = self.get_user()
        self.folders = self.get_emby_folders()

    def get_emby_folders(self) -> List[dict[str, Any]]:
        """
        读取服务端可选媒体库及其目录信息。

        :return: 服务端返回的目录记录列表；配置不完整或查询失败时返回空列表
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
                folders: object = res.json()
                if not isinstance(folders, list) or not all(isinstance(folder, dict) for folder in folders):
                    raise TypeError("Emby 媒体库目录响应格式错误")
                return folders
            else:
                logger.error("Library/SelectableMediaFolders 未获取到返回数据")
                return []
        except Exception as e:
            logger.error("连接Library/SelectableMediaFolders 出错：" + str(e))
            return []

    def get_emby_virtual_folders(self) -> List[dict[str, Any]]:
        """
        读取虚拟媒体库及其全部目录，优先使用共享路径。

        :return: 包含 Id、Name 和路径列表 Path 的字典列表；查询失败时返回空列表
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
                logger.error("Library/VirtualFolders/Query 未获取到返回数据")
                return []
        except Exception as e:
            logger.error("连接Library/VirtualFolders/Query 出错：" + str(e))
            return []

    def __get_emby_librarys(self, username: Optional[str] = None) -> Optional[List[dict[str, Any]]]:
        """
        获取Emby媒体库列表。
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
        params = {"api_key": self._apikey}
        try:
            res = RequestUtils().get_res(url, params)
            if res:
                items = res.json().get("Items")
                return items if isinstance(items, list) else None
            else:
                logger.error("User/Views 未获取到返回数据")
                return None
        except Exception as e:
            logger.error("连接User/Views 出错：" + str(e))
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
        source_libraries = self.__get_emby_librarys(username)
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
            elif library.get("CollectionType") in ("music", "musicvideos", "audio"):
                library_type = MediaType.MUSIC.value
            elif library.get("CollectionType") == "tvshows":
                library_type = MediaType.TV.value
            else:
                library_type = MediaType.UNKNOWN.value
            image = (
                self.__get_local_image_by_id(
                    library.get("Id"),
                    image_tag=(library.get("ImageTags") or {}).get("Primary"),
                )
                if options.includes(MediaServerField.IMAGES)
                else None
            )
            server_id = library.get("ServerId") or self.serverid
            server_query = f"serverId={server_id}&" if server_id else ""
            libraries.append(
                _SchemaMediaServerLibrary(
                    server="emby",
                    id=library.get("Id"),
                    item_id=library.get("Id"),
                    server_id=server_id,
                    name=library.get("Name"),
                    path=library.get("Path") if options.includes(MediaServerField.PATH) else None,
                    type=library_type,
                    item_count=self.get_items_count(
                        library.get("Id"),
                        "MusicAlbum" if library_type == MediaType.MUSIC.value else "Movie,Series",
                    )
                    if options.includes(MediaServerField.COUNTS)
                    else None,
                    image=image,
                    link=(
                        f"{self._playhost or self._host}web/index.html"
                        f"#!/videos?{server_query}parentId={library.get('Id')}"
                    )
                    if options.includes(MediaServerField.LINK)
                    else None,
                    server_type="emby",
                )
            )
        return libraries

    def get_user(self, user_name: Optional[str] = None) -> Optional[Union[str, int]]:
        """
        获取用于查询用户范围数据的用户ID。

        优先匹配指定用户名，其次匹配媒体服务器配置用户名，最后回退管理员。

        :param user_name: 优先匹配的用户名
        :return: 匹配到的用户ID，未找到可用用户时返回None
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
                candidate_usernames = []
                for candidate_username in (user_name, self._username):
                    if candidate_username and candidate_username not in candidate_usernames:
                        candidate_usernames.append(candidate_username)
                for candidate_username in candidate_usernames:
                    for emby_user in users:
                        if emby_user.get("Name") == candidate_username:
                            return _user_id(emby_user.get("Id"))
                for emby_user in users:
                    if emby_user.get("Policy", {}).get("IsAdministrator"):
                        return _user_id(emby_user.get("Id"))
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
        params = {
            'api_key': self._apikey
        }
        for path in ("System/Info", "emby/System/Info"):
            url = f"{self._host}{path}"
            try:
                res = RequestUtils().get_res(url, params)
                if res:
                    result = res.json() or {}
                    server_id = result.get("Id") or result.get("ServerId")
                    if server_id:
                        return _optional_text(server_id)
                else:
                    logger.error(f"{path} 未获取到返回数据")
            except Exception as e:
                logger.error(f"连接{path}出错：" + str(e))
        return None

    def get_user_count(self) -> Optional[int]:
        """
        查询媒体服务器的用户数量。

        :return: 服务端用户数量；配置不完整或请求失败时返回 0
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
                count: object = res.json().get("TotalRecordCount")
                if count is None or isinstance(count, int):
                    return count
                raise TypeError("Emby 用户数量格式错误")
            else:
                logger.error("Users/Query 未获取到返回数据")
                return 0
        except Exception as e:
            logger.error("连接Users/Query出错：" + str(e))
            return 0

    def get_medias_count(self) -> _SchemaStatistic:
        """
        统计电影、剧集、分集和音乐数量。

        :return: 统一统计对象；查询失败时返回空统计，音乐优先采用专辑数，再回退到歌曲数
        """
        if not self._host or not self._apikey:
            return _SchemaStatistic()
        url = f"{self._host}emby/Items/Counts"
        params = {
            'api_key': self._apikey
        }
        try:
            res = RequestUtils().get_res(url, params)
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

    def __get_emby_series_id_by_name(
        self, name: Optional[str], year: Optional[Union[str, int]], *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[str]:
        """
        根据名称查询Emby中剧集的SeriesId。

        :param name: 标题
        :param year: 年份
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
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
            "api_key": self._apikey,
        }
        try:
            params.update(emby_query_params(query))
            res = RequestUtils().get_res(url, params)
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
        year: Optional[Union[str, int]] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Optional[List[_SchemaMediaServerItem]]:
        """
        搜索电影并核对标题、年份和来源身份。

        只检查服务端搜索返回的前 10 个候选。

        :param title: 必须精确匹配的电影标题
        :param year: 可选年份，空值表示不限年份
        :param media_source: 可选媒体来源，用于排除来源身份冲突
        :param media_id: 来源内的媒体标识，与 media_source 配合使用
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 匹配的统一电影条目；候选均被过滤时可能为空列表，服务端无候选或查询失败时返回 None
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
            "api_key": self._apikey,
        }
        try:
            params.update(
                emby_query_params(query.with_fields(MediaServerField.IDENTITY) if query is not None else None)
            )
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
        url = f"{self._host}emby/Users/{self.user}/Items"
        params = {
            "IncludeItemTypes": "MusicAlbum,Audio",
            "Fields": "Album,AlbumArtist,AlbumArtists,Artists,ArtistItems,ChildCount,"
            "ProviderIds,OriginalTitle,ProductionYear,Path,ParentId",
            "SearchTerm": search_term,
            "Recursive": "true",
            "Limit": 20,
            "api_key": self._apikey,
        }
        try:
            params.update(emby_query_params(query))
            res = RequestUtils().get_res(url, params)
            items = res.json().get("Items", []) if res and res.status_code == 200 else []
            return [item for item in (self.__format_item_info(item) for item in items) if item]
        except Exception as e:
            logger.debug(f"查询Emby音乐出错：{e}")
            return []

    def get_tv_episodes(
        self,
        item_id: Optional[str] = None,
        title: Optional[str] = None,
        year: Optional[Union[str, int]] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> Tuple[Optional[str], Optional[Dict[int, List[int]]]]:
        """
        查询电视剧身份及各季已经存在的集号。

        已有缓存标识失效时按标题及年份重新查找；仅统计服务端实际存在且具有季号、集号的条目。
        合并文件按 IndexNumber 至 IndexNumberEnd 的闭区间计入全部集数。

        :param item_id: Emby 条目标识
        :param title: 缺少条目标识或缓存标识失效时用于回退搜索的剧名
        :param year: 回退搜索时限定的年份，空值表示不限
        :param media_source: 可选媒体来源，用于排除身份冲突
        :param media_id: 对应来源的媒体标识
        :param season: 目标季号；None 表示查询全部季
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 电视剧条目标识与“季号到集号列表”映射；配置缺失或异常返回两项 None，
            未找到、身份不兼容或响应无可用数据时返回 None 和空映射
        """
        if not self._host or not self._apikey:
            return None, None
        cached_item_id = item_id
        if not item_id:
            item_id = self.__get_emby_series_id_by_name(title, year, **MediaServerQueryOptions.as_kwargs(query))
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
            logger.warning(f"Emby缓存的电视剧媒体ID {cached_item_id} 已失效，尝试按标题重新搜索：{title}")
            item_id = self.__get_emby_series_id_by_name(title, year, **MediaServerQueryOptions.as_kwargs(query))
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
        try:
            url = f"{self._host}emby/Shows/{item_id}/Episodes"
            params = {"Season": season, "IsMissing": "false", "api_key": self._apikey}
            params.update(
                emby_query_params(
                    MediaServerQueryOptions(fields=frozenset()) if query is not None and query.fields is not None else None
                )
            )
            res_json = RequestUtils().get_res(url, params)
            if res_json:
                tv_item = res_json.json()
                res_items = tv_item.get("Items")
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
                    if season_index not in season_episodes:
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

        :param item_id: Emby 条目标识
        :param season: 目标季号
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 集号到条目标识的映射；输入不完整、无分集或查询失败时返回空映射
        """
        if not item_id or not self._host or not self._apikey:
            return {}
        try:
            url = f"{self._host}emby/Shows/{item_id}/Episodes"
            params = {"Season": season, "IsMissing": "false", "api_key": self._apikey}
            params.update(
                emby_query_params(
                    MediaServerQueryOptions(fields=frozenset()) if query is not None and query.fields is not None else None
                )
            )
            res_json = RequestUtils().get_res(url, params)
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
            logger.error(f"获取 Emby 季集条目 ID 出错：{str(e)}")
            return {}

    def get_remote_image_by_id(self, item_id: str, image_type: str) -> Optional[str]:
        """
        优先查询指定类型的 TMDB 图片，必要时回退媒体服务器图片。

        没有匹配图片时尝试外网媒体服务器地址。远程图片查询发生异常时直接返回 None。

        :param item_id: Emby 条目标识
        :param image_type: 服务端图片类型，如 Primary 或 Backdrop，需与响应中的 Type 一致
        :return: 匹配的图片 URL；回退失败时返回 None
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
                            return _optional_text(image.get("Url"))
            logger.info("Items/RemoteImages 未获取到返回数据，采用本地图片")
            return self.generate_external_image_link(item_id, image_type)
        except Exception as e:
            logger.error("连接Items/Id/RemoteImages出错：" + str(e))
        return None

    def generate_external_image_link(self, item_id: str, image_type: str) -> Optional[str]:
        """
        探测外网播放地址下的条目图片。

        :param item_id: Emby 条目标识
        :param image_type: 服务端图片类型，如 Backdrop 或 Primary
        :return: 可访问响应的最终 URL；外网地址缺失、图片不存在或请求失败时返回 None
        """
        if not self._playhost:
            logger.error("Emby外网播放地址未能获取或为空")
            return None

        url = f"{self._playhost}Items/{item_id}/Images/{image_type}"
        try:
            res = RequestUtils().get_res(url)
            if res and res.status_code != 404:
                logger.info(f"影片图片链接:{res.url}")
                return _optional_text(res.url)
            else:
                logger.info("Items/Id/Images 未获取到返回数据或无该影片{}图片".format(image_type))
                return None
        except Exception as e:
            logger.error("连接Items/Id/Images出错：" + str(e))
            return None

    def __refresh_emby_library_by_id(self, item_id: str) -> bool:
        """
        通知Emby刷新一个项目的媒体库。
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
            logger.error("连接Items/Id/Refresh出错：" + str(e))
            return False
        return False

    def refresh_root_library(self) -> bool:
        """
        请求 Emby 刷新整个媒体库。

        :return: 刷新请求被接受时返回 True，配置不完整或请求失败时返回 False；不等待扫描完成
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
                logger.info("刷新媒体库失败，无法连接Emby！")
        except Exception as e:
            logger.error("连接Library/Refresh出错：" + str(e))
            return False
        return False

    def refresh_library_by_items(self, items: List[_SchemaRefreshMediaItem]) -> Optional[bool]:
        """
        按媒体身份和目标路径合并刷新范围，并逐项请求刷新。

        已有电视剧仅刷新对应剧集，已有电影不重复刷新。不能定位媒体库时刷新整个根库。

        :param items: 包含媒体类型、标题、年份及目标路径的刷新请求列表
        :return: 输入为空时返回 False；有任一刷新请求失败时返回 False，无需刷新或全部请求成功时返回 True
        """
        if not items:
            return False
        # 收集要刷新的媒体库信息
        logger.info("开始刷新Emby媒体库...")
        library_ids = []
        for item in items:
            library_id = self.__get_emby_library_id_by_item(item)
            if library_id and library_id not in library_ids:
                library_ids.append(library_id)
        if "/" in library_ids:
            return self.refresh_root_library()
        success = True
        for library_id in library_ids:
            if library_id != "/":
                refreshed = self.__refresh_emby_library_by_id(library_id)
                success = bool(refreshed) and success
        logger.info("Emby媒体库刷新完成")
        return success

    def __get_emby_library_id_by_item(self, item: _SchemaRefreshMediaItem) -> Optional[str]:
        """
        根据媒体身份、路径及分类确定需要刷新的位置。

        :param item: 待刷新的媒体信息及目标路径
        :return: 电视剧或媒体库标识；无法定位时返回 / 表示根库，缺少必要信息或电影已存在时返回 None
        """
        if not item.type or not item.target_path:
            return None
        if item.type == MediaType.TV and item.title and item.year:
            item_id = self.__get_emby_series_id_by_name(item.title, item.year)
            if item_id:
                # 存在电视剧，则直接刷新这个电视剧就行
                return item_id
        elif item.type == MediaType.MOVIE and item.title and item.year:
            if self.get_movies(item.title, item.year):
                # 已存在，不用刷新
                return None
        # 查找需要刷新的媒体库ID
        item_path = Path(item.target_path)
        for folder in self.folders:
            for subfolder in folder.get("SubFolders") or []:
                try:
                    subfolder_path = Path(subfolder.get("Path"))
                    if item_path.is_relative_to(subfolder_path):
                        return folder.get("Id")
                except Exception as err:
                    logger.debug(f"匹配子目录出错：{err} - {traceback.format_exc()}")
        # 如果找不到，只要路径中有分类目录名就命中
        if item.category:
            for folder in self.folders:
                for subfolder in folder.get("SubFolders") or []:
                    if subfolder.get("Path") and re.search(
                            r"[/\\]%s" % re.escape(item.category), subfolder.get("Path")
                    ):
                        return folder.get("Id")
        return "/"

    @staticmethod
    def __format_item_info(item: dict[str, Any]) -> Optional[_SchemaMediaServerItem]:
        """
        通过统一 Emby 系转换契约生成条目，并保留 Emby 的服务器 ID。
        """
        return format_emby_family_item(
            item,
            server="emby",
            include_server_id=True,
        )

    def get_iteminfo(
        self, itemid: str, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[_SchemaMediaServerItem]:
        """
        查询单个条目并转换为统一媒体模型。

        :param itemid: Emby 条目标识
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 条目详情；标识为空、配置不完整、查询失败或类型无法转换时返回 None
        """
        if not itemid:
            return None
        if not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/Users/{self.user}/Items/{itemid}"
        params: dict[str, Any] = {"api_key": self._apikey}
        projected = query is not None and query.fields is not None
        if projected:
            url = url.rsplit("/", 1)[0]
            params.update({"Ids": itemid, "Limit": 1, **emby_query_params(query)})
        try:
            res = RequestUtils().get_res(url, params)
            if res and res.status_code == 200:
                payload = res.json()
                if projected:
                    payload = next(iter(payload.get("Items") or []), None)
                if payload is None:
                    return None
                iteminfo = self.__format_item_info(payload)
                return iteminfo
        except Exception as e:
            logger.error(f"连接/Users/{self.user}/Items/{itemid}出错：" + str(e))
        return None

    def _get_series_identity(
        self,
        series_id: Optional[str],
    ) -> Tuple[Optional[MediaSource], Optional[str]]:
        """
        查询 Emby Series 条目的规范身份，查询失败时返回空身份。
        """
        if not series_id or not self._host or not self._apikey or not self.user:
            return None, None
        url = f"{self._host}emby/Users/{self.user}/Items/{series_id}"
        params = {
            "api_key": self._apikey,
            "Fields": "ProviderIds",
        }
        try:
            res = RequestUtils().get_res(url, params)
            if res and res.status_code == 200:
                return MediaServerIdentityHelper.from_provider_ids(res.json().get("ProviderIds"))
        except Exception as e:
            logger.error(f"获取 Emby 剧集身份出错：{str(e)}")
        return None, None

    def get_items_count(self, parent: Optional[Union[str, int]], include_item_types: str = "Movie,Series") -> Optional[int]:
        """
        递归统计指定媒体库内的可同步条目。

        :param parent: 媒体库标识
        :param include_item_types: 逗号分隔的服务端条目类型，默认 Movie,Series
        :return: 条目总数；输入或配置不完整、响应缺少计数或查询失败时返回 None
        """
        if not parent or not self._host or not self._apikey:
            return None
        url = f"{self._host}emby/Users/{self.user}/Items"
        params = {
            "ParentId": parent,
            "Recursive": "true",
            "IncludeItemTypes": include_item_types,
            "Limit": 0,
            "api_key": self._apikey,
        }
        try:
            res = RequestUtils().get_res(url, params)
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
        Folder 展开为成员，只返回电影、电视剧和音乐专辑。

        :param parent: 媒体库标识，不接受空值
        :param start_index: 分页起始索引；不限制数量时不发送此参数
        :param limit: 本层请求数量；None 或 -1 不限制，递归子目录始终不分页
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :yields: 转换后的统一媒体条目
        :raises ValueError: 媒体库标识为空
        :raises ConnectionError: 配置不完整或读取媒体库失败
        """
        if not parent:
            raise ValueError("Emby 媒体库 ID 不能为空")
        if not self._host or not self._apikey:
            raise ConnectionError("Emby 服务器未连接")
        url = f"{self._host}emby/Users/{self.user}/Items"
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
            res = RequestUtils().get_res(url, params)
            if not res or res.status_code != 200:
                raise ConnectionError(
                    f"读取 Emby 媒体库 {parent} 失败，状态码：{res.status_code if res is not None else '无响应'}"
                )
            items = res.json().get("Items") or []
            for item in items:
                if not item:
                    continue
                if "Folder" in item.get("Type"):
                    for items in self.get_items(
                        parent=item.get("Id"), **MediaServerQueryOptions.as_kwargs(query)
                    ):
                        yield items
                elif item.get("Type") in ["Movie", "Series", "MusicAlbum"]:
                    yield self.__format_item_info(item)
        except Exception as e:
            logger.error(f"同步读取 Emby 媒体库 {parent} 出错：{str(e)}")
            raise

    def get_webhook_message(self, form: Any, args: dict[str, object]) -> Optional[_SchemaWebhookEventInfo]:
        """
        解析 Emby Webhook，并补全统一事件中的媒体身份与图片。

        Episode 和 Season 的 ProviderIds 可能指向单集或季，电视剧身份从 SeriesId 对应条目读取。
        处理事件可能查询媒体服务器获取剧集身份及背景图片。

        :param form: 表单数据；优先解析 data 字段中的 JSON
        :param args: 表单无 data 时作为事件字段来源的查询参数
        :return: 统一 Webhook 事件；输入为空、JSON 无法解析或缺少 Event 时返回 None
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
            logger.debug("解析emby webhook报文出错：" + str(e))
            return None
        eventType = message.get('Event')
        if not eventType:
            return None
        logger.debug(f"接收到emby webhook：{message}")
        eventItem = _SchemaWebhookEventInfo(event=eventType, channel="emby")
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
            if message.get('Item', {}).get('Type') in {"Episode", "Season"}:
                eventItem.media_source, eventItem.media_id = self._get_series_identity(eventItem.item_id)
            else:
                eventItem.media_source, eventItem.media_id = MediaServerIdentityHelper.from_provider_ids(
                    message.get('Item', {}).get('ProviderIds')
                )
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
            eventItem.media_source, eventItem.media_id = MediaServerIdentityHelper.from_provider_ids({
                "tmdb_id": message.get("tmdb_id"),
            })
            eventItem.season_id = message.get("season_id")
            eventItem.episode_id = message.get("episode_id")

        if eventItem.item_id:
            eventItem.image_url = self.get_remote_image_by_id(item_id=eventItem.item_id,
                                                              image_type="Backdrop")

        eventItem.json_object = message

        return eventItem

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
            return RequestUtils(content_type="application/json").get_res(url=url)
        except Exception as e:
            logger.error("连接Emby出错：" + str(e))
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
            return RequestUtils(
                headers=headers,
            ).post_res(url=url, data=data)
        except Exception as e:
            logger.error("连接Emby出错：" + str(e))
            return None

    def get_play_url(self, item_id: str, server_id: Optional[str] = None) -> str:
        """
        构造媒体服务器网页中的条目播放链接。

        :param item_id: Emby 条目标识
        :param server_id: 链接使用的服务器标识，省略时采用当前实例标识
        :return: 优先使用外网播放地址的网页链接；只构造链接，不验证条目存在
        """
        server_id = server_id or self.serverid
        server_query = f"&serverId={server_id}" if server_id else ""
        return f"{self._playhost or self._host}web/index.html#!" \
            f"/item?id={item_id}&context=home{server_query}"

    def get_backdrop_url(self, item_id: str, image_tag: str, remote: Optional[bool] = False) -> str:
        """
        构造带 API 密钥和版本标签的背景图片地址。

        :param item_id: Emby 条目标识
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

    def __get_local_image_by_id(self, item_id: Optional[Union[str, int]], image_tag: Optional[str] = None) -> str:
        """
        构造服务内网的主封面图片地址。

        :param item_id: Emby 条目标识
        :param image_tag: 可选图片版本标签，用于更新缓存键
        :return: 主封面地址；服务配置不完整时返回空字符串
        """
        if not self._host or not self._apikey:
            return ""
        image_url = f"{self._host}Items/{item_id}/Images/Primary"
        return f"{image_url}?tag={image_tag}" if image_tag else image_url

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
            "Fields": "ProductionYear,Path,ImageTags",
            "api_key": self._apikey,
        }
        options = query or MediaServerQueryOptions()
        try:
            params.update(emby_query_params(query.with_fields(MediaServerField.PATH) if query is not None else None))
            res = RequestUtils().get_res(url, params)
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
                    server_id = item.get("ServerId") or self.serverid
                    link = (
                        self.get_play_url(item.get("Id"), server_id=server_id)
                        if options.includes(MediaServerField.LINK)
                        else None
                    )
                    if item_type == MediaType.MOVIE.value:
                        title = item.get("Name")
                        subtitle = str(item.get("ProductionYear")) if item.get("ProductionYear") else None
                    else:
                        title = f"{item.get('SeriesName')}"
                        subtitle = f"S{item.get('ParentIndexNumber')}:{item.get('IndexNumber')} - {item.get('Name')}"
                    image = self._get_resume_image(item, query)
                    ret_resume.append(
                        _SchemaMediaServerPlayItem(
                            id=item.get("Id"),
                            item_id=item.get("Id"),
                            server_id=server_id,
                            title=title,
                            subtitle=subtitle,
                            type=item_type,
                            image=image,
                            link=link,
                            percent=item.get("UserData", {}).get("PlayedPercentage"),
                            server_type="emby",
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
        item_type = MediaType.MOVIE.value if item.get("Type") == "Movie" else MediaType.TV.value
        if item_type == MediaType.MOVIE.value:
            if item.get("BackdropImageTags"):
                image = self.get_backdrop_url(item_id=item["Id"], image_tag=(item.get("BackdropImageTags") or [])[0])
            else:
                image = self.__get_local_image_by_id(
                    item.get("Id"),
                    image_tag=(item.get("ImageTags") or {}).get("Primary"),
                )
        else:
            image = self.get_backdrop_url(
                item_id=item.get("SeriesId", ""), image_tag=item.get("SeriesPrimaryImageTag", "")
            )
            if not image:
                image = self.__get_local_image_by_id(
                    item.get("SeriesId"),
                    image_tag=item.get("SeriesPrimaryImageTag"),
                )
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
            "Fields": "ProductionYear,Path,BackdropImageTags,ImageTags",
            "api_key": self._apikey,
        }
        options = query or MediaServerQueryOptions()
        try:
            params.update(emby_query_params(query.with_fields(MediaServerField.PATH) if query is not None else None))
            res = RequestUtils().get_res(url, params)
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
                    server_id = item.get("ServerId") or self.serverid
                    link = (
                        self.get_play_url(item.get("Id"), server_id=server_id)
                        if options.includes(MediaServerField.LINK)
                        else None
                    )
                    image = (
                        self.__get_local_image_by_id(
                            item_id=item.get("Id"),
                            image_tag=(item.get("ImageTags") or {}).get("Primary"),
                        )
                        if options.includes(MediaServerField.IMAGES)
                        else None
                    )
                    ret_latest.append(
                        _SchemaMediaServerPlayItem(
                            id=item.get("Id"),
                            item_id=item.get("Id"),
                            server_id=server_id,
                            title=item.get("Name"),
                            subtitle=str(item.get("ProductionYear")) if item.get("ProductionYear") else None,
                            type=item_type,
                            image=image,
                            link=link,
                            BackdropImageTags=item.get("BackdropImageTags")
                            if options.includes(MediaServerField.IMAGES)
                            else None,
                            server_type="emby",
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
        for library in self.get_emby_virtual_folders() or []:
            if self._sync_libraries and library.get("Id") not in self._sync_libraries:
                continue
            paths = library.get("Path")
            if paths is None:
                raise TypeError("Emby 媒体库路径列表为空")
            library_folders += [folder for folder in paths]
        return library_folders
