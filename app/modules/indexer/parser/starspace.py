"""
解析星空 FireFly 页面的用户身份、流量、资料和做种列表。
"""

import re
from typing import Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from lxml import etree

from app.foundation import temporal as time_tools
from app.foundation import text as text_tools
from app.modules.indexer.parser import SitePage, SiteSchema
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.runtime.log import logger

# 页面等级图片的 class1 至 class11 对应的站点等级名称。
_USER_LEVELS = (
    "User", "Power User", "Elite User", "Crazy User", "Insane User", "Veteran User",
    "Extreme User", "Ultimate User", "Master User", "Star User", "God User",
)
_SEEDING_PATH = "/p_torrent/torrent_user.php"


class StarSpaceSiteUserInfo(NexusPhpSiteUserInfo):
    """
    复用用户数据请求生命周期，独立解释星空的带前缀页面协议。

    :ivar _visited_seeding_pages: 本轮已请求或已安排的做种页面，防止循环分页
    """

    schema = SiteSchema.StarSpace
    allow_schema_fallback = False

    def _parse_site_page(self, html_text: SitePage) -> None:
        """
        从本站个人资料链接读取用户身份，并设置详情和做种入口。

        Cookie 中的 ffid 只作为认证凭据，不能代替页面确认登录后的用户身份。
        """
        self.userid = None
        self._user_detail_page = None
        self._torrent_seeding_page = None
        self._user_traffic_page = None
        # 星空的消息协议独立，不能请求 NexusPHP 的默认消息页。
        self._user_mail_unread_page = None
        self._sys_mail_unread_page = None
        self._visited_seeding_pages: set[str] = set()

        html = etree.HTML(self._prepare_html_text(html_text))
        if html is None:
            return
        for link in html.xpath('//a[contains(@href, "user_detail.php")]'):
            target = urlsplit(urljoin(self._site_url, link.get("href", "")))
            userids = parse_qs(target.query).get("uid", [])
            if (target.scheme != urlsplit(self._base_url).scheme
                    or target.netloc != urlsplit(self._base_url).netloc
                    or target.path != "/p_user/user_detail.php"
                    or len(userids) != 1 or not userids[0].isdigit() or int(userids[0]) <= 0):
                continue
            self.userid = userids[0]
            self.username = link.xpath("string(.)").strip() or None
            self._user_detail_page = f"p_user/user_detail.php?uid={self.userid}"
            self._torrent_seeding_page = f"p_torrent/torrent_user.php?pop=8&uid={self.userid}"
            return

    def _parse_user_base_info(self, html_text: SitePage) -> None:
        """
        从已登录首页读取流量及当前账号的统计，避免重复请求首页。
        """
        self._parse_user_traffic_info(self._prepare_html_text(html_text))

    @staticmethod
    def _parse_size(value: str) -> Optional[int]:
        """
        解释星空省略 B 后缀的容量，缺失或无效值保持未知。

        :param value: 页面显示的容量，支持 G、GB 和 GiB 等单位
        :return: 容量的整数字节数；格式无效时为 None
        """
        match = re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*([KMGTPI](?:i?B)?|B)?\s*", value, re.I)
        if not match:
            return None
        unit = (match.group(2) or "B").upper()
        return StarSpaceSiteUserInfo.num_filesize(f"{match.group(1)} {unit if unit.endswith('B') else unit + 'B'}")

    def _parse_user_traffic_info(self, html_text: str) -> None:
        """
        从账号概览读取上传、下载、魔力和明确给出的做种、下载数量。
        """
        html = etree.HTML(html_text)
        if html is None:
            return
        overview = " ".join(html.xpath(
            '//span[@id="user_info" or @id="user_info_no_hover"]//text()'
        ))
        for label, attribute in (("上传", "upload"), ("下载", "download")):
            match = re.search(rf"{label}\s*[：:]\s*([\d,.]+\s*(?:[KMGTPI](?:i?B)?|B)?)", overview, re.I)
            value = self._parse_size(match.group(1)) if match else None
            if value is not None:
                setattr(self, attribute, value)
        self.ratio = round(self.upload / self.download, 3) if self.download else 0.0

        for link in html.xpath('//a[contains(@href, "torrent_user.php")]'):
            query = parse_qs(urlsplit(link.get("href", "")).query)
            if query.get("uid", [str(self.userid)]) != [str(self.userid)]:
                continue
            count = re.search(r"[\d,]+", link.xpath("string(.)"))
            if count and query.get("pop") == ["8"]:
                self._summary_seeding = self.seeding = int(count.group().replace(",", ""))
            elif count and query.get("pop") == ["9"]:
                self.leeching = int(count.group().replace(",", ""))
        bonus = html.xpath('//a[contains(@href, "p_bonus/bonus_hour.php")]')
        if bonus:
            match = re.search(r"[\d,]+(?:\.\d+)?", bonus[0].xpath("string(.)"))
            if match:
                self.bonus = text_tools.parse_float(match.group().replace(",", ""))

    @staticmethod
    def _profile_field(html: etree._Element, label: str) -> Optional[etree._Element]:
        """
        按资料表字段名读取相邻值单元格，避免扫描整页误取其他用户数据。

        :param html: 当前账号的资料页节点
        :param label: 资料表中的完整字段名
        :return: 字段对应的值单元格；页面没有该字段时为 None
        """
        cells = html.xpath('//tr/td[normalize-space(.)=$label]/following-sibling::td[1]', label=label)
        return cells[0] if cells else None

    def _parse_user_detail_info(self, html_text: str) -> None:
        """
        读取资料表的等级、注册时间、传输统计及魔力，保留首页已取得的字段。
        """
        html = etree.HTML(html_text)
        if html is None:
            return
        self._parse_user_traffic_info(html_text)
        for label, attribute in (("用户名称", "username"), ("加入日期", "join_at"), ("魔力", "bonus")):
            cell = self._profile_field(html, label)
            value = cell.xpath("string(.)").strip() if cell is not None else ""
            if not value:
                continue
            if attribute == "join_at":
                self.join_at = time_tools.normalize_datetime(value.split(" (")[0])
            elif attribute == "bonus":
                self.bonus = text_tools.parse_float(value.replace(",", ""))
            else:
                self.username = value
        level = self._profile_field(html, "用户等级")
        if level is not None:
            markers = level.xpath('.//img/@alt | .//img/@src')
            match = re.search(r"class(\d+)", " ".join(markers))
            if match and 1 <= int(match.group(1)) <= len(_USER_LEVELS):
                self.user_level = _USER_LEVELS[int(match.group(1)) - 1]
        transfer = self._profile_field(html, "传输")
        if transfer is not None:
            cells = transfer.xpath('.//tr[@id="tr_item_min"]/td')
            if len(cells) >= 4:
                for index, attribute in ((0, "upload"), (2, "download")):
                    size = self._parse_size(cells[index].xpath("string(.)").strip())
                    if size is not None:
                        setattr(self, attribute, size)
                ratio = cells[3].xpath("string(.)").strip()
                self.ratio = (text_tools.parse_float(ratio.replace(",", ""))
                              if re.fullmatch(r"[\d,]+(?:\.\d+)?", ratio)
                              else round(self.upload / self.download, 3) if self.download else 0.0)

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: Optional[bool] = False) -> Optional[str]:
        """
        累加做种列表中的实际容量和可见做种人数，并跟随本站的下一页入口。

        :param html_text: 当前做种列表页
        :param multi_page: 是否为后续页面
        :return: 尚未请求的下一页地址；无后续页时为 None
        """
        html = etree.HTML(html_text)
        if html is None:
            return None
        if not multi_page:
            self._visited_seeding_pages = {urljoin(self._base_url, self._torrent_seeding_page or "")}
        rows = html.xpath('//table[@id="table_tm"]//tr[@id="tr_list" or starts-with(@class, "tm_tr")]')
        for row in rows:
            cells = row.xpath("./td")
            if len(cells) < 4:
                continue
            size = self._parse_size(cells[3].xpath("string(.)").strip())
            if size is None:
                logger.warning(f"{self._site_name} 做种列表存在无法解析的体积，统计可能不完整")
                continue
            self.seeding += 1
            self.seeding_size += size
            seeders = cells[4].xpath("string(.)").strip() if len(cells) > 4 else ""
            if re.fullmatch(r"[\d,]+", seeders):
                self.seeding_info.append([int(seeders.replace(",", "")), size])
        return self._next_seeding_page(html) if rows else None

    def _next_seeding_page(self, html: etree._Element) -> Optional[str]:
        """
        保留当前账号和做种筛选，拒绝跨站、跨账号或循环的分页链接。

        :param html: 当前做种列表页节点
        :return: 保留账号筛选的完整下一页地址；没有有效下一页时为 None
        """
        links: list[str] = html.xpath('//a[@rel="next" or contains(., "下一页") or contains(., "下一頁")]/@href')
        for link in links:
            target = urlsplit(urljoin(urljoin(self._base_url, self._torrent_seeding_page or ""), link))
            query = dict(parse_qsl(target.query))
            if (target.scheme != urlsplit(self._base_url).scheme
                    or target.netloc != urlsplit(self._base_url).netloc or target.path != _SEEDING_PATH
                    or query.get("pop", "8") != "8" or query.get("uid", str(self.userid)) != str(self.userid)):
                continue
            query.update(pop="8", uid=str(self.userid))
            next_page = urlunsplit(target._replace(query=urlencode(sorted(query.items())), fragment=""))
            if next_page not in self._visited_seeding_pages:
                self._visited_seeding_pages.add(next_page)
                return next_page
        return None
