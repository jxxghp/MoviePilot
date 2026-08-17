"""下载任务查询与控制应用服务。"""

from typing import Callable, List, Optional

from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import TorrentStatus


class DownloadTaskService:
    """通过下载器和历史端口查询、启停及删除下载任务。"""

    def __init__(
        self,
        list_torrents: Callable[..., List[DownloaderTorrent]],
        get_history_by_hashes: Callable[[list[str]], dict],
        start_torrents: Callable[..., bool],
        stop_torrents: Callable[..., bool],
        remove_torrents: Callable[..., bool],
    ) -> None:
        """注入下载器操作和历史读取端口。"""
        self._list_torrents = list_torrents
        self._get_history_by_hashes = get_history_by_hashes
        self._start_torrents = start_torrents
        self._stop_torrents = stop_torrents
        self._remove_torrents = remove_torrents

    def downloading(self, name: Optional[str] = None) -> List[DownloaderTorrent]:
        """查询下载中任务，并附加对应下载历史的媒体与用户信息。"""
        torrents = self._list_torrents(
            downloader=name,
            status=TorrentStatus.DOWNLOADING,
        )
        if not torrents:
            return []
        history_map = self._get_history_by_hashes(
            [torrent.hash for torrent in torrents if torrent.hash]
        )
        for torrent in torrents:
            history = history_map.get(torrent.hash)
            if not history:
                continue
            torrent.media = {
                "media_source": history.media_source,
                "media_id": history.media_id,
                "type": history.type,
                "title": history.title,
                "season": history.seasons,
                "episode": history.episodes,
                "image": history.poster,
                "poster": history.poster,
                "backdrop": history.image,
            }
            torrent.site_name = history.torrent_site
            torrent.userid = history.userid
            torrent.username = history.username
        return torrents

    def set_downloading(
        self,
        hash_str: str,
        operation: str,
        name: Optional[str] = None,
    ) -> bool:
        """按 start/stop 操作控制单个下载任务。"""
        if operation == "start":
            return self._start_torrents(hashs=[hash_str], downloader=name)
        if operation == "stop":
            return self._stop_torrents(hashs=[hash_str], downloader=name)
        return False

    def remove_downloading(
        self,
        hash_str: str,
        name: Optional[str] = None,
    ) -> bool:
        """删除单个下载任务。"""
        return self._remove_torrents(hashs=[hash_str], downloader=name)
