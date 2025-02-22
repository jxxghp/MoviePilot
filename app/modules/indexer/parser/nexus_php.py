# -*- coding: utf-8 -*-
import re
from typing import Optional

from lxml import etree

from app.log import logger
from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.utils.string import StringUtils


class NexusPhpSiteUserInfo(SiteParserBase):
    schema = SiteSchema.NexusPhp

    def _parse_site_page(self, html_text: str):
        html_text = self._prepare_html_text(html_text)

        user_detail = re.search(r"userdetails.php\?id=(\d+)", html_text)
        if user_detail and user_detail.group().strip():
            self._user_detail_page = user_detail.group().strip().lstrip('/')
            self.userid = user_detail.group(1)
            self._torrent_seeding_page = f"getusertorrentlistajax.php?userid={self.userid}&type=seeding"
        else:
            user_detail = re.search(r"(userdetails)", html_text)
            if user_detail and user_detail.group().strip():
                self._user_detail_page = user_detail.group().strip().lstrip('/')
                self.userid = None
                self._torrent_seeding_page = None

    def _parse_message_unread(self, html_text):
        """
        解析未读短消息数量
        :param html_text:
        :return:
        """
        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return

        message_labels = html.xpath('//a[@href="messages.php"]/..')
        message_labels.extend(html.xpath('//a[contains(@href, "messages.php")]/..'))
        if message_labels:
            message_text = message_labels[0].xpath("string(.)")

            logger.debug(f"{self._site_name} 消息原始信息 {message_text}")
            message_unread_match = re.findall(r"[^Date](信息箱\s*|\((?![^)]*:)|你有\xa0)(\d+)", message_text)

            if message_unread_match and len(message_unread_match[-1]) == 2:
                self.message_unread = StringUtils.str_int(message_unread_match[-1][1])
            elif message_text.isdigit():
                self.message_unread = StringUtils.str_int(message_text)

    def _parse_user_base_info(self, html_text: str):
        """
        解析用户基本信息
        """
        # 合并解析，减少额外请求调用
        self._parse_user_traffic_info(html_text)
        self._user_traffic_page = None

        self._parse_message_unread(html_text)

        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return

        ret = html.xpath(f'//a[contains(@href, "userdetails") and contains(@href, "{self.userid}")]//b//text()')
        if ret:
            self.username = str(ret[0])
            return
        ret = html.xpath(f'//a[contains(@href, "userdetails") and contains(@href, "{self.userid}")]//text()')
        if ret:
            self.username = str(ret[0])

        ret = html.xpath('//a[contains(@href, "userdetails")]//strong//text()')
        if ret:
            self.username = str(ret[0])
            return

    def _parse_user_traffic_info(self, html_text):
        """
        解析用户流量信息
        """
        html_text = self._prepare_html_text(html_text)
        upload_match = re.search(r"[^总]上[传傳]量?[:：_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+[KMGTPI]*B)", html_text,
                                 re.IGNORECASE)
        self.upload = StringUtils.num_filesize(upload_match.group(1).strip()) if upload_match else 0
        download_match = re.search(r"[^总子影力]下[载載]量?[:：_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+[KMGTPI]*B)", html_text,
                                   re.IGNORECASE)
        self.download = StringUtils.num_filesize(download_match.group(1).strip()) if download_match else 0
        ratio_match = re.search(r"分享率[:：_<>/a-zA-Z-=\"'\s#;]+([\d,.\s]+)", html_text)
        # 计算分享率
        calc_ratio = 0.0 if self.download <= 0.0 else round(self.upload / self.download, 3)
        # 优先使用页面上的分享率
        self.ratio = StringUtils.str_float(ratio_match.group(1)) if (
                ratio_match and ratio_match.group(1).strip()) else calc_ratio
        leeching_match = re.search(r"(Torrents leeching|下载中)[\u4E00-\u9FA5\D\s]+(\d+)[\s\S]+<", html_text)
        self.leeching = StringUtils.str_int(leeching_match.group(2)) if leeching_match and leeching_match.group(
            2).strip() else 0
        html = etree.HTML(html_text)
        has_ucoin, self.bonus = self._parse_ucoin(html)
        if has_ucoin:
            return
        tmps = html.xpath('//a[contains(@href,"mybonus")]/text()') if html else None
        if tmps:
            bonus_text = str(tmps[0]).strip()
            bonus_match = re.search(r"([\d,.]+)", bonus_text)
            if bonus_match and bonus_match.group(1).strip():
                self.bonus = StringUtils.str_float(bonus_match.group(1))
                return
        bonus_match = re.search(r"mybonus.[\[\]:：<>/a-zA-Z_\-=\"'\s#;.(使用魔力值豆]+\s*([\d,.]+)[<()&\s]", html_text)
        try:
            if bonus_match and bonus_match.group(1).strip():
                self.bonus = StringUtils.str_float(bonus_match.group(1))
                return
            bonus_match = re.search(r"[魔力值|\]][\[\]:：<>/a-zA-Z_\-=\"'\s#;]+\s*([\d,.]+|\"[\d,.]+\")[<>()&\s]",
                                    html_text,
                                    flags=re.S)
            if bonus_match and bonus_match.group(1).strip():
                self.bonus = StringUtils.str_float(bonus_match.group(1).strip('"'))
        except Exception as err:
            logger.error(f"{self._site_name} 解析魔力值出错, 错误信息: {str(err)}")

    @staticmethod
    def _parse_ucoin(html):
        """
        解析ucoin, 统一转换为铜币
        :param html:
        :return:
        """
        if StringUtils.is_valid_html_element(html):
            gold, silver, copper = None, None, None

            golds = html.xpath('//span[@class = "ucoin-symbol ucoin-gold"]//text()')
            if golds:
                gold = StringUtils.str_float(str(golds[-1]))
            silvers = html.xpath('//span[@class = "ucoin-symbol ucoin-silver"]//text()')
            if silvers:
                silver = StringUtils.str_float(str(silvers[-1]))
            coppers = html.xpath('//span[@class = "ucoin-symbol ucoin-copper"]//text()')
            if coppers:
                copper = StringUtils.str_float(str(coppers[-1]))
            if gold or silver or copper:
                gold = gold if gold else 0
                silver = silver if silver else 0
                copper = copper if copper else 0
                return True, gold * 100 * 100 + silver * 100 + copper
        return False, 0.0

    def _parse_user_torrent_seeding_info(self, html_text: str, multi_page: bool = False) -> Optional[str]:
        """
        做种相关信息
        :param html_text:
        :param multi_page: 是否多页数据
        :return: 下页地址
        """
        html = etree.HTML(str(html_text).replace(r'\/', '/'))
        if not StringUtils.is_valid_html_element(html):
            return None

        # 首页存在扩展链接，使用扩展链接
        seeding_url_text = html.xpath('//a[contains(@href,"torrents.php") '
                                      'and contains(@href,"seeding")]/@href')
        if multi_page is False and seeding_url_text and seeding_url_text[0].strip():
            self._torrent_seeding_page = seeding_url_text[0].strip()
            return self._torrent_seeding_page

        size_col = 3
        seeders_col = 4
        # 搜索size列
        size_col_xpath = '//tr[position()=1]/' \
                         'td[(img[@class="size"] and img[@alt="size"])' \
                         ' or (text() = "大小")' \
                         ' or (a/img[@class="size" and @alt="size"])]'
        if html.xpath(size_col_xpath):
            size_col = len(html.xpath(f'{size_col_xpath}/preceding-sibling::td')) + 1
        # 搜索seeders列
        seeders_col_xpath = '//tr[position()=1]/' \
                            'td[(img[@class="seeders"] and img[@alt="seeders"])' \
                            ' or (text() = "在做种")' \
                            ' or (a/img[@class="seeders" and @alt="seeders"])]'
        if html.xpath(seeders_col_xpath):
            seeders_col = len(html.xpath(f'{seeders_col_xpath}/preceding-sibling::td')) + 1

        page_seeding = 0
        page_seeding_size = 0
        page_seeding_info = []
        # 如果 table class="torrents"，则增加table[@class="torrents"]
        table_class = '//table[@class="torrents"]' if html.xpath('//table[@class="torrents"]') else ''
        seeding_sizes = html.xpath(f'{table_class}//tr[position()>1]/td[{size_col}]')
        seeding_seeders = html.xpath(f'{table_class}//tr[position()>1]/td[{seeders_col}]/b/a/text()')
        if not seeding_seeders:
            seeding_seeders = html.xpath(f'{table_class}//tr[position()>1]/td[{seeders_col}]//text()')
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
        next_page_text = html.xpath(
            '//a[contains(.//text(), "下一页") or contains(.//text(), "下一頁") or contains(.//text(), ">")]/@href')

        # 防止识别到详情页
        while next_page_text:
            next_page = next_page_text.pop().strip()
            if not next_page.startswith('details.php'):
                break
            next_page = None

        # fix up page url
        if next_page:
            if self.userid not in next_page:
                next_page = f'{next_page}&userid={self.userid}&type=seeding'

        return next_page

    def _parse_user_detail_info(self, html_text: str):
        """
        解析用户额外信息，加入时间，等级
        :param html_text:
        :return:
        """
        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return

        self._get_user_level(html)

        self._fixup_traffic_info(html)

        # 加入日期
        join_at_text = html.xpath(
            '//tr/td[text()="加入日期" or text()="注册日期" or *[text()="加入日期"]]/following-sibling::td[1]//text()'
            '|//div/b[text()="加入日期"]/../text()')
        if join_at_text:
            self.join_at = StringUtils.unify_datetime_str(join_at_text[0].split(' (')[0].strip())

        # 做种体积 & 做种数
        # seeding 页面获取不到的话，此处再获取一次
        seeding_sizes = html.xpath('//tr/td[text()="当前上传"]/following-sibling::td[1]//'
                                   'table[tr[1][td[4 and text()="尺寸"]]]//tr[position()>1]/td[4]')
        seeding_seeders = html.xpath('//tr/td[text()="当前上传"]/following-sibling::td[1]//'
                                     'table[tr[1][td[5 and text()="做种者"]]]//tr[position()>1]/td[5]//text()')
        tmp_seeding = len(seeding_sizes)
        tmp_seeding_size = 0
        tmp_seeding_info = []
        for i in range(0, len(seeding_sizes)):
            size = StringUtils.num_filesize(seeding_sizes[i].xpath("string(.)").strip())
            seeders = StringUtils.str_int(seeding_seeders[i])

            tmp_seeding_size += size
            tmp_seeding_info.append([seeders, size])

        if not self.seeding_size:
            self.seeding_size = tmp_seeding_size
        if not self.seeding:
            self.seeding = tmp_seeding
        if not self.seeding_info:
            self.seeding_info = tmp_seeding_info

        seeding_sizes = html.xpath('//tr/td[text()="做种统计"]/following-sibling::td[1]//text()')
        if seeding_sizes:
            seeding_match = re.search(r"总做种数:\s+(\d+)", seeding_sizes[0], re.IGNORECASE)
            seeding_size_match = re.search(r"总做种体积:\s+([\d,.\s]+[KMGTPI]*B)", seeding_sizes[0], re.IGNORECASE)
            tmp_seeding = StringUtils.str_int(seeding_match.group(1)) if (
                    seeding_match and seeding_match.group(1)) else 0
            tmp_seeding_size = StringUtils.num_filesize(
                seeding_size_match.group(1).strip()) if seeding_size_match else 0
        if not self.seeding_size:
            self.seeding_size = tmp_seeding_size
        if not self.seeding:
            self.seeding = tmp_seeding

        self._fixup_torrent_seeding_page(html)

    def _fixup_torrent_seeding_page(self, html):
        """
        修正种子页面链接
        :param html:
        :return:
        """
        # 单独的种子页面
        seeding_url_text = html.xpath('//a[contains(@href,"getusertorrentlist.php") '
                                      'and contains(@href,"seeding")]/@href')
        if seeding_url_text:
            self._torrent_seeding_page = seeding_url_text[0].strip()
        # 从JS调用种获取用户ID
        seeding_url_text = html.xpath('//a[contains(@href, "javascript: getusertorrentlistajax") '
                                      'and contains(@href,"seeding")]/@href')
        csrf_text = html.xpath('//meta[@name="x-csrf"]/@content')
        if not self._torrent_seeding_page and seeding_url_text:
            user_js = re.search(r"javascript: getusertorrentlistajax\(\s*'(\d+)", seeding_url_text[0])
            if user_js and user_js.group(1).strip():
                self.userid = user_js.group(1).strip()
                self._torrent_seeding_page = f"getusertorrentlistajax.php?userid={self.userid}&type=seeding"
        elif seeding_url_text and csrf_text:
            if csrf_text[0].strip():
                self._torrent_seeding_page \
                    = f"ajax_getusertorrentlist.php"
                self._torrent_seeding_params = {'userid': self.userid, 'type': 'seeding', 'csrf': csrf_text[0].strip()}

        # 分类做种模式
        # 临时屏蔽
        # seeding_url_text = html.xpath('//tr/td[text()="当前做种"]/following-sibling::td[1]'
        #                              '/table//td/a[contains(@href,"seeding")]/@href')
        # if seeding_url_text:
        #    self._torrent_seeding_page = seeding_url_text

    def _get_user_level(self, html):
        # 等级 获取同一行等级数据，图片格式等级，取title信息，否则取文本信息
        user_levels_text = html.xpath('//tr/td[text()="等級" or text()="等级" or *[text()="等级"]]/'
                                      'following-sibling::td[1]/img[1]/@title')
        if user_levels_text:
            self.user_level = user_levels_text[0].strip()
            return

        user_levels_text = html.xpath('//tr/td[text()="等級" or text()="等级"]/'
                                      'following-sibling::td[1 and not(img)]'
                                      '|//tr/td[text()="等級" or text()="等级"]/'
                                      'following-sibling::td[1 and img[not(@title)]]')
        if user_levels_text:
            self.user_level = user_levels_text[0].xpath("string(.)").strip()
            return

        user_levels_text = html.xpath('//tr/td[text()="等級" or text()="等级"]/'
                                      'following-sibling::td[1]')
        if user_levels_text:
            self.user_level = user_levels_text[0].xpath("string(.)").strip()
            return

        user_levels_text = html.xpath('//a[contains(@href, "userdetails")]/text()')
        if not self.user_level and user_levels_text:
            for user_level_text in user_levels_text:
                user_level_match = re.search(r"\[(.*)]", user_level_text)
                if user_level_match and user_level_match.group(1).strip():
                    self.user_level = user_level_match.group(1).strip()
                    break

    def _parse_message_unread_links(self, html_text: str, msg_links: list) -> Optional[str]:
        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return None

        message_links = html.xpath('//tr[not(./td/img[@alt="Read"])]/td/a[contains(@href, "viewmessage")]/@href')
        msg_links.extend(message_links)
        # 是否存在下页数据
        next_page = None
        next_page_text = html.xpath('//a[contains(.//text(), "下一页") or contains(.//text(), "下一頁")]/@href')
        if next_page_text:
            next_page = next_page_text[-1].strip()

        return next_page

    def _parse_message_content(self, html_text):
        html = etree.HTML(html_text)
        if not StringUtils.is_valid_html_element(html):
            return None, None, None
        # 标题
        message_head_text = None
        message_head = html.xpath('//h1/text()'
                                  '|//div[@class="layui-card-header"]/span[1]/text()')
        if message_head:
            message_head_text = message_head[-1].strip()

        # 消息时间
        message_date_text = None
        message_date = html.xpath('//h1/following-sibling::table[.//tr/td[@class="colhead"]]//tr[2]/td[2]'
                                  '|//div[@class="layui-card-header"]/span[2]/span[2]')
        if message_date:
            message_date_text = message_date[0].xpath("string(.)").strip()

        # 消息内容
        message_content_text = None
        message_content = html.xpath('//h1/following-sibling::table[.//tr/td[@class="colhead"]]//tr[3]/td'
                                     '|//div[contains(@class,"layui-card-body")]')
        if message_content:
            message_content_text = message_content[0].xpath("string(.)").strip()

        return message_head_text, message_date_text, message_content_text

    def _fixup_traffic_info(self, html):
        # fixup bonus
        if not self.bonus:
            bonus_text = html.xpath('//tr/td[text()="魔力值" or text()="猫粮"]/following-sibling::td[1]/text()')
            if bonus_text:
                self.bonus = StringUtils.str_float(bonus_text[0].strip())
