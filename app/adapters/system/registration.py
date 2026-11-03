"""
为外部历史登记提供可超时且不跟随最终符号链接的本地目录项证明。
"""

from pathlib import Path
from typing import Protocol, TypeVar

from app.adapters.system.fsproxy import fsproxy

EntryT = TypeVar("EntryT", covariant=True)


class LocalEntryFactory(Protocol[EntryT]):
    """
    组合根注入的有界目录项构造能力，系统适配器不依赖消费方 DTO。
    """

    def __call__(
        self, *, dev: int, ino: int, size: int, mtime_ns: int, ctime_ns: int,
        is_file: bool, is_symlink: bool, parent_dev: int, parent_ino: int,
        canonical_parent: str, name: str,
    ) -> EntryT:
        """
        从固定字段构造消费方的冻结事实对象。

        :param dev: 文件设备号
        :param ino: 文件节点号
        :param size: 文件字节数
        :param mtime_ns: 纳秒修改时间
        :param ctime_ns: 纳秒状态变化时间
        :param is_file: 是否为普通文件
        :param is_symlink: 最终目录项是否为符号链接
        :param parent_dev: 父目录设备号
        :param parent_ino: 父目录节点号
        :param canonical_parent: 仅解析父目录后的绝对地址
        :param name: 原请求最终目录项名称
        :return: 消费方定义的目录项事实
        """
        ...


def probe_local_entry(path: str, *, entry_factory: LocalEntryFactory[EntryT]) -> EntryT | None:
    """
    读取文件和真实父目录身份，保留原目录项名称供大小写别名判定。

    :param path: 原请求中的规范绝对地址
    :param entry_factory: 组合根提供的固定字段 DTO 构造器
    :return: 普通或符号链接目录项事实；仅确认不存在时返回 None
    """
    target = Path(path)
    try:
        entry = fsproxy.lstat(target)
    except FileNotFoundError:
        return None
    parent = fsproxy.realpath(target.parent)
    parent_stat = fsproxy.stat(parent)
    return entry_factory(
        dev=entry["dev"], ino=entry["ino"], size=entry["size"],
        mtime_ns=entry["mtime_ns"], ctime_ns=entry["ctime_ns"],
        is_file=entry["is_file"], is_symlink=entry["is_symlink"],
        parent_dev=parent_stat["dev"], parent_ino=parent_stat["ino"],
        canonical_parent=parent.as_posix(), name=target.name,
    )
