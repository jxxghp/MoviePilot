"""查询下载工具"""

import json
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.download import DownloadChain
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.runtime.log import logger
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import MUSIC_ENTITY_RECORDING, TorrentQueryStatus, media_type_to_agent


class QueryDownloadTasksInput(BaseModel):
    """查询下载工具的输入参数模型"""
    downloader: Optional[str] = Field(None,
                                      description="Name of specific downloader to query (optional, if not provided queries all configured downloaders)")
    status: Optional[str] = Field("all",
                                  description="Filter downloads by status: 'downloading' for active downloads, 'completed' for finished downloads, 'paused' for paused downloads, 'all' for all downloads")
    include_all_tags: Optional[bool] = Field(
        False,
        description="Include tasks without the MoviePilot built-in tag. Default false keeps the normal MoviePilot task scope.",
    )
    include_trackers: Optional[bool] = Field(
        False,
        description="Include tracker URLs when supported. Hash queries always include trackers.",
    )
    hash: Optional[str] = Field(None, description="Query specific download task by hash (optional, if provided will search for this specific task regardless of status)")
    title: Optional[str] = Field(None, description="Query download tasks by title/name (optional, supports partial match, searches all tasks if provided)")
    tag: Optional[str] = Field(None, description="Filter download tasks by tag (optional, supports partial match, e.g. 'movie' will match tasks with tag 'movie' or 'movie_2024')")


class QueryDownloadTasksTool(MoviePilotTool):
    """查询下载器任务并用下载历史补全影视或音乐身份。"""

    name: str = "query_download_tasks"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.Download,
    ]
    description: str = "Query download status and list download tasks. Can query all active downloads, or search for specific tasks by hash, title, or tag. Shows download progress, completion status, tags, and task details from configured downloaders."
    args_schema: Type[BaseModel] = QueryDownloadTasksInput

    @staticmethod
    def _normalize_query_status(status: Optional[str]) -> TorrentQueryStatus:
        """
        归一下载任务查询状态。
        """
        status_value = str(status or "").strip().lower()
        if not status_value or status_value == TorrentQueryStatus.ALL.value:
            return TorrentQueryStatus.ALL
        if status_value in {"completed", "complete", "seeding"}:
            return TorrentQueryStatus.COMPLETED
        if status_value in {"paused", "pause"}:
            return TorrentQueryStatus.PAUSED
        if status_value == TorrentQueryStatus.DOWNLOADING.value:
            return TorrentQueryStatus.DOWNLOADING
        return TorrentQueryStatus.ALL

    @staticmethod
    def _normalize_include_all_tags(include_all_tags: Any) -> bool:
        """
        归一全部标签查询开关。
        """
        if isinstance(include_all_tags, bool):
            return include_all_tags
        if isinstance(include_all_tags, str):
            return include_all_tags.strip().lower() in {"1", "true", "yes", "on", "是"}
        return bool(include_all_tags)

    @staticmethod
    def _get_all_torrents(
        download_chain: DownloadChain,
        downloader: Optional[str] = None,
        include_all_tags: bool = False,
    ) -> List[DownloaderTorrent]:
        """
        查询所有状态的任务（包括下载中和已完成的任务）
        """
        return download_chain.list_torrents(
            downloader=downloader,
            include_all_tags=include_all_tags,
        ) or []

    @staticmethod
    def _format_progress(progress: Optional[float]) -> Optional[str]:
        """
        将下载进度格式化为保留一位小数的百分比字符串
        """
        try:
            if progress is None:
                return None
            return f"{float(progress):.1f}%"
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _apply_download_history(
        torrent: DownloaderTorrent, history: Any
    ) -> None:
        """将下载历史中的补充信息回填到下载任务结果中。"""
        if not history:
            return
        if hasattr(torrent, "media"):
            media_payload = {
                "type": history.type,
                "title": history.title,
                "season": history.seasons,
                "episode": history.episodes,
                "image": history.image,
                "poster": history.poster,
                "media_source": history.media_source,
                "media_id": history.media_id,
            }
            music_note = (
                (history.note or {}).get("music")
                if isinstance(history.note, dict)
                else None
            ) or {}
            music_media = music_note.get("media") or {}
            if media_type_to_agent(history.type) == "music":
                media_payload.update({
                    "music_type": music_media.get("music_type") or MUSIC_ENTITY_RECORDING,
                    "artists": music_media.get("artists") or [],
                    "album": music_media.get("album"),
                    "album_id": music_media.get("album_id"),
                    "total_tracks": music_media.get("total_tracks"),
                    "track_number": music_media.get("track_number"),
                })
            torrent.media = media_payload
        if hasattr(torrent, "username"):
            torrent.username = history.username
        torrent.userid = history.userid

    @classmethod
    def _load_history_map(
        cls, torrents: List[DownloaderTorrent]
    ) -> Dict[str, Any]:
        """批量加载下载历史，避免逐条查询形成 N+1。"""
        hashes = [torrent.hash for torrent in torrents if getattr(torrent, "hash", None)]
        if not hashes:
            return {}
        return DownloadHistoryOper().get_by_hashes(hashes)

    @classmethod
    def _query_downloads_sync(
        cls,
        downloader: Optional[str] = None,
        status: Optional[str] = "all",
        hash_value: Optional[str] = None,
        title: Optional[str] = None,
        tag: Optional[str] = None,
        include_all_tags: bool = False,
        include_trackers: bool = False,
    ) -> Dict[str, Any]:
        """
        同步查询下载器和下载历史，整个链路放在线程池中执行。
        """
        download_chain = DownloadChain()
        query_status = cls._normalize_query_status(status)
        include_all_tags = cls._normalize_include_all_tags(include_all_tags)

        if hash_value:
            torrents = (
                download_chain.list_torrents(
                    downloader=downloader,
                    hashs=[hash_value],
                    include_all_tags=include_all_tags,
                )
                or []
            )
            if not torrents:
                return {
                    "message": f"未找到hash为 {hash_value} 的下载任务（该任务可能已完成、已删除或不存在）"
                }

            history_map = cls._load_history_map(torrents)
            for torrent in torrents:
                cls._apply_download_history(torrent, history_map.get(torrent.hash))
            filtered_downloads = list(torrents)
        elif title:
            all_torrents = cls._get_all_torrents(
                download_chain,
                downloader,
                include_all_tags=include_all_tags,
            )
            history_map = cls._load_history_map(all_torrents)
            filtered_downloads = []
            title_lower = title.lower()

            for torrent in all_torrents:
                history = history_map.get(torrent.hash)
                matched = title_lower in (torrent.title or "").lower() or title_lower in (
                    getattr(torrent, "name", None) or ""
                ).lower()
                if not matched and history and history.title:
                    matched = title_lower in history.title.lower()

                if not matched:
                    continue

                cls._apply_download_history(torrent, history)
                filtered_downloads.append(torrent)

            if not filtered_downloads:
                return {"message": f"未找到标题包含 '{title}' 的下载任务"}
        else:
            if query_status == TorrentQueryStatus.DOWNLOADING and not include_all_tags:
                downloads = download_chain.downloading(name=downloader) or []
                filtered_downloads = [
                    dl
                    for dl in downloads
                    if not downloader or dl.downloader == downloader
                ]
            else:
                list_status = None if query_status == TorrentQueryStatus.ALL else query_status.value
                filtered_downloads = download_chain.list_torrents(
                    downloader=downloader,
                    status=list_status,
                    include_all_tags=include_all_tags,
                ) or []

                history_map = cls._load_history_map(filtered_downloads)
                for torrent in filtered_downloads:
                    cls._apply_download_history(torrent, history_map.get(torrent.hash))

        if tag and filtered_downloads:
            tag_lower = tag.lower()
            filtered_downloads = [
                d for d in filtered_downloads if d.tags and tag_lower in d.tags.lower()
            ]
            if not filtered_downloads:
                return {"message": f"未找到标签包含 '{tag}' 的下载任务"}

        if not filtered_downloads:
            return {"message": "未找到相关下载任务"}

        if hash_value or include_trackers:
            for torrent in filtered_downloads:
                if not getattr(torrent, "hash", None):
                    continue
                tracker_map = download_chain.get_torrent_trackers(
                    hash_string=torrent.hash,
                    downloader=getattr(torrent, "downloader", None) or downloader,
                ) or {}
                torrent.trackers = tracker_map.get(getattr(torrent, "downloader", None)) or []

        return {"downloads": filtered_downloads}

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        downloader = kwargs.get("downloader")
        status = kwargs.get("status", "all")
        hash_value = kwargs.get("hash")
        title = kwargs.get("title")
        include_all_tags = self._normalize_include_all_tags(
            kwargs.get("include_all_tags", False)
        )
        
        parts = ["查询下载任务"]
        
        if downloader:
            parts.append(f"下载器: {downloader}")
        
        if status != "all":
            status_map = {"downloading": "下载中", "completed": "已完成", "paused": "已暂停"}
            parts.append(f"状态: {status_map.get(status, status)}")
        
        if hash_value:
            parts.append(f"Hash: {hash_value[:8]}...")
        elif title:
            parts.append(f"标题: {title}")

        tag = kwargs.get("tag")
        if tag:
            parts.append(f"标签: {tag}")
        if include_all_tags:
            parts.append("范围: 全部标签")
        if kwargs.get("include_trackers"):
            parts.append("包含Tracker")
        
        return " | ".join(parts) if len(parts) > 1 else parts[0]

    async def run(self, downloader: Optional[str] = None,
                  status: Optional[str] = "all",
                  hash: Optional[str] = None,
                  title: Optional[str] = None,
                  tag: Optional[str] = None,
                  include_all_tags: Optional[bool] = False,
                  include_trackers: Optional[bool] = False,
                  **kwargs) -> str:
        """执行下载任务查询并输出有界的状态摘要。"""
        logger.info(
            f"执行工具: {self.name}, 参数: downloader={downloader}, status={status}, "
            f"hash={hash}, title={title}, tag={tag}, include_all_tags={include_all_tags}, "
            f"include_trackers={include_trackers}"
        )
        try:
            payload = await self.run_blocking(
                "downloader",
                self._query_downloads_sync,
                downloader,
                status,
                hash,
                title,
                tag,
                self._normalize_include_all_tags(include_all_tags),
                self._normalize_include_all_tags(include_trackers),
            )
            if payload.get("message"):
                return payload["message"]

            filtered_downloads = payload.get("downloads") or []
            if filtered_downloads:
                # 限制最多20条结果
                total_count = len(filtered_downloads)
                limited_downloads = filtered_downloads[:20]
                # 精简字段，只保留关键信息
                simplified_downloads = []
                for d in limited_downloads:
                    simplified = {
                        "downloader": d.downloader,
                        "hash": d.hash,
                        "title": d.title,
                        "name": getattr(d, "name", None),
                        "year": getattr(d, "year", None),
                        "season_episode": getattr(d, "season_episode", None),
                        "size": d.size,
                        "progress": self._format_progress(d.progress),
                        "state": d.state,
                        "upspeed": getattr(d, "upspeed", None),
                        "dlspeed": getattr(d, "dlspeed", None),
                        "tags": d.tags,
                        "save_path": getattr(d, "save_path", None),
                        "content_path": getattr(d, "content_path", None) or (
                            d.path.as_posix() if getattr(d, "path", None) else None
                        ),
                        "category": getattr(d, "category", None),
                        "download_limit": getattr(d, "download_limit", None),
                        "upload_limit": getattr(d, "upload_limit", None),
                        "ratio_limit": getattr(d, "ratio_limit", None),
                        "seeding_time_limit": getattr(d, "seeding_time_limit", None),
                        "trackers": getattr(d, "trackers", None) or [],
                        "left_time": getattr(d, "left_time", None)
                    }
                    # 精简 media 字段
                    media = getattr(d, "media", None)
                    if media:
                        simplified["media"] = {
                            "type": media_type_to_agent(media.get("type")),
                            "title": media.get("title"),
                            "season": media.get("season"),
                            "episode": media.get("episode"),
                            "media_source": media.get("media_source"),
                            "media_id": media.get("media_id"),
                            "music_type": media.get("music_type"),
                            "artists": media.get("artists"),
                            "album": media.get("album"),
                            "album_id": media.get("album_id"),
                            "total_tracks": media.get("total_tracks"),
                            "track_number": media.get("track_number"),
                        }
                    simplified_downloads.append(simplified)
                result_json = json.dumps(simplified_downloads, ensure_ascii=False, indent=2)
                # 如果结果被裁剪，添加提示信息
                if total_count > 20:
                    return f"注意：查询结果共找到 {total_count} 条，为节省上下文空间，仅显示前 20 条结果。\n\n{result_json}"
                
                # 如果查询的是特定hash或title，添加明确的状态信息
                if hash:
                    return f"找到hash为 {hash} 的下载任务：\n\n{result_json}"
                elif title:
                    return f"找到 {total_count} 个标题包含 '{title}' 的下载任务：\n\n{result_json}"
                
                return result_json
            return "未找到相关下载任务"
        except Exception as e:
            logger.error(f"查询下载失败: {e}", exc_info=True)
            return f"查询下载时发生错误: {str(e)}"
