"""整理与分类域的能力端口客户端。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Dict, List, Optional

from app.application.orchestration.ports.dispatch import CapabilityPorts
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.category import CategoryConfig
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo
from app.schemas.workflow import FileItem


class TransferPorts(CapabilityPorts):
    """文件整理、整理后处理与二级分类配置的能力端口。"""

    def transfer(
            self,
            fileitem: FileItem,
            meta: MetaBase,
            mediainfo: MediaInfo,
            target_directory: TransferDirectoryConf = None,
            target_storage: Optional[str] = None,
            target_path: Path = None,
            transfer_type: Optional[str] = None,
            scrape: bool = None,
            library_type_folder: bool = None,
            library_category_folder: bool = None,
            episodes_info: List[TmdbEpisode] = None,
            source_oper: Callable = None,
            target_oper: Callable = None,
            preview: bool = False,
    ) -> Optional[TransferInfo]:
        """
        文件转移
        :param fileitem:  文件信息
        :param meta: 预识别的元数据
        :param mediainfo:  识别的媒体信息
        :param target_directory:  目标目录配置
        :param target_storage:  目标存储
        :param target_path:  目标路径
        :param transfer_type:  转移模式
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按类型创建目录
        :param library_category_folder: 是否按类别创建目录
        :param episodes_info: 当前季的全部集信息
        :param source_oper:  源存储操作类
        :param target_oper:  目标存储操作类
        :param preview: 是否仅预览，不执行实际转移
        :return: {path, target_path, message}
        """
        return self._dispatch.unicast(
            "transfer",
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            target_directory=target_directory,
            target_path=target_path,
            target_storage=target_storage,
            transfer_type=transfer_type,
            scrape=scrape,
            library_type_folder=library_type_folder,
            library_category_folder=library_category_folder,
            episodes_info=episodes_info,
            source_oper=source_oper,
            target_oper=target_oper,
            preview=preview,
        )

    def transfer_completed(self, hashs: str, downloader: Optional[str] = None) -> None:
        """
        下载器转移完成后的处理
        :param hashs:  种子Hash
        :param downloader:  下载器
        """
        self._dispatch.broadcast(
            "transfer_completed", hashs=hashs, downloader=downloader
        )

    def media_category(self) -> Optional[Dict[str, list]]:
        """
        获取媒体分类
        :return: 获取二级分类配置字典项，需包括电影、电视剧
        """
        return self._dispatch.unicast("media_category")

    def category_config(self) -> CategoryConfig:
        """
        获取分类策略配置
        """
        return self._dispatch.unicast("load_category_config")

    def save_category_config(self, config: CategoryConfig) -> bool:
        """
        保存分类策略配置
        """
        return self._dispatch.unicast("save_category_config", config=config)
