# -*- coding: utf-8 -*-
import re
from typing import Optional

from lxml import etree

from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class TorrentLeechSiteUserInfo(SiteParserBase):
    """
    TorrentLeech 站点用户信息解析器
    """

    schema = SiteSchema.TorrentLeech

    def _parse_site_page(self, html_text: str) -> None:
        """
        解析当前用户 ID 并初始化用户资料页面地址

        :param html_text: 站点首页 HTML
        """
        html_text = self._prepare_html_text(html_text)

        html = etree.HTML(html_text)
        current_userid = None
        try:
            if StringUtils.is_valid_html_element(html):
                profile_routes = html.xpath(
                    '//span[contains(concat(" ", normalize-space(@class), " "), " centerTopBar ")]'
                    '//*[@onclick]/@onclick'
                )
                for route in profile_routes:
                    profile_view = re.search(r"/profile/([^/]+)/view", route)
                    if profile_view:
                        current_userid = profile_view.group(1).strip()
                        break
        finally:
            if html is not None:
                del html

        user_detail = re.search(r"/profile/([^/]+)/", html_text)
        fallback_userid = user_detail.group(1).strip() if user_detail else None
        self.userid = current_userid or fallback_userid
        if not self.userid:
            self.err_msg = "未获取到用户ID"
            self._user_detail_page = None
            self._user_traffic_page = None
            self._torrent_seeding_page = None
            return

        self._user_detail_page = f"profile/{self.userid}/view"
        self._user_traffic_page = None
        self._torrent_seeding_page = f"profile/{self.userid}/seeding"

    def _parse_user_base_info(self, html_text: str) -> None:
        """
        使用用户 ID 初始化基础用户名

        :param html_text: 站点首页 HTML
        """
        self.username = self.userid

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        解析用户资料页中的用户名、流量、等级、注册时间和积分

        :param html_text: 用户资料页 HTML
        """
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)
        try:
            if not StringUtils.is_valid_html_element(html):
                return

            username_html = html.xpath('//div[contains(concat(" ", normalize-space(@class), " "), '
                                       '" profile-username ")]/text()')
            if not username_html:
                username_html = html.xpath('//table[contains(@class, "profileViewTable")]'
                                           '//tr/td[normalize-space()="Username"]/'
                                           'following-sibling::td[1]/text()')
            if username_html and username_html[0].strip():
                self.username = username_html[0].strip()

            upload_html = html.xpath('//div[contains(@class,"profile-uploaded")]//span/text()')
            if upload_html:
                self.upload = StringUtils.num_filesize(upload_html[0])
            download_html = html.xpath('//div[contains(@class,"profile-downloaded")]//span/text()')
            if download_html:
                self.download = StringUtils.num_filesize(download_html[0])
            ratio_html = html.xpath('//div[contains(@class,"profile-ratio")]//span/text()')
            if ratio_html:
                self.ratio = StringUtils.str_float(ratio_html[0].replace('∞', '0'))

            user_level_html = html.xpath('//table[contains(@class, "profileViewTable")]'
                                         '//tr/td[normalize-space()="Class"]/'
                                         'following-sibling::td[1]/text()')
            if user_level_html:
                self.user_level = user_level_html[0].strip()

            join_at_html = html.xpath('//table[contains(@class, "profileViewTable")]'
                                      '//tr/td[normalize-space()="Registration date"]/'
                                      'following-sibling::td[1]/text()')
            if join_at_html:
                self.join_at = StringUtils.unify_datetime_str(join_at_html[0].strip())

            bonus_html = html.xpath('//span[contains(@class, "total-TL-points")]/text()')
            if bonus_html:
                self.bonus = StringUtils.str_float(bonus_html[0].strip())
        finally:
            if html is not None:
                del html

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        解析包含流量和账户属性的用户资料页

        :param html_text: 用户资料页 HTML
        """
        self._parse_user_traffic_info(html_text)

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: Optional[bool] = False) -> Optional[str]:
        """
        做种相关信息
        :param html_text:
        :param multi_page: 是否多页数据
        :return: 下页地址
        """
        html = etree.HTML(html_text)
        try:
            if not StringUtils.is_valid_html_element(html):
                return None

            size_col = 2
            seeders_col = 7

            page_seeding = 0
            page_seeding_size = 0
            page_seeding_info = []
            seeding_sizes = html.xpath(f'//tbody/tr/td[{size_col}]')
            seeding_seeders = html.xpath(f'//tbody/tr/td[{seeders_col}]/text()')
            if seeding_sizes and seeding_seeders:
                page_seeding = len(seeding_sizes)

                for i in range(0, len(seeding_sizes)):
                    size = StringUtils.num_filesize(seeding_sizes[i].xpath("string(.)").strip())
                    seeders = StringUtils.str_int(seeding_seeders[i])

                    page_seeding_size += size
                    page_seeding_info.append([seeders, size])

            self.seeding += page_seeding
            self.seeding_size += page_seeding_size
            self.seeding_info.extend(page_seeding_info)

            # 是否存在下页数据
            next_page = None
        finally:
            if html is not None:
                del html

        return next_page

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        """
        TorrentLeech 暂不解析未读消息链接

        :param html_text: 消息页面 HTML
        :param msg_links: 已解析的消息链接列表
        :return: 始终返回 None
        """
        return None

    def _parse_message_content(self, html_text: str) -> tuple[None, None, None]:
        """
        TorrentLeech 暂不解析消息正文

        :param html_text: 消息正文页面 HTML
        :return: 空标题、时间和正文
        """
        return None, None, None
