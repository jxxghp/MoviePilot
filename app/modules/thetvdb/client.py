"""
TVDB v4 客户端，保留原始 JSON 响应与历史查询方法兼容性。
"""

__author__ = "Weylin Wagnon"
__version__ = "1.0.12"

import json
import urllib.parse
from collections.abc import Callable
from http import HTTPStatus
from typing import Protocol, TypeAlias, TypeVar, Union, cast

from app.adapters.network.http import RequestUtils
from app.runtime.cache import cached
from app.runtime.settings import get_runtime_setting

# 保留接口 data 原始 JSON 与 304 状态字典，不伪装成固定列表或对象。
TVDBJson: TypeAlias = Union[dict[str, "TVDBJson"], list["TVDBJson"], str, int, float, bool, None]
_RequestFunction = TypeVar("_RequestFunction", bound=Callable[..., object])


class _ClearableRequest(Protocol):
    """
    请求缓存装饰器附加的同步清理入口。
    """

    def cache_clear(self) -> None:
        """
        清空当前请求函数共享的缓存。
        """
        ...


def _cache_request(function: _RequestFunction) -> _RequestFunction:
    """
    缓存保持原始参数和返回值，类型桥接不改变缓存配置。
    """
    return cached(
        maxsize=get_runtime_setting('CONF').tmdb,
        ttl=get_runtime_setting('CONF').meta,
        skip_none=True,
    )(function)


class Auth:
    """
    执行 TVDB 登录并保存服务端返回的访问令牌。
    """

    def __init__(self, url: str, apikey: str, pin: str = "", proxy: dict[str, str] | None = None, timeout: int = 15) -> None:
        """
        立即向登录接口提交应用密钥和可选 PIN。

        初始化即发起网络请求，失败不会创建可用认证对象。

        :param url: 完整登录接口地址
        :param apikey: TVDB v4 应用密钥
        :param pin: 订阅用户 PIN，空字符串时不发送该字段
        :param proxy: HTTP 代理配置，None 使用无显式代理配置的请求
        :param timeout: 单次请求超时秒数，默认 15 秒
        :raises Exception: 网络、响应解析或认证失败
        """
        login_info = {"apikey": apikey}
        if pin != "":
            login_info["pin"] = pin

        login_info_bytes = json.dumps(login_info, indent=2)

        try:
            req_utils = RequestUtils(proxies=proxy, timeout=timeout)
            response = req_utils.post_res(
                url=url,
                data=login_info_bytes,
                headers={"Content-Type": "application/json"}
            )

            if response and response.status_code == 200:
                result = response.json()
                self.token: TVDBJson = result["data"]["token"]
            else:
                if response is not None:
                    try:
                        error_data = response.json()
                        error_msg = f"Code: {response.status_code}, {error_data.get('message', '未知错误')}"
                    except Exception as err:
                        error_msg = f"Code: {response.status_code}, 响应解析失败：{err}"
                else:
                    error_msg = "网络连接失败，未收到响应"
                raise Exception(error_msg)
        except Exception as e:
            raise Exception(f"TVDB认证失败: {str(e)}")

    def get_token(self) -> TVDBJson:
        """
        读取登录响应中的原始令牌值。

        :return: 服务端 data.token 的原始 JSON 值，不在本地重新验证
        """
        return self.token


class Request:
    """
    发送带认证的 TVDB 请求并记录最近一次实际响应的分页链接。
    """

    def __init__(self, auth_token: TVDBJson, proxy: dict[str, str] | None = None, timeout: int = 15) -> None:
        """
        保存认证令牌、代理和超时配置，不发起请求。

        :param auth_token: 登录接口返回的令牌，发送时写入 Bearer 认证头
        :param proxy: HTTP 代理配置，None 使用无显式代理配置的请求
        :param timeout: 单次请求超时秒数，默认 15 秒
        """
        self.auth_token = auth_token
        self.links: TVDBJson = None
        self.proxy = proxy
        self.timeout = timeout

    @_cache_request
    def make_request(self, url: str, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        发送带缓存的认证 GET 请求，保留业务数据的原始 JSON 形态。

        仅实际收到成功且含 data 的响应时更新 links；缓存命中与 304 不更新分页链接。

        :param url: 完整 TVDB API 地址
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 响应 data 原始值；HTTP 304 返回包含 code 和 message 的状态映射
        :raises ValueError: 网络失败、HTTP 错误、业务失败或响应解析失败
        """
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        if if_modified_since:
            headers["If-Modified-Since"] = str(if_modified_since)

        try:
            req_utils = RequestUtils(proxies=self.proxy, timeout=self.timeout)
            response = req_utils.get_res(url=url, headers=headers)

            if response is None:
                raise ValueError(f"获取 {url} 失败\n  网络连接失败")

            if response.status_code == HTTPStatus.NOT_MODIFIED:
                return {
                    "code": HTTPStatus.NOT_MODIFIED.real,
                    "message": "Not-Modified",
                }

            if response.status_code == 200:
                result = response.json()
                data: TVDBJson = result.get("data", None)
                if data is not None and result.get("status", "failure") != "failure":
                    self.links = result.get("links", None)
                    return data

                msg = result.get("message", "未知错误")
                raise ValueError(f"获取 {url} 失败\n  {str(msg)}")
            else:
                try:
                    error_data = response.json()
                    msg = error_data.get("message", f"HTTP {response.status_code}")
                except Exception as err:
                    msg = f"HTTP {response.status_code} {err}"
                raise ValueError(f"获取 {url} 失败\n  {str(msg)}")

        except Exception as e:
            if isinstance(e, ValueError):
                raise
            raise ValueError(f"获取 {url} 失败\n  {str(e)}")


class Url:
    """
    组装 TVDB v4 路径与查询参数，不发起请求。
    """

    def __init__(self) -> None:
        """
        初始化 TVDB v4 地址前缀。
        """
        self.base_url = "https://api4.thetvdb.com/v4/"

    def construct(self, url_sect: str, url_id: int | str | None = None,
                  url_subsect: str | None = None, url_lang: str | None = None, **kwargs: object) -> str:
        """
        拼接接口路径，并对非 None 查询参数进行 URL 编码。

        :param url_sect: 接口主路径，如 series 或 movies
        :param url_id: 可选资源标识，假值不加入路径
        :param url_subsect: 可选子路径，如 extended 或 translations
        :param url_lang: 可选路径语言代码
        :param kwargs: 查询参数，None 被移除，其他值交给 urlencode
        :return: 完整接口 URL；路径片段原样拼接，不执行转义
        """
        url = self.base_url + url_sect
        if url_id:
            url += "/" + str(url_id)
        if url_subsect:
            url += "/" + url_subsect
        if url_lang:
            url += "/" + url_lang
        if kwargs:
            params = {var: val for var, val in kwargs.items() if val is not None}
            if params:
                url += "?" + urllib.parse.urlencode(params)
        return url


class TVDB:
    """
    TVDB v4 接口门面，统一登录、地址构造和请求缓存。

    各查询返回服务端 data 原始形态，条件请求可能返回 304 状态映射。
    网络或业务失败由请求层抛出 ValueError，门面不隐藏异常。
    """

    def __init__(self, apikey: str, pin: str = "", proxy: dict[str, str] | None = None, timeout: int = 15) -> None:
        """
        创建地址、认证与请求对象，构造期间立即登录。

        :param apikey: TVDB v4 应用密钥
        :param pin: 订阅用户 PIN，空字符串时不发送该字段
        :param proxy: HTTP 代理配置，None 使用无显式代理配置的请求
        :param timeout: 单次请求超时秒数，默认 15 秒
        :raises Exception: 登录请求或认证失败
        """
        self.url = Url()
        login_url = self.url.construct("login")
        self.auth = Auth(login_url, apikey, pin, proxy, timeout)
        auth_token = self.auth.get_token()
        self.request = Request(auth_token, proxy, timeout)

    def get_req_links(self) -> TVDBJson:
        """
        读取最近一次实际成功响应记录的分页链接。

        :return: 原始 links 值；初始为 None；缓存命中和 HTTP 304 不刷新该值
        """
        return self.request.links

    def get_artwork_statuses(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回艺术图状态列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("artwork/statuses", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_artwork_types(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回艺术图类型列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("artwork/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_artwork(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个艺术图信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("artwork", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_artwork_extended(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个艺术图的扩展信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("artwork", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_awards(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回奖项列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("awards", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_award(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个奖项信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("awards", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_award_extended(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个奖项的扩展信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("awards", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_award_categories(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回奖项类别列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("awards/categories", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_award_category(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个奖项类别信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("awards/categories", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_award_category_extended(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个奖项类别的扩展信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("awards/categories", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_content_ratings(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回内容分级列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("content/ratings", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_countries(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回国家列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("countries", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_companies(self, page: int | None = None, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回公司列表，支持服务端分页。

        :param page: 服务端页码，原样传入；None 表示使用接口默认分页
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("companies", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_company_types(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回公司类型列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("companies/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_company(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个公司信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("companies", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_series(self, page: int | None = None, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回剧集列表，支持服务端分页。

        :param page: 服务端页码，原样传入；None 表示使用接口默认分页
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("series", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_series(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个剧集信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("series", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_series_by_slug(self, slug: str, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        通过 slug (别名) 返回单个剧集信息的字典。

        :param slug: 目标实体的网页路径别名，直接作为路径片段
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("series/slug", slug, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_series_extended(self, id: int, meta: str | None = None, short: bool = False, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个剧集的扩展信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param short: 是否请求精简扩展数据，布尔值原样编码为查询参数
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("series", id, "extended", meta=meta, short=short)
        return self.request.make_request(url, if_modified_since)

    def get_series_episodes(self, id: int, season_type: str = "default", page: int = 0,
                            lang: str | None = None, meta: str | None = None, if_modified_since: str | bool | None = None, **kwargs: object) -> TVDBJson:
        """
        返回指定剧集和季类型的各集信息字典，支持分页与语言选择。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param season_type: 电视剧分季顺序类型，默认 default
        :param page: 服务端页码，原样传入；None 表示使用接口默认分页
        :param lang: TVDB 语言代码，直接传给对应路径或查询字段
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :param kwargs: 其他服务端查询筛选键，原样编码并过滤 None 值
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct(
            "series", id, "episodes/" + season_type, lang, page=page, meta=meta, **kwargs
        )
        return self.request.make_request(url, if_modified_since)

    def get_series_translation(self, id: int, lang: str, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回剧集的指定语言翻译信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param lang: TVDB 语言代码，直接传给对应路径或查询字段
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("series", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_series_artworks(self, id: int, lang: str, type: int | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回包含艺术图数组的剧集记录 (可指定语言和类型)。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param lang: TVDB 语言代码，直接传给对应路径或查询字段
        :param type: 图片类型数字标识，None 表示不限制
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("series", id, "artworks", lang=lang, type=type)
        return self.request.make_request(url, if_modified_since)

    def get_series_next_aired(self, id: int, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回剧集的下一播出信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("series", id, "nextAired")
        return self.request.make_request(url, if_modified_since)

    def get_all_movies(self, page: int | None = None, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回电影列表，支持服务端分页。

        :param page: 服务端页码，原样传入；None 表示使用接口默认分页
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("movies", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_movie(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个电影信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("movies", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_movie_by_slug(self, slug: str, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        通过 slug (别名) 返回单个电影信息的字典。

        :param slug: 目标实体的网页路径别名，直接作为路径片段
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("movies/slug", slug, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_movie_extended(self, id: int, meta: str | None = None, short: bool = False, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回电影的扩展信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param short: 是否请求精简扩展数据，布尔值原样编码为查询参数
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("movies", id, "extended", meta=meta, short=short)
        return self.request.make_request(url, if_modified_since)

    def get_movie_translation(self, id: int, lang: str, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回电影的指定语言翻译信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param lang: TVDB 语言代码，直接传给对应路径或查询字段
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("movies", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_seasons(self, page: int | None = None, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回季列表，支持服务端分页。

        :param page: 服务端页码，原样传入；None 表示使用接口默认分页
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("seasons", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_season(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单季信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("seasons", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_season_extended(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单季的扩展信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("seasons", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_season_types(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回季类型列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("seasons/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_season_translation(self, id: int, lang: str, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回季的指定语言翻译信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param lang: TVDB 语言代码，直接传给对应路径或查询字段
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("seasons", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_episodes(self, page: int | None = None, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回集列表，支持服务端分页。

        :param page: 服务端页码，原样传入；None 表示使用接口默认分页
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("episodes", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_episode(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单集信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("episodes", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_episode_extended(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单集的扩展信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("episodes", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_episode_translation(self, id: int, lang: str, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单集的指定语言翻译信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param lang: TVDB 语言代码，直接传给对应路径或查询字段
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("episodes", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    # 兼容旧函数名。
    get_episodes_translation = get_episode_translation

    def get_all_genders(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回性别列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("genders", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_genres(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回类型（流派）列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("genres", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_genre(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个类型（流派）信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("genres", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_languages(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回语言列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("languages", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_people(self, page: int | None = None, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回人物列表，支持服务端分页。

        :param page: 服务端页码，原样传入；None 表示使用接口默认分页
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("people", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_person(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个人物信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("people", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_person_extended(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个人物的扩展信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("people", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_person_translation(self, id: int, lang: str, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回人物的指定语言翻译信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param lang: TVDB 语言代码，直接传给对应路径或查询字段
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("people", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_character(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回角色信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("characters", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_people_types(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回人物类型列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("people/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    # 兼容旧函数名
    get_all_people_types = get_people_types

    def get_source_types(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回来源类型列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("sources/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    # 兼容旧函数名
    get_all_sourcetypes = get_source_types

    def get_updates(self, since: int, *, url_id: int | str | None = None,
                    url_subsect: str | None = None, url_lang: str | None = None,
                    **kwargs: object) -> TVDBJson:
        """
        返回更新响应，兼容路径扩展参数与任意查询过滤键。

        :param since: 仅查询此 Unix 秒级时间戳之后的更新
        :param url_id: 兼容扩展路径的资源标识，None 时省略
        :param url_subsect: 兼容扩展路径的子段，None 时省略
        :param url_lang: 兼容扩展路径的语言段，None 时省略
        :param kwargs: 其他服务端查询筛选键，原样编码并过滤 None 值
        :return: 服务端 data 原始值
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("updates", url_id, url_subsect, url_lang, since=since, **kwargs)
        return self.request.make_request(url)

    def get_all_tag_options(self, page: int | None = None, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回标签选项列表，支持服务端分页。

        :param page: 服务端页码，原样传入；None 表示使用接口默认分页
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("tags/options", page=page, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_tag_option(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个标签选项信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("tags/options", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_all_lists(self, page: int | None = None, meta: str | None = None) -> TVDBJson:
        """
        返回所有公开的列表信息，支持服务端分页。

        :param page: 服务端页码，原样传入；None 表示使用接口默认分页
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :return: 服务端 data 原始值
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("lists", page=page, meta=meta)
        return self.request.make_request(url)

    def get_list(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个列表信息的字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("lists", id, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_list_by_slug(self, slug: str, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        通过 slug (别名) 返回单个列表信息的字典。

        :param slug: 目标实体的网页路径别名，直接作为路径片段
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("lists/slug", slug, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_list_extended(self, id: int, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回单个列表的扩展信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("lists", id, "extended", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_list_translation(self, id: int, lang: str, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回列表的指定语言翻译信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :param lang: TVDB 语言代码，直接传给对应路径或查询字段
        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("lists", id, "translations", lang, meta=meta)
        return self.request.make_request(url, if_modified_since)

    def get_inspiration_types(self, meta: str | None = None, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回灵感类型列表。

        :param meta: 服务端可选元数据展开参数，None 表示不传
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("inspiration/types", meta=meta)
        return self.request.make_request(url, if_modified_since)

    def search(self, query: str, *, url_id: int | str | None = None,
               url_subsect: str | None = None, url_lang: str | None = None,
               **kwargs: object) -> TVDBJson:
        """
        按查询字符串搜索，兼容路径扩展参数并保留原始 JSON 响应。

        :param query: 搜索关键词，编码为 query 查询参数
        :param url_id: 兼容扩展路径的资源标识，None 时省略
        :param url_subsect: 兼容扩展路径的子段，None 时省略
        :param url_lang: 兼容扩展路径的语言段，None 时省略
        :param kwargs: 其他服务端查询筛选键，原样编码并过滤 None 值
        :return: 服务端 data 原始值
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("search", url_id, url_subsect, url_lang, query=query, **kwargs)
        return self.request.make_request(url)

    def search_by_remote_id(self, remoteid: str) -> TVDBJson:
        """
        通过外部 ID 精确匹配搜索，并返回结果列表。

        :param remoteid: 外部数据库原生标识，如 IMDb 标识
        :return: 服务端 data 原始值
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("search/remoteid", remoteid)
        return self.request.make_request(url)

    def get_tags(self, slug: str, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回具有指定 slug 的标签实体信息字典 (此方法基于的 /entities/{slug} 端点非标准，请谨慎使用)。

        :param slug: 目标实体的网页路径别名，直接作为路径片段
        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("entities", url_subsect=slug)
        return self.request.make_request(url, if_modified_since)

    def get_entities_types(self, if_modified_since: str | bool | None = None) -> TVDBJson:
        """
        返回可用的实体类型列表。

        :param if_modified_since: 条件请求时间文本；假值不发送请求头，真值会转为字符串
        :return: 服务端 data 原始值；HTTP 304 时返回状态映射
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("entities")
        return self.request.make_request(url, if_modified_since)

    def get_user_by_id(self, id: int) -> TVDBJson:
        """
        通过用户 ID 返回用户信息字典。

        :param id: 待查询实体的 TVDB 数字标识，所属实体由当前接口决定
        :return: 服务端 data 原始值
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("user", id)
        return self.request.make_request(url)

    def get_user(self) -> TVDBJson:
        """
        返回当前认证的用户信息字典。

        :return: 服务端 data 原始值
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct("user")
        return self.request.make_request(url)

    def get_user_favorites(self) -> TVDBJson:
        """
        返回当前认证用户的收藏夹信息字典。

        :return: 服务端 data 原始值
        :raises ValueError: 网络、HTTP 或业务请求失败
        """
        url = self.url.construct('user/favorites')
        return self.request.make_request(url)

    def clear_cache(self) -> None:
        """
        清除缓存。
        """
        cast(_ClearableRequest, self.request.make_request).cache_clear()
