# -*- coding: utf-8 -*-
import re

from lxml import etree

from app.modules.indexer.parser import SiteSchema
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo


class NexusProjectSiteUserInfo(NexusPhpSiteUserInfo):
    schema = SiteSchema.NexusProject

    def _parse_site_page(self, html_text: str):
        html_text = self._prepare_html_text(html_text)

        user_detail = re.search(r"userdetails\.php\?id=(\d+)", html_text)
        if user_detail and user_detail.group().strip():
            self._user_detail_page = user_detail.group().strip().lstrip('/')
            self.userid = user_detail.group(1)
        else:
            # 兼容部分 NexusProject 变种站点(如 star-space.net)的
            # p_user/user_detail.php?uid= 用户定位格式
            user_detail = re.search(r"user_detail\.php\?uid=(\d+)", html_text)
            if user_detail and user_detail.group().strip():
                self._user_detail_page = user_detail.group().strip().lstrip('/')
                self.userid = user_detail.group(1)
                uname = re.search(
                    r"user_detail\.php\?uid=\d+[^>]*?>\s*<[^>]*>([^<]+)<", html_text)
                if uname:
                    self.username = uname.group(1).strip()

        self._torrent_seeding_page = f"viewusertorrents.php?id={self.userid}&show=seeding"

    def _parse_user_traffic_info(self, html_text):
        # 兼容部分 NexusProject 变种站点(如 star-space.net)以
        # span#user_info / span#user_info_no_hover 文本展示流量:
        # "上传：445.11 G" / "下载：29.61 G"(单位无 B 后缀)
        try:
            html = etree.HTML(html_text)
            if html is not None:
                body_text = " ".join(html.xpath(
                    '//span[@id="user_info"]//text() | //span[@id="user_info_no_hover"]//text()'))
                size_match = re.search(r"上传[：:]\s*([\d.,]+)\s*([GMKT]?i?B?)", body_text, re.I)
                if size_match:
                    self.upload = self.num_filesize(
                        f"{size_match.group(1).replace(',', '')} {size_match.group(2).upper()}B")
                size_match = re.search(r"下载[：:]\s*([\d.,]+)\s*([GMKT]?i?B?)", body_text, re.I)
                if size_match:
                    self.download = self.num_filesize(
                        f"{size_match.group(1).replace(',', '')} {size_match.group(2).upper()}B")
                if self.upload and self.download:
                    self.ratio = round(self.upload / self.download, 3)
                if self.upload or self.download:
                    return
        except Exception:
            pass
        super()._parse_user_traffic_info(html_text)
