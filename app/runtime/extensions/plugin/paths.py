"""插件运行目录、本地仓和联邦产物路径解析。"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Optional

from app.runtime.extensions.plugin.system import PluginSystemServices
from app.runtime.extensions.plugin.version import (
    plugin_version_from_dir_name,
    resolve_plugin_version_dir,
)


class PluginPathResolver:
    """把文件事件解析为插件 ID、本地候选和联邦入口状态。"""

    def __init__(
        self,
        *,
        runtime_root: Path,
        running: Callable[[], Mapping[str, Any]],
        system: Callable[[], PluginSystemServices],
        strict_system_version: Callable[[], bool],
        log: Any,
    ) -> None:
        """保存运行目录和插件市场路径解析端口。"""
        self._runtime_root = runtime_root.resolve()
        self._running = running
        self._system = system
        self._strict_system_version = strict_system_version
        self._logger = log

    def federated_change(
        self,
        event_path: Path,
    ) -> Optional[tuple[str, Optional[dict], bool]]:
        """识别联邦构建产物变化并确认入口文件已完整生成。"""
        try:
            event_path = event_path.resolve()
            candidate = self.local_candidate(event_path)
            if candidate:
                plugin_id = candidate.get("id")
                plugin_dir = Path(candidate.get("path")).resolve()
            else:
                if not event_path.is_relative_to(self._runtime_root):
                    return None
                relative_parts = event_path.relative_to(self._runtime_root).parts
                if not relative_parts:
                    return None
                plugin_dir = self._runtime_root / relative_parts[0]
                plugin_id = next(
                    (
                        item
                        for item in self._running()
                        if item.lower() == relative_parts[0].lower()
                    ),
                    None,
                )
            if not plugin_id:
                return None
            plugin = self._running().get(plugin_id)
            if not plugin:
                return None
            render_mode, dist_path = plugin.get_render_mode()
            if render_mode != "vue" or not isinstance(dist_path, str) or not dist_path:
                return None
            relative_dist_path = Path(dist_path)
            if (
                relative_dist_path.is_absolute()
                or ".." in relative_dist_path.parts
                or "\\" in dist_path
            ):
                return None
            plugin_dir = plugin_dir.resolve()
            # 联邦产物随源码一起落在版本目录里，遏制范围也要跟着收到版本目录，
            # 否则一个版本的构建事件会被另一个版本的 dist 路径判定为越界
            version_dir = resolve_plugin_version_dir(plugin_dir)
            dist_dir = (version_dir / relative_dist_path).resolve()
            if (
                dist_dir == version_dir
                or not dist_dir.is_relative_to(version_dir)
                or not event_path.is_relative_to(dist_dir)
            ):
                return None
            remote_entry = dist_dir / "remoteEntry.js"
            ready = remote_entry.is_file() and remote_entry.resolve().is_relative_to(
                version_dir
            )
            return plugin_id, candidate, ready
        except Exception as error:
            self._logger.error(f"识别插件联邦构建产物变化时出错: {error}")
            return None

    def runtime_plugin(self, event_path: Path) -> Optional[str]:
        """从运行目录中的插件 ``__init__.py`` AST 解析插件类名。

        版本化布局把源码放在 ``<插件ID>/v1_2_0`` 下；事件落在某个版本目录内时
        按该版本目录取主模块，否则插件根目录没有平铺 ``__init__.py``，事件会被
        直接丢弃，版本目录里的源码改动将不再触发重载。
        """
        try:
            event_path = event_path.resolve()
            if not event_path.is_relative_to(self._runtime_root):
                return None
            parts = event_path.relative_to(self._runtime_root).parts
            if not parts:
                return None
            source_root = self._runtime_root / parts[0]
            if len(parts) > 1 and plugin_version_from_dir_name(parts[1]):
                source_root = source_root / parts[1]
            init_file = source_root / "__init__.py"
            if not init_file.exists():
                return None
            tree = ast.parse(
                init_file.read_text(encoding="utf-8", errors="replace")
            )
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if any(
                    isinstance(base, ast.Name) and base.id == "_PluginBase"
                    for base in node.bases
                ):
                    return node.name
            return None
        except Exception as error:
            self._logger.error(f"从路径解析插件 ID 时出错: {error}")
            return None

    def local_candidate(self, event_path: Path) -> Optional[dict]:
        """按 ``plugins``、``plugins.v2``、``plugins.v3`` 目录解析候选。"""
        try:
            event_path = event_path.resolve()
            for repo_path in self._system().local_repo_paths():
                if not repo_path.exists() or not repo_path.is_dir():
                    continue
                if not event_path.is_relative_to(repo_path):
                    continue
                parts = event_path.relative_to(repo_path).parts
                if len(parts) < 2:
                    continue
                if parts[0] == "plugins":
                    package_version = ""
                elif parts[0].startswith("plugins."):
                    package_version = parts[0].split(".", 1)[1]
                else:
                    continue
                return self._system().local_candidate(
                    parts[1],
                    package_version=package_version,
                    repo_path=repo_path,
                    strict_compat=False,
                    strict_system_version=self._strict_system_version(),
                )
            return None
        except Exception as error:
            self._logger.error(f"从本地插件仓路径解析候选时出错: {error}")
            return None
