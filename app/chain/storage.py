"""
存储模块的文件查询、操作与路径访问编排。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, cast

from app.application.directory import DirectoryHelper
from app.chain.base import ChainBase
from app.runtime.log import logger
from app.schemas.file import FileItem as _SchemaFileItem


class StorageChain(ChainBase):
    """
    存储处理链
    """

    def manage_storage(self, storage: str, action: str, **params: Any) -> Dict[str, Any]:
        """
        对指定网盘存储执行管理动作

        不包含任何存储特定逻辑：存储标识、动作语义与参数解释全部封闭在
        实现 storage_manage 契约的模块内部

        :param storage: 存储类型标识，用于模块路由
        :param action: 通用管理动作标识，具体语义由存储实现解释
        :param params: 表单与动作参数，原样透传给模块
        :return: 统一结构 {"success": bool, "message": ..., "data": ...}
        """
        result = cast(Dict[str, Any], self.run_module("storage_manage", storage=storage, action=action, **params))
        return result or {"success": False, "message": "该存储类型未启用或不支持此管理动作"}

    def list_files(self, fileitem: _SchemaFileItem, recursion: bool = False) -> Optional[List[_SchemaFileItem]]:
        """
        查询当前目录下所有目录和文件
        """
        return cast(Optional[List[_SchemaFileItem]], self.run_module("list_files", fileitem=fileitem, recursion=recursion))

    def any_files(self, fileitem: _SchemaFileItem, extensions: Optional[Sequence[str]] = None) -> Optional[bool]:
        """
        查询当前目录下是否存在指定扩展名任意文件
        """
        return cast(Optional[bool], self.run_module("any_files", fileitem=fileitem, extensions=extensions))

    def create_folder(self, fileitem: _SchemaFileItem, name: str) -> Optional[_SchemaFileItem]:
        """
        创建目录
        """
        return cast(Optional[_SchemaFileItem], self.run_module("create_folder", fileitem=fileitem, name=name))

    def get_folder(self, storage: str, path: Path) -> Optional[_SchemaFileItem]:
        """
        获取目录，不存在则递归创建
        """
        return cast(Optional[_SchemaFileItem], self.run_module("get_folder", storage=storage, path=path))

    def download_file(self, fileitem: _SchemaFileItem, path: Optional[Path] = None) -> Optional[Path]:
        """
        下载文件

        :param fileitem: 文件项
        :param path: 本地保存路径
        """
        return cast(Optional[Path], self.run_module("download_file", fileitem=fileitem, path=path))

    def upload_file(self, fileitem: _SchemaFileItem, path: Path,
                    new_name: Optional[str] = None) -> Optional[_SchemaFileItem]:
        """
        上传文件

        :param fileitem: 保存目录项
        :param path: 本地文件路径
        :param new_name: 新文件名
        """
        return cast(Optional[_SchemaFileItem], self.run_module("upload_file", fileitem=fileitem, path=path, new_name=new_name))

    def link_file(self, fileitem: _SchemaFileItem, target_file: Path) -> Optional[bool]:
        """
        尝试在同一存储中为文件创建硬链接。

        :param fileitem: 已存在的源文件项
        :param target_file: 目标存储中的完整文件路径
        :return: 存储实现报告的成功标记；未提供硬链接能力时返回 None
        """
        return cast(Optional[bool], self.run_module("link_file", fileitem=fileitem, target_file=target_file))

    def delete_file(self, fileitem: _SchemaFileItem) -> Optional[bool]:
        """
        删除文件或目录
        """
        return cast(Optional[bool], self.run_module("delete_file", fileitem=fileitem))

    def rename_file(self, fileitem: _SchemaFileItem, name: str) -> Optional[bool]:
        """
        重命名文件或目录
        """
        return cast(Optional[bool], self.run_module("rename_file", fileitem=fileitem, name=name))

    def exists(self, fileitem: _SchemaFileItem) -> Optional[bool]:
        """
        判断文件或目录是否存在
        """
        return True if self.get_item(fileitem) else False

    def get_item(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        查询目录或文件
        """
        if not fileitem.storage or not fileitem.path:
            return None
        return self.get_file_item(storage=fileitem.storage, path=Path(fileitem.path))

    def get_file_item(self, storage: str, path: Path) -> Optional[_SchemaFileItem]:
        """
        根据路径获取文件项
        """
        return cast(Optional[_SchemaFileItem], self.run_module("get_file_item", storage=storage, path=path))

    def get_file_item_strict(
            self,
            storage: str,
            path: Path,
    ) -> Optional[_SchemaFileItem]:
        """
        严格查询文件项：确认不存在返回空，能力提供方 或 I/O 失败直接抛出。
        """
        return cast(
            Optional[_SchemaFileItem],
            self.run_module_strict(
                "get_file_item",
                storage=storage,
                path=path,
            ),
        )

    def get_parent_item(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        获取上级目录项
        """
        return cast(Optional[_SchemaFileItem], self.run_module("get_parent_item", fileitem=fileitem))

    def snapshot_storage(self, storage: str, path: Path,
                         last_snapshot_time: Optional[float] = None, max_depth: int = 5,
                         previous_snapshot: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[Dict[str, Dict[str, Any]]]:
        """
        快照存储

        :param storage: 存储类型
        :param path: 路径
        :param last_snapshot_time: 上次快照时间，用于增量快照
        :param max_depth: 最大递归深度，避免过深遍历
        :param previous_snapshot: 上次完整快照，用于增量对账
        """
        return cast(Optional[Dict[str, Dict[str, Any]]], self.run_module("snapshot_storage", storage=storage, path=path,
                               last_snapshot_time=last_snapshot_time, max_depth=max_depth,
                               previous_snapshot=previous_snapshot))

    def is_bluray_folder(self, fileitem: Optional[_SchemaFileItem]) -> bool:
        """
        检查是否蓝光目录
        """
        if not fileitem or fileitem.type != "dir" or not fileitem.storage or not fileitem.path:
            return False
        if self.get_file_item(storage=fileitem.storage, path=Path(fileitem.path) / "BDMV"):
            return True
        if self.get_file_item(storage=fileitem.storage, path=Path(fileitem.path) / "CERTIFICATE"):
            return True
        return False

    @staticmethod
    def contains_bluray_subdirectories(fileitems: Optional[List[_SchemaFileItem]]) -> bool:
        """
        判断是否包含蓝光必备的文件夹
        """
        required_files = {"BDMV", "CERTIFICATE"}
        return any(
            item.type == "dir" and item.name in required_files
            for item in fileitems or []
        )

    def delete_media_file(self, fileitem: _SchemaFileItem, delete_self: bool = True) -> bool:
        """
        删除媒体文件，以及不含媒体文件的目录

        向上清理止于同一存储中的配置根目录、类型或分类目录，保留共享目录结构。
        """
        media_exts = self.runtime_config.media_extensions
        fileitem_path = Path(fileitem.path) if fileitem.path else Path("")
        if len(fileitem_path.parts) <= 2:
            logger.warn(f"【{fileitem.storage}】{fileitem.path} 根目录或一级目录不允许删除")
            return False
        if fileitem.type == "dir":
            if self.is_bluray_folder(fileitem):
                logger.warn(f"正在删除蓝光原盘目录：【{fileitem.storage}】{fileitem.path}")
                if not self.delete_file(fileitem):
                    logger.warn(f"【{fileitem.storage}】{fileitem.path} 删除失败")
                    return False

        elif delete_self:
            logger.warn(f"正在删除文件【{fileitem.storage}】{fileitem.path}")
            if not self.delete_file(fileitem):
                logger.warn(f"【{fileitem.storage}】{fileitem.path} 删除失败")
                return False

        dir_item = fileitem if fileitem.type == "dir" else self.get_parent_item(fileitem)
        if not dir_item:
            logger.warn(f"【{fileitem.storage}】{fileitem.path} 上级目录不存在")
            return True

        associated_dir = DirectoryHelper().get_media_cleanup_root(fileitem)

        while dir_item and dir_item.path and len(Path(dir_item.path).parts) > 2:
            # 共享目录边界及其祖先必须保留，边界下的影片目录仍可清理。
            if associated_dir and associated_dir.is_relative_to(Path(dir_item.path)):
                logger.debug(f"【{dir_item.storage}】{dir_item.path} 位于资源或媒体库目录结构中，不删除")
                break

            elif not associated_dir and self.list_files(dir_item, recursion=False):
                logger.debug(f"【{dir_item.storage}】{dir_item.path} 不是空目录，不删除")
                break

            if self.any_files(dir_item, extensions=media_exts) is not False:
                logger.debug(f"【{dir_item.storage}】{dir_item.path} 存在媒体文件，不删除")
                break

            logger.warn(f"【{dir_item.storage}】{dir_item.path} 不存在其它媒体文件，正在删除空目录")
            if not self.delete_file(dir_item):
                logger.warn(f"【{dir_item.storage}】{dir_item.path} 删除失败")
                return False
            dir_item = self.get_parent_item(dir_item)

        return True
