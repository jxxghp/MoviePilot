# -*- coding: utf-8 -*-
import json
import re
from urllib.parse import urljoin

from lxml import etree

from app.runtime.log import logger
from app.modules.indexer.parser import SiteSchema
from app.modules.indexer.parser.nexus_php import NexusPhpSiteUserInfo
from app.foundation import size as size_tools
from app.foundation import text as text_tools
from app.foundation.dom import DomUtils


class NexusAudiencesSiteUserInfo(NexusPhpSiteUserInfo):
    schema = SiteSchema.NexusAudiences
    __UNKNOWN_UNREAD_COUNT = 99999

    def __init__(self, *args, **kwargs):
        """
        初始化 Audiences 未读私信列表地址，第一页不能携带 page 参数。
        """
        super().__init__(*args, **kwargs)
        self._user_mail_unread_page = self.__build_unread_mailbox_page(box=1)
        self._sys_mail_unread_page = None
        self.__next_mail_page = 1
        self.__seen_unread_message_links = set()
        self.__message_list_previews = {}

    def _parse_message_unread(self, html_text):
        """
        解析 Audiences 新版顶部用户栏中的未读消息数。
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                super()._parse_message_unread(html_text)
                return

            message_tools = html.xpath(
                '//a[contains(@class, "site-userbar__compact-tool") and contains(@href, "messages.php") '
                'and (contains(@class, "site-userbar__compact-tool--has-unread") '
                'or .//*[contains(@class, "site-userbar__compact-tool-badge--unread")])]'
                '|//a[contains(@href, "messages.php") '
                'and (contains(@title, "收件箱") or contains(@aria-label, "收件箱"))]'
            )
            for message_link in message_tools:
                unread = self.__parse_inbox_unread(message_link)
                if unread is not None:
                    self.message_unread = unread
                    return
            if message_tools:
                return
        finally:
            if html is not None:
                del html

        super()._parse_message_unread(html_text)

    def _parse_message_unread_links(self, html_text: str, msg_links: list):
        """
        解析 Audiences 未读消息链接。
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return None

            message_links = self.__parse_table_unread_message_links(html)
            message_links.extend(self.__parse_pm_item_unread_message_links(html))
            new_message_links = self.__filter_new_message_links(message_links)
            if message_links and not new_message_links:
                logger.warn(f"{self._site_name} 未读消息页只发现重复消息链接，停止后续翻页")
            msg_links.extend(new_message_links)
            next_page = self.__build_next_unread_mailbox_page(
                self.__should_fetch_next_unread_page(new_message_links)
            )
        finally:
            if html is not None:
                del html

        return next_page

    def _parse_message_content(self, html_text):
        """
        解析 Audiences 新版短消息详情页。
        """
        html = etree.HTML(html_text)
        try:
            if DomUtils.has_child_elements(html):
                head = self.__extract_first_text(
                    html,
                    '//*[contains(concat(" ", normalize-space(@class), " "), " pm-hero__title ")]'
                )
                date = self.__extract_pm_view_meta(html, "日期")
                content = self.__extract_first_text(
                    html,
                    '//*[contains(concat(" ", normalize-space(@class), " "), " pm-view__body ")]'
                )
                if not self.__is_empty_message_content(head, date, content):
                    return head, date, content
        finally:
            if html is not None:
                del html

        return super()._parse_message_content(html_text)

    def _pase_unread_msgs(self):
        """
        解析 Audiences 未读消息，避免异常分页重复通知和空详情通知。
        """
        self.__reset_unread_message_parse_state()
        unread_msg_links = []
        if self.message_unread > 0 or self.message_read_force:
            next_page = self.__parse_unread_message_list_page(
                link=self._user_mail_unread_page,
                unread_msg_links=unread_msg_links
            )
            while next_page:
                next_page = self.__parse_unread_message_list_page(
                    link=next_page,
                    unread_msg_links=unread_msg_links
                )
        if self.message_unread == self.__UNKNOWN_UNREAD_COUNT:
            self.message_unread = len(unread_msg_links)
        elif unread_msg_links and not self.message_unread:
            self.message_unread = len(unread_msg_links)
        for msg_link in unread_msg_links:
            logger.debug(f"{self._site_name} 信息链接 {msg_link}")
            head, date, content = self._parse_message_content(
                self._get_page_content(
                    urljoin(self._base_url, msg_link),
                    params=self._mail_content_params,
                    headers=self._mail_content_headers
                )
            )
            head, date, content = self.__fill_empty_message_content_from_list(msg_link, head, date, content)
            logger.debug(f"{self._site_name} 标题 {head} 时间 {date} 内容 {content}")
            if self.__is_empty_message_content(head, date, content):
                logger.warn(f"{self._site_name} 信息链接 {msg_link} 解析结果为空，跳过消息通知")
                continue
            self.message_unread_contents.append((head, date, content))

    def __parse_unread_message_list_page(self, link: str, unread_msg_links: list):
        """
        读取并解析一页 Audiences 未读消息列表。
        """
        if not link:
            return None
        return self._parse_message_unread_links(
            self._get_page_content(
                url=urljoin(self._base_url, link),
                params=self._mail_unread_params,
                headers=self._mail_unread_headers
            ),
            unread_msg_links
        )

    def __reset_unread_message_parse_state(self):
        """
        重置 Audiences 未读消息分页状态，避免复用解析器时沿用上次页码和去重集合。
        """
        self.__next_mail_page = 1
        self.__seen_unread_message_links.clear()
        self.__message_list_previews.clear()

    def __filter_new_message_links(self, message_links: list) -> list:
        """
        过滤 Audiences 异常分页重复返回的消息详情链接。
        """
        new_message_links = []
        for message_link in message_links:
            message_link_key = urljoin(self._base_url, message_link)
            if message_link_key in self.__seen_unread_message_links:
                continue
            self.__seen_unread_message_links.add(message_link_key)
            new_message_links.append(message_link)
        return new_message_links

    @staticmethod
    def __parse_table_unread_message_links(html) -> list:
        """
        解析 Audiences 旧版表格消息列表中的未读消息链接。
        """
        return html.xpath(
            '//tr[.//img[contains(concat(" ", normalize-space(@class), " "), " unreadpm ") '
            'or @alt="Unread" or @title="未读"]]/td/a[contains(@href, "viewmessage")]/@href'
        )

    def __parse_pm_item_unread_message_links(self, html) -> list:
        """
        解析 Audiences 新版 pm-item 私信列表中的未读消息链接。
        """
        message_links = []
        unread_rows = html.xpath(
            '//*[contains(concat(" ", normalize-space(@class), " "), " pm-item-row ") '
            'and contains(concat(" ", normalize-space(@class), " "), " is-unread ")]'
        )
        if not unread_rows:
            unread_rows = html.xpath(
                '//*[contains(concat(" ", normalize-space(@class), " "), " pm-item-row ") '
                'and .//*[contains(concat(" ", normalize-space(@class), " "), " pm-item__status--unread ") '
                'or @title="未读"]]'
            )

        for row in unread_rows:
            row_links = row.xpath('.//a[contains(@href, "viewmessage")]/@href')
            if not row_links:
                continue
            message_link = row_links[0].strip()
            if not message_link:
                continue
            message_links.append(message_link)
            self.__cache_pm_item_preview(message_link, row)
        return message_links

    def __cache_pm_item_preview(self, message_link: str, row):
        """
        缓存新版列表页预览，用于详情页结构变化时兜底生成站点消息。
        """
        head = self.__extract_pm_item_text(
            row,
            './/*[contains(concat(" ", normalize-space(@class), " "), " pm-item__subject ")]'
        )
        date = self.__extract_pm_item_text(
            row,
            './/*[contains(concat(" ", normalize-space(@class), " "), " pm-item__time ")]'
        )
        content = self.__extract_pm_item_text(
            row,
            './/*[contains(concat(" ", normalize-space(@class), " "), " pm-item__preview ")]'
        )
        self.__message_list_previews[urljoin(self._base_url, message_link)] = (head, date, content)

    @staticmethod
    def __extract_pm_item_text(row, xpath: str):
        """
        提取新版私信列表节点文本并规整空白字符。
        """
        nodes = row.xpath(xpath)
        if not nodes:
            return None
        text = nodes[0].xpath("string(.)")
        text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
        return text or None

    @staticmethod
    def __extract_first_text(html, xpath: str):
        """
        提取第一个匹配节点的规整文本。
        """
        nodes = html.xpath(xpath)
        if not nodes:
            return None
        return NexusAudiencesSiteUserInfo.__normalize_text(nodes[0].xpath("string(.)"))

    @staticmethod
    def __extract_pm_view_meta(html, label: str):
        """
        按标签提取 Audiences 新版短消息详情页中的元信息。
        """
        values = html.xpath(
            '//*[contains(concat(" ", normalize-space(@class), " "), " pm-view__meta ") '
            f'and .//*[contains(concat(" ", normalize-space(@class), " "), " pm-view__label ") '
            f'and normalize-space()="{label}"]]'
            '//*[contains(concat(" ", normalize-space(@class), " "), " pm-view__value ")]'
        )
        if not values:
            return None
        return NexusAudiencesSiteUserInfo.__normalize_text(values[0].xpath("string(.)"))

    @staticmethod
    def __normalize_text(text: str):
        """
        规整 Audiences 新版消息页文本空白字符。
        """
        if not text:
            return None
        text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
        return text or None

    def __fill_empty_message_content_from_list(self, msg_link: str, head, date, content):
        """
        使用列表页预览填补详情页解析不到的字段。
        """
        preview = self.__message_list_previews.get(urljoin(self._base_url, msg_link))
        if not preview:
            return head, date, content
        preview_head, preview_date, preview_content = preview
        return head or preview_head, date or preview_date, content or preview_content

    def __should_fetch_next_unread_page(self, new_message_links: list) -> bool:
        """
        判断是否还需要继续请求 Audiences 下一页未读消息列表。
        """
        if not new_message_links:
            return False
        return not self.__has_reached_expected_unread_count()

    def __has_reached_expected_unread_count(self) -> bool:
        """
        已达到 Audiences 顶部栏给出的未读数时停止翻页。
        """
        return not self.message_read_force \
            and self.message_unread > 0 \
            and self.message_unread != self.__UNKNOWN_UNREAD_COUNT \
            and len(self.__seen_unread_message_links) >= self.message_unread

    @staticmethod
    def __is_empty_message_content(head, date, content) -> bool:
        """
        判断消息详情是否完全为空，避免把解析失败页包装成 None 通知。
        """
        return not any(str(item).strip() for item in (head, date, content) if item is not None)

    @classmethod
    def __build_unread_mailbox_page(cls, box: int) -> str:
        """
        构造 Audiences 未读私信列表首页地址。
        """
        return f"messages.php?action=viewmailbox&box={box}&unread=yes"

    def __build_next_unread_mailbox_page(self, has_unread: bool) -> str:
        """
        当前页存在未读消息时按 Audiences 的 page 参数规则生成下一页地址。
        """
        if not has_unread:
            return None

        next_page = self.__next_mail_page
        self.__next_mail_page += 1
        return f"{self._user_mail_unread_page}&page={next_page}"

    def _parse_user_traffic_info(self, html_text):
        """
        解析用户流量信息
        """
        super()._parse_user_traffic_info(html_text)
        self.__parse_userbar_info(html_text)

    def _parse_user_detail_info(self, html_text: str):
        """
        解析用户额外信息
        """
        super()._parse_user_detail_info(html_text)
        self.__parse_userbar_info(html_text)

    def __parse_userbar_info(self, html_text: str):
        """
        解析 Audiences 新版顶部用户栏，覆盖 NexusPHP 通用正则的误判。
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return

            for user_node in html.xpath('//*[@data-uploader-url or @data-uploader-stats]'):
                self.__parse_user_identity(user_node)
                self.__parse_uploader_stats(user_node.get("data-uploader-stats"))

            # data-uploader-stats 不包含分享率，需从 compact metric 的 class 中读取。
            self.__parse_compact_metric(html, "ratio", "ratio")
            self.__parse_compact_metric(html, "uploaded", "upload")
            self.__parse_compact_metric(html, "downloaded", "download")
            self.__parse_compact_metric(html, "bonus", "bonus")
            self.__parse_compact_metric(html, "active", "active")
        finally:
            if html is not None:
                del html

    def __parse_user_identity(self, user_node):
        """
        从新版用户卡属性中提取用户 ID、用户名和等级。
        """
        user_url = user_node.get("data-uploader-url") or ""
        user_detail = re.search(r"userdetails\.php\?id=(\d+)", user_url)
        if user_detail and user_detail.group(1).strip():
            self.userid = user_detail.group(1).strip()

        username = user_node.get("data-uploader-label")
        if username and username.strip():
            self.username = username.strip()

        user_level = user_node.get("data-uploader-badge")
        if user_level and user_level.strip():
            self.user_level = user_level.strip()

    def __parse_uploader_stats(self, stats_text: str):
        """
        解析 data-uploader-stats 中的结构化流量数据。
        """
        if not stats_text:
            return

        try:
            stats = json.loads(stats_text)
        except (TypeError, ValueError):
            return

        if not isinstance(stats, list):
            return

        for item in stats:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip(" ：:")
            tone = str(item.get("tone") or "").strip()
            value = str(item.get("value") or "").strip()
            self.__set_metric_value(label=label, tone=tone, value=value)

    def __parse_compact_metric(self, html, metric: str, field: str):
        """
        按 compact metric 的 class 读取新版用户栏中的单项数据。
        """
        values = html.xpath(
            f'//*[contains(concat(" ", normalize-space(@class), " "), " site-userbar__compact-metric--{metric} ")]'
            '//span[normalize-space()][last()]/text()'
        )
        if not values:
            values = html.xpath(
                f'//*[contains(concat(" ", normalize-space(@class), " "), " site-userbar__compact-metric--{metric} ")]'
                '/text()'
            )
        if values:
            self.__set_metric_value(field=field, value=values[-1].strip())

    def __set_metric_value(self, value: str, label: str = None, tone: str = None, field: str = None):
        """
        将 Audiences 用户栏指标写入通用用户数据字段。
        """
        if not value:
            return

        metric_key = field or tone or label
        if metric_key in {"uploaded", "上传量", "upload"}:
            self.upload = size_tools.parse_size(value)
        elif metric_key in {"downloaded", "下载量", "download"}:
            self.download = size_tools.parse_size(value)
        elif metric_key in {"bonus", "爆米花"}:
            self.bonus = text_tools.parse_float(value)
        elif metric_key == "ratio":
            self.ratio = text_tools.parse_float(value)
        elif metric_key in {"active", "活跃"}:
            active_match = re.search(r"↑\s*(\d+)\s*/\s*↓\s*(\d+)", value)
            if active_match:
                self.seeding = text_tools.parse_int(active_match.group(1))
                self.leeching = text_tools.parse_int(active_match.group(2))

    def __parse_inbox_unread(self, message_link):
        """
        从 Audiences 收件箱入口提取未读数。
        """
        for inbox_text in [
            message_link.get("title"),
            message_link.get("aria-label"),
        ]:
            unread = self.__extract_inbox_unread_pair(inbox_text)
            if unread is not None:
                return unread

        for inbox_text in message_link.xpath(
                './/*[contains(@class, "site-userbar__compact-tool-badge--unread")]/text()'):
            unread = self.__extract_inbox_unread_badge(inbox_text)
            if unread is not None:
                return unread

        if self.__has_inbox_unread_marker(message_link):
            return self.__UNKNOWN_UNREAD_COUNT

        return None

    @staticmethod
    def __extract_inbox_unread_pair(text: str):
        """
        从 Audiences 总数/未读数格式中提取未读数，例如 1749/172。
        """
        if not text:
            return None

        text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
        if not text:
            return None

        inbox_count = re.search(r"(?:收件箱\s*)?(\d[\d,]*)\s*/\s*(\d[\d,]*)", text)
        if inbox_count:
            return text_tools.parse_int(inbox_count.group(2))

        return None

    @staticmethod
    def __extract_inbox_unread_badge(text: str):
        """
        从明确的未读角标中提取未读数，避免把普通收件箱总数误作未读。
        """
        unread = NexusAudiencesSiteUserInfo.__extract_inbox_unread_pair(text)
        if unread is not None:
            return unread

        if not text:
            return None
        text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
        single_count = re.fullmatch(r"(\d[\d,]*)", text)
        if single_count:
            return text_tools.parse_int(single_count.group(1))
        return None

    @staticmethod
    def __has_inbox_unread_marker(message_link) -> bool:
        """
        判断收件箱入口是否只有未读状态但没有可靠数量。
        """
        link_class = message_link.get("class") or ""
        if "site-userbar__compact-tool--has-unread" in link_class:
            return True
        return bool(message_link.xpath('.//*[contains(@class, "site-userbar__compact-tool-badge--unread")]'))

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
        try:
            if not DomUtils.has_child_elements(html):
                return
            total_row = html.xpath('//table[@class="table table-bordered"]//tr[td[1][normalize-space()="Total"]]')
            if not total_row:
                return
            seeding_count = total_row[0].xpath('./td[2]/text()')
            seeding_size = total_row[0].xpath('./td[3]/text()')
            self.seeding = text_tools.parse_int(seeding_count[0]) if seeding_count else 0
            self.seeding_size = size_tools.parse_size(seeding_size[0].strip()) if seeding_size else 0
        finally:
            if html is not None:
                del html
