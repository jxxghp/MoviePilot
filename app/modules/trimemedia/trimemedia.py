"""
飞牛影视客户端，协调登录、媒体库权限、刷新、播放链接与图片授权。
"""

from pathlib import Path
from typing import Dict, Generator, List, Optional, Tuple, Union

import app.modules.trimemedia.api as fnapi
from app.application.mediaserver import MediaServerIdentityHelper
from app.application.security.url import SecurityUtils
from app.foundation.url import UrlUtils
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerItemUserState as _SchemaMediaServerItemUserState
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import RefreshMediaItem as _SchemaRefreshMediaItem
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import MediaSource, MediaType


class TrimeMedia:
    """
    飞牛影视媒体服务器客户端。
    """

    _username: Optional[str] = None
    _password: Optional[str] = None
    _access_code: Optional[str] = None

    _userinfo: Optional[fnapi.User] = None
    _host: Optional[str] = None
    _playhost: Optional[str] = None

    _libraries: dict[str, fnapi.MediaDb] = {}
    _sync_libraries: List[str] = []

    _api: Optional[fnapi.Api] = None
    _version: Optional[fnapi.Version] = None

    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        access_code: Optional[str] = None,
        play_host: Optional[str] = None,
        sync_libraries: Optional[list[str]] = None,
        **kwargs: object,
    ) -> None:
        """
        保存飞牛服务配置并登录，同时探测可用的外网播放地址。

        地址、用户名或密码缺失时不连接；连接失败时保留配置供后续重连。

        :param host: 飞牛服务地址；探测时优先补齐 /v 路径
        :param username: 登录用户名
        :param password: 登录密码
        :param access_code: 可选设备访问码，启用后用于登录和图片请求
        :param play_host: 可选外网播放地址，探测失败时保留规整后的配置值
        :param sync_libraries: 同步媒体库标识，空列表或包含 all 表示不限制
        :param kwargs: 通用服务配置的额外参数，当前不读取
        """
        if not host or not username or not password:
            logger.error("飞牛影视配置不完整！！")
            return
        self._username = username
        self._password = password
        self._access_code = access_code
        self._host = host
        self._sync_libraries = sync_libraries or []

        if not self.reconnect():
            logger.error(f"请检查服务端地址 {host}")
            return
        if result := self.__create_api(play_host, access_code):
            self._playhost = result.api.host
            result.api.close()
        elif play_host:
            logger.warning(f"请检查外网播放地址 {play_host}")
            self._playhost = UrlUtils.standardize_base_url(play_host).rstrip("/")

    @property
    def api(self) -> Optional[fnapi.Api]:
        """
        获得飞牛API。
        """
        return self._api

    @property
    def version(self) -> Optional[fnapi.Version]:
        """
        获得飞牛API的版本。
        """
        return self._version

    class _ApiCreateResult:
        """
        已经验证可访问的飞牛 API 与对应版本信息。

        :ivar api: 通过访问码及版本探测的客户端，接管方负责关闭
        :ivar version: 客户端版本接口返回的前后端版本
        """
        api: fnapi.Api
        version: fnapi.Version

    @staticmethod
    def __create_api(
        host: Optional[str], access_code: Optional[str] = None
    ) -> Optional["TrimeMedia._ApiCreateResult"]:
        """
        创建一个飞牛API。

        :param host: 服务端地址
        :param access_code: 访问码，未开启时为空
        :return: 如果地址无效、不可访问则返回None
        """

        if not host:
            return None
        api_key = "16CCEB3D-AB42-077D-36A1-F355324E4237"
        host = UrlUtils.standardize_base_url(host).rstrip("/")

        if not host.endswith("/v"):
            # 尝试补上结尾的/v 测试能否正常访问
            res = TrimeMedia._ApiCreateResult()
            res.api = fnapi.Api(host + "/v", api_key, access_code)
            # 开启访问码后，需先校验才能访问各应用接口
            if res.api.verify_access_code() and (fnver := res.api.sys_version()):
                res.version = fnver
                return res
            res.api.close()
        # 测试用户配置的地址
        res = TrimeMedia._ApiCreateResult()
        res.api = fnapi.Api(host, api_key, access_code)
        if res.api.verify_access_code() and (fnver := res.api.sys_version()):
            res.version = fnver
            return res
        res.api.close()
        return None

    def close(self) -> None:
        """
        登出当前用户并关闭飞牛 API 会话。
        """
        self.disconnect()

    def is_configured(self) -> bool:
        """
        检查是否同时配置服务地址、用户名和密码。

        :return: 三项配置均非空时返回 True，不代表服务当前可达
        """
        return bool(self._host and self._username and self._password)

    def is_authenticated(self) -> bool:
        """
        检查当前实例是否具有完整登录上下文。

        :return: 配置完整且 API、令牌和用户信息均存在时返回 True，不发起远程验证
        """
        return (
            self.is_configured()
            and self._api is not None
            and self._api.token is not None
            and self._userinfo is not None
        )

    def is_inactive(self) -> bool:
        """
        通过用户信息查询验证当前会话，并更新用户缓存。

        :return: 尚未认证或当前用户信息查询失败时返回 True
        """
        if not self.is_authenticated():
            return True
        assert self._api is not None
        self._userinfo = self._api.user_info()
        return self._userinfo is None

    def reconnect(self) -> bool:
        """
        关闭旧连接，重新探测版本、验证访问码并登录。

        成功后刷新媒体库缓存；目录查询失败不会撤销已经建立的认证状态。

        :return: 登录并获取用户信息后返回 True；配置缺失、探测或认证失败时返回 False
        """
        if not self.is_configured():
            return False
        self.disconnect()
        if result := self.__create_api(self._host, self._access_code):
            self._api = result.api
            self._version = result.version
            # 版本号:0.8.53, 服务版本:0.8.23
            # 版本号:0.8.56, 服务版本:0.8.23 接口/memory/user/list改为/manager/user/list
            logger.debug(
                f"版本号:{result.version.frontend}, 服务版本:{result.version.backend}"
            )
        else:
            return False
        if self._username is None or self._password is None:
            return False
        if self._api.login(self._username, self._password) is None:
            return False
        self._userinfo = self._api.user_info()
        if self._userinfo is None:
            return False
        logger.debug(f"{self._username} 成功登录飞牛影视")
        # 刷新媒体库列表
        self.get_librarys()
        return True

    def disconnect(self) -> None:
        """
        退出当前账号、关闭 HTTP 会话并清空客户端及用户状态。
        """
        if self._api:
            self._api.logout()
            self._api.close()
            self._api = None
            self._userinfo = None
            logger.debug(f"{self._username} 已断开飞牛影视")

    def get_librarys(
        self, hidden: Optional[bool] = False
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        按用户权限读取媒体库，管理员接口同时提供实际目录。

        每次成功查询都会替换实例的媒体库缓存，后续路径匹配使用该缓存。

        :param hidden: 是否隐藏同步范围外的媒体库
        :return: 统一媒体库列表；未认证或查询失败时返回 None，其他分类库始终不返回
        """
        if not self.is_authenticated():
            return None
        assert self._api is not None
        assert self._userinfo is not None
        if self._userinfo.is_admin == 1:
            mdb_list = self._api.mdb_list()
        else:
            mdb_list = self._api.mediadb_list()
        if mdb_list is None:
            return None
        self._libraries = {lib.guid: lib for lib in mdb_list}
        libraries = []
        for library in self._libraries.values():
            if hidden and self.__is_library_blocked(library.guid):
                continue
            if library.category == fnapi.Category.MOVIE:
                library_type = MediaType.MOVIE.value
            elif library.category == fnapi.Category.TV:
                library_type = MediaType.TV.value
            elif getattr(library.category, "value", "").lower() in ("music", "audio"):
                library_type = MediaType.MUSIC.value
            elif library.category == fnapi.Category.OTHERS:
                # 忽略这个库
                continue
            else:
                library_type = MediaType.UNKNOWN.value
            libraries.append(
                _SchemaMediaServerLibrary(
                    server="trimemedia",
                    id=library.guid,
                    name=library.name,
                    type=library_type,
                    path=library.dir_list,
                    item_count=self.get_items_count(library.guid),
                    image_list=[
                        f"{self._api.host}{img_path}?w=256"
                        for img_path in library.posters or []
                    ],
                    link=f"{self._playhost or self._api.host}/library/{library.guid}",
                    server_type="trimemedia",
                    use_cookies=True,
                )
            )
        return libraries

    def get_user_count(self) -> int:
        """
        查询管理员账号可见的用户数量。

        :return: 用户总数；未认证、非管理员或查询失败时返回 0
        """
        if not self.is_authenticated():
            return 0
        assert self._api is not None
        if not self._userinfo or self._userinfo.is_admin != 1:
            return 0
        return len(self._api.user_list() or [])

    def get_medias_count(self) -> _SchemaStatistic:
        """
        查询电影、电视剧及可用的音乐统计。

        :return: 统一统计对象；未认证或请求失败时返回空统计
        """
        if not self.is_authenticated():
            return _SchemaStatistic()
        assert self._api is not None
        if (info := self._api.mediadb_sum()) is None:
            return _SchemaStatistic()
        return _SchemaStatistic(
            movie_count=info.movie,
            tv_count=info.tv,
            music_count=getattr(info, "music", 0) or getattr(info, "audio", 0) or 0,
        )

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """
        使用独立临时 API 验证用户名和密码。

        验证结束后会登出并关闭临时会话；返回令牌仅用于认证结果，不保证之后仍可访问服务。
        新会话沿用实例访问码，先校验访问权限，再验证待登录用户。

        :param username: 待验证的用户名
        :param password: 待验证的原始密码
        :return: 成功时返回认证令牌，输入不完整或认证失败时返回 None
        """
        if not username or not password:
            return None
        if not self.is_configured():
            return None
        if result := self.__create_api(self._host, self._access_code):
            try:
                return result.api.login(username, password)
            finally:
                result.api.logout()
                result.api.close()
        return None

    def get_movies(
        self, title: str, year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
    ) -> Optional[List[_SchemaMediaServerItem]]:
        """
        搜索电影并核对名称、年份和来源身份。

        :param title: 需匹配标题或原始标题的电影名称
        :param year: 可选年份，与发行日期前四位比较
        :param media_source: 用于排除身份冲突的来源
        :param media_id: 对应来源内的媒体标识
        :return: 统一电影条目列表；未认证时返回 None，无匹配或搜索失败时返回空列表
        """
        if not self.is_authenticated():
            return None
        assert self._api is not None
        movies = []
        items = self._api.search_list(keywords=title) or []
        for item in items:
            if item.type != fnapi.Type.MOVIE:
                continue
            item_source, item_media_id = MediaServerIdentityHelper.from_provider_ids(
                {"tmdb_id": item.tmdb_id, "imdb_id": item.imdb_id}
            )
            if (
                MediaServerIdentityHelper.are_compatible(
                    item_source, item_media_id, media_source, media_id
                )
                and title in [item.title, item.original_title]
                and (not year or (item.release_date and item.release_date[:4] == year))
            ):
                movies.append(self.__build_media_server_item(item))
        return movies

    def __get_series_id_by_name(self, name: Optional[str], year: Optional[str]) -> Optional[str]:
        """
        从搜索候选中按剧名与首播年份查找电视剧。

        :param name: 目标剧名
        :param year: 可选年份，空值时不限年份
        :return: 首个匹配电视剧的 guid；无匹配或搜索失败时返回 None
        """
        assert self._api is not None
        items = self._api.search_list(keywords=name) or []
        for item in items:
            if item.type != fnapi.Type.TV:
                continue
            # 搜索接口可能不返回 original_title，也不能直接限定分类或年份。
            if name in [item.title, item.original_title]:
                if not year or (item.air_date and item.air_date[:4] == year):
                    return item.guid
        return None

    def get_tv_episodes(
        self,
        item_id: Optional[str] = None,
        title: Optional[str] = None,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
    ) -> Tuple[Optional[str], Optional[Dict[int, list[int]]]]:
        """
        查询电视剧已入库的季集编号，并校验媒体来源身份。

        缓存标识失效时按标题重新搜索；个别季的分集查询失败会保留其他季已经取得的结果。

        :param item_id: 优先使用的缓存电视剧 guid
        :param title: 缺少标识或缓存失效时回退搜索的剧名
        :param year: 搜索时限定的首播年份，空值不限制
        :param media_source: 用于排除身份冲突的来源
        :param media_id: 对应来源内的标识
        :param season: 目标季号，None 表示全部季
        :return: 电视剧 guid 与“季号到集号列表”映射；未认证或标题查找失败时返回两项 None，
            详情无效、身份冲突或季列表不可用时返回 None 和空映射
        """
        if not self.is_authenticated():
            return None, None
        assert self._api is not None

        cached_item_id = item_id
        if not item_id:
            item_id = self.__get_series_id_by_name(title, year)
            if item_id is None:
                return None, None

        item_info = self.get_iteminfo(item_id)
        if not item_info and cached_item_id and title:
            # 媒体删除后重新入库会导致缓存ID失效，回退到标题搜索避免误判整部剧缺失。
            logger.warning(f"飞牛影视缓存的电视剧媒体ID {cached_item_id} 已失效，尝试按标题重新搜索：{title}")
            item_id = self.__get_series_id_by_name(title, year)
            if item_id is None:
                return None, None
            item_info = self.get_iteminfo(item_id)
        if not item_info:
            return None, {}

        if not MediaServerIdentityHelper.is_compatible(item_info, media_source, media_id):
            return None, {}

        seasons = self._api.season_list(item_id)
        if not seasons:
            # 季列表获取失败
            return None, {}

        if season is not None:
            for item in seasons:
                if item.season_number == season:
                    seasons = [item]
                    break
            else:
                # 没有匹配的季
                return None, {}

        season_episodes: Dict[int, list[int]] = {}
        for item in seasons:
            episodes = self._api.episode_list(item.guid)
            for episode in episodes or []:
                if episode.season_number is None or episode.episode_number is None:
                    continue
                season_episodes.setdefault(episode.season_number, []).append(episode.episode_number)
        return item_id, season_episodes

    def refresh_root_library(self) -> Optional[bool]:
        """
        以管理员身份请求扫描全部媒体库。

        扫描前先查询运行任务，兼容服务端可能错误报告重复任务的行为。

        :return: 扫描请求是否成功；未认证返回 None，非管理员返回 False，不等待扫描结束
        """
        if not self.is_authenticated():
            return None
        assert self._api is not None
        if not self._userinfo or self._userinfo.is_admin != 1:
            logger.error("飞牛仅支持管理员账号刷新媒体库")
            return False

        # 扫描前先查询任务状态，避免服务端误报 -14（重复任务）。
        self._api.task_running()
        logger.info("刷新所有媒体库")
        return self._api.mdb_scanall()

    def refresh_library_by_items(
        self, items: List[_SchemaRefreshMediaItem]
    ) -> Optional[bool]:
        """
        按整理目标路径定位媒体库，并以管理员身份请求刷新。

        路径无法定位或单库刷新失败时回退全库刷新；重复媒体库只请求一次。

        :param items: 包含 target_path 的媒体刷新请求列表
        :return: 扫描请求结果；未认证返回 None，非管理员返回 False
        """
        if not self.is_authenticated():
            return None
        assert self._api is not None
        if not self._userinfo or self._userinfo.is_admin != 1:
            logger.error("飞牛仅支持管理员账号刷新媒体库")
            return False

        libraries = set()
        for item in items:
            lib = self.__match_library_by_path(item.target_path)
            if lib is None:
                # 如果有匹配失败的,刷新整个库
                return self.refresh_root_library()
            # 媒体库去重
            libraries.add(lib.guid)

        # 扫描前先查询任务状态，避免服务端误报 -14（重复任务）。
        self._api.task_running()
        for lib_guid in libraries:
            # 逐个刷新
            lib = self._libraries[lib_guid]
            logger.info(f"刷新媒体库：{lib.name}")
            if not self._api.mdb_scan(lib):
                # 如果失败，刷新整个库
                return self.refresh_root_library()
        return True

    def __match_library_by_path(self, path: Optional[Path]) -> Optional[fnapi.MediaDb]:
        """
        按缓存的实际目录查找第一个包含目标路径的媒体库。

        解析路径后按目录层级比较；普通用户接口不提供目录时无法定位，只能由调用方回退全库刷新。

        :param path: 待刷新的目标路径
        :return: 匹配的媒体库对象；路径为空或无目录匹配时返回 None
        """
        def is_subpath(_path: Path, _parent: Path) -> bool:
            """
            判断_path是否是_parent的子目录下。
            """
            _path = _path.resolve()
            _parent = _parent.resolve()
            return _path.parts[: len(_parent.parts)] == _parent.parts

        if path is None:
            return None
        for lib in self._libraries.values():
            for d in lib.dir_list or []:
                if is_subpath(path, Path(d)):
                    return lib
        return None

    def get_webhook_message(self, body: object) -> Optional[_SchemaWebhookEventInfo]:
        """
        保留统一 Webhook 入口，飞牛当前不实现事件解析。

        :param body: 兼容接口的请求体，当前不读取
        :return: 始终返回 None
        """
        return None

    def get_iteminfo(self, itemid: str) -> Optional[_SchemaMediaServerItem]:
        """
        查询单个条目并转换为统一媒体详情。

        :param itemid: 飞牛条目 guid
        :return: 统一条目模型；未认证或查询失败时返回 None
        """
        if not self.is_authenticated():
            return None
        assert self._api is not None
        if item := self._api.item(guid=itemid):
            return self.__build_media_server_item(item)
        return None

    @staticmethod
    def __build_media_server_item(item: fnapi.Item) -> _SchemaMediaServerItem:
        """
        将飞牛条目投影为统一媒体身份和用户观看状态。

        :param item: 包含类型、日期、身份及播放时间的飞牛条目
        :return: 统一媒体条目；日期前四位作为年份，播放秒数除以总秒数转换为百分比
        """
        if item.air_date and item.type == fnapi.Type.TV:
            year = item.air_date[:4]
        elif item.release_date:
            year = item.release_date[:4]
        else:
            year = None

        user_state = _SchemaMediaServerItemUserState()
        if item.watched:
            user_state.played = True
        if item.duration and item.ts is not None:
            user_state.percentage = item.ts / item.duration * 100
            user_state.resume = True
        if item.type is None:
            item_type = None
        else:
            # 将飞牛的媒体类型转为MP能识别的
            item_type = "Series" if item.type == fnapi.Type.TV else item.type.value
        media_source, media_id = MediaServerIdentityHelper.from_provider_ids({
            "tmdb_id": item.tmdb_id,
            "imdb_id": item.imdb_id,
            "douban_id": item.douban_id,
        })
        return _SchemaMediaServerItem(
            server="trimemedia",
            library=item.ancestor_guid,
            item_id=item.guid,
            item_type=item_type,
            title=item.title,
            original_title=item.original_title,
            year=year,
            media_source=media_source,
            media_id=media_id,
            user_state=user_state,
        )

    @staticmethod
    def __build_play_url(host: str, item: fnapi.Item) -> str:
        """
        拼装播放链接。
        """
        if item.type == fnapi.Type.EPISODE:
            return f"{host}/tv/episode/{item.guid}"
        elif item.type == fnapi.Type.SEASON:
            return f"{host}/tv/season/{item.guid}"
        elif item.type == fnapi.Type.MOVIE:
            return f"{host}/movie/{item.guid}"
        elif item.type == fnapi.Type.TV:
            return f"{host}/tv/{item.guid}"
        else:
            # 其它类型走通用页面，由飞牛来判断
            return f"{host}/other/{item.guid}"

    def __build_media_server_play_item(
        self, item: fnapi.Item
    ) -> _SchemaMediaServerPlayItem:
        """
        构造仪表盘播放条目，分集展示剧名和季集副标题。

        :param item: 飞牛媒体条目
        :return: 需要 Cookie 访问图片的播放模型，含观看百分比及对应网页链接
        """
        assert self._api is not None
        if item.type == fnapi.Type.EPISODE:
            title = item.tv_title
            subtitle = f"S{item.season_number}:{item.episode_number} - {item.title}"
        else:
            title = item.title
            subtitle = "电影" if item.type == fnapi.Type.MOVIE else "视频"
        types = (
            MediaType.MOVIE.value
            if item.type in [fnapi.Type.MOVIE, fnapi.Type.VIDEO]
            else MediaType.TV.value
        )
        return _SchemaMediaServerPlayItem(
            id=item.guid,
            title=title,
            subtitle=subtitle,
            type=types,
            image=f"{self._api.host}{item.poster}",
            link=self.__build_play_url(self._playhost or self._api.host, item),
            percent=(
                item.ts / item.duration * 100.0
                if item.duration and item.ts is not None
                else 0
            ),
            server_type="trimemedia",
            use_cookies=True,
        )

    def get_items_count(self, parent: Union[str, int]) -> Optional[int]:
        """
        获取指定媒体库可同步的媒体条目总数。

        :param parent: 媒体库ID
        :return: 媒体条目总数，查询失败时返回None
        """
        if not self.is_authenticated():
            return None
        assert self._api is not None
        return self._api.item_count(
            guid=str(parent),
            types=[fnapi.Type.MOVIE, fnapi.Type.TV],
        )

    def get_items(
        self,
        parent: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
    ) -> Generator[_SchemaMediaServerItem, None, None]:
        """
        查询一页媒体库内容，并递归展开目录中的电影和电视剧。

        未认证或底层查询失败时中止迭代，避免将失败误认为成功的空媒体库。

        :param parent: 媒体库或父目录 guid
        :param start_index: 当前实现将该值加 1 作为页码；并非条目偏移，默认 0 请求第一页
        :param limit: 传给服务端的每页数量；None 或 -1 传为 -1，递归子目录使用默认不限数量
        :yields: 统一电影或电视剧条目
        :raises ConnectionError: 未认证或任意一层目录读取失败
        """
        if not self.is_authenticated():
            raise ConnectionError("飞牛影视服务尚未认证")
        assert self._api is not None
        if (page_size := limit) is None:
            page_size = -1
        if start_index is None:
            raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'int'")
        items = self._api.item_list(
            guid=parent,
            page=start_index + 1,
            page_size=page_size,
            types=[fnapi.Type.MOVIE, fnapi.Type.TV, fnapi.Type.DIRECTORY],
        )
        if items is None:
            raise ConnectionError("读取飞牛影视媒体库失败")
        for item in items:
            if item.type == fnapi.Type.DIRECTORY:
                yield from self.get_items(parent=item.guid)
            elif item.type in [fnapi.Type.MOVIE, fnapi.Type.TV]:
                yield self.__build_media_server_item(item)
        return None

    def get_play_url(self, item_id: str) -> Optional[str]:
        """
        读取条目类型后构造外网或内部网页播放链接。

        :param item_id: 飞牛条目 guid
        :return: 对应电影、剧集、季或分集页面的链接；未认证或条目查询失败时返回 None
        """
        if not self.is_authenticated():
            return None
        assert self._api is not None
        if (item := self._api.item(item_id)) is None:
            return None
        # 根据查询到的信息拼装出播放链接
        return self.__build_play_url(self._playhost or self._api.host, item)

    def get_resume(
        self, num: Optional[int] = 12
    ) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        获取同步范围内的继续观看条目。

        :param num: 最多保留的条目数；None 不限数量
        :return: 播放列表；未认证或查询失败时返回 None
        """
        if not self.is_authenticated():
            return None
        assert self._api is not None
        items = self._api.play_list()
        if items is None:
            return None
        ret_resume: list[_SchemaMediaServerPlayItem] = []
        for item in items:
            if len(ret_resume) == num:
                break
            if self.__is_library_blocked(item.ancestor_guid):
                continue
            ret_resume.append(self.__build_media_server_play_item(item))
        return ret_resume

    def get_latest(self, num: Optional[int] = 20) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        读取近期创建的媒体，并筛选同步范围内的条目。

        先请求至少 100 条或目标数量的 5 倍，再按媒体库范围筛选，不继续请求下一页。

        :param num: 最多保留的条目数，默认 20
        :return: 最新媒体列表；未认证或查询失败时返回 None
        """
        if not self.is_authenticated():
            return None
        if num is None:
            raise TypeError("unsupported operand type(s) for *: 'NoneType' and 'int'")
        assert self._api is not None
        items = self._api.item_list(
            page=1,
            page_size=max(100, num * 5),
            types=[fnapi.Type.MOVIE, fnapi.Type.TV],
        )
        if items is None:
            return None
        latest: list[_SchemaMediaServerPlayItem] = []
        for item in items:
            if len(latest) == num:
                break
            if self.__is_library_blocked(item.ancestor_guid):
                continue
            latest.append(self.__build_media_server_play_item(item))
        return latest

    def get_latest_backdrops(self, num: Optional[int] = 20, remote: Optional[bool] = False) -> Optional[List[str]]:
        """
        查询最新媒体的背景图，缺少背景图时回退海报。

        图片链接不保证可匿名访问，调用方可能仍需通过 get_image_cookies 获取授权 Cookie。

        :param num: 最多返回的图片数量，默认 20
        :param remote: 是否优先使用外网播放地址作为图片根地址
        :return: 图片 URL 列表；未认证时返回 None，没有可用查询结果时返回空列表
        """
        if not self.is_authenticated():
            return None
        if num is None:
            raise TypeError("unsupported operand type(s) for *: 'NoneType' and 'int'")
        assert self._api is not None
        items = (
            self._api.item_list(
                page=1,
                page_size=max(100, num * 5),
                types=[fnapi.Type.MOVIE, fnapi.Type.TV],
            )
            or []
        )
        backdrops: list[str] = []
        for item in items:
            if len(backdrops) == num:
                break
            if item.type not in (fnapi.Type.MOVIE, fnapi.Type.TV):
                continue
            if self.__is_library_blocked(item.ancestor_guid):
                continue
            if (item_details := self._api.item(item.guid)) is None:
                continue
            if remote:
                # 外网地址只影响图片根地址，访问时仍可能需要认证 Cookie。
                img_host = self._playhost or self._api.host
            else:
                img_host = self._api.host
            item_image: Optional[str]
            if item_details.backdrops:
                item_image = item_details.backdrops
            else:
                item_image = (
                    item_details.posters
                    if item_details.posters
                    else item_details.poster
                )
            backdrops.append(f"{img_host}{item_image}")
        return backdrops

    def __is_library_blocked(self, library_guid: str) -> bool:
        """
        判断媒体库是否属于其他分类或被同步范围排除。

        :param library_guid: 待判断的媒体库 guid
        :return: 缓存中属于 OTHERS，或非空同步范围未包含该库且没有 all 时返回 True
        """
        if library := self._libraries.get(library_guid):
            if library.category == fnapi.Category.OTHERS:
                # 忽略这个库
                return True
        return (
            True
            if (
                self._sync_libraries
                and "all" not in self._sync_libraries
                and library_guid not in self._sync_libraries
            )
            else False
        )

    def get_image_cookies(self, image_url: str) -> Optional[dict[str, Optional[str]]]:
        """
        为当前飞牛服务同源图片提供登录和访问码 Cookie。

        :param image_url: 需要获取授权信息的图片 URL，必须通过服务地址的严格安全检查
        :return: 包含 Trim-MC-token 和可选访问码凭据的字典；未认证、地址为空或不是允许地址时返回 None
        """
        if not self.is_authenticated():
            return None
        assert self._api is not None
        if not image_url or not SecurityUtils.is_safe_url(
            image_url, [self._api.host], strict=True
        ):
            return None
        cookies = {"Trim-MC-token": self._api.token}
        if self._access_code:
            # 开启访问码后，图片请求也需要携带访问码校验凭证
            cookies.update(self._api.cookies)
        return cookies
