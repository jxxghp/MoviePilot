"""
通过 FluxTorrent 兼容层搜索种子并转换为通用索引结果。
"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.application.site.throttle import async_wait_for_site_request, wait_for_site_request
from app.foundation import temporal as time_tools
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MediaType

# FluxTorrent 兼容层的只读接口前缀。
_COMPAT_PREFIX = "/api/v1/compat/nexusphp"
# 网关偶发 503 时，只读请求最多重试两次，分别退避三秒和六秒。
_GATEWAY_RETRY_SECONDS = 3
_GATEWAY_RETRIES = 2

# 兼容层促销档位到下载、上传量系数的映射，与站点计费口径一致：
# free 下载 0 / x2 上传 2 / x2free 0+2 / half 下载 0.5 / x2half 0.5+2 / p30 下载 0.3。
_PROMOTION_FACTORS: Dict[str, Tuple[float, float]] = {
    "free": (0.0, 1.0),
    "x2free": (0.0, 2.0),
    "half": (0.5, 1.0),
    "x2half": (0.5, 2.0),
    "x2": (1.0, 2.0),
    "p30": (0.3, 1.0),
}

# Newznab 标准分类号到 MoviePilot 媒体类型的映射。站点按标准号登记分类，
# 因此同一套映射对所有 FluxTorrent 站点都成立；软件与游戏没有对应媒体类型，
# 有声书与电子书归入系列。
_NEWZNAB_MEDIA_TYPES: Dict[int, MediaType] = {
    2000: MediaType.MOVIE,
    2010: MediaType.MOVIE,
    2030: MediaType.MOVIE,
    2040: MediaType.MOVIE,
    2045: MediaType.MOVIE,
    3000: MediaType.MUSIC,
    3030: MediaType.MUSIC,
    3040: MediaType.MUSIC,
    5000: MediaType.TV,
    5030: MediaType.TV,
    5040: MediaType.TV,
    5060: MediaType.TV,
    5070: MediaType.TV,
    5080: MediaType.TV,
    5090: MediaType.TV,
    7000: MediaType.COLLECTION,
    7010: MediaType.COLLECTION,
    7020: MediaType.COLLECTION,
    7030: MediaType.COLLECTION,
}

# 站点分类名到 MoviePilot 媒体类型的兜底映射，标准号缺失时使用。
_CATEGORY_NAME_MEDIA_TYPES: Dict[str, MediaType] = {
    "电影": MediaType.MOVIE,
    "电视剧": MediaType.TV,
    "短剧": MediaType.TV,
    "综艺": MediaType.TV,
    "动漫": MediaType.TV,
    "纪录片": MediaType.TV,
    # 教育影音不是剧集：映射成电视剧会把图书与课程资源标成剧集，
    # 未识别的分类统一回落到站内分类名（见 _media_type）。
    "音乐": MediaType.MUSIC,
    "有声书": MediaType.COLLECTION,
    "电子书": MediaType.COLLECTION,
}


def site_base_url(site: Dict[str, Any]) -> str:
    """
    返回站点根地址。

    :param site: 站点索引配置
    :return: 去掉末尾斜杠的站点根地址；没有可用地址时为空字符串
    """
    raw = str(site.get("url") or site.get("domain") or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw.lstrip('/')}"
    return raw.rstrip("/")


def site_api_token(site: Dict[str, Any]) -> str:
    """
    返回站点配置的开放 API 令牌。

    :param site: 站点索引配置
    :return: 开放 API 令牌；未配置时为空字符串
    """
    return str(site.get("apikey") or site.get("token") or "").strip()


def site_request_limits(site: Dict[str, Any]) -> Dict[str, Any]:
    """
    构造兼容层请求共用的无凭据流控配置。

    保留用户设置的请求间隔和次数窗口，使连接检测、搜索与用户资料请求
    共用同一站点额度。

    :param site: 站点索引配置或已保存的站点流控快照
    :return: 只包含站点身份和流控字段的独立配置
    """
    limits = {key: site.get(key) for key in
              ("id", "limit_seconds", "limit_interval", "limit_count")}
    limits["domain"] = site_base_url(site)
    return limits


def get_api_response(
    request: RequestUtils,
    url: str,
    limits: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    在共享流控下请求只读 API，网关 503 最多退避重试两次。

    :param request: 已配置认证头和网络选项的同步传输客户端
    :param url: 只读 API 的完整地址
    :param limits: 不含凭据的站点流控配置
    :param params: 查询参数，未提供时不添加参数
    :return: 最后一次响应或 None，由调用方解析并关闭
    """
    response = None
    for attempt in range(_GATEWAY_RETRIES + 1):
        wait_for_site_request(limits)
        response = request.get_res(url=url, params=params, allow_redirects=False)
        if response is None or response.status_code != 503 or attempt == _GATEWAY_RETRIES:
            break
        response.close()
        time.sleep(_GATEWAY_RETRY_SECONDS * (attempt + 1))
    return response


async def async_get_api_response(
    request: AsyncRequestUtils,
    url: str,
    limits: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    异步申请同一站点额度，网关 503 使用可取消的延迟最多重试两次。

    :param request: 已配置认证头和网络选项的异步传输客户端
    :param url: 只读 API 的完整地址
    :param limits: 不含凭据的站点流控配置
    :param params: 查询参数，未提供时不添加参数
    :return: 最后一次响应或 None，由调用方解析并关闭
    """
    response = None
    for attempt in range(_GATEWAY_RETRIES + 1):
        await async_wait_for_site_request(limits)
        response = await request.get_res(url=url, params=params, allow_redirects=False)
        if response is None or response.status_code != 503 or attempt == _GATEWAY_RETRIES:
            break
        await response.aclose()
        await asyncio.sleep(_GATEWAY_RETRY_SECONDS * (attempt + 1))
    return response


class FluxTorrentSpider:
    """
    FluxTorrent 兼容层索引器。

    FluxTorrent 是通用 PT 建站系统，其兼容层按 NexusPHP 字段口径暴露只读 JSON，
    因此本索引器不解析站点页面，只调用兼容层接口。
    """

    # 兼容层单页容量上限为 50。
    _size = 50
    # 每次实际 API 请求自行申请额度，模块入口无需重复等待。
    manages_request_throttle = True

    def __init__(self, indexer: dict[str, Any]):
        """
        初始化 FluxTorrent 兼容层索引器。

        :param indexer: 合并站点认证信息后的索引配置
        """
        indexer = indexer or {}
        self._name = indexer.get("name") or "FluxTorrent"
        self._site_url = site_base_url(indexer)
        self._api_token = site_api_token(indexer)
        self._proxy = get_runtime_setting("PROXY") if indexer.get("proxy") else None
        self._user_agent = self.get_user_agent(indexer.get("ua"))
        self._timeout = indexer.get("timeout") or 30
        self._rate_site = site_request_limits(indexer)

    @staticmethod
    def get_user_agent(ua: Optional[str]) -> str:
        """
        为 API 请求和种子下载选择站点自定义或默认浏览器 User-Agent。

        好学网关会拒绝默认 MoviePilot 标识，返回 Nginx 503。资源合并填入的
        宿主默认标识同样转换为浏览器标识，站点显式设置的其他值保持优先。

        :param ua: 站点配置或资源合并后的 User-Agent
        :return: 本次站点请求使用的 User-Agent
        """
        if not ua or ua == get_runtime_setting("USER_AGENT"):
            return str(get_runtime_setting("NORMAL_USER_AGENT"))
        return ua

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取搜索接口单页容量。

        :param keyword: 搜索关键字，兼容层不按关键字改变分页容量
        :return: 搜索接口单页容量
        """
        return cls._size

    @property
    def _base_url(self) -> str:
        """
        返回兼容层接口基址。

        :return: 站点根路径拼接兼容层前缀后的地址
        """
        return f"{self._site_url}{_COMPAT_PREFIX}"

    def _request_headers(self) -> Dict[str, Any]:
        """
        构造兼容层请求头。

        :return: 带开放 API 令牌的请求头
        """
        return {
            "Authorization": f"Token {self._api_token}",
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }

    def _response_data(self, response: Any, operation: str) -> Any:
        """
        解析兼容层统一响应信封并返回业务数据。

        :param response: 请求返回的响应对象
        :param operation: 错误信息中的操作名称
        :return: 成功时返回响应 data，失败时返回 None
        """
        if response is None:
            logger.warning(f"{self._name} {operation}失败：站点无响应")
            return None
        status_code = getattr(response, "status_code", 0)
        try:
            body = response.json()
        except Exception:
            logger.warning(f"{self._name} {operation}失败：响应不是 JSON（状态码 {status_code}）")
            return None
        if not isinstance(body, dict):
            logger.warning(f"{self._name} {operation}失败：响应结构异常")
            return None
        code = body.get("code")
        if code != 0:
            logger.warning(f"{self._name} {operation}失败：接口返回 {code} {body.get('message') or ''}")
            return None
        return body.get("data")

    def _build_params(
        self,
        keyword: Optional[str],
        page: Optional[int],
    ) -> Dict[str, Any]:
        """
        构造兼容层搜索参数。

        兼容层的分类筛选只接受单个站内分类 ID，而 MoviePilot 的媒体类型与分类
        筛选都是站点自定义口径，因此这里只按关键字与分页检索，分类交由结果的
        分类字段表达，避免用错口径的分类 ID 把结果筛空。

        :param keyword: 搜索关键字
        :param page: MoviePilot 从 0 开始的页码
        :return: 兼容层查询参数
        """
        params: Dict[str, Any] = {
            "page": (page or 0) + 1,
            "pagesize": self._size,
        }
        if keyword:
            params["keyword"] = keyword
        return params

    def test_connection(self) -> Tuple[bool, str]:
        """
        用开放 API Token 验证兼容层用户接口。

        FluxTorrent 是前后端分离站点，首页 HTML 不含 NexusPHP 的登录标记，
        通用网页检测会把有效令牌误判成「Cookie已失效」。这里与搜索、用户资料
        解析共用同一枚令牌和同一套请求头，不做任何页面抓取。

        :return: 令牌是否可用及认证或连接失败原因
        """
        if not self._api_token:
            return False, "未配置开放 API Token（fxo_ 开头）"
        response = get_api_response(RequestUtils(
            headers=self._request_headers(),
            ua=self._user_agent,
            proxies=self._proxy or {},
            timeout=self._timeout,
        ),
            url=f"{self._base_url}/user.json",
            limits=self._rate_site,
        )
        try:
            if response is None:
                return False, "无法连接 FluxTorrent 兼容层接口"
            if response.status_code != 200:
                # 兼容层鉴权失败也是标准信封，把「未认证 / 权限不足」原样透出，
                # 避免站长再去排查早已不需要的 Cookie。
                try:
                    payload = response.json()
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict) and payload.get("message"):
                    return False, (
                        f"开放 API Token 无效：{payload['message']}"
                        f"（HTTP {response.status_code}）"
                    )
                return False, f"错误：{response.status_code} {response.reason}"
            try:
                payload = response.json()
            except (TypeError, ValueError):
                return False, "站点未返回 FluxTorrent 兼容层响应"
            if not isinstance(payload, dict):
                return False, "站点未返回 FluxTorrent 兼容层响应"
            if payload.get("code") == 0 and isinstance(payload.get("data"), dict):
                return True, "连接成功"
            return False, str(
                payload.get("message") or "开放 API Token 无效或已过期"
            )
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _media_type(item: Dict[str, Any]) -> str:
        """
        将兼容层种子分类映射为 MoviePilot 媒体类型。

        标准号与通用分类名都不认识时回落到站内分类名：教育类（小学/初中/高中/
        大学/教育影音）没有对应的 Newznab 媒体号，一律写「未知」既丢掉了站内
        已有的分类信息，也分不出图书、课程与影音资源。

        :param item: 兼容层返回的种子记录
        :return: MoviePilot 媒体类型或站内分类名，两者都缺失时为未知类型
        """
        newznab_id = item.get("category_newznab")
        media_type = None
        if isinstance(newznab_id, int):
            media_type = _NEWZNAB_MEDIA_TYPES.get(newznab_id)
        site_category = str(item.get("category_name") or "").strip()
        if media_type is None:
            media_type = _CATEGORY_NAME_MEDIA_TYPES.get(site_category)
        if media_type is not None:
            return media_type.value
        return site_category or MediaType.UNKNOWN.value

    @staticmethod
    def _volume_factors(promotion: Optional[str]) -> Tuple[float, float]:
        """
        将兼容层促销档位换算为下载、上传量系数。

        :param promotion: 兼容层返回的促销档位
        :return: 下载量系数与上传量系数
        """
        return _PROMOTION_FACTORS.get(str(promotion or "").strip(), (1.0, 1.0))

    def _build_download_url(self, torrent_id: object) -> str:
        """
        构造种子文件下载地址。

        兼容层下载端点接受开放 API 令牌作为查询参数，下载器取种子时不会携带
        请求头，因此令牌必须落在地址里。

        :param torrent_id: 兼容层返回的种子 ID
        :return: 兼容层下载接口地址
        """
        return f"{self._base_url}/download.php?id={torrent_id}&apikey={self._api_token}"

    def _parse_result(self, items: List[Any]) -> List[Dict[str, Any]]:
        """
        将兼容层种子数据转换为 MoviePilot 标准字段。

        :param items: 兼容层 data.items 数组
        :return: MoviePilot 标准种子字典列表
        """
        torrents: List[Dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            torrent_id = item.get("id")
            download_factor, upload_factor = self._volume_factors(item.get("promotion"))
            labels: List[str] = []
            promotion_name = str(item.get("promotion_name") or "").strip()
            if promotion_name:
                labels.append(promotion_name)
            if item.get("official") is True:
                labels.append("官方")
            torrents.append({
                "title": item.get("name"),
                "description": item.get("small_descr"),
                "enclosure": self._build_download_url(torrent_id),
                "pubdate": time_tools.format_timestamp(item.get("added")),
                "size": int(item.get("size") or 0),
                "seeders": int(item.get("seeders") or 0),
                "peers": int(item.get("leechers") or 0),
                "grabs": int(item.get("completed") or 0),
                "downloadvolumefactor": download_factor,
                "uploadvolumefactor": upload_factor,
                "page_url": f"{self._site_url}/torrent/{torrent_id}",
                "labels": labels,
                "category": self._media_type(item),
            })
        return torrents

    def _process_search_response(self, response: Any) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        处理兼容层种子搜索响应。

        :param response: 请求返回的响应对象
        :return: 是否失败及标准种子列表
        """
        data = self._response_data(response, "搜索")
        if not isinstance(data, dict):
            return True, []
        return False, self._parse_result(data.get("items") or [])

    def search(
        self,
        keyword: Optional[str],
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        同步搜索 FluxTorrent 站点种子。

        :param keyword: 搜索关键字
        :param page: MoviePilot 从 0 开始的页码
        :return: 是否失败及标准种子列表
        """
        if not self._api_token:
            logger.warning(f"{self._name} 未配置开放 API 令牌")
            return True, []
        params = self._build_params(keyword, page)
        response = get_api_response(RequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ), url=f"{self._base_url}/torrents.json", limits=self._rate_site, params=params)
        try:
            return self._process_search_response(response)
        finally:
            if response is not None:
                response.close()

    async def async_search(
        self,
        keyword: Optional[str],
        page: Optional[int] = 0,
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """
        异步搜索 FluxTorrent 站点种子。

        :param keyword: 搜索关键字
        :param page: MoviePilot 从 0 开始的页码
        :return: 是否失败及标准种子列表
        """
        if not self._api_token:
            logger.warning(f"{self._name} 未配置开放 API 令牌")
            return True, []
        params = self._build_params(keyword, page)
        response = await async_get_api_response(AsyncRequestUtils(
            headers=self._request_headers(),
            proxies=self._proxy,
            timeout=self._timeout,
        ), url=f"{self._base_url}/torrents.json", limits=self._rate_site, params=params)
        try:
            return self._process_search_response(response)
        finally:
            if response is not None:
                await response.aclose()
