"""Navidrome Subsonic/OpenSubsonic API 客户端。"""

import hashlib
import re
import secrets
from typing import Any, Dict, Generator, List, Optional
from urllib.parse import urlencode

from app.schemas.dashboard import Statistic as _SchemaStatistic
from app.schemas.mediaserver import MediaServerItem as _SchemaMediaServerItem
from app.schemas.mediaserver import MediaServerLibrary as _SchemaMediaServerLibrary
from app.schemas.mediaserver import MediaServerPlayItem as _SchemaMediaServerPlayItem
from app.runtime.log import logger
from app.schemas.types import MediaType
from app.adapters.network.http import RequestUtils
from app.foundation.url import UrlUtils


class Navidrome:
    """通过 Subsonic API 访问 Navidrome 音乐库。"""

    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        play_host: Optional[str] = None,
        sync_libraries: Optional[list] = None,
        **kwargs: Any,
    ) -> None:
        """初始化 Navidrome 连接配置。"""
        self._host = UrlUtils.standardize_base_url(host) if host else None
        self._play_host = UrlUtils.standardize_base_url(play_host) if play_host else self._host
        self._username = username or ""
        self._password = password or ""
        self._sync_libraries = sync_libraries or []
        self.user = self.get_user()

    def _api_url(self) -> Optional[str]:
        """返回 Subsonic REST 根地址。"""
        if not self._host:
            return None
        return f"{self._host.rstrip('/')}/rest"

    def _params(self, **kwargs: Any) -> Dict[str, Any]:
        """构造 Subsonic token 鉴权参数。"""
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

    def _call(self, method: str, **kwargs: Any) -> Optional[dict]:
        """调用 Subsonic 方法并返回成功响应体。"""
        url = self._api_url()
        if not url or not self._username or not self._password:
            return None
        response = None
        try:
            response = RequestUtils().get_res(f"{url}/{method}", self._params(**kwargs))
            if not response:
                return None
            payload = response.json().get("subsonic-response", {})
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
        """判断当前连接是否不可用。"""
        return bool(self._host and self._username and self._password and not self.user)

    def reconnect(self) -> None:
        """重新验证 Navidrome 连接。"""
        self.user = self.get_user()

    def get_user(self) -> Optional[str]:
        """验证凭据并返回配置用户名。"""
        return self._username if self._call("ping") else None

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """验证用户凭据，成功时返回 Navidrome 用户名。"""
        if not username or not password or not self._host:
            return None
        original_username, original_password = self._username, self._password
        try:
            self._username, self._password = username, password
            return username if self._call("ping") else None
        finally:
            self._username, self._password = original_username, original_password

    def get_user_count(self) -> int:
        """返回当前凭据可确认的用户数量。"""
        return 1 if self.user else 0

    @staticmethod
    def _album_name(album: dict) -> str:
        """从 Subsonic 专辑对象提取稳定标题。"""
        return album.get("name") or album.get("album") or ""

    def _album_to_item(self, album: dict) -> _SchemaMediaServerItem:
        """将 Subsonic 专辑转换为统一媒体条目。"""
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

    def _song_to_item(self, song: dict) -> _SchemaMediaServerItem:
        """将 Subsonic 单曲转换为统一音乐条目。"""
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
        """忽略大小写、空白和标点比较 Navidrome 音乐名称。"""
        normalized_left = re.sub(r"[^\w]+", "", str(left or "").casefold())
        normalized_right = re.sub(r"[^\w]+", "", str(right or "").casefold())
        return bool(normalized_left) and normalized_left == normalized_right

    @classmethod
    def _same_artist(cls, item: dict, artist: Optional[str]) -> bool:
        """检查专辑或单曲的艺术家是否与目标一致；目标为空时不限制。"""
        if not artist:
            return True
        return any(
            cls._same_name(candidate, artist)
            for candidate in (item.get("artist"), item.get("albumArtist"))
        )

    def _album_cover(self, album: dict) -> Optional[str]:
        """返回专辑封面地址。"""
        cover_id = album.get("coverArt")
        if not cover_id:
            return None
        url = self._api_url()
        if not url:
            return None
        return f"{url}/getCoverArt?{urlencode(self._params(id=cover_id, size=512))}"

    def _albums(self, list_type: str = "alphabeticalByName", size: int = 500) -> List[dict]:
        """分页读取专辑列表。"""
        albums: List[dict] = []
        offset = 0
        while True:
            payload = self._call("getAlbumList2", type=list_type, size=size, offset=offset)
            page = ((payload or {}).get("albumList2") or {}).get("album") or []
            if not page:
                break
            albums.extend(page)
            if len(page) < size:
                break
            offset += len(page)
        return albums

    def get_medias_count(self) -> _SchemaStatistic:
        """统计 Navidrome 专辑数量并映射为音乐数量。"""
        return _SchemaStatistic(music_count=len(self._albums())) if self.user else _SchemaStatistic()

    def get_librarys(self, hidden: Optional[bool] = False) -> Optional[List[_SchemaMediaServerLibrary]]:
        """返回单个虚拟音乐库。"""
        if not self.user or (hidden and self._sync_libraries and "all" not in self._sync_libraries):
            return []
        count = self.get_items_count("music")
        return [_SchemaMediaServerLibrary(
            server="navidrome",
            id="music",
            item_id="music",
            name="音乐",
            type=MediaType.MUSIC.value,
            item_count=count,
            link=self._play_host,
            server_type="navidrome",
        )]

    def get_items_count(self, library_id: str = "music") -> int:
        """返回虚拟音乐库中的专辑数量。"""
        return len(self._albums()) if self.user else 0

    def get_items(self, start_index: int = 0, limit: int = -1) -> Generator[_SchemaMediaServerItem, None, None]:
        """逐条返回 Navidrome 专辑。"""
        albums = self._albums()
        end = None if limit is None or limit == -1 else start_index + limit
        for album in albums[start_index:end]:
            yield self._album_to_item(album)

    def get_iteminfo(self, item_id: str) -> Optional[_SchemaMediaServerItem]:
        """获取 Navidrome 专辑详情。"""
        payload = self._call("getAlbum", id=item_id)
        album = ((payload or {}).get("album") or {})
        return self._album_to_item(album) if album else None

    def search_music(
        self, title: Optional[str] = None, artist: Optional[str] = None,
        album: Optional[str] = None,
    ) -> List[_SchemaMediaServerItem]:
        """按歌曲或专辑名称精确筛选音乐条目，避免模糊搜索误报已入库。"""
        target = album or title
        query = " ".join(dict.fromkeys(filter(None, [target, artist]))).strip()
        if not query:
            return []
        payload = self._call("search3", query=query, artistCount=0, albumCount=20, songCount=20)
        result = ((payload or {}).get("searchResult3") or {})
        albums = result.get("album") or []
        songs = result.get("song") or []
        if album:
            return [
                self._album_to_item(item)
                for item in albums
                if self._same_name(self._album_name(item), album)
                and self._same_artist(item, artist)
            ]
        return [
            self._song_to_item(item)
            for item in songs
            if self._same_name(item.get("title") or item.get("name"), title)
            and self._same_artist(item, artist)
        ]

    def _to_play_item(self, album: dict) -> _SchemaMediaServerPlayItem:
        """将专辑转换为仪表盘播放/最新条目。"""
        album_id = str(album.get("id") or "")
        return _SchemaMediaServerPlayItem(
            id=album_id,
            item_id=album_id,
            title=self._album_name(album),
            subtitle=album.get("artist") or album.get("albumArtist"),
            type=MediaType.MUSIC.value,
            image=self._album_cover(album),
            link=self._play_host,
            server_type="navidrome",
        )

    def _song_to_play_item(self, song: dict) -> _SchemaMediaServerPlayItem:
        """将正在播放的单曲转换为仪表盘条目，避免把所属专辑名误作曲名。"""
        song_id = str(song.get("id") or "")
        return _SchemaMediaServerPlayItem(
            id=song_id,
            item_id=song_id,
            title=song.get("title") or song.get("name") or "",
            subtitle=song.get("artist") or song.get("albumArtist"),
            type=MediaType.MUSIC.value,
            image=self._album_cover(song),
            link=self._play_host,
            server_type="navidrome",
        )

    def get_latest(self, count: int = 20) -> List[_SchemaMediaServerPlayItem]:
        """返回最近新增专辑。"""
        return [self._to_play_item(album) for album in self._albums("newest")[:count]]

    def get_resume(self, count: int = 20) -> List[_SchemaMediaServerPlayItem]:
        """返回当前用户正在播放的音乐。"""
        payload = self._call("getNowPlaying")
        items = ((payload or {}).get("nowPlaying") or {}).get("entry") or []
        return [self._song_to_play_item(item) for item in items[:count]]

    def get_play_url(self, item_id: str) -> Optional[str]:
        """返回 Navidrome 流播放地址。"""
        url = self._api_url()
        if not url:
            return None
        return f"{url}/stream?{urlencode(self._params(id=item_id))}"

    def refresh_root_library(self) -> bool:
        """请求 Navidrome 执行增量音乐库扫描。"""
        return self._call("startScan", fullScan=False) is not None
