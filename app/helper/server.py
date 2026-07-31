import json
import platform
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse, urlsplit

from app.core.cache import cached
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.db.subscribe_oper import SubscribeOper
from app.db.systemconfig_oper import SystemConfigOper
from app.db.workflow_oper import WorkflowOper
from app.log import logger
from app.schemas.types import MediaType, SystemConfigKey, media_type_to_agent
from app.utils.http import AsyncRequestUtils, RequestUtils
from app.utils.media import resolve_media_identity
from app.utils.system import SystemUtils
from version import APP_VERSION, FRONTEND_VERSION


class MoviePilotServerHelper:
    """
    MoviePilot 服务端请求辅助工具。
    """

    USER_UID_HEADER = "X-MoviePilot-User-Uid"
    _USAGE_REPORT_PATH = "/usage/report"
    _USAGE_STATISTIC_PATH = "/usage/statistic"
    _PLUGIN_INSTALL_PATH = "/plugin/install"
    _PLUGIN_RATING_PATH = "/plugin/rating"
    _PLUGIN_STATISTIC_PATH = "/plugin/statistic"
    _SUBSCRIBE_ADD_PATH = "/subscribe/add"
    _SUBSCRIBE_DONE_PATH = "/subscribe/done"
    _SUBSCRIBE_REPORT_PATH = "/subscribe/report"
    _SUBSCRIBE_STATISTIC_PATH = "/subscribe/statistic"
    _SUBSCRIBE_SHARE_PATH = "/subscribe/share"
    _SUBSCRIBE_SHARES_PATH = "/subscribe/shares"
    _SUBSCRIBE_SHARE_STATISTICS_PATH = "/subscribe/share/statistics"
    _SUBSCRIBE_FORK_PATH = "/subscribe/fork"
    _WORKFLOW_SHARE_PATH = "/workflow/share"
    _WORKFLOW_SHARES_PATH = "/workflow/shares"
    _WORKFLOW_FORK_PATH = "/workflow/fork"
    _RECOGNIZE_SHARE_PATH = "/recognize/share"
    _USER_PERMISSIONS_PATH = "/user/permissions"
    _LOCAL_REPO_PREFIX = "local://"
    _user_uid: Optional[str] = None
    _github_user: Optional[str] = None

    @classmethod
    def get_user_uid(cls) -> Optional[str]:
        """
        获取当前安装实例用于服务端统计识别的稳定用户 ID。
        """
        if cls._user_uid is None:
            cls._user_uid = SystemUtils.generate_user_unique_id()
        return cls._user_uid

    @staticmethod
    def is_server_url(url: str) -> bool:
        """
        判断请求地址是否指向配置中的 MoviePilot 服务端。
        """
        server_host = (settings.MP_SERVER_HOST or "").strip().rstrip("/")
        if not server_host or not url:
            return False

        try:
            target = urlparse(str(url).strip())
            server = urlparse(server_host)
        except Exception:
            return False

        return bool(
            target.scheme
            and target.netloc
            and server.scheme
            and server.netloc
            and target.scheme == server.scheme
            and target.netloc == server.netloc
        )

    @classmethod
    def build_headers(
            cls,
            url: str,
            headers: Optional[dict] = None,
            content_type: Optional[str] = None,
    ) -> dict:
        """
        构建访问 MoviePilot 服务端需要的请求头。
        """
        request_headers = {
            key: value
            for key, value in (headers or {}).items()
            if value is not None
        }
        if content_type and not cls._has_header(request_headers, "Content-Type"):
            request_headers["Content-Type"] = content_type
        if not cls.is_server_url(url) or cls._has_header(request_headers, cls.USER_UID_HEADER):
            return request_headers

        user_uid = cls.get_user_uid()
        if user_uid:
            request_headers[cls.USER_UID_HEADER] = user_uid
        request_headers["User-Agent"] = settings.USER_AGENT
        return request_headers

    @classmethod
    def get_user_uuid(cls) -> str:
        """
        获取当前用户 UUID。
        """
        user_uid = cls.get_user_uid()
        if user_uid:
            logger.info(f"当前用户UUID: {user_uid}")
        return user_uid or ""

    @classmethod
    def get_github_user(cls) -> str:
        """
        获取当前 GitHub 用户名。
        """
        if cls._github_user is None and settings.GITHUB_HEADERS:
            res = RequestUtils(
                headers=settings.GITHUB_HEADERS,
                proxies=settings.PROXY,
                timeout=15,
            ).get_res("https://api.github.com/user")
            if res:
                cls._github_user = res.json().get("login")
                logger.info(f"当前Github用户: {cls._github_user}")
        return cls._github_user or ""

    @classmethod
    async def async_get_github_user(cls) -> str:
        """
        异步获取当前 GitHub 用户名。
        """
        if cls._github_user is None and settings.GITHUB_HEADERS:
            res = await AsyncRequestUtils(
                headers=settings.GITHUB_HEADERS,
                proxies=settings.PROXY,
                timeout=15,
            ).get_res("https://api.github.com/user")
            if res:
                cls._github_user = res.json().get("login")
                logger.info(f"当前Github用户: {cls._github_user}")
        return cls._github_user or ""

    @classmethod
    def user_permissions(cls, github_user: str):
        """
        查询服务端用户权限。
        """
        return cls._get(
            cls._server_url(cls._USER_PERMISSIONS_PATH),
            params={"github_user": github_user},
            include_user_uid=False,
            timeout=5,
        )

    @classmethod
    async def async_user_permissions(cls, github_user: str):
        """
        异步查询服务端用户权限。
        """
        return await cls._async_get(
            cls._server_url(cls._USER_PERMISSIONS_PATH),
            params={"github_user": github_user},
            include_user_uid=False,
            timeout=5,
        )

    @classmethod
    def get_user_permissions(cls) -> Dict[str, Any]:
        """
        获取当前用户在服务端配置中的权限。
        """
        github_user = cls.get_github_user()
        if not github_user:
            return {}
        try:
            res = cls.user_permissions(github_user)
            if res is not None and res.status_code == 200:
                return res.json()
        except Exception as err:
            logger.debug(f"获取服务端用户权限失败：{str(err)}")
        return {}

    @classmethod
    async def async_get_user_permissions(cls) -> Dict[str, Any]:
        """
        异步获取当前用户在服务端配置中的权限。
        """
        github_user = await cls.async_get_github_user()
        if not github_user:
            return {}
        try:
            res = await cls.async_user_permissions(github_user)
            if res is not None and res.status_code == 200:
                return res.json()
        except Exception as err:
            logger.debug(f"异步获取服务端用户权限失败：{str(err)}")
        return {}

    @classmethod
    def is_admin_user(cls) -> bool:
        """
        判断当前用户是否为共享管理用户。
        """
        permissions = cls.get_user_permissions()
        return bool(
            permissions.get("is_admin")
            or permissions.get("subscribe_share_manage")
            or permissions.get("workflow_share_manage")
        )

    @classmethod
    async def async_is_admin_user(cls) -> bool:
        """
        异步判断当前用户是否为共享管理用户。
        """
        permissions = await cls.async_get_user_permissions()
        return bool(
            permissions.get("is_admin")
            or permissions.get("subscribe_share_manage")
            or permissions.get("workflow_share_manage")
        )

    @staticmethod
    def get_frontend_version() -> str:
        """
        获取当前前端版本。
        """
        if SystemUtils.is_frozen() and SystemUtils.is_windows():
            version_file = settings.CONFIG_PATH.parent / "nginx" / "html" / "version.txt"
        else:
            version_file = Path(settings.FRONTEND_PATH) / "version.txt"
        if version_file.exists():
            try:
                with open(version_file, "r", encoding="utf-8", errors="replace") as file:
                    version = str(file.read()).strip()
                return version or FRONTEND_VERSION
            except Exception as err:
                logger.debug(f"加载版本文件 {version_file} 出错：{str(err)}")
        return FRONTEND_VERSION

    @classmethod
    def build_usage_payload(cls) -> Dict[str, Any]:
        """
        构建安装版本统计上报载荷。
        """
        return {
            "user_uid": cls.get_user_uid(),
            "backend_version": APP_VERSION,
            "frontend_version": cls.get_frontend_version(),
            "version_flag": settings.VERSION_FLAG,
            "platform": f"{platform.system()} {platform.release()}".strip(),
            "arch": SystemUtils.cpu_arch(),
        }

    @classmethod
    def report_usage(cls) -> bool:
        """
        上报当前安装实例的版本统计。
        """
        if not settings.USAGE_STATISTIC_SHARE:
            return False
        payload = cls.build_usage_payload()
        if not payload.get("user_uid"):
            return False
        try:
            res = cls.usage_report(payload)
            return bool(res is not None and res.status_code == 200)
        except Exception as err:
            logger.debug(f"上报安装版本统计失败：{str(err)}")
            return False

    @classmethod
    async def async_report_usage(cls) -> bool:
        """
        异步上报当前安装实例的版本统计。
        """
        if not settings.USAGE_STATISTIC_SHARE:
            return False
        payload = cls.build_usage_payload()
        if not payload.get("user_uid"):
            return False
        try:
            res = await cls.async_usage_report(payload)
            return bool(res is not None and res.status_code == 200)
        except Exception as err:
            logger.debug(f"异步上报安装版本统计失败：{str(err)}")
            return False

    @classmethod
    async def async_get_usage_statistic(cls) -> Dict[str, Any]:
        """
        异步获取安装版本统计报表。
        """
        if not settings.USAGE_STATISTIC_SHARE:
            return {}
        try:
            res = await cls.async_usage_statistic()
            if res is not None and res.status_code == 200:
                return res.json()
        except Exception as err:
            logger.debug(f"异步获取安装版本统计报表失败：{str(err)}")
        return {}

    @classmethod
    def init_subscribe_report(cls) -> None:
        """
        初始化订阅统计上报状态。
        """
        systemconfig = SystemConfigOper()
        if settings.SUBSCRIBE_STATISTIC_SHARE:
            if not systemconfig.get(SystemConfigKey.SubscribeReport):
                if cls.sub_report():
                    systemconfig.set(SystemConfigKey.SubscribeReport, "1")

    @classmethod
    def init_plugin_report(cls) -> None:
        """
        初始化插件安装统计上报状态。
        """
        systemconfig = SystemConfigOper()
        if settings.PLUGIN_STATISTIC_SHARE:
            if not systemconfig.get(SystemConfigKey.PluginInstallReport):
                if cls.install_plugin_report():
                    systemconfig.set(SystemConfigKey.PluginInstallReport, "1")

    @staticmethod
    def _handle_list_response(res) -> List[dict]:
        """
        处理服务端返回的列表响应。
        """
        if res is not None and res.status_code == 200:
            return res.json()
        return []

    @staticmethod
    def _handle_response(res, clear_cache=None) -> Tuple[bool, str]:
        """
        处理服务端写入类接口响应。
        """
        if res is None:
            return False, "连接MoviePilot服务器失败"
        if res.status_code == 200:
            if clear_cache:
                clear_cache()
            return True, ""
        try:
            return False, res.json().get("message", "未知错误")
        except (json.JSONDecodeError, ValueError, AttributeError):
            return False, f"响应解析失败: {getattr(res, 'text', '')[:100]}..."

    @classmethod
    def usage_report(cls, payload: Dict[str, Any]):
        """
        上报安装版本统计。
        """
        return cls._post_json(cls._server_url(cls._USAGE_REPORT_PATH), payload, timeout=5)

    @classmethod
    async def async_usage_report(cls, payload: Dict[str, Any]):
        """
        异步上报安装版本统计。
        """
        return await cls._async_post_json(cls._server_url(cls._USAGE_REPORT_PATH), payload, timeout=5)

    @classmethod
    def usage_statistic(cls):
        """
        获取安装版本统计报表。
        """
        return cls._get(cls._server_url(cls._USAGE_STATISTIC_PATH), timeout=10)

    @classmethod
    async def async_usage_statistic(cls):
        """
        异步获取安装版本统计报表。
        """
        return await cls._async_get(cls._server_url(cls._USAGE_STATISTIC_PATH), timeout=10)

    @classmethod
    def plugin_statistic(cls):
        """
        获取插件安装统计。
        """
        return cls._get(cls._server_url(cls._PLUGIN_STATISTIC_PATH), timeout=10)

    @classmethod
    async def async_plugin_statistic(cls):
        """
        异步获取插件安装统计。
        """
        return await cls._async_get(cls._server_url(cls._PLUGIN_STATISTIC_PATH), timeout=10)

    @classmethod
    async def async_plugin_ratings(cls, plugin_ids: Optional[List[str]] = None):
        """
        异步批量查询中心端插件评分。
        """
        params = {"plugin_ids": ",".join(plugin_ids)} if plugin_ids is not None else None
        return await cls._async_get(
            cls._server_url(cls._PLUGIN_RATING_PATH),
            params=params,
            timeout=10,
        )

    @classmethod
    async def async_plugin_rating(cls, plugin_id: str):
        """
        异步查询中心端单个插件评分。
        """
        return await cls._async_get(
            f"{cls._server_url(cls._PLUGIN_RATING_PATH)}/{quote(plugin_id, safe='')}",
            timeout=10,
        )

    @classmethod
    async def async_rate_plugin(cls, plugin_id: str, rating: float):
        """
        异步提交当前安装实例的插件评分。
        """
        return await cls._async_post_json(
            f"{cls._server_url(cls._PLUGIN_RATING_PATH)}/{quote(plugin_id, safe='')}",
            {"rating": rating},
            timeout=10,
        )

    @classmethod
    def plugin_install(cls, plugin_id: str, payload: Dict[str, Any]):
        """
        上报单个插件安装统计。
        """
        return cls._post_json(f"{cls._server_url(cls._PLUGIN_INSTALL_PATH)}/{plugin_id}", payload, timeout=5)

    @classmethod
    async def async_plugin_install(cls, plugin_id: str, payload: Dict[str, Any]):
        """
        异步上报单个插件安装统计。
        """
        return await cls._async_post_json(
            f"{cls._server_url(cls._PLUGIN_INSTALL_PATH)}/{plugin_id}",
            payload,
            timeout=5,
        )

    @classmethod
    def plugin_install_report(cls, plugins: List[Dict[str, Any]]):
        """
        批量上报插件安装统计。
        """
        return cls._post_json(cls._server_url(cls._PLUGIN_INSTALL_PATH), {"plugins": plugins}, timeout=5)

    @classmethod
    async def async_plugin_install_report(cls, plugins: List[Dict[str, Any]]):
        """
        异步批量上报插件安装统计。
        """
        return await cls._async_post_json(
            cls._server_url(cls._PLUGIN_INSTALL_PATH),
            {"plugins": plugins},
            timeout=5,
        )

    @classmethod
    @cached(maxsize=1, ttl=1800)
    def get_plugin_statistic(cls) -> Dict:
        """
        获取插件安装统计。
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return {}
        res = cls.plugin_statistic()
        if res is not None and res.status_code == 200:
            return res.json()
        return {}

    @classmethod
    async def async_get_plugin_statistic(cls) -> Dict:
        """
        异步获取插件安装统计。
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return {}
        res = await cls.async_plugin_statistic()
        if res is not None and res.status_code == 200:
            return res.json()
        return {}

    @classmethod
    async def async_get_plugin_ratings(
            cls,
            plugin_ids: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        批量获取插件评分，中心端不可用时返回空结果。
        """
        try:
            res = await cls.async_plugin_ratings(plugin_ids)
            if res is not None and res.status_code == 200:
                return res.json()
        except Exception as err:
            logger.debug(f"批量获取插件评分失败：{str(err)}")
        return {}

    @classmethod
    async def async_get_plugin_rating(cls, plugin_id: str) -> Dict[str, Any]:
        """
        获取单个插件评分，中心端不可用时返回零评分。
        """
        empty_rating = {
            "plugin_id": plugin_id,
            "average_rating": 0.0,
            "rating_count": 0,
            "user_rating": None,
        }
        try:
            res = await cls.async_plugin_rating(plugin_id)
            if res is not None and res.status_code == 200:
                return res.json()
        except Exception as err:
            logger.debug(f"获取插件 {plugin_id} 评分失败：{str(err)}")
        return empty_rating

    @classmethod
    async def async_submit_plugin_rating(
            cls,
            plugin_id: str,
            rating: float,
    ) -> Optional[Dict[str, Any]]:
        """
        提交插件评分，成功时返回最新评分结果。
        """
        try:
            res = await cls.async_rate_plugin(plugin_id, rating)
            if res is not None and res.status_code == 200:
                return res.json()
        except Exception as err:
            logger.debug(f"提交插件 {plugin_id} 评分失败：{str(err)}")
        return None

    @classmethod
    def install_plugin_reg(cls, plugin_id: str, repo_url: Optional[str] = None) -> bool:
        """
        上报单个插件安装统计。
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return False
        if not plugin_id:
            return False
        res = cls.plugin_install(plugin_id, {
            "plugin_id": plugin_id,
            "repo_url": cls.sanitize_plugin_repo_url(repo_url),
        })
        return bool(res is not None and res.status_code == 200)

    @classmethod
    async def async_install_plugin_reg(cls, plugin_id: str, repo_url: Optional[str] = None) -> bool:
        """
        异步上报单个插件安装统计。
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return False
        if not plugin_id:
            return False
        res = await cls.async_plugin_install(plugin_id, {
            "plugin_id": plugin_id,
            "repo_url": cls.sanitize_plugin_repo_url(repo_url),
        })
        return bool(res is not None and res.status_code == 200)

    @classmethod
    def install_plugin_report(cls, items: Optional[List[Tuple[str, Optional[str]]]] = None) -> bool:
        """
        批量上报存量插件安装统计。
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return False
        payload_plugins = cls._build_plugin_report_payload(items)
        if not payload_plugins:
            return False
        res = cls.plugin_install_report(payload_plugins)
        return bool(res is not None and res.status_code == 200)

    @classmethod
    async def async_install_plugin_report(cls, items: Optional[List[Tuple[str, Optional[str]]]] = None) -> bool:
        """
        异步批量上报存量插件安装统计。
        """
        if not settings.PLUGIN_STATISTIC_SHARE:
            return False
        payload_plugins = cls._build_plugin_report_payload(items)
        if not payload_plugins:
            return False
        res = await cls.async_plugin_install_report(payload_plugins)
        return bool(res is not None and res.status_code == 200)

    @classmethod
    def subscribe_statistic(cls, params: Dict[str, Any]):
        """
        获取订阅统计数据。
        """
        return cls._get(cls._server_url(cls._SUBSCRIBE_STATISTIC_PATH), params=params, timeout=15)

    @classmethod
    async def async_subscribe_statistic(cls, params: Dict[str, Any]):
        """
        异步获取订阅统计数据。
        """
        return await cls._async_get(cls._server_url(cls._SUBSCRIBE_STATISTIC_PATH), params=params, timeout=15)

    @classmethod
    def subscribe_add(cls, payload: Dict[str, Any]):
        """
        新增订阅统计。
        """
        return cls._post_json(cls._server_url(cls._SUBSCRIBE_ADD_PATH), payload, timeout=5)

    @classmethod
    async def async_subscribe_add(cls, payload: Dict[str, Any]):
        """
        异步新增订阅统计。
        """
        return await cls._async_post_json(cls._server_url(cls._SUBSCRIBE_ADD_PATH), payload, timeout=5)

    @classmethod
    def subscribe_done(cls, payload: Dict[str, Any]):
        """
        完成订阅统计。
        """
        return cls._post_json(cls._server_url(cls._SUBSCRIBE_DONE_PATH), payload, timeout=5)

    @classmethod
    def subscribe_report(cls, subscribes: List[Dict[str, Any]]):
        """
        批量上报存量订阅统计。
        """
        return cls._post_json(
            cls._server_url(cls._SUBSCRIBE_REPORT_PATH),
            {"subscribes": subscribes},
            timeout=10,
        )

    @classmethod
    def subscribe_share(cls, payload: Dict[str, Any]):
        """
        分享订阅数据。
        """
        return cls._post_json(cls._server_url(cls._SUBSCRIBE_SHARE_PATH), payload, timeout=10)

    @classmethod
    async def async_subscribe_share(cls, payload: Dict[str, Any]):
        """
        异步分享订阅数据。
        """
        return await cls._async_post_json(cls._server_url(cls._SUBSCRIBE_SHARE_PATH), payload, timeout=10)

    @classmethod
    def subscribe_share_delete(cls, share_id: int, share_uid: str):
        """
        删除订阅分享数据。
        """
        return cls._delete(
            f"{cls._server_url(cls._SUBSCRIBE_SHARE_PATH)}/{share_id}",
            params={"share_uid": share_uid},
            timeout=5,
        )

    @classmethod
    async def async_subscribe_share_delete(cls, share_id: int, share_uid: str):
        """
        异步删除订阅分享数据。
        """
        return await cls._async_delete(
            f"{cls._server_url(cls._SUBSCRIBE_SHARE_PATH)}/{share_id}",
            params={"share_uid": share_uid},
            timeout=5,
        )

    @classmethod
    def subscribe_fork(cls, share_id: int):
        """
        复用订阅分享数据。
        """
        return cls._get(f"{cls._server_url(cls._SUBSCRIBE_FORK_PATH)}/{share_id}", timeout=5)

    @classmethod
    async def async_subscribe_fork(cls, share_id: int):
        """
        异步复用订阅分享数据。
        """
        return await cls._async_get(f"{cls._server_url(cls._SUBSCRIBE_FORK_PATH)}/{share_id}", timeout=5)

    @classmethod
    def subscribe_shares(cls, params: Dict[str, Any]):
        """
        获取订阅分享数据。
        """
        return cls._get(cls._server_url(cls._SUBSCRIBE_SHARES_PATH), params=params, timeout=15)

    @classmethod
    async def async_subscribe_shares(cls, params: Dict[str, Any]):
        """
        异步获取订阅分享数据。
        """
        return await cls._async_get(cls._server_url(cls._SUBSCRIBE_SHARES_PATH), params=params, timeout=15)

    @classmethod
    def subscribe_share_statistics(cls):
        """
        获取订阅分享统计数据。
        """
        return cls._get(cls._server_url(cls._SUBSCRIBE_SHARE_STATISTICS_PATH), timeout=15)

    @classmethod
    async def async_subscribe_share_statistics(cls):
        """
        异步获取订阅分享统计数据。
        """
        return await cls._async_get(cls._server_url(cls._SUBSCRIBE_SHARE_STATISTICS_PATH), timeout=15)

    @staticmethod
    def _build_subscribe_query_params(
            page: Optional[int] = 1,
            count: Optional[int] = 30,
            genre_id: Optional[int] = None,
            min_rating: Optional[float] = None,
            max_rating: Optional[float] = None,
            sort_type: Optional[str] = None,
            **extra,
    ) -> Dict[str, Any]:
        """
        构建订阅统计与分享查询参数。
        """
        params = {
            "page": page,
            "count": count,
            **extra,
        }
        if genre_id is not None:
            params["genre_id"] = genre_id
        if min_rating is not None:
            params["min_rating"] = min_rating
        if max_rating is not None:
            params["max_rating"] = max_rating
        if sort_type is not None:
            params["sort_type"] = sort_type
        return params

    @classmethod
    @cached(region="subscribe_share", maxsize=32, ttl=1800, skip_empty=True)
    def get_subscribe_statistic(
            cls,
            stype: str,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
            genre_id: Optional[int] = None,
            min_rating: Optional[float] = None,
            max_rating: Optional[float] = None,
            sort_type: Optional[str] = None,
    ) -> List[dict]:
        """
        获取订阅统计数据。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return []
        params = cls._build_subscribe_query_params(
            page=page,
            count=count,
            genre_id=genre_id,
            min_rating=min_rating,
            max_rating=max_rating,
            sort_type=sort_type,
            stype=stype,
        )
        return cls._handle_list_response(cls.subscribe_statistic(params))

    @classmethod
    @cached(region="subscribe_share", maxsize=32, ttl=1800, skip_empty=True)
    async def async_get_subscribe_statistic(
            cls,
            stype: str,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
            genre_id: Optional[int] = None,
            min_rating: Optional[float] = None,
            max_rating: Optional[float] = None,
            sort_type: Optional[str] = None,
    ) -> List[dict]:
        """
        异步获取订阅统计数据。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return []
        params = cls._build_subscribe_query_params(
            page=page,
            count=count,
            genre_id=genre_id,
            min_rating=min_rating,
            max_rating=max_rating,
            sort_type=sort_type,
            stype=stype,
        )
        return cls._handle_list_response(await cls.async_subscribe_statistic(params))

    @classmethod
    def sub_reg(cls, sub: dict) -> bool:
        """
        新增订阅统计。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False
        res = cls.subscribe_add(sub)
        return bool(res is not None and res.status_code == 200)

    @classmethod
    async def async_sub_reg(cls, sub: dict) -> bool:
        """
        异步新增订阅统计。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False
        res = await cls.async_subscribe_add(sub)
        return bool(res is not None and res.status_code == 200)

    @classmethod
    def sub_done(cls, sub: dict) -> bool:
        """
        完成订阅统计。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False
        res = cls.subscribe_done(sub)
        return bool(res is not None and res.status_code == 200)

    @classmethod
    def sub_reg_async(cls, sub: dict) -> bool:
        """
        开线程新增订阅统计。
        """
        Thread(target=cls.sub_reg, args=(sub,)).start()
        return True

    @classmethod
    def sub_done_async(cls, sub: dict) -> bool:
        """
        开线程完成订阅统计。
        """
        Thread(target=cls.sub_done, args=(sub,)).start()
        return True

    @classmethod
    def sub_report(cls) -> bool:
        """
        上报存量订阅统计。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False
        subscribes = SubscribeOper().list()
        if not subscribes:
            return True
        res = cls.subscribe_report([sub.to_dict() for sub in subscribes])
        return bool(res is not None and res.status_code == 200)

    @classmethod
    def sub_share(
            cls,
            subscribe_id: int,
            share_title: str,
            share_comment: str,
            share_user: str,
    ) -> Tuple[bool, str]:
        """
        分享订阅。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False, "当前没有开启订阅数据共享功能"
        subscribe = SubscribeOper().get(subscribe_id)
        if not subscribe:
            return False, "订阅不存在"
        subscribe_dict = subscribe.to_dict()
        subscribe_dict.pop("id", None)
        payload = {
            "share_title": share_title,
            "share_comment": share_comment,
            "share_user": share_user,
            "share_uid": cls.get_user_uuid(),
            **subscribe_dict,
        }
        return cls._handle_response(cls.subscribe_share(payload), cls._clear_subscribe_share_cache)

    @classmethod
    async def async_sub_share(
            cls,
            subscribe_id: int,
            share_title: str,
            share_comment: str,
            share_user: str,
    ) -> Tuple[bool, str]:
        """
        异步分享订阅。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False, "当前没有开启订阅数据共享功能"
        subscribe = await SubscribeOper().async_get(subscribe_id)
        if not subscribe:
            return False, "订阅不存在"
        subscribe_dict = subscribe.to_dict()
        subscribe_dict.pop("id", None)
        payload = {
            "share_title": share_title,
            "share_comment": share_comment,
            "share_user": share_user,
            "share_uid": cls.get_user_uuid(),
            **subscribe_dict,
        }
        return cls._handle_response(
            await cls.async_subscribe_share(payload),
            cls._clear_subscribe_share_cache,
        )

    @classmethod
    def share_delete(cls, share_id: int) -> Tuple[bool, str]:
        """
        删除订阅分享。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False, "当前没有开启订阅数据共享功能"
        return cls._handle_response(
            cls.subscribe_share_delete(share_id, cls.get_user_uuid()),
            cls._clear_subscribe_share_cache,
        )

    @classmethod
    async def async_share_delete(cls, share_id: int) -> Tuple[bool, str]:
        """
        异步删除订阅分享。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False, "当前没有开启订阅数据共享功能"
        return cls._handle_response(
            await cls.async_subscribe_share_delete(share_id, cls.get_user_uuid()),
            cls._clear_subscribe_share_cache,
        )

    @classmethod
    def sub_fork(cls, share_id: int) -> Tuple[bool, str]:
        """
        复用订阅分享。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False, "当前没有开启订阅数据共享功能"
        return cls._handle_response(cls.subscribe_fork(share_id))

    @classmethod
    async def async_sub_fork(cls, share_id: int) -> Tuple[bool, str]:
        """
        异步复用订阅分享。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return False, "当前没有开启订阅数据共享功能"
        return cls._handle_response(await cls.async_subscribe_fork(share_id))

    @classmethod
    @cached(region="subscribe_share", maxsize=32, ttl=1800, skip_empty=True)
    def get_subscribe_shares(
            cls,
            name: Optional[str] = None,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
            genre_id: Optional[int] = None,
            min_rating: Optional[float] = None,
            max_rating: Optional[float] = None,
            sort_type: Optional[str] = None,
    ) -> List[dict]:
        """
        获取订阅分享数据。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return []
        params = cls._build_subscribe_query_params(
            page=page,
            count=count,
            genre_id=genre_id,
            min_rating=min_rating,
            max_rating=max_rating,
            sort_type=sort_type,
            name=name,
        )
        return cls._handle_list_response(cls.subscribe_shares(params))

    @classmethod
    @cached(region="subscribe_share", maxsize=32, ttl=1800, skip_empty=True)
    async def async_get_subscribe_shares(
            cls,
            name: Optional[str] = None,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
            genre_id: Optional[int] = None,
            min_rating: Optional[float] = None,
            max_rating: Optional[float] = None,
            sort_type: Optional[str] = None,
    ) -> List[dict]:
        """
        异步获取订阅分享数据。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return []
        params = cls._build_subscribe_query_params(
            page=page,
            count=count,
            genre_id=genre_id,
            min_rating=min_rating,
            max_rating=max_rating,
            sort_type=sort_type,
            name=name,
        )
        return cls._handle_list_response(await cls.async_subscribe_shares(params))

    @classmethod
    @cached(region="subscribe_share", maxsize=32, ttl=1800, skip_empty=True)
    def get_subscribe_share_statistics(cls) -> List[dict]:
        """
        获取订阅分享统计数据。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return []
        return cls._handle_list_response(cls.subscribe_share_statistics())

    @classmethod
    @cached(region="subscribe_share", maxsize=32, ttl=1800, skip_empty=True)
    async def async_get_subscribe_share_statistics(cls) -> List[dict]:
        """
        异步获取订阅分享统计数据。
        """
        if not settings.SUBSCRIBE_STATISTIC_SHARE:
            return []
        return cls._handle_list_response(await cls.async_subscribe_share_statistics())

    @classmethod
    def workflow_share(cls, payload: Dict[str, Any]):
        """
        分享工作流数据。
        """
        return cls._post_json(cls._server_url(cls._WORKFLOW_SHARE_PATH), payload, timeout=10)

    @classmethod
    async def async_workflow_share(cls, payload: Dict[str, Any]):
        """
        异步分享工作流数据。
        """
        return await cls._async_post_json(cls._server_url(cls._WORKFLOW_SHARE_PATH), payload, timeout=10)

    @classmethod
    def workflow_share_delete(cls, share_id: int, share_uid: str):
        """
        删除工作流分享数据。
        """
        return cls._delete(
            f"{cls._server_url(cls._WORKFLOW_SHARE_PATH)}/{share_id}",
            params={"share_uid": share_uid},
            timeout=5,
        )

    @classmethod
    async def async_workflow_share_delete(cls, share_id: int, share_uid: str):
        """
        异步删除工作流分享数据。
        """
        return await cls._async_delete(
            f"{cls._server_url(cls._WORKFLOW_SHARE_PATH)}/{share_id}",
            params={"share_uid": share_uid},
            timeout=5,
        )

    @classmethod
    def workflow_fork(cls, share_id: int):
        """
        复用工作流分享数据。
        """
        return cls._get(f"{cls._server_url(cls._WORKFLOW_FORK_PATH)}/{share_id}", timeout=5)

    @classmethod
    async def async_workflow_fork(cls, share_id: int):
        """
        异步复用工作流分享数据。
        """
        return await cls._async_get(f"{cls._server_url(cls._WORKFLOW_FORK_PATH)}/{share_id}", timeout=5)

    @classmethod
    def workflow_shares(cls, params: Dict[str, Any]):
        """
        获取工作流分享数据。
        """
        return cls._get(cls._server_url(cls._WORKFLOW_SHARES_PATH), params=params, timeout=15)

    @classmethod
    async def async_workflow_shares(cls, params: Dict[str, Any]):
        """
        异步获取工作流分享数据。
        """
        return await cls._async_get(cls._server_url(cls._WORKFLOW_SHARES_PATH), params=params, timeout=15)

    @staticmethod
    def _prepare_workflow_data(workflow) -> dict:
        """
        准备工作流分享数据。
        """
        workflow_dict = workflow.to_dict()
        workflow_dict.pop("id", None)
        workflow_dict.pop("context", None)
        workflow_dict["actions"] = json.dumps(workflow_dict["actions"] or [])
        workflow_dict["flows"] = json.dumps(workflow_dict["flows"] or [])
        return workflow_dict

    @classmethod
    def workflow_share_by_id(
            cls,
            workflow_id: int,
            share_title: str,
            share_comment: str,
            share_user: str,
    ) -> Tuple[bool, str]:
        """
        分享工作流。
        """
        if not settings.WORKFLOW_STATISTIC_SHARE:
            return False, "当前没有开启工作流数据共享功能"
        workflow = WorkflowOper().get(workflow_id)
        valid, message = cls._validate_workflow(workflow)
        if not valid:
            return False, message
        payload = {
            "share_title": share_title,
            "share_comment": share_comment,
            "share_user": share_user,
            "share_uid": cls.get_user_uuid(),
            **cls._prepare_workflow_data(workflow),
        }
        return cls._handle_response(cls.workflow_share(payload), cls._clear_workflow_share_cache)

    @classmethod
    async def async_workflow_share_by_id(
            cls,
            workflow_id: int,
            share_title: str,
            share_comment: str,
            share_user: str,
    ) -> Tuple[bool, str]:
        """
        异步分享工作流。
        """
        if not settings.WORKFLOW_STATISTIC_SHARE:
            return False, "当前没有开启工作流数据共享功能"
        workflow = await WorkflowOper().async_get(workflow_id)
        valid, message = cls._validate_workflow(workflow)
        if not valid:
            return False, message
        payload = {
            "share_title": share_title,
            "share_comment": share_comment,
            "share_user": share_user,
            "share_uid": cls.get_user_uuid(),
            **cls._prepare_workflow_data(workflow),
        }
        return cls._handle_response(
            await cls.async_workflow_share(payload),
            cls._clear_workflow_share_cache,
        )

    @classmethod
    def workflow_share_delete_by_id(cls, share_id: int) -> Tuple[bool, str]:
        """
        删除工作流分享。
        """
        if not settings.WORKFLOW_STATISTIC_SHARE:
            return False, "当前没有开启工作流数据共享功能"
        return cls._handle_response(
            cls.workflow_share_delete(share_id, cls.get_user_uuid()),
            cls._clear_workflow_share_cache,
        )

    @classmethod
    async def async_workflow_share_delete_by_id(cls, share_id: int) -> Tuple[bool, str]:
        """
        异步删除工作流分享。
        """
        if not settings.WORKFLOW_STATISTIC_SHARE:
            return False, "当前没有开启工作流数据共享功能"
        return cls._handle_response(
            await cls.async_workflow_share_delete(share_id, cls.get_user_uuid()),
            cls._clear_workflow_share_cache,
        )

    @classmethod
    def workflow_fork_by_id(cls, share_id: int) -> Tuple[bool, str]:
        """
        复用工作流分享。
        """
        if not settings.WORKFLOW_STATISTIC_SHARE:
            return False, "当前没有开启工作流数据共享功能"
        return cls._handle_response(cls.workflow_fork(share_id))

    @classmethod
    async def async_workflow_fork_by_id(cls, share_id: int) -> Tuple[bool, str]:
        """
        异步复用工作流分享。
        """
        if not settings.WORKFLOW_STATISTIC_SHARE:
            return False, "当前没有开启工作流数据共享功能"
        return cls._handle_response(await cls.async_workflow_fork(share_id))

    @classmethod
    @cached(region="workflow_share", maxsize=1, skip_empty=True)
    def get_workflow_shares(
            cls,
            name: Optional[str] = None,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
    ) -> List[dict]:
        """
        获取工作流分享数据。
        """
        if not settings.WORKFLOW_STATISTIC_SHARE:
            return []
        return cls._handle_list_response(cls.workflow_shares({
            "name": name,
            "page": page,
            "count": count,
        }))

    @classmethod
    @cached(region="workflow_share", maxsize=1, skip_empty=True)
    async def async_get_workflow_shares(
            cls,
            name: Optional[str] = None,
            page: Optional[int] = 1,
            count: Optional[int] = 30,
    ) -> List[dict]:
        """
        异步获取工作流分享数据。
        """
        if not settings.WORKFLOW_STATISTIC_SHARE:
            return []
        return cls._handle_list_response(await cls.async_workflow_shares({
            "name": name,
            "page": page,
            "count": count,
        }))

    @staticmethod
    def _validate_workflow(workflow) -> Tuple[bool, str]:
        """
        验证工作流是否可以分享。
        """
        if not workflow:
            return False, "工作流不存在"
        if not workflow.actions or not workflow.flows:
            return False, "请分享有动作和流程的工作流"
        return True, ""

    @classmethod
    def recognize_share_url(cls) -> Optional[str]:
        """
        获取共享识别服务端地址。
        """
        custom_api = (settings.MEDIA_RECOGNIZE_SHARE_API or "").strip()
        if custom_api:
            return custom_api.rstrip("/")
        server_host = (settings.MP_SERVER_HOST or "").strip().rstrip("/")
        if not server_host:
            return None
        return f"{server_host}{cls._RECOGNIZE_SHARE_PATH}"

    @classmethod
    def recognize_query(cls, params: Dict[str, Any]):
        """
        查询共享识别结果。
        """
        api_url = cls.recognize_share_url()
        if not api_url:
            return None
        return cls._get(api_url, params=params, timeout=5)

    @classmethod
    async def async_recognize_query(cls, params: Dict[str, Any]):
        """
        异步查询共享识别结果。
        """
        api_url = cls.recognize_share_url()
        if not api_url:
            return None
        return await cls._async_get(api_url, params=params, timeout=5)

    @classmethod
    def recognize_report(cls, payload: Dict[str, Any]):
        """
        上报共享识别结果。
        """
        api_url = cls.recognize_share_url()
        if not api_url:
            return None
        return cls._post_json(api_url, payload, timeout=5)

    @classmethod
    async def async_recognize_report(cls, payload: Dict[str, Any]):
        """
        异步上报共享识别结果。
        """
        api_url = cls.recognize_share_url()
        if not api_url:
            return None
        return await cls._async_post_json(api_url, payload, timeout=5)

    @classmethod
    def query_recognize_share(
            cls,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
    ) -> Optional[dict]:
        """
        查询共享识别结果。
        """
        if not settings.MEDIA_RECOGNIZE_SHARE:
            return None
        params = cls._build_recognize_query_params(
            meta=meta,
            mtype=mtype,
            keyword_meta=keyword_meta,
        )
        if not params:
            return None
        response = cls.recognize_query(params)
        return cls._parse_recognize_response(response, params.get("keyword"))

    @classmethod
    async def async_query_recognize_share(
            cls,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
    ) -> Optional[dict]:
        """
        异步查询共享识别结果。
        """
        if not settings.MEDIA_RECOGNIZE_SHARE:
            return None
        params = cls._build_recognize_query_params(
            meta=meta,
            mtype=mtype,
            keyword_meta=keyword_meta,
        )
        if not params:
            return None
        response = await cls.async_recognize_query(params)
        return cls._parse_recognize_response(response, params.get("keyword"))

    @classmethod
    def report_recognize_share(
            cls,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> bool:
        """
        上报共享识别结果。
        """
        if not settings.MEDIA_RECOGNIZE_SHARE:
            return False
        payload = cls._build_recognize_report_payload(
            meta=meta,
            mediainfo=mediainfo,
            keyword_meta=keyword_meta,
        )
        if not payload:
            return False
        response = cls.recognize_report(payload)
        return cls._parse_recognize_report_response(response)

    @classmethod
    async def async_report_recognize_share(
            cls,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> bool:
        """
        异步上报共享识别结果。
        """
        if not settings.MEDIA_RECOGNIZE_SHARE:
            return False
        payload = cls._build_recognize_report_payload(
            meta=meta,
            mediainfo=mediainfo,
            keyword_meta=keyword_meta,
        )
        if not payload:
            return False
        response = await cls.async_recognize_report(payload)
        return cls._parse_recognize_report_response(response)

    @classmethod
    def to_recognize_params(cls, item: Optional[dict]) -> Optional[dict]:
        """
        将服务端返回的共享识别结果转成本地识别参数。
        """
        if not isinstance(item, dict):
            return None

        media_type = cls._normalize_media_type(item.get("type"))
        mtype = MediaType.from_agent(media_type) if media_type else None
        tmdbid = item.get("tmdbid")
        doubanid = item.get("doubanid")
        bangumiid = item.get("bangumiid")
        anilistid = item.get("anilistid")
        media_source = item.get("media_source")
        media_id = item.get("media_id")
        if not any([tmdbid, doubanid, bangumiid, anilistid, media_id]):
            return None

        return {
            "mtype": mtype,
            "tmdbid": tmdbid,
            "doubanid": doubanid,
            "bangumiid": bangumiid,
            "anilistid": anilistid,
            "source": media_source,
            "mediaid": media_id,
            "season": item.get("season"),
        }

    @classmethod
    def sanitize_plugin_repo_url(cls, repo_url: Optional[str]) -> Optional[str]:
        """
        统计上报前脱敏插件仓库地址。
        """
        if not repo_url:
            return repo_url
        if not repo_url.startswith(cls._LOCAL_REPO_PREFIX):
            return repo_url

        plugin_id = cls._parse_local_repo_plugin_id(repo_url)
        if not plugin_id:
            return cls._LOCAL_REPO_PREFIX.rstrip("/")

        return cls._make_local_repo_url(
            plugin_id=plugin_id,
            package_version=cls._parse_local_repo_package_version(repo_url),
        )

    @classmethod
    def _normalize_media_type(cls, media_type: Optional[object]) -> Optional[str]:
        """
        统一媒体类型，兼容枚举、中文值和 agent 风格字符串。
        """
        normalized = media_type_to_agent(media_type)
        if normalized in {"movie", "tv"}:
            return normalized
        if isinstance(media_type, str):
            if media_type == MediaType.MOVIE.value:
                return "movie"
            if media_type == MediaType.TV.value:
                return "tv"
        return None

    @staticmethod
    def _extract_keyword(meta: Optional[MetaBase]) -> Optional[str]:
        """
        提取识别关键字。
        """
        if not meta:
            return None
        keyword = meta.original_name or meta.name
        if keyword:
            keyword = str(keyword).strip()
        return keyword or None

    @classmethod
    def _extract_media_type(
            cls,
            meta: Optional[MetaBase] = None,
            mtype: Optional[MediaType] = None,
            mediainfo: Optional[MediaInfo] = None,
    ) -> Optional[str]:
        """
        提取媒体类型。
        """
        media_type = cls._normalize_media_type(mtype)
        if media_type:
            return media_type
        if mediainfo and mediainfo.type in {MediaType.MOVIE, MediaType.TV}:
            return mediainfo.type.to_agent()
        if meta and meta.type in {MediaType.MOVIE, MediaType.TV}:
            return meta.type.to_agent()
        if meta and (meta.begin_season is not None or meta.begin_episode is not None):
            return "tv"
        return None

    @classmethod
    def _extract_season(
            cls,
            media_type: Optional[str],
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[MediaInfo] = None,
    ) -> Optional[int]:
        """
        提取季信息，仅电视剧使用。
        """
        if media_type != "tv":
            return None
        season = meta.begin_season if meta else None
        if season is None and mediainfo:
            season = mediainfo.season
        try:
            return int(season) if season is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_year(
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[MediaInfo] = None,
    ) -> Optional[str]:
        """
        提取年份。
        """
        year = (meta.year if meta else None) or (mediainfo.year if mediainfo else None)
        if year is None:
            return None
        year_text = str(year).strip()
        return year_text or None

    @classmethod
    def _build_recognize_query_params(
            cls,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
    ) -> Optional[dict]:
        """
        组装共享识别查询参数。
        """
        keyword = cls._extract_keyword(keyword_meta or meta)
        if not keyword:
            return None

        media_type = cls._extract_media_type(meta=meta, mtype=mtype)
        params = {"keyword": keyword}
        if media_type:
            params["type"] = media_type
        if year := cls._extract_year(meta=meta):
            params["year"] = year
        season = cls._extract_season(media_type=media_type, meta=meta)
        if season is not None:
            params["season"] = season
        return params

    @classmethod
    def _build_recognize_report_payload(
            cls,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> Optional[dict]:
        """
        组装共享识别上报载荷。
        """
        if not meta or not mediainfo:
            return None

        keyword = cls._extract_keyword(keyword_meta or meta)
        media_type = cls._extract_media_type(meta=meta, mediainfo=mediainfo)
        if not keyword or not media_type:
            return None
        media_source, media_id = resolve_media_identity(media=mediainfo)
        if not media_id:
            return None

        return {
            "keyword": keyword,
            "type": media_type,
            "title": mediainfo.title or keyword,
            "year": cls._extract_year(meta=meta, mediainfo=mediainfo),
            "season": cls._extract_season(
                media_type=media_type,
                meta=meta,
                mediainfo=mediainfo,
            ),
            "tmdbid": mediainfo.tmdb_id,
            "doubanid": mediainfo.douban_id,
            "bangumiid": mediainfo.bangumi_id,
            "anilistid": mediainfo.anilist_id,
            "media_source": media_source,
            "media_id": media_id,
        }

    @classmethod
    def _parse_recognize_response(cls, response, keyword: Optional[str]) -> Optional[dict]:
        """
        解析共享识别查询响应。
        """
        if not response or response.status_code != 200:
            if response is not None:
                logger.warn(
                    f"查询共享媒体识别失败：status={response.status_code} "
                    f"message={cls._response_message(response)}"
                )
            return None
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as err:
            logger.warn(f"解析共享媒体识别响应失败：{err}")
            return None
        if payload.get("code") != 0:
            return None
        item = cls._parse_response_item(payload)
        if item:
            logger.info(f"共享媒体识别命中：{keyword} - {item}")
        return item

    @classmethod
    def _parse_recognize_report_response(cls, response) -> bool:
        """
        解析共享识别上报响应。
        """
        if not response or response.status_code != 200:
            if response is not None:
                logger.warn(
                    f"上报共享媒体识别失败：status={response.status_code} "
                    f"message={cls._response_message(response)}"
                )
            return False
        try:
            result = response.json()
        except (json.JSONDecodeError, ValueError) as err:
            logger.warn(f"解析共享媒体识别上报响应失败：{err}")
            return False
        return result.get("code") == 0

    @staticmethod
    def _parse_response_item(data: Optional[dict]) -> Optional[dict]:
        """
        解析服务端返回的共享识别数据。
        """
        if not isinstance(data, dict):
            return None
        item = (data.get("data") or {}).get("item")
        if not isinstance(item, dict):
            return None
        return item

    @staticmethod
    def _response_message(response) -> str:
        """
        获取响应消息，兼容非 JSON 响应。
        """
        try:
            payload = response.json()
            return str(payload.get("message") or "")
        except (json.JSONDecodeError, ValueError, AttributeError):
            return ""

    @classmethod
    def _build_plugin_report_payload(
            cls,
            items: Optional[List[Tuple[str, Optional[str]]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        构建批量插件安装统计载荷。
        """
        if items:
            return [
                {
                    "plugin_id": plugin_id,
                    "repo_url": cls.sanitize_plugin_repo_url(repo_url),
                }
                for plugin_id, repo_url in items
                if plugin_id
            ]

        plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins)
        if not plugins:
            return []
        return [{"plugin_id": plugin, "repo_url": None} for plugin in plugins]

    @classmethod
    def _parse_local_repo_plugin_id(cls, repo_url: str) -> Optional[str]:
        """
        从本地插件来源标识中解析插件 ID。
        """
        try:
            parts = urlsplit(repo_url)
            plugin_id = parts.netloc or parts.path.strip("/")
        except Exception:
            plugin_id = repo_url[len(cls._LOCAL_REPO_PREFIX):].split("?", 1)[0].strip("/")
        return plugin_id or None

    @staticmethod
    def _parse_local_repo_package_version(repo_url: str) -> Optional[str]:
        """
        从本地插件来源标识中解析 package 版本。
        """
        try:
            values = parse_qs(urlsplit(repo_url).query).get("version")
            if not values:
                return None
            return values[0]
        except Exception:
            return None

    @classmethod
    def _make_local_repo_url(
            cls,
            plugin_id: str,
            package_version: Optional[str] = None,
    ) -> str:
        """
        生成脱敏后的本地插件来源标识。
        """
        repo_url = f"{cls._LOCAL_REPO_PREFIX}{quote(plugin_id, safe='')}"
        if package_version:
            repo_url = f"{repo_url}?version={quote(package_version, safe='')}"
        return repo_url

    @classmethod
    def _clear_subscribe_share_cache(cls) -> None:
        """
        清理订阅共享相关缓存。
        """
        cls.get_subscribe_shares.cache_clear()
        cls.async_get_subscribe_shares.cache_clear()
        cls.get_subscribe_statistic.cache_clear()
        cls.async_get_subscribe_statistic.cache_clear()
        cls.get_subscribe_share_statistics.cache_clear()
        cls.async_get_subscribe_share_statistics.cache_clear()

    @classmethod
    def _clear_workflow_share_cache(cls) -> None:
        """
        清理工作流共享相关缓存。
        """
        cls.get_workflow_shares.cache_clear()
        cls.async_get_workflow_shares.cache_clear()

    @classmethod
    def _has_header(cls, headers: dict, name: str) -> bool:
        """
        按 HTTP 头大小写不敏感规则判断请求头是否存在。
        """
        header_name = name.lower()
        return any(str(key).lower() == header_name for key in headers)

    @staticmethod
    def _server_url(path: str) -> str:
        """
        根据服务端基础地址和路径生成完整 URL。
        """
        return f"{settings.MP_SERVER_HOST.rstrip('/')}{path}"

    @classmethod
    def _get(
            cls,
            url: str,
            params: Optional[dict] = None,
            timeout: int = 10,
            include_user_uid: bool = True,
    ):
        """
        发送服务端 GET 请求，默认携带安装用户 ID。
        """
        return RequestUtils(
            proxies=settings.PROXY,
            timeout=timeout,
            headers=cls.build_headers(url) if include_user_uid else {},
        ).get_res(url, params=params)

    @classmethod
    async def _async_get(
            cls,
            url: str,
            params: Optional[dict] = None,
            timeout: int = 10,
            include_user_uid: bool = True,
    ):
        """
        异步发送服务端 GET 请求，默认携带安装用户 ID。
        """
        return await AsyncRequestUtils(
            proxies=settings.PROXY,
            timeout=timeout,
            headers=cls.build_headers(url) if include_user_uid else {},
        ).get_res(url, params=params)

    @classmethod
    def _post_json(cls, url: str, payload: Dict[str, Any], timeout: int = 10):
        """
        发送携带安装用户 ID 的服务端 JSON POST 请求。
        """
        return RequestUtils(
            proxies=settings.PROXY,
            timeout=timeout,
            headers=cls.build_headers(url, content_type="application/json"),
        ).post(url, json=payload)

    @classmethod
    async def _async_post_json(cls, url: str, payload: Dict[str, Any], timeout: int = 10):
        """
        异步发送携带安装用户 ID 的服务端 JSON POST 请求。
        """
        return await AsyncRequestUtils(
            proxies=settings.PROXY,
            timeout=timeout,
            headers=cls.build_headers(url, content_type="application/json"),
        ).post(url, json=payload)

    @classmethod
    def _delete(cls, url: str, params: Optional[dict] = None, timeout: int = 10):
        """
        发送携带安装用户 ID 的服务端 DELETE 请求。
        """
        return RequestUtils(
            proxies=settings.PROXY,
            timeout=timeout,
            headers=cls.build_headers(url),
        ).delete_res(url, params=params)

    @classmethod
    async def _async_delete(cls, url: str, params: Optional[dict] = None, timeout: int = 10):
        """
        异步发送携带安装用户 ID 的服务端 DELETE 请求。
        """
        return await AsyncRequestUtils(
            proxies=settings.PROXY,
            timeout=timeout,
            headers=cls.build_headers(url),
        ).delete_res(url, params=params)
