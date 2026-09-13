"""插件分身创建运行时用例。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Optional

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


class PluginCloneService:
    """创建共享源码的虚拟插件实例并协调失败回滚。"""

    def __init__(
        self,
        *,
        plugin_class: Callable[[str], Optional[Any]],
        instance_id_taken: Callable[[str], bool],
        source_plugin_id: Callable[[str], str],
        save_instance: Callable[[PluginInstance], Any],
        delete_instance: Callable[[str], bool],
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
        self._source_plugin_id = source_plugin_id
        self._save_instance = save_instance
        self._delete_instance = delete_instance
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
    ) -> tuple[bool, str]:
        """创建虚拟分身，复制隔离配置并保持默认禁用语义。

        :param plugin_id: 源插件ID
        :param suffix: 追加到源插件ID后的分身后缀；留空时自动分配最小可用序号
        :param name: 分身展示名称
        :param description: 分身展示描述
        :param version: 旧客户端仍会带上；分身始终跟随源插件版本，此处不消费
        :param icon: 分身展示图标
        :return: 是否成功与分身ID或失败原因
        """
        del version
        rejection = self._reject_invalid_source(plugin_id)
        if rejection:
            return False, rejection

        with self._reservation_lock:
            clone_id, message = self._reserve(
                plugin_id=plugin_id,
                suffix=suffix,
                name=name,
                description=description,
                icon=icon,
            )
        if clone_id is None:
            return False, message
        return self._activate(clone_id, plugin_id=plugin_id)

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
    ) -> tuple[Optional[str], str]:
        """定下分身 ID 并把实例行落库，返回分身ID或拒绝原因。

        调用方须持有占位锁：本方法内部先读后写，两个并发创建各自读到「未占用」就会
        落到同一行上。
        """
        resolved_suffix = (suffix or "").strip().lower() or self._allocate_suffix(plugin_id)
        if not resolved_suffix:
            return None, f"插件 {plugin_id} 的可用分身后缀已耗尽，请手动指定一个"
        clone_id = f"{plugin_id}{resolved_suffix}"
        if self._instance_id_taken(clone_id):
            return None, f"分身插件 {clone_id} 已存在"

        instance, message = self._build_instance(
            clone_id=clone_id,
            source_plugin_id=plugin_id,
            name=name,
            description=description,
            icon=icon,
        )
        if instance is None:
            return None, message
        self._save_instance(instance)
        return clone_id, ""

    def _allocate_suffix(self, plugin_id: str) -> str:
        """为新分身分配一个最小可用的数字后缀，无号可用时返回空串。

        占用判据与显式指定后缀走的是同一个 ``instance_id_taken``，自动分配因此不可能
        挑中一个手填时会被判成「已存在」的 ID。
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
    ) -> tuple[Optional[PluginInstance], str]:
        """构造实例描述，并在任何写入之前拦下不合法的实例 ID。

        合法性由 ``PluginInstance`` 自己判定，这里不另抄一份规则；构造排在落库之前，
        非法 ID 因而不会先写出一行再靠回滚擦掉。
        """
        try:
            instance = PluginInstance(
                instance_id=clone_id,
                source_plugin_id=source_plugin_id,
                plugin_name=name or None,
                plugin_desc=description or None,
                plugin_icon=icon or None,
                # 新建的分身就是要拿去跑的；启用位是装载判据，留空会让它建出来却不加载
                is_enabled=True,
            )
        except ValidationError as error:
            return None, f"分身实例 ID {clone_id} 不合法：{_first_validation_message(error)}"
        return instance, ""

    def _activate(self, clone_id: str, *, plugin_id: str) -> tuple[bool, str]:
        """准备配置并完成首次加载，失败时回滚本次创建的全部产物。"""
        try:
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
            self._logger.info(f"插件分身 {clone_id} 创建成功")
            return True, clone_id
        except Exception as error:  # noqa: BLE001
            self._rollback(clone_id)
            self._logger.error(f"创建插件分身失败：{error}")
            return False, f"创建插件分身失败：{error}"

    def _rollback(self, clone_id: str) -> None:
        """逐项清理失败实例，单个清理错误不得阻断其余回滚。"""
        rollback_steps: list[tuple[str, Callable[[str], Any]]] = [
            ("运行态", self._remove_plugin),
            ("实例描述", self._delete_instance),
            ("配置", self._delete_config),
        ]
        for label, rollback in rollback_steps:
            try:
                rollback(clone_id)
            except Exception as rollback_error:  # noqa: BLE001
                self._logger.warning(
                    f"回滚插件分身 {clone_id} 的{label}失败：{rollback_error}"
                )
