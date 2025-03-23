# -*- coding: utf-8 -*-
import json
import re
from typing import Optional

from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class TNodeSiteUserInfo(SiteParserBase):
    schema = SiteSchema.TNode

    def _parse_site_page(self, html_text: str):
        html_text = self._prepare_html_text(html_text)

        # <meta name="x-csrf-token" content="fd169876a7b4846f3a7a16fcd5cccf8d">
        csrf_token = re.search(r'<meta name="x-csrf-token" content="(.+?)">', html_text)
        if csrf_token:
            self._addition_headers = {'X-CSRF-TOKEN': csrf_token.group(1)}
            self._user_detail_page = "api/user/getMainInfo"
            self._torrent_seeding_page = "api/user/listTorrentActivity?id=&type=seeding&page=1&size=20000"

    def _parse_logged_in(self, html_text):
        """
        判断是否登录成功, 通过判断是否存在用户信息
        暂时跳过检测，待后续优化
        :param html_text:
        :return:
        """
        return True

    def _parse_user_base_info(self, html_text: str):
        self.username = self.userid

    def _parse_user_traffic_info(self, html_text: str):
        pass

    def _parse_user_detail_info(self, html_text: str):
        try:
            detail = json.loads(html_text)
        except json.JSONDecodeError:
            return
        if detail.get("status") != 200:
            return

        user_info = detail.get("data", {})
        self.userid = user_info.get("id")
        self.username = user_info.get("username")
        self.user_level = user_info.get("class", {}).get("name")
        self.join_at = user_info.get("regTime", 0)
        self.join_at = StringUtils.unify_datetime_str(str(self.join_at))

        self.upload = user_info.get("upload")
        self.download = user_info.get("download")
        self.ratio = 0 if self.download <= 0 else round(self.upload / self.download, 3)
        self.bonus = user_info.get("bonus")

        self.message_unread = user_info.get("unreadAdmin", 0) + user_info.get("unreadInbox", 0) + user_info.get(
            "unreadSystem", 0)
        pass

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: Optional[bool] = False) -> Optional[str]:
        """
        解析用户做种信息
        """
        seeding_info = json.loads(html_text)
        if seeding_info.get("status") != 200:
            return None

        torrents = seeding_info.get("data", {}).get("torrents", [])

        page_seeding_size = 0
        page_seeding_info = []
        for torrent in torrents:
            size = torrent.get("size", 0)
            seeders = torrent.get("seeding", 0)

            page_seeding_size += size
            page_seeding_info.append([seeders, size])

        self.seeding += len(torrents)
        self.seeding_size += page_seeding_size
        self.seeding_info.extend(page_seeding_info)

        # 是否存在下页数据
        next_page = None

        return next_page

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        return None

    def _parse_message_content(self, html_text):
        """
        系统信息 api/message/listSystem?page=1&size=20
        收件箱信息 api/message/listInbox?page=1&size=20
        管理员信息 api/message/listAdmin?page=1&size=20
        :param html_text:
        :return:
        """
        return None, None, None
