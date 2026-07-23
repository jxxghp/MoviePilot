# -*- coding: utf-8 -*-
import json
from typing import Optional, Tuple
from urllib.parse import urlencode, urljoin

from app.log import logger
from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class SunnyPTSiteUserInfo(SiteParserBase):
    """
    SunnyPT MoviePilot API 用户数据解析器
    """

    schema = SiteSchema.SunnyPT
    request_mode = "apikey"

    def _parse_site_page(self, html_text: str) -> None:
        """
        配置 SunnyPT 用户数据接口地址和认证请求头

        :param html_text: API Key 模式下的空首页数据
        """
        self._base_url = f"{str(self._api_url or 'https://api.sunnypt.top/api/v1/mp').rstrip('/')}/"
        self._user_basic_page = "profile"
        self._user_detail_page = None
        self._user_traffic_page = None
        self._torrent_seeding_page = None
        self._user_mail_unread_page = "messages"
        self._sys_mail_unread_page = None
        self._addition_headers = {
            "Accept": "application/json",
            "User-Agent": self._ua,
            "X-API-Key": self.apikey,
        }

    def _load_api_payload(self, html_text: str, operation: str) -> Optional[dict]:
        """
        解析并校验 SunnyPT 通用 JSON 响应

        :param html_text: API 响应文本
        :param operation: 错误信息中的操作名称
        :return: 成功时返回响应 data，失败时返回 None
        """
        if not html_text:
            self.err_msg = f"{operation}失败，未收到 API 响应"
            return None
        try:
            payload = json.loads(html_text)
        except (TypeError, json.JSONDecodeError) as err:
            self.err_msg = f"{operation}失败，API 响应不是有效 JSON"
            logger.warning(f"{self._site_name} {self.err_msg}：{str(err)}")
            return None
        if not isinstance(payload, dict):
            self.err_msg = f"{operation}失败，API 响应结构无效"
            logger.warning(f"{self._site_name} {self.err_msg}")
            return None
        if str(payload.get("code")) != "0":
            self.err_msg = payload.get("msg") or f"{operation}失败"
            logger.warning(f"{self._site_name} {operation}失败：{self.err_msg}")
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def _parse_user_base_info(self, html_text: str) -> None:
        """
        解析 SunnyPT 用户资料、流量和做种统计

        :param html_text: profile 接口响应文本
        """
        user_info = self._load_api_payload(html_text, "获取用户信息")
        if not user_info:
            return
        self.userid = user_info.get("id")
        self.username = user_info.get("username")
        self.user_level = user_info.get("level") or str(user_info.get("class") or "")
        self.join_at = StringUtils.unify_datetime_str(user_info.get("registered_at"))
        self.upload = int(user_info.get("uploaded") or 0)
        self.download = int(user_info.get("downloaded") or 0)
        self.ratio = float(user_info.get("ratio") or 0)
        self.bonus = float(user_info.get("bonus") or 0)
        self.seeding = int(user_info.get("seeding_count") or 0)
        self.seeding_size = int(user_info.get("seeding_size") or 0)
        self.leeching = int(user_info.get("leeching_count") or 0)
        self.leeching_size = int(user_info.get("leeching_size") or 0)
        self.message_unread = int(user_info.get("unread_messages") or 0)

    def _pase_unread_msgs(self) -> None:
        """
        分页读取 SunnyPT 未读消息；解析阶段不标记已读，避免投递失败后丢失通知
        """
        page = 1
        while True:
            query = urlencode({
                "unread_only": "true",
                "page": page,
                "page_size": 100,
            })
            html_text = self._get_page_content(
                url=urljoin(self._base_url, f"messages?{query}")
            )
            has_more = self._parse_message_unread_links(html_text, [])
            if not has_more:
                return
            page += 1

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        """
        解析 SunnyPT 未读消息列表并直接保存消息正文

        :param html_text: messages 接口响应文本
        :param msg_links: 兼容解析器基类签名的消息链接容器
        :return: 存在下一页时返回占位字符串，否则返回 None
        """
        messages_data = self._load_api_payload(html_text, "获取未读消息")
        if not messages_data:
            return None
        self.message_unread = int(messages_data.get("unread_count") or 0)
        for message in messages_data.get("items") or []:
            if not isinstance(message, dict) or not message.get("unread"):
                continue
            title = message.get("title")
            content = message.get("content")
            created_at = StringUtils.unify_datetime_str(message.get("created_at"))
            message_id = message.get("id")
            if title and content and created_at:
                message_source = f"sunnypt-message:{message_id}" if message_id is not None else None
                self.message_unread_contents.append(
                    (title, created_at, content, message_source)
                )
        return "next" if messages_data.get("has_more") else None

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        跳过独立流量页面，profile 接口已经返回完整统计

        :param html_text: 未使用的页面文本
        """

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        跳过独立用户详情页面，profile 接口已经返回完整资料

        :param html_text: 未使用的页面文本
        """

    def _parse_user_torrent_seeding_info(
        self,
        html_text: str,
        multi_page: bool = False,
    ) -> Optional[str]:
        """
        跳过独立做种列表，profile 接口已经返回做种数量和体积

        :param html_text: 未使用的页面文本
        :param multi_page: 是否为后续分页
        :return: 始终返回 None
        """
        return None

    def _parse_message_content(
        self,
        html_text: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        跳过消息详情请求，messages 接口已经返回完整正文

        :param html_text: 未使用的页面文本
        :return: 三个空值
        """
        return None, None, None
