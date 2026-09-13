"""插件数据目录的定位与删除，按插件标识拼出的数据路径一律从这里出去。

插件数据根是配置根下的一层子目录（``CONFIG_PATH/plugins``），而插件标识在多条路径上
都来自 HTTP 请求。把标识直接拼进路径再交给文件系统，一个 ``..`` 就能把删除动作抬到
配置根上。路径拼接因此收口在本模块，调用方不再各自 ``PLUGIN_DATA_PATH / plugin_id``。
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


def plugin_data_root() -> Path:
    """
    返回插件数据根目录。
    :return: 插件数据根目录，生产环境下为 ``CONFIG_PATH/plugins``
    """
    return Path(get_runtime_setting("PLUGIN_DATA_PATH"))


def _require_single_segment(plugin_id: str) -> None:
    """
    要求插件标识恰好构成一个普通目录名，否则拒绝。

    这是纯词法判断，不碰文件系统，因而在目录尚未存在时同样成立。判据用的是「必须正好
    是一个路径段」这个正向条件，而不是「不能含 ``..``」这种反向黑名单：反向名单永远补
    不完，``..``、URL 解码后的 ``..``、``..%2f`` 解码后的 ``../``、绝对路径、Windows
    盘符各是一种写法，漏掉任意一种就是一个洞。

    POSIX 与 Windows 两套规则各判一次：宿主可能跑在任一平台上，而 ``a\\..\\..`` 在
    Linux 下只是个古怪的文件名，到了 Windows 下却是两级上跳。

    :param plugin_id: 待校验的插件标识
    :raise ValueError: 标识为空、是 ``.``/``..``、含路径分隔符、带盘符或为绝对路径
    """
    if not plugin_id or plugin_id in (".", ".."):
        raise ValueError(f"插件标识 {plugin_id!r} 不是一个合法的数据目录名")
    for flavour in (PurePosixPath, PureWindowsPath):
        pure = flavour(plugin_id)
        if pure.is_absolute() or pure.drive or pure.root:
            raise ValueError(f"插件标识 {plugin_id!r} 不得是绝对路径")
        if len(pure.parts) != 1 or pure.parts[0] != plugin_id:
            raise ValueError(f"插件标识 {plugin_id!r} 不得包含路径分隔符或上跳")


def resolve_plugin_data_directory(plugin_id: str) -> Path:
    """
    解析插件标识对应的数据目录，保证它在词法上就是数据根下的一层子目录。

    只做词法判断、不解析软链，是为了保留「把某个插件的数据目录软链到别的盘」这种既有
    部署方式：读写自有数据库文件本身不具备破坏性，把这类部署一并拒掉是纯粹的功能回退。
    需要不可逆删除的调用方另有 :func:`remove_plugin_data_directory`，那里才加上解析后的
    包含性判断。

    :param plugin_id: 插件标识
    :return: 该插件的数据目录，不保证存在
    :raise ValueError: 标识不是一个合法的单层目录名
    """
    _require_single_segment(plugin_id)
    return plugin_data_root() / plugin_id


def _require_within_data_root(data_dir: Path) -> Path:
    """
    确认目录解析后确实落在插件数据根之内，并拒绝软链。

    包含性判断建立在 ``resolve()`` 之后的真实路径上：这是唯一与编码形式无关的事实判断，
    大小写折叠、Unicode 归一、宿主平台差异造出的花样写法在这里全部塌缩成同一个答案。
    数据根本身也要解析，否则根路径上有软链时合法目录会被误判成越界。

    软链一律拒绝而不是跟随：``resolve()`` 会跟到软链的目标上，一条指向数据根之外的软链
    足以把递归删除引到任意目录；即便目标仍在根内，删掉的也不是调用方以为的那个插件。
    ``rmtree`` 本身也拒绝删除软链，这里提前拒绝只是为了给出明确原因而不是静默失败。

    :param data_dir: 待确认的目录
    :return: 解析后的真实目录
    :raise ValueError: 目录是软链，或解析后不在插件数据根之内
    """
    if data_dir.is_symlink():
        raise ValueError(f"插件数据目录 {data_dir} 是软链，拒绝按它递归删除")
    resolved_root = plugin_data_root().resolve()
    resolved = data_dir.resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise ValueError(f"插件数据目录 {data_dir} 不在插件数据根 {resolved_root} 之内")
    return resolved


def remove_plugin_data_directory(plugin_id: str) -> bool:
    """
    删除该插件在数据目录下的整个目录，返回删除前它是否存在。

    这个目录此前没有任何代码清理过：插件把落盘文件写在这里，卸载与重置都只动数据库，
    目录会一直留着，重建同名实例时静默继承上一轮的文件。

    删除失败只记日志不抛出：彻底清理是一串收尾动作，把某个文件删不掉升级成异常，只会
    让已经删掉的配置行与仍然存在的目录停在不一致的中间态，而调用方对此无从补救。

    :param plugin_id: 插件标识
    :return: 删除前该目录是否存在
    :raise ValueError: 标识越界，或目标目录解析后不在插件数据根之内
    """
    data_dir = resolve_plugin_data_directory(plugin_id)
    if not data_dir.is_dir():
        return False
    target = _require_within_data_root(data_dir)

    def _log_failure(_function: Any, path: Any, error: BaseException) -> None:
        """逐个记录删不掉的条目，避免整目录清理静默留下残余。"""
        logger.warning(f"删除插件 {plugin_id} 的数据目录条目 {path} 失败：{error}")

    shutil.rmtree(target, onexc=_log_failure)
    return True
