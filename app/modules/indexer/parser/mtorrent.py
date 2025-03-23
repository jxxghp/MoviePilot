# -*- coding: utf-8 -*-
import json
from typing import Optional, Tuple
from urllib.parse import urljoin

from app.log import logger
from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class MTorrentSiteUserInfo(SiteParserBase):
    schema = SiteSchema.MTorrent
    request_mode = "apikey"

    # 用户级别字典
    MTeam_sysRoleList = {
        "1": "User",
        "2": "Power User",
        "3": "Elite User",
        "4": "Crazy User",
        "5": "Insane User",
        "6": "Veteran User",
        "7": "Extreme User",
        "8": "Ultimate User",
        "9": "Nexus Master",
        "10": "VIP",
        "11": "Retiree",
        "12": "Uploader",
        "13": "Moderator",
        "14": "Administrator",
        "15": "Sysop",
        "16": "Staff",
        "17": "Offer memberStaff",
        "18": "Bet memberStaff",
    }

    def _parse_site_page(self, html_text: str):
        """
        获取站点页面地址
        """
        # 更换api地址
        self._base_url = f"https://api.{StringUtils.get_url_domain(self._base_url)}"
        self._user_traffic_page = None
        self._user_detail_page = None
        self._user_basic_page = "api/member/profile"
        self._user_basic_params = {
            "uid": self.userid
        }
        self._sys_mail_unread_page = None
        self._user_mail_unread_page = "api/msg/search"
        self._mail_unread_params = {
            "keyword": "",
            "box": "-2",
            "type": "pageNumber",
            "pageSize": 100
        }
        self._torrent_seeding_page = "api/member/getUserTorrentList"
        self._torrent_seeding_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*"
        }
        self._addition_headers = {
            "x-api-key": self.apikey,
        }

    def _parse_logged_in(self, html_text):
        """
        判断是否登录成功, 通过判断是否存在用户信息
        暂时跳过检测，待后续优化
        :param html_text:
        :return:
        """
        return True

    def _parse_user_base_info(self, html_text: str):
        """
        解析用户基本信息，这里把_parse_user_traffic_info和_parse_user_detail_info合并到这里
        """
        if not html_text:
            return None
        detail = json.loads(html_text)
        if not detail or detail.get("code") != "0":
            return
        user_info = detail.get("data", {})
        self.userid = user_info.get("id")
        self.username = user_info.get("username")
        self.user_level = self.MTeam_sysRoleList.get(user_info.get("role") or "1")
        self.join_at = user_info.get("memberStatus", {}).get("createdDate")

        self.upload = int(user_info.get("memberCount", {}).get("uploaded") or '0')
        self.download = int(user_info.get("memberCount", {}).get("downloaded") or '0')
        self.ratio = user_info.get("memberCount", {}).get("shareRate") or 0
        self.bonus = user_info.get("memberCount", {}).get("bonus") or 0
        self.message_read_force = True
        self._torrent_seeding_params = {
            "pageNumber": 1,
            "pageSize": 200,
            "type": "SEEDING",
            "userid": self.userid
        }

    def _parse_user_traffic_info(self, html_text: str):
        """
        解析用户流量信息
        """
        pass

    def _parse_user_detail_info(self, html_text: str):
        """
        解析用户详细信息
        """
        pass

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: Optional[bool] = False) -> Optional[str]:
        """
        解析用户做种信息
        """
        if not html_text:
            return None
        seeding_info = json.loads(html_text)
        if not seeding_info or seeding_info.get("code") != "0":
            return None
        torrents = seeding_info.get("data", {}).get("data", [])
        page_seeding_size = 0
        page_seeding_info = []
        for info in torrents:
            torrent = info.get("torrent", {})
            size = int(torrent.get("size") or '0')
            seeders = int(torrent.get("source") or '0')
            page_seeding_size += size
            page_seeding_info.append([seeders, size])
        self.seeding += len(torrents)
        self.seeding_size += page_seeding_size
        self.seeding_info.extend(page_seeding_info)

        # 查询总做种数
        seeder_count = 0
        try:
            result = self._get_page_content(
                url=urljoin(self._base_url, "api/tracker/myPeerStatus"),
                params={"uid": self.userid},
            )
            if result:
                seeder_info = json.loads(result)
                seeder_count = int(seeder_info.get("data", {}).get("seeder") or 0)
        except Exception as e:
            logger.error(f"获取做种数失败: {str(e)}")
        if not seeder_count:
            return None
        if self.seeding >= seeder_count:
            return None
        # 还有下一页
        self._torrent_seeding_params["pageNumber"] += 1
        return ""

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        """
        解析未读消息链接，这里直接读出详情
        """
        if not html_text:
            return None
        messages_info = json.loads(html_text)
        if not messages_info or messages_info.get("code") != "0":
            return None
        messages = messages_info.get("data", {}).get("data", [])
        for message in messages:
            if not message.get("unread"):
                continue
            head = message.get("title")
            date = message.get("createdDate")
            content = message.get("context")
            if head and date and content:
                self.message_unread_contents.append((head, date, content))
                # 设置已读
                self._get_page_content(
                    url=urljoin(self._base_url, f"api/msg/markRead"),
                    params={"msgId": message.get("id")}
                )
        # 是否存在下页数据
        return None

    def _parse_message_content(self, html_text) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        解析消息内容
        """
        pass
