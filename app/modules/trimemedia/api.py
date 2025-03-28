import hashlib
import json
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union, List

from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils, requests


@dataclass
class User:
    guid: str
    username: str
    is_admin: int = 0


class Category(Enum):
    Movie = "Movie"
    TV = "TV"
    Mix = "Mix"
    Others = "Others"

    @classmethod
    def _missing_(cls, value):
        return cls.Others


class Type(Enum):
    Movie = "Movie"
    TV = "TV"
    Season = "Season"
    Episode = "Episode"
    Video = "Video"
    Directory = "Directory"

    @classmethod
    def _missing_(cls, value):
        return cls.Video


@dataclass
class MediaDb:
    guid: str
    category: Category
    name: Optional[str] = None
    posters: Optional[list[str]] = None
    dir_list: Optional[list[str]] = None


@dataclass
class MediaDbSumary:
    favorite: int = 0
    movie: int = 0
    tv: int = 0
    video: int = 0
    total: int = 0


@dataclass
class Item:
    guid: str
    ancestor_guid: str = ""
    type: Optional[Type] = None
    # 当type为Episode时是剧名，parent_title是季名，title作为分集名称
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
    duration: Optional[int] = None  # 片长(秒)
    ts: Optional[int] = None  # 已播放(秒)
    watched: Optional[int] = None  # 1:已看完

    @property
    def tmdb_id(self) -> Optional[int]:
        if self.trim_id is None:
            return None
        if self.trim_id.startswith("tt") or self.trim_id.startswith("tm"):
            # 飞牛给tmdbid加了前缀用以区分tv或movie
            return int(self.trim_id[2:])
        return None


class Api:
    __slots__ = (
        "_host",
        "_token",
        "_apikey",
        "_api_path",
        "_request_utils",
    )

    @property
    def token(self) -> Optional[str]:
        return self._token

    @property
    def host(self) -> str:
        return self._host

    @property
    def apikey(self) -> str:
        return self._apikey

    def __init__(self, host: str, apikey: str):
        self._api_path = "/v/api/v1"
        self._host = host.rstrip("/")
        self._apikey = apikey
        self._token = None
        self._request_utils = RequestUtils(session=requests.Session())

    def login(self, username, password) -> Optional[str]:
        """
        登录飞牛影视

        :return: 成功返回token 否则返回None
        """
        if (
                res := self.__request_api(
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
        退出账号
        """
        if (res := self.__request_api("/user/logout", method="post")) and res.success:
            if res.data:
                self._token = None
                return True
        return False

    def user_list(self) -> Optional[list[User]]:
        """
        用户列表(仅管理员有权访问)
        """
        if (res := self.__request_api("/manager/user/list")) and res.success:
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
        当前用户信息
        """
        if (res := self.__request_api("/user/info")) and res.success:
            _user = User("", "")
            _user.__dict__.update(res.data)
            return _user
        return None

    def mediadb_sum(self) -> Optional[MediaDbSumary]:
        """
        媒体数量统计
        """
        if (res := self.__request_api("/mediadb/sum")) and res.success:
            sums = MediaDbSumary()
            sums.__dict__.update(res.data)
            return sums
        return None

    def mediadb_list(self) -> Optional[List[MediaDb]]:
        """
        媒体库列表(普通用户)
        """
        if (res := self.__request_api("/mediadb/list")) and res.success:
            _items = []
            for info in res.data:
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
        if not img_path:
            return None
        if img_path[0] != "/":
            img_path = "/" + img_path
        return f"{self._api_path}/sys/img{img_path}"

    def mdb_list(self) -> Optional[list[MediaDb]]:
        """
        媒体库列表(管理员)
        """
        if (res := self.__request_api("/mdb/list")) and res.success:
            _items = []
            for info in res.data:
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
        扫描所有媒体库
        """
        if (res := self.__request_api("/mdb/scanall", method="post")) and res.success:
            if res.data:
                self._token = None
                return True
        return False

    def mdb_scan(self, mdb: MediaDb) -> bool:
        """
        扫描指定媒体库
        """
        if (
                res := self.__request_api(f"/mdb/scan/{mdb.guid}", data={})
        ) and res.success:
            if res.data:
                self._token = None
                return True
        return False

    def __build_item(self, info: dict) -> Item:
        """
        构造媒体Item
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
            guid: Optional[str] = None,
            type=None,
            exclude_grouped_video=True,
            page=1,
            page_size=22,
            sort_by="create_time",
            sort="DESC",
    ) -> Optional[list[Item]]:
        """
        媒体列表
        """
        if type is None:
            type = [Type.Movie, Type.TV, Type.Directory, Type.Video]
        post = {
            "tags": {"type": type} if type else {},
            "sort_type": sort,
            "sort_column": sort_by,
            "page": page,
            "page_size": page_size,
        }
        if guid:
            post["ancestor_guid"] = guid
        if exclude_grouped_video:
            post["exclude_grouped_video"] = 1

        if (res := self.__request_api("/item/list", data=post)) and res.success:
            return [self.__build_item(info) for info in res.data.get("list", [])]
        return None

    def search_list(self, keywords: str) -> Optional[list[Item]]:
        """
        搜索影片、演员
        """
        if (
                res := self.__request_api("/search/list", params={"q": keywords})
        ) and res.success:
            return [self.__build_item(info) for info in res.data]
        return None

    def item(self, guid: str) -> Optional[Item]:
        """ """
        if (res := self.__request_api(f"/item/{guid}")) and res.success:
            return self.__build_item(res.data)
        return None

    def season_list(self, tv_guid: str) -> Optional[list[Item]]:
        """ """
        if (res := self.__request_api(f"/season/list/{tv_guid}")) and res.success:
            return [self.__build_item(info) for info in res.data]
        return None

    def episode_list(self, season_guid: str) -> Optional[list[Item]]:
        """ """
        if (res := self.__request_api(f"/episode/list/{season_guid}")) and res.success:
            return [self.__build_item(info) for info in res.data]
        return None

    def play_list(self) -> Optional[list[Item]]:
        """
        继续观看列表
        """
        if (res := self.__request_api("/play/list")) and res.success:
            return [self.__build_item(info) for info in res.data]
        return None

    ################################################################

    def __get_authx(self, api_path, body: Optional[str]):
        """
        计算消息签名
        """
        if api_path[0] != "/":
            api_path = "/" + api_path
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

    def __request_api(
            self, api: str, method: str = None, params: dict = None, data: dict = None
    ):
        """
        请求飞牛影视API
        """

        @dataclass
        class Result:
            @property
            def success(self) -> bool:
                return code == 0

            code: int
            msg: Optional[str] = None
            data: Optional[Union[dict, list, str, bool]] = None

        class JsonEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, Type):
                    return obj.value
                return super().default(obj)

        if not self._host or not api:
            return None
        if not api.startswith("/"):
            api_path = f"{self._api_path}/{api}"
        else:
            api_path = self._api_path + api
        url = self._host + api_path
        if method is None:
            method = "get" if data is None else "post"
        if method == "post":
            json_body = (
                json.dumps(data, allow_nan=False, cls=JsonEncoder) if data else ""
            )
        else:
            json_body = None
        headers = {
            "User-Agent": settings.USER_AGENT,
            "Authorization": self._token,
            "authx": self.__get_authx(api_path, json_body),
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
                    logger.error(f"请求接口 {api_path} 失败，错误码：{code} {msg}")
                    return Result(code, msg)
                return Result(0, msg, resp.get("data"))
            else:
                logger.error(f"请求接口 {api_path} 失败")
        except Exception as e:
            logger.error(f"请求接口 {api_path} 异常：" + str(e))
        return None


if __name__ == "__main__":
    fnApi = Api("http://192.168.1.49:5666/", "16CCEB3D-AB42-077D-36A1-F355324E4237")
    fnApi.login("adad", "123456")
    logger.debug(f"token={fnApi.token}")

    user = fnApi.user_info()
    logger.debug(user)

    mediadbs = fnApi.mdb_list()
    logger.debug(mediadbs)

    items = fnApi.item_list(mediadbs[0].guid, page=1, page_size=0)
    logger.debug(items)

    fnApi.logout()
