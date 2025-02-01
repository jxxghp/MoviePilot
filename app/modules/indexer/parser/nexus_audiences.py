# -*- coding: utf-8 -*-
from urllib.parse import urljoin

from lxml import etree

from app.modules.indexer.parser import SiteSchema
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.utils.string import StringUtils


class NexusAudiencesSiteUserInfo(NexusPhpSiteUserInfo):
    schema = SiteSchema.NexusAudiences

    def _parse_seeding_pages(self):
        if not self._torrent_seeding_page:
            return
        self._torrent_seeding_headers = {"Referer": urljoin(self._base_url, self._user_detail_page)}
        html_text = self._get_page_content(
            url=urljoin(self._base_url, self._torrent_seeding_page),
            params=self._torrent_seeding_params,
            headers=self._torrent_seeding_headers
        )
        if not html_text:
            return
        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return
        total_row = html.xpath('//table[@class="table table-bordered"]//tr[td[1][normalize-space()="Total"]]')
        if not total_row:
            return
        seeding_count = total_row[0].xpath('./td[2]/text()')
        seeding_size = total_row[0].xpath('./td[3]/text()')
        self.seeding = StringUtils.str_int(seeding_count[0]) if seeding_count else 0
        self.seeding_size = StringUtils.num_filesize(seeding_size[0].strip()) if seeding_size else 0
