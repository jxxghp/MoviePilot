# -*- coding: utf-8 -*-
import json
from typing import Optional, Tuple

from app.log import logger
from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class YemaSiteUserInfo(SiteParserBase):
    """
    YemaPT 开放 API 用户数据解析器
    """

    schema = SiteSchema.Yema
    request_mode = "apikey"

    def _parse_site_page(self, html_text: str) -> None:
        """
        配置 YemaPT 用户基本信息接口和认证请求头

        :param html_text: API AuthKey 模式下的空首页数据
        """
        self._user_basic_page = "openApi/user/fetchBasicInfo.json"
        self._user_basic_params = {}
        self._user_basic_method = "post"
        self._user_detail_page = None
        self._user_traffic_page = None
        self._torrent_seeding_page = None
        self._sys_mail_unread_page = None
        self._user_mail_unread_page = None
        self._addition_headers = {
            "Authorization": self.apikey,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self._ua,
        }

    def _parse_user_base_info(self, html_text: str) -> None:
        """
        解析开放 API 返回的用户基本信息和促销流量

        :param html_text: fetchBasicInfo 接口响应文本
        """
        if not html_text:
            self.err_msg = "获取用户信息失败，未收到开放 API 响应"
            return
        try:
            payload = json.loads(html_text)
        except (TypeError, json.JSONDecodeError) as err:
            self.err_msg = "获取用户信息失败，开放 API 响应不是有效 JSON"
            logger.warning(f"{self._site_name} {self.err_msg}：{str(err)}")
            return
        if not isinstance(payload, dict):
            self.err_msg = "获取用户信息失败，开放 API 响应结构无效"
            logger.warning(f"{self._site_name} {self.err_msg}")
            return
        if not payload.get("success") or not isinstance(payload.get("data"), dict):
            self.err_msg = payload.get("errorMessage") or "获取用户信息失败"
            logger.warning(f"{self._site_name} 获取用户信息失败：{self.err_msg}")
            return

        user_info = payload["data"]
        self.userid = user_info.get("id")
        self.username = user_info.get("name")
        self.user_level = str(user_info.get("level")) \
            if user_info.get("level") is not None else None
        self.join_at = StringUtils.unify_datetime_str(user_info.get("registerTime"))
        self.upload = int(user_info.get("promotionUploadSize") or 0)
        self.download = int(user_info.get("promotionDownloadSize") or 0)
        self.ratio = round(self.upload / (self.download or 1), 2)
        self.bonus = float(user_info.get("bonus") or 0)

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        跳过独立流量页面，用户基本信息接口已经返回促销流量

        :param html_text: 未使用的页面文本
        """

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        跳过独立用户详情页面，开放 API 未提供该接口

        :param html_text: 未使用的页面文本
        """

    def _parse_user_torrent_seeding_info(
        self,
        html_text: str,
        multi_page: bool = False,
    ) -> Optional[str]:
        """
        跳过做种统计，开放 API 未提供该接口

        :param html_text: 未使用的页面文本
        :param multi_page: 是否为后续分页
        :return: 始终返回 None
        """
        return None

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        """
        跳过站内消息，开放 API 未提供该接口

        :param html_text: 未使用的页面文本
        :param msg_links: 未使用的消息链接容器
        :return: 始终返回 None
        """
        return None

    def _parse_message_content(
        self,
        html_text: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        跳过消息详情，开放 API 未提供该接口

        :param html_text: 未使用的页面文本
        :return: 三个空值
        """
        return None, None, None
