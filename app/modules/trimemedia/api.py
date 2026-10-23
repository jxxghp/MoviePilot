"""
飞牛影视 API 协议及媒体库数据结构，负责登录、请求签名与条目转换。
"""

import hashlib
import json
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional, Union
from urllib.parse import quote

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


@dataclass
class User:
    """
    飞牛影视用户身份与管理员标记。

    :param guid: 服务端用户唯一标识
    :param username: 登录用户名
    :param is_admin: 管理员标记，1 表示管理员，默认 0
    """
    guid: str
    username: str
    is_admin: int = 0


class Category(Enum):
    """
    飞牛媒体库的内容分类，未知分类统一归入其他。
    """
    # 电影
    MOVIE = "Movie"
    # 电视剧
    TV = "TV"
    # 混合内容库
    MIX = "Mix"
    # 未识别或不受支持的媒体库分类
    OTHERS = "Others"

    @classmethod
    def _missing_(cls, value: object) -> Optional["Category"]:
        """
        为未知媒体库分类提供兼容回退。

        :param value: 服务端返回的未识别分类值
        :return: 返回 OTHERS，避免新增分类导致枚举构造失败
        """
        return cls.OTHERS


class Type(Enum):
    """
    飞牛媒体条目的类型，包含电影、剧集、季、分集、视频与目录。
    """
    # 电影
    MOVIE = "Movie"
    # 电视剧
    TV = "TV"
    # 电视剧的季
    SEASON = "Season"
    # 电视剧分集
    EPISODE = "Episode"
    # 未归类的普通视频
    VIDEO = "Video"
    # 媒体目录
    DIRECTORY = "Directory"

    @classmethod
    def _missing_(cls, value: object) -> Optional["Type"]:
        """
        将未识别的条目类型按通用视频处理。

        :param value: 服务端返回的未知类型值
        :return: 返回 VIDEO
        """
        return cls.VIDEO


@dataclass
class MediaDb:
    """
    飞牛媒体库的标识、类型及可访问资源。

    :param guid: 媒体库唯一标识
    :param category: 媒体库内容分类
    :param name: 媒体库显示名称；未提供时为 None
    :param posters: 媒体库封面的相对 API 路径列表；路径转换失败的元素为 None，未提供时为 None
    :param dir_list: 媒体库实际目录列表；普通用户接口可能不提供，缺失时为 None
    """
    guid: str
    category: Category
    name: Optional[str] = None
    posters: Optional[list[Optional[str]]] = None
    dir_list: Optional[list[str]] = None


@dataclass
class MediaDbSummary:
    """
    飞牛服务端的媒体数量汇总，缺失计数默认为零。

    :param favorite: 收藏条目数量
    :param movie: 电影数量
    :param tv: 电视剧数量
    :param video: 通用视频数量
    :param total: 服务端汇总的条目总数
    """
    favorite: int = 0
    movie: int = 0
    tv: int = 0
    video: int = 0
    total: int = 0


@dataclass
class Version:
    """
    飞牛影视的前后端版本信息。

    :param frontend: 网页前端版本，接口未提供时为 None
    :param backend: 媒体服务端版本，接口未提供时为 None
    """
    frontend: Optional[str] = None
    backend: Optional[str] = None


@dataclass
class Item:
    """
    飞牛媒体条目及可选的详情、图片和观看状态。

    :param guid: 条目唯一标识
    :param ancestor_guid: 所属祖先媒体库或目录标识，缺失时为空字符串
    :param type: 媒体条目类型；未赋值时为 None
    :param tv_title: 分集所属的电视剧名称，其他类型可能为空
    :param parent_title: 父条目名称；分集场景通常为季名称
    :param title: 条目显示标题；分集场景为分集名称
    :param original_title: 原始语言标题，搜索接口可能不提供
    :param overview: 剧情或条目简介
    :param poster: 主封面图片路径，转换时缺失可回退 posters
    :param backdrops: 详情接口提供的背景图片路径
    :param posters: 详情接口提供的海报图片路径，虽为复数名称但保留服务端字符串格式
    :param douban_id: 豆瓣条目标识
    :param imdb_id: IMDb 条目标识
    :param trim_id: 飞牛媒体身份，tt 或 tm 前缀后的数字可解析为 TMDB 标识
    :param release_date: 电影发行日期文本，调用方从前四位提取年份
    :param air_date: 电视剧首播日期文本，调用方从前四位提取年份
    :param vote_average: 服务端返回的评分文本，未提供时为 None
    :param season_number: 季号，未提供时为 None
    :param episode_number: 集号，未提供时为 None
    :param duration: 媒体总时长，单位秒
    :param ts: 已播放时长，单位秒
    :param watched: 观看完成标记，1 表示已看完；未提供时为 None
    """
    guid: str
    ancestor_guid: str = ""
    type: Optional[Type] = None
    tv_title: Optional[str] = None
    parent_title: Optional[str] = None
    title: Optional[str] = None
    original_title: Optional[str] = None
    overview: Optional[str] = None
    poster: Optional[str] = None
    backdrops: Optional[str] = None
    posters: Optional[str] = None
    douban_id: Optional[int] = None
    imdb_id: Optional[str] = None
    trim_id: Optional[str] = None
    release_date: Optional[str] = None
    air_date: Optional[str] = None
    vote_average: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    duration: Optional[int] = None
    ts: Optional[int] = None
    watched: Optional[int] = None

    @property
    def tmdb_id(self) -> Optional[int]:
        """
        从飞牛身份前缀提取 TMDB 数字标识。

        :return: tt 或 tm 前缀后的整数；标识缺失或前缀不匹配时返回 None
        :raises ValueError: 已识别前缀后的内容不是有效整数
        """
        if self.trim_id is None:
            return None
        if self.trim_id.startswith("tt") or self.trim_id.startswith("tm"):
            # 飞牛给tmdbid加了前缀用以区分tv或movie
            return int(self.trim_id[2:])
        return None


class Api:
    """
    飞牛影视 API 客户端，管理访问码会话、登录令牌和请求签名。
    """
    __slots__ = (
        "_host",
        "_token",
        "_apikey",
        "_access_code",
        "_api_path",
        "_request_utils",
        "_version",
    )

    @property
    def token(self) -> Optional[str]:
        """
        读取当前登录访问令牌。

        :return: 当前令牌，尚未登录时为 None
        """
        return self._token

    @property
    def host(self) -> str:
        """
        读取飞牛影视服务根地址。

        :return: 移除尾部斜杠后的配置地址
        """
        return self._host

    @property
    def apikey(self) -> str:
        """
        读取请求签名使用的 API 密钥。

        :return: 构造时传入的客户端密钥
        """
        return self._apikey

    @property
    def version(self) -> Optional[Version]:
        """
        读取最近一次版本查询结果。

        :return: 缓存的版本对象，尚未成功查询时为 None
        """
        return self._version

    @property
    def cookies(self) -> dict[str, str]:
        """
        读取当前 HTTP 会话的 Cookie，开启访问码后包含校验凭证。

        :return: Cookie 名称及会话提供的字符串值
        """
        return self._request_utils.get_cookies()

    def __init__(self, host: str, apikey: str, access_code: Optional[str] = None):
        """
        保存服务地址和客户端密钥，并创建复用的 HTTP 会话。

        :param host: 飞牛影视根地址，如 http://127.0.0.1:5666/v
        :param apikey: 参与 authx 签名的客户端 API 密钥
        :param access_code: 设备访问码；未启用访问码时省略
        """
        self._api_path = "/api/v1"
        self._host = host.rstrip("/")
        self._apikey = apikey
        self._access_code = access_code
        self._token: Optional[str] = None
        self._version: Optional[Version] = None
        self._request_utils = RequestUtils(use_session=True, timeout=10)

    def verify_access_code(self) -> bool:
        """
        校验访问码，通过后会话获得访问凭证，否则无法访问登录页和各应用接口。

        :return: 未配置访问码或校验通过返回True
        """
        if not self._access_code:
            return True
        # 访问码校验地址位于设备根路径，不在/v下
        root = self._host[: -len("/v")] if self._host.endswith("/v") else self._host
        url = f"{root}/c/{quote(self._access_code, safe='')}"
        res = self._request_utils.get_res(url, allow_redirects=True)
        if res is None:
            logger.error(f"校验飞牛访问码失败，无法访问 {url}")
            return False
        if res.status_code == 404:
            # 访问码错误或校验失败时返回404
            logger.error("飞牛访问码校验失败，请检查访问码是否正确")
            return False
        if not res.ok:
            logger.error(f"飞牛访问码校验失败，状态码：{res.status_code}")
            return False
        return True

    def sys_version(self) -> Optional[Version]:
        """
        查询并缓存飞牛影视前后端版本。

        :return: 版本对象；请求失败或响应无版本数据时返回 None
        """
        if (res := self.request("/sys/version")) and res.success:
            if res.data:
                self._version = Version(
                    frontend=res.data.get("version"),
                    backend=res.data.get("mediasrvVersion"),
                )
                return self._version
        return None

    def login(self, username: str, password: str) -> Optional[str]:
        """
        校验访问码后登录飞牛影视，并保存访问令牌。

        优先请求 v2 登录。只有 v2 请求未返回业务响应时才回退 v1 明文密码接口；
        已返回明确登录失败的 v2 响应不触发回退。

        :param username: 登录用户名
        :param password: 原始密码；v2 接口发送其 SHA-256 十六进制摘要
        :return: 成功时返回会话令牌；访问码验证或业务登录失败时返回 None
        """
        # 开启访问码后需先通过访问码校验，否则无法访问登录接口
        if not self.verify_access_code():
            return None
        # v2 协议要求密码为明文密码的 SHA256 十六进制小写摘要
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        res = self.request(
            "/user/loginByPassword",
            data={
                "username": username,
                "password": password_hash,
                "app_name": "trimemedia-web",
            },
            base_path="/api/v2",
            suppress_log=True,
        )
        if res and res.success:
            self._token = res.data.get("token")
            return self._token
        if res:
            # v2 接口存在但登录失败（如账号密码错误），回退 v1 也无法成功
            logger.error(f"飞牛影视登录失败，错误码：{res.code} {res.msg}")
            return None
        # v2 接口不可用（旧版服务端），回退 v1 明文登录
        if (
            res := self.request(
                "/login",
                data={
                    "username": username,
                    "password": password,
                    "app_name": "trimemedia-web",
                },
            )
        ) and res.success:
            self._token = res.data.get("token")
        return self._token

    def logout(self) -> bool:
        """
        请求退出账号，并在服务端确认成功后清除本地令牌。

        :return: 原本没有令牌，或业务成功且返回有效数据时为 True；否则 False 并保留令牌
        """
        if not self._token:
            return True
        if (res := self.request("/user/logout", method="post")) and res.success:
            if res.data:
                self._token = None
                return True
        return False

    def user_list(self) -> Optional[list[User]]:
        """
        查询管理员可访问的用户列表。

        :return: 用户列表；无用户时为空列表，请求失败或权限不足时返回 None
        """
        if (res := self.request("/manager/user/list")) and res.success:
            if not res.data:
                return []
            return [
                User(
                    guid=info.get("guid"),
                    username=info.get("username"),
                    is_admin=info.get("is_admin", 0),
                )
                for info in res.data
            ]
        return None

    def user_info(self) -> Optional[User]:
        """
        查询当前会话的用户详情。

        :return: 用户对象；请求业务失败时返回 None
        """
        if (res := self.request("/user/info")) and res.success:
            _user = User("", "")
            _user.__dict__.update(res.data)
            return _user
        return None

    def mediadb_sum(self) -> Optional[MediaDbSummary]:
        """
        查询当前可见内容的媒体数量。

        :return: 媒体数量汇总对象；请求失败时返回 None
        """
        if (res := self.request("/mediadb/sum")) and res.success:
            sums = MediaDbSummary()
            sums.__dict__.update(res.data)
            return sums
        return None

    def mediadb_list(self) -> Optional[List[MediaDb]]:
        """
        查询普通用户可见的媒体库。

        :return: 含封面相对路径的媒体库列表；无数据时为空列表，请求失败时返回 None
        """
        if (res := self.request("/mediadb/list")) and res.success:
            _items = []
            for info in res.data or []:
                mdb = MediaDb(
                    guid=info.get("guid"),
                    category=Category(info.get("category")),
                    name=info.get("title", ""),
                    posters=[
                        self.__build_img_api_url(poster)
                        for poster in info.get("posters", [])
                    ],
                )
                _items.append(mdb)
            return _items
        return None

    def __build_img_api_url(self, img_path: Optional[str]) -> Optional[str]:
        """
        将图片路径拼接为飞牛 v1 图片接口的相对地址。

        :param img_path: 服务端返回的图片路径，空值表示无图片
        :return: 以 /api/v1/sys/img 开头的路径；输入为空时返回 None
        """
        if not img_path:
            return None
        if img_path[0] != "/":
            img_path = "/" + img_path
        return f"{self._api_path}/sys/img{img_path}"

    def mdb_list(self) -> Optional[list[MediaDb]]:
        """
        查询管理员可见的媒体库及实际目录。

        :return: 含 dir_list 的媒体库列表；无数据时为空列表，权限不足或请求失败时返回 None
        """
        if (res := self.request("/mdb/list")) and res.success:
            _items = []
            for info in res.data or []:
                mdb = MediaDb(
                    guid=info.get("guid"),
                    category=Category(info.get("category")),
                    name=info.get("name", ""),
                    posters=[
                        self.__build_img_api_url(poster)
                        for poster in info.get("posters", [])
                    ],
                    dir_list=info.get("dir_list"),
                )
                _items.append(mdb)
            return _items
        return None

    def mdb_scanall(self) -> bool:
        """
        请求扫描全部媒体库。

        :return: 业务成功且响应 data 为真时返回 True，否则 False；不等待扫描完成
        """
        if (res := self.request("/mdb/scanall", method="post")) and res.success:
            if res.data:
                return True
        return False

    def mdb_scan(self, mdb: MediaDb) -> bool:
        """
        请求扫描指定媒体库。

        :param mdb: 包含目标 guid 的媒体库对象
        :return: 业务成功且响应 data 为真时返回 True，否则 False；不等待扫描完成
        """
        if (res := self.request(f"/mdb/scan/{mdb.guid}", data={})) and res.success:
            if res.data:
                return True
        return False

    def task_running(self) -> bool:
        """
        查询服务端当前是否存在运行任务。

        :return: 业务成功且 data 为真时返回 True；只报告存在性，不返回任务详情
        """
        if (res := self.request("/task/running")) and res.success:
            if res.data:
                return True
        return False

    def __build_item(self, info: dict[str, object]) -> Item:
        """
        复制条目字段，并归一类型与图片 API 路径。

        :param info: 服务端条目字典，额外字段也保留到对象属性中
        :return: 飞牛条目对象；主封面缺失时回退详情海报路径
        """
        item = Item(guid="")
        item.__dict__.update(info)
        item.type = Type(info.get("type"))
        # Item详情接口才有posters和backdrops
        item.posters = self.__build_img_api_url(item.posters)
        item.backdrops = self.__build_img_api_url(item.backdrops)
        item.poster = (
            self.__build_img_api_url(item.poster) if item.poster else item.posters
        )
        return item

    def item_list(
        self,
        guid: Optional[Union[str, int]] = None,
        types: Optional[list[Type]] = None,
        exclude_grouped_video: bool = True,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "create_time",
        sort: str = "DESC",
    ) -> Optional[list[Item]]:
        """
        按媒体库、类型及排序条件查询一页条目。

        :param guid: 祖先媒体库或目录标识，可传字符串或数字；空值时不限祖先
        :param types: 媒体类型列表；None 默认查询电影、剧集、目录和视频，空列表不筛选类型
        :param exclude_grouped_video: 是否排除已归组视频，默认启用
        :param page: 从 1 开始的页码
        :param page_size: 每页条目数量，-1 透传服务端的不限数量约定
        :param sort_by: 服务端排序字段，默认 create_time
        :param sort: 排序方向，默认 DESC 降序
        :return: 媒体条目列表；业务成功但无数据时为空列表，请求失败时返回 None
        """
        if types is None:
            types = [Type.MOVIE, Type.TV, Type.DIRECTORY, Type.VIDEO]
        post = {
            "tags": {"type": types} if types else {},
            "sort_type": sort,
            "sort_column": sort_by,
            "page": page,
            "page_size": page_size,
        }
        if guid:
            post["ancestor_guid"] = guid
        if exclude_grouped_video:
            post["exclude_grouped_video"] = 1

        if (res := self.request("/item/list", data=post)) and res.success:
            if res.data is None:
                return []
            if not isinstance(res.data, dict) or not isinstance(res.data.get("list", []), list):
                return None
            return [self.__build_item(info) for info in res.data.get("list", [])]
        return None

    def item_count(self, guid: str, types: Optional[list[Type]] = None) -> Optional[int]:
        """
        获取指定媒体库的媒体条目总数。

        :param guid: 媒体库GUID
        :param types: 需要统计的媒体类型
        :return: 媒体条目总数，查询失败时返回None
        """
        if types is None:
            types = [Type.MOVIE, Type.TV]
        post = {
            "ancestor_guid": guid,
            "tags": {"type": types},
            "exclude_grouped_video": 1,
            "page": 1,
            "page_size": 1,
        }
        if (res := self.request("/item/list", data=post)) and res.success:
            if not res.data:
                return 0
            total_count = res.data.get("total")
            if total_count is None:
                total_count = res.data.get("total_count")
            return int(total_count) if total_count is not None else None
        return None

    def search_list(self, keywords: Optional[str]) -> Optional[list[Item]]:
        """
        按关键词查询影片和演员相关条目。

        :param keywords: 搜索关键词；未提供时交由服务端处理
        :return: 条目列表；无结果时为空列表，请求失败时返回 None
        """
        if (
            res := self.request("/search/list", params={"q": keywords})
        ) and res.success:
            if not res.data:
                return []
            return [self.__build_item(info) for info in res.data]
        return None

    def item(self, guid: str) -> Optional[Item]:
        """
        查询单个媒体条目的完整详情。

        :param guid: 飞牛条目唯一标识
        :return: 含详情图片的条目对象；请求失败时返回 None
        """
        if (res := self.request(f"/item/{guid}")) and res.success:
            return self.__build_item(res.data)
        return None

    def del_item(self, guid: str, delete_file: bool) -> bool:
        """
        删除媒体记录，并按选项请求删除实际文件。

        :param guid: 媒体条目唯一标识
        :param delete_file: True 同时删除媒体文件，False 只移除媒体库记录
        :return: 业务成功且返回有效 data 时为 True，否则 False
        """
        if (
            res := self.request(
                f"/item/{guid}",
                method="delete",
                data={"delete_file": 1 if delete_file else 0, "media_guids": []},
            )
        ) and res.success:
            if res.data:
                return True
        return False

    def season_list(self, tv_guid: str) -> Optional[list[Item]]:
        """
        查询电视剧下的季条目。

        :param tv_guid: 电视剧条目标识
        :return: 季条目列表；无数据时为空列表，请求失败时返回 None
        """
        if (res := self.request(f"/season/list/{tv_guid}")) and res.success:
            if not res.data:
                return []
            return [self.__build_item(info) for info in res.data]
        return None

    def episode_list(self, season_guid: str) -> Optional[list[Item]]:
        """
        查询指定季下的分集条目。

        :param season_guid: 季条目标识
        :return: 分集条目列表；无数据时为空列表，请求失败时返回 None
        """
        if (res := self.request(f"/episode/list/{season_guid}")) and res.success:
            if not res.data:
                return []
            return [self.__build_item(info) for info in res.data]
        return None

    def play_list(self) -> Optional[list[Item]]:
        """
        查询当前用户的继续观看条目。

        :return: 媒体条目列表；无数据时为空列表，请求失败时返回 None
        """
        if (res := self.request("/play/list")) and res.success:
            if not res.data:
                return []
            return [self.__build_item(info) for info in res.data]
        return None

    def __get_authx(self, api_path: str, body: Optional[str]) -> str:
        """
        按飞牛客户端协议计算单次请求的 authx 签名。

        :param api_path: 接口路径，未以 /v 开头时补齐该前缀
        :param body: 参与摘要的请求正文或未编码查询串，空值按空字符串处理
        :return: 包含六位随机数、毫秒时间戳及 MD5 签名的请求头值
        """
        if not api_path.startswith("/v"):
            api_path = "/v" + api_path
        nonce = str(random.randint(100000, 999999))
        ts = str(int(time.time() * 1000))
        md5 = hashlib.md5()
        md5.update((body or "").encode())
        data_hash = md5.hexdigest()
        md5 = hashlib.md5()
        md5.update(
            "_".join(
                [
                    "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh",
                    api_path,
                    nonce,
                    ts,
                    data_hash,
                    self._apikey,
                ]
            ).encode()
        )
        sign = md5.hexdigest()
        return f"nonce={nonce}&timestamp={ts}&sign={sign}"

    def request(
        self,
        api: str,
        method: Optional[str] = None,
        params: Optional[dict[str, object]] = None,
        data: Optional[dict[str, object]] = None,
        base_path: Optional[str] = None,
        suppress_log: bool = False,
    ) -> Any:
        """
        构造签名并发送飞牛 API 请求，解析业务状态与响应数据。

        业务失败仍返回状态对象但不保留 data；请求体序列化发生在请求异常捕获之前，
        无法序列化的数据会直接向调用方抛出异常。

        :param api: 接口相对路径
        :param method: 小写 HTTP 方法；省略时有 data 使用 post，否则 get
        :param params: URL 查询参数，按插入顺序生成签名原文
        :param data: 请求 JSON 数据，非 GET 时参与正文序列化和签名
        :param base_path: 接口前缀，默认 /api/v1，可显式切换为 /api/v2
        :param suppress_log: 是否抑制本方法的 HTTP 和业务失败日志，默认不抑制
        :return: 业务响应对象；地址或接口为空、无有效 HTTP 响应或请求解析异常时返回 None
        """

        @dataclass
        class Result:
            """
            单次飞牛 API 请求解析后的业务结果。

            :param code: 业务状态码，0 表示成功
            :param msg: 可选业务说明
            :param data: 业务数据，失败响应或未提供时为 None
            """
            @property
            def success(self) -> bool:
                """
                读取本次响应解析时确定的业务成功状态。

                :return: 当前 request 调用解析出的状态码为 0 时返回 True；不重新读取可变的 code 属性
                """
                return code == 0

            code: int
            msg: Optional[str] = None
            data: Optional[Union[dict[str, Any], list[Any], str, bool]] = None

        class JsonEncoder(json.JSONEncoder):
            """
            为请求体提供飞牛媒体类型枚举的 JSON 编码规则。
            """
            def default(self, obj: object) -> object:
                """
                把飞牛 Type 枚举编码为服务端类型值。

                :param obj: 需要补充编码的对象
                :return: 枚举对应的字符串；其他对象交给标准 JSON 编码器处理
                :raises TypeError: 标准编码器也无法序列化该对象
                """
                if isinstance(obj, Type):
                    return obj.value
                return super().default(obj)

        if not self._host or not api:
            return None
        prefix = base_path if base_path is not None else self._api_path
        if not api.startswith("/"):
            api_path = f"{prefix}/{api}"
        else:
            api_path = prefix + api
        url = self._host + api_path
        if method is None:
            method = "get" if data is None else "post"
        if method != "get":
            json_body = (
                json.dumps(data, allow_nan=False, cls=JsonEncoder) if data else ""
            )
        else:
            json_body = None
        if params:
            queries_unquoted = "&".join([f"{k}={v}" for k, v in params.items()])
        else:
            queries_unquoted = None
        headers = {
            "User-Agent": get_runtime_setting('USER_AGENT'),
            "Accept": "application/json",
            "Referer": self._host,
            "Authorization": self._token,
            "authx": self.__get_authx(api_path, json_body or queries_unquoted),
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            res = self._request_utils.request(
                method=method, url=url, headers=headers, params=params, data=json_body
            )
            if res:
                resp = res.json()
                msg = resp.get("msg")
                if code := int(resp.get("code", -1)):
                    if not suppress_log:
                        logger.error(f"请求接口 {url} 失败，错误码：{code} {msg}")
                    return Result(code, msg)
                return Result(0, msg, resp.get("data"))
            elif not suppress_log:
                logger.error(f"请求接口 {url} 失败")
        except Exception as e:
            if not suppress_log:
                logger.error(f"请求接口 {url} 异常：" + str(e))
        return None

    def close(self) -> None:
        """
        关闭API会话。
        """
        self._request_utils.close()
