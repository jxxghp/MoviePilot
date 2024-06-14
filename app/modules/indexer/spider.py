import copy
import datetime
import re
import traceback
from typing import List
from urllib.parse import quote, urlencode, urlparse, parse_qs

import chardet
from jinja2 import Template
from pyquery import PyQuery
from ruamel.yaml import CommentedMap

from app.core.config import settings
from app.helper.browser import PlaywrightHelper
from app.log import logger
from app.schemas.types import MediaType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class TorrentSpider:
    # 是否出现错误
    is_error: bool = False
    # 索引器ID
    indexerid: int = None
    # 索引器名称
    indexername: str = None
    # 站点域名
    domain: str = None
    # 站点Cookie
    cookie: str = None
    # 站点UA
    ua: str = None
    # Requests 代理
    proxies: dict = None
    # playwright 代理
    proxy_server: dict = None
    # 是否渲染
    render: bool = False
    # Referer
    referer: str = None
    # 搜索关键字
    keyword: str = None
    # 媒体类型
    mtype: MediaType = None
    # 搜索路径、方式配置
    search: dict = {}
    # 批量搜索配置
    batch: dict = {}
    # 浏览配置
    browse: dict = {}
    # 站点分类配置
    category: dict = {}
    # 站点种子列表配置
    list: dict = {}
    # 站点种子字段配置
    fields: dict = {}
    # 页码
    page: int = 0
    # 搜索条数, 默认: 100条
    result_num: int = 100
    # 单个种子信息
    torrents_info: dict = {}
    # 种子列表
    torrents_info_array: list = []
    # 搜索超时, 默认: 15秒
    _timeout = 15

    def __init__(self,
                 indexer: CommentedMap,
                 keyword: [str, list] = None,
                 page: int = 0,
                 referer: str = None,
                 mtype: MediaType = None):
        """
        设置查询参数
        :param indexer: 索引器
        :param keyword: 搜索关键字，如果数组则为批量搜索
        :param page: 页码
        :param referer: Referer
        :param mtype: 媒体类型
        """
        if not indexer:
            return
        self.keyword = keyword
        self.mtype = mtype
        self.indexerid = indexer.get('id')
        self.indexername = indexer.get('name')
        self.search = indexer.get('search')
        self.batch = indexer.get('batch')
        self.browse = indexer.get('browse')
        self.category = indexer.get('category')
        self.list = indexer.get('torrents').get('list', {})
        self.fields = indexer.get('torrents').get('fields')
        self.render = indexer.get('render')
        self.domain = indexer.get('domain')
        self.result_num = int(indexer.get('result_num') or 100)
        self._timeout = int(indexer.get('timeout') or 15)
        self.page = page
        if self.domain and not str(self.domain).endswith("/"):
            self.domain = self.domain + "/"
        if indexer.get('ua'):
            self.ua = indexer.get('ua') or settings.USER_AGENT
        else:
            self.ua = settings.USER_AGENT
        if indexer.get('proxy'):
            self.proxies = settings.PROXY
            self.proxy_server = settings.PROXY_SERVER
        if indexer.get('cookie'):
            self.cookie = indexer.get('cookie')
        if referer:
            self.referer = referer
        self.torrents_info_array = []

    def get_torrents(self) -> List[dict]:
        """
        开始请求
        """
        if not self.search or not self.domain:
            return []

        # 种子搜索相对路径
        paths = self.search.get('paths', [])
        torrentspath = ""
        if len(paths) == 1:
            torrentspath = paths[0].get('path', '')
        else:
            for path in paths:
                if path.get("type") == "all" and not self.mtype:
                    torrentspath = path.get('path')
                    break
                elif path.get("type") == "movie" and self.mtype == MediaType.MOVIE:
                    torrentspath = path.get('path')
                    break
                elif path.get("type") == "tv" and self.mtype == MediaType.TV:
                    torrentspath = path.get('path')
                    break

        # 精确搜索
        if self.keyword:

            if isinstance(self.keyword, list):
                # 批量查询
                if self.batch:
                    delimiter = self.batch.get('delimiter') or ' '
                    space_replace = self.batch.get('space_replace') or ' '
                    search_word = delimiter.join([str(k).replace(' ',
                                                                 space_replace) for k in self.keyword])
                else:
                    search_word = " ".join(self.keyword)
                # 查询模式：或
                search_mode = "1"
            else:
                # 单个查询
                search_word = self.keyword
                # 查询模式与
                search_mode = "0"

            # 搜索URL
            indexer_params = self.search.get("params", {}).copy()
            if indexer_params:
                search_area = indexer_params.get('search_area')
                # search_area非0表示支持imdbid搜索
                if (search_area and
                        (not self.keyword or not self.keyword.startswith('tt'))):
                    # 支持imdbid搜索，但关键字不是imdbid时，不启用imdbid搜索
                    indexer_params.pop('search_area')
                # 变量字典
                inputs_dict = {
                    "keyword": search_word
                }
                # 查询参数，默认查询标题
                params = {
                    "search_mode": search_mode,
                    "search_area": 0,
                    "page": self.page or 0,
                    "notnewword": 1
                }
                # 额外参数
                for key, value in indexer_params.items():
                    params.update({
                        "%s" % key: str(value).format(**inputs_dict)
                    })
                # 分类条件
                if self.category:
                    if self.mtype == MediaType.TV:
                        cats = self.category.get("tv") or []
                    elif self.mtype == MediaType.MOVIE:
                        cats = self.category.get("movie") or []
                    else:
                        cats = (self.category.get("movie") or []) + (self.category.get("tv") or [])
                    for cat in cats:
                        if self.category.get("field"):
                            value = params.get(self.category.get("field"), "")
                            params.update({
                                "%s" % self.category.get("field"): value + self.category.get("delimiter",
                                                                                             ' ') + cat.get("id")
                            })
                        else:
                            params.update({
                                "cat%s" % cat.get("id"): 1
                            })
                searchurl = self.domain + torrentspath + "?" + urlencode(params)
            else:
                # 变量字典
                inputs_dict = {
                    "keyword": quote(search_word),
                    "page": self.page or 0
                }
                # 无额外参数
                searchurl = self.domain + str(torrentspath).format(**inputs_dict)

        # 列表浏览
        else:
            # 变量字典
            inputs_dict = {
                "page": self.page or 0,
                "keyword": ""
            }
            # 有单独浏览路径
            if self.browse:
                torrentspath = self.browse.get("path")
                if self.browse.get("start"):
                    start_page = int(self.browse.get("start")) + int(self.page or 0)
                    inputs_dict.update({
                        "page": start_page
                    })
            elif self.page:
                torrentspath = torrentspath + f"?page={self.page}"
            # 搜索Url
            searchurl = self.domain + str(torrentspath).format(**inputs_dict)

        logger.info(f"开始请求：{searchurl}")

        if self.render:
            # 浏览器仿真
            page_source = PlaywrightHelper().get_page_source(
                url=searchurl,
                cookies=self.cookie,
                ua=self.ua,
                proxies=self.proxy_server,
                timeout=self._timeout
            )
        else:
            # requests请求
            ret = RequestUtils(
                ua=self.ua,
                cookies=self.cookie,
                timeout=self._timeout,
                referer=self.referer,
                proxies=self.proxies
            ).get_res(searchurl, allow_redirects=True)
            if ret is not None:
                # 使用chardet检测字符编码
                raw_data = ret.content
                if raw_data:
                    try:
                        result = chardet.detect(raw_data)
                        encoding = result['encoding']
                        # 解码为字符串
                        page_source = raw_data.decode(encoding)
                    except Exception as e:
                        logger.debug(f"chardet解码失败：{str(e)}")
                        # 探测utf-8解码
                        if re.search(r"charset=\"?utf-8\"?", ret.text, re.IGNORECASE):
                            ret.encoding = "utf-8"
                        else:
                            ret.encoding = ret.apparent_encoding
                        page_source = ret.text
                else:
                    page_source = ret.text
            else:
                page_source = ""

        # 解析
        return self.parse(page_source)

    def __get_title(self, torrent):
        # title default text
        if 'title' not in self.fields:
            return
        selector = self.fields.get('title', {})
        if 'selector' in selector:
            title = torrent(selector.get('selector', '')).clone()
            self.__remove(title, selector)
            items = self.__attribute_or_text(title, selector)
            self.torrents_info['title'] = self.__index(items, selector)
        elif 'text' in selector:
            render_dict = {}
            if "title_default" in self.fields:
                title_default_selector = self.fields.get('title_default', {})
                title_default_item = torrent(title_default_selector.get('selector', '')).clone()
                self.__remove(title_default_item, title_default_selector)
                items = self.__attribute_or_text(title_default_item, selector)
                title_default = self.__index(items, title_default_selector)
                render_dict.update({'title_default': title_default})
            if "title_optional" in self.fields:
                title_optional_selector = self.fields.get('title_optional', {})
                title_optional_item = torrent(title_optional_selector.get('selector', '')).clone()
                self.__remove(title_optional_item, title_optional_selector)
                items = self.__attribute_or_text(title_optional_item, title_optional_selector)
                title_optional = self.__index(items, title_optional_selector)
                render_dict.update({'title_optional': title_optional})
            self.torrents_info['title'] = Template(selector.get('text')).render(fields=render_dict)
        self.torrents_info['title'] = self.__filter_text(self.torrents_info.get('title'),
                                                         selector.get('filters'))

    def __get_description(self, torrent):
        # title optional text
        if 'description' not in self.fields:
            return
        selector = self.fields.get('description', {})
        if "selector" in selector \
                or "selectors" in selector:
            description = torrent(selector.get('selector', selector.get('selectors', ''))).clone()
            if description:
                self.__remove(description, selector)
                items = self.__attribute_or_text(description, selector)
                self.torrents_info['description'] = self.__index(items, selector)
        elif "text" in selector:
            render_dict = {}
            if "tags" in self.fields:
                tags_selector = self.fields.get('tags', {})
                tags_item = torrent(tags_selector.get('selector', '')).clone()
                self.__remove(tags_item, tags_selector)
                items = self.__attribute_or_text(tags_item, tags_selector)
                tag = self.__index(items, tags_selector)
                render_dict.update({'tags': tag})
            if "subject" in self.fields:
                subject_selector = self.fields.get('subject', {})
                subject_item = torrent(subject_selector.get('selector', '')).clone()
                self.__remove(subject_item, subject_selector)
                items = self.__attribute_or_text(subject_item, subject_selector)
                subject = self.__index(items, subject_selector)
                render_dict.update({'subject': subject})
            if "description_free_forever" in self.fields:
                description_free_forever_selector = self.fields.get("description_free_forever", {})
                description_free_forever_item = torrent(description_free_forever_selector.get("selector", '')).clone()
                self.__remove(description_free_forever_item, description_free_forever_selector)
                items = self.__attribute_or_text(description_free_forever_item, description_free_forever_selector)
                description_free_forever = self.__index(items, description_free_forever_selector)
                render_dict.update({"description_free_forever": description_free_forever})
            if "description_normal" in self.fields:
                description_normal_selector = self.fields.get("description_normal", {})
                description_normal_item = torrent(description_normal_selector.get("selector", '')).clone()
                self.__remove(description_normal_item, description_normal_selector)
                items = self.__attribute_or_text(description_normal_item, description_normal_selector)
                description_normal = self.__index(items, description_normal_selector)
                render_dict.update({"description_normal": description_normal})
            self.torrents_info['description'] = Template(selector.get('text')).render(fields=render_dict)
        self.torrents_info['description'] = self.__filter_text(self.torrents_info.get('description'),
                                                               selector.get('filters'))

    def __get_detail(self, torrent):
        # details page text
        if 'details' not in self.fields:
            return
        selector = self.fields.get('details', {})
        details = torrent(selector.get('selector', '')).clone()
        self.__remove(details, selector)
        items = self.__attribute_or_text(details, selector)
        item = self.__index(items, selector)
        detail_link = self.__filter_text(item, selector.get('filters'))
        if detail_link:
            if not detail_link.startswith("http"):
                if detail_link.startswith("//"):
                    self.torrents_info['page_url'] = self.domain.split(":")[0] + ":" + detail_link
                elif detail_link.startswith("/"):
                    self.torrents_info['page_url'] = self.domain + detail_link[1:]
                else:
                    self.torrents_info['page_url'] = self.domain + detail_link
            else:
                self.torrents_info['page_url'] = detail_link

    def __get_download(self, torrent):
        # download link text
        if 'download' not in self.fields:
            return
        selector = self.fields.get('download', {})
        download = torrent(selector.get('selector', '')).clone()
        self.__remove(download, selector)
        items = self.__attribute_or_text(download, selector)
        item = self.__index(items, selector)
        download_link = self.__filter_text(item, selector.get('filters'))
        if download_link:
            if not download_link.startswith("http") \
                    and not download_link.startswith("magnet"):
                _scheme, _domain = StringUtils.get_url_netloc(self.domain)
                if _domain in download_link:
                    if download_link.startswith("/"):
                        self.torrents_info['enclosure'] = f"{_scheme}:{download_link}"
                    else:
                        self.torrents_info['enclosure'] = f"{_scheme}://{download_link}"
                else:
                    if download_link.startswith("/"):
                        self.torrents_info['enclosure'] = f"{self.domain}{download_link[1:]}"
                    else:
                        self.torrents_info['enclosure'] = f"{self.domain}{download_link}"
            else:
                self.torrents_info['enclosure'] = download_link

    def __get_imdbid(self, torrent):
        # imdbid
        if "imdbid" not in self.fields:
            return
        selector = self.fields.get('imdbid', {})
        imdbid = torrent(selector.get('selector', '')).clone()
        self.__remove(imdbid, selector)
        items = self.__attribute_or_text(imdbid, selector)
        item = self.__index(items, selector)
        self.torrents_info['imdbid'] = item
        self.torrents_info['imdbid'] = self.__filter_text(self.torrents_info.get('imdbid'),
                                                          selector.get('filters'))

    def __get_size(self, torrent):
        # torrent size int
        if 'size' not in self.fields:
            return
        selector = self.fields.get('size', {})
        size = torrent(selector.get('selector', selector.get("selectors", ''))).clone()
        self.__remove(size, selector)
        items = self.__attribute_or_text(size, selector)
        item = self.__index(items, selector)
        if item:
            size_val = item.replace("\n", "").strip()
            size_val = self.__filter_text(size_val,
                                          selector.get('filters'))
            self.torrents_info['size'] = StringUtils.num_filesize(size_val)
        else:
            self.torrents_info['size'] = 0

    def __get_leechers(self, torrent):
        # torrent leechers int
        if 'leechers' not in self.fields:
            return
        selector = self.fields.get('leechers', {})
        leechers = torrent(selector.get('selector', '')).clone()
        self.__remove(leechers, selector)
        items = self.__attribute_or_text(leechers, selector)
        item = self.__index(items, selector)
        if item:
            peers_val = item.split("/")[0]
            peers_val = peers_val.replace(",", "")
            peers_val = self.__filter_text(peers_val,
                                           selector.get('filters'))
            self.torrents_info['peers'] = int(peers_val) if peers_val and peers_val.isdigit() else 0
        else:
            self.torrents_info['peers'] = 0

    def __get_seeders(self, torrent):
        # torrent leechers int
        if 'seeders' not in self.fields:
            return
        selector = self.fields.get('seeders', {})
        seeders = torrent(selector.get('selector', '')).clone()
        self.__remove(seeders, selector)
        items = self.__attribute_or_text(seeders, selector)
        item = self.__index(items, selector)
        if item:
            seeders_val = item.split("/")[0]
            seeders_val = seeders_val.replace(",", "")
            seeders_val = self.__filter_text(seeders_val,
                                             selector.get('filters'))
            self.torrents_info['seeders'] = int(seeders_val) if seeders_val and seeders_val.isdigit() else 0
        else:
            self.torrents_info['seeders'] = 0

    def __get_grabs(self, torrent):
        # torrent grabs int
        if 'grabs' not in self.fields:
            return
        selector = self.fields.get('grabs', {})
        grabs = torrent(selector.get('selector', '')).clone()
        self.__remove(grabs, selector)
        items = self.__attribute_or_text(grabs, selector)
        item = self.__index(items, selector)
        if item:
            grabs_val = item.split("/")[0]
            grabs_val = grabs_val.replace(",", "")
            grabs_val = self.__filter_text(grabs_val,
                                           selector.get('filters'))
            self.torrents_info['grabs'] = int(grabs_val) if grabs_val and grabs_val.isdigit() else 0
        else:
            self.torrents_info['grabs'] = 0

    def __get_pubdate(self, torrent):
        # torrent pubdate yyyy-mm-dd hh:mm:ss
        if 'date_added' not in self.fields:
            return
        selector = self.fields.get('date_added', {})
        pubdate = torrent(selector.get('selector', '')).clone()
        self.__remove(pubdate, selector)
        items = self.__attribute_or_text(pubdate, selector)
        pubdate_str = self.__index(items, selector)
        if pubdate_str:
            pubdate_str = pubdate_str.replace('\n', ' ').strip()
        self.torrents_info['pubdate'] = self.__filter_text(pubdate_str,
                                                           selector.get('filters'))

    def __get_date_elapsed(self, torrent):
        # torrent data elaspsed text
        if 'date_elapsed' not in self.fields:
            return
        selector = self.fields.get('date_elapsed', {})
        date_elapsed = torrent(selector.get('selector', '')).clone()
        self.__remove(date_elapsed, selector)
        items = self.__attribute_or_text(date_elapsed, selector)
        self.torrents_info['date_elapsed'] = self.__index(items, selector)
        self.torrents_info['date_elapsed'] = self.__filter_text(self.torrents_info.get('date_elapsed'),
                                                                selector.get('filters'))

    def __get_downloadvolumefactor(self, torrent):
        # downloadvolumefactor int
        selector = self.fields.get('downloadvolumefactor', {})
        if not selector:
            return
        self.torrents_info['downloadvolumefactor'] = 1
        if 'case' in selector:
            for downloadvolumefactorselector in list(selector.get('case', {}).keys()):
                downloadvolumefactor = torrent(downloadvolumefactorselector)
                if len(downloadvolumefactor) > 0:
                    self.torrents_info['downloadvolumefactor'] = selector.get('case', {}).get(
                        downloadvolumefactorselector)
                    break
        elif "selector" in selector:
            downloadvolume = torrent(selector.get('selector', '')).clone()
            self.__remove(downloadvolume, selector)
            items = self.__attribute_or_text(downloadvolume, selector)
            item = self.__index(items, selector)
            if item:
                downloadvolumefactor = re.search(r'(\d+\.?\d*)', item)
                if downloadvolumefactor:
                    self.torrents_info['downloadvolumefactor'] = int(downloadvolumefactor.group(1))

    def __get_uploadvolumefactor(self, torrent):
        # uploadvolumefactor int
        selector = self.fields.get('uploadvolumefactor', {})
        if not selector:
            return
        self.torrents_info['uploadvolumefactor'] = 1
        if 'case' in selector:
            for uploadvolumefactorselector in list(selector.get('case', {}).keys()):
                uploadvolumefactor = torrent(uploadvolumefactorselector)
                if len(uploadvolumefactor) > 0:
                    self.torrents_info['uploadvolumefactor'] = selector.get('case', {}).get(
                        uploadvolumefactorselector)
                    break
        elif "selector" in selector:
            uploadvolume = torrent(selector.get('selector', '')).clone()
            self.__remove(uploadvolume, selector)
            items = self.__attribute_or_text(uploadvolume, selector)
            item = self.__index(items, selector)
            if item:
                uploadvolumefactor = re.search(r'(\d+\.?\d*)', item)
                if uploadvolumefactor:
                    self.torrents_info['uploadvolumefactor'] = int(uploadvolumefactor.group(1))

    def __get_labels(self, torrent):
        # labels ['label1', 'label2']
        if 'labels' not in self.fields:
            return
        selector = self.fields.get('labels', {})
        labels = torrent(selector.get("selector", "")).clone()
        self.__remove(labels, selector)
        items = self.__attribute_or_text(labels, selector)
        if items:
            self.torrents_info['labels'] = [item for item in items if item]
        else:
            self.torrents_info['labels'] = []

    def __get_free_date(self, torrent):
        # free date yyyy-mm-dd hh:mm:ss
        if 'freedate' not in self.fields:
            return
        selector = self.fields.get('freedate', {})
        freedate = torrent(selector.get('selector', '')).clone()
        self.__remove(freedate, selector)
        items = self.__attribute_or_text(freedate, selector)
        self.torrents_info['freedate'] = self.__index(items, selector)
        self.torrents_info['freedate'] = self.__filter_text(self.torrents_info.get('freedate'),
                                                            selector.get('filters'))

    def __get_hit_and_run(self, torrent):
        # hitandrun True/False
        if 'hr' not in self.fields:
            return
        selector = self.fields.get('hr', {})
        hit_and_run = torrent(selector.get('selector', ''))
        if hit_and_run:
            self.torrents_info['hit_and_run'] = True
        else:
            self.torrents_info['hit_and_run'] = False

    def __get_category(self, torrent):
        # category 电影/电视剧
        if 'category' not in self.fields:
            return
        selector = self.fields.get('category', {})
        category = torrent(selector.get('selector', '')).clone()
        self.__remove(category, selector)
        items = self.__attribute_or_text(category, selector)
        category_value = self.__index(items, selector)
        category_value = self.__filter_text(category_value,
                                            selector.get('filters'))
        if category_value and self.category:
            tv_cats = [str(cat.get("id")) for cat in self.category.get("tv") or []]
            movie_cats = [str(cat.get("id")) for cat in self.category.get("movie") or []]
            if category_value in tv_cats \
                    and category_value not in movie_cats:
                self.torrents_info['category'] = MediaType.TV.value
            elif category_value in movie_cats:
                self.torrents_info['category'] = MediaType.MOVIE.value
            else:
                self.torrents_info['category'] = MediaType.UNKNOWN.value
        else:
            self.torrents_info['category'] = MediaType.UNKNOWN.value

    def get_info(self, torrent) -> dict:
        """
        解析单条种子数据
        """
        self.torrents_info = {}
        try:
            # 标题
            self.__get_title(torrent)
            # 描述
            self.__get_description(torrent)
            # 详情页面
            self.__get_detail(torrent)
            # 下载链接
            self.__get_download(torrent)
            # 完成数
            self.__get_grabs(torrent)
            # 下载数
            self.__get_leechers(torrent)
            # 做种数
            self.__get_seeders(torrent)
            # 大小
            self.__get_size(torrent)
            # IMDBID
            self.__get_imdbid(torrent)
            # 下载系数
            self.__get_downloadvolumefactor(torrent)
            # 上传系数
            self.__get_uploadvolumefactor(torrent)
            # 发布时间
            self.__get_pubdate(torrent)
            # 已发布时间
            self.__get_date_elapsed(torrent)
            # 免费载止时间
            self.__get_free_date(torrent)
            # 标签
            self.__get_labels(torrent)
            # HR
            self.__get_hit_and_run(torrent)
            # 分类
            self.__get_category(torrent)

        except Exception as err:
            logger.error("%s 搜索出现错误：%s" % (self.indexername, str(err)))
        return self.torrents_info

    @staticmethod
    def __filter_text(text: str, filters: list):
        """
        对文件进行处理
        """
        if not text or not filters or not isinstance(filters, list):
            return text
        if not isinstance(text, str):
            text = str(text)
        for filter_item in filters:
            if not text:
                break
            method_name = filter_item.get("name")
            try:
                args = filter_item.get("args")
                if method_name == "re_search" and isinstance(args, list):
                    rematch = re.search(r"%s" % args[0], text)
                    if rematch:
                        text = rematch.group(args[-1])
                elif method_name == "split" and isinstance(args, list):
                    text = text.split(r"%s" % args[0])[args[-1]]
                elif method_name == "replace" and isinstance(args, list):
                    text = text.replace(r"%s" % args[0], r"%s" % args[-1])
                elif method_name == "dateparse" and isinstance(args, str):
                    text = text.replace("\n", " ").strip()
                    text = datetime.datetime.strptime(text, r"%s" % args)
                elif method_name == "strip":
                    text = text.strip()
                elif method_name == "appendleft":
                    text = f"{args}{text}"
                elif method_name == "querystring":
                    parsed_url = urlparse(text)
                    query_params = parse_qs(parsed_url.query)
                    param_value = query_params.get(args)
                    text = param_value[0] if param_value else ''
            except Exception as err:
                logger.debug(f'过滤器 {method_name} 处理失败：{str(err)} - {traceback.format_exc()}')
        return text.strip()

    @staticmethod
    def __remove(item, selector):
        """
        移除元素
        """
        if selector and "remove" in selector:
            removelist = selector.get('remove', '').split(', ')
            for v in removelist:
                item.remove(v)

    @staticmethod
    def __attribute_or_text(item, selector: dict):
        if not selector:
            return item
        if not item:
            return []
        if 'attribute' in selector:
            items = [i.attr(selector.get('attribute')) for i in item.items() if i]
        else:
            items = [i.text() for i in item.items() if i]
        return items

    @staticmethod
    def __index(items: list, selector: dict):
        if not items:
            return None
        if selector:
            if "contents" in selector \
                    and len(items) > int(selector.get("contents")):
                items = items[0].split("\n")[selector.get("contents")]
            elif "index" in selector \
                    and len(items) > int(selector.get("index")):
                items = items[int(selector.get("index"))]
        if isinstance(items, list):
            items = items[0]
        return items

    def parse(self, html_text: str) -> List[dict]:
        """
        解析整个页面
        """
        if not html_text:
            self.is_error = True
            return []
        # 清空旧结果
        self.torrents_info_array = []
        try:
            # 解析站点文本对象
            html_doc = PyQuery(html_text)
            # 种子筛选器
            torrents_selector = self.list.get('selector', '')
            # 遍历种子html列表
            for torn in html_doc(torrents_selector):
                self.torrents_info_array.append(copy.deepcopy(self.get_info(PyQuery(torn))))
                if len(self.torrents_info_array) >= int(self.result_num):
                    break
            return self.torrents_info_array
        except Exception as err:
            self.is_error = True
            logger.warn(f"错误：{self.indexername} {str(err)}")
