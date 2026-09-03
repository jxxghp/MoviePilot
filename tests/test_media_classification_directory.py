"""稳定分类引用与下载、媒体库目录路由测试。"""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.application.classification.reference import (
    ClassificationCategoryResolver,
    append_classification_category_path,
    ensure_path_within_root,
)
from app.application.directory import (
    DirectoryHelper,
    configure_directory_classification_resolver,
    normalize_directory_configurations,
    normalize_directory_system_config_value,
    reset_directory_classification_resolver,
)
from app.chain.download.subtitle import DownloadSubtitleOwner
from app.chain.transfer.workflow import TransferWorkflowOwner
from app.domain.classification.validation import (
    validate_classification_category_path,
)
from app.domain.context import MediaInfo, MusicInfo
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.category import (
    ClassificationCategory,
    ClassificationPolicy,
    ClassificationResult,
    ClassificationSelection,
)
from app.schemas.system import TransferDirectoryConf
from app.schemas.types import MediaType, SystemConfigKey


class _PolicyProvider:
    """为目录引用测试提供可替换的活动策略。"""

    def __init__(self, policy: ClassificationPolicy | None) -> None:
        """保存当前活动策略。"""
        self.policy = policy

    def active_policy(self) -> ClassificationPolicy | None:
        """返回当前测试策略。"""
        return self.policy


def _policy(
    *categories: ClassificationCategory,
    revision: int = 7,
) -> ClassificationPolicy:
    """构造只用于引用解析、不进入发布校验的活动策略。"""
    return ClassificationPolicy(
        revision=revision,
        categories=list(categories),
    )


def _category(
    category_id: str,
    media_type: str,
    path: list[str],
    *,
    enabled: bool = True,
) -> ClassificationCategory:
    """构造稳定分类定义。"""
    return ClassificationCategory(
        id=category_id,
        media_type=media_type,
        name=path[-1],
        path=path,
        enabled=enabled,
    )


def _media(
    *,
    media_type: MediaType = MediaType.TV,
    category_id: str | None = "tv.anime.jp",
    category_path: list[str] | None = None,
) -> MediaInfo:
    """构造带生效分类快照的媒体对象。"""
    path = category_path or ["旧动漫", "日番"]
    return MediaInfo(
        media_source="themoviedb",
        media_id="1",
        type=media_type,
        title="测试媒体",
        classification=ClassificationResult(
            effective=ClassificationSelection(
                category_id=category_id,
                category_path=path,
                source="automatic",
            )
        ),
    )


@pytest.fixture
def directory_resolver() -> ClassificationCategoryResolver:
    """装配并在用例后清理目录分类解析器。"""
    resolver = ClassificationCategoryResolver(
        _PolicyProvider(
            _policy(
                _category("tv.anime.jp", "电视剧", ["动漫", "日番"]),
                _category("movie.animation", "电影", ["动画"]),
                _category("music.live", "音乐", ["音乐", "现场"]),
            )
        )
    )
    previous = configure_directory_classification_resolver(resolver)
    yield resolver
    reset_directory_classification_resolver(previous)


@pytest.mark.parametrize(
    "segments",
    [
        [],
        ["."],
        [".."],
        ["动漫/日番"],
        ["动漫\\日番"],
        ["CON"],
        ["尾随."],
        [" 前导空格"],
        ["控制\x01字符"],
        ["一", "二", "三", "四", "五"],
    ],
)
def test_category_path_validator_rejects_unsafe_segments(
    segments: list[str],
) -> None:
    """任何来源的分类快照都不能绕过策略路径安全约束。"""
    with pytest.raises(ValueError):
        validate_classification_category_path(segments)


def test_category_path_appends_each_valid_segment_below_root() -> None:
    """多级分类必须逐段追加且始终保留目标根目录。"""
    root = Path("/library")

    assert append_classification_category_path(
        root,
        ["动漫", "日番"],
    ) == Path("/library/动漫/日番")


@pytest.mark.parametrize(
    "target",
    [Path("/escape/file.mkv"), Path("/library/../escape/file.mkv")],
)
def test_final_target_cannot_escape_selected_root(target: Path) -> None:
    """模板或插件返回绝对路径和上级目录时必须在规划阶段拒绝。"""
    with pytest.raises(ValueError):
        ensure_path_within_root(Path("/library"), target)


def test_rename_event_cannot_replace_target_with_path_outside_root() -> None:
    """插件改写后的绝对目标必须再次经过冻结根目录包含性校验。"""

    def update_path(event_type, event_data):
        """模拟重命名插件把结果替换为根目录外的绝对路径。"""
        if event_type.value == "transfer.rename":
            event_data.updated = True
            event_data.updated_str = "/escape/file.mkv"
        return type("Result", (), {"event_data": event_data})()

    with patch(
        "app.modules.filemanager.transhandler.eventmanager.send_event",
        side_effect=update_path,
    ), pytest.raises(ValueError):
        TransHandler.get_rename_path(
            template_string="{{ title }}.mkv",
            rename_dict={"title": "Safe"},
            path=Path("/library"),
        )


def test_stable_id_uses_current_policy_path_after_rename(
    directory_resolver: ClassificationCategoryResolver,
) -> None:
    """媒体和目录的旧路径快照不应阻断稳定 ID 在改名后的匹配。"""
    helper = DirectoryHelper(directory_resolver)
    directory = TransferDirectoryConf(
        media_type="tv",
        media_category_id="tv.anime.jp",
        media_category="旧动漫/日番",
    )
    media = _media()

    reference = helper.resolve_directory_category(directory, media)

    assert reference.stable is True
    assert reference.path == ("动漫", "日番")
    assert helper.media_match_rank(directory, media) == 0


def test_directory_write_refreshes_id_snapshot_and_media_type(
    directory_resolver: ClassificationCategoryResolver,
) -> None:
    """新目录写入应保留稳定 ID，并使用当前路径和规范媒体类型覆盖旧快照。"""
    normalized = normalize_directory_configurations(
        [
            {
                "name": "动漫",
                "media_type": "tv",
                "media_category_id": " tv.anime.jp ",
                "media_category": "旧动漫/日番",
                "extension_field": "preserved",
            }
        ]
    )

    assert normalized == [
        {
            "name": "动漫",
            "media_type": "电视剧",
            "media_category_id": "tv.anime.jp",
            "media_category": "动漫/日番",
            "extension_field": "preserved",
        }
    ]


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        (
            {
                "media_type": "tv",
                "media_category_id": "tv.deleted",
                "media_category": "动漫/日番",
            },
            "分类 ID 无效",
        ),
        ({"media_type": "tv", "media_category": "动漫/../逃逸"}, "分类路径无效"),
        ({"media_type": "podcast"}, "媒体类型"),
    ],
)
def test_directory_write_rejects_invalid_category_reference(
    directory_resolver: ClassificationCategoryResolver,
    configuration: dict[str, object],
    message: str,
) -> None:
    """新目录写入不得持久化失效 ID、不安全路径或未知媒体类型。"""
    with pytest.raises(ValueError, match=message):
        normalize_directory_configurations([configuration])


def test_directory_normalizer_leaves_other_system_config_unchanged() -> None:
    """共享配置规范化边界不能改变目录之外的系统配置。"""
    value = [{"name": "qb"}]

    assert normalize_directory_system_config_value(SystemConfigKey.Downloaders, value) is value


def test_invalid_stable_id_is_excluded_from_automatic_directory_selection() -> None:
    """已删除分类 ID 不得在自动选择时静默按旧路径抢占目录。"""
    resolver = ClassificationCategoryResolver(_PolicyProvider(_policy()))
    helper = DirectoryHelper(resolver)
    stale = TransferDirectoryConf(
        media_type="电视剧",
        media_category_id="tv.deleted",
        media_category="动漫/日番",
    )
    media = _media(category_id=None, category_path=["动漫", "日番"])

    resolution = helper.resolve_directory_category(stale, media)

    assert resolution.state == "category_missing"
    assert resolution.path == ("动漫", "日番")
    assert helper.media_match_rank(stale, media) is None
    assert helper.media_match_rank(
        stale,
        media,
        allow_stale_reference=True,
    ) == 1


def test_directory_selection_prefers_id_then_path_then_typed_and_global(
    directory_resolver: ClassificationCategoryResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置顺序不能让通用目录盖过更精确的分类目录。"""
    directories = [
        TransferDirectoryConf(name="全局", library_path="/global"),
        TransferDirectoryConf(
            name="电视剧",
            media_type="tv",
            library_path="/tv",
        ),
        TransferDirectoryConf(
            name="路径",
            media_type="电视剧",
            media_category="动漫/日番",
            library_path="/legacy",
        ),
        TransferDirectoryConf(
            name="稳定ID",
            media_type="电视剧",
            media_category_id="tv.anime.jp",
            media_category="旧动漫/日番",
            library_path="/stable",
        ),
    ]
    monkeypatch.setattr(DirectoryHelper, "get_dirs", staticmethod(lambda: directories))

    selected = DirectoryHelper(directory_resolver).get_dir(
        _media(),
        include_unsorted=True,
    )

    assert selected is directories[3]


def test_directory_priority_applies_only_within_same_match_rank(
    directory_resolver: ClassificationCategoryResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分类精确度相同时才按数字优先级选择目录。"""
    low_priority = TransferDirectoryConf(
        name="低优先级",
        priority=20,
        media_type="tv",
        media_category_id="tv.anime.jp",
        library_path="/slow",
    )
    high_priority = TransferDirectoryConf(
        name="高优先级",
        priority=1,
        media_type="电视剧",
        media_category_id="tv.anime.jp",
        library_path="/fast",
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "get_dirs",
        staticmethod(lambda: [low_priority, high_priority]),
    )

    selected = DirectoryHelper(directory_resolver).get_dir(
        _media(),
        include_unsorted=True,
    )

    assert selected is high_priority


def test_directory_media_type_aliases_match_consistently(
    directory_resolver: ClassificationCategoryResolver,
) -> None:
    """中文媒体类型和 movie/tv/music 读侧别名应使用相同匹配语义。"""
    helper = DirectoryHelper(directory_resolver)
    media = _media(media_type=MediaType.TV)

    assert helper.matches_media(TransferDirectoryConf(media_type="tv"), media)
    assert helper.matches_media(TransferDirectoryConf(media_type="电视剧"), media)
    assert not helper.matches_media(TransferDirectoryConf(media_type="movie"), media)


def test_library_path_uses_current_multilevel_path_without_duplicate_type_root(
    directory_resolver: ClassificationCategoryResolver,
) -> None:
    """音乐旧快照含类型首段时，开启类型目录也只能生成一个音乐根。"""
    media = MusicInfo(
        media_source="musicbrainz",
        media_id="release-1",
        title="现场专辑",
        classification=ClassificationResult(
            effective=ClassificationSelection(
                category_id="music.live",
                category_path=["旧音乐", "现场"],
                source="automatic",
            )
        ),
    )
    directory = TransferDirectoryConf(
        library_path="/library",
        media_category_id="music.live",
        media_category="旧音乐/现场",
        library_type_folder=True,
        library_category_folder=True,
    )

    target = TransHandler.get_dest_dir(media, directory)

    assert target == Path("/library/音乐/现场")


def test_download_path_uses_safe_multilevel_automatic_category(
    directory_resolver: ClassificationCategoryResolver,
) -> None:
    """下载动态分类目录应使用当前策略路径并逐段追加。"""
    directory = TransferDirectoryConf(
        download_path="/downloads",
        download_type_folder=True,
        download_category_folder=True,
    )

    target = DownloadSubtitleOwner._append_download_classification(
        root_path=Path("/downloads"),
        dir_info=directory,
        media_info=_media(),
    )

    assert target == Path("/downloads/电视剧/动漫/日番")


def test_fixed_download_category_does_not_append_duplicate_folder(
    directory_resolver: ClassificationCategoryResolver,
) -> None:
    """固定分类下载根继续代表分类本身，不重复追加策略路径。"""
    directory = TransferDirectoryConf(
        download_path="/downloads/anime",
        media_type="tv",
        media_category_id="tv.anime.jp",
        media_category="旧动漫/日番",
        download_category_folder=True,
    )

    target = DownloadSubtitleOwner._append_download_classification(
        root_path=Path("/downloads/anime"),
        dir_info=directory,
        media_info=_media(),
    )

    assert target == Path("/downloads/anime")


def test_shared_download_roots_follow_policy_path_and_type_root_deduplication(
    directory_resolver: ClassificationCategoryResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史回查边界必须与音乐真实下载目录的多级路径完全一致。"""
    directory = TransferDirectoryConf(
        download_path="/downloads",
        download_type_folder=True,
        download_category_folder=True,
    )
    monkeypatch.setattr(
        DirectoryHelper,
        "get_download_dirs",
        lambda _self: [directory],
    )

    roots = TransferWorkflowOwner._get_shared_download_roots(
        Path("/downloads/音乐/现场/Concert.flac")
    )

    assert "/downloads/音乐" in roots
    assert "/downloads/音乐/现场" in roots
    assert "/downloads/音乐/音乐" not in roots
