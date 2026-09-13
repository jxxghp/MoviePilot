"""插件分身创建运行时用例。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, NamedTuple, Optional

from pydantic import ValidationError

from app.schemas.plugin import PluginInstance, PluginRuntimeStatus

# 自动分配的后缀从 2 起算：源插件本体在用户眼里就是第 1 个实例，分身接着往下排
_FIRST_AUTO_SUFFIX = 2
# 探测次数上限：占用判据若因端口故障恒为真，没有上限会让请求线程原地打转并一直扣着占位锁
_MAX_AUTO_SUFFIX_PROBES = 1000


def _first_validation_message(error: ValidationError) -> str:
    """取校验错误里的首条说明，供回给用户的单行原因使用。"""
    details = error.errors()
    return str(details[0].get("msg")) if details else str(error)


class _Reservation(NamedTuple):
    """占位阶段已经定下来的事实，供后续加载与回滚判断来历。"""

    clone_id: str
    restoring: bool


class PluginCloneService:
    """创建共享源码的虚拟插件实例并协调失败回滚。"""

    def __init__(
        self,
        *,
        plugin_class: Callable[[str], Optional[Any]],
        instance_id_taken: Callable[[str], bool],
        get_instance: Callable[[str], Optional[PluginInstance]],
        source_plugin_id: Callable[[str], str],
        save_instance: Callable[[PluginInstance], Any],
        delete_instance: Callable[[str], bool],
        disable_instance: Callable[[str], bool],
        read_config: Callable[[str], dict],
        save_config: Callable[[str, dict], bool],
        delete_config: Callable[[str], bool],
        reload_plugin: Callable[[str], Any],
        remove_plugin: Callable[[str], Any],
        log: Any,
    ) -> None:
        """保存实例描述、持久化和运行态端口。"""
        self._plugin_class = plugin_class
        self._instance_id_taken = instance_id_taken
        self._get_instance = get_instance
        self._source_plugin_id = source_plugin_id
        self._save_instance = save_instance
        self._delete_instance = delete_instance
        self._disable_instance = disable_instance
        self._read_config = read_config
        self._save_config = save_config
        self._delete_config = delete_config
        self._reload_plugin = reload_plugin
        self._remove_plugin = remove_plugin
        self._logger = log
        # 「挑一个没被占用的 ID」与「把这个 ID 占下来」之间存在窗口，两个并发的创建
        # 请求会在窗口里选出同一个 ID，随后一个覆盖另一个的实例行。创建是低频的管理
        # 动作，用一把进程内互斥把占位串行化即可，代价远小于让两个分身共用一行
        self._reservation_lock = threading.Lock()

    def clone(
        self,
        *,
        plugin_id: str,
        suffix: Optional[str] = None,
        name: str,
        description: str,
        version: Optional[str] = None,
        icon: Optional[str] = None,
        restore_previous: bool = True,
    ) -> tuple[bool, str]:
        """创建虚拟分身，复制隔离配置并保持默认禁用语义。

        :param plugin_id: 源插件ID
        :param suffix: 追加到源插件ID后的分身后缀；留空时自动分配最小可用序号
        :param name: 分身展示名称，恢复时留空表示沿用停用前登记的那一份
        :param description: 分身展示描述，留空时的处理同 ``name``
        :param version: 旧客户端仍会带上；分身始终跟随源插件版本，此处不消费
        :param icon: 分身展示图标，留空时的处理同 ``name``
        :param restore_previous: 该后缀名下留有一个已停用的分身时是否沿用它的业务
            参数；为假时改按源插件模板重建一份配置
        :return: 是否成功与分身ID或失败原因
        """
        del version
        rejection = self._reject_invalid_source(plugin_id)
        if rejection:
            return False, rejection

        with self._reservation_lock:
            reservation, message = self._reserve(
                plugin_id=plugin_id,
                suffix=suffix,
                name=name,
                description=description,
                icon=icon,
                restore_previous=restore_previous,
            )
        if reservation is None:
            return False, message
        return self._activate(
            reservation,
            plugin_id=plugin_id,
            restore_previous=restore_previous,
        )

    def _reject_invalid_source(self, plugin_id: str) -> str:
        """判断给定插件能否作为分身来源，可以时返回空串。

        分身不能再生分身：分身共享源插件的源码，它自己并不是一份源码。放行的话新实例
        会挂到源插件名下，后缀却是按分身 ID 算的，自动分配因而会在一个与占用判据对不上
        的号段里挑号。
        """
        if not plugin_id:
            return "插件ID不能为空"
        if self._plugin_class(plugin_id) is None:
            return f"原插件 {plugin_id} 不存在"
        source_id = self._source_plugin_id(plugin_id)
        if source_id != plugin_id:
            return (
                f"{plugin_id} 是分身实例，不能作为分身来源，"
                f"请对源插件 {source_id} 创建分身"
            )
        return ""

    def _reserve(
        self,
        *,
        plugin_id: str,
        suffix: Optional[str],
        name: str,
        description: str,
        icon: Optional[str],
        restore_previous: bool,
    ) -> tuple[Optional[_Reservation], str]:
        """定下分身 ID 并把实例行落库，返回占位结果或拒绝原因。

        调用方须持有占位锁：本方法内部先读后写，两个并发创建各自读到「未占用」就会
        落到同一行上。
        """
        resolved_suffix = (suffix or "").strip().lower() or self._allocate_suffix(plugin_id)
        if not resolved_suffix:
            return None, f"插件 {plugin_id} 的可用分身后缀已耗尽，请手动指定一个"
        clone_id = f"{plugin_id}{resolved_suffix}"
        # 已停用的分身仍留着自己那一行，业务参数原样挂在上面：同后缀重建就是把它连同
        # 配置一起拿回来，而不是撞车。归属对不上的行不算可恢复——它是别的源插件的分身
        previous = self._get_instance(clone_id)
        restoring = (
            previous is not None
            and not previous.is_enabled
            and previous.source_plugin_id == plugin_id
        )
        if not restoring and self._instance_id_taken(clone_id):
            return None, f"分身插件 {clone_id} 已存在"

        instance, message = self._build_instance(
            clone_id=clone_id,
            source_plugin_id=plugin_id,
            name=name,
            description=description,
            icon=icon,
            previous=previous if restoring else None,
            inherit_display=restore_previous,
        )
        if instance is None:
            return None, message
        self._save_instance(instance)
        return _Reservation(clone_id=clone_id, restoring=restoring), ""

    def _allocate_suffix(self, plugin_id: str) -> str:
        """为新分身分配一个最小可用的数字后缀，无号可用时返回空串。

        占用判据与显式指定后缀走的是同一个 ``instance_id_taken``，自动分配因此不可能
        挑中一个手填时会被判成「已存在」的 ID。已停用的分身同样占位：重用它的 ID 是
        「恢复」而不是新建，用户没点恢复就不该凭空拿到上一个分身留下的配置。
        """
        for index in range(
            _FIRST_AUTO_SUFFIX,
            _FIRST_AUTO_SUFFIX + _MAX_AUTO_SUFFIX_PROBES,
        ):
            if not self._instance_id_taken(f"{plugin_id}{index}"):
                return str(index)
        return ""

    def _build_instance(
        self,
        *,
        clone_id: str,
        source_plugin_id: str,
        name: str,
        description: str,
        icon: Optional[str],
        previous: Optional[PluginInstance],
        inherit_display: bool,
    ) -> tuple[Optional[PluginInstance], str]:
        """构造实例描述，并在任何写入之前拦下不合法的实例 ID。

        合法性由 ``PluginInstance`` 自己判定，这里不另抄一份规则；构造排在落库之前，
        非法 ID 因而不会先写出一行再靠回滚擦掉。

        :param previous: 正在被恢复的那一行，非恢复时为 None
        :param inherit_display: 恢复时展示信息是否沿用停用前登记的那一份
        """
        if previous is not None and inherit_display:
            name = name or (previous.plugin_name or "")
            description = description or (previous.plugin_desc or "")
            icon = icon or (previous.plugin_icon or "")
        try:
            instance = PluginInstance(
                instance_id=clone_id,
                source_plugin_id=source_plugin_id,
                plugin_name=name or None,
                plugin_desc=description or None,
                plugin_icon=icon or None,
                # 恢复时保留该行原有的默认目标置位；新建的分身不该凭空成为默认目标
                is_default_target=(
                    previous.is_default_target if previous is not None else False
                ),
                # 新建的分身就是要拿去跑的；启用位是装载判据，留空会让它建出来却不加载
                is_enabled=True,
            )
        except ValidationError as error:
            return None, f"分身实例 ID {clone_id} 不合法：{_first_validation_message(error)}"
        return instance, ""

    def _activate(
        self,
        reservation: _Reservation,
        *,
        plugin_id: str,
        restore_previous: bool,
    ) -> tuple[bool, str]:
        """准备配置并完成首次加载，失败时按来历回滚。"""
        clone_id = reservation.clone_id
        # 恢复留存配置时不得用源插件模板盖掉它，那正是用户要拿回来的东西
        keep_previous_config = reservation.restoring and restore_previous
        try:
            if not keep_previous_config:
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
            action = "恢复" if reservation.restoring else "创建"
            self._logger.info(f"插件分身 {clone_id} {action}成功")
            return True, clone_id
        except Exception as error:  # noqa: BLE001
            self._rollback(clone_id, purge_instance=not reservation.restoring)
            self._logger.error(f"创建插件分身失败：{error}")
            return False, f"创建插件分身失败：{error}"

    def _rollback(self, clone_id: str, *, purge_instance: bool) -> None:
        """逐项清理失败实例，单个清理错误不得阻断其余回滚。

        :param clone_id: 本次创建的分身ID
        :param purge_instance: 实例行与配置是否由本次创建产生、可随之抹掉；恢复一个
            已停用的分身时为假——那一行连同用户留在上面的业务参数是特意保留的，一次
            加载失败不该把它毁掉，只需把启用位退回停用，下次仍可再试一遍恢复
        """
        rollback_steps: list[tuple[str, Callable[[str], Any]]] = [
            ("运行态", self._remove_plugin),
        ]
        if purge_instance:
            rollback_steps.append(("实例描述", self._delete_instance))
            rollback_steps.append(("配置", self._delete_config))
        else:
            rollback_steps.append(("启用位", self._disable_instance))
        for label, rollback in rollback_steps:
            try:
                rollback(clone_id)
            except Exception as rollback_error:  # noqa: BLE001
                self._logger.warning(
                    f"回滚插件分身 {clone_id} 的{label}失败：{rollback_error}"
                )
