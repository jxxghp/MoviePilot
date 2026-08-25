"""存量插件来源身份的一次性启动迁移。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginIdentityConflictError,
    PluginMarketAvailability,
    PluginSourceCandidate,
    TrustedPluginSourceType,
    normalize_physical_plugin_id,
    plan_legacy_plugin_identity,
)
from app.application.plugin.source import CandidateInventory
from app.runtime.log import logger

InventoryProvider = Callable[[bool], Awaitable[CandidateInventory]]
InstalledPluginsReader = Callable[[], list[str]]
VirtualInstancePredicate = Callable[[str], bool]


class PluginIdentityMigrationPersistence(Protocol):
    """存量来源迁移所需的最小异步持久化端口。"""

    async def get_identity(self, plugin_id: str) -> PluginIdentity | None:
        """读取一个物理插件的当前身份。"""

    async def migrate_identity(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int | None,
    ) -> PluginIdentity:
        """创建尚不存在的存量身份。"""

    async def bind_online_identity(
        self,
        identity: PluginIdentity,
        *,
        expected_revision: int,
    ) -> PluginIdentity:
        """把 legacy_unbound 身份绑定到已确认的在线来源。"""


@dataclass(frozen=True, slots=True)
class PluginIdentityMigrationResult:
    """一次迁移批次创建、绑定和跳过的物理插件数量。"""

    created: int = 0
    bound: int = 0
    unbound: int = 0
    skipped: int = 0


class PluginIdentityMigrationService:
    """在任何自动更新前为已安装物理插件建立最小来源身份。"""

    def __init__(
        self,
        *,
        persistence: PluginIdentityMigrationPersistence,
        inventory: InventoryProvider,
        installed_plugins: InstalledPluginsReader,
        is_virtual_instance: VirtualInstancePredicate,
        clock: Callable[[], datetime],
    ) -> None:
        """保存库存、安装清单、虚拟实例判定和 CAS 端口。"""
        self.__persistence = persistence
        self.__inventory = inventory
        self.__installed_plugins = installed_plugins
        self.__is_virtual_instance = is_virtual_instance
        self.__clock = clock

    async def migrate(self) -> PluginIdentityMigrationResult:
        """幂等迁移全部存量身份；数据库异常会阻止后续自动更新。"""
        inventory = await self.__inventory(False)
        created = 0
        bound = 0
        unbound = 0
        skipped = 0
        seen: set[str] = set()

        for plugin_id in self.__installed_plugins() or []:
            try:
                normalized_id = normalize_physical_plugin_id(plugin_id)
            except ValueError as error:
                logger.warning("跳过插件 %s 的存量来源迁移：%s", plugin_id, error)
                skipped += 1
                continue

            if normalized_id in seen or self.__is_virtual_instance(plugin_id):
                skipped += 1
                continue
            seen.add(normalized_id)
            candidates = inventory.candidates_for(plugin_id)
            planned = plan_legacy_plugin_identity(
                plugin_id=plugin_id,
                market_availability=(
                    PluginMarketAvailability.AVAILABLE
                    if inventory.can_use_for_tofu
                    else PluginMarketAvailability.UNAVAILABLE
                ),
                online_candidates=tuple(
                    PluginSourceCandidate(
                        source_type=candidate.source_type,
                        source_key=candidate.source_key,
                    )
                    for candidate in candidates
                ),
                is_virtual_instance=False,
                now=self.__clock(),
            )
            existing = await self.__persistence.get_identity(normalized_id)

            if planned is None:
                skipped += 1
                continue
            if existing is None:
                try:
                    migrated = await self.__persistence.migrate_identity(
                        planned,
                        expected_revision=None,
                    )
                except PluginIdentityConflictError:
                    if await self.__persistence.get_identity(plugin_id) is None:
                        raise
                    skipped += 1
                    continue
                created += 1
                if migrated.trusted_source_type is TrustedPluginSourceType.UNKNOWN:
                    unbound += 1
                else:
                    bound += 1
                continue
            if (
                existing.binding_basis is not PluginBindingBasis.LEGACY_UNBOUND
                or planned.trusted_source_type is TrustedPluginSourceType.UNKNOWN
            ):
                skipped += 1
                continue

            target = replace(
                planned,
                plugin_id=existing.plugin_id,
                normalized_plugin_id=existing.normalized_plugin_id,
                revision=existing.revision + 1,
                created_at=existing.created_at,
                updated_at=self.__clock(),
            )
            try:
                await self.__persistence.bind_online_identity(
                    target,
                    expected_revision=existing.revision,
                )
            except PluginIdentityConflictError:
                current = await self.__persistence.get_identity(plugin_id)
                if current is None or current.revision == existing.revision:
                    raise
                skipped += 1
                continue
            bound += 1

        result = PluginIdentityMigrationResult(
            created=created,
            bound=bound,
            unbound=unbound,
            skipped=skipped,
        )
        logger.info(
            "插件来源身份迁移完成：创建=%s，已绑定=%s，未绑定=%s，跳过=%s",
            result.created,
            result.bound,
            result.unbound,
            result.skipped,
        )
        return result


_IDENTITY_MIGRATION_SERVICE: list[PluginIdentityMigrationService] = []


def configure_plugin_identity_migration(
    service: PluginIdentityMigrationService,
) -> None:
    """由组合根登记当前 lifespan 的存量身份迁移服务。"""
    _IDENTITY_MIGRATION_SERVICE.clear()
    _IDENTITY_MIGRATION_SERVICE.append(service)


def get_plugin_identity_migration() -> PluginIdentityMigrationService:
    """返回已装配迁移服务；缺失时拒绝绕过来源迁移。"""
    if not _IDENTITY_MIGRATION_SERVICE:
        raise RuntimeError("插件来源身份迁移服务尚未完成初始化")
    return _IDENTITY_MIGRATION_SERVICE[0]


def reset_plugin_identity_migration() -> None:
    """清除当前 lifespan 的迁移服务，供测试和停机复位。"""
    _IDENTITY_MIGRATION_SERVICE.clear()
