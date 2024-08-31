# -*- coding: utf-8 -*-
import re
from typing import Optional

from lxml import etree

from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class DiscuzUserInfo(SiteParserBase):
    schema = SiteSchema.DiscuzX

    def _parse_user_base_info(self, html_text: str):
        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)

        user_info = html.xpath('//a[contains(@href, "&uid=")]')
        if user_info:
            user_id_match = re.search(r"&uid=(\d+)", user_info[0].attrib['href'])
            if user_id_match and user_id_match.group().strip():
                self.userid = user_id_match.group(1)
                self._torrent_seeding_page = f"forum.php?&mod=torrents&cat_5up=on"
                self._user_detail_page = user_info[0].attrib['href']
                self.username = user_info[0].text.strip()

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
        user_levels_text = html.xpath('//a[contains(@href, "usergroup")]/text()')
        if user_levels_text:
            self.user_level = user_levels_text[-1].strip()

        # 加入日期
        join_at_text = html.xpath('//li[em[text()="注册时间"]]/text()')
        if join_at_text:
            self.join_at = StringUtils.unify_datetime_str(join_at_text[0].strip())

        # 分享率
        ratio_text = html.xpath('//li[contains(.//text(), "分享率")]//text()')
        if ratio_text:
            ratio_match = re.search(r"\(([\d,.]+)\)", ratio_text[0])
            if ratio_match and ratio_match.group(1).strip():
                self.bonus = StringUtils.str_float(ratio_match.group(1))

        # 积分
        bouns_text = html.xpath('//li[em[text()="积分"]]/text()')
        if bouns_text:
            self.bonus = StringUtils.str_float(bouns_text[0].strip())

        # 上传
        upload_text = html.xpath('//li[em[contains(text(),"上传量")]]/text()')
        if upload_text:
            self.upload = StringUtils.num_filesize(upload_text[0].strip().split('/')[-1])

        # 下载
        download_text = html.xpath('//li[em[contains(text(),"下载量")]]/text()')
        if download_text:
            self.download = StringUtils.num_filesize(download_text[0].strip().split('/')[-1])

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False) -> Optional[str]:
        """
        做种相关信息
        :param html_text:
        :param multi_page: 是否多页数据
        :return: 下页地址
        """
        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return None

        size_col = 3
        seeders_col = 4
        # 搜索size列
        if html.xpath('//tr[position()=1]/td[.//img[@class="size"] and .//img[@alt="size"]]'):
            size_col = len(html.xpath('//tr[position()=1]/td[.//img[@class="size"] '
                                      'and .//img[@alt="size"]]/preceding-sibling::td')) + 1
        # 搜索seeders列
        if html.xpath('//tr[position()=1]/td[.//img[@class="seeders"] and .//img[@alt="seeders"]]'):
            seeders_col = len(html.xpath('//tr[position()=1]/td[.//img[@class="seeders"] '
                                         'and .//img[@alt="seeders"]]/preceding-sibling::td')) + 1

        page_seeding = 0
        page_seeding_size = 0
        page_seeding_info = []
        seeding_sizes = html.xpath(f'//tr[position()>1]/td[{size_col}]')
        seeding_seeders = html.xpath(f'//tr[position()>1]/td[{seeders_col}]//text()')
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
        next_page_text = html.xpath('//a[contains(.//text(), "下一页") or contains(.//text(), "下一頁")]/@href')
        if next_page_text:
            next_page = next_page_text[-1].strip()

        return next_page

    def _parse_user_traffic_info(self, html_text: str):
        pass

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        return None

    def _parse_message_content(self, html_text):
        return None, None, None
