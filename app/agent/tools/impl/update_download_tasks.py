"""更新下载任务工具"""

import json
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.chain.download import DownloadChain
from app.helper.directory import validate_download_save_path
from app.log import logger


class UpdateDownloadTasksInput(BaseModel):
    """更新下载任务工具的输入参数模型"""

    hash: str = Field(
        ..., description="Task hash (can be obtained from query_download_tasks tool)"
    )
    action: Optional[str] = Field(
        None,
        description="Action to perform on the task: 'start' to resume downloading, 'stop' to pause downloading.",
    )
    tags: Optional[List[str]] = Field(
        None,
        description="List of tags to add to the download task. Example: ['movie', 'hd']",
    )
    downloader: Optional[str] = Field(
        None,
        description="Name of specific downloader. If omitted, the tool resolves it from the task hash.",
    )
    download_limit: Optional[float] = Field(
        None,
        description="Per-task download speed limit in KB/s. Use 0 to disable the limit when supported.",
    )
    upload_limit: Optional[float] = Field(
        None,
        description="Per-task upload speed limit in KB/s. Use 0 to disable the limit when supported.",
    )
    trackers: Optional[List[str]] = Field(
        None,
        description="Tracker URL list to add or set, depending on downloader support.",
    )
    save_path: Optional[str] = Field(
        None,
        description="New save/download directory for the task, when supported.",
    )
    category: Optional[str] = Field(
        None,
        description="Downloader category to set, when supported.",
    )
    ratio_limit: Optional[float] = Field(
        None,
        description="Per-task share ratio limit, when supported.",
    )
    seeding_time_limit: Optional[int] = Field(
        None,
        description="Per-task seeding time limit in minutes, when supported.",
    )


class UpdateDownloadTasksTool(MoviePilotTool):
    """更新下载任务工具"""

    name: str = "update_download_tasks"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Download,
        ToolTag.Admin,
    ]
    description: str = (
        "Update a download task by hash. Supports start/stop, adding tags, per-task "
        "upload/download speed limits, trackers, save directory, category, share ratio, "
        "and seeding time where the configured downloader supports them. "
        "Use query_download_tasks first to get the hash and current downloader."
    )
    args_schema: Type[BaseModel] = UpdateDownloadTasksInput
    require_admin: bool = True

    @staticmethod
    def _is_valid_hash(hash_value: str) -> bool:
        """校验下载任务Hash格式。"""
        return len(hash_value) == 40 and all(c in "0123456789abcdefABCDEF" for c in hash_value)

    @staticmethod
    def _normalize_non_empty_list(values: Optional[List[str]]) -> Optional[List[str]]:
        """清理字符串列表中的空值。"""
        if values is None:
            return None
        return [str(value).strip() for value in values if str(value).strip()]

    @staticmethod
    def _has_update_params(**kwargs) -> bool:
        """判断是否传入至少一个修改参数。"""
        return any(value is not None and value != [] for value in kwargs.values())

    @staticmethod
    def _build_result(operation: str, success: bool, message: str) -> Dict[str, Any]:
        """构造单项操作结果。"""
        return {
            "operation": operation,
            "success": success,
            "message": message,
        }

    @classmethod
    def _resolve_downloader(
            cls,
            download_chain: DownloadChain,
            hash_value: str,
            downloader: Optional[str],
    ) -> Optional[str]:
        """根据Hash解析下载任务所在下载器。"""
        if downloader:
            return downloader
        torrents = download_chain.list_torrents(
            hashs=[hash_value],
            include_all_tags=True,
        ) or []
        return getattr(torrents[0], "downloader", None) if torrents else None

    @classmethod
    def _update_download_sync(
            cls,
            hash_value: str,
            action: Optional[str] = None,
            tags: Optional[List[str]] = None,
            downloader: Optional[str] = None,
            download_limit: Optional[float] = None,
            upload_limit: Optional[float] = None,
            trackers: Optional[List[str]] = None,
            save_path: Optional[str] = None,
            category: Optional[str] = None,
            ratio_limit: Optional[float] = None,
            seeding_time_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """同步更新下载任务，避免下载器 SDK 阻塞事件循环。"""
        download_chain = DownloadChain()
        resolved_downloader = cls._resolve_downloader(
            download_chain=download_chain,
            hash_value=hash_value,
            downloader=downloader,
        )
        if not resolved_downloader:
            return {
                "hash": hash_value,
                "downloader": downloader,
                "results": [
                    cls._build_result("resolve_downloader", False, "未找到下载任务或下载器不可用")
                ],
            }

        if save_path is not None:
            try:
                save_path = validate_download_save_path(save_path)
            except ValueError:
                return {
                    "hash": hash_value,
                    "downloader": resolved_downloader,
                    "results": [
                        cls._build_result("save_path", False, "保存目录不在允许的下载目录范围内")
                    ],
                }

        results = []
        if tags:
            tag_result = download_chain.set_torrents_tag(
                hashs=[hash_value], tags=tags, downloader=resolved_downloader
            )
            results.append(
                cls._build_result(
                    "tags",
                    bool(tag_result),
                    f"成功设置标签：{', '.join(tags)}" if tag_result else "设置标签失败",
                )
            )

        if action:
            action_result = download_chain.set_downloading(
                hash_str=hash_value, oper=action, name=resolved_downloader
            )
            action_desc = "开始" if action == "start" else "暂停"
            results.append(
                cls._build_result(
                    action,
                    bool(action_result),
                    f"成功{action_desc}下载任务" if action_result else f"{action_desc}下载任务失败",
                )
            )

        update_result = {}
        if cls._has_update_params(
                download_limit=download_limit,
                upload_limit=upload_limit,
                trackers=trackers,
                save_path=save_path,
                category=category,
                ratio_limit=ratio_limit,
                seeding_time_limit=seeding_time_limit,
        ):
            update_result = download_chain.update_torrent(
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
        operation_messages = {
            "limits": "限速/做种策略",
            "trackers": "Tracker",
            "save_path": "保存目录",
            "category": "分类",
        }
        for operation, success in (update_result or {}).items():
            label = operation_messages.get(operation, operation)
            results.append(
                cls._build_result(
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

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据更新参数生成友好的提示消息。"""
        hash_value = kwargs.get("hash", "")
        parts = [f"更新下载任务: {hash_value}"]
        action = kwargs.get("action")
        if action == "start":
            parts.append("操作: 开始下载")
        elif action == "stop":
            parts.append("操作: 暂停下载")
        if kwargs.get("tags"):
            parts.append(f"标签: {', '.join(kwargs.get('tags'))}")
        if kwargs.get("download_limit") is not None or kwargs.get("upload_limit") is not None:
            parts.append("限速")
        if kwargs.get("trackers") is not None:
            parts.append("Tracker")
        if kwargs.get("save_path"):
            parts.append("保存目录")
        if kwargs.get("category") is not None:
            parts.append("分类")
        if kwargs.get("downloader"):
            parts.append(f"下载器: {kwargs.get('downloader')}")
        return " | ".join(parts)

    async def run(
        self,
        hash: str,
        action: Optional[str] = None,
        tags: Optional[List[str]] = None,
        downloader: Optional[str] = None,
        download_limit: Optional[float] = None,
        upload_limit: Optional[float] = None,
        trackers: Optional[List[str]] = None,
        save_path: Optional[str] = None,
        category: Optional[str] = None,
        ratio_limit: Optional[float] = None,
        seeding_time_limit: Optional[int] = None,
        **kwargs,
    ) -> str:
        """执行下载任务更新。"""
        logger.info(
            f"执行工具: {self.name}, 参数: hash={hash}, action={action}, tags={tags}, "
            f"downloader={downloader}, download_limit={download_limit}, upload_limit={upload_limit}, "
            f"trackers={trackers}, save_path={save_path}, category={category}, "
            f"ratio_limit={ratio_limit}, seeding_time_limit={seeding_time_limit}"
        )
        try:
            if not self._is_valid_hash(hash):
                return "参数错误：hash 格式无效，请先使用 query_download_tasks 工具获取正确的 hash。"

            tags = self._normalize_non_empty_list(tags)
            trackers = self._normalize_non_empty_list(trackers)
            if action and action not in ("start", "stop"):
                return f"参数错误：action 只支持 'start'（开始下载）或 'stop'（暂停下载），收到: '{action}'。"
            if not self._has_update_params(
                    action=action,
                    tags=tags,
                    download_limit=download_limit,
                    upload_limit=upload_limit,
                    trackers=trackers,
                    save_path=save_path,
                    category=category,
                    ratio_limit=ratio_limit,
                    seeding_time_limit=seeding_time_limit,
            ):
                return "参数错误：至少需要指定一个要更新的字段。"

            result = await self.run_blocking(
                "downloader",
                self._update_download_sync,
                hash,
                action,
                tags,
                downloader,
                download_limit,
                upload_limit,
                trackers,
                save_path,
                category,
                ratio_limit,
                seeding_time_limit,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"更新下载任务失败: {e}", exc_info=True)
            return f"更新下载任务时发生错误: {str(e)}"
