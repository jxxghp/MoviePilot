"""
绿联媒体服务器客户端，协调内部加密接口、媒体库兼容接口及会话缓存。
"""

import hashlib
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Mapping, Optional, Union
from urllib.parse import parse_qs, urlparse

from app.application.configuration import get_configured_system_config
from app.application.mediaserver import (
    MediaServerIdentityHelper,
    MusicMediaServerHelper,
    format_emby_family_item,
)
from app.foundation.url import UrlUtils
from app.modules.ugreen.api import Api, UgreenMediaApi
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerItemUserState as _SchemaMediaServerItemUserState
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.mediaserver import RefreshMediaItem as _SchemaRefreshMediaItem
from app.schemas.mediaserver import WebhookEventInfo as _SchemaWebhookEventInfo
from app.schemas.types import MediaSource, MediaType, SystemConfigKey


def _media_service_items(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """
    校验媒体库服务列表，保留合法空数组并拒绝未知响应结构。
    """
    items = data.get("Items")
    if not isinstance(items, list):
        raise ConnectionError("绿联媒体库服务列表响应格式错误")
    return items


class Ugreen:
    """
    绿联影视媒体服务器客户端。
    """

    # 目录索引最多读取 200 页，防止固件错误的分页标志导致无限循环。
    LIBRARY_PATH_PAGE_LIMIT = 200
    # UGOS Pro 独立媒体库服务的固定 HTTPS 端口。
    MEDIA_SERVICE_PORT = 9443
    INTERNAL_CONNECTION_MODE = "internal"
    MEDIA_SERVICE_CONNECTION_MODE = "media_service"

    _username: Optional[str] = None
    _password: Optional[str] = None

    # 用户资料、媒体库和会话负载沿用 API 的原始 JSON 字典合同。
    _userinfo: Optional[dict[str, Any]] = None
    _host: Optional[str] = None
    _playhost: Optional[str] = None

    _libraries: dict[str, dict[str, Any]] = {}
    _library_paths: dict[str, str] = {}
    _sync_libraries: List[str] = []
    _scan_type: int = 2
    _verify_ssl: bool = True
    _connection_mode: str = INTERNAL_CONNECTION_MODE

    _api: Optional[Api] = None
    _media_api: Optional[UgreenMediaApi] = None

    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        play_host: Optional[str] = None,
        sync_libraries: Optional[list[str]] = None,
        scan_mode: Optional[Union[str, int]] = None,
        scan_type: Optional[Union[str, int]] = None,
        verify_ssl: Optional[Union[bool, str, int]] = True,
        mode: Optional[str] = None,
        **kwargs: object,
    ) -> None:
        """
        保存连接配置，并按端口或显式模式连接绿联影视服务。

        地址或用户名密码缺失时不连接；内部接口优先复用持久化会话，媒体库服务重新认证。

        :param host: 服务地址，9443 端口默认使用媒体库服务兼容接口
        :param username: 登录用户名
        :param password: 登录密码
        :param play_host: 用于播放跳转和远程图片的外网地址
        :param sync_libraries: 同步范围内的媒体库标识，空列表或包含 all 表示不限制
        :param scan_mode: 扫描模式名称或编号，缺省使用补充缺失模式
        :param scan_type: 显式扫描类型编号 1、2 或 3，优先于 scan_mode
        :param verify_ssl: 是否验证证书；None 或无法识别的值默认启用
        :param mode: 连接模式，支持 internal 和 media_service 及兼容别名；无有效显式值时按端口识别
        :param kwargs: 通用服务配置的额外参数，当前不读取
        """
        if not host or not username or not password:
            logger.error("绿联影视配置不完整！！")
            return

        self._host = host
        self._username = username
        self._password = password
        self._connection_mode = self.__resolve_connection_mode(host, mode)
        self._sync_libraries = sync_libraries or []
        # 绿联媒体库扫描模式：
        # 1 新添加和修改、2 补充缺失、3 覆盖扫描
        self._scan_type = self.__resolve_scan_type(scan_mode=scan_mode, scan_type=scan_type)
        # HTTPS 证书校验开关：默认开启，仅兼容自签证书等场景下可关闭。
        self._verify_ssl = self.__resolve_verify_ssl(verify_ssl)

        if play_host:
            self._playhost = UrlUtils.standardize_base_url(play_host).rstrip("/")

        if not self.reconnect():
            logger.error(f"请检查服务端地址 {host}")

    @property
    def api(self) -> Optional[Api]:
        """
        读取内部加密接口客户端。

        :return: 内部接口客户端；媒体库服务模式或未连接时为 None
        """
        return self._api

    def close(self) -> None:
        """
        关闭绿联影视连接。
        """
        self.disconnect()

    def is_configured(self) -> bool:
        """
        检查绿联影视连接配置是否完整。
        """
        return bool(self._host and self._username and self._password)

    def is_authenticated(self) -> bool:
        """
        检查绿联影视会话是否已认证。
        """
        if self.__is_media_service_mode():
            return bool(
                self.is_configured()
                and self._media_api is not None
                and self._media_api.token
                and self._media_api.user_id
            )
        return (
            self.is_configured()
            and self._api is not None
            and self._api.token is not None
            and self._userinfo is not None
        )

    def is_inactive(self) -> bool:
        """
        验证当前会话，并更新实例缓存的用户资料。

        :return: 尚未认证或当前用户查询失败时返回 True；此检查会调用服务端接口
        """
        if not self.is_authenticated():
            return True
        if self.__is_media_service_mode():
            self._userinfo = self._media_api.current_user() if self._media_api else None
            return self._userinfo is None
        self._userinfo = self._api.current_user() if self._api else None
        return self._userinfo is None

    @staticmethod
    def __resolve_connection_mode(host: str, mode: Optional[str] = None) -> str:
        """
        解析绿联连接模式。

        显式配置优先；未配置时按 UGOS Pro 媒体库服务固定使用的 9443 端口
        自动选择 Emby 兼容接口，其他地址继续使用原有内部接口。
        """
        configured_mode = str(mode or "").strip().lower()
        if configured_mode in {
            Ugreen.MEDIA_SERVICE_CONNECTION_MODE,
            "media-server",
            "media_server",
            "emby",
        }:
            return Ugreen.MEDIA_SERVICE_CONNECTION_MODE
        if configured_mode in {Ugreen.INTERNAL_CONNECTION_MODE, "ugreen"}:
            return Ugreen.INTERNAL_CONNECTION_MODE
        try:
            normalized_host = UrlUtils.standardize_base_url(host)
            if urlparse(normalized_host).port == Ugreen.MEDIA_SERVICE_PORT:
                return Ugreen.MEDIA_SERVICE_CONNECTION_MODE
        except ValueError:
            pass
        return Ugreen.INTERNAL_CONNECTION_MODE

    def __is_media_service_mode(self) -> bool:
        """
        判断当前实例是否使用媒体库服务 Emby 兼容接口。
        """
        return getattr(self, "_connection_mode", self.INTERNAL_CONNECTION_MODE) == self.MEDIA_SERVICE_CONNECTION_MODE

    def __session_cache_key(self) -> str:
        """
        生成当前绿联实例的会话缓存键（基于 host、username 和连接模式）。
        """
        normalized_host = UrlUtils.standardize_base_url(self._host or "").rstrip("/").lower()
        username = (self._username or "").strip().lower()
        raw = f"{normalized_host}|{username}"
        if self.__is_media_service_mode():
            raw = f"{raw}|{self.MEDIA_SERVICE_CONNECTION_MODE}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def __password_digest(self) -> str:
        """
        存储密码摘要用于检测配置是否变更，避免明文落盘。
        """
        return hashlib.sha256((self._password or "").encode("utf-8")).hexdigest()

    @staticmethod
    def __load_all_session_cache() -> dict[str, Any]:
        """
        读取系统配置中保存的所有绿联会话。

        :return: 会话缓存字典；配置不存在或不是字典时返回空字典
        """
        sessions = get_configured_system_config().get(SystemConfigKey.UgreenSessionCache)
        return sessions if isinstance(sessions, dict) else {}

    @staticmethod
    def __save_all_session_cache(sessions: dict[str, Any]) -> None:
        """
        把所有绿联实例的会话字典写回系统配置。

        :param sessions: 完整会话缓存映射；本方法替换该配置值，不只更新单个实例
        """
        get_configured_system_config().set(SystemConfigKey.UgreenSessionCache, sessions)

    def __remove_persisted_session(self) -> None:
        """
        从持久化会话映射中删除当前地址和账号对应的记录。
        """
        cache_key = self.__session_cache_key()
        sessions = self.__load_all_session_cache()
        if cache_key in sessions:
            sessions.pop(cache_key, None)
            self.__save_all_session_cache(sessions)

    def __save_persisted_session(self) -> None:
        """
        保存内部接口会话、Cookie、密码摘要和更新时间。

        仅在内部 API 能导出有效会话时写入系统配置；其他实例的记录保留。
        """
        if not self._api:
            return
        session_state = self._api.export_session_state()
        if not session_state:
            return

        sessions = self.__load_all_session_cache()
        cache_key = self.__session_cache_key()
        sessions[cache_key] = {
            **session_state,
            "host": UrlUtils.standardize_base_url(self._host or "").rstrip("/"),
            "username": self._username,
            "password_digest": self.__password_digest(),
            "updated_at": int(datetime.now().timestamp()),
        }
        self.__save_all_session_cache(sessions)

    def __restore_persisted_session(self, host: str) -> bool:
        """
        验证缓存会话的密码摘要和服务端用户状态，成功后接管客户端。

        密码变更或会话失效时删除旧记录；验证失败后关闭临时客户端，调用方可继续密码登录。

        :param host: 重连入口已校验的服务地址
        :return: 恢复并验证成功时返回 True；记录缺失、配置变更或会话失效时返回 False
        """
        cache_key = self.__session_cache_key()
        sessions = self.__load_all_session_cache()
        cached = sessions.get(cache_key)
        if not isinstance(cached, Mapping):
            return False

        # 配置变更（尤其密码变更）后，不复用旧会话
        if cached.get("password_digest") != self.__password_digest():
            logger.info(f"绿联影视 {self._username} 检测到密码变更，清理旧会话缓存")
            self.__remove_persisted_session()
            return False

        api = Api(host=host, verify_ssl=self._verify_ssl)
        if not api.import_session_state(cached):
            api.close()
            self.__remove_persisted_session()
            return False

        userinfo = api.current_user()
        if not userinfo:
            # 会话失效，清理缓存并走正常登录
            api.close()
            self.__remove_persisted_session()
            logger.info(f"绿联影视 {self._username} 持久化会话已失效，准备重新登录")
            return False

        self._api = api
        self._userinfo = userinfo
        logger.debug(f"{self._username} 已复用绿联影视持久化会话")
        return True

    def reconnect(self) -> bool:
        """
        关闭旧客户端并按配置重新认证，重置媒体库与目录缓存。

        关闭旧连接时不主动登出，以保留可复用会话。内部接口成功登录后保存新会话，
        媒体库服务模式不使用内部接口的会话持久化流程。

        :return: 认证并获取必要用户信息后返回 True；配置缺失或认证失败时返回 False
        """
        if not self.is_configured():
            return False

        media_host = self._host
        media_username = self._username
        media_password = self._password
        if not media_host or not media_username or not media_password:
            return False

        self._libraries = {}
        self._library_paths = {}

        # 关闭旧连接（不主动登出，避免破坏可复用会话）
        self.disconnect(logout=False)

        if self.__is_media_service_mode():
            media_api = UgreenMediaApi(
                host=media_host,
                verify_ssl=self._verify_ssl,
            )
            session = media_api.authenticate(media_username, media_password)
            if session is None:
                media_api.close()
                return False
            self._media_api = media_api
            self._userinfo = session.user or {
                "Id": session.user_id,
                "Name": media_username,
            }
            logger.debug(f"{self._username} 成功登录绿联媒体库服务")
            return True

        if self.__restore_persisted_session(media_host):
            return True

        self._api = Api(host=media_host, verify_ssl=self._verify_ssl)
        if self._api.login(media_username, media_password) is None:
            self.__remove_persisted_session()
            return False

        self._userinfo = self._api.current_user()
        if not self._userinfo:
            self.__remove_persisted_session()
            return False

        # 登录成功后持久化参数，下次优先复用
        self.__save_persisted_session()
        logger.debug(f"{self._username} 成功登录绿联影视")
        return True

    def disconnect(self, logout: bool = False) -> None:
        """
        关闭客户端并清空内存中的用户、媒体库与路径状态。

        媒体库服务模式只关闭 HTTP 会话，不调用服务端登出接口。

        :param logout: 是否注销内部接口会话并删除持久化记录；默认保留供重连复用
        """
        if self._media_api:
            self._media_api.close()
            self._media_api = None
            self._userinfo = None
            logger.debug(f"{self._username} 已断开绿联媒体库服务")
        if self._api:
            if logout:
                # 显式登出时同步清理本地缓存
                self._api.logout()
                self.__remove_persisted_session()
            self._api.close()
            self._api = None
            self._userinfo = None
            logger.debug(f"{self._username} 已断开绿联影视")
        self._libraries = {}
        self._library_paths = {}

    @staticmethod
    def __normalize_dir_path(path: Union[str, Path, None]) -> str:
        """
        将目录分隔符统一为斜杠，并去除尾部斜杠。

        :param path: 字符串或路径对象；None 转为空字符串
        :return: 用于字符串层级匹配的路径，不访问文件系统或解析符号链接
        """
        if path is None:
            return ""
        value = str(path).replace("\\", "/").rstrip("/")
        return value

    @staticmethod
    def __is_subpath(path: Union[str, Path, None], parent: Union[str, Path, None]) -> bool:
        """
        按路径分隔符判断目标是否等于或位于父目录之下。

        :param path: 待判断的目标路径
        :param parent: 父目录路径
        :return: 两个路径都非空且目标属于父目录时返回 True；比较区分大小写
        """
        path_str = Ugreen.__normalize_dir_path(path)
        parent_str = Ugreen.__normalize_dir_path(parent)
        if not path_str or not parent_str:
            return False
        return path_str == parent_str or path_str.startswith(parent_str + "/")

    def __build_image_stream_url(self, source_url: str, size: int = 1) -> Optional[str]:
        """
        通过绿联 getImaStream 中转图片，规避 scraper.ugnas.com 403 问题。
        """
        if not self._api:
            return None

        auth_token = self._api.static_token or self._api.token
        if not auth_token:
            return None

        params = {
            "app_name": "web",
            "name": source_url,
            "size": size,
        }
        if self._api.is_ugk:
            params["ugk"] = auth_token
        else:
            params["token"] = auth_token

        return UrlUtils.combine_url(
            host=self._api.host,
            path="/ugreen/v2/video/getImaStream",
            query=params,
        )

    def __resolve_image(self, path: Optional[str]) -> Optional[str]:
        """
        将图片地址转换为可用的外链或绿联图片中转地址。

        scraper.ugnas.com 图片优先经本机 getImaStream 代理；普通外部 HTTP 地址保持原样。

        :param path: 服务端原始图片地址或路径
        :return: 可用 URL；过期签名且无法中转、相对路径或空值时返回 None
        """
        if not path:
            return None
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            if parsed.netloc.lower() == "scraper.ugnas.com":
                # scraper 链接优先改为本机 getImaStream，避免签名过期导致 403
                if image_stream_url := self.__build_image_stream_url(path):
                    return image_stream_url

            # 绿联返回的 scraper.ugnas.com 图片常带 auth_key 时效签名，
            # 过期后会直接 403。这里提前过滤，避免前端出现裂图。
            if self.__is_expired_signed_image(path):
                return None
            return path
        # 绿联本地图片路径需要额外鉴权头，MP图片代理当前仅支持Cookie，故先忽略本地路径。
        return None

    @staticmethod
    def __is_expired_signed_image(url: str) -> bool:
        """
        判断绿联 scraper 签名图是否已过期。

        auth_key 结构通常为：
        `{过期时间戳}-{随机串}-...`
        """
        try:
            parsed = urlparse(url)
            if parsed.netloc.lower() != "scraper.ugnas.com":
                return False
            auth_key = parse_qs(parsed.query).get("auth_key", [None])[0]
            if not auth_key:
                return False
            expire_part = str(auth_key).split("-", 1)[0]
            expire_ts = int(expire_part)
            now_ts = int(datetime.now().timestamp())
            return expire_ts <= now_ts
        except Exception:
            return False

    @staticmethod
    def __parse_year(video_info: dict[str, Any]) -> Optional[Union[str, int]]:
        """
        读取有效年份，缺失时从发行时间戳推断。

        :param video_info: 内部接口的视频信息字典
        :return: 正整数年份；只有正的发行秒时间戳时按本地时区取年，无法转换时返回 None
        """
        year = video_info.get("year")
        if isinstance(year, int) and year > 0:
            return year
        release_date = video_info.get("release_date")
        if isinstance(release_date, (int, float)) and release_date > 0:
            try:
                return datetime.fromtimestamp(release_date).year
            except Exception:
                return None
        return None

    @staticmethod
    def __map_item_type(video_type: Any) -> Optional[str]:
        """
        将绿联内部数值类型映射为统一条目类型。

        :param video_type: 0 为目录，1 为电影，2 为剧集，3 为合集
        :return: Folder、Movie、Series 或 Collection；其他值统一返回 Video
        """
        if video_type == 2:
            return "Series"
        if video_type == 1:
            return "Movie"
        if video_type == 3:
            return "Collection"
        if video_type == 0:
            return "Folder"
        return "Video"

    @staticmethod
    def __build_media_server_item(
        video_info: dict[str, Any], play_status: Optional[dict[str, Any]] = None,
    ) -> Optional[_SchemaMediaServerItem]:
        """
        将内部视频信息和播放状态转换为统一媒体模型。

        :param video_info: 含 ug_video_info_id、名称、类型及可选 TMDB 身份的视频信息
        :param play_status: 可选播放状态，progress 为 0 至 1 的比例，watch_status=2 表示已看完
        :return: 统一媒体条目；缺少 ug_video_info_id 时返回 None，仅正整数 tmdb_id 被接受为来源身份
        """
        user_state = _SchemaMediaServerItemUserState()
        if isinstance(play_status, dict):
            progress = play_status.get("progress")
            watch_status = play_status.get("watch_status")
            if watch_status == 2:
                user_state.played = True
            if isinstance(progress, (int, float)) and progress > 0:
                user_state.resume = progress < 1
                user_state.percentage = progress * 100.0
            last_play_time = play_status.get("last_access_time") or play_status.get("LastPlayTime")
            if isinstance(last_play_time, (int, float)) and last_play_time > 0:
                user_state.last_played_date = str(int(last_play_time))

        tmdb_id = video_info.get("tmdb_id")
        if not isinstance(tmdb_id, int) or tmdb_id <= 0:
            tmdb_id = None

        item_id = video_info.get("ug_video_info_id")
        if item_id is None:
            return None

        return _SchemaMediaServerItem(
            server="ugreen",
            library=video_info.get("media_lib_set_id"),
            item_id=str(item_id),
            item_type=Ugreen.__map_item_type(video_info.get("type")),
            title=video_info.get("name"),
            original_title=video_info.get("original_name"),
            year=Ugreen.__parse_year(video_info),
            media_source=MediaSource.TMDB if tmdb_id else None,
            media_id=str(tmdb_id) if tmdb_id else None,
            user_state=user_state,
        )

    def __build_root_url(self) -> str:
        """
        统一返回 NAS Web 根地址作为跳转链接，避免失效深链。
        """
        host = self._playhost or (
            self._media_api.host
            if self._media_api
            else self._api.host
            if self._api
            else ""
        )
        if not host:
            return ""
        return f"{host.rstrip('/')}/"

    def __build_play_url(self, item_id: Union[str, int], video_type: Any, media_lib_set_id: Any) -> str:
        """
        按连接模式生成播放跳转地址。

        :param item_id: 媒体库服务的条目标识
        :param video_type: 兼容内部调用的类型参数，当前不参与链接构造
        :param media_lib_set_id: 兼容内部调用的媒体库标识，当前不参与链接构造
        :return: 媒体库模式返回条目网页链接；内部模式因深链不稳定回退 NAS 根地址
        """
        if self.__is_media_service_mode() and self._media_api:
            host = self._playhost or self._media_api.host
            server_id = self._media_api.server_id
            server_query = f"&serverId={server_id}" if server_id else ""
            return f"{host.rstrip('/')}/web/index.html#!/item?id={item_id}&context=home{server_query}"
        # 绿联深链在部分版本会失效，统一回落到 NAS 根地址。
        return self.__build_root_url()

    def __format_media_service_item(
        self,
        item: Mapping[str, Any],
    ) -> Optional[_SchemaMediaServerItem]:
        """
        把媒体库服务返回的 Emby 条目转换为绿联媒体条目模型。
        """
        if not isinstance(item, Mapping):
            return None
        media_item = format_emby_family_item(
            item,
            server="ugreen",
            include_server_id=True,
        )
        if media_item and self._media_api and not media_item.server_id:
            media_item.server_id = self._media_api.server_id
        return media_item

    @staticmethod
    def __media_service_library_type(collection_type: Any) -> str:
        """
        把 Emby 媒体库集合类型转换为 MoviePilot 媒体类型。
        """
        normalized_type = str(collection_type or "").strip().lower()
        if normalized_type == "movies":
            return MediaType.MOVIE.value
        if normalized_type in {"tvshows", "tv", "series"}:
            return MediaType.TV.value
        if normalized_type in {"music", "musicvideos", "audio"}:
            return MediaType.MUSIC.value
        return MediaType.UNKNOWN.value

    def __build_media_service_library_link(self, library_id: Union[str, int]) -> str:
        """
        生成媒体库服务媒体库视图的 Emby 网页链接。
        """
        if not self._media_api:
            return self.__build_root_url()
        host = self._playhost or self._media_api.host
        server_id = self._media_api.server_id
        server_query = f"serverId={server_id}&" if server_id else ""
        return f"{host.rstrip('/')}/web/index.html#!/videos?{server_query}parentId={library_id}"

    def __get_media_service_librarys(self, hidden: Optional[bool] = False) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        获取媒体库服务 Emby 兼容接口返回的媒体库列表。

        绿联媒体库服务不实现 `GET /Users`，因此直接使用认证响应中的用户 ID
        查询 `Users/{id}/Views`，避免沿用 Emby 适配器的全局用户枚举前置条件。
        """
        if not self.is_authenticated() or not self._media_api:
            return None
        source_libraries = self._media_api.views()
        if source_libraries is None:
            return None

        self._libraries = {}
        libraries: List[_SchemaMediaServerLibrary] = []
        for library in source_libraries:
            library_id = str(library.get("Id") or "").strip()
            if not library_id or (hidden and self.__is_library_blocked(library_id)):
                continue
            library_path = library.get("Path") or library.get("Locations")
            library_type = self.__media_service_library_type(library.get("CollectionType"))
            if library_type == MediaType.UNKNOWN.value:
                library_type = self.__infer_library_type(library.get("Name") or "", str(library_path or ""))
            self._libraries[library_id] = {
                "id": library_id,
                "name": library.get("Name") or "",
                "path": library_path,
                "type": library_type,
                "video_count": 0,
            }
            item_count = self.get_items_count(library_id)
            self._libraries[library_id]["video_count"] = item_count or 0
            libraries.append(
                _SchemaMediaServerLibrary(
                    server="ugreen",
                    id=library_id,
                    item_id=library_id,
                    server_id=library.get("ServerId") or self._media_api.server_id,
                    name=library.get("Name"),
                    path=library_path,
                    type=library_type,
                    item_count=item_count,
                    image=self.__build_media_service_image_url(library_id),
                    link=self.__build_media_service_library_link(library_id),
                    server_type="ugreen",
                    use_cookies=False,
                )
            )
        return libraries

    def __build_media_service_play_item(self, item: Mapping[str, Any]) -> Optional[_SchemaMediaServerPlayItem]:
        """
        把媒体库服务的继续观看或最近入库条目转换为播放模型。
        """
        if not self._media_api or not isinstance(item, Mapping):
            return None
        item_id = item.get("Id")
        item_type = item.get("Type")
        if not item_id or item_type not in {"Movie", "Series", "Episode"}:
            return None

        server_id = item.get("ServerId") or self._media_api.server_id
        user_data_value = item.get("UserData")
        user_data: Mapping[str, Any] = user_data_value if isinstance(user_data_value, Mapping) else {}
        subtitle: Optional[str]
        if item_type == "Episode":
            title = item.get("SeriesName") or item.get("Name")
            subtitle = f"S{item.get('ParentIndexNumber')}:{item.get('IndexNumber')} - {item.get('Name')}"
            image_id = item.get("SeriesId") or item_id
        else:
            title = item.get("Name")
            subtitle = str(item.get("ProductionYear")) if item.get("ProductionYear") else None
            image_id = item_id
        image_type = "Backdrop" if item.get("BackdropImageTags") else "Primary"
        item_media_type = MediaType.TV.value if item_type in {"Series", "Episode"} else MediaType.MOVIE.value
        return _SchemaMediaServerPlayItem(
            id=item_id,
            item_id=item_id,
            server_id=server_id,
            title=title,
            subtitle=subtitle,
            type=item_media_type,
            image=self.__build_media_service_image_url(image_id, image_type, remote=True),
            link=self.__build_play_url(item_id, item_type, item.get("ParentId")),
            percent=user_data.get("PlayedPercentage"),
            BackdropImageTags=item.get("BackdropImageTags") or [],
            server_type="ugreen",
            use_cookies=False,
        )

    def __build_media_service_image_url(
        self,
        item_id: Optional[Union[str, int]],
        image_type: str = "Primary",
        remote: bool = False,
    ) -> Optional[str]:
        """
        生成媒体库服务条目的图片地址，并在远程场景使用播放地址。
        """
        if not self._media_api or not item_id:
            return None
        image_url = self._media_api.image_url(item_id, image_type)
        if remote and image_url and self._playhost:
            return image_url.replace(
                f"{self._media_api.host}/",
                f"{self._playhost.rstrip('/')}/",
                1,
            )
        return image_url

    def __build_play_item_from_wrapper(self, wrapper: dict[str, Any]) -> Optional[_SchemaMediaServerPlayItem]:
        """
        将内部接口的视频与播放状态包装转换为仪表盘条目。

        :param wrapper: 包含 video_info、play_status 的包装，或直接传入视频信息字典
        :return: 播放条目；缺少视频标识时返回 None，进度转换并限制在 0 至 100 之间
        """
        video_info_value = wrapper.get("video_info")
        video_info = video_info_value if isinstance(video_info_value, dict) else wrapper
        if not isinstance(video_info, dict):
            return None

        item_id = video_info.get("ug_video_info_id")
        if item_id is None:
            return None

        play_status_value = wrapper.get("play_status")
        play_status = play_status_value if isinstance(play_status_value, dict) else {}
        progress_value = play_status.get("progress")
        progress = progress_value if isinstance(progress_value, (int, float)) else 0

        if video_info.get("type") == 2:
            subtitle = play_status.get("tv_name") or "剧集"
            media_type = MediaType.TV.value
        else:
            subtitle = "电影" if video_info.get("type") == 1 else "视频"
            media_type = MediaType.MOVIE.value

        image = self.__resolve_image(video_info.get("poster_path")) or self.__resolve_image(
            video_info.get("backdrop_path")
        )

        return _SchemaMediaServerPlayItem(
            id=str(item_id),
            title=video_info.get("name"),
            subtitle=subtitle,
            type=media_type,
            image=image,
            link=self.__build_play_url(item_id, video_info.get("type"), video_info.get("media_lib_set_id")),
            percent=max(0.0, min(100.0, progress * 100.0)),
            server_type="ugreen",
            use_cookies=False,
        )

    @staticmethod
    def __infer_library_type(name: str, path: Optional[str]) -> str:
        """
        在接口缺少媒体库类型时按名称与目录关键词推断。

        :param name: 媒体库名称
        :param path: 可选媒体库目录
        :return: 优先判定剧集，再判定电影与音乐；无匹配关键词时返回未知类型
        """
        name = name or ""
        path = path or ""
        if "电视剧" in path or any(key in name for key in ["剧", "综艺", "动漫", "纪录片"]):
            return MediaType.TV.value
        if "电影" in path or "电影" in name:
            return MediaType.MOVIE.value
        if any(key in name.lower() for key in ["music", "audio", "音乐"]):
            return MediaType.MUSIC.value
        return MediaType.UNKNOWN.value

    def __is_library_blocked(self, library_id: str) -> bool:
        """
        判断媒体库是否被当前同步范围排除。

        :param library_id: 媒体库标识，比较时转换为字符串
        :return: 非空同步列表不含 all 且未选中该库时返回 True
        """
        return (
            True
            if (
                self._sync_libraries
                and "all" not in self._sync_libraries
                and str(library_id) not in self._sync_libraries
            )
            else False
        )

    @staticmethod
    def __resolve_scan_type(
        scan_mode: Optional[Union[str, int]] = None,
        scan_type: Optional[Union[str, int]] = None,
    ) -> int:
        """
        解析绿联扫描模式并转为 `media_lib_scan_type`。

        支持值：
        - 1 / new_and_modified: 新添加和修改
        - 2 / supplement_missing: 补充缺失
        - 3 / full_override: 覆盖扫描
        """
        # 优先使用显式 scan_type 数值配置。
        for value in (scan_type, scan_mode):
            try:
                # None 等非法输入由紧邻的异常分支处理，兼容旧配置值类型。
                parsed = int(value)  # type: ignore[arg-type]
                if parsed in (1, 2, 3):
                    return parsed
            except Exception:
                pass

        mode = str(scan_mode or "").strip().lower()
        mode_map = {
            "new_and_modified": 1,
            "new_modified": 1,
            "add": 1,
            "added": 1,
            "new": 1,
            "scan_new_modified": 1,
            "supplement_missing": 2,
            "supplement": 2,
            "additional": 2,
            "missing": 2,
            "scan_missing": 2,
            "full_override": 3,
            "override": 3,
            "cover": 3,
            "replace": 3,
            "scan_override": 3,
        }
        return mode_map.get(mode, 2)

    @staticmethod
    def __resolve_verify_ssl(verify_ssl: Optional[Union[bool, str, int]]) -> bool:
        """
        解析证书验证开关，未知配置默认启用验证。

        :param verify_ssl: 布尔值或 1/0、true/false、yes/no、on/off 文本
        :return: 规范布尔开关，None 或无法识别的文本返回 True
        """
        if isinstance(verify_ssl, bool):
            return verify_ssl
        if verify_ssl is None:
            return True
        value = str(verify_ssl).strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        return True

    def __scan_library(self, library_id: str, scan_type: Optional[int] = None) -> bool:
        """
        向内部接口发送单个媒体库的扫描请求。

        :param library_id: 媒体库集合标识
        :param scan_type: 扫描类型，空值使用实例配置
        :return: 请求业务成功时返回 True；内部 API 不存在或请求失败时返回 False，不等待扫描完成
        """
        if not self._api:
            return False
        return self._api.scan(
            media_lib_set_id=library_id,
            scan_type=scan_type or self._scan_type,
            op_type=2,
        )

    def __load_library_paths(self) -> dict[str, str]:
        """
        分页收集内部接口的媒体库目录，限制最多 200 页。

        :return: 媒体库标识到路径的映射；接口失败保留此前结果，达到页数上限时记录警告并停止
        """
        if not self._api:
            return {}

        paths: dict[str, str] = {}
        page = 1
        while page <= self.LIBRARY_PATH_PAGE_LIMIT:
            data = self._api.poster_wall_get_folder(page=page, page_size=100)
            if not data:
                break

            for folder in data.get("folder_arr") or []:
                lib_id = folder.get("media_lib_set_id")
                lib_path = folder.get("path")
                if lib_id is not None and lib_path:
                    paths[str(lib_id)] = str(lib_path)

            if data.get("is_last_page"):
                break
            page += 1

        if page > self.LIBRARY_PATH_PAGE_LIMIT:
            # 部分固件分页标志异常时会无限返回下一页，这里加硬限制避免阻塞调用方。
            logger.warning(
                f"绿联影视 {self._username} 媒体库目录分页超过上限 {self.LIBRARY_PATH_PAGE_LIMIT} 页，停止继续加载"
            )

        return paths

    def get_librarys(self, hidden: Optional[bool] = False) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        查询当前连接模式的媒体库，并刷新内存中的库及路径缓存。

        :param hidden: 是否过滤同步范围外的媒体库，默认不过滤
        :return: 统一媒体库列表；未认证或查询失败时返回 None
        """
        if self.__is_media_service_mode():
            return self.__get_media_service_librarys(hidden=hidden)
        if not self.is_authenticated() or not self._api:
            return None

        media_libs = self._api.media_list()
        if media_libs is None:
            return None
        self._library_paths = self.__load_library_paths()
        libraries = []
        self._libraries = {}

        for lib in media_libs:
            lib_id = str(lib.get("media_lib_set_id"))
            if hidden and self.__is_library_blocked(lib_id):
                continue

            lib_name = lib.get("media_name") or ""
            lib_path = self._library_paths.get(lib_id)
            library_type = self.__infer_library_type(lib_name, lib_path)

            poster_paths = lib.get("poster_paths") or []
            backdrop_paths = lib.get("backdrop_paths") or []
            image_list = list(
                filter(
                    None,
                    [self.__resolve_image(p) for p in [*poster_paths, *backdrop_paths]],
                )
            )

            self._libraries[lib_id] = {
                "id": lib_id,
                "name": lib_name,
                "path": lib_path,
                "type": library_type,
                "video_count": lib.get("video_count") or 0,
            }

            libraries.append(
                _SchemaMediaServerLibrary(
                    server="ugreen",
                    id=lib_id,
                    name=lib_name,
                    type=library_type,
                    path=lib_path,
                    item_count=lib.get("video_count") or 0,
                    image_list=image_list,
                    link=self.__build_root_url(),
                    server_type="ugreen",
                    use_cookies=False,
                )
            )

        return libraries

    def get_user_count(self) -> int:
        """
        查询媒体库用户数量。

        :return: 内部模式返回接口用户数；媒体库服务不支持枚举，已认证时记 1、否则记 0
        """
        if self.__is_media_service_mode():
            # 媒体库服务不开放用户枚举，当前实例只代表一个已认证媒体库账号。
            return 1 if self.is_authenticated() else 0
        if not self.is_authenticated() or not self._api:
            return 0
        users = self._api.media_lib_users()
        return len(users)

    def get_medias_count(self) -> _SchemaStatistic:
        """
        查询电影、电视剧和音乐数量，按服务能力提供分集统计。

        :return: 统一统计对象；未认证或查询失败时对应计数为空或零，内部模式的分集总数固定为 None
        """
        if self.__is_media_service_mode():
            if not self.is_authenticated() or not self._media_api:
                return _SchemaStatistic()
            counts = self._media_api.counts()
            if not counts:
                return _SchemaStatistic()
            episode_count = counts.get("EpisodeCount")
            return _SchemaStatistic(
                movie_count=int(counts.get("MovieCount") or 0),
                tv_count=int(counts.get("SeriesCount") or 0),
                episode_count=(int(episode_count) if episode_count is not None else None),
                music_count=int(
                    counts.get("MusicAlbumCount") or counts.get("AlbumCount") or counts.get("SongCount") or 0
                ),
            )
        if not self.is_authenticated() or not self._api:
            return _SchemaStatistic()

        movie_data = self._api.video_all(classification=-102, page=1, page_size=1) or {}
        tv_data = self._api.video_all(classification=-103, page=1, page_size=1) or {}
        music_data = self._api.video_all(classification=-104, page=1, page_size=1) or {}

        return _SchemaStatistic(
            movie_count=int(movie_data.get("total_num") or 0),
            tv_count=int(tv_data.get("total_num") or 0),
            # 绿联当前不统计剧集总数，返回 None 由前端展示“未获取”。
            episode_count=None,
            music_count=int(music_data.get("total_num") or 0),
        )

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """
        使用独立临时客户端验证账号，不替换当前实例会话。

        媒体库模式验证后关闭临时连接；内部模式还会立即登出，因此返回令牌仅用于认证结果，不能据此长期复用该会话。

        :param username: 待验证的登录用户名
        :param password: 待验证的密码
        :return: 成功时返回认证令牌，输入不完整或认证失败时返回 None
        """
        host = self._host
        if not username or not password or not host:
            return None

        if self.__is_media_service_mode():
            media_api = UgreenMediaApi(host, verify_ssl=self._verify_ssl)
            try:
                session = media_api.authenticate(username, password)
                return session.token if session else None
            finally:
                media_api.close()

        api = Api(host, verify_ssl=self._verify_ssl)
        try:
            return api.login(username, password)
        finally:
            api.logout()
            api.close()

    @staticmethod
    def __extract_video_info_list(bucket: object) -> list[dict[str, Any]]:
        """
        从搜索分类桶的 video_arr 中提取有效 video_info 字典。

        :param bucket: 按媒体类型分组的搜索结果
        :return: 视频信息字典列表；分组或列表形状不符时返回空列表
        """
        if not isinstance(bucket, Mapping):
            return []
        video_arr = bucket.get("video_arr")
        if not isinstance(video_arr, list):
            return []
        result = []
        for item in video_arr:
            if not isinstance(item, Mapping):
                continue
            info = item.get("video_info")
            if isinstance(info, Mapping):
                result.append(dict(info))
        return result

    def __get_media_service_movies(
        self,
        title: str,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
    ) -> Optional[List[_SchemaMediaServerItem]]:
        """
        通过媒体库服务用户条目接口查找电影。
        """
        if not self.is_authenticated() or not self._media_api or not title:
            return None
        data = self._media_api.items(
            include_item_types="Movie",
            search_term=title,
            limit=10,
            fields=("ProviderIds,OriginalTitle,ProductionYear,Path,UserDataPlayCount,UserDataLastPlayedDate,ParentId"),
        )
        if data is None:
            return None

        movies: List[_SchemaMediaServerItem] = []
        for item in data.get("Items") or []:
            media_item = self.__format_media_service_item(item)
            if not media_item or not MediaServerIdentityHelper.is_compatible(media_item, media_source, media_id):
                continue
            if title not in [media_item.title, media_item.original_title]:
                continue
            if year and str(media_item.year) != str(year):
                continue
            movies.append(media_item)
        return movies

    def __get_media_service_music(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
    ) -> List[_SchemaMediaServerItem]:
        """
        通过媒体库服务用户条目接口查找音乐。
        """
        if not self.is_authenticated() or not self._media_api:
            return []
        query = " ".join(filter(None, [title, artist, album])).strip()
        if not query:
            return []
        data = self._media_api.items(
            include_item_types="MusicAlbum,Audio",
            search_term=query,
            limit=20,
            fields=(
                "Album,AlbumArtist,AlbumArtists,Artists,ArtistItems,ChildCount,"
                "ProviderIds,OriginalTitle,ProductionYear,Path,ParentId"
            ),
        )
        if not data:
            return []
        results: List[_SchemaMediaServerItem] = []
        for item in data.get("Items") or []:
            media_item = self.__format_media_service_item(item)
            if media_item:
                results.append(media_item)
        return results

    def __search_media_service_tv_item(
        self,
        title: str,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
    ) -> Optional[_SchemaMediaServerItem]:
        """
        通过媒体库服务用户条目接口查找电视剧。
        """
        if not self.is_authenticated() or not self._media_api or not title:
            return None
        data = self._media_api.items(
            include_item_types="Series",
            search_term=title,
            limit=10,
            fields="ProviderIds,OriginalTitle,ProductionYear,Path,ParentId",
        )
        if not data:
            return None
        for item in data.get("Items") or []:
            media_item = self.__format_media_service_item(item)
            if not media_item or not MediaServerIdentityHelper.is_compatible(media_item, media_source, media_id):
                continue
            if title not in [media_item.title, media_item.original_title]:
                continue
            if year and str(media_item.year) != str(year):
                continue
            return media_item
        return None

    def __get_media_service_tv_episodes(
        self,
        item_id: Optional[str] = None,
        title: Optional[str] = None,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[Dict[int, List[int]]]]:
        """
        通过媒体库服务的 Shows/Episodes 接口查询已入库剧集。
        """
        if not self.is_authenticated() or not self._media_api:
            return None, None
        cached_item_id = item_id
        if not item_id:
            if not title:
                return None, None
            tv_item = self.__search_media_service_tv_item(title, year, media_source, media_id)
            if not tv_item or not tv_item.item_id:
                return None, None
            item_id = tv_item.item_id
        item_id = str(item_id)

        item_info = self.get_iteminfo(item_id)
        if not item_info and cached_item_id and title:
            logger.warning(f"绿联媒体库服务缓存的电视剧媒体ID {cached_item_id} 已失效，尝试按标题重新搜索：{title}")
            tv_item = self.__search_media_service_tv_item(title, year, media_source, media_id)
            if not tv_item or not tv_item.item_id:
                return None, {}
            item_id = str(tv_item.item_id)
            item_info = self.get_iteminfo(item_id)
        if not item_info:
            return None, {}
        if not MediaServerIdentityHelper.is_compatible(item_info, media_source, media_id):
            return None, {}

        episodes = self._media_api.episodes(item_id, season=season)
        if episodes is None:
            return None, None
        season_episodes: Dict[int, List[int]] = {}
        for episode in episodes:
            season_number = episode.get("ParentIndexNumber")
            episode_number = episode.get("IndexNumber")
            if not isinstance(season_number, int) or not isinstance(episode_number, int):
                continue
            season_episodes.setdefault(season_number, []).append(episode_number)
        for season_number in list(season_episodes):
            season_episodes[season_number] = sorted(set(season_episodes[season_number]))
        return item_id, season_episodes

    def __get_media_service_latest_items(self, num: int = 20) -> Optional[List[dict[str, Any]]]:
        """
        按创建时间获取媒体库服务中的最近电影和剧集条目。
        """
        if not self.is_authenticated() or not self._media_api:
            return None
        page_size = max(1, num)
        data = self._media_api.items(
            include_item_types="Movie,Series",
            limit=page_size,
            sort_by="DateCreated",
            sort_order="Descending",
            fields=(
                "ProviderIds,OriginalTitle,ProductionYear,Path,ParentId,"
                "BackdropImageTags,UserDataPlayCount,UserDataLastPlayedDate"
            ),
        )
        if data is None:
            return None
        return [dict(item) for item in data.get("Items") or [] if isinstance(item, Mapping)]

    def get_movies(
        self, title: str, year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
    ) -> Optional[List[_SchemaMediaServerItem]]:
        """
        按连接模式搜索电影，并核对名称、年份及来源身份。

        :param title: 电影标题，内部模式同时匹配名称与原始名称
        :param year: 可选年份，空值不限制年份
        :param media_source: 用于排除身份冲突的来源
        :param media_id: 对应来源内的媒体标识
        :return: 匹配的统一电影列表；未认证或标题为空时返回 None，无结果时返回空列表
        """
        if self.__is_media_service_mode():
            return self.__get_media_service_movies(title, year, media_source, media_id)
        if not self.is_authenticated() or not self._api or not title:
            return None

        data = self._api.search(title)
        if not data:
            return []

        movies = []
        for info in self.__extract_video_info_list(data.get("movies_list")):
            info_tmdb = info.get("tmdb_id")
            if not MediaServerIdentityHelper.are_compatible(
                    MediaSource.TMDB if info_tmdb else None,
                    str(info_tmdb) if info_tmdb else None,
                    media_source,
                    media_id,
            ):
                continue
            if title not in [info.get("name"), info.get("original_name")]:
                continue
            item_year = info.get("year")
            if year and str(item_year) != str(year):
                continue
            media_item = self.__build_media_server_item(info)
            if media_item:
                movies.append(media_item)
        return movies

    def get_music(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
    ) -> List[_SchemaMediaServerItem]:
        """
        按专辑、歌曲或艺术家名称查询音乐候选。

        :param title: 歌曲标题
        :param artist: 艺术家名称
        :param album: 专辑名称，内部模式优先作为搜索词
        :return: 统一音乐候选列表；无查询词、未认证或请求失败时返回空列表
        """
        if self.__is_media_service_mode():
            return self.__get_media_service_music(title, artist, album)
        if not self.is_authenticated() or not self._api:
            return []
        query = album or title or artist
        if not query:
            return []

        data = self._api.search(query)
        if not data:
            return []

        # 绿联搜索按媒体类型分桶返回，音乐相关桶名在不同固件版本上并不统一
        music_buckets = ("music_list", "audio_list", "album_list", "songs_list")
        results: List[_SchemaMediaServerItem] = []
        for bucket in music_buckets:
            for info in self.__extract_video_info_list(data.get(bucket)):
                media_item = self.__build_media_server_item(info)
                if media_item:
                    media_item.note = MusicMediaServerHelper.build_note(info)
                    results.append(media_item)
        return results

    def __search_tv_item(
            self, title: str, year: Optional[str] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """
        按标题、年份与统一媒体身份查找电视剧条目。
        """
        if not self._api:
            return None
        data = self._api.search(title)
        if not data:
            return None

        for info in self.__extract_video_info_list(data.get("tv_list")):
            info_tmdb = info.get("tmdb_id")
            if not MediaServerIdentityHelper.are_compatible(
                    MediaSource.TMDB if info_tmdb else None,
                    str(info_tmdb) if info_tmdb else None,
                    media_source,
                    media_id,
            ):
                continue
            if title not in [info.get("name"), info.get("original_name")]:
                continue
            item_year = info.get("year")
            if year and str(item_year) != str(year):
                continue
            return info
        return None

    def get_tv_episodes(
        self,
        item_id: Optional[str] = None,
        title: Optional[str] = None,
        year: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[Dict[int, list[int]]]]:
        """
        根据标题、年份、媒体身份和季号查询绿联媒体库中的电视剧已入库集数。

        :param item_id: 绿联媒体库中的剧集ID，存在缓存ID时优先使用
        :param title: 标题
        :param year: 年份
        :param media_source: 媒体来源
        :param media_id: 媒体来源原生ID
        :param season: 季号
        :return: 命中的剧集ID及每季已入库集数
        """
        if self.__is_media_service_mode():
            return self.__get_media_service_tv_episodes(
                item_id=item_id,
                title=title,
                year=year,
                media_source=media_source,
                media_id=media_id,
                season=season,
            )
        if not self.is_authenticated() or not self._api:
            return None, None

        cached_item_id = item_id
        if not item_id:
            if not title:
                return None, None
            if not (tv_info := self.__search_tv_item(
                    title, year, media_source, media_id
            )):
                return None, None
            found_item_id = tv_info.get("ug_video_info_id")
            if found_item_id is None:
                return None, None
            item_id = str(found_item_id)
        else:
            item_id = str(item_id)

        item_info = self.get_iteminfo(item_id)
        if not item_info and cached_item_id and title:
            # 媒体删除后重新入库会导致缓存ID失效，回退到标题搜索避免误判整部剧缺失。
            logger.warning(f"绿联缓存的电视剧媒体ID {cached_item_id} 已失效，尝试按标题重新搜索：{title}")
            if not (tv_info := self.__search_tv_item(
                    title, year, media_source, media_id
            )):
                return None, {}
            found_item_id = tv_info.get("ug_video_info_id")
            if found_item_id is None:
                return None, {}
            item_id = str(found_item_id)
            item_info = self.get_iteminfo(item_id)
        if not item_info:
            return None, {}
        if not MediaServerIdentityHelper.is_compatible(item_info, media_source, media_id):
            return None, {}

        tv_detail = self._api.get_tv(item_id, folder_path="ALL")
        if not tv_detail:
            return None, {}

        season_map = {}
        for info in tv_detail.get("season_info") or []:
            if not isinstance(info, dict):
                continue
            category_id = info.get("category_id")
            season_num = info.get("season_num")
            if category_id and isinstance(season_num, int):
                season_map[str(category_id)] = season_num

        season_episodes: Dict[int, list[int]] = {}
        for ep in tv_detail.get("tv_info") or []:
            if not isinstance(ep, dict):
                continue
            episode = ep.get("episode")
            if not isinstance(episode, int):
                continue
            season_num = season_map.get(str(ep.get("category_id")), 1)
            if season is not None and season_num != season:
                continue
            season_episodes.setdefault(season_num, []).append(episode)

        for season_num in list(season_episodes.keys()):
            season_episodes[season_num] = sorted(set(season_episodes[season_num]))

        return item_id, season_episodes

    def refresh_root_library(self, scan_mode: Optional[Union[str, int]] = None) -> Optional[bool]:
        """
        请求刷新缓存中的全部媒体库；媒体库服务模式暂不支持。

        未加载媒体库时先查询目录；只等待扫描请求被接受，不等待扫描结束。

        :param scan_mode: 本次扫描模式，省略时使用实例配置
        :return: 全部请求成功或没有媒体库时返回 True；未认证时返回 None，媒体库服务模式返回 False
        """
        if self.__is_media_service_mode():
            if not self.is_authenticated():
                return None
            logger.debug("绿联媒体库服务未实现媒体库刷新接口，跳过刷新")
            return False
        if not self.is_authenticated() or not self._api:
            return None

        if not self._libraries:
            self.get_librarys()

        scan_type = (
            self.__resolve_scan_type(scan_mode=scan_mode)
            if scan_mode is not None
            else self._scan_type
        )
        results = []
        for lib_id in self._libraries.keys():
            logger.info(
                f"刷新媒体库：{self._libraries[lib_id].get('name')}（扫描模式: {scan_type}）"
            )
            results.append(self.__scan_library(library_id=lib_id, scan_type=scan_type))

        return all(results) if results else True

    def __match_library_id_by_path(self, path: Optional[Path]) -> Optional[str]:
        """
        按目录层级查找目标路径所属的第一个媒体库。

        :param path: 媒体整理后的目标路径
        :return: 匹配的媒体库标识；路径为空或没有匹配时返回 None，路径缓存为空时先查询媒体库
        """
        if path is None:
            return None

        path_str = self.__normalize_dir_path(path)
        if not self._library_paths:
            self.get_librarys()

        for lib_id, lib_path in self._library_paths.items():
            if self.__is_subpath(path_str, lib_path):
                return lib_id
        return None

    def refresh_library_by_items(
        self,
        items: List[_SchemaRefreshMediaItem],
        scan_mode: Optional[Union[str, int]] = None,
    ) -> Optional[bool]:
        """
        合并媒体目标路径所属的库，并请求扫描。

        无法定位任一目标或局部扫描失败时回退到刷新全部媒体库；空输入无需扫描并返回 True。

        :param items: 包含目标路径的媒体刷新列表
        :param scan_mode: 本次扫描模式，省略时使用实例配置
        :return: 刷新结果；未认证时返回 None，媒体库服务模式不支持并返回 False
        """
        if self.__is_media_service_mode():
            if not self.is_authenticated():
                return None
            logger.debug("绿联媒体库服务未实现媒体库刷新接口，跳过刷新")
            return False
        if not self.is_authenticated() or not self._api:
            return None

        scan_type = (
            self.__resolve_scan_type(scan_mode=scan_mode)
            if scan_mode is not None
            else self._scan_type
        )
        library_ids = set()
        for item in items:
            library_id = self.__match_library_id_by_path(item.target_path)
            if library_id is None:
                return self.refresh_root_library(scan_mode=scan_mode)
            library_ids.add(library_id)

        for library_id in library_ids:
            lib_name = self._libraries.get(library_id, {}).get("name", library_id)
            logger.info(f"刷新媒体库：{lib_name}（扫描模式: {scan_type}）")
            if not self.__scan_library(library_id=library_id, scan_type=scan_type):
                return self.refresh_root_library(scan_mode=scan_mode)

        return True

    @staticmethod
    def get_webhook_message(body: Any) -> Optional[_SchemaWebhookEventInfo]:
        """
        保留统一 Webhook 解析入口，绿联当前不提供事件解析。

        :param body: 兼容统一接口的请求体，当前不读取
        :return: 始终返回 None
        """
        return None

    def get_iteminfo(self, itemid: str) -> Optional[_SchemaMediaServerItem]:
        """
        按当前连接模式查询单个媒体条目。

        :param itemid: 对应服务的条目标识
        :return: 统一媒体详情；未认证、标识为空、查询失败或缺少视频身份时返回 None
        """
        if self.__is_media_service_mode():
            if not self.is_authenticated() or not self._media_api or not itemid:
                return None
            item = self._media_api.item(itemid)
            return self.__format_media_service_item(item) if item else None
        if not self.is_authenticated() or not self._api or not itemid:
            return None

        info = self._api.recently_played_info(itemid)
        if not info:
            return None

        video_info = info.get("video_info") if isinstance(info.get("video_info"), dict) else None
        if not video_info or not video_info.get("ug_video_info_id"):
            return None

        return self.__build_media_server_item(video_info, info.get("play_status"))

    def _iter_library_videos(
        self, root_path: str, page_size: int = 100,
    ) -> Generator[dict[str, Any], None, None]:
        """
        广度优先分页遍历海报墙目录，最多访问 20000 个不同路径。

        重复路径只访问一次。任意目录或分页失败都中止，不能将部分结果视为完整媒体库。

        :param root_path: 遍历起始目录
        :param page_size: 每个目录的分页大小，默认 100
        :yields: 服务端 video_arr 中的有效视频字典
        :raises ConnectionError: 目录读取失败或超过完整遍历的路径数量上限
        """
        if not self._api or not root_path:
            raise ConnectionError("绿联内部服务尚未连接或媒体库路径为空")

        queue = deque([root_path])
        visited: set[str] = set()
        max_paths = 20000

        while queue and len(visited) < max_paths:
            current_path = queue.popleft()
            if current_path in visited:
                continue
            visited.add(current_path)

            page = 1
            while True:
                data = self._api.poster_wall_get_folder(
                    path=current_path,
                    page=page,
                    page_size=page_size,
                    sort_type=1,
                    order_type=1,
                )
                if data is None:
                    raise ConnectionError("读取绿联海报墙目录失败")
                if any(field in data and not isinstance(data[field], list) for field in ("video_arr", "folder_arr")):
                    raise ConnectionError("绿联海报墙目录响应格式错误")
                if not data:
                    break

                for video in data.get("video_arr") or []:
                    if isinstance(video, dict):
                        yield video

                for folder in data.get("folder_arr") or []:
                    if not isinstance(folder, dict):
                        continue
                    sub_path = folder.get("path")
                    if sub_path and sub_path not in visited:
                        queue.append(str(sub_path))

                if data.get("is_last_page"):
                    break
                page += 1
        if queue:
            raise ConnectionError("绿联媒体库目录数量超过遍历上限")

    def __iter_media_service_items(
        self,
        parent: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
    ) -> Generator[_SchemaMediaServerItem, None, None]:
        """
        分页遍历媒体库服务用户可见的电影、剧集和音乐专辑。

        :raises ConnectionError: 未认证、分页失败或列表结构错误，调用方须保留旧缓存
        """
        if not self.is_authenticated() or not self._media_api or not parent:
            raise ConnectionError("绿联媒体库服务尚未连接或媒体库标识为空")
        if self.__is_library_blocked(str(parent)):
            return

        remaining = None if limit in (None, -1) else max(0, limit or 0)
        if remaining == 0:
            return
        offset = max(0, start_index or 0)
        fields = (
            "ProviderIds,OriginalTitle,ProductionYear,Path,ParentId,"
            "BackdropImageTags,Album,AlbumArtist,AlbumArtists,Artists,"
            "ArtistItems,ChildCount,UserDataPlayCount,UserDataLastPlayedDate"
        )

        while True:
            page_size = min(100, remaining) if remaining is not None else 100
            data = self._media_api.items(
                parent_id=parent,
                include_item_types="Movie,Series,MusicAlbum",
                start_index=offset,
                limit=page_size,
                fields=fields,
            )
            if data is None:
                raise ConnectionError("读取绿联媒体库服务条目失败")
            page_items = _media_service_items(data)
            if not page_items:
                return

            for raw_item in page_items:
                if not isinstance(raw_item, Mapping):
                    continue
                if raw_item.get("Type") not in {"Movie", "Series", "MusicAlbum"}:
                    continue
                media_item = self.__format_media_service_item(raw_item)
                if media_item:
                    yield media_item
                    if remaining is not None:
                        remaining -= 1
                        if remaining <= 0:
                            return

            offset += len(page_items)
            total_count = data.get("TotalRecordCount")
            try:
                reached_total = total_count is not None and offset >= int(total_count)
            except TypeError, ValueError:
                reached_total = False
            if len(page_items) < page_size or reached_total:
                return

    def get_items_count(self, parent: Union[str, int]) -> Optional[int]:
        """
        获取指定媒体库可同步的媒体条目总数。

        :param parent: 媒体库ID
        :return: 媒体条目总数，查询失败时返回None
        """
        if self.__is_media_service_mode():
            if not self.is_authenticated() or not self._media_api or not parent:
                return None
            media_library = self._libraries.get(str(parent), {})
            library_type = str(media_library.get("type") or "")
            include_item_types = {
                MediaType.MOVIE.value: "Movie",
                MediaType.TV.value: "Series",
                MediaType.MUSIC.value: "MusicAlbum,Audio",
            }.get(library_type, "Movie,Series,MusicAlbum")
            data = self._media_api.items(
                parent_id=parent,
                include_item_types=include_item_types,
                limit=0,
            )
            if data is None:
                return None
            total_count = data.get("TotalRecordCount")
            try:
                return int(total_count) if total_count is not None else None
            except TypeError, ValueError:
                return None
        if not self.is_authenticated() or not self._api:
            return None
        if not self._libraries:
            self.get_librarys()
        library = self._libraries.get(str(parent))
        if not library:
            return None
        return int(library.get("video_count") or 0)

    def get_items(
        self,
        parent: Union[str, int],
        start_index: Optional[int] = 0,
        limit: Optional[int] = -1,
    ) -> Generator[_SchemaMediaServerItem, None, None]:
        """
        按连接模式逐项遍历媒体库中可同步的媒体。

        内部模式按目录遍历并只返回电影、剧集，媒体库模式还支持音乐专辑。
        当前内部模式对 limit=0 仍可能产生首个条目，媒体库模式不产生条目。
        底层读取失败抛出异常，调用方不得清理未读取到的旧缓存。

        :param parent: 媒体库标识
        :param start_index: 跳过的条目数量，负值或空值按 0 处理
        :param limit: 期望返回的条目上限，None 或 -1 表示不限；调用方应提供正数或不限标记
        :yields: 统一媒体条目
        :raises ConnectionError: 服务未连接、媒体库路径缺失或任意目录分页失败
        """
        if self.__is_media_service_mode():
            yield from self.__iter_media_service_items(parent, start_index, limit)
            return None
        if not self.is_authenticated() or not self._api:
            raise ConnectionError("绿联内部服务尚未认证")

        library_id = str(parent)
        if not self._library_paths:
            self.get_librarys()

        root_path = self._library_paths.get(library_id)
        if not root_path:
            raise ConnectionError("绿联媒体库路径不可用")

        skip = max(0, start_index or 0)
        remain = -1 if limit is None or limit == -1 else max(0, limit)

        for video in self._iter_library_videos(root_path=root_path):
            video_type = video.get("type")
            if video_type not in [1, 2]:
                continue

            if skip > 0:
                skip -= 1
                continue

            item = self.__build_media_server_item(video)
            if item:
                yield item
                if remain != -1:
                    remain -= 1
                    if remain <= 0:
                        break

        return None

    def get_play_url(self, item_id: str) -> Optional[str]:
        """
        查询条目后生成当前连接模式的网页跳转链接。

        :param item_id: 对应服务的媒体条目标识
        :return: 媒体库模式返回条目链接，内部模式返回 NAS 根地址；未认证、条目无效或查询失败时返回 None
        """
        if self.__is_media_service_mode():
            if not self.is_authenticated() or not self._media_api or not item_id:
                return None
            item = self._media_api.item(item_id)
            if not item:
                return None
            return self.__build_play_url(
                item_id=item_id,
                video_type=item.get("Type"),
                media_lib_set_id=item.get("ParentId"),
            )
        if not self.is_authenticated() or not self._api:
            return None

        info = self._api.recently_played_info(item_id)
        if not info:
            return None

        video_info = info.get("video_info") if isinstance(info.get("video_info"), dict) else None
        if not video_info:
            return None

        return self.__build_play_url(
            item_id=item_id,
            video_type=video_info.get("type"),
            media_lib_set_id=video_info.get("media_lib_set_id"),
        )

    def get_resume(self, num: Optional[int] = 12) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        获取当前账号的继续观看条目。

        :param num: 期望数量，空值或 0 使用 12，最少请求 1 条
        :return: 播放条目列表；未认证或查询失败时返回 None，内部模式会排除同步范围外的媒体库
        """
        if self.__is_media_service_mode():
            if not self.is_authenticated() or not self._media_api:
                return None
            page_size = max(1, num or 12)
            items = self._media_api.resume(limit=page_size)
            if items is None:
                return None
            media_resume: List[_SchemaMediaServerPlayItem] = []
            for item in items:
                if len(media_resume) == page_size:
                    break
                play_item = self.__build_media_service_play_item(item)
                if play_item:
                    media_resume.append(play_item)
            return media_resume
        if not self.is_authenticated() or not self._api:
            return None

        page_size = max(1, num or 12)
        data = self._api.recently_played(page=1, page_size=page_size)
        if data is None:
            return None

        ret_resume: list[_SchemaMediaServerPlayItem] = []
        for item in data.get("video_arr") or []:
            if len(ret_resume) == page_size:
                break
            if not isinstance(item, dict):
                continue
            video_info_value = item.get("video_info")
            video_info = video_info_value if isinstance(video_info_value, dict) else {}
            library_id = str(video_info.get("media_lib_set_id") or "")
            if self.__is_library_blocked(library_id):
                continue
            play_item = self.__build_play_item_from_wrapper(item)
            if play_item:
                ret_resume.append(play_item)

        return ret_resume

    def get_latest(self, num: int = 20) -> Optional[List[_SchemaMediaServerPlayItem]]:
        """
        查询最近入库的电影和电视剧。

        :param num: 期望条目数，最少请求 1 条
        :return: 最新条目列表；未认证或查询失败时返回 None，内部模式会排除同步范围外的媒体库
        """
        if self.__is_media_service_mode():
            items = self.__get_media_service_latest_items(num=num)
            if items is None:
                return None
            page_size = max(1, num)
            media_latest: List[_SchemaMediaServerPlayItem] = []
            for item in items:
                if len(media_latest) == page_size:
                    break
                play_item = self.__build_media_service_play_item(item)
                if play_item:
                    media_latest.append(play_item)
            return media_latest
        if not self.is_authenticated() or not self._api:
            return None

        page_size = max(1, num)
        data = self._api.recently_updated(page=1, page_size=page_size)
        if data is None:
            return None

        latest: list[_SchemaMediaServerPlayItem] = []
        for item in data.get("video_arr") or []:
            if len(latest) == page_size:
                break
            if not isinstance(item, dict):
                continue
            video_info_value = item.get("video_info")
            video_info = video_info_value if isinstance(video_info_value, dict) else {}
            library_id = str(video_info.get("media_lib_set_id") or "")
            if self.__is_library_blocked(library_id):
                continue
            play_item = self.__build_play_item_from_wrapper(item)
            if play_item:
                latest.append(play_item)

        return latest

    def get_latest_backdrops(self, num: int = 20, remote: bool = False) -> Optional[List[str]]:
        """
        查询最近入库媒体可用的背景图，缺少时回退主封面。

        :param num: 期望图片数量，默认 20
        :param remote: 媒体库服务模式是否优先使用外网地址；内部模式不使用此参数
        :return: 图片 URL 列表；未认证或媒体库服务查询失败时返回 None，内部模式无查询结果时返回空列表
        """
        if self.__is_media_service_mode():
            items = self.__get_media_service_latest_items(num=num)
            if items is None:
                return None
            media_images: List[str] = []
            for item in items:
                if len(media_images) == num:
                    break
                item_id = item.get("Id")
                if not item_id:
                    continue
                image_type = "Backdrop" if item.get("BackdropImageTags") else "Primary"
                image = self.__build_media_service_image_url(item_id, image_type, remote=remote)
                if image:
                    media_images.append(image)
            return media_images
        if not self.is_authenticated() or not self._api:
            return None

        data = self._api.recently_updated(page=1, page_size=max(1, num))
        if not data:
            return []

        images: List[str] = []
        for item in data.get("video_arr") or []:
            if len(images) == num:
                break
            if not isinstance(item, dict):
                continue

            video_info_value = item.get("video_info")
            video_info = video_info_value if isinstance(video_info_value, dict) else {}
            video_type = video_info.get("type") if isinstance(video_info, dict) else None
            if video_type != 1 and video_type != 2:
                continue
            library_id = str(video_info.get("media_lib_set_id") or "")
            if self.__is_library_blocked(library_id):
                continue

            image = self.__resolve_image(video_info.get("backdrop_path")) or self.__resolve_image(
                video_info.get("poster_path")
            )
            if image:
                images.append(image)

        return images

    @staticmethod
    def get_image_cookies(image_url: str) -> None:
        # 绿联图片流接口依赖加密鉴权头，当前图片代理仅支持Cookie注入。
        """
        保留图片 Cookie 接口；绿联图片认证不通过 Cookie 提供。

        :param image_url: 兼容统一接口的图片地址，当前不读取
        :return: 始终返回 None
        """
        return None
