"""分类策略启动迁移、运行时回退和旧 API 发布测试。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar, cast

import pytest

from app.api.endpoints import media as media_endpoint
from app.application.classification.configuration import (
    ClassificationPolicyConfigurationService,
    build_default_classification_policy,
)
from app.application.classification.contract import ClassificationPolicyConflictError
from app.application.classification.runtime import ClassificationRuntime
from app.schemas.category import (
    ClassificationPolicyState,
)
from app.schemas.types import SystemConfigKey
from app.startup.composition import classification as classification_composition

T = TypeVar("T")


class _MemoryPolicyStore:
    """模拟分类状态 CAS 存储并记录成功写入次数。"""

    def __init__(self, state: ClassificationPolicyState | None = None) -> None:
        """保存可选初始状态。"""
        self.state = state.model_copy(deep=True) if state else None
        self.write_count = 0

    def load(self) -> ClassificationPolicyState | None:
        """返回隔离的状态快照。"""
        return self.state.model_copy(deep=True) if self.state else None

    def compare_and_set(
        self,
        *,
        expected_revision: int,
        state: ClassificationPolicyState,
    ) -> None:
        """按活动 revision 原子替换状态。"""
        current_revision = self.state.active.revision if self.state else 0
        if current_revision != expected_revision:
            raise ClassificationPolicyConflictError(
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        self.state = state.model_copy(deep=True)
        self.write_count += 1


class _InlineExecutor:
    """直接执行启动阶段的同步操作。"""

    async def run(self, operation: Callable[[], T]) -> T:
        """执行并返回操作结果。"""
        return operation()


class _SystemConfig:
    """提供启动组合所需的只读配置键目录。"""

    def __init__(self, values: Mapping[str, Any]) -> None:
        """保存配置快照。"""
        self._values = dict(values)

    def all(self) -> dict[str, Any]:
        """返回隔离的配置快照。"""
        return dict(self._values)

    def get(self, key: object) -> Any:
        """按枚举或字符串键返回启动测试配置值。"""
        normalized = getattr(key, "value", key)
        return self._values.get(str(normalized))

    def publish_many(self, values: Mapping[object, Any]) -> None:
        """模拟提交后的系统配置快照发布。"""
        for key, value in values.items():
            normalized = getattr(key, "value", key)
            self._values[str(normalized)] = value


def _published_default_state() -> ClassificationPolicyState:
    """构造已经持久化的 revision 1 默认策略状态。"""
    policy = build_default_classification_policy().model_copy(update={"revision": 1})
    return ClassificationPolicyState(active=policy)


@pytest.mark.asyncio  # type: ignore[misc]
async def test_existing_policy_never_reads_legacy_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """新配置键一旦存在，启动不得再让 category.yaml 影响事实源。"""
    state = _published_default_state()
    store = _MemoryPolicyStore(state)
    legacy_path = tmp_path / "category.yaml"
    legacy_path.write_text("movie: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        classification_composition,
        "SystemConfigClassificationPolicyStore",
        lambda *_args: store,
    )
    monkeypatch.setattr(
        classification_composition,
        "_load_legacy_yaml",
        lambda _path: pytest.fail("存在新策略时不应读取 YAML"),
    )
    system_config = _SystemConfig({SystemConfigKey.MediaClassificationPolicy.value: state.model_dump(mode="json")})

    composition = await classification_composition.compose_classification(
        executor=cast(Any, _InlineExecutor()),
        settings=cast(Any, SimpleNamespace(CONFIG_PATH=tmp_path)),
        system_config=cast(Any, system_config),
    )

    assert composition.migrated is False
    assert composition.runtime.require_policy().revision == 1
    assert store.write_count == 0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_absent_policy_migrates_yaml_once_without_rewriting_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """合法旧配置只发布 revision 1，原 YAML 内容保持不变。"""
    content = """movie:\n  动画电影:\n    genre_ids: '16'\n  外语电影:\ntv:\n  未分类:\n"""
    legacy_path = tmp_path / "category.yaml"
    legacy_path.write_text(content, encoding="utf-8")
    store = _MemoryPolicyStore()
    monkeypatch.setattr(
        classification_composition,
        "SystemConfigClassificationPolicyStore",
        lambda *_args: store,
    )

    composition = await classification_composition.compose_classification(
        executor=cast(Any, _InlineExecutor()),
        settings=cast(Any, SimpleNamespace(CONFIG_PATH=tmp_path)),
        system_config=cast(Any, _SystemConfig({})),
    )

    assert composition.migrated is True
    assert composition.runtime.require_policy().revision == 1
    assert store.write_count == 1
    assert legacy_path.read_text(encoding="utf-8") == content


@pytest.mark.asyncio  # type: ignore[misc]
async def test_invalid_legacy_config_keeps_runtime_uninitialized_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """无法无损表达的旧字段阻止自动发布，并保留结构化诊断。"""
    (tmp_path / "category.yaml").write_text(
        "movie:\n  错误规则:\n    Bad/Field: value\ntv:\n  未分类:\n",
        encoding="utf-8",
    )
    store = _MemoryPolicyStore()
    monkeypatch.setattr(
        classification_composition,
        "SystemConfigClassificationPolicyStore",
        lambda *_args: store,
    )

    composition = await classification_composition.compose_classification(
        executor=cast(Any, _InlineExecutor()),
        settings=cast(Any, SimpleNamespace(CONFIG_PATH=tmp_path)),
        system_config=cast(Any, _SystemConfig({})),
    )

    assert composition.migrated is False
    assert composition.runtime.active_policy() is None
    assert any(issue.severity == "error" for issue in composition.runtime.diagnostics())
    assert store.write_count == 0


def test_runtime_compat_projection_is_read_only_and_includes_music_categories() -> None:
    """运行时兼容门面只投影旧结构，并让分类列表包含音乐。"""
    store = _MemoryPolicyStore()
    service = ClassificationPolicyConfigurationService(store)
    service.initialize()
    runtime = ClassificationRuntime(service)

    categories = runtime.media_categories().root

    assert categories["音乐"] == ["未分类"]
    assert runtime.legacy_config().movie == {}
    assert store.write_count == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_legacy_category_get_endpoints_use_classification_runtime_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧分类只读端点不得继续调度 TMDB 模块。"""
    store = _MemoryPolicyStore()
    service = ClassificationPolicyConfigurationService(store)
    service.initialize()
    runtime = ClassificationRuntime(service)
    monkeypatch.setattr(
        media_endpoint,
        "MediaChain",
        lambda: pytest.fail("旧分类端点不应再调用 MediaChain"),
    )

    before = media_endpoint.get_category_config(object(), runtime)
    categories = await media_endpoint.category(object(), runtime)

    assert before.success is True
    assert before.data == {"movie": {}, "tv": {}}
    assert categories.root == {
        "电影": ["未分类"],
        "电视剧": ["未分类"],
        "音乐": ["未分类"],
    }


def test_legacy_category_config_route_has_no_write_method() -> None:
    """旧分类配置端点只保留 GET 投影，不再暴露任何写方法。"""
    methods = {
        method
        for route in media_endpoint.router.routes
        if route.path == "/category/config"
        for method in route.methods or set()
    }

    assert methods == {"GET"}
    assert not hasattr(media_endpoint, "save_category_config")
