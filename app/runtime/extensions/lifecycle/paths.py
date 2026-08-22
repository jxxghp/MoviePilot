"""插件持久化目录的定位与实例布局迁移。

目录按 ``config/plugins/<插件ID>/<实例ID>/<用途>/`` 分段，标识在拼路径前逐段校验，
拒绝分隔符、盘符、空字符与上级目录跳出。存量的实例前布局在首次访问时原子改名迁移，
迁移结果由插件持久化根目录下的哨兵文件记录。
"""

import errno
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.foundation.paths import ensure_path_segment
from app.runtime.config import settings
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID
from app.runtime.log import logger


# 当前支持的插件持久化路径用途：data 为插件业务数据，db 为插件自管理数据库
_PLUGIN_PATH_KINDS = frozenset({"data", "db"})
# 插件实例目录布局迁移完成后写入插件持久化根目录下的哨兵文件名
_INSTANCE_LAYOUT_SENTINEL_NAME = ".instance-layout-migrated"
# 迁移过程中源目录的改名中转目录名前缀，与插件持久化根目录同级
_INSTANCE_LAYOUT_STAGING_INFIX = ".migrating-"


def plugin_instance_path(plugin_id: str, instance_id: str, kind: str) -> Path:
    """返回插件某一实例、某一用途的持久化目录，目录不存在时创建。

    ``plugin_id`` 与 ``instance_id`` 各自独立校验，任一非法都会拒绝整次调用；
    校验通过后目录固定形如 ``config/plugins/<插件ID>/<实例ID>/<kind>/``。

    :param plugin_id: 插件标识
    :param instance_id: 实例标识
    :param kind: 用途分类，取值为 "data"（插件业务数据）或 "db"（插件自管理数据库）
    :return: 对应目录的绝对路径
    :raises ValueError: 标识包含路径分隔符、盘符、空字符、指向上级目录，或 kind 不受支持
    """
    if kind not in _PLUGIN_PATH_KINDS:
        raise ValueError(f"不支持的插件路径用途：{kind!r}")
    safe_plugin_id = ensure_path_segment(plugin_id, subject="插件ID")
    safe_instance_id = ensure_path_segment(instance_id, subject="插件实例ID")

    plugin_root = settings.PLUGIN_DATA_PATH / safe_plugin_id
    if safe_instance_id == DEFAULT_INSTANCE_ID:
        target = _resolve_default_instance_dir(plugin_root, kind)
    else:
        target = plugin_root / safe_instance_id / kind

    target.mkdir(parents=True, exist_ok=True)
    return target


def _resolve_default_instance_dir(plugin_root: Path, kind: str) -> Path:
    """定位默认实例下指定用途的目录，首次访问时顺带完成一次历史数据迁移。

    :param plugin_root: 插件持久化根目录（迁移前的旧数据目录）
    :param kind: 用途分类
    :return: 迁移完成时为 ``plugin_root/default/<kind>``；迁移放弃或失败时为
        仍持有历史数据的目录（不追加 kind 层级，与迁移前的旧布局一致）
    """
    sentinel = plugin_root / _INSTANCE_LAYOUT_SENTINEL_NAME
    default_dir = plugin_root / DEFAULT_INSTANCE_ID

    if sentinel.exists():
        return default_dir / kind

    staging = _find_leftover_staging(plugin_root)
    if staging is None:
        if not plugin_root.exists() or _only_contains(plugin_root, default_dir):
            _write_instance_layout_sentinel(sentinel)
            return default_dir / kind
        staging = plugin_root.parent / (
            f"{plugin_root.name}{_INSTANCE_LAYOUT_STAGING_INFIX}{uuid.uuid4().hex}"
        )

    return _migrate_to_default_instance(plugin_root, staging, sentinel, default_dir, kind)


def _only_contains(directory: Path, only_entry: Path) -> bool:
    """判断目录是否为空，或仅含 ``only_entry`` 这一个子项。

    :param directory: 待判断的目录，调用方需确保其存在
    :param only_entry: 允许存在的唯一子项
    :return: 目录为空或仅含 only_entry 时为 True
    """
    return all(entry == only_entry for entry in directory.iterdir())


def _find_leftover_staging(plugin_root: Path) -> Optional[Path]:
    """在插件目录的同级查找上次迁移中断遗留的改名中转目录。

    :param plugin_root: 插件持久化根目录
    :return: 遗留的中转目录，不存在时为 None
    """
    parent = plugin_root.parent
    if not parent.is_dir():
        return None
    prefix = f"{plugin_root.name}{_INSTANCE_LAYOUT_STAGING_INFIX}"
    candidates = sorted(
        entry
        for entry in parent.iterdir()
        if entry.is_dir() and entry.name.startswith(prefix)
    )
    return candidates[0] if candidates else None


def _migrate_to_default_instance(
    plugin_root: Path,
    staging: Path,
    sentinel: Path,
    default_dir: Path,
    kind: str,
) -> Path:
    """执行或续做一次迁移的改名步骤，成功后写入哨兵。

    续做时按 ``staging``、``default_dir`` 是否已存在判断上次改名到了哪一步，跳过
    已完成的部分。改名的落地目标是 ``default_dir/kind`` 而非 ``default_dir`` 本身，
    使旧布局下直接位于插件目录的文件迁移后仍位于调用方按 kind 取到的目录下。

    :param plugin_root: 插件持久化根目录
    :param staging: 改名中转目录
    :param sentinel: 迁移完成后写入的哨兵文件
    :param default_dir: 默认实例目录
    :param kind: 用途分类
    :return: 改名成功时为 default_dir/kind；改名失败时为仍持有数据的目录（staging
        或 plugin_root）
    """
    kind_dir = default_dir / kind
    try:
        if not staging.exists():
            os.rename(plugin_root, staging)
        if not default_dir.exists():
            default_dir.mkdir(parents=True)
        if staging.exists():
            os.rename(staging, kind_dir)
    except OSError as error:
        if getattr(error, "errno", None) == errno.EXDEV:
            logger.warning(
                f"插件持久化目录跨设备无法原子改名，放弃本次迁移：{plugin_root} - {error}"
            )
        else:
            logger.error(f"插件持久化目录迁移失败：{plugin_root} - {error}")
        return staging if staging.exists() else plugin_root

    try:
        _write_instance_layout_sentinel(sentinel)
    except OSError as error:
        logger.warning(f"迁移哨兵写入失败，下次访问将重试：{sentinel} - {error}")
    return kind_dir


def _write_instance_layout_sentinel(sentinel: Path) -> None:
    """写入迁移完成哨兵文件，内容为完成时刻的 UTC 时间戳。

    :param sentinel: 哨兵文件路径
    """
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
