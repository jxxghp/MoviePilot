import datetime
import re
import traceback
from typing import Any, List, Optional
from urllib.parse import parse_qs, quote, urlparse

from jinja2 import Template
from pyquery import PyQuery

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.adapters.system import rust as rust_accel
from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.foundation import url as url_tools
from app.foundation.url import UrlUtils
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MediaType


def select_media_categories(category: Optional[dict], mtype: Optional[MediaType]) -> list[dict]:
    """根据媒体类型选择站点索引配置中的分类列表。"""
    if not category:
        return []
    if mtype == MediaType.TV:
        return category.get("tv") or []
    if mtype == MediaType.MOVIE:
        return category.get("movie") or []
    if mtype == MediaType.MUSIC:
        return category.get("music") or []
    return (
        (category.get("movie") or [])
        + (category.get("tv") or [])
        + (category.get("music") or [])
    )


def resolve_category_media_type(category_value: Any, category: Optional[dict]) -> MediaType:
    """将站点分类 ID 映射为统一的电影、电视剧或音乐类型。"""
    if category_value is None or not category:
        return MediaType.UNKNOWN
    category_id = str(category_value)
    matches = [
        media_type
        for media_type, key in (
            (MediaType.MOVIE, "movie"),
            (MediaType.TV, "tv"),
            (MediaType.MUSIC, "music"),
        )
        if category_id in {
            str(item.get("id"))
            for item in category.get(key) or []
            if isinstance(item, dict) and item.get("id") is not None
        }
    ]
    return matches[0] if len(matches) == 1 else MediaType.UNKNOWN


class SiteSpider:
    """
    站点爬虫
    """

    _default_result_num = 100

    @property
    def __class__(self):
        """隐藏 Spider 的真实类型，避免模板或脚本读取内部实现。"""
        return object

    @property
    def __dict__(self):
        """隐藏 Spider 实例属性，避免外部枚举请求上下文。"""
        return {}

    @property
    def __dir__(self):
        """拒绝外部列举 Spider 的受保护属性。"""
        raise AttributeError("Cannot read protected attribute!")

    def __init__(self,
                 indexer: dict,
                 keyword: Optional[str] = None,
                 mtype: MediaType = None,
                 cat: Optional[str] = None,
                 page: Optional[int] = 0,
                 referer: Optional[str] = None,
                 search_type: Optional[str] = "torrents"):
        """
        设置查询参数
        :param indexer: 索引器
        :param keyword: 搜索关键字，如果数组则为批量搜索
        :param mtype: 媒体类型
        :param cat: 搜索分类
        :param page: 页码
        :param referer: Referer
        """
        if not indexer:
            return
        self.keyword = keyword
        self.cat = cat
        self.mtype = mtype
        self.search_type = search_type or "torrents"
        self.indexerid = indexer.get('id')
        self.indexername = indexer.get('name')
        self.site_media_type = MediaType.from_agent(indexer.get('media_type'))
        if self.search_type == "subtitles":
            subtitle_conf = indexer.get('subtitles') or {}
            self.search = subtitle_conf.get('search')
            self.batch = subtitle_conf.get('batch')
            self.browse = subtitle_conf.get('browse')
            self.category = subtitle_conf.get('category')
            self.list = subtitle_conf.get('list') or {}
            self.fields = subtitle_conf.get('fields') or {}
            result_num = subtitle_conf.get('result_num') or indexer.get('result_num')
        else:
            self.search = indexer.get('search')
            self.batch = indexer.get('batch')
            self.browse = indexer.get('browse')
            self.category = indexer.get('category')
            self.list = (indexer.get('torrents') or {}).get('list', {})
            self.fields = (indexer.get('torrents') or {}).get('fields') or {}
            if not keyword and self.browse:
                self.list = self.browse.get('list') or self.list
                self.fields = self.browse.get('fields') or self.fields
            result_num = indexer.get('result_num')
        self.result_media_type_from_request = (
            (self.search or {}).get("result_media_type") == "requested"
        )
        self.requested_result_media_type = None
        self._field_templates = self.__build_field_templates()
        self.domain = indexer.get('domain')
        self.result_num = int(result_num or self.default_result_num())
        self._timeout = int(indexer.get('timeout') or 15)
        self.page = page
        if self.domain and not str(self.domain).endswith("/"):
            self.domain = self.domain + "/"
        self.ua = indexer.get('ua') or get_runtime_setting('USER_AGENT')
        self.proxies = get_runtime_setting('PROXY') if indexer.get('proxy') else None
        self.proxy_server = get_runtime_setting('PROXY_SERVER') if indexer.get('proxy') else None
        self.cookie = indexer.get('cookie')
        self.referer = referer
        # 初始化属性
        self.is_error = False
        self.torrents_info = {}
        self.torrents_info_array = []

    def __build_field_templates(self) -> dict:
        """
        预编译字段模板，避免按每条种子重复构造 Jinja Template。
        """
        templates = {}
        for name in ("title", "description", "date"):
            selector = (self.fields or {}).get(name, {})
            template_text = selector.get("text") if isinstance(selector, dict) else None
            if not template_text:
                continue
            templates[name] = Template(template_text)
        return templates

    @classmethod
    def default_result_num(cls) -> int:
        """
        获取普通配置站点的默认单页数量。
        """
        return cls._default_result_num

    def __get_search_url(self):
        """
        获取搜索URL
        """
        # 种子搜索相对路径
        paths = self.search.get('paths', [])
        torrentspath = ""
        # 是否选中了媒体类型专用路径，浏览模式下专用路径优先于 browse 配置
        typed_path_selected = False
        category_filter_selected = False
        if len(paths) == 1:
            torrentspath = paths[0].get('path', '')
        else:
            # 优先使用媒体类型专用路径；没有专用路径时回退到 all，兼容仅为某一类新增分支的站点。
            fallback_path = ""
            expected_type = {
                MediaType.MOVIE: "movie",
                MediaType.TV: "tv",
                MediaType.MUSIC: "music",
            }.get(self.mtype)
            for path in paths:
                path_type = path.get("type")
                if path_type == "all" and not fallback_path:
                    fallback_path = path.get('path', '')
                if (expected_type and path_type == expected_type) or (
                    not expected_type and path_type == "all"
                ):
                    torrentspath = path.get('path', '')
                    typed_path_selected = bool(expected_type and path_type == expected_type)
                    break
            if not torrentspath:
                torrentspath = fallback_path

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
            is_imdbid_search = isinstance(self.keyword, str) and re.fullmatch(r"tt\d+", self.keyword)
            search_word = self.__format_search_word(search_word)

            # 搜索URL
            indexer_params = self.search.get("params", {}).copy()
            if indexer_params:
                search_area = indexer_params.get('search_area')
                # search_area非0表示支持imdbid搜索
                if search_area and not is_imdbid_search:
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
                    cats = select_media_categories(self.category, self.mtype)
                    allowed_cats = set(self.cat.split(',')) if self.cat else None
                    for cat in cats:
                        if allowed_cats and str(cat.get('id')) not in allowed_cats:
                            continue
                        if self.category.get("field"):
                            category_filter_selected = True
                            value = params.get(self.category.get("field"), "")
                            params.update({
                                "%s" % self.category.get("field"): value + self.category.get("delimiter",
                                                                                             ' ') + cat.get("id")
                            })
                        else:
                            category_param = cat.get("param") or self.category.get("param")
                            if category_param:
                                category_filter_selected = True
                                # 某些站点（例如憨憨）使用重复的 cat[] 参数，字典值列表可由
                                # UrlUtils.combine_url 以 doseq=True 正确展开。
                                category_id = cat.get("value", cat.get("id"))
                                current_value = params.get(category_param)
                                if current_value is None:
                                    params[category_param] = category_id
                                elif isinstance(current_value, list):
                                    current_value.append(category_id)
                                else:
                                    params[category_param] = [current_value, category_id]
                            else:
                                category_filter_selected = True
                                params.update({
                                    "cat%s" % cat.get("id"): 1
                                })
                        # 分类项可以附带站点要求的额外开关，例如音乐专用展示模式。
                        if isinstance(cat, dict) and cat.get("params"):
                            params.update(cat.get("params"))
                searchurl = UrlUtils.combine_url(self.domain, torrentspath, params)
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
            # 有单独浏览路径；指定了媒体类型专用路径时不覆盖，确保音乐等专用入口可达
            if self.browse and not typed_path_selected:
                torrentspath = self.browse.get("path")
                if self.browse.get("start"):
                    start_page = int(self.browse.get("start")) + int(self.page or 0)
                    inputs_dict.update({
                        "page": start_page
                    })
            elif self.page and "{page}" not in str(torrentspath):
                # 按路径是否已带查询参数选择连接符，避免拼出两个问号的非法地址
                separator = "&" if "?" in str(torrentspath) else "?"
                torrentspath = torrentspath + f"{separator}page={self.page}"
            # 搜索Url
            searchurl = self.domain + str(torrentspath).format(**inputs_dict)

        if self.result_media_type_from_request \
                and self.mtype \
                and (typed_path_selected or category_filter_selected):
            self.requested_result_media_type = self.mtype
        else:
            self.requested_result_media_type = None
        return searchurl

    def __format_search_word(self, search_word: str) -> str:
        """
        按站点配置转换搜索关键字，用于兼容站点特殊的 IMDb ID 查询格式。
        """
        if not search_word or not isinstance(search_word, str):
            return search_word
        if re.fullmatch(r"tt\d+", search_word):
            imdbid_format = self.search.get("imdbid_format")
            if imdbid_format:
                return str(imdbid_format).format(
                    keyword=search_word,
                    imdbid=search_word,
                    imdbid_num=search_word[2:]
                )
        return search_word

    def __can_search(self) -> bool:
        """判断当前站点配置与请求媒体类型是否允许发起搜索。"""
        if self.site_media_type and self.mtype and self.site_media_type != self.mtype:
            return False
        return bool(self.search and self.domain)

    @staticmethod
    def __decode_response(response: Any) -> Any:
        """按统一编码策略将同步或异步响应投影为待解析文本。"""
        return RequestUtils.get_decoded_html_content(
            response,
            performance_mode=get_runtime_setting('ENCODING_DETECTION_PERFORMANCE_MODE'),
            confidence_threshold=get_runtime_setting('ENCODING_DETECTION_MIN_CONFIDENCE')
        )

    def get_torrents(self) -> List[dict]:
        """
        开始请求
        """
        if not self.__can_search():
            return []

        # 获取搜索URL
        searchurl = self.__get_search_url()

        logger.info(f"开始请求：{searchurl}")

        # requests请求
        ret = RequestUtils(
            ua=self.ua,
            cookies=self.cookie,
            timeout=self._timeout,
            referer=self.referer,
            proxies=self.proxies
        ).get_res(searchurl, allow_redirects=True)
        # 解析返回
        return self.parse(self.__decode_response(ret))

    async def async_get_torrents(self) -> List[dict]:
        """
        异步请求
        """
        if not self.__can_search():
            return []

        # 获取搜索URL
        searchurl = self.__get_search_url()

        logger.info(f"开始异步请求：{searchurl}")

        # httpx请求
        ret = await AsyncRequestUtils(
            ua=self.ua,
            cookies=self.cookie,
            timeout=self._timeout,
            referer=self.referer,
            proxies=self.proxies
        ).get_res(searchurl, allow_redirects=True)
        # 解析返回
        return await run_in_threadpool(
            self.parse,
            self.__decode_response(ret)
        )

    def __get_title(self, torrent: Any):
        """按站点字段配置提取并清洗种子标题。"""
        if 'title' not in self.fields:
            return
        selector = self.fields.get('title', {})
        if 'selector' in selector:
            self.torrents_info['title'] = self._safe_query(torrent, selector)
        elif 'text' in selector:
            render_dict = {}
            if "title_default" in self.fields:
                title_default_selector = self.fields.get('title_default', {})
                title_default = self._safe_query(torrent, title_default_selector)
                render_dict.update({'title_default': title_default})
            if "title_optional" in self.fields:
                title_optional_selector = self.fields.get('title_optional', {})
                title_optional = self._safe_query(torrent, title_optional_selector)
                render_dict.update({'title_optional': title_optional})
            template = self._field_templates.get("title") or Template(selector.get("text"))
            self.torrents_info['title'] = template.render(fields=render_dict)
        self.torrents_info['title'] = self.__filter_text(self.torrents_info.get('title'),
                                                         selector.get('filters'))

    def __get_description(self, torrent: Any):
        """按选择器或模板生成种子副标题。"""
        if 'description' not in self.fields:
            return
        selector = self.fields.get('description', {})
        if "selector" in selector or "selectors" in selector:
            # 对于selectors情况，需要特殊处理selector_config
            desc_selector = selector.copy()
            if "selectors" in selector and "selector" not in selector:
                desc_selector["selector"] = selector.get("selectors", "")
            self.torrents_info['description'] = self._safe_query(torrent, desc_selector)
        elif "text" in selector:
            render_dict = {}
            if "tags" in self.fields:
                tags_selector = self.fields.get('tags', {})
                tag = self._safe_query(torrent, tags_selector)
                render_dict.update({'tags': tag})
            if "subject" in self.fields:
                subject_selector = self.fields.get('subject', {})
                subject = self._safe_query(torrent, subject_selector)
                render_dict.update({'subject': subject})
            if "description_free_forever" in self.fields:
                description_free_forever_selector = self.fields.get("description_free_forever", {})
                description_free_forever = self._safe_query(torrent, description_free_forever_selector)
                render_dict.update({"description_free_forever": description_free_forever})
            if "description_normal" in self.fields:
                description_normal_selector = self.fields.get("description_normal", {})
                description_normal = self._safe_query(torrent, description_normal_selector)
                render_dict.update({"description_normal": description_normal})
            template = self._field_templates.get("description") or Template(selector.get("text"))
            self.torrents_info['description'] = template.render(fields=render_dict)
        self.torrents_info['description'] = self.__filter_text(self.torrents_info.get('description'),
                                                               selector.get('filters'))

    def __get_detail(self, torrent: Any):
        """提取并规范化种子详情页地址。"""
        if 'details' not in self.fields:
            return
        selector = self.fields.get('details', {})
        item = self._safe_query(torrent, selector)
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

    def __get_download(self, torrent: Any):
        """提取并规范化种子下载地址。"""
        if 'download' not in self.fields:
            return
        selector = self.fields.get('download', {})
        item = self._safe_query(torrent, selector)
        download_link = self.__filter_text(item, selector.get('filters'))
        if download_link:
            if not download_link.startswith("http") \
                    and not download_link.startswith("magnet"):
                _scheme, _domain = url_tools.split_netloc(self.domain)
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

    def __get_report_url(self, torrent: Any):
        """
        获取字幕举报页面链接。
        """
        if 'report' not in self.fields:
            return
        selector = self.fields.get('report', {})
        item = self._safe_query(torrent, selector)
        report_link = self.__filter_text(item, selector.get('filters'))
        if report_link:
            self.torrents_info['report_url'] = self.__normalize_link(report_link)

    def __get_language_icon(self, torrent: Any):
        """
        获取字幕语言图标链接。
        """
        if 'language_icon' not in self.fields:
            return
        selector = self.fields.get('language_icon', {})
        item = self._safe_query(torrent, selector)
        icon_link = self.__filter_text(item, selector.get('filters'))
        if icon_link:
            self.torrents_info['language_icon'] = self.__normalize_link(icon_link)

    def __get_imdbid(self, torrent: Any):
        """提取种子关联的 IMDb 标识。"""
        if "imdbid" not in self.fields:
            return
        selector = self.fields.get('imdbid', {})
        item = self._safe_query(torrent, selector)
        self.torrents_info['imdbid'] = self.__filter_text(item, selector.get('filters'))

    def __get_size(self, torrent: Any):
        """提取种子大小并转换为字节数。"""
        if 'size' not in self.fields:
            return
        selector = self.fields.get('size', {})
        item = self._safe_query(torrent, selector)
        if item is not None and item != "":
            size_val = item.replace("\n", "").strip()
            size_val = self.__filter_text(size_val,
                                          selector.get('filters'))
            self.torrents_info['size'] = size_tools.parse_size(size_val)
        else:
            self.torrents_info['size'] = 0

    def __get_leechers(self, torrent: Any):
        """提取种子的下载人数。"""
        if 'leechers' not in self.fields:
            return
        selector = self.fields.get('leechers', {})
        item = self._safe_query(torrent, selector)
        if item:
            peers_val = item.split("/")[0]
            peers_val = peers_val.replace(",", "")
            peers_val = self.__filter_text(peers_val, selector.get('filters'))
            self.torrents_info['peers'] = int(peers_val) if peers_val and peers_val.isdigit() else 0
        else:
            self.torrents_info['peers'] = 0

    def __get_seeders(self, torrent: Any):
        """提取种子的做种人数。"""
        if 'seeders' not in self.fields:
            return
        selector = self.fields.get('seeders', {})
        item = self._safe_query(torrent, selector)
        if item:
            seeders_val = item.split("/")[0]
            seeders_val = seeders_val.replace(",", "")
            seeders_val = self.__filter_text(seeders_val, selector.get('filters'))
            self.torrents_info['seeders'] = int(seeders_val) if seeders_val and seeders_val.isdigit() else 0
        else:
            self.torrents_info['seeders'] = 0

    def __get_grabs(self, torrent: Any):
        """提取种子的完成人数。"""
        if 'grabs' not in self.fields:
            return
        selector = self.fields.get('grabs', {})
        item = self._safe_query(torrent, selector)
        if item is not None and item != "":
            grabs_val = item.split("/")[0]
            grabs_val = grabs_val.replace(",", "")
            grabs_val = self.__filter_text(grabs_val, selector.get('filters'))
            self.torrents_info['grabs'] = int(grabs_val) if grabs_val and grabs_val.isdigit() else 0
        else:
            self.torrents_info['grabs'] = 0

    def __get_pubdate(self, torrent: Any):
        """按站点日期配置提取并规范化发布时间。"""
        if 'date_added' not in self.fields and 'date' not in self.fields:
            return
        selector = self.fields.get('date_added', {})
        pubdate_str = self._safe_query(torrent, selector)
        if not pubdate_str:
            selector = self.fields.get('date', {})
            pubdate_str = self.__get_date(torrent, selector)
        if pubdate_str:
            pubdate_str = pubdate_str.replace('\n', ' ').strip()
        self.torrents_info['pubdate'] = self.__filter_text(pubdate_str, selector.get('filters'))
        if self.torrents_info.get('pubdate'):
            try:
                if isinstance(self.torrents_info['pubdate'], datetime.datetime):
                    self.torrents_info['pubdate'] = self.torrents_info['pubdate'].strftime('%Y-%m-%d %H:%M:%S')
                else:
                    datetime.datetime.strptime(str(self.torrents_info['pubdate']), '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                self.torrents_info['pubdate'] = time_tools.normalize_datetime(str(self.torrents_info['pubdate']))
            if self.__is_invalid_pubdate_text(self.torrents_info.get('pubdate')):
                self.torrents_info.pop('pubdate', None)

    def __get_date(self, torrent: Any, selector: dict) -> Optional[str]:
        """
        从 date 模板解析发布时间。
        """
        if not selector:
            return None
        if "selector" in selector:
            return self._safe_query(torrent, selector)
        template_text = selector.get("text")
        if not template_text:
            return None

        render_dict = {}
        for field_name in ("date_elapsed", "date_added"):
            field_selector = self.fields.get(field_name, {})
            field_value = self._safe_query(torrent, field_selector)
            if not field_value:
                field_value = self.__get_date_from_cell(torrent, field_selector)
            render_dict[field_name] = field_value
        if not any(render_dict.values()):
            return None

        template = self._field_templates.get("date") or Template(template_text)
        pubdate_str = template.render(fields=render_dict)
        if pubdate_str == "now" or self.__is_relative_pubdate_text(pubdate_str):
            return None
        return pubdate_str

    def __get_date_from_cell(self, torrent: Any, selector: dict) -> Optional[str]:
        """
        兼容 NexusPHP 发生时间模式下不再渲染 span 的时间单元格。
        """
        cell_selector = self.__date_cell_selector(selector.get("selector"))
        if not cell_selector:
            return None
        return self._safe_query(torrent, {"selector": cell_selector})

    @staticmethod
    def __date_cell_selector(selector: Optional[str]) -> Optional[str]:
        """
        从时间字段选择器推导父级 td 选择器。
        """
        if not selector:
            return None
        selector = selector.strip()
        if not selector or "> span" not in selector:
            return None
        return selector.split("> span", 1)[0].strip()

    @staticmethod
    def __is_relative_pubdate_text(pubdate: Optional[str]) -> bool:
        """
        判断是否为相对时间，避免写入不可排序的发布时间。
        """
        if not pubdate:
            return False
        text = str(pubdate).strip().lower()
        if re.search(r"\d{4}[-/年]\d{1,2}", text):
            return False
        if "ago" in text:
            return True
        return bool(re.search(r"\d+\s*(秒|分钟|分|小时|天|周|月|年)", text))

    @classmethod
    def __is_invalid_pubdate_text(cls, pubdate: Optional[str]) -> bool:
        """
        判断是否为不可用发布时间，避免列错位文本污染 pubdate。
        """
        if not pubdate:
            return True
        text = str(pubdate).strip()
        if text.lower() == "now" or text == "0":
            return True
        if cls.__is_relative_pubdate_text(text):
            return True
        try:
            datetime.datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
            return False
        except (ValueError, TypeError):
            return True

    def __get_date_elapsed(self, torrent: Any):
        """提取相对发布时间对应的已过秒数。"""
        if 'date_elapsed' not in self.fields:
            return
        selector = self.fields.get('date_elapsed', {})
        date_elapsed = self._safe_query(torrent, selector)
        self.torrents_info['date_elapsed'] = self.__filter_text(date_elapsed, selector.get('filters'))

    def __get_downloadvolumefactor(self, torrent: Any):
        """按促销标签解析种子下载系数。"""
        selector = self.fields.get('downloadvolumefactor', {})
        if not selector:
            return
        self.torrents_info['downloadvolumefactor'] = 1
        if 'case' in selector:
            for downloadvolumefactorselector in list(selector.get('case', {}).keys()):
                downloadvolumefactor = torrent(downloadvolumefactorselector)
                try:
                    if len(downloadvolumefactor) > 0:
                        self.torrents_info['downloadvolumefactor'] = selector.get('case', {}).get(
                            downloadvolumefactorselector)
                        break
                finally:
                    downloadvolumefactor.clear()
                    del downloadvolumefactor
        elif "selector" in selector:
            item = self._safe_query(torrent, selector)
            if item:
                downloadvolumefactor = re.search(r'(\d+\.?\d*)', item)
                if downloadvolumefactor:
                    self.torrents_info['downloadvolumefactor'] = int(downloadvolumefactor.group(1))

    def __get_uploadvolumefactor(self, torrent: Any):
        """按促销标签解析种子上传系数。"""
        selector = self.fields.get('uploadvolumefactor', {})
        if not selector:
            return
        self.torrents_info['uploadvolumefactor'] = 1
        if 'case' in selector:
            for uploadvolumefactorselector in list(selector.get('case', {}).keys()):
                uploadvolumefactor = torrent(uploadvolumefactorselector)
                try:
                    if len(uploadvolumefactor) > 0:
                        self.torrents_info['uploadvolumefactor'] = selector.get('case', {}).get(
                            uploadvolumefactorselector)
                        break
                finally:
                    uploadvolumefactor.clear()
                    del uploadvolumefactor
        elif "selector" in selector:
            item = self._safe_query(torrent, selector)
            if item:
                uploadvolumefactor = re.search(r'(\d+\.?\d*)', item)
                if uploadvolumefactor:
                    self.torrents_info['uploadvolumefactor'] = int(uploadvolumefactor.group(1))

    def __get_labels(self, torrent: Any):
        """提取并清洗种子标签列表。"""
        if 'labels' not in self.fields:
            return
        selector = self.fields.get('labels', {})
        if not selector.get('selector'):
            self.torrents_info['labels'] = []
            return

        # labels需要特殊处理，因为它返回的是列表
        labels = torrent(selector.get("selector", "")).clone()
        try:
            self.__remove(labels, selector)
            items = self.__attribute_or_text(labels, selector)
            if items:
                self.torrents_info['labels'] = [item for item in items if item]
            else:
                self.torrents_info['labels'] = []
        finally:
            labels.clear()
            del labels

    def __get_free_date(self, torrent: Any):
        """提取种子免费促销截止时间。"""
        if 'freedate' not in self.fields:
            return
        selector = self.fields.get('freedate', {})
        freedate = self._safe_query(torrent, selector)
        self.torrents_info['freedate'] = self.__filter_text(freedate, selector.get('filters'))

    def __get_hit_and_run(self, torrent: Any):
        """判定种子是否带有 Hit and Run 要求。"""
        if 'hr' not in self.fields:
            return
        selector = self.fields.get('hr', {})
        hit_and_run = torrent(selector.get('selector', ''))
        try:
            if hit_and_run:
                self.torrents_info['hit_and_run'] = True
            else:
                self.torrents_info['hit_and_run'] = False
        finally:
            hit_and_run.clear()
            del hit_and_run

    def __get_category(self, torrent: Any):
        """将站点分类字段投影为统一媒体类型。"""
        if self.requested_result_media_type:
            self.torrents_info['category'] = self.requested_result_media_type.value
            return
        if 'category' not in self.fields:
            if self.site_media_type:
                self.torrents_info['category'] = self.site_media_type.value
            return
        selector = self.fields.get('category', {})
        category_value = self._safe_query(torrent, selector)
        category_value = self.__filter_text(category_value, selector.get('filters'))
        resolved_type = resolve_category_media_type(category_value, self.category)
        if resolved_type == MediaType.UNKNOWN and self.site_media_type:
            resolved_type = self.site_media_type
        self.torrents_info['category'] = resolved_type.value

    def __apply_result_media_type(self, torrents: Optional[List[dict]]) -> List[dict]:
        """
        为解析结果补充统一媒体类型。

        显式请求类型始终覆盖结果；纯媒体站点在分类缺失或未知时兜底，
        保持 Python 与 Rust 两条解析路径的分类行为一致。
        """
        results = torrents or []
        if self.requested_result_media_type:
            for torrent in results:
                torrent["category"] = self.requested_result_media_type.value
            return results
        if not self.site_media_type:
            return results
        for torrent in results:
            if torrent.get("category") in (
                    None,
                    "",
                    MediaType.UNKNOWN,
                    MediaType.UNKNOWN.value,
            ):
                torrent["category"] = self.site_media_type.value
        return results

    def __get_subtitle_field(self, torrent: Any, field_name: str):
        """
        按配置读取字幕字段。
        """
        selector = self.fields.get(field_name, {})
        if not selector:
            return
        item = self._safe_query(torrent, selector)
        value = self.__filter_text(item, selector.get('filters'))
        if value is not None:
            self.torrents_info[field_name] = value

    def __fill_subtitle_ids(self):
        """
        从字幕下载链接中补充站点种子ID和字幕ID。
        """
        enclosure = self.torrents_info.get("enclosure")
        if not enclosure:
            return
        query_params = parse_qs(urlparse(enclosure).query)
        if not self.torrents_info.get("torrent_id"):
            torrent_id = query_params.get("torrentid") or query_params.get("torrent_id")
            if torrent_id:
                self.torrents_info["torrent_id"] = torrent_id[0]
        if not self.torrents_info.get("subtitle_id"):
            subtitle_id = query_params.get("subid") or query_params.get("subtitle")
            if subtitle_id:
                self.torrents_info["subtitle_id"] = subtitle_id[0]

    def __normalize_link(self, link: Optional[str]) -> Optional[str]:
        """
        将站点相对链接转换为绝对链接。
        """
        if not link:
            return None
        parsed_link = urlparse(link)
        if parsed_link.scheme:
            return link
        if not link.startswith("http"):
            if link.startswith("//"):
                return self.domain.split(":")[0] + ":" + link
            if link.startswith("/"):
                return self.domain + link[1:]
            return self.domain + link
        return link

    def _safe_query(self, torrent: Any, selector_config: Optional[dict]) -> Optional[str]:
        """
        安全地执行PyQuery查询并自动清理资源
        :param torrent: PyQuery对象
        :param selector_config: 选择器配置
        :return: 处理后的结果
        """
        if not selector_config or not selector_config.get('selector'):
            return None

        should_clone = bool(selector_config.get("remove"))
        query_obj = torrent(selector_config.get('selector', ''))
        if should_clone:
            query_obj = query_obj.clone()
        try:
            self.__remove(query_obj, selector_config)
            items = self.__attribute_or_text(query_obj, selector_config)
            return self.__index(items, selector_config)
        finally:
            if should_clone:
                query_obj.clear()
            del query_obj

    def get_info(self, torrent: Any) -> dict:
        """
        解析单条种子数据
        """
        # 每次调用时重新初始化，避免数据累积
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
            # 返回当前种子信息的副本，而不是引用
            return self.torrents_info.copy() if self.torrents_info else {}
        except Exception as err:
            logger.error("%s 搜索出现错误：%s" % (self.indexername, str(err)))
            return {}
        finally:
            self.torrents_info.clear()

    def get_subtitle_info(self, subtitle: Any) -> dict:
        """
        解析单条字幕数据。
        """
        self.torrents_info = {}
        try:
            self.__get_title(subtitle)
            self.__get_description(subtitle)
            self.__get_detail(subtitle)
            self.__get_download(subtitle)
            self.__get_size(subtitle)
            self.__get_pubdate(subtitle)
            self.__get_date_elapsed(subtitle)
            self.__get_grabs(subtitle)
            self.__get_language_icon(subtitle)
            self.__get_report_url(subtitle)
            for field_name in (
                    "language", "uploader", "torrent_id", "subtitle_id", "file_name"
            ):
                self.__get_subtitle_field(subtitle, field_name)
            self.__fill_subtitle_ids()
            if not self.torrents_info.get("title") or not self.torrents_info.get("enclosure"):
                return {}
            return self.torrents_info.copy() if self.torrents_info else {}
        except Exception as err:
            logger.error("%s 字幕搜索出现错误：%s" % (self.indexername, str(err)))
            return {}
        finally:
            self.torrents_info.clear()

    @staticmethod
    def __filter_text(text: Optional[str], filters: Optional[List[dict]]) -> str:
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
                    parsed_url = urlparse(str(text))
                    query_params = parse_qs(parsed_url.query)
                    param_value = query_params.get(args)
                    text = param_value[0] if param_value else ''
            except Exception as err:
                logger.debug(f'过滤器 {method_name} 处理失败：{str(err)} - {traceback.format_exc()}')
        return text.strip() if isinstance(text, str) else text

    @staticmethod
    def __remove(item: Any, selector: Optional[dict]):
        """
        移除元素
        """
        if selector and "remove" in selector:
            removelist = selector.get('remove', '').split(', ')
            for v in removelist:
                item.remove(v)

    @staticmethod
    def __attribute_or_text(item: Any, selector: Optional[dict]) -> list:
        """
        获取查询结果的属性或文本列表。
        """
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
    def __index(items: Optional[list], selector: Optional[dict]) -> Optional[str]:
        """
        按配置下标读取查询结果。
        """
        if not items:
            return None
        if selector:
            if "contents" in selector \
                    and len(items) > int(selector.get("contents")):
                item = items[0].split("\n")[selector.get("contents")]
            elif "index" in selector \
                    and len(items) > int(selector.get("index")):
                item = items[int(selector.get("index"))]
            else:
                item = items[0]
        else:
            item = items[0]
        return item

    @staticmethod
    def __is_login_or_permission_page(html_doc: Any) -> bool:
        """
        判断返回内容是否是登录或权限提示页。
        """
        title = (html_doc("title").text() or "").strip()
        page_text = " ".join((html_doc.text() or "").split())[:1000]
        if title == "登录" or ":: 登录" in title:
            return True
        return any(
            marker in page_text
            for marker in (
                "未登录",
                "登录 / 注册",
                "必须在登录后才能访问",
                "你需要启用cookies才能登录",
            )
        )

    def parse(self, html_text: str) -> List[dict]:
        """
        解析整个页面
        """
        if not html_text:
            self.is_error = True
            return []

        try:
            status_doc = PyQuery(html_text)
            if self.__is_login_or_permission_page(status_doc):
                self.is_error = True
                logger.warn(f"错误：{self.indexername} 返回登录或权限提示页")
                return []
        except Exception as err:
            self.is_error = True
            logger.warn(f"错误：{self.indexername} {str(err)}")
            return []
        finally:
            if 'status_doc' in locals():
                status_doc.clear()  # noqa
                del status_doc

        if self.search_type == "subtitles":
            rust_subtitles = rust_accel.parse_indexer_subtitles(
                html_text=html_text,
                domain=self.domain,
                list_config=self.list,
                fields=self.fields,
                result_num=self.result_num
            )
            if rust_subtitles is not None:
                return rust_subtitles
        else:
            rust_torrents = rust_accel.parse_indexer_torrents(
                html_text=html_text,
                domain=self.domain,
                list_config=self.list,
                fields=self.fields,
                category=self.category,
                result_num=self.result_num
            )
            if rust_torrents is not None:
                return self.__apply_result_media_type(rust_torrents)

        # 清空旧结果
        self.torrents_info_array = []
        html_doc = None
        try:
            # 解析站点文本对象
            html_doc = PyQuery(html_text)
            # 种子筛选器
            torrents_selector = self.list.get('selector', '')
            rows = html_doc(torrents_selector)
            # 遍历种子html列表
            for i, torn in enumerate(rows):
                if i >= int(self.result_num):
                    break
                # 创建临时PyQuery对象进行解析
                torrent_query = PyQuery(torn)
                try:
                    # 直接获取种子信息，避免深拷贝
                    if self.search_type == "subtitles":
                        torrent_info = self.get_subtitle_info(torrent_query)
                    else:
                        torrent_info = self.get_info(torrent_query)
                    if torrent_info:
                        # 浅拷贝即可，减少内存使用
                        self.torrents_info_array.append(torrent_info)
                finally:
                    # 显式删除临时PyQuery对象
                    torrent_query.clear()
                    del torrent_query
            # 返回数组的副本，防止被后续清理操作影响
            return self.__apply_result_media_type(self.torrents_info_array.copy())
        except Exception as err:
            self.is_error = True
            logger.warn(f"错误：{self.indexername} {str(err)}")
            return []
        finally:
            # 清理种子缓存
            self.torrents_info_array.clear()
            # 清理HTML文档对象
            if html_doc is not None:
                html_doc.clear()
                del html_doc
            # 清理html_text引用
            del html_text
