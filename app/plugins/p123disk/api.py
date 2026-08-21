"""123 云盘的目录与元数据操作。

本类不读宿主配置、也不认识存储实例：连接与存储令牌都由调用方经两个取值回调交进来。
一个 `P123Api` 只服务一个存储实例，路径到文件 ID 的缓存因此归它私有——同一份缓存被
两个账号共用时，「/媒体库」在甲账号里的 ID 会被拿去访问乙账号，取回的是另一个人的
文件。
"""

import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional

from app.schemas import FileItem, StorageQueryError, StorageUsage
from app.sdk.logging import logger

from .client import check_response
from .fileitem import TYPE_DIRECTORY, build_file_item, is_directory, join_path

# 根目录在 123 云盘里的固定文件 ID
ROOT_FILE_ID = "0"

# 单次目录列举的条目数上限
_PAGE_SIZE = 100

# 分页游标走到末尾时接口返回的取值
_PAGE_END = "-1"

# 连续翻页之间的间隔秒数，用于避开接口频率限制
_PAGE_INTERVAL_SECONDS = 1

# 重命名时的重名处理方式：保留两者并自动加后缀
_RENAME_DUPLICATE_POLICY = 2


class P123Api:
    """123 云盘的目录与元数据操作。

    :param client: 取用客户端的回调，凭据变化后取到的是重建过的连接
    :param storage_token: 取用本实例存储令牌的回调，产出的文件项按它打戳
    """

    def __init__(
        self, client: Callable[[], Any], storage_token: Callable[[], str]
    ) -> None:
        """记录取值回调并建立本实例私有的路径缓存。"""
        self._client = client
        self._storage_token = storage_token
        self._id_cache: Dict[str, str] = {}

    @property
    def client(self) -> Any:
        """本实例当前可用的客户端。"""
        return self._client()

    @property
    def storage_token(self) -> str:
        """本实例的存储令牌。"""
        return self._storage_token()

    def forget_path(self, path: str) -> None:
        """
        丢弃一条路径的文件 ID 缓存

        :param path: 存储内的绝对路径
        """
        self._id_cache.pop(_cache_key(path), None)

    def remember_path(self, path: str, file_id: str) -> None:
        """
        记住一条路径的文件 ID

        :param path: 存储内的绝对路径
        :param file_id: 该路径对应的文件 ID
        """
        self._id_cache[_cache_key(path)] = str(file_id)

    def path_to_id(self, path: str) -> str:
        """
        按路径逐级查得文件 ID

        :param path: 存储内的绝对路径
        :return: 文件 ID
        :raises FileNotFoundError: 路径上某一级不存在
        """
        key = _cache_key(path)
        if key == "/":
            return ROOT_FILE_ID
        cached = self._id_cache.get(key)
        if cached:
            return cached

        current_id = ROOT_FILE_ID
        parent_path = "/"
        for ancestor in PurePosixPath(key).parents:
            ancestor_key = ancestor.as_posix()
            if ancestor_key in self._id_cache:
                parent_path = ancestor_key
                current_id = self._id_cache[ancestor_key]
                break

        for part in PurePosixPath(key).relative_to(parent_path).parts:
            current_id = self._find_child_id(current_id, part, path)
        self._id_cache[key] = str(current_id)
        return str(current_id)

    def _find_child_id(self, parent_id: str, name: str, path: str) -> str:
        """
        在指定目录下按名称查得子项的文件 ID

        :param parent_id: 父目录的文件 ID
        :param name: 子项名称
        :param path: 完整路径，仅用于报错时说明找不到的是谁
        :return: 子项的文件 ID
        :raises FileNotFoundError: 该目录下没有同名子项
        """
        for entry in self._iter_entries(parent_id):
            if entry.get("FileName") == name:
                return str(entry["FileId"])
        raise FileNotFoundError(f"【123云盘】{path} 不存在")

    def _iter_entries(self, parent_id: str):
        """
        逐页列举目录下的全部条目

        :param parent_id: 父目录的文件 ID
        :return: 条目字典的迭代器
        :raises StorageQueryError: 目录列举中途失败，结果不完整
        """
        page = 1
        cursor = 0
        while True:
            if page > 1:
                time.sleep(_PAGE_INTERVAL_SECONDS)
            try:
                response = self._client().fs_list(
                    {
                        "limit": _PAGE_SIZE,
                        "next": cursor,
                        "Page": page,
                        "parentFileId": int(parent_id),
                        "inDirectSpace": "false",
                    }
                )
                check_response(response)
            except Exception as error:
                raise StorageQueryError(
                    f"【123云盘】列举目录失败：{parent_id} - {error}"
                ) from error
            data = response.get("data") or {}
            entries = data.get("InfoList")
            if not entries:
                return
            yield from entries
            if data.get("Next") == _PAGE_END:
                return
            page += 1
            cursor = data.get("Next")

    def list(self, fileitem: FileItem) -> List[FileItem]:
        """
        浏览目录，文件项本身则返回它的详情

        :param fileitem: 目录项或文件项
        :return: 子项列表；传入的是文件时为该文件的详情单项列表
        :raises StorageQueryError: 目录列举中途失败
        """
        if fileitem.type == "file":
            detail = self.detail(fileitem)
            return [detail] if detail else []

        parent_id = fileitem.fileid or self.path_to_id(fileitem.path)
        token = self._storage_token()
        items: List[FileItem] = []
        for entry in self._iter_entries(parent_id):
            path = join_path(
                fileitem.path,
                str(entry.get("FileName") or ""),
                is_directory=is_directory(entry),
            )
            self.remember_path(path, entry["FileId"])
            items.append(build_file_item(entry, storage_token=token, path=path))
        return items

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        """
        在指定目录下创建子目录

        :param fileitem: 父目录项
        :param name: 目录名
        :return: 创建出的目录项；创建失败时为 None
        """
        path = join_path(fileitem.path, name, is_directory=True)
        try:
            response = self._client().fs_mkdir(
                name, parent_id=self.path_to_id(fileitem.path)
            )
            check_response(response)
            entry = dict(response["data"]["Info"])
        except Exception as error:
            logger.error(f"【123云盘】创建目录 {path} 失败：{error}")
            return None
        entry.setdefault("FileName", name)
        entry.setdefault("Type", TYPE_DIRECTORY)
        self.remember_path(path, entry["FileId"])
        return build_file_item(
            entry, storage_token=self._storage_token(), path=path
        )

    def get_folder(self, path: Path) -> Optional[FileItem]:
        """
        取得目录，不存在时逐级创建

        :param path: 目录路径
        :return: 目录项；中途创建失败时为 None
        """
        existing = self.get_item(path)
        if existing:
            return existing
        current = FileItem(storage=self._storage_token(), path="/", type="dir")
        for part in path.parts[1:]:
            child = self._find_child_directory(current, part)
            if child is None:
                child = self.create_folder(current, part)
            if child is None:
                return None
            current = child
        return current

    def _find_child_directory(
        self, fileitem: FileItem, name: str
    ) -> Optional[FileItem]:
        """
        在指定目录下按名称查找子目录

        :param fileitem: 父目录项
        :param name: 子目录名
        :return: 子目录项；不存在时为 None
        """
        for child in self.list(fileitem):
            if child.type == "dir" and child.name == name:
                return child
        return None

    def get_item(self, path: Path) -> Optional[FileItem]:
        """
        按路径取得文件或目录

        :param path: 文件或目录路径
        :return: 文件项；不存在或查询失败时为 None
        """
        try:
            return self._query_item(path)
        except Exception as error:
            logger.debug(f"【123云盘】查询 {path} 失败：{error}")
            return None

    def get_item_strict(self, path: Path) -> Optional[FileItem]:
        """
        按路径取得文件或目录，无法确认状态时报错而不是当作不存在

        覆盖保护按「目标不存在」放行，把查询失败混同为不存在会让保护形同虚设，
        因此确认不存在与无法确认必须分开回答。

        :param path: 文件或目录路径
        :return: 文件项；确认不存在时为 None
        :raises StorageQueryError: 网络或接口异常导致无法确认目标状态
        """
        try:
            return self._query_item(path)
        except FileNotFoundError:
            return None
        except StorageQueryError:
            raise
        except Exception as error:
            raise StorageQueryError(
                f"【123云盘】查询 {path} 失败：{error}"
            ) from error

    def _query_item(self, path: Path) -> Optional[FileItem]:
        """
        向接口查询一个文件项

        :param path: 文件或目录路径
        :return: 文件项
        :raises FileNotFoundError: 路径确认不存在
        """
        file_id = self.path_to_id(path.as_posix())
        response = self._client().fs_info(int(file_id))
        check_response(response)
        entries = (response.get("data") or {}).get("infoList") or []
        if not entries:
            raise FileNotFoundError(f"【123云盘】{path} 不存在")
        return build_file_item(
            entries[0],
            storage_token=self._storage_token(),
            path=path.as_posix(),
        )

    def get_parent(self, fileitem: FileItem) -> Optional[FileItem]:
        """
        取得上级目录项

        :param fileitem: 文件项
        :return: 上级目录项；不存在时为 None
        """
        return self.get_item(Path(fileitem.path).parent)

    def detail(self, fileitem: FileItem) -> Optional[FileItem]:
        """
        取得文件详情

        :param fileitem: 文件项
        :return: 带完整信息的文件项；查询失败时为 None
        """
        return self.get_item(Path(fileitem.path))

    def delete(self, fileitem: FileItem) -> bool:
        """
        把文件或目录移入回收站

        :param fileitem: 待删除的文件项
        :return: 删除成功时为 True
        """
        try:
            check_response(
                self._client().fs_trash(
                    int(self._resolve_id(fileitem)), event="intoRecycle"
                )
            )
        except Exception as error:
            logger.error(f"【123云盘】删除 {fileitem.path} 失败：{error}")
            return False
        self.forget_path(fileitem.path)
        return True

    def rename(self, fileitem: FileItem, name: str) -> bool:
        """
        重命名文件或目录

        :param fileitem: 待重命名的文件项
        :param name: 新名称
        :return: 重命名成功时为 True
        """
        try:
            check_response(
                self._client().fs_rename(
                    {
                        "FileId": int(self._resolve_id(fileitem)),
                        "fileName": name,
                        "duplicate": _RENAME_DUPLICATE_POLICY,
                    }
                )
            )
        except Exception as error:
            logger.error(f"【123云盘】重命名 {fileitem.path} 失败：{error}")
            return False
        self.forget_path(fileitem.path)
        return True

    def copy(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件或目录到指定目录并改名

        :param fileitem: 待复制的文件项
        :param path: 目标目录路径
        :param new_name: 复制后的名称
        :return: 复制并改名都成功时为 True
        """
        return self._transfer(
            fileitem, path, new_name, operation="fs_copy", action="复制"
        )

    def move(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件或目录到指定目录并改名

        :param fileitem: 待移动的文件项
        :param path: 目标目录路径
        :param new_name: 移动后的名称
        :return: 移动并改名都成功时为 True
        """
        return self._transfer(
            fileitem, path, new_name, operation="fs_move", action="移动"
        )

    def _transfer(
        self, fileitem: FileItem, path: Path, new_name: str, *,
        operation: str, action: str,
    ) -> bool:
        """
        把文件或目录转移到指定目录并改名

        改名失败即整体失败：调用方按返回值判断整理是否完成，转移成功而改名失败会
        在目标目录留下一个原名文件，此时报成功等于让调用方以为整理已经做完。

        :param fileitem: 待转移的文件项
        :param path: 目标目录路径
        :param new_name: 转移后的名称
        :param operation: 客户端上执行转移的方法名
        :param action: 面向日志的动作名称
        :return: 转移并改名都成功时为 True
        """
        try:
            check_response(
                getattr(self._client(), operation)(
                    self._resolve_id(fileitem),
                    parent_id=self.path_to_id(path.as_posix()),
                )
            )
        except Exception as error:
            logger.error(f"【123云盘】{action} {fileitem.path} 失败：{error}")
            return False
        self.forget_path(fileitem.path)
        moved = self.get_item(path / fileitem.name)
        if moved is None:
            logger.error(f"【123云盘】{action}后未找到 {path / fileitem.name}")
            return False
        if moved.name == new_name:
            return True
        return self.rename(moved, new_name)

    def usage(self) -> Optional[StorageUsage]:
        """
        取得空间使用情况

        :return: 空间使用情况；查询失败时为 None
        """
        try:
            response = self._client().user_info()
            check_response(response)
            data = response["data"]
            total = int(data["SpacePermanent"])
            return StorageUsage(total=total, available=total - int(data["SpaceUsed"]))
        except Exception as error:
            logger.error(f"【123云盘】查询空间使用情况失败：{error}")
            return None

    def _resolve_id(self, fileitem: FileItem) -> str:
        """
        取得文件项的文件 ID，文件项自带时直接用，否则按路径查

        :param fileitem: 文件项
        :return: 文件 ID
        :raises FileNotFoundError: 路径不存在
        """
        return fileitem.fileid or self.path_to_id(fileitem.path)


def _cache_key(path: str) -> str:
    """
    把路径归一为缓存键，目录的尾部斜杠不参与

    :param path: 存储内的绝对路径
    :return: 缓存键
    """
    return PurePosixPath(path or "/").as_posix()
