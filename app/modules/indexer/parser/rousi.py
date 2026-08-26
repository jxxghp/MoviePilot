# -*- coding: utf-8 -*-
import json
from typing import Optional, Tuple
from urllib.parse import urljoin

from app.adapters.network.http import RequestUtils
from app.domain import site as site_rules
from app.foundation import temporal as time_tools
from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


class RousiSiteUserInfo(SiteParserBase):
    """
    Rousi.pro PeerGo 用户数据解析器。

    使用具有 profile:read 权限的个人 API Key 访问兼容资料接口。
    """
    schema = SiteSchema.RousiPro
    request_mode = "apikey"

    def _parse_site_page(self, html_text: str):
        """
        配置 API 请求地址和请求头
        使用 PeerGo MoviePilot 兼容的 /profile 接口获取用户信息。
        """
        self._base_url = f"https://{site_rules.extract_domain(self._site_url)}"
        self._user_basic_page = "api/v1/profile"
        self._user_basic_params = {}
        self._user_basic_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.apikey}"
        }

        # Rousi.pro API v1 在单个接口返回所有信息，无需额外页面
        self._user_traffic_page = None
        self._user_detail_page = None
        self._torrent_seeding_page = None
        self._user_mail_unread_page = None
        self._sys_mail_unread_page = None

    def _parse_logged_in(self, html_text):
        """
        判断是否登录成功
        API 认证模式下，通过 HTTP 状态码判断，此处始终返回 True
        """
        return True

    def _parse_user_base_info(self, html_text: str):
        """
        解析用户基本信息
        通过 API v1 接口获取用户完整信息，包括上传下载量、做种数据等

        API 响应示例：
        {
            "code": 0,
            "message": "success",
            "data": {
                "id": 1,
                "username": "example",
                "level_text": "Lv.5",
                "registered_at": "2024-01-01T00:00:00Z",
                "uploaded": 1073741824,
                "downloaded": 536870912,
                "ratio": 2.0,
                "karma": 1000.5,
                "seeding_leeching_data": {
                    "seeding_count": 10,
                    "seeding_size": 10737418240,
                    "leeching_count": 2,
                    "leeching_size": 2147483648
                }
            }
        }
        """
        if not html_text:
            return

        try:
            data = json.loads(html_text)
        except json.JSONDecodeError:
            logger.error(f"{self._site_name} JSON 解析失败")
            return

        if not isinstance(data, dict):
            self.err_msg = "用户数据响应结构无效"
            logger.warning(f"{self._site_name} API 响应结构无效")
            return

        if data.get("code") != 0:
            self.err_msg = str(data.get("message") or "未知错误")
            logger.warning(f"{self._site_name} API 错误: {self.err_msg}")
            return

        user_info = data.get("data")
        if not isinstance(user_info, dict):
            return

        # 基本信息
        self.userid = user_info.get("id")
        self.username = user_info.get("username")
        self.user_level = user_info.get("level_text") or user_info.get("role_text")

        # 注册时间：统一格式为 YYYY-MM-DD HH:MM:SS
        registered_at = user_info.get("registered_at")
        join_at = (
            time_tools.normalize_datetime(registered_at)
            if isinstance(registered_at, str)
            else None
        )
        if join_at:
            # 确保格式为 YYYY-MM-DD HH:MM:SS (19位)
            if len(join_at) >= 19:
                self.join_at = join_at[:19]
            else:
                self.join_at = join_at

        # 流量信息
        self.upload = int(user_info.get("uploaded") or 0)
        self.download = int(user_info.get("downloaded") or 0)
        self.ratio = round(float(user_info.get("ratio") or 0), 2)

        # 魔力值（站点称为 karma）
        self.bonus = float(user_info.get("karma") or 0)

        # 做种/下载中数据
        sl_data = user_info.get("seeding_leeching_data", {})
        self.seeding = int(sl_data.get("seeding_count") or 0)
        self.seeding_size = int(sl_data.get("seeding_size") or 0)
        self.leeching = int(sl_data.get("leeching_count") or 0)
        self.leeching_size = int(sl_data.get("leeching_size") or 0)

    def _parse_user_traffic_info(self, html_text: str):
        """
        解析用户流量信息
        Rousi.pro API v1 在 _parse_user_base_info 中已完成所有解析，此方法无需实现
        """
        pass

    def _parse_user_detail_info(self, html_text: str):
        """
        解析用户详细信息
        Rousi.pro API v1 在 _parse_user_base_info 中已完成所有解析，此方法无需实现
        """
        pass

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: Optional[bool] = False) -> Optional[str]:
        """
        解析用户做种信息
        Rousi.pro API v1 在 _parse_user_base_info 中已通过 seeding_leeching_data 获取做种数据

        :param html_text: 页面内容
        :param multi_page: 是否多页数据
        :return: 下页地址（无下页返回 None）
        """
        return None

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        """
        解析未读消息链接
        Rousi.pro API v1 暂未提供消息相关接口

        :param html_text: 页面内容
        :param msg_links: 消息链接列表
        :return: 下页地址（无下页返回 None）
        """
        return None

    def _parse_message_content(self, html_text) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        解析消息内容
        Rousi.pro API v1 暂未提供消息相关接口

        :param html_text: 页面内容
        :return: (标题, 日期, 内容)
        """
        return None, None, None

    def _pase_unread_msgs(self):
        """
        解析所有未读消息标题和内容
        Rousi.pro API v1 暂未提供消息相关接口，暂时以网页接口实现
        
        :return:
        """
        if not self.token:
            logger.warn(f"{self._site_name} 站点未配置 Authorization 请求头，跳过消息解析")
            return
        
        headers = {
            "User-Agent": self._ua,
            "Accept": "application/json, text/plain, */*",
            "Authorization": self.token if self.token.startswith("Bearer ") else f"Bearer {self.token}"
        }
        
        def __get_message_list(page: int):
            params = {
                "page": page,
                "page_size": 100,
                "unread_only": "true"
            }
            res = RequestUtils(
                headers=headers,
                timeout=60,
                proxies=get_runtime_setting('PROXY') if self._proxy else None
            ).get_res(
                url=urljoin(self._base_url, "api/messages"),
                params=params
            )
            if not res or res.status_code != 200 or res.json().get("code", -1) != 0:
                logger.warn(f"{self._site_name} 站点解析消息失败，状态码: {res.status_code if res else '无响应'}")
                return {
                    "messages": [],
                    "total_pages": 0
                }
            return res.json().get("data")
        
        # 分页获取所有未读消息
        page = 0
        res = __get_message_list(page)
        page += 1
        messages = res.get("messages", [])
        total_pages = res.get("total_pages", 0)
        while page < total_pages:
            res = __get_message_list(page)
            messages.extend(res.get("messages", []))
            page += 1
        
        self.message_unread = len(messages)
        for messsage in messages:
            head = messsage.get("title")
            date = time_tools.normalize_datetime(messsage.get("created_at"))
            content = messsage.get("content")
            logger.debug(f"{self._site_name} 标题 {head} 时间 {date} 内容 {content}")
            self.message_unread_contents.append((head, date, content))
            
        # 更新消息为已读
        RequestUtils(
            headers=headers,
            timeout=60,
            proxies=get_runtime_setting('PROXY') if self._proxy else None
        ).post_res(
            url=urljoin(self._base_url, "api/messages/read-all")
        )
