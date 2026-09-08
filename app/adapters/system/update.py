"""MoviePilot 主程序与站点资源的后台检查、下载和待安装状态管理。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, cast

from app.adapters.network.http import RequestUtils
from app.adapters.system.resource import ResourceHelper, get_resource_versions
from app.foundation.environment import is_docker
from app.foundation.singleton import SingletonClass
from app.foundation.version import compare_version
from app.runtime.dependencies.profile import runtime_sync_arguments
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.thread import ThreadHelper
from app.runtime.version import get_app_version
from app.schemas.system import SystemUpdateStatus, SystemUpdateType

_APPLICATION: SystemUpdateType = "application"
_RESOURCES: SystemUpdateType = "resources"
_TARGETS: tuple[SystemUpdateType, ...] = (_APPLICATION, _RESOURCES)
_ITEM_FIELDS = {
    "state",
    "current_version",
    "version",
    "frontend_version",
    "current_auth_version",
    "auth_version",
    "current_indexer_version",
    "indexer_version",
    "release_name",
    "release_notes",
    "published_at",
    "checked_at",
    "downloaded_bytes",
    "total_bytes",
    "progress",
    "error",
    "can_update",
    "can_install",
}


class SystemUpdateManager(metaclass=SingletonClass):
    """持久化两类升级状态，并保证同一时刻只有一个后台下载任务。"""

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
        self._active_target: SystemUpdateType | None = None

    @property
    def _root(self) -> Path:
        """返回跨重启保留更新制品的暂存根目录。"""
        return Path(get_runtime_setting("TEMP_PATH")) / "moviepilot-update"

    @property
    def _state_file(self) -> Path:
        """返回更新状态文件路径。"""
        return self._root / "state.json"

    @property
    def _install_file(self) -> Path:
        """返回 Docker root worker 或本地 CLI 消费的安装意图文件路径。"""
        return self._root / "install.json"

    @property
    def _backend_archive(self) -> Path:
        """返回后端 Release 压缩包路径。"""
        return self._root / "backend.zip"

    @property
    def _frontend_archive(self) -> Path:
        """返回后端版本声明对应的前端压缩包路径。"""
        return self._root / "frontend.zip"

    @property
    def _resource_dir(self) -> Path:
        """返回站点资源包暂存目录。"""
        return self._root / "resources"

    @property
    def _docker_app_dir(self) -> Path:
        """返回 Docker 当前后端源码目录。"""
        return Path(get_runtime_setting("ROOT_PATH"))

    @property
    def _docker_public_dir(self) -> Path:
        """返回 Docker 当前前端静态文件目录。"""
        return Path(get_runtime_setting("FRONTEND_PATH"))

    @property
    def _docker_pending_file(self) -> Path:
        """返回 Docker 载荷切换事务标记路径。"""
        return Path(get_runtime_setting("TEMP_PATH")) / "__update_pending__"

    @property
    def _docker_previous_app_dir(self) -> Path:
        """返回 Docker 更新前后端源码备份目录。"""
        app_dir = self._docker_app_dir
        return app_dir.with_name(f"{app_dir.name}.__update_previous__")

    @property
    def _docker_previous_public_dir(self) -> Path:
        """返回 Docker 更新前前端静态文件备份目录。"""
        public_dir = self._docker_public_dir
        return public_dir.with_name(f"{public_dir.name}.__update_previous__")

    @staticmethod
    def _now() -> str:
        """返回 UTC ISO 时间戳。"""
        return datetime.now(timezone.utc).isoformat()

    def _default_item(self, target: SystemUpdateType) -> dict[str, Any]:
        """创建单类升级的初始状态。"""
        if target == _APPLICATION:
            return {
                "type": target,
                "state": "idle",
                "current_version": get_app_version(),
            }
        auth_version, indexer_version = get_resource_versions()
        return {
            "type": target,
            "state": "idle",
            "current_auth_version": auth_version,
            "current_indexer_version": indexer_version,
        }

    def _default_state(self) -> dict[str, Any]:
        """创建兼容旧客户端字段的聚合状态。"""
        application = self._default_item(_APPLICATION)
        resources = self._default_item(_RESOURCES)
        return self._sync_aggregate(
            {
                "updates": [application, resources],
                "current_version": get_app_version(),
            }
        )

    def _read_state(self) -> dict[str, Any]:
        """读取并规范化状态文件，兼容只有主程序字段的旧状态。"""
        payload: dict[str, Any] = {}
        try:
            loaded = json.loads(self._state_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            pass

        defaults = self._default_state()
        updates_payload = payload.get("updates")
        if not isinstance(updates_payload, list):
            application = self._default_item(_APPLICATION)
            application.update(
                {key: payload[key] for key in _ITEM_FIELDS if key in payload}
            )
            updates_payload = [application, self._default_item(_RESOURCES)]

        normalized_updates: list[dict[str, Any]] = []
        for target in _TARGETS:
            item = next(
                (
                    value
                    for value in updates_payload
                    if isinstance(value, dict) and value.get("type") == target
                ),
                None,
            )
            normalized = self._default_item(target)
            if item:
                normalized.update(item)
            normalized["type"] = target
            normalized_updates.append(normalized)

        state = {**defaults, **payload, "updates": normalized_updates}
        return self._sync_aggregate(state)

    def _write_state(self, **changes: Any) -> dict[str, Any]:
        """原子写入聚合状态，并保留旧版直接修改主程序字段的调用语义。"""
        with self._lock:
            state = self._read_state()
            application = self._get_item(state, _APPLICATION)
            for key, value in changes.items():
                if key in _ITEM_FIELDS:
                    application[key] = value
                else:
                    state[key] = value
            state = self._sync_aggregate(state)
            return self._persist_state(state)

    def _write_item(self, target: SystemUpdateType, **changes: Any) -> dict[str, Any]:
        """更新一个升级类型并同步聚合字段。"""
        with self._lock:
            state = self._read_state()
            self._get_item(state, target).update(changes)
            return self._persist_state(self._sync_aggregate(state))

    def _persist_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """校验并原子替换状态 JSON。"""
        validated = cast(dict[str, Any], SystemUpdateStatus.model_validate(state).model_dump())
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._state_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self._state_file)
        return validated

    @staticmethod
    def _get_item(state: dict[str, Any], target: SystemUpdateType) -> dict[str, Any]:
        """从聚合状态中取得指定升级类型。"""
        return next(item for item in state["updates"] if item["type"] == target)

    def _sync_aggregate(self, state: dict[str, Any]) -> dict[str, Any]:
        """从两类明细重新计算旧版顶层字段和汇总进度。"""
        application = self._get_item(state, _APPLICATION)
        state.update(
            {
                "state": application.get("state", "idle"),
                "current_version": get_app_version(),
                "version": application.get("version"),
                "frontend_version": application.get("frontend_version"),
                "release_name": application.get("release_name"),
                "release_notes": application.get("release_notes"),
                "published_at": application.get("published_at"),
                "checked_at": application.get("checked_at"),
                "downloaded_bytes": sum(
                    int(item.get("downloaded_bytes") or 0) for item in state["updates"]
                ),
                "total_bytes": sum(
                    int(item.get("total_bytes") or 0) for item in state["updates"]
                ),
                "error": next(
                    (item.get("error") for item in state["updates"] if item.get("error")),
                    None,
                ),
                "can_update": any(item.get("can_update") for item in state["updates"]),
                "can_install": any(item.get("can_install") for item in state["updates"]),
            }
        )
        priorities = ("failed", "installing", "downloading", "ready", "available", "idle")
        state["state"] = next(
            (
                priority
                for priority in priorities
                if any(item.get("state") == priority for item in state["updates"])
            ),
            "idle",
        )
        state["progress"] = self._progress(
            state["downloaded_bytes"], state["total_bytes"]
        )
        for item in state["updates"]:
            item["progress"] = self._progress(
                item.get("downloaded_bytes", 0), item.get("total_bytes", 0)
            )
        return state

    @staticmethod
    def _progress(downloaded: Any, total: Any) -> int:
        """将下载字节数转换为 0 到 100 的整数进度。"""
        try:
            downloaded_value = max(0, int(downloaded))
            total_value = max(0, int(total))
        except (TypeError, ValueError):
            return 0
        if total_value <= 0:
            return 0
        return min(100, int(downloaded_value * 100 / total_value))

    def get_status(self) -> SystemUpdateStatus:
        """收敛安装状态并返回快照；提醒开关实时读取，避免缓存绕过关闭设置。"""
        with self._lock:
            state = self._read_state()
            changed = False
            auth_version, indexer_version = get_resource_versions()
            resources = self._get_item(state, _RESOURCES)
            previous_auth_version = resources.get("current_auth_version")
            previous_indexer_version = resources.get("current_indexer_version")
            resources["current_auth_version"] = auth_version
            resources["current_indexer_version"] = indexer_version
            for item in state["updates"]:
                target = item["type"]
                if item.get("state") == "downloading" and not self._download_active:
                    item.update(
                        {
                            "state": "failed",
                            "error": "更新包下载因服务重启而中断，请重试",
                            "can_update": True,
                            "can_install": False,
                        }
                    )
                    changed = True
                elif item.get("state") == "installing" and self._is_install_applied(item, target):
                    self._reset_item_after_install(item)
                    changed = True
                elif (
                    target == _RESOURCES
                    and item.get("state") == "ready"
                    and self._is_install_applied(item, target)
                ):
                    # 容器替换或手工安装可能先让运行资源达到目标版本，需丢弃残留待安装包。
                    self._discard_prepared_target(target)
                    self._reset_item_after_install(item)
                    changed = True
            resources_changed = (
                previous_auth_version != auth_version
                or previous_indexer_version != indexer_version
            )
            if changed or resources_changed:
                state = self._persist_state(self._sync_aggregate(state))
            else:
                state = self._sync_aggregate(state)
            state["auto_update"] = get_runtime_setting("MOVIEPILOT_AUTO_UPDATE") is True
            state["auto_update_resource"] = get_runtime_setting("AUTO_UPDATE_RESOURCE") is True
            return cast(SystemUpdateStatus, SystemUpdateStatus.model_validate(state))

    def _is_install_applied(self, item: dict[str, Any], target: SystemUpdateType) -> bool:
        """判断启动器应用后的当前版本是否已经达到安装目标。"""
        if target == _APPLICATION:
            return bool(item.get("version")) and item["version"] == get_app_version()
        current_auth, current_indexer = get_resource_versions()
        checks = []
        if item.get("auth_version"):
            checks.append(compare_version(current_auth, ">=", item["auth_version"]) is True)
        if item.get("indexer_version"):
            checks.append(compare_version(current_indexer, ">=", item["indexer_version"]) is True)
        return bool(checks) and all(checks)

    @staticmethod
    def _reset_item_after_install(item: dict[str, Any]) -> None:
        """清理已在启动前应用完成的单类安装状态。"""
        current_values = {
            key: item.get(key)
            for key in (
                "type",
                "current_version",
                "current_auth_version",
                "current_indexer_version",
            )
        }
        item.clear()
        item.update(
            {
                **current_values,
                "state": "idle",
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "progress": 0,
                "error": None,
                "can_update": False,
                "can_install": False,
            }
        )

    def check_scheduled(self) -> SystemUpdateStatus:
        """按实时开关分别检查主程序和资源，避免热重载前的排队任务越过关闭设置。"""
        for target, setting in (
            (_APPLICATION, "MOVIEPILOT_AUTO_UPDATE"),
            (_RESOURCES, "AUTO_UPDATE_RESOURCE"),
        ):
            if get_runtime_setting(setting) is True:
                self.check(target)
        return self.get_status()

    def check(self, target: SystemUpdateType | None = None) -> SystemUpdateStatus:
        """检查主程序和站点资源更新，定时检查失败只记录在对应明细中。"""
        targets = (target,) if target else _TARGETS
        for update_target in targets:
            current = self.get_status()
            item = self._get_item(current.model_dump(), update_target)
            if item["state"] in {"downloading", "ready", "installing"}:
                continue
            try:
                if update_target == _APPLICATION:
                    self._check_application()
                else:
                    self._check_resources()
            except Exception as error:  # noqa: BLE001  定时检查不得打扰当前任务
                logger.warning(f"检查 {update_target} 更新失败：{error}")
                self._write_item(
                    update_target,
                    state="idle",
                    checked_at=self._now(),
                    error=str(error),
                    can_update=False,
                    can_install=False,
                )
        return self.get_status()

    def _check_application(self) -> None:
        """查询最新稳定主程序 Release，并记录对应前端版本待下载。"""
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
                and self._STABLE_VERSION_PATTERN.fullmatch(str(item.get("tag_name") or ""))
            ),
            None,
        )
        if not release:
            raise RuntimeError("未找到可用的 v3 稳定版本")
        version = str(release["tag_name"])
        current_version = get_app_version()
        has_update = compare_version(version, "gt", current_version) is True
        self._write_item(
            _APPLICATION,
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
        if has_update:
            logger.info(f"发现 MoviePilot 主程序更新：{current_version} -> {version}")
        else:
            logger.info(f"MoviePilot 主程序已是最新版本：{current_version}")

    def _check_resources(self) -> None:
        """查询当前平台认证资源和索引资源的最新版本。"""
        current_auth, current_indexer = get_resource_versions()
        info = ResourceHelper().get_update_info(
            auth_version=current_auth,
            indexer_version=current_indexer,
        )
        if info is None:
            self._write_item(
                _RESOURCES,
                state="idle",
                version=None,
                auth_version=None,
                indexer_version=None,
                current_auth_version=current_auth,
                current_indexer_version=current_indexer,
                checked_at=self._now(),
                downloaded_bytes=0,
                total_bytes=0,
                error=None,
                can_update=False,
                can_install=False,
            )
            return
        self._write_item(
            _RESOURCES,
            state="available",
            version=info.get("package_version") or None,
            auth_version=info.get("auth_version"),
            indexer_version=info.get("indexer_version"),
            current_auth_version=current_auth,
            current_indexer_version=current_indexer,
            checked_at=self._now(),
            downloaded_bytes=0,
            total_bytes=0,
            error=None,
            can_update=True,
            can_install=False,
        )

    def start_download(self, target: SystemUpdateType = _APPLICATION) -> SystemUpdateStatus:
        """启动指定升级类型的唯一后台下载线程。"""
        if target not in _TARGETS:
            raise ValueError(f"未知升级类型：{target}")
        with self._lock:
            state = self.get_status()
            item = self._get_item(state.model_dump(), target)
            if self._download_active:
                return state
            if item["state"] == "ready":
                return state
            if item["state"] == "downloading" and self._download_active:
                return state
            if item["state"] != "available":
                state = self.check(target)
                item = self._get_item(state.model_dump(), target)
            if item["state"] != "available":
                return state

            self._discard_prepared_target(target)
            if target == _APPLICATION:
                self._backend_archive.unlink(missing_ok=True)
                self._frontend_archive.unlink(missing_ok=True)
            else:
                if self._resource_dir.exists():
                    for path in self._resource_dir.iterdir():
                        if path.is_file():
                            path.unlink()
            self._write_item(
                target,
                state="downloading",
                downloaded_bytes=0,
                total_bytes=0,
                error=None,
                can_update=False,
                can_install=False,
            )
            self._download_active = True
            self._active_target = target
            try:
                ThreadHelper().submit(self._download_update, target)
            except RuntimeError as error:
                self._download_active = False
                self._active_target = None
                self._write_item(
                    target,
                    state="failed",
                    error=f"无法启动更新包下载：{error}",
                    can_update=True,
                    can_install=False,
                )
            return self.get_status()

    def request_install(self, target: SystemUpdateType = _APPLICATION) -> tuple[bool, str]:
        """校验指定待安装制品，并写入 Docker worker 消费的安装意图。"""
        if target not in _TARGETS:
            return False, f"未知升级类型：{target}"
        with self._lock:
            temporary: Path | None = None
            state = self.get_status()
            item = self._get_item(state.model_dump(), target)
            if item["state"] != "ready":
                return False, "更新包尚未下载完成"
            try:
                prepared = self._read_prepared_manifest()
                if target == _APPLICATION:
                    self._validate_application_manifest(prepared)
                    message = "主程序更新包已就绪，正在重启安装"
                else:
                    self._validate_resource_manifest(prepared)
                    message = "站点资源包已就绪，正在重启安装"
                install_payload = self._read_install_manifest_optional()
                targets: list[SystemUpdateType] = [
                    cast(SystemUpdateType, value)
                    for value in install_payload.get("targets", [])
                    if value in _TARGETS
                ]
                if target not in targets:
                    targets.append(target)
                prepared["targets"] = targets
                self._install_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._install_file.with_suffix(f".tmp.{os.getpid()}")
                temporary.write_text(
                    json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                temporary.replace(self._install_file)
                self._write_item(target, state="installing", can_install=False, error=None)
                return True, message
            except (OSError, RuntimeError, json.JSONDecodeError) as error:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                self._write_item(target, state="failed", error=str(error), can_install=False)
                return False, str(error)

    def apply_prepared_update(self) -> tuple[bool, str]:
        """由 Docker root 更新 worker 将已确认制品替换到当前运行目录。"""
        if not is_docker():
            return False, "当前运行环境不是 Docker"

        targets: set[SystemUpdateType] = set()
        with self._lock:
            try:
                prepared = self._read_install_manifest()
                targets = self._prepared_targets(prepared)
                if not targets:
                    raise RuntimeError("更新清单缺少可安装目标")
                if _APPLICATION in targets:
                    self._validate_application_manifest(prepared)
                    version = str(prepared.get("version") or "")
                    frontend_version = str(prepared.get("frontend_version") or "")
                    if self._validate_backend_archive(version) != frontend_version:
                        raise RuntimeError("后端更新包声明的前端版本不匹配")
                    self._validate_frontend_archive(frontend_version)
                if _RESOURCES in targets:
                    self._validate_resource_manifest(prepared)

                if _APPLICATION in targets:
                    self._apply_docker_application(
                        prepared,
                        include_resources=_RESOURCES in targets,
                    )
                elif _RESOURCES in targets:
                    self._apply_docker_resources(prepared)

                try:
                    for target in (_APPLICATION, _RESOURCES):
                        if target in targets:
                            self._consume_prepared_target(target)
                except (OSError, RuntimeError, ValueError) as error:
                    # 载荷已经替换成功，清单清理失败不能阻止 worker 通知入口重启。
                    logger.warning(f"更新载荷已替换，但清理下载清单失败：{error}")
                try:
                    self._install_file.unlink(missing_ok=True)
                except OSError as error:
                    logger.warning(f"清理 Docker 更新安装清单失败：{error}")
                return True, "已下载的更新已替换到 Docker 程序目录"
            except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
                message = f"Docker 更新包替换失败：{error}"
                self._mark_install_failed(targets, message)
                logger.error(message)
                return False, message

    def _read_install_manifest(self) -> dict[str, Any]:
        """读取必须存在的 Docker 更新安装清单。"""
        payload = json.loads(self._install_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("更新安装清单格式无效")
        return payload

    @staticmethod
    def _prepared_targets(prepared: dict[str, Any]) -> set[SystemUpdateType]:
        """解析更新清单中的主程序和站点资源安装目标。"""
        raw_targets = prepared.get("targets")
        if raw_targets is not None and not isinstance(raw_targets, list):
            raise RuntimeError("更新清单目标格式无效")
        if isinstance(raw_targets, list) and any(
            not isinstance(target, str) or target not in _TARGETS
            for target in raw_targets
        ):
            raise RuntimeError("更新清单包含未知安装目标")
        targets = {
            cast(SystemUpdateType, target)
            for target in raw_targets or []
        }
        if not targets and prepared.get("backend_archive"):
            targets.add(_APPLICATION)
        return targets

    def _mark_install_failed(
        self, targets: set[SystemUpdateType], message: str
    ) -> None:
        """记录 Docker 更新失败并撤销本次安装意图，保留下载包供重试。"""
        try:
            self._install_file.unlink(missing_ok=True)
        except OSError as error:
            logger.warning(f"清理 Docker 更新安装清单失败：{error}")
        if not targets:
            try:
                state = self._read_state()
                targets = {
                    cast(SystemUpdateType, item["type"])
                    for item in state.get("updates", [])
                    if item.get("state") == "installing" and item.get("type") in _TARGETS
                }
            except Exception as error:  # noqa: BLE001  失败路径只记录，不能遮蔽原始错误
                logger.warning(f"读取待安装更新状态失败：{error}")
        for target in targets:
            try:
                self._write_item(
                    target,
                    state="failed",
                    error=message,
                    can_update=True,
                    can_install=False,
                )
            except Exception as error:  # noqa: BLE001  失败路径不得遮蔽原始安装错误
                logger.error(f"记录 {target} 更新失败状态失败：{error}")

    def _consume_prepared_target(self, target: SystemUpdateType) -> None:
        """从持久化下载清单移除已替换目标，保留另一类下载制品。"""
        prepared = self._read_prepared_manifest_optional()
        if target == _APPLICATION:
            for key in (
                "version",
                "frontend_version",
                "backend_archive",
                "frontend_archive",
                "backend_sha256",
                "frontend_sha256",
            ):
                prepared.pop(key, None)
        elif target == _RESOURCES:
            for key in ("resource_package_version", "resource_files"):
                prepared.pop(key, None)
        else:
            raise ValueError(f"未知升级类型：{target}")

        targets = [
            value
            for value in prepared.get("targets", [])
            if value in _TARGETS and value != target
        ]
        if targets:
            prepared["targets"] = targets
        else:
            prepared.pop("targets", None)

        prepared_file = self._root / "prepared.json"
        if prepared.get("backend_archive") or prepared.get("resource_files"):
            temporary = prepared_file.with_suffix(f".tmp.{os.getpid()}")
            temporary.write_text(
                json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(prepared_file)
        else:
            prepared_file.unlink(missing_ok=True)

    def _set_docker_pending(self, state: str) -> None:
        """原子写入 Docker 载荷切换状态，供入口脚本在异常重启时恢复。"""
        self._docker_pending_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._docker_pending_file.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(f"{state}\n", encoding="utf-8")
        temporary.replace(self._docker_pending_file)

    def _clear_docker_pending(self) -> None:
        """清除已经提交完成的 Docker 载荷切换状态。"""
        self._docker_pending_file.unlink(missing_ok=True)

    @staticmethod
    def _setting_text(key: str, default: str = "") -> str:
        """读取更新 worker 所需的运行配置，并兼容独立 root 子进程环境。"""
        try:
            value = get_runtime_setting(key)
        except AttributeError:
            value = None
        return str(value or os.getenv(key, default) or default).strip()

    def _sync_docker_dependencies(self, project_dir: Path, *, force: bool = False) -> bool:
        """按新后端清单同步 Docker 共享虚拟环境依赖。"""
        current_dir = self._docker_app_dir
        if not force and all(
            (current_dir / name).read_bytes() == (project_dir / name).read_bytes()
            for name in ("pyproject.toml", "uv.lock")
        ):
            return False

        venv_path = self._setting_text("VENV_PATH", "/opt/venv")
        uv_bin = self._setting_text("UV_BIN", "/usr/local/bin/uv")
        command = [
            uv_bin,
            "sync",
            "--project",
            str(project_dir),
            "--locked",
            "--inexact",
            "--no-dev",
            "--no-install-project",
            "--python",
            f"{venv_path}/bin/python3",
            *runtime_sync_arguments(),
        ]
        package_index = self._setting_text("PIP_PROXY")
        if package_index:
            command.extend(("--default-index", package_index))
        environment = os.environ.copy()
        proxy = self._setting_text("PROXY_HOST")
        if proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                environment[key] = proxy
        environment.update(
            {
                "UV_PROJECT_ENVIRONMENT": venv_path,
                "UV_LINK_MODE": "copy",
            }
        )
        try:
            result = subprocess.run(
                command,
                cwd=str(project_dir),
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise RuntimeError(f"依赖同步执行失败：{error}") from error
        if result.returncode != 0:
            raise RuntimeError(f"依赖同步失败，退出码：{result.returncode}")
        return True

    def _extract_backend_archive(self, archive_path: Path, destination: Path) -> Path:
        """安全解压后端 Release，并返回唯一的源码根目录。"""
        with zipfile.ZipFile(archive_path) as archive:
            self._validate_zip_members(archive)
            roots = {
                PurePosixPath(name).parts[0]
                for name in archive.namelist()
                if PurePosixPath(name).parts
            }
            if len(roots) != 1:
                raise RuntimeError("后端更新包源码根目录无效")
            archive.extractall(destination)
            source_root = destination / next(iter(roots))
        if not source_root.is_dir():
            raise RuntimeError("后端更新包源码目录不存在")
        return source_root

    def _extract_frontend_archive(self, archive_path: Path, destination: Path) -> Path:
        """安全解压前端 dist.zip，并返回静态文件目录。"""
        with zipfile.ZipFile(archive_path) as archive:
            self._validate_zip_members(archive)
            archive.extractall(destination)
        frontend_dir = destination / "dist"
        if not frontend_dir.is_dir():
            raise RuntimeError("前端更新包缺少 dist 目录")
        return frontend_dir

    @staticmethod
    def _remove_path(path: Path) -> None:
        """删除 Docker 更新事务中的文件或目录。"""
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    @staticmethod
    def _preserve_tree_ownership(source: Path, destination: Path) -> None:
        """复制运行时目录后恢复原目录的所有者，避免插件变成 root 不可写。"""
        source_paths = (source, *source.rglob("*"))
        for source_path in source_paths:
            destination_path = destination / source_path.relative_to(source)
            source_stat = source_path.lstat()
            os.chown(
                destination_path,
                source_stat.st_uid,
                source_stat.st_gid,
                follow_symlinks=False,
            )

    @staticmethod
    def _clear_staged_native_resources(resource_dir: Path) -> None:
        """清除暂存目录中的旧平台原生站点资源。"""
        if not resource_dir.is_dir():
            return
        for path in resource_dir.iterdir():
            if path.is_file() and path.name.startswith("sites.") and path.suffix in {
                ".so",
                ".pyd",
                ".dylib",
            }:
                path.unlink()

    def _resource_source_dir(self, app_dir: Path) -> Path:
        """定位当前后端携带的站点资源目录，并兼容历史目录。"""
        resource_dir = app_dir / "app" / "application" / "site"
        for legacy_dir in (
            app_dir / "app" / "infrastructure",
            app_dir / "app" / "adapters" / "network",
            app_dir / "app" / "helper",
        ):
            if not resource_dir.is_dir() and legacy_dir.is_dir():
                resource_dir = legacy_dir
        return resource_dir

    def _copy_prepared_resources(
        self, prepared: dict[str, Any], resource_dir: Path
    ) -> None:
        """把已校验的完整站点资源包复制到指定源码目录。"""
        self._clear_staged_native_resources(resource_dir)
        for item in prepared.get("resource_files", []):
            name = Path(str(item.get("name") or ""))
            if name.name != str(name):
                raise RuntimeError("站点资源文件名不安全")
            shutil.copy2(str(item["path"]), resource_dir / name)

    def _prepare_docker_application(
        self,
        prepared: dict[str, Any],
        temporary_root: Path,
        *,
        include_resources: bool,
    ) -> tuple[Path, Path]:
        """解压并组装待切换的 Docker 后端、前端和插件资源载荷。"""
        backend_extract = temporary_root / "backend"
        frontend_extract = temporary_root / "frontend"
        backend_extract.mkdir()
        frontend_extract.mkdir()
        source_app = self._extract_backend_archive(
            Path(str(prepared["backend_archive"])), backend_extract
        )
        source_public = self._extract_frontend_archive(
            Path(str(prepared["frontend_archive"])), frontend_extract
        )
        stage_app = temporary_root / "App"
        stage_public = temporary_root / "public"
        source_app.replace(stage_app)
        source_public.replace(stage_public)

        current_app = self._docker_app_dir
        current_plugins = current_app / "app" / "plugins"
        stage_plugins = stage_app / "app" / "plugins"
        if stage_plugins.exists() or stage_plugins.is_symlink():
            self._remove_path(stage_plugins)
        if current_plugins.is_dir():
            shutil.copytree(current_plugins, stage_plugins, symlinks=True)
            self._preserve_tree_ownership(current_plugins, stage_plugins)
        else:
            stage_plugins.mkdir(parents=True, exist_ok=True)
        if not (stage_plugins / "__init__.py").is_file():
            raise RuntimeError("插件运行目录缺少 app.plugins 兼容入口")

        stage_resources = stage_app / "app" / "application" / "site"
        if stage_resources.exists() or stage_resources.is_symlink():
            self._remove_path(stage_resources)
        current_resources = self._resource_source_dir(current_app)
        if current_resources.is_dir():
            shutil.copytree(current_resources, stage_resources, symlinks=True)
        else:
            stage_resources.mkdir(parents=True, exist_ok=True)
        if include_resources:
            self._copy_prepared_resources(prepared, stage_resources)
        return stage_app, stage_public

    def _restore_docker_payload(self) -> None:
        """在 Docker 载荷切换失败时恢复更新前的源码和前端目录。"""
        for current, previous in (
            (self._docker_app_dir, self._docker_previous_app_dir),
            (self._docker_public_dir, self._docker_previous_public_dir),
        ):
            if not previous.exists():
                continue
            if current.exists() or current.is_symlink():
                self._remove_path(current)
            previous.replace(current)

    def _apply_docker_application(
        self, prepared: dict[str, Any], *, include_resources: bool
    ) -> None:
        """原子替换 Docker 后端源码和前端静态目录，并同步依赖。"""
        app_dir = self._docker_app_dir
        public_dir = self._docker_public_dir
        previous_app = self._docker_previous_app_dir
        previous_public = self._docker_previous_public_dir
        if not app_dir.is_dir() or not public_dir.is_dir():
            raise RuntimeError("Docker 当前程序目录不完整")
        if previous_app.exists() or previous_public.exists():
            raise RuntimeError("存在未完成的 Docker 更新事务")

        with TemporaryDirectory(prefix=".moviepilot-update-", dir=str(app_dir.parent)) as temp:
            temporary_root = Path(temp)
            stage_app, stage_public = self._prepare_docker_application(
                prepared,
                temporary_root,
                include_resources=include_resources,
            )
            dependencies_changed = any(
                (app_dir / name).read_bytes() != (stage_app / name).read_bytes()
                for name in ("pyproject.toml", "uv.lock")
            )
            dependency_sync_started = False
            self._set_docker_pending("prepared")
            try:
                if dependencies_changed:
                    self._set_docker_pending("dependencies")
                    dependency_sync_started = True
                    self._sync_docker_dependencies(stage_app)
                self._set_docker_pending("prepared")
                app_dir.replace(previous_app)
                try:
                    public_dir.replace(previous_public)
                    stage_app.replace(app_dir)
                    stage_public.replace(public_dir)
                except OSError:
                    self._restore_docker_payload()
                    raise
                self._set_docker_pending("committed")
            except Exception:
                rollback_failed = False
                try:
                    self._restore_docker_payload()
                except OSError as error:
                    logger.error(f"Docker 更新回滚失败：{error}")
                    rollback_failed = True
                if dependency_sync_started:
                    try:
                        self._set_docker_pending("dependencies")
                        self._sync_docker_dependencies(app_dir, force=True)
                    except (OSError, RuntimeError) as error:
                        logger.error(f"Docker 更新依赖回滚失败：{error}")
                        rollback_failed = True
                if rollback_failed:
                    raise
                try:
                    self._clear_docker_pending()
                except OSError as error:
                    logger.error(f"清理 Docker 更新事务标记失败：{error}")
                    raise
                raise

        try:
            self._remove_path(previous_app)
            self._remove_path(previous_public)
            self._clear_docker_pending()
        except OSError as error:
            logger.warning(f"Docker 更新已完成但旧载荷清理失败：{error}")

    def _apply_docker_resources(self, prepared: dict[str, Any]) -> None:
        """备份并替换 Docker 站点资源，失败时恢复旧目录。"""
        resource_dir = self._resource_source_dir(self._docker_app_dir)
        resource_dir.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix=".moviepilot-resource-update-", dir=str(resource_dir.parent)
        ) as temp:
            stage_dir = Path(temp) / "site"
            if resource_dir.is_dir():
                shutil.copytree(resource_dir, stage_dir, symlinks=True)
            else:
                stage_dir.mkdir()
            self._copy_prepared_resources(prepared, stage_dir)
            backup_dir = resource_dir.with_name(f"{resource_dir.name}.__prepared_previous__")
            self._remove_path(backup_dir)
            if resource_dir.exists() or resource_dir.is_symlink():
                # OverlayFS 下镜像层目录不能直接重命名，先复制完整备份再移除。
                shutil.copytree(resource_dir, backup_dir, symlinks=True)
            try:
                self._remove_path(resource_dir)
                stage_dir.replace(resource_dir)
            except OSError:
                if backup_dir.exists():
                    self._remove_path(resource_dir)
                    backup_dir.replace(resource_dir)
                raise
            self._remove_path(backup_dir)

    def cancel_install(self, reason: str) -> None:
        """重启请求失败时撤销全部已选安装意图，避免下次普通启动意外安装。"""
        with self._lock:
            targets: list[SystemUpdateType] = []
            try:
                payload = json.loads(self._install_file.read_text(encoding="utf-8"))
                targets = [cast(SystemUpdateType, target) for target in payload.get("targets", []) if target in _TARGETS]
            except (OSError, json.JSONDecodeError):
                pass
            self._install_file.unlink(missing_ok=True)
            state = self._read_state()
            for target in targets or _TARGETS:
                item = self._get_item(state, target)
                if item.get("state") == "installing":
                    item.update(
                        {
                            "state": "ready",
                            "error": reason,
                            "can_update": False,
                            "can_install": True,
                        }
                    )
            self._persist_state(self._sync_aggregate(state))

    def _download_update(self, target: SystemUpdateType) -> None:
        """在后台线程中下载并校验指定升级类型的制品。"""
        try:
            if target == _APPLICATION:
                self._download_application()
            else:
                self._download_resources()
        except Exception as error:  # noqa: BLE001  后台线程必须沉淀为可查询失败
            logger.error(f"下载 {target} 更新包失败：{error}")
            self._write_item(target, state="failed", error=str(error), can_update=True, can_install=False)
        finally:
            with self._lock:
                self._download_active = False
                self._active_target = None

    def _download_application(self) -> None:
        """下载后端 Release 和其 version.py 声明的前端 dist.zip。"""
        target = self._get_item(self._read_state(), _APPLICATION)
        version = str(target.get("version") or "")
        if not version:
            raise RuntimeError("主程序更新缺少目标版本")
        downloaded, backend_total = self._download_file(
            self._proxied(self._BACKEND_ARCHIVE_URL.format(tag=version)),
            self._backend_archive,
            0,
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
        self._write_item(
            _APPLICATION,
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

        self._merge_prepared_manifest(
            {
                "version": version,
                "frontend_version": frontend_version,
                "backend_archive": str(self._backend_archive),
                "frontend_archive": str(self._frontend_archive),
                "backend_sha256": self._sha256(self._backend_archive),
                "frontend_sha256": frontend_sha256,
                "prepared_at": self._now(),
            },
        )
        self._write_item(
            _APPLICATION,
            state="ready",
            downloaded_bytes=downloaded,
            total_bytes=max(total, downloaded),
            error=None,
            can_update=False,
            can_install=True,
        )
        logger.info(f"MoviePilot {version} 更新包已下载完成，等待用户确认重启")

    def _download_resources(self) -> None:
        """下载当前平台认证与索引资源到外部暂存目录。"""
        current_auth, current_indexer = get_resource_versions()
        helper = ResourceHelper()
        info = helper.get_update_info(
            auth_version=current_auth,
            indexer_version=current_indexer,
        )
        if info is None:
            raise RuntimeError("站点资源已是最新版本")
        files = helper.get_download_files(info)
        total = sum(int(item.get("size") or 0) for item in files)
        self._write_item(_RESOURCES, total_bytes=total)
        downloaded = 0

        def on_progress(delta: int, _file_total: int) -> None:
            """把资源文件下载进度映射到统一状态。"""
            nonlocal downloaded
            downloaded += delta
            self._write_item(
                _RESOURCES,
                downloaded_bytes=downloaded,
                total_bytes=total,
            )

        prepared_files = helper.download_files(files, self._resource_dir, on_progress)
        self._merge_prepared_manifest(
            {
                "resource_package_version": info.get("package_version"),
                "resource_files": prepared_files,
                "prepared_at": self._now(),
            },
        )
        self._write_item(
            _RESOURCES,
            state="ready",
            version=info.get("package_version") or None,
            auth_version=info.get("auth_version"),
            indexer_version=info.get("indexer_version"),
            current_auth_version=current_auth,
            current_indexer_version=current_indexer,
            downloaded_bytes=downloaded,
            total_bytes=max(total, downloaded),
            error=None,
            can_update=False,
            can_install=True,
        )
        logger.info("站点资源包已下载完成，等待用户确认重启")

    def _merge_prepared_manifest(
        self,
        changes: dict[str, Any],
        *,
        remove_keys: set[str] | None = None,
    ) -> None:
        """合并不同升级类型的已下载制品，避免覆盖另一类待安装包。"""
        prepared = self._read_prepared_manifest_optional()
        for key in remove_keys or set():
            prepared.pop(key, None)
        prepared.update(changes)
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / "prepared.json").write_text(
            json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _discard_prepared_target(self, target: SystemUpdateType) -> None:
        """重新下载一类制品时只清理该类型的旧清单，保留另一类制品。"""
        prepared = self._read_prepared_manifest_optional()
        if target == _APPLICATION:
            for key in (
                "version",
                "frontend_version",
                "backend_archive",
                "frontend_archive",
                "backend_sha256",
                "frontend_sha256",
            ):
                prepared.pop(key, None)
        else:
            for key in ("resource_package_version", "resource_files"):
                prepared.pop(key, None)
        targets = [
            value
            for value in prepared.get("targets", [])
            if value in _TARGETS and value != target
        ]
        if targets:
            prepared["targets"] = targets
        else:
            prepared.pop("targets", None)
        if prepared:
            (self._root / "prepared.json").write_text(
                json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            (self._root / "prepared.json").unlink(missing_ok=True)

    def _read_prepared_manifest_optional(self) -> dict[str, Any]:
        """读取可选的已下载制品清单。"""
        try:
            payload = json.loads((self._root / "prepared.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_prepared_manifest(self) -> dict[str, Any]:
        """读取必须存在的已下载制品清单。"""
        payload = json.loads((self._root / "prepared.json").read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("更新包清单格式无效")
        return payload

    def _read_install_manifest_optional(self) -> dict[str, Any]:
        """读取当前重启请求，避免把历史安装目标重新加入本次请求。"""
        try:
            payload = json.loads(self._install_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _validate_application_manifest(self, prepared: dict[str, Any]) -> None:
        """校验主程序后端包和配套前端包的路径、摘要与版本。"""
        backend_archive = Path(str(prepared.get("backend_archive") or ""))
        frontend_archive = Path(str(prepared.get("frontend_archive") or ""))
        if not prepared.get("version") or not prepared.get("frontend_version"):
            raise RuntimeError("主程序更新清单缺少版本信息")
        for archive, expected in (
            (backend_archive, self._backend_archive),
            (frontend_archive, self._frontend_archive),
        ):
            if archive.resolve() != expected.resolve():
                raise RuntimeError("主程序更新包路径不安全")
        if not backend_archive.is_file() or self._sha256(backend_archive) != prepared.get("backend_sha256"):
            raise RuntimeError("后端更新包校验失败")
        if not frontend_archive.is_file() or self._sha256(frontend_archive) != prepared.get("frontend_sha256"):
            raise RuntimeError("前端更新包校验失败")

    def _validate_resource_manifest(self, prepared: dict[str, Any]) -> None:
        """校验完整资源文件只能来自更新暂存目录且摘要匹配。"""
        files = prepared.get("resource_files")
        if not isinstance(files, list) or not files:
            raise RuntimeError("站点资源更新清单为空")
        expected_names = set(ResourceHelper._get_needed_files())
        actual_names = {
            str(item.get("name") or "") for item in files if isinstance(item, dict)
        }
        if len(files) != len(expected_names) or actual_names != expected_names:
            raise RuntimeError("站点资源更新清单不是当前平台的完整资源包")
        root = self._root.resolve()
        for item in files:
            if not isinstance(item, dict):
                raise RuntimeError("站点资源更新清单格式无效")
            path = Path(str(item.get("path") or ""))
            try:
                path.resolve().relative_to(root)
            except ValueError as error:
                raise RuntimeError("站点资源文件路径不安全") from error
            if not path.is_file() or self._sha256(path) != item.get("sha256"):
                raise RuntimeError(f"站点资源文件校验失败：{item.get('name')}")

    def _request(self) -> RequestUtils:
        """创建访问 GitHub Release 的请求客户端。"""
        return RequestUtils(
            proxies=get_runtime_setting("PROXY"),
            headers=get_runtime_setting("GITHUB_HEADERS"),
            timeout=60,
        )

    def _download_file(
        self, url: str, destination: Path, downloaded_before: int, total_hint: int
    ) -> tuple[int, int]:
        """流式下载文件并把进度写入当前升级类型。"""
        temporary = destination.with_suffix(".part")
        temporary.unlink(missing_ok=True)
        with self._request().get_stream(url) as response:
            if response is None or response.status_code != 200:
                raise RuntimeError(
                    f"下载更新包失败：HTTP {getattr(response, 'status_code', '无响应')}"
                )
            content_length = int(response.headers.get("content-length") or 0)
            total = total_hint or content_length
            current = downloaded_before
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    current += len(chunk)
                    self._write_item(
                        self._active_target or _APPLICATION,
                        downloaded_bytes=current,
                        total_bytes=total,
                    )
        temporary.replace(destination)
        return current, content_length

    def _fetch_frontend_release(self, version: str) -> dict[str, Any]:
        """读取后端版本声明对应的前端 Release。"""
        response = self._request().get_res(self._FRONTEND_RELEASE_API.format(tag=version))
        if response is None or response.status_code != 200:
            raise RuntimeError(f"无法获取前端 {version} Release")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("前端 Release 返回格式异常")
        return payload

    def _validate_backend_archive(self, version: str) -> str:
        """校验后端压缩包结构，并只读取其声明的前端版本。"""
        with zipfile.ZipFile(self._backend_archive) as archive:
            self._validate_zip_members(archive)
            version_name = next(
                (
                    name
                    for name in archive.namelist()
                    if name.count("/") == 1 and name.endswith("/version.py")
                ),
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
        """校验前端包只包含可接受的发布目录和匹配版本。"""
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
        """拒绝绝对路径、目录穿越和符号链接成员。"""
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            file_type = (item.external_attr >> 16) & 0o170000
            if path.is_absolute() or ".." in path.parts or file_type == stat.S_IFLNK:
                raise RuntimeError("更新包包含不安全路径")

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
        """计算文件 SHA256。"""
        digest = hashlib.sha256()
        with path.open("rb") as file_handle:
            for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _proxied(url: str) -> str:
        """为 GitHub 下载地址添加配置的代理前缀。"""
        proxy = str(get_runtime_setting("GITHUB_PROXY") or "").strip()
        return f"{proxy}{url}" if proxy else url


system_update_manager = SystemUpdateManager()
