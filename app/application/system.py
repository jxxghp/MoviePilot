"""系统管理 API 的应用用例与外部能力端口。"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

from app.application.classification.contract import ClassificationPolicyStateCorruptError
from app.application.configuration import RuntimeSettingsService, SystemConfigService
from app.runtime.scheduling import TimerUtils
from app.schemas.common import JsonData
from app.schemas.exception import PluginMutationRejectedError
from app.schemas.system import SystemUpdateStatus, SystemUpdateType
from app.schemas.types import SystemConfigKey

if TYPE_CHECKING:
    from app.application.rules import AsyncRuleGroupMutationService

PLUGIN_MARKET_WIKI_URL = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Wiki/main/plugin.md"
_DATABASE_BACKUP_SETTING_KEYS = {
    "DB_BACKUP_ENABLE",
    "DB_BACKUP_CRON",
    "DB_BACKUP_ON_UPGRADE",
    "DB_BACKUP_PATH",
    "DB_BACKUP_RETENTION_DAYS",
    "DB_BACKUP_MAX_COUNT",
}


class LogNotFoundError(FileNotFoundError):
    """表示请求的日志标识或文件不属于可读取日志集合。"""


@dataclass(frozen=True, slots=True)
class LogFileData:
    """日志归档中的稳定文件名与内容。"""

    name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class SystemOperationResult:
    """系统管理用例返回给传输层的稳定执行结果。"""

    success: bool
    message: str | None = None
    data: Any = None


@dataclass(frozen=True, slots=True)
class MarketFetchResult:
    """Wiki 市场清单传输端口返回的状态与仓库列表。"""

    status_code: int | None
    repos: list[str]


class SystemLogPort(Protocol):
    """日志文件发现、读取与轮询的异步端口。"""

    async def read(self, logfile: str) -> str:
        """读取一个受准入保护的日志文件。"""
        ...

    async def follow(
        self,
        logfile: str,
        length: int,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[str]:
        """返回历史尾部及后续新增日志行。"""
        ...

    async def collect(self, name: str) -> list[LogFileData]:
        """读取一个主程序或插件日志归档集合。"""
        ...


class PluginMarketPort(Protocol):
    """读取并解析 Wiki 插件市场清单的传输端口。"""

    async def fetch(self, url: str) -> MarketFetchResult:
        """读取指定 Wiki 文档并返回规范化仓库列表。"""
        ...


class ConfigurationEventPort(Protocol):
    """发布系统配置变更事实的端口。"""

    async def publish(self, key: Any, value: JsonData = None) -> None:
        """发布一个已经持久化完成的配置变更。"""
        ...


class ServerInfoPort(Protocol):
    """MoviePilot Server 查询能力的最小端口。"""

    async def user_global(self) -> dict[str, Any]:
        """返回当前用户的服务端权限投影。"""
        ...

    async def usage(self) -> dict[str, Any]:
        """返回匿名安装版本统计。"""
        ...


class ReleaseCatalogPort(Protocol):
    """读取 GitHub Release 目录的传输端口。"""

    async def list(self) -> list[dict[str, Any]] | None:
        """返回 Release 列表，读取失败时返回空值。"""
        ...


class RuntimeFeaturePort(Protocol):
    """查询本机可选运行能力的端口。"""

    def snapshot(self) -> dict[str, bool]:
        """返回 Rust 加速等本机能力快照。"""
        ...


class SystemControlPort(Protocol):
    """主进程更新和重启控制端口。"""

    def can_restart(self) -> bool:
        """返回当前部署环境是否支持受管重启。"""
        ...

    def restart(self) -> tuple[bool, str]:
        """请求重启并返回兼容结果。"""
        ...

    def upgrade_dev(self) -> tuple[bool, str]:
        """执行开发分支更新。"""
        ...


class SystemUpdatePort(Protocol):
    """后台 Release 更新状态机端口。"""

    def status(self) -> SystemUpdateStatus:
        """读取当前后台更新状态。"""
        ...

    def check(self) -> SystemUpdateStatus:
        """立即检查正式版本更新。"""
        ...

    def download(self, target: SystemUpdateType = "application") -> SystemUpdateStatus:
        """启动指定升级类型的后台下载并返回当前状态。"""
        ...

    def prepare_install(self, target: SystemUpdateType = "application") -> tuple[bool, str]:
        """确认消费指定升级类型的已校验制品。"""
        ...

    def cancel_install(self, message: str) -> None:
        """在重启失败时撤销安装请求。"""
        ...


class LlmCapabilityPort(Protocol):
    """校验 LLM 服务端工具能力的端口。"""

    def validate(self, env: dict[str, Any], settings: RuntimeSettingsService) -> str | None:
        """校验本次设置变更是否请求了不可用能力。"""
        ...


class SystemService:
    """集中执行 System API 的日志、设置、市场和进程控制用例。"""

    def __init__(
        self,
        *,
        settings: RuntimeSettingsService,
        system_config: SystemConfigService,
        logs: SystemLogPort,
        market: PluginMarketPort,
        events: ConfigurationEventPort,
        server: ServerInfoPort,
        releases: ReleaseCatalogPort,
        features: RuntimeFeaturePort,
        control: SystemControlPort,
        updates: SystemUpdatePort,
        llm: LlmCapabilityPort,
        plugin_mutation: Callable[[str], AbstractContextManager[None]],
        rule_group_mutation: Callable[[], AbstractAsyncContextManager[AsyncRuleGroupMutationService]],
    ) -> None:
        """保存由启动组合根注入的全部外部端口。"""
        self.settings = settings
        self._system_config = system_config
        self._logs = logs
        self._market = market
        self._events = events
        self._server = server
        self._releases = releases
        self._features = features
        self._control = control
        self._updates = updates
        self._llm = llm
        self._plugin_mutation = plugin_mutation
        self._rule_group_mutation = rule_group_mutation

    async def publish_config_changed(
        self,
        key: Any,
        value: JsonData = None,
    ) -> None:
        """发布一项已经持久化完成的系统配置变更。"""
        await self._events.publish(key, value)

    async def read_log(self, logfile: str) -> str:
        """读取日志并按旧接口语义倒序返回。"""
        text = await self._logs.read(logfile)
        return "\n".join(text.split("\n")[::-1])

    async def follow_log(
        self,
        logfile: str,
        length: int,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[str]:
        """取得日志轮询数据源，传输层只负责 SSE framing。"""
        return await self._logs.follow(logfile, length, disconnected)

    async def collect_logs(self, name: str) -> list[LogFileData]:
        """读取日志归档条目，传输层只负责 ZIP framing。"""
        return await self._logs.collect(name)

    async def update_environment(self, env: dict[str, Any]) -> SystemOperationResult:
        """校验并原子执行一批可变部署设置更新。"""
        validation_error = self._llm.validate(env, self.settings)
        if validation_error:
            return SystemOperationResult(False, validation_error)
        validation_error = self._validate_database_backup_config(env)
        if validation_error:
            return SystemOperationResult(False, validation_error)
        result = self.settings.update_many(env)
        success_updates = {key: value for key, value in result.items() if value[0]}
        failed_updates = {key: value for key, value in result.items() if value[0] is False}
        if failed_updates:
            return SystemOperationResult(
                False,
                ", ".join(value[1] for value in failed_updates.values()),
                {"success_updates": success_updates, "failed_updates": failed_updates},
            )
        if success_updates:
            await self._events.publish(success_updates.keys())
        return SystemOperationResult(
            True,
            "所有配置项更新成功",
            {"success_updates": success_updates},
        )

    async def sync_plugin_market(self, wiki_url: str | None) -> SystemOperationResult:
        """读取 Wiki 清单、合并本地仓库并更新运行设置。"""
        source_url = (wiki_url or PLUGIN_MARKET_WIKI_URL).strip()
        if not self._is_allowed_wiki_url(source_url):
            return SystemOperationResult(False, "不支持的 Wiki 同步地址")
        fetched = await self._market.fetch(source_url)
        if fetched.status_code is None:
            return SystemOperationResult(False, "无法访问 Wiki 插件仓库清单")
        if fetched.status_code != 200:
            return SystemOperationResult(False, f"访问 Wiki 插件仓库清单失败，状态码：{fetched.status_code}")
        if not fetched.repos:
            return SystemOperationResult(False, "未在 Wiki 中识别到插件仓库地址")
        local_repos = self._split_repos(self.settings.get("PLUGIN_MARKET", ""))
        local_keys = {repo.lower() for repo in local_repos}
        added_count = sum(repo.lower() not in local_keys for repo in fetched.repos)
        merged_repos = self._merge_repos(local_repos, fetched.repos)
        merged_value = ",".join(merged_repos)
        success, message = self.settings.update("PLUGIN_MARKET", merged_value)
        if success:
            await self._events.publish("PLUGIN_MARKET", merged_value)
        elif success is None:
            success = True
        return SystemOperationResult(
            bool(success),
            message,
            {
                "value": merged_value,
                "repos": merged_repos,
                "wiki_repos": fetched.repos,
                "added_count": added_count,
                "total_count": len(merged_repos),
                "source_url": source_url,
            },
        )

    async def update_setting(self, key: str, value: JsonData) -> SystemOperationResult:
        """按配置所有权更新运行设置或持久化系统配置。"""
        if self.settings.contains(key):
            success, message = self.settings.update(key, value)
            if success:
                await self._events.publish(key, value)
            elif success is None:
                success = True
            return SystemOperationResult(bool(success), message)
        if key not in {item.value for item in SystemConfigKey}:
            return SystemOperationResult(False, f"配置项 '{key}' 不存在")
        if isinstance(value, list):
            value = list(filter(None, value)) or None
        try:
            with self._plugin_mutation(key):
                event_value: JsonData = value
                if key == SystemConfigKey.UserFilterRuleGroups.value:
                    current = self._system_config.get(SystemConfigKey.UserFilterRuleGroups)
                    expected = self._dict_list(current)
                    definitions = self._dict_list(value)
                    async with self._rule_group_mutation() as mutation:
                        await mutation.apply(definitions, expected_rule_groups=expected)
                    event_value = definitions
                    success = True
                else:
                    write_result = (
                        await self._system_config.async_set_with_normalized_value(
                            key,
                            value,
                        )
                    )
                    success = write_result.changed
                    event_value = write_result.normalized_value
                if success:
                    await self._events.publish(key, event_value)
                # None 表示值未变化，仍是成功完成；只有明确 False 才代表持久化失败。
                return SystemOperationResult(
                    success is not False,
                    data=event_value if key == SystemConfigKey.Directories.value else None,
                )
        except (
            ClassificationPolicyStateCorruptError,
            PluginMutationRejectedError,
            ValueError,
        ) as error:
            return SystemOperationResult(False, str(error))

    async def user_global(self) -> dict[str, Any]:
        """返回 MoviePilot Server 用户权限投影。"""
        return await self._server.user_global()

    async def usage(self) -> dict[str, Any]:
        """返回安装版本统计。"""
        return await self._server.usage()

    async def releases(self) -> list[dict[str, Any]] | None:
        """返回 GitHub Release 目录。"""
        return await self._releases.list()

    def runtime_features(self) -> dict[str, bool]:
        """返回本机可选运行能力快照。"""
        return self._features.snapshot()

    def restart(self) -> SystemOperationResult:
        """执行受管重启。"""
        if not self._control.can_restart():
            return SystemOperationResult(False, "当前运行环境不支持重启操作！")
        success, message = self._control.restart()
        return SystemOperationResult(success, message)

    def upgrade(self, mode: str | None) -> SystemOperationResult:
        """仅保留兼容的开发分支更新入口。"""
        if str(mode or "").strip().lower() != "dev":
            return SystemOperationResult(False, "Release 更新请使用 /system/update/check、download 和 install 接口")
        if not self._control.can_restart():
            return SystemOperationResult(False, "当前运行环境不支持升级操作！")
        success, message = self._control.upgrade_dev()
        return SystemOperationResult(success, message)

    def update_status(self) -> SystemUpdateStatus:
        """读取后台更新状态。"""
        return self._updates.status()

    def check_update(self) -> SystemUpdateStatus:
        """立即检查正式版本更新。"""
        return self._updates.check()

    def download_update(self, target: SystemUpdateType = "application") -> SystemOperationResult:
        """校验环境并启动指定升级类型的后台下载。"""
        if not self._control.can_restart():
            return SystemOperationResult(False, "当前运行环境不支持升级操作！")
        status = self._updates.download(target)
        return SystemOperationResult(status.state != "failed", status.error, status)

    def install_update(self, target: SystemUpdateType = "application") -> SystemOperationResult:
        """确认指定制品并在重启失败时回滚安装请求。"""
        if not self._control.can_restart():
            return SystemOperationResult(False, "当前运行环境不支持升级操作！")
        prepared, message = self._updates.prepare_install(target)
        if not prepared:
            return SystemOperationResult(False, message)
        success, restart_message = self._control.restart()
        if not success:
            self._updates.cancel_install(restart_message)
            return SystemOperationResult(False, restart_message)
        return SystemOperationResult(True, message)

    def _validate_database_backup_config(self, env: dict[str, Any]) -> str | None:
        """在批量写入前校验数据库备份策略。"""
        if not _DATABASE_BACKUP_SETTING_KEYS.intersection(env):
            return None
        cron = str(env.get("DB_BACKUP_CRON", self.settings.get("DB_BACKUP_CRON")) or "").strip()
        if cron:
            try:
                TimerUtils.normalize_schedule_trigger("cron", cron, self.settings.get("TZ"))
            except (TypeError, ValueError):
                return "数据库备份周期格式不正确"
        backup_path = env.get("DB_BACKUP_PATH", self.settings.get("DB_BACKUP_PATH"))
        if backup_path is not None and not isinstance(backup_path, str):
            return "数据库备份目录必须是路径字符串"
        for key, label in (
            ("DB_BACKUP_RETENTION_DAYS", "数据库备份过期天数"),
            ("DB_BACKUP_MAX_COUNT", "数据库备份最大保留份数"),
        ):
            value = env.get(key, self.settings.get(key))
            if isinstance(value, bool):
                return f"{label}必须是大于等于 0 的整数"
            try:
                converted = int(value)
            except (TypeError, ValueError):
                return f"{label}必须是大于等于 0 的整数"
            if converted < 0 or str(value).strip() != str(converted):
                return f"{label}必须是大于等于 0 的整数"
        return None

    @staticmethod
    def _is_allowed_wiki_url(wiki_url: str) -> bool:
        """限制 Wiki 同步到维护者控制的固定文档源。"""
        parsed = urlparse(wiki_url)
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() == "raw.githubusercontent.com"
            and bool(re.fullmatch(r"/jxxghp/MoviePilot-Wiki/[^/]+/plugin\.md", parsed.path))
        )

    @staticmethod
    def _split_repos(value: Any) -> list[str]:
        """拆分并规范化逗号分隔的仓库地址。"""
        return SystemService._merge_repos([], str(value or "").split(","))

    @staticmethod
    def _merge_repos(existing: list[str], incoming: list[str]) -> list[str]:
        """按来源顺序合并仓库地址并执行大小写不敏感去重。"""
        merged: list[str] = []
        seen: set[str] = set()
        for repo in (*existing, *incoming):
            normalized = str(repo or "").strip().rstrip("/")
            key = normalized.lower()
            if normalized and key not in seen:
                seen.add(key)
                merged.append(normalized)
        return merged

    @staticmethod
    def _dict_list(value: JsonData) -> list[dict[str, Any]]:
        """只保留规则组列表中的字典定义并复制输入。"""
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]
