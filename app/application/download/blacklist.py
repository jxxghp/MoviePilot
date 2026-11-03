"""
通过现有系统配置原子更新端口管理永久种子黑名单。
"""

from datetime import datetime, timezone
from functools import partial
from uuid import uuid4

from app.application.configuration import SystemConfigService, get_configured_system_config
from app.domain.blacklist import (
    BlacklistConfigurationError,
    BlacklistIdentityError,
    blacklist_matches,
    parse_blacklist_config,
    validated_content_hashes,
)
from app.domain.context import TorrentInfo
from app.schemas.blacklist import (
    BlacklistAddResult,
    BlacklistConfig,
    BlacklistEntry,
    BlacklistIdentity,
    BlacklistPage,
    BlacklistRule,
)
from app.schemas.common import JsonData
from app.schemas.types import SystemConfigKey


def _merge_resources(
    candidate: BlacklistEntry,
    entries: list[BlacklistEntry],
) -> tuple[BlacklistEntry, list[BlacklistEntry]] | None:
    """
    合并由新增别名连接的所有已有资源，保留最早条目的 ID、时间和展示来源。
    """
    if candidate.identity is None:
        return None
    digests, hashes = set(candidate.identity.digests), set(candidate.identity.hashes)
    remaining = list(entries)
    matched: list[BlacklistEntry] = []
    while True:
        connected = [
            entry for entry in remaining if entry.identity is not None
            and (digests.intersection(entry.identity.digests) or hashes.intersection(entry.identity.hashes))
        ]
        if not connected:
            break
        for entry in connected:
            if entry.identity is not None:
                digests.update(entry.identity.digests)
                hashes.update(entry.identity.hashes)
        matched.extend(connected)
        connected_ids = {entry.id for entry in connected}
        remaining = [entry for entry in remaining if entry.id not in connected_ids]
    if not matched:
        return None
    original = min(matched, key=lambda entry: (entry.created_at, entry.id))
    identity = BlacklistIdentity(
        digests=tuple(sorted(digests)), hashes=tuple(sorted(hashes)),
        site_id=original.identity.site_id if original.identity else None,
        uploader=original.identity.uploader if original.identity else None,
    )
    merged = original.model_copy(update={"identity": identity})
    return merged, [*remaining, merged]


def _add_entry(candidate: BlacklistEntry, current: object) -> tuple[BlacklistAddResult, dict[str, JsonData]]:
    """
    纯原子变更：先归一化去重和合并，最后检查总量；失败不发布新值。
    """
    config = parse_blacklist_config(current)
    entries = list(config.entries)
    for entry in entries:
        if candidate.rule is not None and candidate.rule == entry.rule:
            return BlacklistAddResult(entry=entry, created=False), config.model_dump(mode="json")
    if result := _merge_resources(candidate, entries):
        merged, entries = result
        updated = BlacklistConfig(entries=tuple(entries))
        return BlacklistAddResult(entry=merged, created=False), updated.model_dump(mode="json")
    if len(entries) >= 1000:
        raise ValueError("种子黑名单最多保存 1000 条，请先移除不需要的条目")
    entries.append(candidate)
    updated = BlacklistConfig(entries=tuple(entries))
    return BlacklistAddResult(entry=candidate, created=True), updated.model_dump(mode="json")


def _remove_entry(entry_id: str, current: object) -> tuple[bool, dict[str, JsonData]]:
    """
    根据最新持久化快照幂等删除，不修改下载任务或历史。
    """
    config = parse_blacklist_config(current)
    entries = tuple(entry for entry in config.entries if entry.id != entry_id)
    return len(entries) != len(config.entries), BlacklistConfig(entries=entries).model_dump(mode="json")


class TorrentBlacklistService:
    """
    永久黑名单用例，复用现有单 HostRuntime 原子配置一致性。
    """

    def __init__(self, configuration: SystemConfigService | None = None) -> None:
        """
        接收配置服务或使用已配置端口，不持有匹配缓存。
        """
        self._configuration = configuration if configuration is not None else get_configured_system_config()

    def snapshot(self) -> BlacklistConfig:
        """
        返回深层不可变快照，损坏的持久化限制不会静默丢弃。
        """
        return parse_blacklist_config(self._configuration.get(SystemConfigKey.TorrentBlacklist))

    def list(self, *, page: int = 1, count: int = 30) -> BlacklistPage:
        """
        在单个快照上按创建时间和 ID 倒序分页，避免计数与列表读取不同版本。
        """
        if page < 1 or not 1 <= count <= 100:
            raise ValueError("黑名单页码或每页条数无效")
        entries = sorted(self.snapshot().entries, key=lambda entry: (entry.created_at, entry.id), reverse=True)
        return BlacklistPage(items=tuple(entries[(page - 1) * count:page * count]),
                             total=len(entries), page=page, count=count)

    def rejection(self, torrent: TorrentInfo, content: str | bytes = b"") -> str | None:
        """
        重新读取最新规则并返回拒绝原因，损坏配置也应阻止下载。
        """
        try:
            validated_content_hashes(content)
            if blacklist_matches(self.snapshot().entries, torrent, content):
                return "资源命中永久种子黑名单"
        except (BlacklistConfigurationError, BlacklistIdentityError) as exc:
            return str(exc)
        return None

    def add_rule(self, rule: BlacklistRule) -> BlacklistAddResult:
        """
        同步原子新增手动规则，重复规则保留 ID 和创建时间。
        """
        self.snapshot()
        entry = BlacklistEntry(id=uuid4().hex, created_at=datetime.now(timezone.utc), kind="manual", rule=rule)
        return self._configuration.update_atomically(SystemConfigKey.TorrentBlacklist, partial(_add_entry, entry))

    async def async_add_rule(self, rule: BlacklistRule) -> BlacklistAddResult:
        """
        异步原子新增手动规则，数据库工作交给已有异步执行端口。
        """
        self.snapshot()
        entry = BlacklistEntry(id=uuid4().hex, created_at=datetime.now(timezone.utc), kind="manual", rule=rule)
        return await self._configuration.async_update_atomically(
            SystemConfigKey.TorrentBlacklist, partial(_add_entry, entry))

    def add_resource(self, identity: BlacklistIdentity) -> BlacklistAddResult:
        """
        同步原子加入资源身份，重叠别名合并到已有条目。
        """
        self.snapshot()
        entry = BlacklistEntry(id=uuid4().hex, created_at=datetime.now(timezone.utc), kind="resource", identity=identity)
        return self._configuration.update_atomically(SystemConfigKey.TorrentBlacklist, partial(_add_entry, entry))

    async def async_add_resource(self, identity: BlacklistIdentity) -> BlacklistAddResult:
        """
        异步原子加入从服务端历史提取的安全资源身份。
        """
        self.snapshot()
        entry = BlacklistEntry(id=uuid4().hex, created_at=datetime.now(timezone.utc), kind="resource", identity=identity)
        return await self._configuration.async_update_atomically(
            SystemConfigKey.TorrentBlacklist, partial(_add_entry, entry))

    def remove(self, entry_id: str) -> bool:
        """
        同步原子移除条目；条目不存在时返回否。
        """
        self.snapshot()
        return self._configuration.update_atomically(SystemConfigKey.TorrentBlacklist, partial(_remove_entry, entry_id))

    async def async_remove(self, entry_id: str) -> bool:
        """
        异步原子移除条目；条目不存在仍是成功的幂等操作。
        """
        self.snapshot()
        return await self._configuration.async_update_atomically(
            SystemConfigKey.TorrentBlacklist, partial(_remove_entry, entry_id))
