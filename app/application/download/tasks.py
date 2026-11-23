"""
下载任务查询与控制应用服务。
"""

import re
from typing import Any, Callable, List, Optional

from app.application.directory import validate_download_save_path
from app.application.download.validation import validate_torrent_hash
from app.application.history import DownloadHistorySnapshot
from app.schemas.transfer import DownloaderTorrent, DownloadTaskMedia
from app.schemas.types import TorrentStatus

TASK_REFERENCE_PREFIX = "mp-task-"
_TASK_REFERENCE = re.compile(r"mp-task-[0-9a-f]{32}")


class DownloadTaskService:
    """
    通过下载器和历史端口查询、启停及删除下载任务。
    """

    def __init__(
        self,
        list_torrents: Callable[..., List[DownloaderTorrent]],
        get_history_by_hashes: Callable[
            [list[str]],
            dict[str, DownloadHistorySnapshot],
        ],
        start_torrents: Callable[..., bool],
        stop_torrents: Callable[..., bool],
        remove_torrents: Callable[..., bool],
        *,
        get_owned_tasks: Callable[[str, list[str]], dict[str, DownloadHistorySnapshot]] | None = None,
    ) -> None:
        """
        注入下载器操作和历史读取端口。
        """
        self._list_torrents = list_torrents
        self._get_history_by_hashes = get_history_by_hashes
        self._start_torrents = start_torrents
        self._stop_torrents = stop_torrents
        self._remove_torrents = remove_torrents
        self._get_owned_tasks = get_owned_tasks

    def downloading(self, name: Optional[str] = None, *, owner_identity: str | None = None) -> List[DownloaderTorrent]:
        """
        查询下载中任务，并附加对应下载历史的媒体与用户信息。
        """
        torrents = self._list_torrents(
            downloader=name,
            status=TorrentStatus.DOWNLOADING,
        )
        if not torrents:
            return []
        if owner_identity is not None:
            return self._owned_downloading(torrents, owner_identity)
        history_map = self._get_history_by_hashes([torrent.hash for torrent in torrents if torrent.hash])
        for torrent in torrents:
            if not torrent.hash:
                continue
            history = history_map.get(torrent.hash)
            if not history:
                continue
            self._populate_history(torrent, history)
        return torrents

    def _owned_downloading(
        self, torrents: list[DownloaderTorrent], owner_identity: str,
    ) -> list[DownloaderTorrent]:
        """
        只返回与该账户的提交记录、下载器和任务 Hash 同时匹配的当前任务。
        """
        lookup = self._get_owned_tasks
        if not lookup or not owner_identity:
            return []
        tagged = [(torrent, self._read_reference(torrent.tags)) for torrent in torrents]
        references = sorted({reference for _, reference in tagged if reference})
        history_map = lookup(owner_identity, references)
        owned = []
        for torrent, reference in tagged:
            history = history_map.get(reference) if reference else None
            if (history and torrent.hash and torrent.downloader
                    and torrent.hash == history.download_hash and torrent.downloader == history.downloader):
                snapshot = torrent.model_copy(update={
                    "tags": ",".join(tag for tag in (torrent.tags or "").split(",") if tag.strip() != reference),
                })
                self._populate_history(snapshot, history)
                owned.append(snapshot)
        return owned

    @staticmethod
    def _read_reference(tags: str | None) -> str | None:
        """
        取当前任务的唯一提交标识。无法区分的多标识任务不授予本人可见性。
        """
        references = {value for tag in (tags or "").split(",") if _TASK_REFERENCE.fullmatch(value := tag.strip())}
        return references.pop() if len(references) == 1 else None

    @staticmethod
    def _populate_history(torrent: DownloaderTorrent, history: DownloadHistorySnapshot) -> None:
        """
        把已定位的下载历史展示信息填充到任务对象。
        """
        torrent.media = DownloadTaskMedia(
            media_source=history.media_source,
            media_id=history.media_id,
            type=history.type,
            title=history.title,
            season=history.seasons,
            episode=history.episodes,
            image=history.poster,
            poster=history.poster,
            backdrop=history.image,
        )
        torrent.site_name = history.torrent_site
        torrent.userid = history.userid
        torrent.username = history.username

    def set_downloading(
        self,
        hash_str: str,
        operation: str,
        name: Optional[str] = None,
    ) -> bool:
        """
        按 start/stop 操作控制单个下载任务。
        """
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
        """
        删除单个下载任务。
        """
        return self._remove_torrents(hashs=[hash_str], downloader=name)


class DownloadTaskMutationService:
    """
    执行单个下载任务的受控高级修改。
    """

    def __init__(
        self,
        *,
        list_torrents: Callable[..., list[Any]],
        set_tags: Callable[..., bool],
        set_downloading: Callable[..., bool],
        update_torrent: Callable[..., dict[str, bool]],
    ) -> None:
        """
        注入下载器查询和修改端口。
        """
        self._list_torrents = list_torrents
        self._set_tags = set_tags
        self._set_downloading = set_downloading
        self._update_torrent = update_torrent

    @staticmethod
    def _normalize_list(values: Optional[list[str]]) -> Optional[list[str]]:
        """
        移除字符串列表中的空项。
        """
        if values is None:
            return None
        return [str(value).strip() for value in values if str(value).strip()]

    @staticmethod
    def _result(operation: str, success: bool, message: str) -> dict[str, Any]:
        """
        构造一个稳定的子操作响应。
        """
        return {
            "operation": operation,
            "success": success,
            "message": message,
        }

    def update(
        self,
        *,
        hash_value: str,
        action: Optional[str] = None,
        tags: Optional[list[str]] = None,
        downloader: Optional[str] = None,
        download_limit: Optional[float] = None,
        upload_limit: Optional[float] = None,
        trackers: Optional[list[str]] = None,
        save_path: Optional[str] = None,
        category: Optional[str] = None,
        ratio_limit: Optional[float] = None,
        seeding_time_limit: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        校验并执行启停、标签、限速、Tracker 与保存位置修改。
        """
        validate_torrent_hash(hash_value)
        if action not in {None, "start", "stop"}:
            raise ValueError("action 只支持 start 或 stop")
        tags = self._normalize_list(tags)
        trackers = self._normalize_list(trackers)
        mutation_values = (
            action,
            tags,
            download_limit,
            upload_limit,
            trackers,
            save_path,
            category,
            ratio_limit,
            seeding_time_limit,
        )
        if not any(value is not None and value != [] for value in mutation_values):
            raise ValueError("至少需要指定一个要更新的字段")
        resolved_downloader = downloader
        if not resolved_downloader:
            torrents = (
                self._list_torrents(
                    hashs=[hash_value],
                    include_all_tags=True,
                )
                or []
            )
            resolved_downloader = getattr(torrents[0], "downloader", None) if torrents else None
        if not resolved_downloader:
            raise ValueError("未找到下载任务或下载器不可用")
        if save_path is not None:
            save_path = validate_download_save_path(save_path)
        results = []
        if tags:
            success = bool(
                self._set_tags(
                    hashs=[hash_value],
                    tags=tags,
                    downloader=resolved_downloader,
                )
            )
            results.append(
                self._result(
                    "tags",
                    success,
                    f"成功设置标签：{', '.join(tags)}" if success else "设置标签失败",
                )
            )
        if action:
            success = bool(
                self._set_downloading(
                    hash_str=hash_value,
                    oper=action,
                    name=resolved_downloader,
                )
            )
            action_name = "开始" if action == "start" else "暂停"
            results.append(
                self._result(
                    action,
                    success,
                    f"成功{action_name}下载任务" if success else f"{action_name}下载任务失败",
                )
            )
        advanced_values = (
            download_limit,
            upload_limit,
            trackers,
            save_path,
            category,
            ratio_limit,
            seeding_time_limit,
        )
        if any(value is not None and value != [] for value in advanced_values):
            update_result = (
                self._update_torrent(
                    hash_string=hash_value,
                    downloader=resolved_downloader,
                    download_limit=download_limit,
                    upload_limit=upload_limit,
                    tracker_list=trackers,
                    save_path=save_path,
                    category=category,
                    ratio_limit=ratio_limit,
                    seeding_time_limit=seeding_time_limit,
                )
                or {}
            )
            labels = {
                "limits": "限速/做种策略",
                "trackers": "Tracker",
                "save_path": "保存目录",
                "category": "分类",
            }
            for operation, success in update_result.items():
                label = labels.get(operation, operation)
                results.append(
                    self._result(
                        operation,
                        bool(success),
                        f"{label}修改成功" if success else f"{label}修改失败或下载器不支持",
                    )
                )
        return {
            "hash": hash_value,
            "downloader": resolved_downloader,
            "results": results,
        }
