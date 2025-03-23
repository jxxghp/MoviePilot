# -*- coding: utf-8 -*-
import json
import re
from abc import ABCMeta, abstractmethod
from enum import Enum
from typing import Optional
from urllib.parse import urljoin, urlsplit

from requests import Session

from app.core.config import settings
from app.helper.cloudflare import under_challenge
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils


# 站点框架
class SiteSchema(Enum):
    DiscuzX = "Discuz!"
    Gazelle = "Gazelle"
    Ipt = "IPTorrents"
    NexusPhp = "NexusPhp"
    NexusProject = "NexusProject"
    NexusRabbit = "NexusRabbit"
    NexusHhanclub = "NexusHhanclub"
    NexusAudiences = "NexusAudiences"
    SmallHorse = "Small Horse"
    Unit3d = "Unit3d"
    TorrentLeech = "TorrentLeech"
    FileList = "FileList"
    TNode = "TNode"
    MTorrent = "MTorrent"
    Yema = "Yema"
    HDDolby = "HDDolby"


class SiteParserBase(metaclass=ABCMeta):
    # 站点模版
    schema = None
    # 请求模式 cookie/apikey
    request_mode = "cookie"

    def __init__(self, site_name: str,
                 url: str,
                 site_cookie: str,
                 apikey: str,
                 token: str,
                 session: Session = None,
                 ua: Optional[str] = None,
                 emulate: bool = False,
                 proxy: bool = None):
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
        self._session = session if session else None
        self._ua = ua
        self._emulate = emulate
        self._proxy = proxy
        self._index_html = ""
        # 用户信息
        self.username = None
        self.userid = None
        self.user_level = None
        self.join_at = None
        self.bonus = 0.0

        # 流量信息
        self.upload = 0
        self.download = 0
        self.ratio = 0

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
        self.seeding_info = []

        # 未读消息
        self.message_unread = 0
        self.message_unread_contents = []
        self.message_read_force = False

        # 全局附加请求头
        self._addition_headers = None

        # 用户基础信息页面
        self._user_basic_page = None
        # 用户基础信息参数
        self._user_basic_params = None
        # 用户基础信息请求头
        self._user_basic_headers = None

        # 用户详情信息页面
        self._user_detail_page = "userdetails.php?id="
        # 用户详情信息参数
        self._user_detail_params = None
        # 用户详情信息请求头
        self._user_detail_headers = None

        # 用户流量信息页面
        self._user_traffic_page = "index.php"
        # 用户流量信息参数
        self._user_traffic_params = None
        # 用户流量信息请求头
        self._user_traffic_headers = None

        # 用户未读消息页面
        self._user_mail_unread_page = "messages.php?action=viewmailbox&box=1&unread=yes"
        # 系统未读消息页面
        self._sys_mail_unread_page = "messages.php?action=viewmailbox&box=-2&unread=yes"
        # 未读消息数参数
        self._mail_unread_params = None
        # 未读消息数请求头
        self._mail_unread_headers = None
        # 未读消息内容参数
        self._mail_content_params = None
        # 未读消息内容请求头
        self._mail_content_headers = None

        # 用户做种信息页面
        self._torrent_seeding_page = "getusertorrentlistajax.php?userid="
        # 用户做种信息参数
        self._torrent_seeding_params = None
        # 用户做种信息请求头
        self._torrent_seeding_headers = None

        # 错误信息
        self.err_msg = None

    def site_schema(self) -> SiteSchema:
        """
        站点解析模型
        :return: 站点解析模型
        """
        return self.schema

    def parse(self):
        """
        解析站点信息
        :return:
        """
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
                    headers=self._user_basic_headers
                )
            )
        else:
            self._parse_user_base_info(self._index_html)
        # 解析用户详细信息
        if self._user_detail_page:
            self._parse_user_detail_info(
                self._get_page_content(
                    url=urljoin(self._base_url, self._user_detail_page),
                    params=self._user_detail_params,
                    headers=self._user_detail_headers
                )
            )
        # 解析用户未读消息
        if settings.SITE_MESSAGE:
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

    def _pase_unread_msgs(self):
        """
        解析所有未读消息标题和内容
        :return:
        """
        unread_msg_links = []
        if self.message_unread > 0 or self.message_read_force:
            links = {self._user_mail_unread_page, self._sys_mail_unread_page}
            for link in links:
                if not link:
                    continue
                msg_links = []
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
            head, date, content = self._parse_message_content(
                self._get_page_content(
                    urljoin(self._base_url, msg_link),
                    params=self._mail_content_params,
                    headers=self._mail_content_headers
                )
            )
            logger.debug(f"{self._site_name} 标题 {head} 时间 {date} 内容 {content}")
            self.message_unread_contents.append((head, date, content))

    def _parse_seeding_pages(self):
        """
        解析做种页面
        """
        if self._torrent_seeding_page:
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
    def _prepare_html_text(html_text):
        """
        处理掉HTML中的干扰部分
        """
        return re.sub(r"#\d+", "", re.sub(r"\d+px", "", html_text))

    @abstractmethod
    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        """
        获取未阅读消息链接
        :param html_text:
        :return:
        """
        pass

    def _get_page_content(self, url: str, params: dict = None, headers: dict = None):
        """
        获取页面内容
        :param url: 网页地址
        :param params: post参数
        :param headers: 额外的请求头
        :return:
        """
        req_headers = None
        proxies = settings.PROXY if self._proxy else None
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
            session = None
        else:
            # 使用cookie请求
            cookie = self._site_cookie
            session = self._session

        if params:
            if req_headers.get("Content-Type") == "application/json":
                res = RequestUtils(cookies=cookie,
                                   session=session,
                                   timeout=60,
                                   proxies=proxies,
                                   headers=req_headers).post_res(url=url, json=params)
            else:
                res = RequestUtils(cookies=cookie,
                                   session=session,
                                   timeout=60,
                                   proxies=proxies,
                                   headers=req_headers).post_res(url=url, data=params)
        else:
            res = RequestUtils(cookies=cookie,
                               session=session,
                               timeout=60,
                               proxies=proxies,
                               headers=req_headers).get_res(url=url)
        if res is not None and res.status_code in (200, 500, 403):
            if req_headers and "application/json" in str(req_headers.get("Accept")):
                return json.dumps(res.json())
            else:
                # 如果cloudflare 有防护，尝试使用浏览器仿真
                if under_challenge(res.text):
                    logger.warn(
                        f"{self._site_name} 检测到Cloudflare，请更新Cookie和UA")
                    return ""
                return RequestUtils.get_decoded_html_content(res,
                                                             settings.ENCODING_DETECTION_PERFORMANCE_MODE,
                                                             settings.ENCODING_DETECTION_MIN_CONFIDENCE)

        return ""

    @abstractmethod
    def _parse_site_page(self, html_text: str):
        """
        解析站点相关信息页面
        :param html_text:
        :return:
        """
        pass

    @abstractmethod
    def _parse_user_base_info(self, html_text: str):
        """
        解析用户基础信息
        :param html_text:
        :return:
        """
        pass

    def _parse_logged_in(self, html_text):
        """
        解析用户是否已经登陆
        :param html_text:
        :return: True/False
        """
        logged_in = SiteUtils.is_logged_in(html_text)
        if not logged_in:
            self.err_msg = "未检测到已登陆，请检查cookies是否过期"
            logger.warn(f"{self._site_name} 未登录，跳过后续操作")

        return logged_in

    @abstractmethod
    def _parse_user_traffic_info(self, html_text: str):
        """
        解析用户的上传，下载，分享率等信息
        :param html_text:
        :return:
        """
        pass

    @abstractmethod
    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False) -> Optional[str]:
        """
        解析用户的做种相关信息
        :param html_text:
        :param multi_page: 是否多页数据
        :return: 下页地址
        """
        pass

    @abstractmethod
    def _parse_user_detail_info(self, html_text: str):
        """
        解析用户的详细信息
        加入时间/等级/魔力值等
        :param html_text:
        :return:
        """
        pass

    @abstractmethod
    def _parse_message_content(self, html_text):
        """
        解析短消息内容
        :param html_text:
        :return:  head: message, date: time, content: message content
        """
        pass

    def to_dict(self):
        """
        转化为字典
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
