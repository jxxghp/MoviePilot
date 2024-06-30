# -*- coding: utf-8 -*-
from urllib.parse import urljoin

from app.modules.indexer.parser import  SiteSchema, SITE_BASE_ORDER
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo


class NexusAudiencesSiteUserInfo(NexusPhpSiteUserInfo):
    schema = SiteSchema.NexusAudiences
    order = SITE_BASE_ORDER + 5

    @classmethod
    def match(cls, html_text: str) -> bool:
        return 'audiences.me' in html_text

    def _parse_site_page(self, html_text: str):
        super()._parse_site_page(html_text)
        self._torrent_seeding_page = f"usertorrentlist.php?userid={self.userid}&type=seeding"

    def _parse_seeding_pages(self):
        self._torrent_seeding_headers = {"Referer": urljoin(self._base_url, self._user_detail_page)}
        super()._parse_seeding_pages()
