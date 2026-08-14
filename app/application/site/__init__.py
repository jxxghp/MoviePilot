"""站点目录、认证与索引资源的应用能力包。"""

from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path


def _include_legacy_resource_directory(
        package_paths: list[str], package_dir: Path
) -> None:
    """canonical 扩展缺失时允许读取旧 Docker 更新器写入的资源目录。"""
    extension_names = tuple(f"sites{suffix}" for suffix in EXTENSION_SUFFIXES)
    if any((package_dir / name).is_file() for name in extension_names):
        return

    legacy_dir = package_dir.parent.parent / "helper"
    if (
            legacy_dir.is_dir()
            and any((legacy_dir / name).is_file() for name in extension_names)
            and str(legacy_dir) not in package_paths
    ):
        # 旧镜像内固化的 mp_update.sh 无法随源码热更新，只在过渡场景扩展包搜索路径。
        package_paths.append(str(legacy_dir))


_include_legacy_resource_directory(__path__, Path(__file__).resolve().parent)
