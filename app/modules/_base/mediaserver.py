"""
媒体服务器模块业务样板基类。

沉淀各媒体服务器模块逐字复制的样板：用户辅助认证、媒体存在性检查、
定时重连与连接测试。服务器差异（认证 API、存在性检查端点、连接探测方式）
通过类属性与钩子方法保留在各模块。
"""
from typing import TYPE_CHECKING, Any, Generator, Iterable, Mapping, Optional, Tuple, Union, cast

from app.application.mediaserver import MediaServerQueryError, MusicMediaServerHelper
from app.domain.context import MediaInfo, MusicInfo
from app.modules import TService, _MediaServerBase, _ModuleBase
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.schemas.event import AuthCredentials as _SchemaAuthCredentials
from app.schemas.event import AuthInterceptCredentials as _SchemaAuthInterceptCredentials
from app.schemas.mediaserver import ExistMediaInfo as _SchemaExistMediaInfo
from app.schemas.mediaserver import MediaServerBrowseItem as _SchemaMediaServerBrowseItem
from app.schemas.mediaserver import MediaServerBrowsePage as _SchemaMediaServerBrowsePage
from app.schemas.mediaserver import MediaServerField, MediaServerQueryOptions
from app.schemas.mediaserver import MediaServerFilterOptions as _SchemaMediaServerFilterOptions
from app.schemas.mediaserver import MediaServerItemDetail as _SchemaMediaServerItemDetail
from app.schemas.types import ChainEventType, MediaType

if TYPE_CHECKING:
    from app.adapters.network.http import RequestUtils

# 默认全量枚举按 200 条读取，限制单页 JSON 及递归目录同时保留的数据量。
_EMBY_ITEMS_PAGE_SIZE = 200
# 单个合并文件可覆盖的最大集数；异常的 IndexNumberEnd 展开为千万级列表会耗尽内存。
_EMBY_EPISODE_RANGE_MAX_SPAN = 1000


def read_emby_query_items(
    request: "RequestUtils", url: str, params: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    读取存在性查询的条目列表，区分成功空列表与不可确认的响应。

    :param request: 保留当前媒体服务器认证配置的 HTTP 工具
    :param url: 标题搜索或分集查询地址
    :param params: 当前请求的查询条件与内容投影
    :return: 已验证的原始条目列表，成功查询无条目时为空列表
    :raises MediaServerQueryError: 无响应、非 200 状态或条目结构无效
    """
    response = request.get_res(url, params)
    if response is None:
        raise MediaServerQueryError("媒体库查询失败：未收到响应")
    try:
        if response.status_code != 200:
            raise MediaServerQueryError(f"媒体库查询失败：HTTP {response.status_code}")
        items = response.json().get("Items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise MediaServerQueryError("媒体库查询失败：条目列表格式无效")
        return items
    finally:
        response.close()


def _read_emby_item_page(
    request: "RequestUtils",
    url: str,
    params: dict[str, Any],
) -> tuple[list[dict[str, Any]], Optional[int]]:
    """
    读取一页原始条目，并在交给同步迭代器前释放 HTTP 响应及其字节缓冲。

    :param request: 对应服务器的请求工具，保留其请求头配置
    :param url: 当前用户的条目列表接口
    :param params: 包含目录、投影和分页范围的请求参数
    :return: 原始条目列表与服务端可选总数；总数包含随后可能被过滤的条目
    :raises ConnectionError: 请求无响应或返回非成功状态
    """
    response = request.get_res(url, params)
    if response is None:
        raise ConnectionError("读取媒体服务器条目失败，未收到响应")
    try:
        if response.status_code != 200:
            raise ConnectionError(f"读取媒体服务器条目失败，状态码：{response.status_code}")
        payload = response.json()
        total = payload.get("TotalRecordCount")
        return payload.get("Items") or [], int(total) if total is not None else None
    finally:
        response.close()


def iter_emby_items(
    request: "RequestUtils",
    url: str,
    params: Mapping[str, Any],
    *,
    start_index: Optional[int] = 0,
    limit: Optional[int] = -1,
) -> Generator[dict[str, Any], None, None]:
    """
    按服务端原始条目分页枚举 Emby 系目录，过滤和递归由客户端处理。

    :param request: 保留对应服务器请求头的 HTTP 工具
    :param url: 用户条目列表接口
    :param params: 当前目录、认证与查询投影；不修改调用方参数
    :param start_index: 显式分页的原始条目起始位置；全量枚举保持从 0 开始
    :param limit: None 或 -1 时以有界页读取全部条目，否则只读取调用方指定的一页
    :yields: 尚未经过类型过滤的原始条目
    :raises ConnectionError: 任意页读取失败；不把失败转换为空库
    """
    unlimited = limit is None or limit == -1
    page_size = _EMBY_ITEMS_PAGE_SIZE if unlimited else limit
    offset = 0 if unlimited else start_index or 0
    while True:
        items, total = _read_emby_item_page(
            request, url, {**params, "StartIndex": offset, "Limit": page_size},
        )
        count = len(items)
        yield from items
        # 在下一页请求前释放已消费的列表，不跨页累积原始 JSON。
        del items
        offset += count
        if not unlimited or not count:
            return
        if total is not None:
            if offset >= total:
                return
        elif count < _EMBY_ITEMS_PAGE_SIZE:
            return


def emby_episode_numbers(item: Mapping[str, Any]) -> range:
    """
    展开 Emby 系分集条目的起止集号，缺少结束集号、倒序或跨度异常时按单集处理。

    调用方会把区间物化为集号列表或映射，跨度超过 ``_EMBY_EPISODE_RANGE_MAX_SPAN``
    时视为服务端元数据异常，只保留起始集号并记录警告，避免同步时耗尽内存。

    :param item: 含 IndexNumber 与可空 IndexNumberEnd 的分集响应
    :return: 包含合并文件所覆盖全部集号的区间，未提供起始集号时为空
    """
    number = item.get("IndexNumber")
    if number is None:
        return range(0)
    start = int(number)
    end = item.get("IndexNumberEnd")
    stop = max(start, int(end) if end is not None else start) + 1
    if stop - start > _EMBY_EPISODE_RANGE_MAX_SPAN:
        logger.warning(
            f"媒体服务器分集 {item.get('Name') or item.get('Id')} 的集号范围异常："
            f"{start}-{end}，超过单个文件 {_EMBY_EPISODE_RANGE_MAX_SPAN} 集上限，按单集 {start} 处理，"
            "请检查媒体服务器中该分集的结束集号"
        )
        return range(start, start + 1)
    return range(start, stop)


def emby_query_params(query: Optional[MediaServerQueryOptions]) -> dict[str, str]:
    """
    把显式投影翻译为 Emby/Jellyfin 已公开的列表查询控制参数。

    名称、类型由服务端默认返回；年份必须通过 Fields 显式请求，避免精简查询丢失匹配和同步依据。
    Fields 只追加当前投影所需的字段。
    音乐完整性匹配需要 ChildCount，即使未请求额外统计也不能删除它。

    :param query: 调用方投影；None 或 fields=None 时不改变旧请求
    :return: 字段追加、图片信息和用户数据参数；不添加未公开的总量开关
    """
    if query is None or query.fields is None:
        return {}
    fields = {
        MediaServerField.LIBRARY: "ParentId",
        MediaServerField.ORIGINAL_TITLE: "OriginalTitle",
        MediaServerField.YEAR: "ProductionYear",
        MediaServerField.IDENTITY: "ProviderIds",
        MediaServerField.PATH: "Path",
        MediaServerField.MUSIC: "ChildCount",
    }
    return {
        "Fields": ",".join(value for field, value in fields.items() if query.includes(field)),
        "EnableImages": str(query.includes(MediaServerField.IMAGES)).lower(),
        "EnableUserData": str(query.includes(MediaServerField.USER_STATE)).lower(),
    }


class _MediaServerModuleBase(_ModuleBase, _MediaServerBase[TService]):
    """
    媒体服务器模块业务样板基类。
    """

    # 媒体库标识（用于 ExistMediaInfo.server_type，如 "emby"），子类覆写
    _server_type_value: str = ""
    # 仅已实现查询投影的适配器向原生客户端透传新参数。
    _query_options_supported: bool = False

    def mediaserver_stream_url(
        self,
        server: str,
        item_id: Union[str, int],
        item_type: Optional[str],
        media_source_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        委托客户端获取支持 Range 的原始媒体流地址；未实现时返回 None。

        :param server: 媒体服务器配置名称
        :param item_id: 媒体服务器条目标识
        :param item_type: 媒体服务器返回的条目类型
        :param media_source_id: 可选指定的多版本媒体源标识
        :return: 上游媒体流地址；当前适配器不支持时返回 None
        """
        server_obj = self.get_instance(server)
        getter = getattr(server_obj, "get_stream_url", None)
        if not callable(getter):
            return None
        try:
            if media_source_id:
                return cast(Optional[str], getter(str(item_id), item_type, media_source_id=media_source_id))
            return cast(Optional[str], getter(str(item_id), item_type))
        except TypeError:
            return cast(Optional[str], getter(str(item_id), item_type))

    def mediaserver_subtitle_url(
        self, server: str, item_id: Union[str, int], subtitle_id: str
    ) -> Optional[str]:
        """
        委托客户端解析服务端托管字幕的原始下载流地址。

        :param server: 媒体服务器配置名称
        :param item_id: 媒体服务器条目标识
        :param subtitle_id: 代理字幕标识
        :return: 上游字幕流地址；不支持或未匹配时返回 None
        """
        server_obj = self.get_instance(server)
        getter = getattr(server_obj, "get_subtitle_url", None)
        if not callable(getter):
            return None
        return cast(Optional[str], getter(str(item_id), subtitle_id))

    def mediaserver_browse_items(
        self,
        server: str,
        library_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        item_type: Optional[str] = None,
        genre: Optional[str] = None,
        year: Optional[int] = None,
        played_filter: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        keyword: Optional[str] = None,
        start_index: int = 0,
        limit: int = 30,
        username: Optional[str] = None,
    ) -> Optional[_SchemaMediaServerBrowsePage]:
        """
        分页浏览指定媒体服务器的媒体库内容并支持筛选、排序和搜索。

        :param server: 媒体服务器配置名称
        :param library_id: 可选媒体库标识
        :param parent_id: 可选父目录标识
        :param item_type: 可选条目类型过滤
        :param genre: 可选风格过滤
        :param year: 可选年份过滤
        :param played_filter: 可选观看状态过滤
        :param sort_by: 可选排序键
        :param sort_order: 可选排序方向
        :param keyword: 可选关键词搜索
        :param start_index: 零基分页偏移
        :param limit: 单页数量上限
        :param username: 可选用户名
        :return: 分页浏览模型；实例不存在或不支持时返回 None
        """
        server_obj = self.get_instance(server)
        browser = getattr(server_obj, "browse_items", None)
        if not callable(browser):
            return None
        page = cast(
            Optional[_SchemaMediaServerBrowsePage],
            browser(
                library_id=library_id,
                parent_id=parent_id,
                item_type=item_type,
                genre=genre,
                year=year,
                played_filter=played_filter,
                sort_by=sort_by,
                sort_order=sort_order,
                keyword=keyword,
                start_index=start_index,
                limit=limit,
                username=username,
            ),
        )
        if page:
            for item in page.items:
                item.server = server
        return page

    def mediaserver_filter_options(
        self,
        server: str,
        library_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[_SchemaMediaServerFilterOptions]:
        """
        读取指定媒体库可用的风格、年份与排序筛选选项。

        :param server: 媒体服务器配置名称
        :param library_id: 可选媒体库标识
        :param username: 可选用户名
        :return: 筛选项模型；实例不存在或不支持时返回 None
        """
        server_obj = self.get_instance(server)
        getter = getattr(server_obj, "get_filter_options", None)
        if not callable(getter):
            return None
        return cast(
            Optional[_SchemaMediaServerFilterOptions],
            getter(library_id=library_id, username=username),
        )

    def mediaserver_item_detail(
        self,
        server: str,
        item_id: Union[str, int],
        username: Optional[str] = None,
    ) -> Optional[_SchemaMediaServerItemDetail]:
        """
        读取媒体服务器单个条目的完整详情（含多版本及音轨字幕轨道）。

        :param server: 媒体服务器配置名称
        :param item_id: 媒体服务器条目标识
        :param username: 可选用户名
        :return: 详情模型；实例或条目不存在时返回 None
        """
        server_obj = self.get_instance(server)
        getter = getattr(server_obj, "get_item_detail", None)
        if not callable(getter):
            return None
        detail = cast(
            Optional[_SchemaMediaServerItemDetail],
            getter(str(item_id), username=username),
        )
        if detail:
            detail.server = server
        return detail

    def mediaserver_seasons(
        self,
        server: str,
        item_id: Union[str, int],
        username: Optional[str] = None,
    ) -> Optional[list[_SchemaMediaServerBrowseItem]]:
        """
        读取电视剧的全部季卡片信息。

        :param server: 媒体服务器配置名称
        :param item_id: 电视剧条目标识
        :param username: 可选用户名
        :return: 季卡片列表；实例不存在或不支持时返回 None
        """
        server_obj = self.get_instance(server)
        getter = getattr(server_obj, "get_seasons", None)
        if not callable(getter):
            return None
        seasons = cast(
            Optional[list[_SchemaMediaServerBrowseItem]],
            getter(str(item_id), username=username),
        )
        if seasons is not None:
            for season_item in seasons:
                season_item.server = server
        return seasons

    def mediaserver_season_episodes(
        self,
        server: str,
        item_id: Union[str, int],
        season_id: Optional[str] = None,
        season: Optional[int] = None,
        username: Optional[str] = None,
    ) -> Optional[list[_SchemaMediaServerBrowseItem]]:
        """
        读取电视剧指定季的全部单集详情卡片。

        :param server: 媒体服务器配置名称
        :param item_id: 电视剧条目标识
        :param season_id: 可选季条目标识
        :param season: 可选季号
        :param username: 可选用户名
        :return: 单集卡片列表；实例不存在或不支持时返回 None
        """
        server_obj = self.get_instance(server)
        getter = getattr(server_obj, "get_season_episodes", None)
        if not callable(getter):
            return None
        episodes = cast(
            Optional[list[_SchemaMediaServerBrowseItem]],
            getter(str(item_id), season_id=season_id, season=season, username=username),
        )
        if episodes is not None:
            for ep in episodes:
                ep.server = server
        return episodes

    def mediaserver_report_progress(
        self,
        server: str,
        item_id: Union[str, int],
        position_ms: int,
        media_source_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        is_paused: bool = False,
        state: str = "playing",
        username: Optional[str] = None,
    ) -> Optional[bool]:
        """
        向媒体服务器回传客户端的播放进度状态。

        :param server: 媒体服务器配置名称
        :param item_id: 条目标识
        :param position_ms: 当前播放位置（毫秒）
        :param media_source_id: 可选媒体源标识
        :param duration_ms: 可选总时长（毫秒）
        :param is_paused: 是否暂停
        :param state: 事件状态：playing / paused / stopped
        :param username: 可选用户名
        :return: 服务端回传成功返回 True；不支持时返回 None
        """
        server_obj = self.get_instance(server)
        reporter = getattr(server_obj, "report_progress", None)
        if not callable(reporter):
            return None
        return cast(
            bool,
            reporter(
                str(item_id),
                position_ms=position_ms,
                media_source_id=media_source_id,
                duration_ms=duration_ms,
                is_paused=is_paused,
                state=state,
                username=username,
            ),
        )

    def user_authenticate(
            self,
            credentials: _SchemaAuthCredentials,
            service_name: Optional[str] = None,
    ) -> Optional[_SchemaAuthCredentials]:
        """
        使用媒体服务器用户辅助完成用户认证。

        :param credentials: 认证数据
        :param service_name: 指定要认证的媒体服务器名称，若为 None 则认证所有服务器
        :return: 成功时返回已写入渠道、服务名和令牌的原凭据；不适用或所有服务认证失败时返回 None
        """
        if not credentials or credentials.grant_type != "password":
            return None
        # 确定要认证的服务器列表
        servers: Iterable[tuple[Optional[str], TService]]
        if service_name:
            # 如果指定了服务名，获取该服务实例
            servers = (
                [(service_name, server)]
                if (server := self.get_instance(service_name))
                else []
            )
        else:
            # 如果没有指定服务名，遍历所有服务
            servers = self.get_instances().items()
        # 遍历要认证的服务器
        for name, server in servers:
            service_name = name or ""
            channel_name = self.get_name() or ""
            # 触发认证拦截事件
            intercept_event = eventmanager.send_event(
                etype=ChainEventType.AuthIntercept,
                data=_SchemaAuthInterceptCredentials(
                    username=credentials.username,
                    channel=channel_name,
                    service=service_name,
                    status="triggered",
                ),
            )
            if intercept_event and intercept_event.event_data:
                intercept_data = (
                    intercept_event.event_data
                    if isinstance(intercept_event.event_data, _SchemaAuthInterceptCredentials)
                    else _SchemaAuthInterceptCredentials.model_validate(intercept_event.event_data)
                )
                if intercept_data.cancel:
                    continue
            token = cast(Any, server).authenticate(credentials.username, credentials.password)
            if token:
                credentials.channel = channel_name
                credentials.service = name
                credentials.token = token
                return credentials
        return None

    def media_exists(
        self,
        mediainfo: MediaInfo,
        itemid: Optional[str] = None,
        server: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
        raise_on_query_failure: bool = False,
    ) -> Optional[_SchemaExistMediaInfo]:
        """
        判断媒体文件是否存在。

        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :param server:  媒体服务器名称
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :param raise_on_query_failure: 剧集查询失败时抛 MediaServerQueryError 而不是返回 None；
            默认 False 保持既有行为。仅电视剧分支区分失败与未找到：各剧集服务约定查询失败
            返回两项 None，未找到返回 None 和空映射；电影/音乐服务无法区分两种情况，
            不受该参数影响
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        :raises MediaServerQueryError: 剧集查询失败且 raise_on_query_failure 为 True，
            且没有服务器确认媒体存在时
        """
        if not self._query_options_supported:
            query = None
        servers: Iterable[tuple[Optional[str], Optional[TService]]]
        if server:
            servers = [(server, self.get_instance(server))]
        else:
            servers = self.get_instances().items()
        query_failed = False
        failed_servers: list[str] = []
        for name, s in servers:
            if not s:
                continue
            if mediainfo.type == MediaType.MUSIC:
                # 部分服务器未实现音乐查询，退化为空列表
                if not isinstance(mediainfo, MusicInfo):
                    continue
                matches: list[Any] = getattr(s, "get_music", lambda **_: [])(
                    **MusicMediaServerHelper.search_params(mediainfo),
                    **({"query": query.with_fields(MediaServerField.MUSIC)} if query is not None else {}),
                )
                match = MusicMediaServerHelper.find_match(mediainfo, matches)
                if match:
                    return _SchemaExistMediaInfo(
                        type=MediaType.MUSIC,
                        server_type=self._server_type_value,
                        server=name,
                        itemid=match.item_id,
                    )
                continue
            service = cast(Any, s)
            if mediainfo.type == MediaType.MOVIE:
                if itemid:
                    movie = service.get_iteminfo(itemid, **({"query": query} if query is not None else {}))
                    if movie:
                        logger.info(f"媒体库 {name} 中找到了 {movie}")
                        return _SchemaExistMediaInfo(
                            type=MediaType.MOVIE, server_type=self._server_type_value, server=name, itemid=movie.item_id
                        )
                movies = service.get_movies(
                    title=mediainfo.title,
                    year=mediainfo.year,
                    media_source=mediainfo.media_source,
                    media_id=mediainfo.media_id,
                    **({"query": query} if query is not None else {}),
                )
                if not movies:
                    logger.info(f"{mediainfo.title_year} 没有在媒体库 {name} 中")
                    continue
                else:
                    logger.info(f"媒体库 {name} 中找到了 {movies}")
                    return _SchemaExistMediaInfo(
                        type=MediaType.MOVIE, server_type=self._server_type_value, server=name, itemid=movies[0].item_id
                    )
            else:
                itemid, tvs = service.get_tv_episodes(
                    title=mediainfo.title,
                    year=mediainfo.year,
                    media_source=mediainfo.media_source,
                    media_id=mediainfo.media_id,
                    item_id=itemid,
                    **({"query": query} if query is not None else {}),
                )
                if tvs is None:
                    # 查询失败（返回两项 None）与未找到（返回 None 和空映射）必须区分：
                    # 前者不得被当作缺失，否则一次读取失败会触发整季重复下载。
                    query_failed = True
                    failed_servers.append(str(name))
                    logger.warning(f"{mediainfo.title_year} 查询媒体库 {name} 失败，无法确认是否已入库")
                    continue
                if not tvs:
                    logger.info(f"{mediainfo.title_year} 没有在媒体库 {name} 中")
                    continue
                else:
                    logger.info(f"{mediainfo.title_year} 在媒体库 {name} 中找到 了这些季集：{tvs}")
                    return _SchemaExistMediaInfo(
                        type=MediaType.TV, seasons=tvs, server_type=self._server_type_value, server=name, itemid=itemid
                    )
        if query_failed and raise_on_query_failure:
            raise MediaServerQueryError(
                f"媒体库查询失败，未能确认 {mediainfo.title_year} 是否已入库：{', '.join(failed_servers)}"
            )
        return None

    def scheduler_job(self) -> None:
        """
        巡检已配置媒体服务器，对失活连接发起重连。

        由模块调度器每十分钟调用一次。
        """
        # 定时重连
        for name, server in self.get_instances().items():
            if self._is_inactive(server):
                logger.info(f"{self.get_name()}服务器 {name} 连接断开，尝试重连 ...")
                cast(Any, server).reconnect()

    def _is_inactive(self, server: Any) -> bool:
        """
        定时重连的失活判断钩子，子类可覆写（如增加配置完整性检查）。
        """
        return bool(server.is_inactive())

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        检查已配置媒体服务器，遇到首个不可用实例即返回失败。

        存在初始化失败的服务器配置时优先返回具体错误，避免其余实例可用时掩盖故障。

        :return: 是否就绪与失败说明；没有实例且无初始化错误时返回 None
        """
        init_errors = self.get_init_errors()
        if init_errors:
            init_name, init_error = next(iter(init_errors.items()))
            return False, f"初始化{self.get_name()}服务器失败：{init_name or '未命名'}（{init_error}）"
        if not self.get_instances():
            return None
        for name, server in self.get_instances().items():
            error = self._test_server(server, name)
            if error:
                return False, error
        return True, ""

    def _test_server(self, server: Any, name: Optional[str]) -> Optional[str]:
        """
        连接测试钩子，返回失败信息，None 表示就绪，子类可覆写。
        """
        if server.is_inactive():
            server.reconnect()
        if not server.get_user():
            return f"无法连接{self.get_name()}服务器：{name}"
        return None
