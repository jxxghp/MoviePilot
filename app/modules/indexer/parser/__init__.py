# -*- coding: utf-8 -*-
"""
定义站点资料解析框架及公共流程，统一读取身份、流量、做种和未读消息。
"""

import json
import re
from abc import ABCMeta, abstractmethod
from enum import Enum
from typing import ClassVar, Literal, Optional, TypeAlias, Union
from urllib.parse import urljoin, urlsplit

from app.adapters.network.cloudflare import under_challenge
from app.adapters.network.http import RequestUtils
from app.domain.site import SiteUtils
from app.foundation import size as size_tools
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

# API 模式沿用空字典作为首页占位；实际响应正文仍是文本。
SitePage: TypeAlias = Union[str, dict[str, object]]
MessageContent: TypeAlias = tuple[Optional[str], Optional[str], Optional[str]]


class SiteSchema(Enum):
    """
    站点用户数据解析框架类型。
    """

    # 基于 DiscuzX 论坛页面的站点。
    DiscuzX = "DiscuzX"
    # 基于 Gazelle 框架的站点。
    Gazelle = "Gazelle"
    # IPTorrents 站点。
    Ipt = "IPTorrents"
    # 通用 NexusPHP 站点。
    NexusPhp = "NexusPhp"
    # 采用 NexusProject 页面结构的站点。
    NexusProject = "NexusProject"
    # 采用 NexusRabbit JSON 接口的站点。
    NexusRabbit = "NexusRabbit"
    # 憨憨的 NexusPHP 定制页面。
    NexusHhanclub = "NexusHhanclub"
    # Audiences 的 NexusPHP 定制页面与消息布局。
    NexusAudiences = "NexusAudiences"
    # 采用 SmallHorse 框架的站点。
    SmallHorse = "Small Horse"
    # 基于 Unit3D 框架的站点。
    Unit3d = "Unit3d"
    # TorrentLeech 站点。
    TorrentLeech = "TorrentLeech"
    # FileList 站点。
    FileList = "FileList"
    # 采用 TNode 当前会话接口的站点。
    TNode = "TNode"
    # M-Team 的 mTorrent API。
    MTorrent = "MTorrent"
    # YemaPT 开放 API。
    Yema = "Yema"
    # HDDolby API 与兼容消息页面。
    HDDolby = "HDDolby"
    # 知行站点的页面结构。
    Zhixing = "Zhixing"
    # 极速之星站点的页面结构。
    Bitpt = "Bitpt"
    # Rousi.pro 的 PeerGo 兼容 API。
    RousiPro = "RousiPro"
    # SunnyPT 的 MoviePilot API。
    SunnyPT = "SunnyPT"


class SiteParserBase(metaclass=ABCMeta):
    """
    站点用户数据解析器基类。
    """

    # 站点模版
    schema: ClassVar[Optional[SiteSchema]] = None
    # 请求模式 cookie/apikey
    request_mode = "cookie"
    # 详情页默认依赖用户 ID；当前会话接口可在配置页面时关闭此前置校验。
    _user_detail_requires_userid = True

    def __init__(self, site_name: Optional[str],
                 url: str,
                 site_cookie: Optional[str],
                 apikey: Optional[str],
                 token: Optional[str],
                 request_utils: Optional[RequestUtils] = None,
                 ua: Optional[str] = None,
                 emulate: bool = False,
                 proxy: Optional[bool] = None,
                 api_url: Optional[str] = None) -> None:
        """
        初始化站点用户数据解析器。

        :param site_name: 站点名称；未配置时为 None，仅用于日志标识
        :param url: 站点前端地址
        :param site_cookie: 站点 Cookie
        :param apikey: 站点 API Key
        :param token: 站点 Token
        :param request_utils: 可复用的统一 HTTP 请求客户端
        :param ua: 请求 User-Agent
        :param emulate: 是否使用浏览器仿真
        :param proxy: 是否使用系统代理
        :param api_url: 站点独立 API 基础地址
        """
        super().__init__()

        # 站点信息
        self.apikey = apikey
        self.token = token
        self._site_name = site_name
        self._site_url = url
        __split_url = urlsplit(url)
        self._site_domain = __split_url.netloc
        self._base_url = f"{__split_url.scheme}://{__split_url.netloc}"
        self._site_cookie = site_cookie
        self._api_url = api_url
        self._request_utils = request_utils or RequestUtils(use_session=True)
        self._ua = ua
        self._emulate = emulate
        self._proxy = proxy
        self._index_html: SitePage = ""
        # 用户信息
        self.username: Union[str, int, None] = None
        self.userid: Union[str, int, None] = None
        self.user_level: Union[str, int, None] = None
        self.join_at: Optional[str] = None
        self.bonus = 0.0

        # 上传/下载量按整数字节计，分享率为浮点数
        self.upload = 0
        self.download = 0
        self.ratio = 0.0

        # 做种信息
        self.seeding = 0
        self.leeching = 0
        self.seeding_size = 0
        self.leeching_size = 0
        self.uploaded = 0
        self.completed = 0
        self.incomplete = 0
        self.uploaded_size = 0
        self.completed_size = 0
        self.incomplete_size = 0
        # 做种人数, 种子大小
        self.seeding_info: list[list[int]] = []

        # 未读消息
        self.message_unread = 0
        self.message_unread_contents: list[Union[MessageContent, tuple[Optional[str], Optional[str], Optional[str], Optional[str]]]] = []
        self.message_read_force = False

        # 全局附加请求头
        self._addition_headers: Optional[dict[str, Optional[str]]] = None

        # 用户基础信息页面
        self._user_basic_page: Optional[str] = None
        # 用户基础信息参数
        self._user_basic_params: Optional[dict[str, object]] = None
        # 用户基础信息请求方法
        self._user_basic_method: Optional[str] = None
        # 用户基础信息请求头
        self._user_basic_headers: Optional[dict[str, Optional[str]]] = None

        # 用户详情信息页面
        self._user_detail_page: Optional[str] = "userdetails.php?id="
        # 用户详情信息参数
        self._user_detail_params: Optional[dict[str, object]] = None
        # 用户详情信息请求头
        self._user_detail_headers: Optional[dict[str, Optional[str]]] = None

        # 用户流量信息页面
        self._user_traffic_page: Optional[str] = "index.php"
        # 用户流量信息参数
        self._user_traffic_params: Optional[dict[str, object]] = None
        # 用户流量信息请求头
        self._user_traffic_headers: Optional[dict[str, Optional[str]]] = None

        # 用户未读消息页面
        self._user_mail_unread_page: Optional[str] = "messages.php?action=viewmailbox&box=1&unread=yes"
        # 系统未读消息页面
        self._sys_mail_unread_page: Optional[str] = "messages.php?action=viewmailbox&box=-2&unread=yes"
        # 未读消息数参数
        self._mail_unread_params: Optional[dict[str, object]] = None
        # 未读消息数请求头
        self._mail_unread_headers: Optional[dict[str, Optional[str]]] = None
        # 未读消息内容参数
        self._mail_content_params: Optional[dict[str, object]] = None
        # 未读消息内容请求头
        self._mail_content_headers: Optional[dict[str, Optional[str]]] = None

        # 用户做种信息页面
        self._torrent_seeding_page: Optional[str] = "getusertorrentlistajax.php?userid="
        # 用户做种信息参数
        self._torrent_seeding_params: Optional[dict[str, object]] = None
        # 用户做种信息请求头
        self._torrent_seeding_headers: Optional[dict[str, Optional[str]]] = None

        # 错误信息
        self.err_msg: Optional[str] = None

    def site_schema(self) -> Optional[SiteSchema]:
        """
        站点解析模型。

        :return: 站点解析模型
        """
        return self.schema

    @staticmethod
    def num_filesize(text: Union[str, int, float]) -> int:
        """
        将站点页面中的文件大小文本转换为字节。
        """
        return size_tools.parse_size(text)

    def _has_valid_userid(self) -> bool:
        """
        判断用户 ID 是否存在且有效，避免空 ID 触发站点的无效详情/做种请求。
        """
        if self.userid is None:
            return False
        if isinstance(self.userid, str):
            user_id = self.userid.strip()
            return bool(user_id) and user_id != "0"
        return bool(self.userid) and self.userid != 0

    def parse(self) -> None:
        """
        依次读取站点身份、资料、消息、流量和做种信息，并在结束时关闭请求会话。

        API 模式缺少凭据或 Cookie 模式未登录时提前结束，已解析数据保留在当前实例。
        """
        try:
            # Cookie模式时，获取站点首页html
            if self.request_mode == "apikey":
                if not self.apikey and not self.token:
                    logger.warn(f"{self._site_name} 未设置cookie 或 apikey/token，跳过后续操作")
                    return
                self._index_html = {}
            else:
                # 检查是否已经登录
                self._index_html = self._get_page_content(url=self._site_url)
                if not self._parse_logged_in(self._index_html):
                    return
            # 解析站点页面
            self._parse_site_page(self._index_html)
            # 解析用户基础信息
            if self._user_basic_page:
                self._parse_user_base_info(
                    self._get_page_content(
                        url=urljoin(self._base_url, self._user_basic_page),
                        params=self._user_basic_params,
                        headers=self._user_basic_headers,
                        **({"method": self._user_basic_method} if self._user_basic_method else {}),
                    )
                )
            else:
                self._parse_user_base_info(self._index_html)
            # 解析用户详细信息
            if self._user_detail_page:
                if self._user_detail_requires_userid and not self._has_valid_userid():
                    logger.warn(f"{self._site_name} 未解析到有效用户 ID，跳过用户详情页请求")
                else:
                    self._parse_user_detail_info(
                        self._get_page_content(
                            url=urljoin(self._base_url, self._user_detail_page),
                            params=self._user_detail_params,
                            headers=self._user_detail_headers
                        )
                    )
            # 解析用户未读消息
            if get_runtime_setting('SITE_MESSAGE'):
                self._pase_unread_msgs()
            # 解析用户上传、下载、分享率等信息
            if self._user_traffic_page:
                self._parse_user_traffic_info(
                    self._get_page_content(
                        url=urljoin(self._base_url, self._user_traffic_page),
                        params=self._user_traffic_params,
                        headers=self._user_traffic_headers
                    )
                )
            # 解析用户做种信息
            self._parse_seeding_pages()
        finally:
            # 关闭连接
            self.close()

    def _pase_unread_msgs(self) -> None:
        """
        解析所有未读消息标题和内容。
        """
        unread_msg_links = []
        if self.message_unread > 0 or self.message_read_force:
            links = {self._user_mail_unread_page, self._sys_mail_unread_page}
            for link in links:
                if not link:
                    continue
                msg_links: list[str] = []
                next_page = self._parse_message_unread_links(
                    self._get_page_content(
                        url=urljoin(self._base_url, link),
                        params=self._mail_unread_params,
                        headers=self._mail_unread_headers
                    ),
                    msg_links)
                while next_page:
                    next_page = self._parse_message_unread_links(
                        self._get_page_content(
                            url=urljoin(self._base_url, next_page),
                            params=self._mail_unread_params,
                            headers=self._mail_unread_headers
                        ),
                        msg_links
                    )
                unread_msg_links.extend(msg_links)
        # 重新更新未读消息数（99999表示有消息但数量未知）
        if unread_msg_links and not self.message_unread:
            self.message_unread = len(unread_msg_links)
        # 解析未读消息内容
        for msg_link in unread_msg_links:
            logger.debug(f"{self._site_name} 信息链接 {msg_link}")
            message = self._parse_message_content(
                self._get_page_content(
                    urljoin(self._base_url, msg_link),
                    params=self._mail_content_params,
                    headers=self._mail_content_headers
                )
            )
            if message is None:
                raise TypeError("cannot unpack non-iterable NoneType object")
            head, date, content = message
            logger.debug(f"{self._site_name} 标题 {head} 时间 {date} 内容 {content}")
            self.message_unread_contents.append((head, date, content))

    def _parse_seeding_pages(self) -> None:
        """
        解析做种页面。
        """
        if self._torrent_seeding_page:
            if not self._has_valid_userid():
                logger.warn(f"{self._site_name} 未解析到有效用户 ID，跳过做种信息请求")
                return
            # 第一页
            next_page = self._parse_user_torrent_seeding_info(
                self._get_page_content(
                    url=urljoin(self._base_url, self._torrent_seeding_page),
                    params=self._torrent_seeding_params,
                    headers=self._torrent_seeding_headers
                )
            )

            # 其他页处理
            while next_page is not None and next_page is not False:
                next_page = self._parse_user_torrent_seeding_info(
                    self._get_page_content(
                        url=urljoin(urljoin(self._base_url, self._torrent_seeding_page), next_page),
                        params=self._torrent_seeding_params,
                        headers=self._torrent_seeding_headers
                    ),
                    multi_page=True)

    @staticmethod
    def _prepare_html_text(html_text: SitePage) -> str:
        """
        处理掉HTML中的干扰部分。
        """
        if isinstance(html_text, dict):
            raise TypeError("expected string or bytes-like object, got 'dict'")
        return re.sub(r"#\d+", "", re.sub(r"\d+px", "", html_text))

    @abstractmethod
    def _parse_message_unread_links(self, html_text: str, msg_links: list[str]) -> Optional[str]:
        """
        从消息列表提取未读链接并追加到调用方容器。

        :param html_text: 当前消息列表的响应文本
        :param msg_links: 累积未读消息地址的可变列表，解析结果追加到此列表
        :return: 下一页地址；没有后续页面时返回 None
        """
        pass

    def _get_page_content(
        self,
        url: str,
        params: Optional[dict[str, object]] = None,
        headers: Optional[dict[str, Optional[str]]] = None,
        method: Optional[str] = None,
    ) -> str:
        """
        按当前认证模式请求页面，并返回可供站点解析器处理的文本。

        :param url: 完整请求地址
        :param params: 请求体参数；非空时使用 POST，内容类型决定发送表单还是 JSON
        :param headers: 本次请求的额外请求头
        :param method: 传入 post 可在参数为空时强制使用 POST
        :return: 已解码文本；请求失败、不支持的状态码、JSON 无效或遭遇 Cloudflare 验证时为空字符串
        """
        req_headers: Optional[dict[str, Optional[str]]] = None
        proxies = get_runtime_setting('PROXY') if self._proxy else None
        if self._ua or headers or self._addition_headers:

            if self.request_mode == "apikey":
                req_headers = {}
            else:
                req_headers = {
                    "User-Agent": f"{self._ua}"
                }

            if headers:
                req_headers.update(headers)
            else:
                req_headers.update({
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                })

            if self._addition_headers:
                req_headers.update(self._addition_headers)

        if self.request_mode == "apikey":
            # 使用apikey请求，通过请求头传递
            cookie = None
            request_utils = RequestUtils()
        else:
            # 使用cookie请求
            cookie = self._site_cookie
            request_utils = self._request_utils

        if method == "post" or params:
            if (req_headers or {}).get("Content-Type") == "application/json":
                res = request_utils.post_res(
                    url=url,
                    json=params or {},
                    cookies=cookie,
                    timeout=60,
                    proxies=proxies,
                    headers=req_headers,
                )
            else:
                res = request_utils.post_res(
                    url=url,
                    data=params or {},
                    cookies=cookie,
                    timeout=60,
                    proxies=proxies,
                    headers=req_headers,
                )
        else:
            res = request_utils.get_res(
                url=url,
                cookies=cookie,
                timeout=60,
                proxies=proxies,
                headers=req_headers,
            )
        if res is None:
            logger.warning(f"{self._site_name} 用户数据请求未收到响应")
        elif res.status_code != 200:
            # 不输出带用户标识或令牌的 URL，保留状态码用于诊断入口失效和访问限制。
            logger.warning(f"{self._site_name} 用户数据请求返回 HTTP {res.status_code}")
        if res is not None and res.status_code in (200, 500, 403):
            if req_headers and "application/json" in str(req_headers.get("Accept")):
                try:
                    return json.dumps(res.json())
                except (json.JSONDecodeError, ValueError) as e:
                    logger.error(f"{self._site_name} API响应JSON解析失败: {e}")
                    return ""
            else:
                # 遭遇 Cloudflare 验证时停止解析，并提示更新 Cookie 和 User-Agent。
                if under_challenge(res.text):
                    logger.warn(
                        f"{self._site_name} 检测到Cloudflare，请更新Cookie和UA")
                    return ""
                html_content: str = RequestUtils.get_decoded_html_content(res,
                                                             get_runtime_setting('ENCODING_DETECTION_PERFORMANCE_MODE'),
                                                             get_runtime_setting('ENCODING_DETECTION_MIN_CONFIDENCE'))
                return html_content

        return ""

    @abstractmethod
    def _parse_site_page(self, html_text: SitePage) -> None:
        """
        解析站点相关信息页面。

        :param html_text: 当前页面或 API 响应文本
        """
        pass

    @abstractmethod
    def _parse_user_base_info(self, html_text: SitePage) -> None:
        """
        解析用户基础信息。

        :param html_text: 当前页面或 API 响应文本
        """
        pass

    def _parse_logged_in(self, html_text: str) -> bool:
        """
        解析用户是否已经登陆。

        :param html_text: 当前页面或 API 响应文本
        :return: 已登录时返回 True，否则返回 False
        """
        logged_in = SiteUtils.is_logged_in(html_text)
        if not logged_in:
            self.err_msg = "未检测到已登陆，请检查cookies是否过期"
            logger.warn(f"{self._site_name} 未登录，跳过后续操作")

        return logged_in

    @abstractmethod
    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        解析用户的上传，下载，分享率等信息。

        :param html_text: 当前页面或 API 响应文本
        """
        pass

    @abstractmethod
    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False) -> Union[str, Literal[False], None]:
        """
        解析当前做种页面并更新实例上的数量、体积和做种人数分布。

        :param html_text: 当前做种列表的响应文本
        :param multi_page: 是否为后续页面，供实现区分初始化与累积统计
        :return: 下一页地址；返回 None 或 False 时结束翻页
        """
        pass

    @abstractmethod
    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        解析用户的详细信息。
        加入时间/等级/魔力值等

        :param html_text: 当前页面或 API 响应文本
        """
        pass

    @abstractmethod
    def _parse_message_content(self, html_text: str) -> Optional[MessageContent]:
        """
        解析单条站内消息的标题、时间和正文。

        :param html_text: 消息详情页面的响应文本
        :return: 标题、时间、正文组成的三元组；缺失字段可为 None；旧实现返回 None 时，通用消息流程会报解包错误
        """
        pass

    def close(self) -> None:
        """
        关闭会话。
        """
        self._request_utils.close()

    def clear(self) -> None:
        """
        清空首页文本、做种明细和未读消息正文缓存，不重置已解析的用户统计。
        """
        self._index_html = ""
        self.seeding_info.clear()
        self.message_unread_contents.clear()

    def to_dict(self) -> dict[str, object]:
        """
        导出当前实例的公开数据属性，供用户数据聚合调用方使用。

        :return: 非私有且不可调用的属性字典，站点框架枚举转为对应值
        """
        attributes = [
            attr for attr in dir(self)
            if not callable(getattr(self, attr)) and not attr.startswith("_")
        ]
        return {
            attr: getattr(self, attr).value
            if isinstance(getattr(self, attr), SiteSchema)
            else getattr(self, attr) for attr in attributes
        }
