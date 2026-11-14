"""
Navidrome Subsonic/OpenSubsonic API 客户端。
"""

import hashlib
import re
import secrets
from typing import Any, Dict, Generator, List, Optional
from urllib.parse import urlencode

from app.adapters.network.http import RequestUtils
from app.foundation.url import UrlUtils
from app.runtime.log import logger
from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerField, MediaServerQueryOptions
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.schemas.types import MediaType


class Navidrome:
    """
    通过 Subsonic API 访问 Navidrome 音乐库。
    """

    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        play_host: Optional[str] = None,
        sync_libraries: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> None:
        """
        保存连接配置并调用 ping 验证当前用户。

        :param host: Navidrome 服务地址，省略时客户端保持未连接状态
        :param username: Subsonic 登录用户名
        :param password: Subsonic 登录密码
        :param play_host: 仪表盘跳转使用的地址，省略时使用服务地址
        :param sync_libraries: 同步库选择；隐藏未选库时只识别 all 标记
        :param kwargs: 统一服务配置的额外参数，当前不读取
        """
        self._host = UrlUtils.standardize_base_url(host) if host else None
        self._play_host = UrlUtils.standardize_base_url(play_host) if play_host else self._host
        self._username = username or ""
        self._password = password or ""
        self._sync_libraries = sync_libraries or []
        self.user = self.get_user()

    def _api_url(self) -> Optional[str]:
        """
        返回 Subsonic REST 根地址。
        """
        if not self._host:
            return None
        return f"{self._host.rstrip('/')}/rest"

    def _params(self, **kwargs: Any) -> Dict[str, Any]:
        """
        构造 Subsonic token 鉴权参数。
        """
        salt = secrets.token_hex(8)
        token = hashlib.md5(f"{self._password}{salt}".encode("utf-8")).hexdigest()
        params: Dict[str, Any] = {
            "u": self._username,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "MoviePilot",
            "f": "json",
        }
        params.update({key: value for key, value in kwargs.items() if value is not None})
        return params

    def _call(self, method: str, **kwargs: Any) -> Optional[dict[str, Any]]:
        """
        通过 Subsonic 接口获取业务成功的响应体。

        :param method: rest 根路径下的接口名称
        :param kwargs: 请求参数；值为 None 的项不会发送
        :return: subsonic-response 对象；配置不完整、HTTP 或业务请求失败时返回 None
        """
        url = self._api_url()
        if not url or not self._username or not self._password:
            return None
        response = None
        try:
            response = RequestUtils().get_res(f"{url}/{method}", self._params(**kwargs))
            if not response:
                return None
            response_data = response.json()
            if not isinstance(response_data, dict):
                return None
            payload = response_data.get("subsonic-response", {})
            if not isinstance(payload, dict):
                return None
            if payload.get("status") != "ok":
                logger.warning(f"Navidrome API {method} 返回失败：{payload.get('error')}")
                return None
            return payload
        except Exception as err:
            logger.debug(f"Navidrome API {method} 调用失败：{err}")
            return None
        finally:
            if response:
                response.close()

    def is_inactive(self) -> bool:
        """
        判断完整连接配置是否尚未通过用户验证。

        :return: 地址及用户名密码均已配置但 user 为空时返回 True；配置缺失时返回 False
        """
        return bool(self._host and self._username and self._password and not self.user)

    def reconnect(self) -> None:
        """
        重新验证 Navidrome 连接。
        """
        self.user = self.get_user()

    def get_user(self) -> Optional[str]:
        """
        调用 ping 验证配置中的用户名和密码。

        :return: 验证成功时返回配置用户名，配置不完整或验证失败时返回 None
        """
        return self._username if self._call("ping") else None

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """
        使用独立客户端验证给定凭据，不改变服务账号或其他并发认证上下文。

        :param username: 待验证的用户名
        :param password: 待验证的密码
        :return: 验证成功时返回用户名作为认证结果；输入为空、地址缺失或认证失败时返回 None
        """
        if not username or not password or not self._host:
            return None
        return Navidrome(host=self._host, username=username, password=password).user

    def get_user_count(self) -> int:
        """
        返回当前连接能够确认的用户数量。

        :return: 实例用户已验证时返回 1，否则返回 0；不枚举服务端所有用户
        """
        return 1 if self.user else 0

    @staticmethod
    def _album_name(album: dict[str, Any]) -> str:
        """
        从 Subsonic 专辑对象提取稳定标题。
        """
        return album.get("name") or album.get("album") or ""

    def _album_to_item(self, album: dict[str, Any]) -> _SchemaMediaServerItem:
        """
        将 Subsonic 专辑转换为统一媒体条目。
        """
        album_id = str(album.get("id") or "")
        return _SchemaMediaServerItem(
            id=album_id,
            item_id=album_id,
            title=self._album_name(album),
            original_title=self._album_name(album),
            year=album.get("year") or album.get("created"),
            item_type=MediaType.MUSIC.value,
            server_type="navidrome",
            path=album.get("path"),
            note={"artist": album.get("artist"), "song_count": album.get("songCount")},
        )

    def _song_to_item(self, song: dict[str, Any]) -> _SchemaMediaServerItem:
        """
        将 Subsonic 单曲转换为统一音乐条目。
        """
        song_id = str(song.get("id") or "")
        title = song.get("title") or song.get("name") or ""
        return _SchemaMediaServerItem(
            id=song_id,
            item_id=song_id,
            title=title,
            original_title=title,
            year=song.get("year") or song.get("created"),
            item_type=MediaType.MUSIC.value,
            server_type="navidrome",
            path=song.get("path"),
            note={"artist": song.get("artist"), "album": song.get("album")},
        )

    @staticmethod
    def _same_name(left: Optional[str], right: Optional[str]) -> bool:
        """
        忽略大小写、空白和标点比较 Navidrome 音乐名称。
        """
        normalized_left = re.sub(r"[^\w]+", "", str(left or "").casefold())
        normalized_right = re.sub(r"[^\w]+", "", str(right or "").casefold())
        return bool(normalized_left) and normalized_left == normalized_right

    @classmethod
    def _same_artist(cls, item: dict[str, Any], artist: Optional[str]) -> bool:
        """
        检查专辑或单曲的艺术家是否与目标一致；目标为空时不限制。
        """
        if not artist:
            return True
        return any(
            cls._same_name(candidate, artist)
            for candidate in (item.get("artist"), item.get("albumArtist"))
        )

    def _album_cover(self, album: dict[str, Any]) -> Optional[str]:
        """
        返回专辑封面地址。
        """
        cover_id = album.get("coverArt")
        if not cover_id:
            return None
        url = self._api_url()
        if not url:
            return None
        return f"{url}/getCoverArt?{urlencode(self._params(id=cover_id, size=512))}"

    def _albums(
        self, list_type: str = "alphabeticalByName", size: int = 500, *, strict: bool = False
    ) -> List[dict[str, Any]]:
        """
        分页读取专辑，直至短页、空页或接口失败。

        :param list_type: Subsonic 专辑排序方式，默认按名称排序
        :param size: 每页请求数量，默认 500
        :param strict: 同步读取时要求完整结果，失败抛出 ConnectionError
        :return: 已读取的专辑列表；中途失败会返回此前已读取的部分结果
        """
        albums: List[dict[str, Any]] = []
        offset = 0
        while True:
            payload = self._call("getAlbumList2", type=list_type, size=size, offset=offset)
            if strict:
                if payload is None or not isinstance(payload.get("albumList2"), dict):
                    raise ConnectionError("读取 Navidrome 专辑列表失败")
                if "album" in payload["albumList2"] and not isinstance(payload["albumList2"]["album"], list):
                    raise ConnectionError("Navidrome 专辑列表响应格式错误")
            page = ((payload or {}).get("albumList2") or {}).get("album") or []
            if not page:
                break
            albums.extend(page)
            if len(page) < size:
                break
            offset += len(page)
        return albums

    def get_medias_count(self) -> _SchemaStatistic:
        """
        统计专辑数量并映射为音乐数量。

        :return: 音乐统计对象；用户未验证时返回空统计，接口中途失败可能只计入已读取的专辑
        """
        return _SchemaStatistic(music_count=len(self._albums())) if self.user else _SchemaStatistic()

    def get_librarys(
        self, hidden: Optional[bool] = False, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[List[_SchemaMediaServerLibrary]]:
        """
        将全部专辑展示为单个虚拟音乐库。

        :param hidden: 是否应用同步库筛选；启用且非空同步库配置不含 all 时隐藏音乐库
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 虚拟音乐库列表；用户未验证或音乐库被隐藏时返回空列表
        """
        options = query or MediaServerQueryOptions()
        if not self.user or (
            hidden and self._sync_libraries and not {"all", "music"}.intersection(self._sync_libraries)
        ):
            return []
        count = (self.get_items_count("music")) if options.includes(MediaServerField.COUNTS) else None
        return [
            _SchemaMediaServerLibrary(
                server="navidrome",
                id="music",
                item_id="music",
                name="音乐",
                type=MediaType.MUSIC.value,
                item_count=(count) if options.includes(MediaServerField.COUNTS) else None,
                link=(self._play_host) if options.includes(MediaServerField.LINK) else None,
                server_type="navidrome",
            )
        ]

    def get_items_count(self, library_id: str = "music") -> int:
        """
        统计虚拟音乐库中的专辑数量。

        :param library_id: 兼容统一媒体库接口的标识，当前只有一个音乐库，因此不参与筛选
        :return: 已查询到的专辑数量；用户未验证时返回 0
        """
        return len(self._albums()) if self.user else 0

    def get_items(
        self, start_index: int = 0, limit: int = -1, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Generator[_SchemaMediaServerItem, None, None]:
        """
        读取指定范围的专辑；显式投影使用服务端分页，默认保留全量读取后切片的行为。

        :param start_index: 专辑列表起始索引，默认为 0
        :param limit: 返回数量；-1 或 None 表示从起始位置返回到末尾
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :yields: 已转换为统一媒体模型的专辑条目
        :raises ConnectionError: 任意一页读取失败，不能将部分结果作为完整媒体库
        """
        if query is not None and query.fields is not None:
            for album in self._iter_albums(start_index, limit, strict=True):
                yield self._album_to_item(album)
            return
        albums = self._albums(strict=True)
        end = None if limit is None or limit == -1 else start_index + limit
        for album in albums[start_index:end]:
            yield self._album_to_item(album)

    def _iter_albums(
        self, start_index: int, limit: Optional[int], *, list_type: str = "alphabeticalByName", strict: bool = False
    ) -> Generator[dict[str, Any], None, None]:
        """
        直接使用 Subsonic 的偏移和数量控制逐页读取，避免先取全量再切片。

        :param start_index: 首个专辑偏移，负数按零处理
        :param limit: 本次最大数量；None 或 -1 持续翻页，其它非正值不发请求
        :param list_type: Subsonic 专辑排序方式
        :param strict: 同步读取时要求完整结果，失败抛出 ConnectionError
        :yields: 服务器返回的原始专辑
        """
        offset = max(0, start_index)
        remaining = None if limit is None or limit == -1 else max(0, limit)
        while remaining is None or remaining > 0:
            size = 500 if remaining is None else min(500, remaining)
            payload = self._call("getAlbumList2", type=list_type, size=size, offset=offset)
            if strict:
                if payload is None or not isinstance(payload.get("albumList2"), dict):
                    raise ConnectionError("读取 Navidrome 专辑列表失败")
                if "album" in payload["albumList2"] and not isinstance(payload["albumList2"]["album"], list):
                    raise ConnectionError("Navidrome 专辑列表响应格式错误")
            albums = ((payload or {}).get("albumList2") or {}).get("album") or []
            yield from albums
            if len(albums) < size:
                return
            offset += len(albums)
            if remaining is not None:
                remaining -= len(albums)

    def get_iteminfo(
        self, item_id: str, *, query: Optional[MediaServerQueryOptions] = None
    ) -> Optional[_SchemaMediaServerItem]:
        """
        根据专辑标识获取详情。

        :param item_id: Navidrome 条目标识
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 统一专辑条目；专辑不存在或请求失败时返回 None
        """
        payload = self._call("getAlbum", id=item_id)
        album = (payload or {}).get("album") or {}
        return self._album_to_item(album) if album else None

    def search_music(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        *,
        query: Optional[MediaServerQueryOptions] = None,
    ) -> List[_SchemaMediaServerItem]:
        """
        搜索专辑或歌曲，并按名称和艺术家精确筛选。

        上游每次最多查询 20 张专辑和 20 首单曲；本方法不分页扩大候选范围。

        :param title: 目标单曲标题；未指定 album 时用于单曲匹配
        :param artist: 可选艺术家名称，省略时不限艺术家
        :param album: 目标专辑名称；提供时优先查询和匹配专辑
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 名称忽略大小写、空白及标点后匹配的音乐条目；查询词为空或请求失败时返回空列表
        """
        target = album or title
        search_term = " ".join(dict.fromkeys(filter(None, [target, artist]))).strip()
        if not search_term:
            return []
        payload = self._call("search3", query=search_term, artistCount=0, albumCount=20, songCount=20)
        result = (payload or {}).get("searchResult3") or {}
        albums = result.get("album") or []
        songs = result.get("song") or []
        if album:
            return [
                self._album_to_item(item)
                for item in albums
                if self._same_name(self._album_name(item), album) and self._same_artist(item, artist)
            ]
        return [
            self._song_to_item(item)
            for item in songs
            if self._same_name(item.get("title") or item.get("name"), title) and self._same_artist(item, artist)
        ]

    def _to_play_item(
        self, album: dict[str, Any], *, query: Optional[MediaServerQueryOptions] = None
    ) -> _SchemaMediaServerPlayItem:
        """
        将专辑转换为仪表盘播放/最新条目。

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        options = query or MediaServerQueryOptions()
        album_id = str(album.get("id") or "")
        return _SchemaMediaServerPlayItem(
            id=album_id,
            item_id=album_id,
            title=self._album_name(album),
            subtitle=album.get("artist") or album.get("albumArtist"),
            type=MediaType.MUSIC.value,
            image=(self._album_cover(album)) if options.includes(MediaServerField.IMAGES) else None,
            link=(self._play_host) if options.includes(MediaServerField.LINK) else None,
            server_type="navidrome",
        )

    def _song_to_play_item(
        self, song: dict[str, Any], *, query: Optional[MediaServerQueryOptions] = None
    ) -> _SchemaMediaServerPlayItem:
        """
        将正在播放的单曲转换为仪表盘条目，避免把所属专辑名误作曲名。

        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        """
        options = query or MediaServerQueryOptions()
        song_id = str(song.get("id") or "")
        return _SchemaMediaServerPlayItem(
            id=song_id,
            item_id=song_id,
            title=song.get("title") or song.get("name") or "",
            subtitle=song.get("artist") or song.get("albumArtist"),
            type=MediaType.MUSIC.value,
            image=(self._album_cover(song)) if options.includes(MediaServerField.IMAGES) else None,
            link=(self._play_host) if options.includes(MediaServerField.LINK) else None,
            server_type="navidrome",
        )

    def get_latest(
        self, count: int = 20, *, query: Optional[MediaServerQueryOptions] = None
    ) -> List[_SchemaMediaServerPlayItem]:
        """
        查询最近新增专辑，并转换为仪表盘条目。

        :param count: 最多返回的专辑数，默认 20
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 按新增时间排序的专辑列表；查询失败时可能为空或仅含已读取部分
        """
        albums = (
            self._iter_albums(0, count, list_type="newest")
            if query is not None and query.fields is not None
            else self._albums("newest")[:count]
        )
        return [self._to_play_item(album, **({"query": query} if query is not None else {})) for album in albums]

    def get_resume(
        self, count: int = 20, *, query: Optional[MediaServerQueryOptions] = None
    ) -> List[_SchemaMediaServerPlayItem]:
        """
        查询服务端当前正在播放的音乐。

        :param count: 最多返回的播放条目数，默认 20
        :param query: 可选查询内容投影，省略时保留原有请求和返回内容
        :return: 服务端 getNowPlaying 返回范围内的条目，不再按用户名过滤；请求失败时返回空列表
        """
        payload = self._call("getNowPlaying")
        items = ((payload or {}).get("nowPlaying") or {}).get("entry") or []
        return [
            self._song_to_play_item(item, **({"query": query} if query is not None else {})) for item in items[:count]
        ]

    def get_play_url(self, item_id: str) -> Optional[str]:
        """
        构造带 Subsonic 鉴权参数的音频流地址。

        这里只构造地址，不验证条目存在或凭据有效；不使用仪表盘跳转地址 play_host。

        :param item_id: Navidrome 条目标识
        :return: 基于服务地址生成的 stream URL，包含用户名及随机盐生成的令牌；地址缺失时返回 None
        """
        url = self._api_url()
        if not url:
            return None
        return f"{url}/stream?{urlencode(self._params(id=item_id))}"

    def refresh_root_library(self) -> bool:
        """
        请求服务端执行一次增量音乐库扫描。

        :return: 扫描请求被接受时返回 True；请求失败时返回 False，不等待扫描完成
        """
        return self._call("startScan", fullScan=False) is not None
