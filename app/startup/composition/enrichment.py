"""分类事实补充 provider、线程预算和 TTL 缓存的运行时组合。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from typing import Any

from pydantic import ValidationError

from app.application.classification.enrichment import (
    ClassificationEnrichmentCacheEntry,
    ClassificationEnrichmentProvider,
    ClassificationEnrichmentService,
)
from app.runtime.cache import TTLCache
from app.runtime.log import logger
from app.runtime.thread import ThreadHelper
from app.schemas.category import ClassificationEnrichmentResponse

_METHOD = "get_media_classification_facts"
_CACHE_VERSION = 1
_CACHE_REGION = "media_classification_enrichment"
_CACHE_MAX_SIZE = 2048
_CACHE_TTL_SECONDS = 300

RuntimeManagerProvider = Callable[[], Any | None]
"""延迟返回已存在模块或插件运行时管理器的组合端口。"""


class RuntimeClassificationEnrichmentProviderCatalog:
    """从注入的宿主模块和插件运行态构造稳定 provider 快照。"""

    def __init__(
        self,
        *,
        module_manager: RuntimeManagerProvider,
        plugin_manager: RuntimeManagerProvider,
    ) -> None:
        """保存由允许访问 concrete 运行时的组合根提供的延迟端口。"""
        self._module_manager = module_manager
        self._plugin_manager = plugin_manager

    def providers(self) -> tuple[ClassificationEnrichmentProvider, ...]:
        """返回先宿主、后插件且各自保持运行目录顺序的 provider。"""
        return (*self._host_providers(), *self._plugin_providers())

    def _host_providers(self) -> tuple[ClassificationEnrichmentProvider, ...]:
        """读取已发布宿主模块，不触发能力物化或启动。"""
        manager = self._module_manager()
        if manager is None:
            return ()
        providers: list[ClassificationEnrichmentProvider] = []
        for spec in manager.list_specs():
            module = manager.get_running_module(spec.id)
            callback = getattr(module, _METHOD, None) if module is not None else None
            if not callable(callback):
                continue
            media_sources = _declared_sources(module)
            if not media_sources:
                continue
            providers.append(
                ClassificationEnrichmentProvider(
                    provider_id=f"host:{spec.id}",
                    provider_name=str(spec.metadata.get("name") or spec.id),
                    media_sources=media_sources,
                    callback=callback,
                )
            )
        return tuple(providers)

    def _plugin_providers(self) -> tuple[ClassificationEnrichmentProvider, ...]:
        """读取运行中插件模块及其已登记媒体来源所有权。"""
        manager = self._plugin_manager()
        if manager is None:
            return ()
        providers: list[ClassificationEnrichmentProvider] = []
        for identity, methods in manager.get_plugin_modules().items():
            if not isinstance(methods, Mapping):
                continue
            callback = methods.get(_METHOD)
            if not callable(callback):
                continue
            plugin_id, plugin_name = identity
            source_ids = tuple(
                source_id
                for item in manager.get_media_sources(plugin_id)
                if (source_id := _enum_text(item.get("media_source")))
            )
            if not source_ids:
                continue
            providers.append(
                ClassificationEnrichmentProvider(
                    provider_id=f"plugin:{plugin_id}",
                    provider_name=str(plugin_name or plugin_id),
                    media_sources=source_ids,
                    callback=callback,
                )
            )
        return tuple(providers)


class RuntimeClassificationEnrichmentCache:
    """把项目 TTLCache 适配为可缓存确定性空响应的补充端口。"""

    def __init__(self) -> None:
        """创建独立 region，策略 revision 已包含在每个缓存键中。"""
        self._cache = TTLCache(
            region=_CACHE_REGION,
            maxsize=_CACHE_MAX_SIZE,
            ttl=_CACHE_TTL_SECONDS,
        )

    def get(self, key: str) -> ClassificationEnrichmentCacheEntry | None:
        """读取并重新校验缓存载荷，损坏条目按未命中处理。"""
        value = self._cache.get(key)
        if not isinstance(value, Mapping) or value.get("version") != _CACHE_VERSION:
            return None
        if value.get("empty") is True:
            return ClassificationEnrichmentCacheEntry(response=None)
        try:
            response = ClassificationEnrichmentResponse.model_validate(value.get("response"))
        except TypeError, ValidationError:
            return None
        return ClassificationEnrichmentCacheEntry(response=response)

    def set(self, key: str, entry: ClassificationEnrichmentCacheEntry) -> None:
        """以 JSON 兼容结构写入有效或空响应。"""
        self._cache.set(
            key,
            {
                "version": _CACHE_VERSION,
                "empty": entry.response is None,
                "response": (entry.response.model_dump(mode="json") if entry.response is not None else None),
            },
        )


def compose_classification_enrichment(
    *,
    module_manager: RuntimeManagerProvider,
    plugin_manager: RuntimeManagerProvider,
) -> ClassificationEnrichmentService:
    """构造一个 lifespan 内共享的缺失事实补充服务。"""
    return ClassificationEnrichmentService(
        RuntimeClassificationEnrichmentProviderCatalog(
            module_manager=module_manager,
            plugin_manager=plugin_manager,
        ),
        submit=ThreadHelper().submit,
        cache=RuntimeClassificationEnrichmentCache(),
        logger=logger.warning,
    )


def _declared_sources(module: object) -> tuple[str, ...]:
    """读取宿主 provider 的显式来源清单，异常声明按无能力处理。"""
    declaration = getattr(module, "get_classification_enrichment_sources", None)
    if not callable(declaration):
        return ()
    try:
        value = declaration()
    except Exception:  # noqa: BLE001  单 provider 声明失败必须隔离
        return ()
    values: Sequence[Any] = tuple(value) if isinstance(value, (list, tuple, set)) else (value,)
    return tuple(source for item in values if (source := _enum_text(item)))


def _enum_text(value: object) -> str:
    """把字符串枚举或普通值转换为稳定文本。"""
    if isinstance(value, Enum):
        value = value.value
    return str(value or "").strip()
