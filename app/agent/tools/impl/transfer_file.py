"""整理文件或目录工具"""

from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.log import logger
from app.schemas.workflow import FileItem
from app.schemas.types import MediaType
from app.schemas.types import MUSIC_ENTITY_ALBUM, MUSIC_ENTITY_RECORDING, MediaSource
from app.domain.media import normalize_music_type


class TransferFileInput(BaseModel):
    """整理文件或目录工具的输入参数模型"""

    file_path: str = Field(
        ...,
        description="Path to the file or directory to transfer (e.g., '/path/to/file.mkv' or '/path/to/directory')",
    )
    storage: Optional[str] = Field(
        "local",
        description="Storage type of the source file (default: 'local', can be 'smb', 'alist', etc.)",
    )
    target_path: Optional[str] = Field(
        None,
        description="Target path for the transferred file/directory (optional, uses default library path if not specified)",
    )
    target_storage: Optional[str] = Field(
        None,
        description="Target storage type (optional, uses default storage if not specified)",
    )
    media_type: Optional[str] = Field(None, description="Allowed values: movie, tv, music")
    music_type: Optional[str] = Field(
        None,
        description="For music: recording for one audio file or album for one album directory. Artists cannot be transferred",
    )
    media_source: Optional[MediaSource] = Field(
        None,
        description="Media metadata source; for music use the MusicBrainz recording or album source from search_media",
    )
    media_id: Optional[str] = Field(
        None,
        description="Native ID for media_source; an album ID organizes the directory as one album",
    )
    season: Optional[int] = Field(
        None, description="Season number for TV shows (optional)"
    )
    transfer_type: Optional[str] = Field(
        None,
        description="Transfer mode: 'move' to move files, 'copy' to copy files, 'link' for hard link, 'softlink' for symbolic link (optional, uses default mode if not specified)",
    )
    background: Optional[bool] = Field(
        False,
        description="Whether to run transfer in background (default: False, runs synchronously)",
    )


class TransferFileTool(MoviePilotTool):
    """通过统一整理链将影视或音乐文件写入媒体库。"""

    name: str = "transfer_file"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.Transfer,
        ToolTag.Library,
        ToolTag.File,
        ToolTag.Admin,
    ]
    description: str = (
        "Transfer/organize movie, TV, or music files through MoviePilot's configured library pipeline. "
        "For a complete album, pass the album directory once with media_type='music', music_type='album', "
        "and its source-native ID; for one recording, pass one audio file with music_type='recording'."
    )
    args_schema: Type[BaseModel] = TransferFileInput
    require_admin: bool = True

    @staticmethod
    def _get_fileitem_type(
        file_path: str,
        storage: Optional[str] = "local",
        music_type: Optional[str] = None,
    ) -> str:
        """
        判断待整理路径的文件类型。

        :param file_path: 已规范化的源文件或目录路径
        :param storage: 源存储类型
        :return: ``dir`` 或 ``file``
        """
        if (storage or "local") == "local" and Path(file_path).is_dir():
            return "dir"
        if (storage or "local") != "local" and music_type == MUSIC_ENTITY_ALBUM:
            # 远程存储无法 stat，显式专辑语义比路径尾斜杠更可靠。
            return "dir"
        return "dir" if file_path.endswith("/") else "file"

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据整理参数生成友好的提示消息"""
        file_path = kwargs.get("file_path", "")
        media_type = kwargs.get("media_type")
        transfer_type = kwargs.get("transfer_type")
        background = kwargs.get("background", False)

        message = f"整理文件: {file_path}"
        if media_type:
            message += f" [{media_type}]"
        if transfer_type:
            transfer_map = {
                "move": "移动",
                "copy": "复制",
                "link": "硬链接",
                "softlink": "软链接",
            }
            message += f" 模式: {transfer_map.get(transfer_type, transfer_type)}"
        if background:
            message += " [后台运行]"

        return message

    @staticmethod
    def _transfer_file_sync(
        file_path: str,
        storage: Optional[str] = "local",
        target_path: Optional[str] = None,
        target_storage: Optional[str] = None,
        media_type: Optional[str] = None,
        music_type: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
        transfer_type: Optional[str] = None,
        background: Optional[bool] = False,
    ) -> str:
        """
        文件整理链路包含大量同步磁盘与外部服务调用，需要在线程池中运行。
        """
        if not file_path:
            return "错误：必须提供文件或目录路径"

        if storage == "local":
            if not file_path.startswith("/") and not (
                len(file_path) > 1 and file_path[1] == ":"
            ):
                file_path = str(Path(file_path).resolve())
        elif not file_path.startswith("/"):
            file_path = "/" + file_path

        media_type_enum = None
        if media_type:
            media_type_enum = MediaType.from_agent(media_type)
            if not media_type_enum:
                return f"错误：无效的媒体类型 '{media_type}'，支持的类型：'movie', 'tv', 'music'"
        normalized_music_type = None
        if music_type:
            normalized_music_type = normalize_music_type(
                music_type,
                allow_artist=False,
            )
            if not normalized_music_type:
                return (
                    f"错误：无效的音乐实体类型 '{music_type}'，"
                    "支持的类型：'recording', 'album'"
                )
            if media_type_enum != MediaType.MUSIC:
                return "错误：music_type 仅能与 media_type='music' 一起使用"
        if bool(media_source) != bool(media_id):
            return "错误：media_source 和 media_id 必须同时提供"
        if media_type_enum == MediaType.MUSIC and season is not None:
            return "错误：音乐没有季号，整理音乐时不能传入 season"

        fileitem = FileItem(
            storage=storage or "local",
            path=file_path,
            type=TransferFileTool._get_fileitem_type(
                file_path,
                storage,
                normalized_music_type,
            ),
        )
        if (
            media_type_enum == MediaType.MUSIC
            and normalized_music_type == MUSIC_ENTITY_ALBUM
            and fileitem.type != "dir"
        ):
            return "错误：专辑必须按一个目录整理，不能把单个音频文件作为整张专辑"
        if (
            media_type_enum == MediaType.MUSIC
            and normalized_music_type == MUSIC_ENTITY_RECORDING
            and fileitem.type != "file"
        ):
            return "错误：单曲必须按一个音频文件整理，不能把目录作为一首单曲"
        target_path_obj = Path(target_path) if target_path else None

        from app.chain.transfer import TransferChain

        state, errormsg = TransferChain().manual_transfer(
            fileitem=fileitem,
            target_storage=target_storage,
            target_path=target_path_obj,
            media_source=media_source,
            media_id=media_id,
            mtype=media_type_enum,
            season=season,
            transfer_type=transfer_type,
            background=background,
        )

        if state:
            if background:
                return f"整理任务已提交到后台运行：{file_path}"
            return f"整理成功：{file_path}"

        if isinstance(errormsg, list):
            error_text = f"整理完成，{len(errormsg)} 个文件转移失败"
            if errormsg:
                error_text += "：\n" + "\n".join(str(e) for e in errormsg[:5])
                if len(errormsg) > 5:
                    error_text += f"\n... 还有 {len(errormsg) - 5} 个错误"
        else:
            error_text = str(errormsg)
        return f"整理失败：{error_text}"

    async def run(
        self,
        file_path: str,
        storage: Optional[str] = "local",
        target_path: Optional[str] = None,
        target_storage: Optional[str] = None,
        media_type: Optional[str] = None,
        music_type: Optional[str] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        season: Optional[int] = None,
        transfer_type: Optional[str] = None,
        background: Optional[bool] = False,
        **kwargs,
    ) -> str:
        """在线程池中执行文件整理并返回最终状态。"""
        logger.info(
            f"执行工具: {self.name}, 参数: file_path={file_path}, storage={storage}, target_path={target_path}, "
            f"target_storage={target_storage}, media_type={media_type}, music_type={music_type}, "
            f"media_source={media_source}, media_id={media_id}, "
            f"season={season}, transfer_type={transfer_type}, background={background}"
        )

        try:
            return await self.run_blocking(
                "storage",
                self._transfer_file_sync,
                file_path,
                storage,
                target_path,
                target_storage,
                media_type,
                music_type,
                media_source,
                media_id,
                season,
                transfer_type,
                background,
            )
        except Exception as e:
            logger.error(f"整理文件失败: {e}", exc_info=True)
            return f"整理文件时发生错误: {str(e)}"
