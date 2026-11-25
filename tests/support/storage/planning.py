"""
只读规划目录与可观察存储执行替身；不注册测试用例。
"""

from pathlib import Path
from app.schemas.workflow import FileItem


class ReadOnlyTreeStorage:
    """
    提供目录只读遍历，并让任何写接口调用立即失败。
    """

    def __init__(self, children: dict[str, list[FileItem]]):
        """
        保存按源目录路径索引的测试树。
        """
        self.children = children
        self.reads: list[str] = []

    def list(self, fileitem: FileItem) -> list[FileItem]:
        """
        返回目录孩子并记录只读访问。
        """
        self.reads.append(fileitem.path)
        return self.children.get(fileitem.path, [])

    def __getattr__(self, name: str):
        """
        拒绝规划期意外访问的存储接口。
        """
        raise AssertionError(f"规划阶段不应访问存储接口：{name}")


class RecordingStorage:
    """
    记录执行期存储调用顺序并模拟同一网盘复制。
    """

    def __init__(self, calls: list[str], existing: FileItem = None):
        """
        保存调用日志与可选同名目标。
        """
        self.calls = calls
        self.files = {existing.path: existing} if existing else {}

    def get_item_strict(self, path: Path):
        """
        记录严格目标查询。
        """
        self.calls.append("strict")
        return self.files.get(path.as_posix())

    def get_item(self, path: Path):
        """
        记录普通目标查询。
        """
        self.calls.append("get_item")
        return self.files.get(path.as_posix())

    def get_folder(self, path: Path) -> FileItem:
        """
        记录可能创建目录的接口。
        """
        self.calls.append("get_folder")
        return FileItem(
            storage="alist",
            path=path.as_posix(),
            name=path.name,
            type="dir",
        )

    def delete(self, fileitem: FileItem) -> bool:
        """
        记录删除副作用。
        """
        self.calls.append("delete")
        self.files.pop(fileitem.path, None)
        return True

    def rename(self, fileitem: FileItem, name: str) -> bool:
        """
        模拟拒绝覆盖已有目标的同目录重命名。
        """
        target = Path(fileitem.path).with_name(name)
        self.calls.append(f"rename:{target.as_posix()}")
        assert target.as_posix() not in self.files
        source = self.files.pop(fileitem.path)
        self.files[target.as_posix()] = source.model_copy(update={"path": target.as_posix(), "name": name})
        return True

    def is_support_transtype(self, transfer_type: str) -> bool:
        """
        声明测试存储支持复制。
        """
        return transfer_type == "copy"

    def copy(self, fileitem: FileItem, path: Path, name: str) -> bool:
        """
        记录复制副作用。
        """
        self.calls.append(f"copy:{(path / name).as_posix()}")
        self.files[(path / name).as_posix()] = fileitem.model_copy(update={
            "path": (path / name).as_posix(), "name": name,
        })
        return True
