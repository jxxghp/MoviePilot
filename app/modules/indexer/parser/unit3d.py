# -*- coding: utf-8 -*-
import re
from typing import Optional

from lxml import etree

from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class Unit3dSiteUserInfo(SiteParserBase):
    schema = SiteSchema.Unit3d

    def _parse_user_base_info(self, html_text: str):
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)

        tmps = html.xpath('//a[contains(@href, "/users/") and contains(@href, "settings")]/@href')
        if tmps:
            user_name_match = re.search(r"/users/(.+)/settings", tmps[0])
            if user_name_match and user_name_match.group().strip():
                self.username = user_name_match.group(1)
                self._torrent_seeding_page = f"/users/{self.username}/active?perPage=100&client=&seeding=include"
                self._user_detail_page = f"/users/{self.username}"

        tmps = html.xpath('//a[contains(@href, "bonus/earnings")]')
        if tmps:
            bonus_text = tmps[0].xpath("string(.)")
            bonus_match = re.search(r"([\d,.]+)", bonus_text)
            if bonus_match and bonus_match.group(1).strip():
                self.bonus = StringUtils.str_float(bonus_match.group(1))

    def _parse_site_page(self, html_text: str):
        pass

    def _parse_user_detail_info(self, html_text: str):
        """
        解析用户额外信息，加入时间，等级
        :param html_text:
        :return:
        """
        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return None

        # 用户等级
        user_levels_text = html.xpath('//div[contains(@class, "content")]//span[contains(@class, "badge-user")]/text()')
        if user_levels_text:
            self.user_level = user_levels_text[0].strip()

        # 加入日期
        join_at_text = html.xpath('//div[contains(@class, "content")]//h4[contains(text(), "注册日期") '
                                  'or contains(text(), "註冊日期") '
                                  'or contains(text(), "Registration date")]/text()')
        if join_at_text:
            self.join_at = StringUtils.unify_datetime_str(
                join_at_text[0].replace('注册日期', '').replace('註冊日期', '').replace('Registration date', ''))

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

        size_col = 9
        seeders_col = 2
        # 搜索size列
        if html.xpath('//thead//th[contains(@class,"size")]'):
            size_col = len(html.xpath('//thead//th[contains(@class,"size")][1]/preceding-sibling::th')) + 1
        # 搜索seeders列
        if html.xpath('//thead//th[contains(@class,"seeders")]'):
            seeders_col = len(html.xpath('//thead//th[contains(@class,"seeders")]/preceding-sibling::th')) + 1

        page_seeding = 0
        page_seeding_size = 0
        page_seeding_info = []
        seeding_sizes = html.xpath(f'//tr[position()]/td[{size_col}]')
        seeding_seeders = html.xpath(f'//tr[position()]/td[{seeders_col}]')
        if seeding_sizes and seeding_seeders:
            page_seeding = len(seeding_sizes)

            for i in range(0, len(seeding_sizes)):
                size = StringUtils.num_filesize(seeding_sizes[i].xpath("string(.)").strip())
                seeders = StringUtils.str_int(seeding_seeders[i].xpath("string(.)").strip())

                page_seeding_size += size
                page_seeding_info.append([seeders, size])

        self.seeding += page_seeding
        self.seeding_size += page_seeding_size
        self.seeding_info.extend(page_seeding_info)

        # 是否存在下页数据
        next_page = None
        next_pages = html.xpath('//ul[@class="pagination"]/li[contains(@class,"active")]/following-sibling::li')
        if next_pages and len(next_pages) > 1:
            page_num = next_pages[0].xpath("string(.)").strip()
            if page_num.isdigit():
                next_page = f"{self._torrent_seeding_page}&page={page_num}"

        return next_page

    def _parse_user_traffic_info(self, html_text: str):
        html_text = self._prepare_html_text(html_text)
        upload_match = re.search(r"[^总]上[传傳]量?[:：_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+[KMGTPI]*B)", html_text,
                                 re.IGNORECASE)
        self.upload = StringUtils.num_filesize(upload_match.group(1).strip()) if upload_match else 0
        download_match = re.search(r"[^总子影力]下[载載]量?[:：_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+[KMGTPI]*B)", html_text,
                                   re.IGNORECASE)
        self.download = StringUtils.num_filesize(download_match.group(1).strip()) if download_match else 0
        ratio_match = re.search(r"分享率[:：_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+)", html_text)
        self.ratio = StringUtils.str_float(ratio_match.group(1)) if (
                ratio_match and ratio_match.group(1).strip()) else 0.0

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        return None

    def _parse_message_content(self, html_text):
        return None, None, None
