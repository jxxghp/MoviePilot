import re
from typing import Optional

from pathlib import Path
from pydantic import BaseModel, Field, field_validator
from app.schemas.types import StorageSchema

# Windows 盘符绝对路径，如 Z:/Downloads 或 Z:\Downloads
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")

# 存储标识：字母开头且长度不小于 2，与单字母的 Windows 盘符区分开
_STORAGE_SCHEME_EXPR = r"[A-Za-z][A-Za-z0-9_.+-]+"
_STORAGE_SCHEME_PATTERN = re.compile(rf"^{_STORAGE_SCHEME_EXPR}$")

# 存储实例名与存储标识之间的分隔符，如 u115@work
STORAGE_INSTANCE_SEPARATOR = "@"

# 存储实例名的最大长度
STORAGE_INSTANCE_MAX_LENGTH = 64

# 存储实例名：非空、不含空白与控制字符，且不含 : / \ @ 四个会造成歧义解析的分隔符
_STORAGE_INSTANCE_EXPR = rf"[^\x00-\x20\x7f:/\\@]{{1,{STORAGE_INSTANCE_MAX_LENGTH}}}"
_STORAGE_INSTANCE_PATTERN = re.compile(rf"^{_STORAGE_INSTANCE_EXPR}$")

# 存储令牌：存储标识，可选带一个实例名
_STORAGE_TOKEN_PATTERN = re.compile(
    rf"^({_STORAGE_SCHEME_EXPR})(?:{STORAGE_INSTANCE_SEPARATOR}({_STORAGE_INSTANCE_EXPR}))?$"
)


class FileURI(BaseModel):
    """带存储类型的文件 URI。

    ``storage`` 存放存储令牌，形如 ``u115`` 或 ``u115@work``：前者指该存储类型的
    默认实例，后者指该类型下名为 ``work`` 的具名实例。实例名不得含 ``:``、``/``、
    ``\\``、``@``、空白与控制字符，长度不超过 64。
    """

    # 文件路径
    path: Optional[str] = "/"
    # 存储令牌，形如 u115 或 u115@work
    storage: Optional[str] = Field(default=StorageSchema.Local.value)

    @field_validator("storage")
    @classmethod
    def _check_storage_token(cls, value: Optional[str]) -> Optional[str]:
        """
        校验存储令牌中的实例名写法，不合法即拒绝而不退回默认实例

        :param value: 存储令牌
        :return: 原样返回的存储令牌
        :raises ValueError: 令牌带实例分隔符但实例名或存储标识不合法
        """
        if value and STORAGE_INSTANCE_SEPARATOR in value:
            cls.split_storage(value)
        return value

    @property
    def uri(self) -> str:
        """
        文件 URI，本地存储直接返回路径，其他存储带上存储前缀
        """
        return self.path if self.storage == StorageSchema.Local.value else f"{self.storage}:{self.path}"

    @property
    def storage_id(self) -> str:
        """
        存储令牌中的存储标识部分

        :return: 存储标识
        """
        return self.split_storage(self.storage)[0]

    @property
    def storage_instance(self) -> Optional[str]:
        """
        存储令牌中的实例名部分

        :return: 实例名；令牌未带实例名时为 None
        """
        return self.split_storage(self.storage)[1]

    @classmethod
    def is_storage_scheme(cls, value: str) -> bool:
        """
        判断字符串能否作为文件 URI 的存储标识

        :param value: 待判断的存储标识
        :return: 可作为存储标识时为 True
        """
        return bool(value) and bool(_STORAGE_SCHEME_PATTERN.match(value))

    @classmethod
    def is_storage_instance(cls, value: str) -> bool:
        """
        判断字符串能否作为存储实例名

        :param value: 待判断的实例名
        :return: 可作为实例名时为 True
        """
        return bool(value) and bool(_STORAGE_INSTANCE_PATTERN.match(value))

    @classmethod
    def storage_parts(cls, storage: Optional[str]) -> Optional[tuple[str, Optional[str]]]:
        """
        拆分存储令牌用于比较，无效令牌不参与任何相等判断

        :param storage: 存储令牌，如 u115 或 u115@work
        :return: (存储标识, 实例名) 二元组；令牌为空或写法非法时为 None
        """
        if not storage:
            return None
        try:
            return cls.split_storage(storage)
        except ValueError:
            return None

    @classmethod
    def storage_type(cls, storage: Optional[str]) -> str:
        """
        取存储令牌的类型部分，实例名不参与

        令牌为空或写法非法时给出空串而非退回令牌原文，非法令牌因此不会与任何存储类型相等。

        :param storage: 存储令牌，如 u115 或 u115@work
        :return: 存储标识；令牌为空或写法非法时为空串
        """
        parts = cls.storage_parts(storage)
        return parts[0] if parts else ""

    @classmethod
    def is_local(cls, storage: Optional[str]) -> bool:
        """
        判断存储令牌是否指向本地存储类型，具名实例同样成立

        :param storage: 存储令牌，如 local 或 local@nas
        :return: 类型部分为本地存储时为 True
        """
        return cls.storage_type(storage) == StorageSchema.Local.value

    @classmethod
    def is_same_storage_type(cls, left: Optional[str], right: Optional[str]) -> bool:
        """
        判断两个存储令牌是否属于同一存储类型，实例名不参与

        :param left: 存储令牌
        :param right: 存储令牌
        :return: 两侧类型部分相同且均有效时为 True
        """
        left_type = cls.storage_type(left)
        return bool(left_type) and left_type == cls.storage_type(right)

    @classmethod
    def is_same_storage(cls, left: Optional[str], right: Optional[str]) -> bool:
        """
        判断两个存储令牌是否指向同一存储实例

        裸令牌指该类型的默认实例，与同类型的具名令牌算作不同实例：u115 与 u115@work
        之间的转移是跨实例转移，而非同一存储内的移动。

        :param left: 存储令牌
        :param right: 存储令牌
        :return: 存储标识与实例名均相同且两侧令牌都有效时为 True
        """
        left_parts = cls.storage_parts(left)
        return left_parts is not None and left_parts == cls.storage_parts(right)

    @classmethod
    def join_storage(cls, storage_id: str, instance: Optional[str] = None) -> str:
        """
        把存储标识与实例名拼成存储令牌

        :param storage_id: 存储标识
        :param instance: 实例名，为空表示该类型的默认实例
        :return: 存储令牌
        """
        if not instance:
            return storage_id or ""
        return f"{storage_id}{STORAGE_INSTANCE_SEPARATOR}{instance}"

    @classmethod
    def split_storage(cls, storage: Optional[str]) -> tuple[str, Optional[str]]:
        """
        拆分存储令牌为存储标识与实例名

        不带实例分隔符的令牌原样作为存储标识返回，实例名为 None，表示该存储类型的
        默认实例；此时不校验标识本身的写法，按标识直取的既有调用不受影响。

        :param storage: 存储令牌，如 u115 或 u115@work
        :return: 存储标识与实例名；令牌未带实例名时实例名为 None
        :raises ValueError: 令牌带实例分隔符但整体不是合法令牌
        """
        value = storage or ""
        if not value:
            return "", None
        matched = _STORAGE_TOKEN_PATTERN.match(value)
        if matched:
            return matched.group(1), matched.group(2)
        if STORAGE_INSTANCE_SEPARATOR in value:
            raise ValueError(
                f"存储令牌 {value} 不合法：存储标识需为字母开头、长度不小于 2，"
                f"实例名需为 1-{STORAGE_INSTANCE_MAX_LENGTH} 个字符且不含空白或 : / \\ @"
            )
        return value, None

    @classmethod
    def split_uri(cls, uri: str) -> tuple[Optional[str], str]:
        """
        拆分文件 URI 的存储令牌与路径，路径原样保留

        存储令牌只在首个冒号之前的一段中识别，该段含路径分隔符时一律视为路径本身；
        该段带实例分隔符却不是合法令牌时报错，不退回按无前缀路径解析。

        :param uri: 文件 URI，如 /media/movie、u115:/media/movie、u115@work:/media/movie
            或 Windows 盘符路径 Z:/media
        :return: 存储令牌与去掉前缀的路径；无存储前缀时存储令牌为 None
        :raises ValueError: 存储令牌位置带实例分隔符但不是合法令牌
        """
        value = uri or ""
        head, separator, rest = value.partition(":")
        if not separator:
            return None, value
        if "/" in head or "\\" in head:
            return None, value
        if STORAGE_INSTANCE_SEPARATOR in head:
            cls.split_storage(head)
            return head, rest
        if not _STORAGE_SCHEME_PATTERN.match(head):
            return None, value
        return head, rest

    @classmethod
    def from_uri(cls, uri: str) -> "FileURI":
        """
        解析文件 URI 为存储令牌和路径

        :param uri: 文件 URI，如 /media/movie、u115:/media/movie、u115@work:/media/movie
            或 Windows 盘符路径 Z:/media
        :return: FileURI 对象
        :raises ValueError: 存储令牌位置带实例分隔符但不是合法令牌
        """
        storage, path = cls.split_uri(uri)
        storage = storage or StorageSchema.Local.value
        # Windows 盘符路径本身就是绝对路径，补上根斜杠会得到 /Z:/xxx 这样的非法路径
        if not path.startswith("/") and not WINDOWS_DRIVE_PATTERN.match(path):
            path = "/" + path
        path = Path(path).as_posix()
        return cls(storage=storage, path=path)


class FileItem(FileURI):
    """文件或目录条目，目录可递归包含子条目。"""

    # 类型 dir/file
    type: Optional[str] = None
    # 文件名
    name: Optional[str] = None
    # 文件名
    basename: Optional[str] = None
    # 文件后缀
    extension: Optional[str] = None
    # 文件大小
    size: Optional[int] = None
    # 修改时间
    modify_time: Optional[float] = None
    # 子节点
    children: Optional[list["FileItem"]] = Field(default_factory=list)
    # ID
    fileid: Optional[str] = None
    # 父ID
    parent_fileid: Optional[str] = None
    # 缩略图
    thumbnail: Optional[str] = None
    # 115 pickcode
    pickcode: Optional[str] = None
    # drive_id
    drive_id: Optional[str] = None
    # url
    url: Optional[str] = None


class StorageUsage(BaseModel):
    """存储空间使用情况。"""

    # 总空间
    total: float = 0.0
    # 剩余空间
    available: float = 0.0


class StorageTransType(BaseModel):
    """存储支持的传输类型及其显示名称。"""

    # 传输类型
    transtype: Optional[dict[str, str]] = Field(default_factory=dict)
