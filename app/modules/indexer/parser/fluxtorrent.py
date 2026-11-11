"""
解析 FluxTorrent 兼容层的用户资料与流量信息。
"""

import json
from typing import Any, Dict, Optional, Tuple

from app.modules.indexer.parser import SitePage, SiteParserBase, SiteSchema
from app.runtime.log import logger


class FluxTorrentSiteUserInfo(SiteParserBase):
    """
    FluxTorrent 兼容层用户数据解析器。

    兼容层按 NexusPHP userdetails 口径返回用户资料，因此不需要解析站点页面。
    """

    schema = SiteSchema.FluxTorrent
    request_mode = "apikey"

    def _parse_site_page(self, html_text: SitePage) -> None:
        """
        配置兼容层用户信息接口地址和认证请求头。

        :param html_text: API Key 模式下的空首页数据
        """
        self._user_basic_page = "api/v1/compat/nexusphp/user.json"
        self._user_basic_params = {}
        self._user_detail_page = None
        self._user_traffic_page = None
        self._torrent_seeding_page = None
        self._user_mail_unread_page = None
        self._sys_mail_unread_page = None
        token = self.apikey or self.token or ""
        headers: Dict[str, Optional[str]] = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        }
        if self._ua:
            headers["User-Agent"] = self._ua
        self._addition_headers = headers

    def _load_api_payload(self, html_text: SitePage, operation: str) -> Optional[Dict[str, Any]]:
        """
        解析并校验兼容层统一响应信封。

        :param html_text: 兼容层响应文本
        :param operation: 错误信息中的操作名称
        :return: 成功时返回响应 data，失败时返回 None
        """
        if not html_text:
            self.err_msg = f"{operation}失败，未收到接口响应"
            return None
        payload: Any
        if isinstance(html_text, dict):
            payload = html_text
        else:
            try:
                payload = json.loads(html_text)
            except (TypeError, json.JSONDecodeError) as err:
                self.err_msg = f"{operation}失败，接口响应不是有效 JSON"
                logger.warning(f"{self._site_name} {self.err_msg}：{str(err)}")
                return None
        if not isinstance(payload, dict):
            self.err_msg = f"{operation}失败，接口响应结构无效"
            logger.warning(f"{self._site_name} {self.err_msg}")
            return None
        if payload.get("code") != 0:
            self.err_msg = payload.get("message") or f"{operation}失败"
            logger.warning(f"{self._site_name} {operation}失败：{self.err_msg}")
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def _parse_user_base_info(self, html_text: SitePage) -> None:
        """
        解析兼容层返回的用户资料与流量统计。

        :param html_text: user.json 接口响应文本
        """
        user_info = self._load_api_payload(html_text, "获取用户信息")
        if not user_info:
            return
        self.userid = user_info.get("id")
        self.username = user_info.get("username")
        self.user_level = user_info.get("class_name") or str(user_info.get("class") or "")
        self.upload = int(user_info.get("uploaded") or 0)
        self.download = int(user_info.get("downloaded") or 0)
        # 站点在只上传未下载时把分享率记为无穷大，此时没有可用的有限数值。
        if user_info.get("ratio_infinite") is True:
            self.ratio = 0.0
        else:
            self.ratio = float(user_info.get("ratio") or 0)
        self.bonus = float(user_info.get("seedbonus") or 0)

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        跳过独立流量页面，兼容层用户接口已经返回完整统计。

        :param html_text: 未使用的页面文本
        """

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        跳过独立用户详情页面，兼容层用户接口已经返回完整资料。

        :param html_text: 未使用的页面文本
        """

    def _parse_user_torrent_seeding_info(
        self,
        html_text: str,
        multi_page: bool = False,
    ) -> Optional[str]:
        """
        跳过独立做种列表，兼容层用户接口未提供做种统计。

        :param html_text: 未使用的页面文本
        :param multi_page: 是否为后续分页
        :return: 始终返回 None
        """
        return None

    def _parse_message_unread_links(self, html_text: str, msg_links: list[str]) -> Optional[str]:
        """
        兼容层未提供站内消息接口，跳过未读消息解析。

        :param html_text: 未使用的页面文本
        :param msg_links: 兼容解析器基类签名的消息链接容器
        :return: 始终返回 None
        """
        return None

    def _parse_message_content(
        self,
        html_text: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        兼容层未提供站内消息接口，跳过消息内容解析。

        :param html_text: 未使用的页面文本
        :return: 三个空值
        """
        return None, None, None
