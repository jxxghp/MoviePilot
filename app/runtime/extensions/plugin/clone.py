"""插件分身创建运行时用例。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Optional

from app.schemas.plugin import PluginInstance, PluginRuntimeStatus


class PluginCloneService:
    """创建共享源码的虚拟插件实例并协调失败回滚。"""

    def __init__(
        self,
        *,
        plugin_class: Callable[[str], Optional[Any]],
        plugin_exists: Callable[[str], bool],
        get_instance: Callable[[str], Optional[PluginInstance]],
        get_any_instance: Callable[[str], Optional[PluginInstance]],
        all_instances_for_source: Callable[[str], Sequence[PluginInstance]],
        disable_instance: Callable[[str], bool],
        source_plugin_id: Callable[[str], str],
        installed_versions: Callable[[str], Sequence[str]],
        save_instance: Callable[[PluginInstance], Any],
        purge_instance: Callable[[str], bool],
        read_config: Callable[[str], dict],
        save_config: Callable[[str, dict], bool],
        delete_data: Callable[[str], Any],
        has_data: Callable[[str], bool],
        reload_plugin: Callable[[str], Any],
        remove_plugin: Callable[[str], Any],
        log: Any,
    ) -> None:
        """保存实例描述、持久化和运行态端口。"""
        self._plugin_class = plugin_class
        self._plugin_exists = plugin_exists
        self._get_instance = get_instance
        self._get_any_instance = get_any_instance
        self._all_instances_for_source = all_instances_for_source
        self._disable_instance = disable_instance
        self._source_plugin_id = source_plugin_id
        self._installed_versions = installed_versions
        self._save_instance = save_instance
        self._purge_instance = purge_instance
        self._read_config = read_config
        self._save_config = save_config
        self._delete_data = delete_data
        self._has_data = has_data
        self._reload_plugin = reload_plugin
        self._remove_plugin = remove_plugin
        self._logger = log

    def clone(
        self,
        *,
        plugin_id: str,
        suffix: Optional[str] = None,
        name: str,
        description: str,
        version: Optional[str] = None,
        icon: Optional[str] = None,
        pinned_version: Optional[str] = None,
        restore_previous: bool = True,
    ) -> tuple[bool, str]:
        """创建虚拟分身，复制隔离配置并保持默认禁用语义。

        :param plugin_id: 源插件ID
        :param suffix: 追加到源插件ID后的分身后缀；留空时自动分配最小可用序号
        :param name: 分身展示名称
        :param description: 分身展示描述
        :param version: 旧客户端保留字段，不参与版本绑定
        :param icon: 分身展示图标
        :param pinned_version: 分身锚定的版本号；为空表示跟随源插件当前版本
        :param restore_previous: 该实例 ID 名下留有上一轮的业务数据时是否沿用；
            为假时先清空这些数据，再按源插件模板重建
        :return: 是否成功与分身ID或失败原因
        """
        if not plugin_id:
            return False, "插件ID不能为空"
        if self._plugin_class(plugin_id) is None:
            return False, f"原插件 {plugin_id} 不存在"
        resolved_source = self._source_plugin_id(plugin_id)
        if resolved_source != plugin_id:
            return False, (
                f"{plugin_id} 是分身实例，不能作为分身来源，"
                f"请对源插件 {resolved_source} 创建分身"
            )

        # 后缀只用于区分实例，用户对它无感；不给就自动分配一个最小可用序号
        resolved_suffix = (suffix or "").strip().lower() or self._allocate_suffix(plugin_id)
        clone_id = f"{plugin_id}{resolved_suffix}"
        # 判存不能只看运行态：catalog.exists 要求实例已在运行，源插件本次加载失败时
        # 已有的同名分身会被判成「不存在」而放行，随后覆盖它的描述符，再在回滚里把
        # 它连同配置一起删掉。已登记的类与已落盘的描述符都要算作已占用。
        # 已卸载的分身仍留着自己那一行：同后缀重建就是把它连同配置一起拿回来，
        # 而不是撞车。默认沿用留存的设置，显式要求全新时才先清空。
        restorable = self._get_any_instance(clone_id)
        restoring = restorable is not None and not restorable.is_enabled
        if not restoring and (
            self._plugin_exists(clone_id)
            or self._plugin_class(clone_id) is not None
            or restorable is not None
        ):
            return False, f"分身插件 {clone_id} 已存在"

        if pinned_version:
            # 版本目录必须先在磁盘上：分身与源插件共享源码，锚定一个没装的版本
            # 会建出指向空目录的实例，之后每次启动都必然失败。
            installed = tuple(self._installed_versions(resolved_source))
            if pinned_version not in installed:
                available = "、".join(installed) if installed else "无"
                return False, (
                    f"插件 {resolved_source} 尚未安装版本 {pinned_version}，"
                    f"已装版本：{available}"
                )

        # 配置随实例行存续，行还在就走不到这里，因此这里能撞上的只有被彻底清理掉
        # 实例后仍留在磁盘与插件数据表里的业务数据。默认沿用它，显式要求全新时才
        # 清空——静默丢弃用户数据不能是默认行为。
        inherited_data = bool(self._has_data(clone_id))
        if inherited_data and not restore_previous:
            self._delete_data(clone_id)
            inherited_data = False
        # 恢复时用户可以改名改版本；没填就沿用卸载前登记的那一份
        if restoring and restore_previous and restorable is not None:
            name = name or (restorable.plugin_name or "")
            description = description or (restorable.plugin_desc or "")
            icon = icon or (restorable.plugin_icon or "")
            pinned_version = pinned_version or restorable.pinned_version

        try:
            instance = PluginInstance(
                instance_id=clone_id,
                source_plugin_id=self._source_plugin_id(plugin_id),
                plugin_name=name or None,
                plugin_desc=description or None,
                plugin_icon=icon or None,
                pinned_version=pinned_version or None,
                # 用户刚明确要这个实例，建出即应当被实例化并启动
                is_enabled=True,
            )
            self._save_instance(instance)

            # 恢复留存配置时不得用源插件模板盖掉它，那正是用户要拿回来的东西
            if not (restoring and restore_previous):
                original_config = self._read_config(plugin_id)
                if original_config:
                    clone_config = dict(original_config)
                    clone_config["enable"] = False
                    clone_config["enabled"] = False
                    if not self._save_config(clone_id, clone_config):
                        raise RuntimeError("虚拟实例配置保存失败")

            status = self._reload_plugin(clone_id)
            if status is PluginRuntimeStatus.LOAD_FAILED:
                raise RuntimeError("虚拟实例加载失败")
            # 认证等级不足时实例不会进入运行态，只回报「创建成功」会留下一个
            # 永远不工作、用户也看不出原因的分身。
            if status is PluginRuntimeStatus.BLOCKED_BY_POLICY:
                raise RuntimeError("虚拟实例被访问策略拒绝，未能启动")
            self._logger.info(f"插件分身 {clone_id} 创建成功")
            return True, clone_id
        except Exception as error:  # noqa: BLE001
            self._rollback(
                clone_id,
                purge_data=not inherited_data,
                purge_instance=not restoring,
            )
            self._logger.error(f"创建插件分身失败：{error}")
            return False, f"创建插件分身失败：{error}"

    def _allocate_suffix(self, plugin_id: str) -> str:
        """为新分身分配一个最小可用的数字后缀。

        从 2 起算：源插件本体在用户眼里就是第 1 个实例，分身接着往下排。已卸载的
        分身同样占位——它那一行还留着，重用它的 ID 会变成「恢复」而不是新建。
        """
        taken = {
            instance.instance_id.casefold()
            for instance in self._all_instances_for_source(plugin_id)
        }
        index = 2
        while f"{plugin_id}{index}".casefold() in taken:
            index += 1
        return str(index)

    def _rollback(self, clone_id: str, *, purge_data: bool, purge_instance: bool = True) -> None:
        """逐项清理失败实例，单个清理错误不得阻断其余回滚。

        本次创建已被判存挡在前面，走到这里的 ``clone_id`` 一定是本次新建的，因此
        实例行与配置都是本次的产物，一律连行抹掉——只把启用位置假会凭空留下一个
        「停用待启用」的实例，而它从来没成功存在过。

        业务数据则要分辨来历：``_ensure_database`` 在实例首次启动时就可能建出库
        文件，本次自己建的留着会让下次重建挂到旧库旧数据上；而沿用上一轮残留数据
        时它并非本次产物，清掉等于一次加载失败就毁掉用户特意保留的数据。

        :param clone_id: 本次创建的分身ID
        :param purge_data: 业务数据是否由本次创建产生、可随之销毁
        :param purge_instance: 实例行是否由本次创建产生、可随之抹掉；恢复已卸载的
            分身时为假——那一行是用户特意留着的，一次加载失败不该把它毁掉
        """
        rollback_steps = [("运行态", self._remove_plugin)]
        if purge_instance:
            rollback_steps.append(("实例记录与配置", self._purge_instance))
        else:
            rollback_steps.append(("启用位", self._disable_instance))
        if purge_data:
            rollback_steps.append(("业务数据与自有库", self._delete_data))
        for label, rollback in rollback_steps:
            try:
                rollback(clone_id)
            except Exception as rollback_error:  # noqa: BLE001
                self._logger.warning(
                    f"回滚插件分身 {clone_id} 的{label}失败：{rollback_error}"
                )
