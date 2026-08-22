"""插件源码目录的版本化布局：版本目录命名、已装版本元信息、存量布局迁移。

宿主按 app/plugins/<插件ID>/<版本目录>/ 的版本化布局定位插件源码，用元信息文件
登记已装版本与当前版本，并把旧的平铺布局原地迁移为版本化布局。这些都是宿主内部
的目录管理机制，插件代码不需要也不应该调用，因此不进入 SDK 公开面。
"""

import ast
import errno
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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
# 元数据字符集不含下划线，据此保证点与下划线的互换是单射、可逆。
_PLUGIN_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
# 插件版本回收默认保留的最近版本数（按登记时间，含当前版本）。1 起不到「留退路」
# 的作用；4 个以上在磁盘占用与回退冗余之间收益递减。2 是能满足「装错新版本后
# 一键切回上一版」这一典型场景的最小值，故取为默认值。
PLUGIN_VERSION_RETENTION_WINDOW = 2


def plugin_version_dir_name(version: str) -> str:
    """把插件版本号映射为版本目录名。

    映射规则为前缀 ``v`` 加上版本号中的 ``.`` 全部换成 ``_``，例如 ``1.2.0``
    映射为 ``v1_2_0``。版本号含下划线或其它非法字符时直接拒绝，不做静默转换，
    否则两个不同版本会映射到同一个目录。

    :param version: 插件版本号
    :return: 版本目录名
    :raises ValueError: 版本号为空、含下划线，或含版本号字符集以外的字符
    """
    text = (version or "").strip()
    if not text:
        raise ValueError("插件版本号为空")
    if "_" in text:
        raise ValueError(f"插件版本号含下划线，无法映射为版本目录：{version}")
    if not _PLUGIN_VERSION_PATTERN.match(text) or ".." in text or text.endswith("."):
        raise ValueError(f"插件版本号不是语义化版本：{version}")
    return f"{PLUGIN_VERSION_DIR_PREFIX}{text.replace('.', '_')}"


def plugin_version_from_dir_name(dir_name: str) -> Optional[str]:
    """把版本目录名反解为插件版本号。

    反解规则是去掉前导 ``v`` 后把 ``_`` 换回 ``.``。反解结果需能原样映射回原
    目录名，否则视为不是版本目录。

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


def plugin_version_dirs(plugin_root: Path) -> Dict[str, Path]:
    """列出插件源码目录下所有版本目录。

    :param plugin_root: 插件源码根目录（app/plugins/<插件ID>）
    :return: 版本号到版本目录的映射，目录不存在时为空字典
    """
    result: Dict[str, Path] = {}
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


def read_plugin_versions_manifest(plugin_root: Path) -> Dict[str, Any]:
    """读取插件已装版本元信息。

    :param plugin_root: 插件源码根目录
    :return: 元信息字典，文件缺失或损坏时为空字典
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
    versions: List[Dict[str, Any]],
    current: Optional[str],
) -> None:
    """写入插件已装版本元信息。

    :param plugin_root: 插件源码根目录
    :param versions: 版本条目列表，每条含 version、directory、installed_at、source
    :param current: 当前版本号
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


def plugin_manifest_versions(plugin_root: Path) -> Dict[str, str]:
    """读取元信息登记的版本号到目录名映射，并校验目录名可由版本号推出。

    目录名不是权威真值，权威在元信息的版本号。两者不一致时告警并以元信息为准。

    :param plugin_root: 插件源码根目录
    :return: 版本号到目录名的映射
    """
    result: Dict[str, str] = {}
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
    :raises ValueError: 版本号非法，或与已装版本大小写撞名
    """
    dir_name = plugin_version_dir_name(version)
    known = dict(plugin_manifest_versions(plugin_root))
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


def read_declared_plugin_version(init_file: Path) -> Optional[str]:
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
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target] if isinstance(statement, ast.AnnAssign)
                else []
            )
            if not any(
                isinstance(target, ast.Name) and target.id == "plugin_version"
                for target in targets
            ):
                continue
            value = statement.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value.strip() or None
    return None


def _find_leftover_layout_staging(plugin_root: Path) -> Optional[Path]:
    """在插件源码目录同级查找上次迁移中断遗留的改名中转目录。

    :param plugin_root: 插件源码根目录
    :return: 遗留的中转目录，不存在时为 None
    """
    parent = plugin_root.parent
    if not parent.is_dir():
        return None
    prefix = f"{plugin_root.name}{_PLUGIN_LAYOUT_STAGING_INFIX}"
    candidates = sorted(
        entry for entry in parent.iterdir() if entry.is_dir() and entry.name.startswith(prefix)
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


def migrate_legacy_plugin_layout(plugin_root: Path) -> Optional[Path]:
    """把 app/plugins/<插件ID>/<源码> 的存量布局迁移为按版本分目录的布局。

    先把平铺源码改名搬到同级中转目录，再一次改名落到版本目录，最后写入版本
    元信息文件；元信息文件同时充当迁移完成哨兵。中断后重入会发现遗留的中转
    目录并续做。跨设备无法原子改名时放弃迁移，插件继续按存量布局加载。

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
        [entry for entry in plugin_root.iterdir() if not _is_reserved_layout_entry(entry)]
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
        register_plugin_version(plugin_root, version, dir_name, source="migrated")
    except OSError as error:
        logger.warning(f"插件版本元信息写入失败，下次加载将重试：{plugin_root} - {error}")
    return target


def register_plugin_version(
    plugin_root: Path,
    version: str,
    dir_name: str,
    source: str,
) -> None:
    """把落地的版本登记进插件版本元信息，并置为当前版本。

    :param plugin_root: 插件源码根目录
    :param version: 版本号
    :param dir_name: 版本目录名
    :param source: 版本来源，如 market、local、migrated
    """
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


def resolve_plugin_version_dir(
    plugin_root: Path,
    version: Optional[str] = None,
    migrate: bool = True,
) -> Optional[Path]:
    """定位插件本次要加载的源码目录，必要时先完成一次存量布局迁移。

    未指定版本时按元信息登记的当前版本取；元信息缺失或指向不存在的目录时，
    回落到磁盘上版本号最高的版本目录。

    :param plugin_root: 插件源码根目录
    :param version: 指定加载的版本号，为空时取元信息里的当前版本
    :param migrate: 是否允许顺带迁移存量平铺布局，只读调用方传 False
    :return: 源码目录；放弃迁移时为仍持有存量源码的插件根目录；无源码时为 None
    """
    if not plugin_root.is_dir():
        return None

    if migrate:
        migrated = migrate_legacy_plugin_layout(plugin_root)
        if migrated is not None and migrated == plugin_root:
            return plugin_root
    elif (plugin_root / "__init__.py").is_file() and not plugin_version_dirs(plugin_root):
        return plugin_root

    on_disk = plugin_version_dirs(plugin_root)
    if not on_disk:
        return None

    if version:
        return on_disk.get(version)

    manifest = read_plugin_versions_manifest(plugin_root)
    current = manifest.get("current")
    if isinstance(current, str) and current:
        if current in on_disk:
            return on_disk[current]
        logger.warning(
            f"插件 {plugin_root.name} 元信息登记的当前版本 {current} 在磁盘上不存在，"
            f"回落到版本号最高的已装版本"
        )

    newest = None
    for candidate in sorted(on_disk):
        if newest is None or compare_version(candidate, ">", newest):
            newest = candidate
    return on_disk[newest]


def _delete_plugin_version_dir(plugin_root: Path, version: str, directory: Path) -> bool:
    """删除单个插件版本目录，删除前三重校验，任一不通过即拒绝且不删除。

    校验顺序：目录 ``resolve()`` 后确认位于插件目录之内；确认不等于插件目录
    本身；确认目录名可反解为待删除的版本号本身，据此排除 dist、wheels、
    __pycache__ 等保留条目，也排除元信息与磁盘目录名不一致的条目。删除失败
    （占用、权限等）只记错误日志、不向上抛出，不影响其余版本的回收。
    :param plugin_root: 插件源码根目录
    :param version: 待删除的版本号
    :param directory: 待删除的版本目录
    :return: 是否已删除
    """
    resolved_root = plugin_root.resolve()
    resolved_dir = directory.resolve()
    checks_passed = (
        resolved_dir.is_relative_to(resolved_root)
        and resolved_dir != resolved_root
        and plugin_version_from_dir_name(resolved_dir.name) == version
    )
    if not checks_passed:
        logger.error(f"插件版本目录校验未通过，跳过删除：{resolved_dir}")
        return False
    try:
        shutil.rmtree(resolved_dir)
        return True
    except OSError as error:
        logger.error(f"插件版本目录删除失败：{resolved_dir} - {error}")
        return False


def recycle_plugin_version_directories(
    plugin_root: Path,
    referenced_versions: Set[str],
    keep_recent: int = PLUGIN_VERSION_RETENTION_WINDOW,
) -> Dict[str, Any]:
    """回收插件源码目录下没有实例引用、不在保留窗口内的旧版本目录。

    保留判据（满足其一即保留，且判据取值均为调用方实测的运行态与配置，本函数
    不按目录时间戳猜测）：该版本是版本元信息登记的当前安装版本；该版本落在
    ``referenced_versions`` 里——调用方须确保该集合已经并入实例的已生效版本与
    按跟随开关解析出的期望版本两者，否则会删掉正在用或即将切换到的版本；该
    版本按登记时间排在最近 ``keep_recent`` 个以内。删除前逐一重新校验目录仍是
    该插件下的合法版本目录，单个目录删除失败不影响其余目录的回收，最后把
    已删除的版本从已装版本清单中一并摘除。
    :param plugin_root: 插件源码根目录
    :param referenced_versions: 当前被实例占用的版本号集合（已生效版本 ∪ 按跟随
        开关解析出的期望版本），由调用方基于实测的实例配置算出
    :param keep_recent: 额外按登记时间保留的最近版本数，含当前版本，取值理由见
        ``PLUGIN_VERSION_RETENTION_WINDOW``
    :return: 含 removed（已删除版本号列表）与 kept（版本号到保留理由的映射）的字典
    """
    on_disk = plugin_version_dirs(plugin_root)
    if not on_disk:
        return {"removed": [], "kept": {}}

    manifest = read_plugin_versions_manifest(plugin_root)
    current = manifest.get("current")
    current_version = current if isinstance(current, str) and current else None
    entries = {
        entry["version"]: entry
        for entry in (manifest.get("versions") or [])
        if isinstance(entry, dict) and isinstance(entry.get("version"), str)
    }

    def installed_at(version: str) -> str:
        """返回版本的登记时间，缺失时排到最旧，不占用保留窗口的名额。"""
        return (entries.get(version) or {}).get("installed_at") or ""

    recent_window = set(
        sorted(on_disk, key=installed_at, reverse=True)[: max(keep_recent, 0)]
    )

    kept: Dict[str, str] = {}
    for version in on_disk:
        if version == current_version:
            kept[version] = "当前安装版本"
        elif version in referenced_versions:
            kept[version] = "被实例引用（已生效版本或按跟随开关解析出的期望版本）"
        elif version in recent_window:
            kept[version] = f"保留窗口内（按登记时间的最近 {keep_recent} 个版本）"

    removed: List[str] = []
    for version in sorted(on_disk):
        if version in kept:
            continue
        if _delete_plugin_version_dir(plugin_root, version, on_disk[version]):
            removed.append(version)
        else:
            kept[version] = "本次删除失败，下次回收重试"

    if removed:
        remaining_versions = [
            entry
            for entry in (manifest.get("versions") or [])
            if isinstance(entry, dict) and entry.get("version") not in removed
        ]
        write_plugin_versions_manifest(plugin_root, remaining_versions, current_version)

    return {"removed": removed, "kept": kept}


def plugin_module_name(plugin_root: Path, source_dir: Path) -> str:
    """按插件源码目录拼出其顶层模块名。

    :param plugin_root: 插件源码根目录
    :param source_dir: 本次加载的源码目录，可能就是插件根目录（存量布局）
    :return: 可交给 importlib 的模块名
    """
    base = f"app.plugins.{plugin_root.name}"
    if source_dir == plugin_root:
        return base
    return f"{base}.{source_dir.name}"
