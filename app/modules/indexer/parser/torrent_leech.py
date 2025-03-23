# -*- coding: utf-8 -*-
import re
from typing import Optional

from lxml import etree

from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class TorrentLeechSiteUserInfo(SiteParserBase):
    schema = SiteSchema.TorrentLeech

    def _parse_site_page(self, html_text: str):
        html_text = self._prepare_html_text(html_text)

        user_detail = re.search(r"/profile/([^/]+)/", html_text)
        if user_detail and user_detail.group().strip():
            self._user_detail_page = user_detail.group().strip().lstrip('/')
            self.userid = user_detail.group(1)
        self._user_traffic_page = f"profile/{self.userid}/view"
        self._torrent_seeding_page = f"profile/{self.userid}/seeding"

    def _parse_user_base_info(self, html_text: str):
        self.username = self.userid

    def _parse_user_traffic_info(self, html_text: str):
        """
        上传/下载/分享率 [做种数/魔力值]
        :param html_text:
        :return:
        """
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)
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
                                     '//tr/td[text()="Class"]/following-sibling::td/text()')
        if user_level_html:
            self.user_level = user_level_html[0].strip()

        join_at_html = html.xpath('//table[contains(@class, "profileViewTable")]'
                                  '//tr/td[text()="Registration date"]/following-sibling::td/text()')
        if join_at_html:
            self.join_at = StringUtils.unify_datetime_str(join_at_html[0].strip())

        bonus_html = html.xpath('//span[contains(@class, "total-TL-points")]/text()')
        if bonus_html:
            self.bonus = StringUtils.str_float(bonus_html[0].strip())

    def _parse_user_detail_info(self, html_text: str):
        pass

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: Optional[bool] = False) -> Optional[str]:
        """
        做种相关信息
        :param html_text:
        :param multi_page: 是否多页数据
        :return: 下页地址
        """
        html = etree.HTML(html_text)
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

        return next_page

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        return None

    def _parse_message_content(self, html_text):
        return None, None, None
