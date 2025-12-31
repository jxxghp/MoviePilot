"""查询下载工具"""

import json
from typing import Optional, Type, List, Union

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.chain.download import DownloadChain
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.log import logger
from app.schemas import TransferTorrent, DownloadingTorrent
from app.schemas.types import TorrentStatus


class QueryDownloadTasksInput(BaseModel):
    """查询下载工具的输入参数模型"""
    explanation: str = Field(..., description="Clear explanation of why this tool is being used in the current context")
    downloader: Optional[str] = Field(None,
                                      description="Name of specific downloader to query (optional, if not provided queries all configured downloaders)")
    status: Optional[str] = Field("all",
                                  description="Filter downloads by status: 'downloading' for active downloads, 'completed' for finished downloads, 'paused' for paused downloads, 'all' for all downloads")
    hash: Optional[str] = Field(None, description="Query specific download task by hash (optional, if provided will search for this specific task regardless of status)")
    title: Optional[str] = Field(None, description="Query download tasks by title/name (optional, supports partial match, searches all tasks if provided)")


class QueryDownloadTasksTool(MoviePilotTool):
    name: str = "query_download_tasks"
    description: str = "Query download status and list download tasks. Can query all active downloads, or search for specific tasks by hash or title. Shows download progress, completion status, and task details from configured downloaders."
    args_schema: Type[BaseModel] = QueryDownloadTasksInput

    def _get_all_torrents(self, download_chain: DownloadChain, downloader: Optional[str] = None) -> List[Union[TransferTorrent, DownloadingTorrent]]:
        """
        查询所有状态的任务（包括下载中和已完成的任务）
        """
        all_torrents = []
        # 查询正在下载的任务
        downloading_torrents = download_chain.list_torrents(
            downloader=downloader, 
            status=TorrentStatus.DOWNLOADING
        ) or []
        all_torrents.extend(downloading_torrents)
        
        # 查询已完成的任务（可转移状态）
        transfer_torrents = download_chain.list_torrents(
            downloader=downloader,
            status=TorrentStatus.TRANSFER
        ) or []
        all_torrents.extend(transfer_torrents)
        
        return all_torrents

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息"""
        downloader = kwargs.get("downloader")
        status = kwargs.get("status", "all")
        hash_value = kwargs.get("hash")
        title = kwargs.get("title")
        
        parts = ["正在查询下载任务"]
        
        if downloader:
            parts.append(f"下载器: {downloader}")
        
        if status != "all":
            status_map = {"downloading": "下载中", "completed": "已完成", "paused": "已暂停"}
            parts.append(f"状态: {status_map.get(status, status)}")
        
        if hash_value:
            parts.append(f"Hash: {hash_value[:8]}...")
        elif title:
            parts.append(f"标题: {title}")
        
        return " | ".join(parts) if len(parts) > 1 else parts[0]

    async def run(self, downloader: Optional[str] = None,
                  status: Optional[str] = "all",
                  hash: Optional[str] = None,
                  title: Optional[str] = None, **kwargs) -> str:
        logger.info(f"执行工具: {self.name}, 参数: downloader={downloader}, status={status}, hash={hash}, title={title}")
        try:
            download_chain = DownloadChain()
            
            # 如果提供了hash，直接查询该hash的任务（不限制状态）
            if hash:
                torrents = download_chain.list_torrents(downloader=downloader, hashs=[hash]) or []
                if not torrents:
                    return f"未找到hash为 {hash} 的下载任务（该任务可能已完成、已删除或不存在）"
                # 转换为DownloadingTorrent格式
                downloads = []
                for torrent in torrents:
                    # 获取下载历史信息
                    history = DownloadHistoryOper().get_by_hash(torrent.hash)
                    if history:
                        torrent.media = {
                            "tmdbid": history.tmdbid,
                            "type": history.type,
                            "title": history.title,
                            "season": history.seasons,
                            "episode": history.episodes,
                            "image": history.image,
                        }
                        torrent.userid = history.userid
                        torrent.username = history.username
                    downloads.append(torrent)
                filtered_downloads = downloads
            elif title:
                # 如果提供了title，查询所有任务并搜索匹配的标题
                # 查询所有状态的任务
                all_torrents = self._get_all_torrents(download_chain, downloader)
                filtered_downloads = []
                title_lower = title.lower()
                for torrent in all_torrents:
                    # 获取下载历史信息
                    history = DownloadHistoryOper().get_by_hash(torrent.hash)
                    
                    # 检查标题或名称是否匹配（包括下载历史中的标题）
                    matched = False
                    # 检查torrent的title和name字段
                    if (title_lower in (torrent.title or "").lower()) or \
                       (title_lower in (torrent.name or "").lower()):
                        matched = True
                    # 检查下载历史中的标题
                    if history and history.title:
                        if title_lower in history.title.lower():
                            matched = True
                    
                    if matched:
                        if history:
                            torrent.media = {
                                "tmdbid": history.tmdbid,
                                "type": history.type,
                                "title": history.title,
                                "season": history.seasons,
                                "episode": history.episodes,
                                "image": history.image,
                            }
                            torrent.userid = history.userid
                            torrent.username = history.username
                        filtered_downloads.append(torrent)
                if not filtered_downloads:
                    return f"未找到标题包含 '{title}' 的下载任务"
            else:
                # 根据status决定查询方式
                if status == "downloading":
                    # 如果status为下载中，使用downloading方法
                    downloads = download_chain.downloading(name=downloader) or []
                    filtered_downloads = []
                    for dl in downloads:
                        if downloader and dl.downloader != downloader:
                            continue
                        filtered_downloads.append(dl)
                else:
                    # 其他状态（completed、paused、all），使用list_torrents查询所有任务
                    # 查询所有状态的任务
                    all_torrents = self._get_all_torrents(download_chain, downloader)
                    filtered_downloads = []
                    for torrent in all_torrents:
                        if downloader and torrent.downloader != downloader:
                            continue
                        # 根据status过滤
                        if status == "completed":
                            # 已完成的任务（state为seeding或completed）
                            if torrent.state not in ["seeding", "completed"]:
                                continue
                        elif status == "paused":
                            # 已暂停的任务
                            if torrent.state != "paused":
                                continue
                        # status == "all" 时不过滤
                        # 获取下载历史信息
                        history = DownloadHistoryOper().get_by_hash(torrent.hash)
                        if history:
                            torrent.media = {
                                "tmdbid": history.tmdbid,
                                "type": history.type,
                                "title": history.title,
                                "season": history.seasons,
                                "episode": history.episodes,
                                "image": history.image,
                            }
                            torrent.userid = history.userid
                            torrent.username = history.username
                        filtered_downloads.append(torrent)
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
                        "name": d.name,
                        "year": d.year,
                        "season_episode": d.season_episode,
                        "size": d.size,
                        "progress": d.progress,
                        "state": d.state,
                        "upspeed": d.upspeed,
                        "dlspeed": d.dlspeed,
                        "left_time": d.left_time
                    }
                    # 精简 media 字段
                    if d.media:
                        simplified["media"] = {
                            "tmdbid": d.media.get("tmdbid"),
                            "type": d.media.get("type"),
                            "title": d.media.get("title"),
                            "season": d.media.get("season"),
                            "episode": d.media.get("episode")
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
