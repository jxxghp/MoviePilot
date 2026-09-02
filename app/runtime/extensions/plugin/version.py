"""插件源码按版本分目录布局的目录名映射、元信息读写、存量迁移与加载路径解析。"""

from __future__ import annotations

import ast
import errno
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.foundation.version import compare_version
from app.runtime.log import logger

# 插件源码版本目录名的前缀，用于把版本目录与插件目录下的其它条目区分开
PLUGIN_VERSION_DIR_PREFIX = "v"
# 插件已装版本元信息文件名，位于 app/plugins/<插件ID>/ 下，不是 Python 模块
PLUGIN_VERSIONS_MANIFEST_NAME = "versions.json"
# 版本元信息文件的结构版本号
PLUGIN_VERSIONS_MANIFEST_SCHEMA = 1
# 存量布局迁移过程中的改名中转目录名中缀，与插件源码目录同级
_PLUGIN_LAYOUT_STAGING_INFIX = ".migrating-"
# 存量插件读不到版本号时使用的兜底版本号
PLUGIN_FALLBACK_VERSION = "0.0.0"
# 合法版本号字符集：数字、字母、点、连字符、加号。语义化版本的先行版与构建
# 元数据字符集不含下划线，据此保证点与下划线的互换是单射、可逆
_PLUGIN_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")


def plugin_version_dir_name(version: str) -> str:
    """把插件版本号映射为版本目录名。

    映射规则为前缀 ``v`` 加上版本号中的 ``.`` 全部换成 ``_``，例如 ``1.2.0``
    映射为 ``v1_2_0``。版本号含下划线时直接拒绝，不做静默转换，否则两个不同
    版本号会映射到同一个目录。

    :param version: 插件版本号
    :return: 版本目录名
    :raise ValueError: 版本号为空、含下划线，或含版本号字符集以外的字符
    """
    text = (version or "").strip()
    if not text:
        raise ValueError("插件版本号为空")
    if "_" in text:
        raise ValueError(f"插件版本号含下划线，无法映射为版本目录：{version}")
    if not _PLUGIN_VERSION_PATTERN.match(text) or ".." in text or text.endswith("."):
        raise ValueError(f"插件版本号不是语义化版本：{version}")
    return f"{PLUGIN_VERSION_DIR_PREFIX}{text.replace('.', '_')}"


def plugin_version_from_dir_name(dir_name: str) -> str | None:
    """把版本目录名反解为插件版本号。

    反解规则是去掉前导 ``v`` 后把 ``_`` 换回 ``.``。反解结果需能原样映射回原
    目录名，否则视为不是版本目录，据此排除 dist、wheels、__pycache__ 等目录。

    :param dir_name: 目录名
    :return: 版本号；不是版本目录时为 None
    """
    if not dir_name or not dir_name.startswith(PLUGIN_VERSION_DIR_PREFIX):
        return None
    core = dir_name[len(PLUGIN_VERSION_DIR_PREFIX):]
    if not core or "." in core:
        return None
    version = core.replace("_", ".")
    try:
        if plugin_version_dir_name(version) != dir_name:
            return None
    except ValueError:
        return None
    return version


def plugin_version_dirs(plugin_root: Path) -> dict[str, Path]:
    """列出插件源码目录下所有版本目录。

    :param plugin_root: 插件源码根目录（app/plugins/<插件ID>）
    :return: 版本号到版本目录的映射，目录不存在时为空字典
    """
    result: dict[str, Path] = {}
    try:
        entries = sorted(plugin_root.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return result
    for entry in entries:
        if not entry.is_dir():
            continue
        version = plugin_version_from_dir_name(entry.name)
        if version:
            result[version] = entry
    return result


def read_plugin_versions_manifest(plugin_root: Path) -> dict[str, Any]:
    """读取插件已装版本元信息。

    :param plugin_root: 插件源码根目录
    :return: 元信息字典，文件缺失、损坏或格式不是字典时为空字典
    """
    manifest_file = plugin_root / PLUGIN_VERSIONS_MANIFEST_NAME
    try:
        payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        logger.warning(f"插件版本元信息不可读，按未登记处理：{manifest_file} - {err}")
        return {}
    return payload if isinstance(payload, dict) else {}


def write_plugin_versions_manifest(
    plugin_root: Path,
    versions: list[dict[str, Any]],
    current: str | None,
) -> None:
    """写入插件已装版本元信息。

    :param plugin_root: 插件源码根目录
    :param versions: 版本条目列表，每条含 version、directory、installed_at、source
    :param current: 当前生效版本号
    """
    payload = {
        "schema_version": PLUGIN_VERSIONS_MANIFEST_SCHEMA,
        "plugin_id": plugin_root.name,
        "current": current,
        "versions": versions,
    }
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / PLUGIN_VERSIONS_MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def plugin_manifest_versions(plugin_root: Path) -> dict[str, str]:
    """读取元信息登记的版本号到目录名映射，并校验目录名可由版本号推出。

    目录名不是权威真值，权威在元信息的版本号。两者不一致时告警并以元信息为准。

    :param plugin_root: 插件源码根目录
    :return: 版本号到目录名的映射
    """
    result: dict[str, str] = {}
    for entry in read_plugin_versions_manifest(plugin_root).get("versions") or []:
        if not isinstance(entry, dict):
            continue
        version = entry.get("version")
        directory = entry.get("directory")
        if not isinstance(version, str) or not version:
            continue
        try:
            expected = plugin_version_dir_name(version)
        except ValueError as err:
            logger.warning(f"插件 {plugin_root.name} 元信息版本号非法，已忽略：{err}")
            continue
        if isinstance(directory, str) and directory and directory != expected:
            logger.warning(
                f"插件 {plugin_root.name} 版本 {version} 的目录名 {directory} "
                f"与元信息不一致，以元信息为准使用 {expected}"
            )
        result[version] = expected
    return result


def ensure_plugin_version_dir_available(plugin_root: Path, version: str) -> str:
    """校验版本号可安装并返回其版本目录名。

    除版本号字符集校验外，还对同插件已装版本做大小写不敏感比对，避免在大小写
    不敏感的文件系统上两个版本落到同一个目录。

    :param plugin_root: 插件源码根目录
    :param version: 待安装版本号
    :return: 版本目录名
    :raise ValueError: 版本号非法，或与已装版本大小写撞名
    """
    dir_name = plugin_version_dir_name(version)
    known: dict[str, str] = dict(plugin_manifest_versions(plugin_root))
    known.update(
        {installed: path.name for installed, path in plugin_version_dirs(plugin_root).items()}
    )
    for installed_version, installed_dir in known.items():
        if installed_version == version:
            continue
        if installed_dir.lower() == dir_name.lower():
            raise ValueError(
                f"插件版本 {version} 与已装版本 {installed_version} 的目录名仅大小写不同，拒绝安装"
            )
    return dir_name


def register_plugin_version(plugin_root: Path, version: str, source: str) -> str:
    """把一个已就位的版本目录登记进版本元信息，并置为当前版本。

    调用方需确保 ``plugin_root / <版本目录>`` 已经就位了该版本的源码；本函数
    只更新元信息，不做任何文件搬迁，因此可以安全地被存量迁移和真正的多版本
    安装共用。

    :param plugin_root: 插件源码根目录
    :param version: 版本号
    :param source: 版本来源，如 local、migrated
    :return: 版本目录名
    :raise ValueError: 版本号非法
    """
    dir_name = plugin_version_dir_name(version)
    manifest = read_plugin_versions_manifest(plugin_root)
    versions = [
        entry
        for entry in (manifest.get("versions") or [])
        if isinstance(entry, dict) and entry.get("version") != version
    ]
    versions.append(
        {
            "version": version,
            "directory": dir_name,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
        }
    )
    write_plugin_versions_manifest(plugin_root, versions, version)
    return dir_name


def _find_leftover_layout_staging(plugin_root: Path) -> Path | None:
    """在插件源码目录同级查找上次迁移中断遗留的改名中转目录。

    :param plugin_root: 插件源码根目录
    :return: 遗留的中转目录；不存在时为 None
    """
    parent = plugin_root.parent
    if not parent.is_dir():
        return None
    prefix = f"{plugin_root.name}{_PLUGIN_LAYOUT_STAGING_INFIX}"
    candidates = sorted(
        entry
        for entry in parent.iterdir()
        if entry.is_dir() and entry.name.startswith(prefix)
    )
    return candidates[0] if candidates else None


def _is_reserved_layout_entry(entry: Path) -> bool:
    """判断插件源码目录下的条目是否属于版本化布局自身，不参与存量迁移。

    :param entry: 插件源码目录下的条目
    :return: 是版本目录或元信息文件时为 True
    """
    if entry.name == PLUGIN_VERSIONS_MANIFEST_NAME:
        return True
    return entry.is_dir() and plugin_version_from_dir_name(entry.name) is not None


def migrate_legacy_plugin_layout(plugin_root: Path) -> Path | None:
    """把平铺布局的存量插件源码原地迁移为按版本分目录的布局。

    先把平铺源码改名搬到同级中转目录，再一次改名落到版本目录，最后登记版本
    元信息；元信息写入同时充当迁移完成哨兵，中断后重入会发现遗留的中转目录
    并续做。跨设备无法原子改名时放弃迁移，插件继续按存量布局加载——加载路径
    只在真正要装第二个版本时才调用本函数，平时的加载不会触发磁盘改动。

    :param plugin_root: 插件源码根目录
    :return: 迁移后的版本目录；无需迁移时为 None；放弃迁移时为仍持有源码的目录
    """
    staging = _find_leftover_layout_staging(plugin_root)
    # 只有插件目录下直接放着主模块才算存量平铺布局；已迁移目录里的杂项条目
    # （构建残留、临时文件）不能被当成一个待迁移的版本
    has_flat_source = (plugin_root / "__init__.py").is_file()
    if staging is None and not has_flat_source:
        return None
    pending = (
        [
            entry
            for entry in plugin_root.iterdir()
            if not _is_reserved_layout_entry(entry)
        ]
        if has_flat_source
        else []
    )

    source_root = staging if staging is not None and staging.is_dir() else plugin_root
    version = read_declared_plugin_version(source_root / "__init__.py")
    if not version:
        version = PLUGIN_FALLBACK_VERSION
        logger.warning(
            f"插件 {plugin_root.name} 未声明版本号，存量源码按兜底版本 "
            f"{PLUGIN_FALLBACK_VERSION} 迁移"
        )
    try:
        dir_name = plugin_version_dir_name(version)
    except ValueError as err:
        logger.error(f"插件 {plugin_root.name} 版本号无法映射为版本目录，放弃迁移：{err}")
        return plugin_root if (plugin_root / "__init__.py").is_file() else None

    target = plugin_root / dir_name
    if staging is None:
        staging = plugin_root.parent / (
            f"{plugin_root.name}{_PLUGIN_LAYOUT_STAGING_INFIX}{uuid.uuid4().hex}"
        )
    try:
        if pending:
            staging.mkdir(parents=True, exist_ok=True)
            for entry in pending:
                os.rename(entry, staging / entry.name)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(staging, target)
    except OSError as error:
        if getattr(error, "errno", None) == errno.EXDEV:
            logger.warning(
                f"插件源码目录跨设备无法原子改名，放弃本次迁移：{plugin_root} - {error}"
            )
        else:
            logger.error(f"插件源码目录迁移失败：{plugin_root} - {error}")
        if staging.is_dir() and not any(staging.iterdir()):
            staging.rmdir()
        if (plugin_root / "__init__.py").is_file():
            return plugin_root
        return staging if staging.is_dir() else None

    try:
        register_plugin_version(plugin_root, version, source="migrated")
    except OSError as error:
        logger.warning(f"插件版本元信息写入失败，下次加载将重试：{plugin_root} - {error}")
    return target


def read_declared_plugin_version(init_file: Path) -> str | None:
    """静态解析插件主模块声明的版本号，不导入插件代码。

    :param init_file: 插件主模块 __init__.py 路径
    :return: 版本号；解析不到时为 None
    """
    try:
        tree = ast.parse(init_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            targets: list[ast.expr]
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
            else:
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "plugin_version"
                for target in targets
            ):
                continue
            value = statement.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value.strip() or None
    return None


def resolve_plugin_version_dir(plugin_root: Path, version: str | None = None) -> Path:
    """定位插件本次要加载的源码目录。

    指定版本时返回该版本的版本目录；未指定时返回版本元信息登记的当前版本目录，
    元信息缺失或指向磁盘上不存在的目录时回落到版本号最高的已装版本。插件根目录
    下没有任何版本目录时，视为存量平铺布局，回落到插件根目录本身，使今天没有
    安装任何版本目录的插件加载路径与本函数引入前逐字一致。

    :param plugin_root: 插件源码根目录
    :param version: 指定加载的版本号，为空时取元信息里的当前版本
    :return: 源码目录；没有版本目录时为插件根目录本身
    :raise ValueError: 指定的版本号没有对应的已装版本目录
    """
    on_disk = plugin_version_dirs(plugin_root)
    if not on_disk:
        return plugin_root

    if version:
        target = on_disk.get(version)
        if target is None:
            raise ValueError(f"插件 {plugin_root.name} 未安装版本 {version}")
        return target

    manifest = read_plugin_versions_manifest(plugin_root)
    current = manifest.get("current")
    if isinstance(current, str) and current:
        if current in on_disk:
            return on_disk[current]
        logger.warning(
            f"插件 {plugin_root.name} 元信息登记的当前版本 {current} 在磁盘上不存在，"
            f"回落到版本号最高的已装版本"
        )

    newest = next(iter(sorted(on_disk)))
    for candidate in on_disk:
        if compare_version(candidate, ">", newest):
            newest = candidate
    return on_disk[newest]
