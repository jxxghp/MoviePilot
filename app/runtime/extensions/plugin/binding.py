"""插件已装版本查询与虚拟实例版本绑定切换。"""

from __future__ import annotations

from collections.abc import Callable
from functools import cmp_to_key
from pathlib import Path
from typing import Any, Optional

from app.foundation.version import compare_version
from app.runtime.extensions.plugin.version import (
    plugin_version_dirs,
    read_plugin_versions_manifest,
    recycle_plugin_version_directories,
)
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus

StartInstance = Callable[[str, Optional[str]], dict[str, PluginRuntimeStatus]]
StopInstance = Callable[[str], Optional[bool]]
MultiVersionBlockers = Callable[[str, list[Path]], list[str]]
RefreshRegistrations = Callable[[str], None]
PendingInstallation = Callable[[str], bool]


def _ignore_registration_refresh(_instance_id: str) -> None:
    """在宿主尚未装配注册刷新端口时保持版本绑定可用。"""


def _no_display_name(_instance_id: str) -> Optional[str]:
    """在宿主尚未装配名称解析端口时回落到实例自身登记的名称。"""
    return None


def _compare_versions(left: str, right: str) -> int:
    """按语义比较两个版本号，供版本列表排序使用。"""
    if compare_version(left, ">", right):
        return 1
    if compare_version(right, ">", left):
        return -1
    return 0


class PluginVersionBinding:
    """组装插件版本总览，并执行实例级版本绑定切换。"""

    def __init__(
        self,
        *,
        plugins_root: Path,
        plugin_exists: Callable[[str], bool],
        get_instance: Callable[[str], Optional[PluginInstance]],
        instances_for_source: Callable[[str], list[PluginInstance]],
        all_instances_for_source: Callable[[str], list[PluginInstance]],
        save_instance: Callable[[PluginInstance], None],
        get_host_instance: Callable[[str], Optional[PluginInstance]],
        save_host_instance: Callable[[PluginInstance], None],
        running: Callable[[], dict[str, Any]],
        start: StartInstance,
        stop: StopInstance,
        multi_version_blockers: MultiVersionBlockers,
        log: Any,
        display_name: Callable[[str], Optional[str]] | None = None,
        refresh_registrations: RefreshRegistrations | None = None,
        pending_installation: PendingInstallation | None = None,
    ) -> None:
        """保存版本目录、实例持久化、生命周期和安装事务查询端口。"""
        self._plugins_root = plugins_root
        self._plugin_exists = plugin_exists
        self._get_instance = get_instance
        self._instances_for_source = instances_for_source
        self._all_instances_for_source = all_instances_for_source
        self._save_instance = save_instance
        self._get_host_instance = get_host_instance
        self._save_host_instance = save_host_instance
        self._running = running
        self._start = start
        self._stop = stop
        self._multi_version_blockers = multi_version_blockers
        self._logger = log
        self._display_name = display_name or _no_display_name
        self._refresh_registrations = refresh_registrations or _ignore_registration_refresh
        self._pending_installation = pending_installation or (lambda _plugin_id: False)

    def _plugin_root(self, plugin_id: str) -> Path:
        """定位插件源码根目录。"""
        return self._plugins_root / plugin_id.lower()

    def _current_version(self, plugin_id: str) -> Optional[str]:
        """读取版本元信息登记的当前版本号。"""
        manifest = read_plugin_versions_manifest(self._plugin_root(plugin_id))
        current = manifest.get("current")
        return current if isinstance(current, str) and current else None

    @staticmethod
    def _default_host_instance(plugin_id: str) -> PluginInstance:
        """本体从未被显式绑定过版本时的默认视图：跟随当前版本，未登记已生效版本。"""
        return PluginInstance(
            instance_id=plugin_id,
            source_plugin_id=plugin_id,
        )

    def _host_instance(self, plugin_id: str) -> PluginInstance:
        """读取源插件本体的版本绑定，从未绑定过时给出跟随当前版本的默认视图。"""
        return self._get_host_instance(plugin_id) or self._default_host_instance(plugin_id)

    def overview(self, plugin_id: str) -> dict[str, Any]:
        """组装插件已装版本列表、源插件本体与各分身实例的版本绑定。

        实例列表首项固定是本体自身的版本绑定，其余是引用该源码的各分身实例；
        每项都带 ``is_host`` 标记二者身份，本体从未被显式绑定过版本时按跟随
        当前版本的默认视图呈现，而不是从列表中略去。

        :param plugin_id: 插件ID
        :return: 含已装版本列表与本体、各分身实例绑定信息的字典
        :raise LookupError: 插件不存在，或 ``plugin_id`` 实为某个分身自身的实例 ID
        """
        if not self._plugin_exists(plugin_id):
            raise LookupError(f"插件 {plugin_id} 不存在")
        if self._get_instance(plugin_id) is not None:
            raise LookupError(f"{plugin_id} 是分身实例，请使用源插件 ID 查询版本与实例")
        plugin_root = self._plugin_root(plugin_id)
        manifest = read_plugin_versions_manifest(plugin_root)
        current_version = self._current_version(plugin_id)
        registered = {
            entry.get("version"): entry
            for entry in (manifest.get("versions") or [])
            if isinstance(entry, dict)
        }
        installed_versions = [
            {
                "version": version,
                "directory": path.name,
                "installed_at": (registered.get(version) or {}).get("installed_at"),
                "source": (registered.get(version) or {}).get("source"),
                "is_current": version == current_version,
            }
            # 按版本号语义排序：字典序会把 1.10.0 排在 1.9.0 前面，与响应模型
            # 声明的「按版本号升序」以及加载器解析当前版本时用的比较口径都不一致。
            for version, path in sorted(
                plugin_version_dirs(plugin_root).items(),
                key=lambda item: cmp_to_key(_compare_versions)(item[0]),
            )
        ]
        host_instance = self._host_instance(plugin_id)

        def binding_view(instance: PluginInstance) -> dict[str, Any]:
            """把持久化绑定和运行中实例的实际版本合并为接口投影。"""
            running_instance = self._running_instance(instance.instance_id)
            running_version = (
                getattr(running_instance, "plugin_version", None)
                if running_instance is not None
                else None
            )
            return {
                "instance_id": instance.instance_id,
                # 优先取运行态注册的展示名：分身的名称在加载时才被写进类上，本体
                # 的名称则只存在于插件类，实例描述符里根本没有。加载失败的分身取
                # 不到类，回落到描述符里持久化的名称，而不是退成一行裸 ID。
                "plugin_name": (
                    self._display_name(instance.instance_id) or instance.plugin_name
                ),
                "pinned_version": instance.pinned_version,
                "running": running_instance is not None,
                "running_version": (
                    running_version if isinstance(running_version, str) else None
                ),
                "is_host": instance.is_host,
                "is_default_target": instance.is_default_target,
                # 与 running 是两回事：这里说的是「该不该被实例化」，running 说的是
                # 「此刻在不在跑」。停用的实例仍要出现在列表里，只是开关是关的。
                "is_enabled": instance.is_enabled,
            }

        instances = [
            binding_view(host_instance),
            *(binding_view(instance) for instance in self._instances_for_source(plugin_id)),
        ]
        return {
            "plugin_id": plugin_id,
            "current_version": current_version,
            "installed_versions": installed_versions,
            "instances": instances,
        }

    def _running_instance(self, instance_id: str) -> Any | None:
        """按实例 ID 读取当前运行对象，兼容大小写不同的注册表键。"""
        running = self._running()
        instance = running.get(instance_id)
        if instance is not None:
            return instance
        target = instance_id.casefold()
        return next(
            (value for key, value in running.items() if key.casefold() == target),
            None,
        )

    def _instance_expected_version(
        self,
        instance: PluginInstance,
        current_version: Optional[str],
    ) -> Optional[str]:
        """解析实例按其绑定本应运行的版本，供并存判定使用。"""
        return instance.pinned_version or current_version

    def _creates_version_coexistence(
        self,
        instance: PluginInstance,
        target_version: str,
    ) -> bool:
        """判断把指定实例切到目标版本后，该插件是否会出现多版本同时在跑。

        本体的期望版本同样并入候选集合：本体从未被显式绑定过版本时按跟随当前
        版本的默认视图解析，取值与旧实现里硬编码的当前版本种子等价；本体已被
        钉住某个版本时改按该绑定解析，不再想当然地假设本体始终运行当前版本。
        被切换的正是本体自身时跳过这一项，因为切换后本体将处于 ``target_version``，
        计入切换前的期望版本会把自己和自己比较出一次假並存。
        """
        current_version = self._current_version(instance.source_plugin_id)
        versions: set[str] = set()
        if instance.instance_id != instance.source_plugin_id:
            host_expected = self._instance_expected_version(
                self._host_instance(instance.source_plugin_id), current_version
            )
            if host_expected:
                versions.add(host_expected)
        for sibling in self._instances_for_source(instance.source_plugin_id):
            if sibling.instance_id == instance.instance_id:
                continue
            sibling_version = self._instance_expected_version(sibling, current_version)
            if sibling_version:
                versions.add(sibling_version)
        versions.add(target_version)
        return len(versions) > 1

    def set_instance_version(
        self,
        instance_id: str,
        *,
        pinned_version: Optional[str] = None,
    ) -> tuple[bool, str]:
        """设置实例的版本绑定，并立即完成一次停止再启动。

        不跟随当前版本时校验目标版本已安装；如本次切换会让该插件的多个实例
        分处不同版本，先跑多版本并存静态扫描，命中阻断原因即拒绝切换、不做
        任何改动。切换走停止再启动的完整生命周期，不做热替换：热替换等于在
        运行期换掉一个已注册事件、已起定时任务、可能有在途请求的实例。目标
        版本启动失败时已生效版本保持不动，以该版本重新启动完成回退；回退同样
        失败才判定本次切换失败，失败过程全程记录明确日志。

        本体与分身共用这一入口：``instance_id`` 等于某个源插件 ID 且该插件确实
        存在时，按本体的版本绑定解析（从未绑定过时给出跟随当前版本的默认视图），
        写回时据此路由到本体或分身各自的持久化端口。

        :param instance_id: 实例ID，可以是分身实例 ID，也可以是源插件本体自身 ID
        :param pinned_version: 锚定的目标版本号；为空表示改为跟随当前版本
        :return: `(是否成功, 成功时为实例ID／失败时为可读原因)`
        """
        instance = self._get_instance(instance_id)
        if instance is None and self._plugin_exists(instance_id):
            instance = self._host_instance(instance_id)
        if instance is None:
            return False, f"插件实例 {instance_id} 不存在"

        try:
            if self._pending_installation(instance.source_plugin_id):
                return False, (
                    f"插件 {instance.source_plugin_id} 正在安装或写入，"
                    "拒绝切换版本"
                )
        except Exception as error:  # noqa: BLE001 - 安装状态未知时失败关闭
            self._logger.error(
                f"检查插件 {instance.source_plugin_id} 的安装事务失败：{error}"
            )
            return False, f"无法确认插件 {instance.source_plugin_id} 的安装状态"

        target_version: Optional[str] = None
        plugin_root = self._plugin_root(instance.source_plugin_id)
        installed = plugin_version_dirs(plugin_root)
        requested_version = (pinned_version or "").strip() or None
        if requested_version is not None:
            target_version = requested_version
            if target_version not in installed:
                return False, f"插件 {instance.source_plugin_id} 未安装版本 {target_version}"
        else:
            # 跟随当前版本同样可能把实例从旧版本切到当前版本，不能绕过多版本
            # 并存守卫。启动时仍传 None，让加载器按绑定记录解析当前版本。
            target_version = self._current_version(instance.source_plugin_id)
            if target_version is None and installed:
                target_version = max(
                    installed,
                    key=cmp_to_key(_compare_versions),
                )

        if target_version and self._creates_version_coexistence(instance, target_version):
            blockers = self._multi_version_blockers(
                instance.source_plugin_id.lower(), list(installed.values())
            )
            if blockers:
                return False, (
                    f"插件 {instance.source_plugin_id} 的写法不支持多版本并存，"
                    "拒绝切换：" + "；".join(blockers)
                )

        previous_running = self._running_instance(instance_id)
        previous_version_value = (
            getattr(previous_running, "plugin_version", None)
            if previous_running is not None
            else None
        )
        previous_version = (
            previous_version_value
            if isinstance(previous_version_value, str) and previous_version_value
            else None
        )

        updated_instance = instance.model_copy(
            # 单列即绑定：为空即跟随当前版本，非空即锚定。本体加载走
            # ``loader.load``，该路径不接收 start 的 version 形参，源码目录只由
            # 这条绑定记录解析，所以锚定值必须落盘。
            update={"pinned_version": requested_version}
        )
        try:
            self._persist_binding(updated_instance)
        except Exception as error:  # noqa: BLE001 - 持久化失败不得进入停启流程
            self._restore_binding(instance, instance_id)
            self._logger.error(f"插件实例 {instance_id} 保存版本绑定失败：{error}")
            return False, f"保存插件实例 {instance_id} 的版本绑定失败"

        try:
            stop_result = self._stop(instance_id)
            if stop_result is False:
                self._restore_binding(instance, instance_id)
                self._logger.error(f"插件实例 {instance_id} 停止未收敛")
                return False, f"切换插件实例 {instance_id} 失败：停止旧实例失败"
        except Exception as error:  # noqa: BLE001 - 停止失败时恢复旧绑定
            self._restore_binding(instance, instance_id)
            self._logger.error(f"插件实例 {instance_id} 停止失败：{error}")
            return False, f"切换插件实例 {instance_id} 失败：停止旧实例失败"

        try:
            results = self._start(
                instance_id,
                requested_version,
            )
        except Exception as error:  # noqa: BLE001 - 统一按加载失败执行补偿
            results = {}
            self._logger.error(f"插件实例 {instance_id} 启动目标版本失败：{error}")

        status = self._result_status(results, instance_id)
        mismatch = (
            self._effective_version_mismatch(instance_id, target_version)
            if status == PluginRuntimeStatus.ACTIVE
            else None
        )
        if status == PluginRuntimeStatus.ACTIVE and mismatch is None:
            if not self._refresh_registrations_safely(instance_id):
                running_version = self._running_version(instance_id)
                return False, (
                    f"版本切换完成但插件实例 {instance_id} 注册刷新失败，"
                    f"当前实际版本为 {running_version or '未知'}"
                )
            return True, instance_id
        if mismatch is not None:
            # 加载器在绑定目录消失等情况下可能回落到当前版本并仍返回 ACTIVE。
            # 先清掉这个错误实例，再恢复绑定并尝试启动切换前实际运行的版本。
            self._logger.error(
                f"插件实例 {instance_id} 请求切换到版本 {target_version}，"
                f"实际加载的是 {mismatch}"
            )
            if not self._stop_failed_runtime(instance_id, previous_running):
                self._restore_binding(instance, instance_id)
                self._refresh_registrations_safely(instance_id)
                return False, f"切换到版本 {target_version} 失败，清理错误运行实例失败"
        elif status != PluginRuntimeStatus.ACTIVE:
            if not self._stop_failed_runtime(instance_id, previous_running):
                self._restore_binding(instance, instance_id)
                self._refresh_registrations_safely(instance_id)
                return False, f"切换到版本 {target_version} 失败，清理错误运行实例失败"

        return self._recover_previous_version(
            instance,
            instance_id,
            target_version,
            previous_version,
        )

    @staticmethod
    def _result_status(
        results: dict[str, PluginRuntimeStatus], instance_id: str
    ) -> Optional[PluginRuntimeStatus]:
        """按大小写不敏感实例 ID 读取生命周期结果。"""
        if not isinstance(results, dict):
            return None
        target = instance_id.casefold()
        return next(
            (status for key, status in results.items() if key.casefold() == target),
            None,
        )

    def _restore_binding(self, instance: PluginInstance, instance_id: str) -> bool:
        """恢复切换前绑定，并把补偿失败转成日志和布尔结果。"""
        try:
            self._persist_binding(instance)
            return True
        except Exception as error:  # noqa: BLE001 - 保留原始失败上下文
            self._logger.error(f"插件实例 {instance_id} 恢复原版本绑定失败：{error}")
            return False

    def _stop_failed_runtime(self, instance_id: str, previous_running: Any | None) -> bool:
        """清理目标启动留下的运行对象，避免错误版本继续占用路由和资源。"""
        current = self._running_instance(instance_id)
        if current is previous_running and current is not None:
            return True
        try:
            # 即使生命周期没有把失败实例放进 running，stop 仍负责清掉失败导入
            # 留下的模块缓存、分类和宿主资源；否则本体回退时 loader 可能复用坏版本
            # 的缓存模块，导致一次失败把可用的旧版本也拖死。
            result = self._stop(instance_id)
            return result is not False
        except Exception as error:  # noqa: BLE001 - 清理失败仍需返回主错误
            self._logger.error(f"插件实例 {instance_id} 清理错误版本失败：{error}")
            return False

    def _refresh_registrations_safely(self, instance_id: str) -> bool:
        """在版本切换完成后刷新该实例的宿主注册投影。"""
        try:
            self._refresh_registrations(instance_id)
            return True
        except Exception as error:  # noqa: BLE001 - 注册刷新不得改变已完成切换
            self._logger.warning(f"插件实例 {instance_id} 注册刷新失败：{error}")
            return False

    def _running_version(self, instance_id: str) -> Optional[str]:
        """读取实例当前运行对象的版本，用于失败结果提示。"""
        running = self._running_instance(instance_id)
        version = getattr(running, "plugin_version", None) if running is not None else None
        return version if isinstance(version, str) and version else None

    def _recover_previous_version(
        self,
        instance: PluginInstance,
        instance_id: str,
        target_version: Optional[str],
        previous_version: Optional[str],
    ) -> tuple[bool, str]:
        """恢复切换前实际运行版本，并在恢复失败时清理残留实例。"""
        if not previous_version or previous_version == target_version:
            self._restore_binding(instance, instance_id)
            self._logger.error(
                f"插件实例 {instance_id} 切换到版本 {target_version} 失败，"
                "没有可回退的已生效运行版本"
            )
            # 目标启动失败后通常没有运行对象；即使目标加载器已经写入了部分
            # 运行态，也必须让宿主重建注册投影，撤销仍指向旧实例的 API、任务和
            # 命令。刷新失败只记录，主切换结果仍按生命周期失败返回。
            self._refresh_registrations_safely(instance_id)
            if target_version is None:
                return False, "切换为跟随当前版本失败，请查看插件日志"
            return False, f"切换到版本 {target_version} 失败，请查看插件日志"

        self._logger.error(
            f"插件实例 {instance_id} 切换到版本 {target_version} 失败，"
            f"已生效版本 {previous_version} 保持不变，正在以该版本重新启动"
        )
        # 本体的 loader.load 只按持久化绑定解析源码，不能依赖 start 的 version
        # 形参。先暂时钉住实际旧版本，等回退实例真正启动后再恢复原来的绑定语义。
        fallback_binding = instance.model_copy(
            update={"pinned_version": previous_version}
        )
        if not self._restore_binding(fallback_binding, instance_id):
            self._restore_binding(instance, instance_id)
            self._refresh_registrations_safely(instance_id)
            return False, f"切换到版本 {target_version} 失败，恢复原版本绑定失败"
        try:
            # 回退成功后要恢复切换前的 follow/current 或 pinned 语义。绑定恢复不能
            # 提前做，否则本体会按新 current 版本解析，忽略这里传入的旧版本。
            fallback_results = self._start(instance_id, previous_version)
        except Exception as error:  # noqa: BLE001 - 回退失败必须可读返回
            fallback_results = {}
            self._logger.error(f"插件实例 {instance_id} 回退启动失败：{error}")
        fallback_status = self._result_status(fallback_results, instance_id)
        fallback_mismatch = self._effective_version_mismatch(instance_id, previous_version)
        if fallback_status == PluginRuntimeStatus.ACTIVE and fallback_mismatch is None:
            refresh_ok = self._refresh_registrations_safely(instance_id)
            binding_restored = self._restore_binding(instance, instance_id)
            if not binding_restored:
                self._refresh_registrations_safely(instance_id)
                return False, (
                    f"切换到版本 {target_version} 失败，已回退到原版本 {previous_version}，"
                    "但恢复原版本绑定失败"
                )
            if not refresh_ok:
                return False, (
                    f"切换到版本 {target_version} 失败，已回退到原版本 {previous_version}，"
                    "但注册刷新失败"
                )
            return False, f"切换到版本 {target_version} 失败，已回退到原版本 {previous_version}"

        self._stop_failed_runtime(instance_id, None)
        self._restore_binding(instance, instance_id)
        # 目标和回退都失败时，旧版本已经被成功停掉，不能留下旧路由、调度和
        # 命令继续指向一个不存在的实例。按实际运行表刷新，空实例会撤销旧注册。
        self._refresh_registrations_safely(instance_id)
        self._logger.error(
            f"插件实例 {instance_id} 以原版本 {previous_version} 回退启动同样失败"
        )
        detail = f"，实际加载的是 {fallback_mismatch}" if fallback_mismatch else ""
        return False, (
            f"切换到版本 {target_version} 失败，回退到原版本 {previous_version} 同样失败{detail}"
        )

    def _effective_version_mismatch(
        self,
        instance_id: str,
        target_version: Optional[str],
    ) -> Optional[str]:
        """核对本次启动实际生效的版本，与目标版本不符时返回实际版本。

        实际生效版本直接从运行中的插件对象读取；运行对象缺少版本声明时无从核对，
        按相符处理，不因判据缺失把成功的切换报成失败。

        :param instance_id: 实例ID
        :param target_version: 本次请求切换到的版本，跟随当前版本时为空
        :return: 实际生效且与目标不符的版本号；相符或无从核对时为 None
        """
        if not target_version:
            return None
        running = self._running_instance(instance_id)
        effective: Optional[str] = (
            getattr(running, "plugin_version", None) if running is not None else None
        )
        if not effective or effective == target_version:
            return None
        return effective

    def _persist_binding(self, instance: PluginInstance) -> None:
        """按实例身份把版本绑定写回本体或分身各自的持久化端口。"""
        if instance.is_host:
            self._save_host_instance(instance)
        else:
            self._save_instance(instance)

    def _referenced_versions(self, plugin_id: str) -> set[str]:
        """收集本体与全部分身实例的已生效版本，以及按跟随开关解析出的期望版本。

        两者都要并入回收判据的引用集合，否则会误删已生效但暂无实例在跑、或
        即将切换过去的版本；本体同样适用这条判据，遗漏本体会误删它正在用的
        版本。集合来自对实例存储的实测查询，任何读取失败都直接向上抛出而不
        是按空集继续，交由回收调用方跳过本次回收，避免在凑不齐引用集合的
        情况下误删仍在用的版本且无从恢复。

        :param plugin_id: 插件ID
        :return: 被引用的版本号集合
        """
        current_version = self._current_version(plugin_id)
        referenced: set[str] = set()
        host_instance = self._host_instance(plugin_id)
        if host_instance.pinned_version:
            referenced.add(host_instance.pinned_version)
        host_expected = self._instance_expected_version(host_instance, current_version)
        if host_expected:
            referenced.add(host_expected)
        host_running = self._running_version(host_instance.instance_id)
        if host_running:
            referenced.add(host_running)
        # 含已卸载的分身：它们锚定的版本目录不能被当成无人引用而删掉，否则恢复
        # 出来的分身会落到一个不存在的版本上
        for instance in self._all_instances_for_source(plugin_id):
            if instance.pinned_version:
                referenced.add(instance.pinned_version)
            expected = self._instance_expected_version(instance, current_version)
            if expected:
                referenced.add(expected)
            running_version = self._running_version(instance.instance_id)
            if running_version:
                referenced.add(running_version)
        return referenced

    def recycle_versions(self, plugin_id: str) -> dict[str, Any]:
        """回收指定插件不再被引用、也不在最近版本窗口内的已装版本目录。

        :param plugin_id: 插件ID
        :return: 含 removed（已删除版本号列表）与 kept（版本号到保留理由的映射）的字典
        :raise LookupError: 插件不存在，或 ``plugin_id`` 实为某个分身自身的实例 ID
        """
        if not self._plugin_exists(plugin_id):
            raise LookupError(f"插件 {plugin_id} 不存在")
        if self._get_instance(plugin_id) is not None:
            raise LookupError(f"{plugin_id} 是分身实例，请使用源插件 ID 回收版本")
        plugin_root = self._plugin_root(plugin_id)
        referenced = self._referenced_versions(plugin_id)
        try:
            has_pending_installation = bool(self._pending_installation(plugin_id))
        except Exception as error:  # noqa: BLE001 - 回收判据未知时必须拒绝删除
            self._logger.error(
                f"检查插件 {plugin_id} 的安装事务失败，跳过版本回收：{error}"
            )
            raise RuntimeError(
                f"无法确认插件 {plugin_id} 的安装事务状态，拒绝版本回收"
            ) from error
        return recycle_plugin_version_directories(
            plugin_root,
            referenced,
            has_pending_installation=has_pending_installation,
        )
