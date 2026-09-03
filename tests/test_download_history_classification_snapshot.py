"""下载历史分类快照在整理识别后的冻结恢复测试。"""

from app.application.history import DownloadHistorySnapshot
from app.chain.transfer.records import apply_download_history_classification
from app.domain.context import MediaInfo
from app.schemas.category import ClassificationResult, ClassificationSelection
from app.schemas.types import MediaSource, MediaType


def _recognized_media() -> MediaInfo:
    """构造已由当前策略完成自动分类的媒体识别结果。"""
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="1001",
        type=MediaType.MOVIE,
        title="当前识别结果",
        library_category="电影/当前分类",
        category="电影/当前分类",
        classification=ClassificationResult(
            recommended=ClassificationSelection(
                category_id="movie.current",
                category_path=["电影", "当前分类"],
                rule_id="rule.current",
                source="automatic",
            ),
            effective=ClassificationSelection(
                category_id="movie.current",
                category_path=["电影", "当前分类"],
                rule_id="rule.current",
                source="automatic",
            ),
            policy_revision=21,
            state="complete",
        ),
    )


def _history(**classification: object) -> DownloadHistorySnapshot:
    """构造只关注分类持久化标量的下载历史快照。"""
    return DownloadHistorySnapshot(
        id=1,
        path="/downloads/example.mkv",
        type=MediaType.MOVIE.value,
        title="历史下载",
        **classification,
    )


def test_full_snapshot_overrides_current_automatic_classification() -> None:
    """完整历史快照必须冻结覆盖当前策略重新识别出的自动分类。"""
    current = _recognized_media()
    history = _history(
        media_category_id="movie.historical",
        media_category="电影/历史分类",
        classification_rule_id="rule.historical",
        classification_policy_revision=7,
        classification_source="subscription",
    )

    restored = apply_download_history_classification(current, history)

    assert restored is not current
    assert restored.library_category == "电影/历史分类"
    assert restored.classification is not None
    assert restored.classification.policy_revision == 7
    assert restored.classification.effective == ClassificationSelection(
        category_id="movie.historical",
        category_path=["电影", "历史分类"],
        rule_id="rule.historical",
        source="subscription",
    )
    assert current.library_category == "电影/当前分类"
    assert current.classification is not None
    assert current.classification.effective is not None
    assert current.classification.effective.category_id == "movie.current"


def test_legacy_media_category_path_is_restored_and_marked_legacy() -> None:
    """仅保存旧 media_category 的历史仍可读取，并显式标记为 legacy。"""
    current = _recognized_media()

    restored = apply_download_history_classification(
        current,
        _history(media_category="电影/旧目录"),
    )

    assert restored is not current
    assert restored.library_category == "电影/旧目录"
    assert restored.classification is not None
    assert restored.classification.effective == ClassificationSelection(
        category_id=None,
        category_path=["电影", "旧目录"],
        rule_id=None,
        source="legacy",
    )


def test_empty_history_keeps_recognized_classification_unchanged() -> None:
    """没有分类标量的旧历史不得覆盖本次识别出的有效分类。"""
    current = _recognized_media()

    restored = apply_download_history_classification(current, _history())

    assert restored is current
    assert restored.library_category == "电影/当前分类"
    assert restored.classification is not None
    assert restored.classification.policy_revision == 21
    assert restored.classification.effective is not None
    assert restored.classification.effective.category_id == "movie.current"


def test_unsafe_history_path_does_not_pollute_recognized_classification() -> None:
    """包含目录逃逸的历史路径必须被丢弃，不得污染识别分类。"""
    current = _recognized_media()

    restored = apply_download_history_classification(
        current,
        _history(media_category="电影/../逃逸"),
    )

    assert restored is current
    assert restored.library_category == "电影/当前分类"
    assert restored.classification is not None
    assert restored.classification.policy_revision == 21
    assert restored.classification.effective is not None
    assert restored.classification.effective.category_id == "movie.current"
