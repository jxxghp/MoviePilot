"""订阅稳定分类引用的写入、兼容与运行时覆盖契约测试。"""

from collections.abc import Iterator

import pytest

from app.application.classification.execution import ClassificationExecutionService
from app.application.classification.reference import (
    ClassificationCategoryResolver,
    configure_classification_category_resolver,
    normalize_classification_reference_payload,
    reset_classification_category_resolver,
    subscription_classification_override,
)
from app.domain.context import MediaInfo
from app.schemas.category import (
    CategoryConfig,
    ClassificationCategory,
    ClassificationPolicy,
)
from app.schemas.types import MediaType


class _MutableClassificationRuntime:
    """为订阅引用测试提供可切换 revision 的活动策略。"""

    def __init__(self, policy: ClassificationPolicy) -> None:
        """保存当前活动策略。"""
        self.policy = policy

    def active_policy(self) -> ClassificationPolicy:
        """返回当前活动策略。"""
        return self.policy

    def legacy_config(self) -> CategoryConfig:
        """返回空 legacy 配置，确保测试只观察新分类体系。"""
        return CategoryConfig()


def _policy(
    *,
    revision: int = 7,
    automatic_path: list[str] | None = None,
) -> ClassificationPolicy:
    """构造同时包含有效、禁用和跨媒体类型分类的测试策略。"""
    return ClassificationPolicy(
        revision=revision,
        categories=[
            ClassificationCategory(
                id="movie.auto",
                media_type="电影",
                name="自动电影",
                path=automatic_path or ["电影", "自动"],
            ),
            ClassificationCategory(
                id="movie.favorite",
                media_type="电影",
                name="收藏电影",
                path=["收藏", "当前路径"],
            ),
            ClassificationCategory(
                id="movie.disabled",
                media_type="电影",
                name="禁用电影",
                path=["电影", "禁用"],
                enabled=False,
            ),
            ClassificationCategory(
                id="tv.favorite",
                media_type="电视剧",
                name="收藏剧集",
                path=["剧集", "收藏"],
            ),
        ],
        fallbacks={"电影": "movie.auto"},
    )


@pytest.fixture
def classification_runtime() -> Iterator[_MutableClassificationRuntime]:
    """装配测试策略解析器，并在用例结束后恢复全局状态。"""
    runtime = _MutableClassificationRuntime(_policy())
    previous = configure_classification_category_resolver(
        ClassificationCategoryResolver(runtime)
    )
    try:
        yield runtime
    finally:
        reset_classification_category_resolver(previous)


def _movie() -> MediaInfo:
    """构造具有完整稳定身份、可执行自动分类的电影。"""
    return MediaInfo(
        media_source="douban",
        media_id="1291561",
        type=MediaType.MOVIE,
        title="测试电影",
    )


def test_subscription_reference_prefers_id_and_refreshes_current_path(
    classification_runtime: _MutableClassificationRuntime,
) -> None:
    """稳定 ID 必须覆盖客户端旧路径，并保存当前策略中的路径快照。"""
    normalized = normalize_classification_reference_payload(
        {
            "media_category_id": "movie.favorite",
            "media_category": "收藏/旧路径",
        },
        media_type=MediaType.MOVIE,
    )

    assert classification_runtime.policy.revision == 7
    assert normalized["media_category_id"] == "movie.favorite"
    assert normalized["media_category"] == "收藏/当前路径"


def test_subscription_reference_accepts_legacy_path_only(
    classification_runtime: _MutableClassificationRuntime,
) -> None:
    """旧订阅只有 path 时仍应可写入并形成无稳定 ID 的人工覆盖。"""
    normalized = normalize_classification_reference_payload(
        {"media_category": "旧目录/电影"},
        media_type=MediaType.MOVIE,
    )
    override = subscription_classification_override(
        category_id=normalized["media_category_id"],
        path_snapshot=normalized["media_category"],
        media_type=MediaType.MOVIE,
    )

    assert classification_runtime.policy.revision == 7
    assert normalized["media_category_id"] is None
    assert normalized["media_category"] == "旧目录/电影"
    assert override is not None
    assert override.category_id is None
    assert override.category_path == ["旧目录", "电影"]
    assert override.source == "subscription"


def test_subscription_reference_explicit_null_clears_id_and_path(
    classification_runtime: _MutableClassificationRuntime,
) -> None:
    """PATCH 显式清空稳定 ID 时不得遗留旧路径人工覆盖。"""
    normalized = normalize_classification_reference_payload(
        {
            "media_category_id": None,
            "media_category": "不得残留/旧路径",
        },
        media_type=MediaType.MOVIE,
    )

    assert classification_runtime.policy.revision == 7
    assert normalized["media_category_id"] is None
    assert normalized["media_category"] is None


def test_subscription_without_override_reclassifies_with_current_policy(
    classification_runtime: _MutableClassificationRuntime,
) -> None:
    """未设置人工分类的订阅不得锁定创建时路径，应跟随当前策略自动分类。"""
    service = ClassificationExecutionService(classification_runtime)
    created = service.finalize(_movie())
    classification_runtime.policy = _policy(
        revision=8,
        automatic_path=["电影", "新版自动"],
    )
    override = subscription_classification_override(
        category_id=None,
        path_snapshot=None,
        media_type=MediaType.MOVIE,
    )

    refreshed = service.finalize(
        created,
        effective_override=override,
        refresh=True,
    )

    assert override is None
    assert created.library_category == "电影/自动"
    assert refreshed.classification is not None
    assert refreshed.classification.policy_revision == 8
    assert refreshed.classification.effective is not None
    assert refreshed.classification.effective.category_id == "movie.auto"
    assert refreshed.library_category == "电影/新版自动"


def test_subscription_override_survives_auxiliary_refresh(
    classification_runtime: _MutableClassificationRuntime,
) -> None:
    """订阅人工覆盖装配后，辅助信息触发的再次分类不得覆盖 effective。"""
    service = ClassificationExecutionService(classification_runtime)
    override = subscription_classification_override(
        category_id="movie.favorite",
        path_snapshot="收藏/旧路径",
        media_type=MediaType.MOVIE,
    )
    overridden = service.finalize(_movie(), effective_override=override)
    overridden.vote_average = 9.5

    refreshed = service.finalize(overridden, refresh=True)

    assert override is not None
    assert refreshed.classification is not None
    assert refreshed.classification.recommended is not None
    assert refreshed.classification.recommended.category_id == "movie.auto"
    assert refreshed.classification.effective is not None
    assert refreshed.classification.effective.category_id == "movie.favorite"
    assert refreshed.classification.effective.category_path == ["收藏", "当前路径"]
    assert refreshed.classification.effective.source == "subscription"
    assert refreshed.library_category == "收藏/当前路径"


@pytest.mark.parametrize(
    ("category_id", "message"),
    [
        ("movie.missing", "不存在"),
        ("movie.disabled", "禁用"),
        ("tv.favorite", "不一致"),
    ],
)
def test_new_subscription_rejects_invalid_category_id_even_with_legacy_path(
    classification_runtime: _MutableClassificationRuntime,
    category_id: str,
    message: str,
) -> None:
    """新写入的无效稳定 ID 不得借合法旧路径降级为可接受引用。"""
    assert classification_runtime.policy.revision == 7

    with pytest.raises(ValueError, match=message):
        normalize_classification_reference_payload(
            {
                "media_category_id": category_id,
                "media_category": "兼容/旧路径",
            },
            media_type=MediaType.MOVIE,
        )
