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

SITE_BASE_ORDER = 1000


# 站点框架
class SiteSchema(Enum):
    DiscuzX = "Discuz!"
    Gazelle = "Gazelle"
    Ipt = "IPTorrents"
    NexusPhp = "NexusPhp"
    NexusProject = "NexusProject"
    NexusRabbit = "NexusRabbit"
    NexusHhanclub = "NexusHhanclub"
    SmallHorse = "Small Horse"
    Unit3d = "Unit3d"
    TorrentLeech = "TorrentLeech"
    FileList = "FileList"
    TNode = "TNode"


class ISiteUserInfo(metaclass=ABCMeta):
    # 站点模版
    schema = SiteSchema.NexusPhp
    # 站点解析时判断顺序，值越小越先解析
    order = SITE_BASE_ORDER

    def __init__(self, site_name: str,
                 url: str,
                 site_cookie: str,
                 index_html: str,
                 session: Session = None,
                 ua: str = None,
                 emulate: bool = False,
                 proxy: bool = None):
        super().__init__()
        # 站点信息
        self.site_name = None
        self.site_url = None
        # 用户信息
        self.username = None
        self.userid = None
        # 未读消息
        self.message_unread = 0
        self.message_unread_contents = []

        # 流量信息
        self.upload = 0
        self.download = 0
        self.ratio = 0

        # 种子信息
        self.seeding = 0
        self.leeching = 0
        self.uploaded = 0
        self.completed = 0
        self.incomplete = 0
        self.seeding_size = 0
        self.leeching_size = 0
        self.uploaded_size = 0
        self.completed_size = 0
        self.incomplete_size = 0
        # 做种人数, 种子大小
        self.seeding_info = []

        # 用户详细信息
        self.user_level = None
        self.join_at = None
        self.bonus = 0.0

        # 错误信息
        self.err_msg = None
        # 内部数据
        self._base_url = None
        self._site_cookie = None
        self._index_html = None
        self._addition_headers = None

        # 站点页面
        self._brief_page = "index.php"
        self._user_detail_page = "userdetails.php?id="
        self._user_traffic_page = "index.php"
        self._torrent_seeding_page = "getusertorrentlistajax.php?userid="
        self._user_mail_unread_page = "messages.php?action=viewmailbox&box=1&unread=yes"
        self._sys_mail_unread_page = "messages.php?action=viewmailbox&box=-2&unread=yes"
        self._torrent_seeding_params = None
        self._torrent_seeding_headers = None

        split_url = urlsplit(url)
        self.site_name = site_name
        self.site_url = url
        self._base_url = f"{split_url.scheme}://{split_url.netloc}"
        self._site_cookie = site_cookie
        self._index_html = index_html
        self._session = session if session else None
        self._ua = ua

        self._emulate = emulate
        self._proxy = proxy

    def site_schema(self) -> SiteSchema:
        """
        站点解析模型
        :return: 站点解析模型
        """
        return self.schema

    @classmethod
    def match(cls, html_text: str) -> bool:
        """
        是否匹配当前解析模型
        :param html_text: 站点首页html
        :return: 是否匹配
        """
        pass

    def parse(self):
        """
        解析站点信息
        :return:
        """
        if not self._parse_logged_in(self._index_html):
            return

        self._parse_site_page(self._index_html)
        self._parse_user_base_info(self._index_html)
        self._pase_unread_msgs()
        if self._user_traffic_page:
            self._parse_user_traffic_info(self._get_page_content(urljoin(self._base_url, self._user_traffic_page)))
        if self._user_detail_page:
            self._parse_user_detail_info(self._get_page_content(urljoin(self._base_url, self._user_detail_page)))

        self._parse_seeding_pages()
        self.seeding_info = json.dumps(self.seeding_info)

    def _pase_unread_msgs(self):
        """
        解析所有未读消息标题和内容
        :return:
        """
        unread_msg_links = []
        if self.message_unread > 0:
            links = {self._user_mail_unread_page, self._sys_mail_unread_page}
            for link in links:
                if not link:
                    continue

                msg_links = []
                next_page = self._parse_message_unread_links(
                    self._get_page_content(urljoin(self._base_url, link)), msg_links)
                while next_page:
                    next_page = self._parse_message_unread_links(
                        self._get_page_content(urljoin(self._base_url, next_page)), msg_links)

                unread_msg_links.extend(msg_links)

        for msg_link in unread_msg_links:
            logger.debug(f"{self.site_name} 信息链接 {msg_link}")
            head, date, content = self._parse_message_content(self._get_page_content(urljoin(self._base_url, msg_link)))
            logger.debug(f"{self.site_name} 标题 {head} 时间 {date} 内容 {content}")
            self.message_unread_contents.append((head, date, content))

    def _parse_seeding_pages(self):
        if self._torrent_seeding_page:
            # 第一页
            next_page = self._parse_user_torrent_seeding_info(
                self._get_page_content(urljoin(self._base_url, self._torrent_seeding_page),
                                       self._torrent_seeding_params,
                                       self._torrent_seeding_headers))

            # 其他页处理
            while next_page:
                next_page = self._parse_user_torrent_seeding_info(
                    self._get_page_content(urljoin(urljoin(self._base_url, self._torrent_seeding_page), next_page),
                                           self._torrent_seeding_params,
                                           self._torrent_seeding_headers),
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
        :param url: 网页地址
        :param params: post参数
        :param headers: 额外的请求头
        :return:
        """
        req_headers = None
        proxies = settings.PROXY if self._proxy else None
        if self._ua or headers or self._addition_headers:
            req_headers = {}
            if headers:
                req_headers.update(headers)

            req_headers.update({
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": f"{self._ua}"
            })

            if self._addition_headers:
                req_headers.update(self._addition_headers)

        if params:
            res = RequestUtils(cookies=self._site_cookie,
                               session=self._session,
                               timeout=60,
                               proxies=proxies,
                               headers=req_headers).post_res(url=url, data=params)
        else:
            res = RequestUtils(cookies=self._site_cookie,
                               session=self._session,
                               timeout=60,
                               proxies=proxies,
                               headers=req_headers).get_res(url=url)
        if res is not None and res.status_code in (200, 500, 403):
            # 如果cloudflare 有防护，尝试使用浏览器仿真
            if under_challenge(res.text):
                logger.warn(
                    f"{self.site_name} 检测到Cloudflare，请更新Cookie和UA")
                return ""
            if re.search(r"charset=\"?utf-8\"?", res.text, re.IGNORECASE):
                res.encoding = "utf-8"
            else:
                res.encoding = res.apparent_encoding
            return res.text

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
            logger.warn(f"{self.site_name} 未登录，跳过后续操作")

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
