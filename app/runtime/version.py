"""当前 MoviePilot 部署的产品版本读取入口。"""

from pathlib import Path

from app.foundation.environment import is_frozen, is_windows
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from version import APP_VERSION as _APP_VERSION
from version import FRONTEND_VERSION as _FRONTEND_VERSION


def get_app_version() -> str:
    """返回当前后端构建的发布版本。"""
    return _APP_VERSION


def _read_version_file(path: Path) -> str | None:
    """读取版本文件，文件缺失、内容为空或读取失败时返回空值。"""
    try:
        version = path.read_text(encoding="utf-8", errors="replace").strip()
        return version or None
    except OSError as error:
        if path.exists():
            logger.debug(f"加载版本文件 {path} 出错：{error}")
        return None


def get_frontend_version(*, fallback_to_declared: bool = True) -> str | None:
    """返回当前部署的前端资源版本，并可关闭发布声明回退。"""
    if is_frozen() and is_windows():
        version_file = (
            Path(get_runtime_setting("CONFIG_PATH")).parent
            / "nginx"
            / "html"
            / "version.txt"
        )
    else:
        version_file = Path(get_runtime_setting("FRONTEND_PATH")) / "version.txt"
    installed_version = _read_version_file(version_file)
    if installed_version or not fallback_to_declared:
        return installed_version
    return _FRONTEND_VERSION
