"""数据库备份单文件的受限文件系统操作。"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from app.runtime.version import get_app_version

_BACKUP_NAME = re.compile(
    r"^(?:moviepilot_(?P<version>v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)_)?"
    r"(?P<db_type>sqlite|postgresql)_"
    r"(?P<timestamp>\d{8}_\d{6})"
    r"(?:_(?P<sequence>\d+))?"
    r"(?P<suffix>\.db|\.dump)$"
)
_RELEASE_VERSION = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class BackupFiles:
    """把备份文件操作限制在一个私有根目录内。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def create_temporary(self, suffix: str) -> Path:
        """在最终目录内创建私有临时文件，保证发布可使用原子替换。"""
        self._ensure_root()
        descriptor, filename = tempfile.mkstemp(
            prefix=".database-",
            suffix=f"{suffix}.partial",
            dir=self.root,
        )
        os.close(descriptor)
        path = Path(filename)
        path.chmod(0o600)
        return path

    def publish(self, temporary: Path, name: str) -> Path:
        """把已校验临时文件发布为正式备份文件。"""
        destination = self._resolve_name(name, require_exists=False)
        os.replace(temporary, destination)
        destination.chmod(0o600)
        return destination

    def discard(self, temporary: Path) -> None:
        """清理本次操作拥有的未发布临时文件。"""
        path = Path(temporary)
        if path.parent == self.root and path.name.startswith(".database-"):
            path.unlink(missing_ok=True)

    def list(self) -> list[Path]:
        """返回当前根目录内格式合法的正式备份文件。"""
        if not self.root.is_dir():
            return []
        paths = [
            path
            for path in self.root.iterdir()
            if path.is_file() and _BACKUP_NAME.fullmatch(path.name)
        ]
        return sorted(paths, key=lambda path: (self.created_at(path.name), path.name), reverse=True)

    def resolve(self, name: str) -> Path:
        """按受限文件名解析一个必须存在的备份文件。"""
        return self._resolve_name(name, require_exists=True)

    def delete(self, name: str) -> None:
        """删除一个已通过名称约束的备份文件。"""
        self.resolve(name).unlink()

    def size(self, name: str) -> int:
        """返回一个已通过名称约束的备份文件字节数。"""
        return self.resolve(name).stat().st_size

    def available_name(
        self,
        *,
        db_type: str,
        created_at: datetime,
        suffix: str,
    ) -> str:
        """生成包含数据库类型和秒级时间的简短可读文件名。"""
        timestamp = created_at.strftime("%Y%m%d_%H%M%S")
        version = get_app_version().strip()
        if _RELEASE_VERSION.fullmatch(version) is None:
            raise ValueError("程序版本号无法用于数据库备份命名")
        base = f"moviepilot_{version}_{db_type}_{timestamp}"
        candidate = f"{base}{suffix}"
        sequence = 1
        while (self.root / candidate).exists():
            candidate = f"{base}_{sequence}{suffix}"
            sequence += 1
        if not _BACKUP_NAME.fullmatch(candidate):
            raise ValueError("数据库备份文件名无效")
        return candidate

    @staticmethod
    def database_type(name: str) -> str:
        """从受管文件名读取数据库类型。"""
        return BackupFiles._match(name).group("db_type")

    @staticmethod
    def created_at(name: str) -> datetime:
        """从受管文件名读取本地创建时间。"""
        return datetime.strptime(
            BackupFiles._match(name).group("timestamp"),
            "%Y%m%d_%H%M%S",
        )

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def _resolve_name(self, name: str, *, require_exists: bool) -> Path:
        normalized = str(name).strip()
        self._match(normalized)
        if Path(normalized).name != normalized:
            raise ValueError("数据库备份文件名不能包含路径")
        path = self.root / normalized
        if require_exists and not path.is_file():
            raise FileNotFoundError(normalized)
        return path

    @staticmethod
    def _match(name: str) -> re.Match[str]:
        matched = _BACKUP_NAME.fullmatch(str(name))
        if matched is None:
            raise ValueError("数据库备份文件名无效")
        return matched
