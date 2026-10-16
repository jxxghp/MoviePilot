# -*- coding: utf-8 -*-
import re
from typing import Optional

from lxml import etree

from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.foundation import text as text_tools
from app.foundation.dom import DomUtils
from app.modules.indexer.parser import SiteParserBase, SiteSchema


class SpeedAppSiteUserInfo(SiteParserBase):
    """SpeedApp 用户资料、流量和站点统计解析器。"""

    schema = SiteSchema.SpeedApp

    def _parse_logged_in(self, html_text: str) -> bool:
        """通过 SpeedApp 的用户资料链接补充登录态识别。"""
        html = etree.HTML(html_text)
        try:
            profile_hrefs = html.xpath("//a[contains(@href, '/profile/')]/@href") if html is not None else []
            if any(re.search(r"/profile/[^/?#]+", href) for href in profile_hrefs):
                return True
        finally:
            if html is not None:
                del html
        return super()._parse_logged_in(html_text)

    def _parse_site_page(self, html_text: str) -> None:
        """配置 SpeedApp 的资料和统计页面，避免调用不兼容的通用 PT 接口。"""
        self._user_basic_page = "profile"
        self._user_detail_page = "profile/menu-stats"
        self._user_traffic_page = None
        self._torrent_seeding_page = None
        self._user_mail_unread_page = None
        self._sys_mail_unread_page = None

    @staticmethod
    def _extract_number(text: str) -> Optional[str]:
        """从页面文本中提取带千位分隔符的整数或小数。"""
        match = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
        return match.group(0) if match else None

    @staticmethod
    def _extract_dt_value(html, labels: tuple[str, ...]) -> str:
        """按资料页的 ``dt`` 标签读取相邻 ``dd`` 文本。"""
        normalized_labels = tuple(label.casefold() for label in labels)
        for label_node in html.xpath("//dt"):
            label_text = " ".join(label_node.xpath(".//text()") or []).strip().casefold()
            if not any(label in label_text for label in normalized_labels):
                continue
            value_nodes = label_node.xpath("following-sibling::dd[1]")
            if value_nodes:
                return value_nodes[0].xpath("string(.)").strip()
        return ""

    @staticmethod
    def _extract_link_value(html, href: str, title: str) -> str:
        """读取 SpeedApp 统计菜单中指定链接的文本。"""
        nodes = html.xpath(f"//a[@href='{href}' and @title='{title}']")
        return nodes[0].xpath("string(.)").strip() if nodes else ""

    def _parse_user_base_info(self, html_text: str) -> None:
        """解析 SpeedApp 资料页中的用户身份、流量、等级和注册时间。"""
        html = etree.HTML(self._prepare_html_text(html_text))
        try:
            if not DomUtils.has_child_elements(html):
                return

            profile_links = html.xpath("//div[@id='kt_profile_aside']//a[contains(@href, '/profile/')]")
            for profile_link in profile_links:
                profile_href = profile_link.get("href") or ""
                profile_match = re.search(r"/profile/([^/?#]+)", profile_href)
                if not profile_match:
                    continue
                self.userid = profile_match.group(1)
                self.username = profile_link.xpath("string(.)").strip() or self.userid
                break

            upload_text = self._extract_dt_value(html, ("Uploaded", "已上传"))
            if upload_text:
                self.upload = size_tools.parse_size(upload_text)
            download_text = self._extract_dt_value(html, ("Downloaded", "已下载"))
            if download_text:
                self.download = size_tools.parse_size(download_text)
            ratio_text = self._extract_dt_value(html, ("Ratio", "分享率", "比率"))
            ratio_value = self._extract_number(ratio_text)
            if ratio_value:
                self.ratio = text_tools.parse_float(ratio_value)
            elif self.download:
                self.ratio = round(self.upload / self.download, 3)

            level_nodes = html.xpath(
                "//div[contains(@class, 'card-body') and contains(@class, 'pt-4')]"
                "//div[contains(concat(' ', normalize-space(@class), ' '), ' text-muted ')]"
            )
            if level_nodes:
                self.user_level = level_nodes[0].xpath("string(.)").strip()

            join_text = self._extract_dt_value(html, ("Signup date", "注册日期"))
            if join_text:
                self.join_at = time_tools.normalize_datetime(join_text)

            message_nodes = html.xpath(
                "//div[@id='notifications-oc-toggle']"
                "//span[contains(concat(' ', normalize-space(@class), ' '), ' label-danger ')]"
            )
            if message_nodes:
                message_text = self._extract_number(message_nodes[0].xpath("string(.)"))
                self.message_unread = text_tools.parse_int(message_text) if message_text else 0

            bonus_dt = self._extract_dt_value(html, ("Bonus points", "奖励积分"))
            bonus_values = [node.xpath("string(.)").strip() for node in html.xpath(
                "//dt[contains(., 'Bonus points') or contains(., '奖励积分')]/following-sibling::dd[1]//b"
            )]
            if not bonus_values and bonus_dt:
                bonus_values = [bonus_dt]
            for value in reversed(bonus_values):
                if re.search(r"[KMGTPI]i?B", value, flags=re.IGNORECASE):
                    self.seeding_size = size_tools.parse_size(value)
                    break
        finally:
            if html is not None:
                del html

    def _parse_user_detail_info(self, html_text: str) -> None:
        """解析 SpeedApp 菜单统计中的积分和当前做种数。"""
        html = etree.HTML(self._prepare_html_text(html_text))
        try:
            if not DomUtils.has_child_elements(html):
                return

            bonus_text = self._extract_link_value(html, "/shop", "Bonus points")
            bonus_value = self._extract_number(bonus_text)
            if bonus_value:
                self.bonus = text_tools.parse_float(bonus_value)

            seeding_text = self._extract_link_value(
                html, "/snatch/seeding", "Currently seeding torrents"
            )
            seeding_value = self._extract_number(seeding_text)
            if seeding_value:
                self.seeding = text_tools.parse_int(seeding_value)
        finally:
            if html is not None:
                del html

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """SpeedApp 资料页已包含流量数据，不再请求独立流量页面。"""

    def _parse_user_torrent_seeding_info(
        self,
        html_text: str,
        multi_page: bool = False,
    ) -> Optional[str]:
        """SpeedApp 菜单统计已提供做种数，不请求未声明的做种列表接口。"""
        return None

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        """SpeedApp 当前适配只读取通知数量，不解析通知正文。"""
        return None

    def _parse_message_content(self, html_text: str) -> tuple[None, None, None]:
        """SpeedApp 当前适配不读取通知正文。"""
        return None, None, None
