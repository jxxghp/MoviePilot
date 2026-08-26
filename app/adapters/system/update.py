"""MoviePilot Release 后台检查、下载与待安装状态管理。"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from app.adapters.network.http import RequestUtils
from app.foundation.singleton import SingletonClass
from app.foundation.version import compare_version
from app.runtime.version import get_app_version
from app.foundation.environment import is_docker
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.thread import ThreadHelper
from app.schemas.system import SystemUpdateStatus


class SystemUpdateManager(metaclass=SingletonClass):
    """持久化更新状态，并保证同一时刻只有一个下载任务。"""

    _BACKEND_RELEASES_API = "https://api.github.com/repos/jxxghp/MoviePilot/releases"
    _FRONTEND_RELEASE_API = (
        "https://api.github.com/repos/jxxghp/MoviePilot-Frontend/releases/tags/{tag}"
    )
    _BACKEND_ARCHIVE_URL = (
        "https://github.com/jxxghp/MoviePilot/archive/refs/tags/{tag}.zip"
    )
    _VERSION_PATTERN = re.compile(r"^v3\.\d+\.\d+(?:[-.](?:alpha|beta|rc)\d*)?$", re.I)
    _STABLE_VERSION_PATTERN = re.compile(r"^v3\.\d+\.\d+$", re.I)

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._download_active = False

    @property
    def _root(self) -> Path:
        return Path(get_runtime_setting('TEMP_PATH')) / "moviepilot-update"

    @property
    def _state_file(self) -> Path:
        return self._root / "state.json"

    @property
    def _install_file(self) -> Path:
        return self._root / "install.json"

    @property
    def _backend_archive(self) -> Path:
        return self._root / "backend.zip"

    @property
    def _frontend_archive(self) -> Path:
        return self._root / "frontend.zip"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _default_state(self) -> dict[str, Any]:
        return SystemUpdateStatus(current_version=get_app_version()).model_dump()

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return {
                    **self._default_state(),
                    **payload,
                    "current_version": get_app_version(),
                }
        except (OSError, json.JSONDecodeError):
            pass
        return self._default_state()

    def _write_state(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            state = self._read_state()
            state.update(changes)
            state["current_version"] = get_app_version()
            state["progress"] = self._progress(
                state.get("downloaded_bytes", 0), state.get("total_bytes", 0)
            )
            validated = SystemUpdateStatus.model_validate(state).model_dump()
            self._root.mkdir(parents=True, exist_ok=True)
            temporary = self._state_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self._state_file)
            return validated

    @staticmethod
    def _progress(downloaded: Any, total: Any) -> int:
        try:
            downloaded_value = max(0, int(downloaded))
            total_value = max(0, int(total))
        except (TypeError, ValueError):
            return 0
        if total_value <= 0:
            return 0
        return min(100, int(downloaded_value * 100 / total_value))

    def get_status(self) -> SystemUpdateStatus:
        """返回状态快照，并在更新完成后的新进程中清理安装终态。"""
        with self._lock:
            state = self._read_state()
            target = str(state.get("version") or "")
            if state.get("state") == "downloading" and not self._download_active:
                state = self._write_state(
                    state="failed",
                    error="更新包下载因服务重启而中断，请重试",
                    can_update=True,
                    can_install=False,
                )
            if state.get("state") == "installing" and target == get_app_version():
                self._install_file.unlink(missing_ok=True)
                state = self._write_state(
                    state="idle",
                    version=None,
                    frontend_version=None,
                    release_name=None,
                    release_notes=None,
                    published_at=None,
                    downloaded_bytes=0,
                    total_bytes=0,
                    error=None,
                    can_update=False,
                    can_install=False,
                )
            return SystemUpdateStatus.model_validate(state)

    def check(self) -> SystemUpdateStatus:
        """查询 GitHub 稳定版 v3 Release，并保留正在下载或待安装状态。"""
        current = self.get_status()
        if current.state in {"downloading", "ready", "installing"}:
            return current

        try:
            response = self._request().get_res(self._BACKEND_RELEASES_API)
            if response is None or response.status_code != 200:
                raise RuntimeError("GitHub Release 请求失败")
            releases = response.json()
            release = next(
                (
                    item
                    for item in releases
                    if isinstance(item, dict)
                    and not item.get("draft")
                    and not item.get("prerelease")
                    and self._STABLE_VERSION_PATTERN.fullmatch(
                        str(item.get("tag_name") or "")
                    )
                ),
                None,
            )
            if not release:
                raise RuntimeError("未找到可用的 v3 稳定版本")
            version = str(release["tag_name"])
            has_update = compare_version(version, "gt", get_app_version()) is True
            return SystemUpdateStatus.model_validate(
                self._write_state(
                    state="available" if has_update else "idle",
                    version=version if has_update else None,
                    frontend_version=None,
                    release_name=str(release.get("name") or version) if has_update else None,
                    release_notes=str(release.get("body") or "") if has_update else None,
                    published_at=release.get("published_at") if has_update else None,
                    checked_at=self._now(),
                    downloaded_bytes=0,
                    total_bytes=0,
                    error=None,
                    can_update=has_update,
                    can_install=False,
                )
            )
        except Exception as error:  # 定时检查失败不应打扰用户，下载失败才进入可见错误态
            logger.warning(f"检查 MoviePilot 更新失败: {error}")
            return SystemUpdateStatus.model_validate(
                self._write_state(
                    state="idle",
                    checked_at=self._now(),
                    error=str(error),
                    can_update=False,
                    can_install=False,
                )
            )

    def start_download(self) -> SystemUpdateStatus:
        """启动唯一后台下载线程，并立即返回下载中状态。"""
        with self._lock:
            state = self.get_status()
            if state.state == "ready":
                return state
            if state.state == "downloading" and self._download_active:
                return state
            if state.state != "available" or not state.version:
                state = self.check()
            if state.state != "available" or not state.version:
                return state

            self._backend_archive.unlink(missing_ok=True)
            self._frontend_archive.unlink(missing_ok=True)
            self._write_state(
                state="downloading",
                downloaded_bytes=0,
                total_bytes=0,
                error=None,
                can_update=False,
                can_install=False,
            )
            self._download_active = True
            try:
                ThreadHelper().submit(self._download_update, state.version)
            except RuntimeError as error:
                self._download_active = False
                return SystemUpdateStatus.model_validate(
                    self._write_state(
                        state="failed",
                        error=f"无法启动更新包下载：{error}",
                        can_update=True,
                        can_install=False,
                    )
                )
            return self.get_status()

    def request_install(self) -> tuple[bool, str]:
        """校验待安装文件并写入启动阶段消费的安装意图。"""
        with self._lock:
            state = self.get_status()
            if state.state != "ready" or not state.version:
                return False, "更新包尚未下载完成"
            try:
                backend_sha256 = self._sha256(self._backend_archive)
                frontend_sha256 = self._sha256(self._frontend_archive)
                prepared = self._read_prepared_manifest()
                if backend_sha256 != prepared.get("backend_sha256"):
                    raise RuntimeError("后端更新包校验失败")
                if frontend_sha256 != prepared.get("frontend_sha256"):
                    raise RuntimeError("前端更新包校验失败")
                self._install_file.write_text(
                    json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                self._write_state(state="installing", can_install=False, error=None)
                return True, "更新包已就绪，正在重启安装"
            except (OSError, RuntimeError, json.JSONDecodeError) as error:
                self._write_state(state="failed", error=str(error), can_install=False)
                return False, str(error)

    def cancel_install(self, reason: str) -> None:
        """重启请求失败时撤销安装意图，避免下次普通启动意外安装。"""
        with self._lock:
            self._install_file.unlink(missing_ok=True)
            self._write_state(
                state="ready",
                error=reason,
                can_update=False,
                can_install=True,
            )

    def _request(self) -> RequestUtils:
        return RequestUtils(
            proxies=get_runtime_setting('PROXY'),
            headers=get_runtime_setting('GITHUB_HEADERS'),
            timeout=60,
        )

    def _download_update(self, version: str) -> None:
        try:
            downloaded = 0
            downloaded, backend_total = self._download_file(
                self._proxied(self._BACKEND_ARCHIVE_URL.format(tag=version)),
                self._backend_archive,
                downloaded,
                0,
            )
            frontend_version = self._validate_backend_archive(version)
            frontend_release = self._fetch_frontend_release(frontend_version)
            frontend_asset = next(
                (
                    item
                    for item in frontend_release.get("assets") or []
                    if item.get("name") == "dist.zip" and item.get("browser_download_url")
                ),
                None,
            )
            if not frontend_asset:
                raise RuntimeError(f"前端 {frontend_version} 缺少 dist.zip 发布资产")
            frontend_total = int(frontend_asset.get("size") or 0)
            total = backend_total + frontend_total
            self._write_state(
                frontend_version=frontend_version,
                downloaded_bytes=downloaded,
                total_bytes=total,
            )
            downloaded, _ = self._download_file(
                self._proxied(str(frontend_asset["browser_download_url"])),
                self._frontend_archive,
                downloaded,
                total,
            )
            self._validate_frontend_archive(frontend_version)
            expected_digest = str(frontend_asset.get("digest") or "")
            frontend_sha256 = self._sha256(self._frontend_archive)
            if expected_digest.startswith("sha256:") and frontend_sha256 != expected_digest.removeprefix("sha256:"):
                raise RuntimeError("前端更新包与 GitHub Release 摘要不一致")

            if not is_docker():
                self._prepare_local_backend_ref(version)

            prepared = {
                "version": version,
                "frontend_version": frontend_version,
                "backend_archive": str(self._backend_archive),
                "frontend_archive": str(self._frontend_archive),
                "backend_sha256": self._sha256(self._backend_archive),
                "frontend_sha256": frontend_sha256,
                "prepared_at": self._now(),
            }
            (self._root / "prepared.json").write_text(
                json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._write_state(
                state="ready",
                downloaded_bytes=downloaded,
                total_bytes=max(total, downloaded),
                error=None,
                can_update=False,
                can_install=True,
            )
            logger.info(f"MoviePilot {version} 更新包已下载完成，等待用户确认重启")
        except Exception as error:  # 后台线程必须把所有失败沉淀为可查询状态
            logger.error(f"下载 MoviePilot 更新包失败: {error}")
            self._write_state(
                state="failed", error=str(error), can_update=True, can_install=False
            )
        finally:
            with self._lock:
                self._download_active = False

    def _download_file(
        self, url: str, destination: Path, downloaded_before: int, total_hint: int
    ) -> tuple[int, int]:
        temporary = destination.with_suffix(".part")
        temporary.unlink(missing_ok=True)
        with self._request().get_stream(url) as response:
            if response is None or response.status_code != 200:
                raise RuntimeError(f"下载更新包失败：HTTP {getattr(response, 'status_code', '无响应')}")
            content_length = int(response.headers.get("content-length") or 0)
            total = total_hint or content_length
            current = downloaded_before
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    current += len(chunk)
                    self._write_state(downloaded_bytes=current, total_bytes=total)
        temporary.replace(destination)
        return current, content_length

    def _fetch_frontend_release(self, version: str) -> dict[str, Any]:
        response = self._request().get_res(
            self._FRONTEND_RELEASE_API.format(tag=version)
        )
        if response is None or response.status_code != 200:
            raise RuntimeError(f"无法获取前端 {version} Release")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("前端 Release 返回格式异常")
        return payload

    def _validate_backend_archive(self, version: str) -> str:
        with zipfile.ZipFile(self._backend_archive) as archive:
            self._validate_zip_members(archive)
            version_name = next(
                (name for name in archive.namelist() if name.count("/") == 1 and name.endswith("/version.py")),
                None,
            )
            if not version_name:
                raise RuntimeError("后端更新包缺少 version.py")
            version_source = archive.read(version_name).decode("utf-8")
            app_match = re.search(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)", version_source, re.M)
            frontend_match = re.search(r"^FRONTEND_VERSION\s*=\s*['\"]([^'\"]+)", version_source, re.M)
            if not app_match or app_match.group(1) != version:
                raise RuntimeError("后端更新包版本与目标 Release 不一致")
            if not frontend_match or not self._VERSION_PATTERN.fullmatch(frontend_match.group(1)):
                raise RuntimeError("后端更新包声明的前端版本无效")
            required = ("pyproject.toml", "uv.lock")
            names = archive.namelist()
            if any(not any(name.endswith(f"/{item}") for name in names) for item in required):
                raise RuntimeError("后端更新包缺少依赖锁定文件")
            return frontend_match.group(1)

    def _validate_frontend_archive(self, version: str) -> None:
        with zipfile.ZipFile(self._frontend_archive) as archive:
            self._validate_zip_members(archive)
            names = set(archive.namelist())
            if "dist/index.html" not in names or "dist/version.txt" not in names:
                raise RuntimeError("前端更新包结构无效")
            archived_version = archive.read("dist/version.txt").decode("utf-8").strip()
            if archived_version != version:
                raise RuntimeError("前端更新包版本与后端声明不一致")

    @staticmethod
    def _validate_zip_members(archive: zipfile.ZipFile) -> None:
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            file_type = (item.external_attr >> 16) & 0o170000
            if (
                path.is_absolute()
                or ".." in path.parts
                or file_type == stat.S_IFLNK
            ):
                raise RuntimeError("更新包包含不安全路径")

    def _read_prepared_manifest(self) -> dict[str, Any]:
        payload = json.loads((self._root / "prepared.json").read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("更新包清单格式无效")
        return payload

    @staticmethod
    def _prepare_local_backend_ref(version: str) -> None:
        """本地 CLI 在下载阶段获取标签，使重启后的代码切换不再联网。"""
        root = Path(__file__).resolve().parents[3]
        if not (root / ".git").is_dir():
            raise RuntimeError("本地安装目录不是 Git 仓库，无法准备 Release 更新")
        try:
            worktree = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if worktree.stdout.strip():
                raise RuntimeError("本地源码存在未提交改动，无法准备 Release 更新")
            subprocess.run(
                ["git", "fetch", "--no-tags", "origin", "tag", version],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            subprocess.run(
                ["git", "rev-parse", "--verify", f"{version}^{{commit}}"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"无法准备本地 Release 标签 {version}") from error

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file_handle:
            for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _proxied(url: str) -> str:
        proxy = str(get_runtime_setting('GITHUB_PROXY') or "").strip()
        return f"{proxy}{url}" if proxy else url


system_update_manager = SystemUpdateManager()
