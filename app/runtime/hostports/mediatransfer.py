"""文件整理的宿主服务端口。

扩展层按本协议推算整理目标路径并执行整理，实现由组合根注入。
"""

from pathlib import Path
from typing import Any, List, Optional, Protocol, runtime_checkable

from app.runtime.hostports.port import HostPort
from app.runtime.hostports.storages import StorageOperations
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo


@runtime_checkable
class MediaTransferProvider(Protocol):
    """整理目标路径推算与文件整理执行的协议。"""

    def get_dest_path(
            self,
            mediainfo: Any,
            target_path: Path,
            need_type_folder: Optional[bool] = False,
            need_category_folder: Optional[bool] = False,
    ) -> Path:
        """
        在自定义目标路径上拼装媒体类型与类别子目录。

        :param mediainfo: 识别的媒体信息（``app.domain.context.MediaInfo``）
        :param target_path: 自定义目标路径
        :param need_type_folder: 是否按媒体类型创建目录
        :param need_category_folder: 是否按媒体类别创建目录
        :return: 拼装后的目标路径
        """
        ...

    def get_dest_dir(
            self,
            mediainfo: Any,
            target_dir: TransferDirectoryConf,
            need_type_folder: Optional[bool] = None,
            need_category_folder: Optional[bool] = None,
    ) -> Path:
        """
        按媒体库目录配置拼装媒体类型与类别子目录。

        :param mediainfo: 识别的媒体信息（``app.domain.context.MediaInfo``）
        :param target_dir: 媒体库目录配置
        :param need_type_folder: 是否按媒体类型创建目录，为空时取目录配置
        :param need_category_folder: 是否按媒体类别创建目录，为空时取目录配置
        :return: 拼装后的媒体库目录
        """
        ...

    def get_naming_dict(
            self,
            meta: Any,
            mediainfo: Any,
            file_ext: Optional[str] = None,
            episodes_info: List[TmdbEpisode] = None,
    ) -> dict:
        """
        构建重命名模板可用的变量字典。

        :param meta: 文件元数据（``app.domain.meta.metabase.MetaBase``）
        :param mediainfo: 识别的媒体信息（``app.domain.context.MediaInfo``）
        :param file_ext: 文件扩展名
        :param episodes_info: 当前季的全部集信息
        :return: 重命名变量字典
        """
        ...

    def get_rename_path(
            self,
            template_string: str,
            rename_dict: dict,
            path: Optional[Path] = None,
            source_path: Optional[str] = None,
            source_item: Optional[FileItem] = None,
    ) -> Path:
        """
        渲染重命名模板并生成完整路径。

        :param template_string: 重命名模板
        :param rename_dict: 重命名变量字典
        :param path: 拼接生成路径的基础路径
        :param source_path: 待整理的文件路径
        :param source_item: 待整理的文件项
        :return: 重命名后的完整路径
        """
        ...

    def transfer_media(
            self,
            fileitem: FileItem,
            in_meta: Any,
            mediainfo: Any,
            target_storage: str,
            target_path: Path,
            transfer_type: str,
            source_oper: StorageOperations,
            target_oper: StorageOperations,
            need_scrape: Optional[bool] = False,
            need_rename: Optional[bool] = True,
            need_notify: Optional[bool] = True,
            overwrite_mode: Optional[str] = None,
            episodes_info: List[TmdbEpisode] = None,
            preview: Optional[bool] = False,
    ) -> TransferInfo:
        """
        整理一个文件或一个目录下的所有文件。

        :param fileitem: 待整理的文件或目录项
        :param in_meta: 预识别元数据（``app.domain.meta.metabase.MetaBase``）
        :param mediainfo: 识别的媒体信息（``app.domain.context.MediaInfo``）
        :param target_storage: 目标存储
        :param target_path: 目标路径
        :param transfer_type: 文件整理方式
        :param source_oper: 源存储操作对象
        :param target_oper: 目标存储操作对象
        :param need_scrape: 是否需要刮削
        :param need_rename: 是否需要重命名
        :param need_notify: 是否需要通知
        :param overwrite_mode: 覆盖模式
        :param episodes_info: 当前季的全部集信息
        :param preview: 是否仅预览
        :return: 整理结果
        """
        ...


media_transfer_port: HostPort[MediaTransferProvider] = HostPort("文件整理")
