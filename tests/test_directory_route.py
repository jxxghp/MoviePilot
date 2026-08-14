from pathlib import Path

from app.domain.context import MediaInfo
from app.services.directory import DirectoryHelper
from app.schemas import TransferDirectoryConf
from app.schemas.types import DirectoryMatchMode, MediaType, SystemConfigKey


def _directory(
        name: str,
        *,
        media_type: str | None = None,
        media_category: str | None = None,
        storage: str = "local",
        library_storage: str = "local",
        download_path: str = "/downloads",
        library_path: str | None = None,
) -> TransferDirectoryConf:
    """构造目录路由测试配置。"""
    return TransferDirectoryConf(
        name=name,
        storage=storage,
        library_storage=library_storage,
        download_path=download_path,
        library_path=library_path or f"/library/{name}",
        media_type=media_type,
        media_category=media_category,
        monitor_type="monitor",
    )


def _variety_media() -> MediaInfo:
    """构造已分类的综艺媒体。"""
    return MediaInfo(type=MediaType.TV, title="测试综艺", category="综艺")


def test_sequential_mode_preserves_first_eligible_directory(monkeypatch) -> None:
    """默认顺序模式必须保持通用目录先命中的现有行为。"""
    helper = DirectoryHelper()
    directories = [
        _directory("通用电视剧", media_type=MediaType.TV.value),
        _directory("综艺", media_type=MediaType.TV.value, media_category="综艺"),
    ]
    monkeypatch.setattr(helper, "get_dirs", lambda: directories)
    monkeypatch.setattr(
        "app.services.directory.SystemConfigOper",
        lambda: type("Config", (), {"get": lambda _self, key: None})(),
    )

    decision = helper.evaluate_route(media=_variety_media())

    assert decision.mode == DirectoryMatchMode.SEQUENTIAL
    assert decision.selected_index == 0
    assert decision.selected_directory.name == "通用电视剧"
    assert decision.candidates[0].selected is True
    assert decision.candidates[1].match_level == "category"
    assert any(warning.code == "generic_before_specific" for warning in decision.warnings)
    assert helper.get_dir(media=_variety_media()).name == "通用电视剧"


def test_specificity_mode_prefers_exact_category_and_keeps_tie_order(monkeypatch) -> None:
    """精确模式按类别、类型、通配排序，同级仍保持用户顺序。"""
    helper = DirectoryHelper()
    directories = [
        _directory("通用电视剧", media_type=MediaType.TV.value),
        _directory("综艺一", media_type=MediaType.TV.value, media_category="综艺"),
        _directory("综艺二", media_type=MediaType.TV.value, media_category="综艺"),
    ]
    monkeypatch.setattr(helper, "get_dirs", lambda: directories)

    decision = helper.evaluate_route(
        media=_variety_media(),
        match_mode=DirectoryMatchMode.SPECIFICITY,
    )

    assert decision.selected_index == 1
    assert decision.selected_directory.name == "综艺一"
    assert helper.get_dir(
        media=_variety_media(),
        match_mode=DirectoryMatchMode.SPECIFICITY,
    ).name == "综艺一"


def test_specificity_applies_after_storage_and_destination_constraints() -> None:
    """精确度不得越过源存储、目标存储和显式目标路径约束。"""
    helper = DirectoryHelper()
    directories = [
        _directory(
            "错误源存储精确目录",
            media_type=MediaType.TV.value,
            media_category="综艺",
            storage="alist",
        ),
        _directory(
            "错误目标精确目录",
            media_type=MediaType.TV.value,
            media_category="综艺",
            library_storage="u115",
        ),
        _directory(
            "正确通用目录",
            media_type=MediaType.TV.value,
            library_path="/library/target",
        ),
    ]

    decision = helper.evaluate_route(
        media=_variety_media(),
        directories=directories,
        storage="local",
        target_storage="local",
        dest_path=Path("/library/target"),
        match_mode=DirectoryMatchMode.SPECIFICITY,
    )

    assert decision.selected_directory.name == "正确通用目录"
    assert decision.candidates[0].eligible is False
    assert decision.candidates[0].reasons[0].code == "source_storage_mismatch"
    assert decision.candidates[1].reasons[0].code == "target_storage_mismatch"


def test_same_source_preference_precedes_specificity(monkeypatch) -> None:
    """同源候选池必须先于媒体精确度选择。"""
    helper = DirectoryHelper()
    directories = [
        _directory(
            "另一存储精确目录",
            media_type=MediaType.TV.value,
            media_category="综艺",
            download_path="/other",
        ),
        _directory(
            "同源通用目录",
            media_type=MediaType.TV.value,
            download_path="/downloads",
        ),
    ]
    monkeypatch.setattr(
        helper,
        "_is_same_source",
        lambda _src, target: target[0] == Path("/downloads"),
    )

    decision = helper.evaluate_route(
        media=_variety_media(),
        directories=directories,
        src_path=Path("/unmanaged/input/file.mkv"),
        match_mode=DirectoryMatchMode.SPECIFICITY,
    )

    assert decision.selected_directory.name == "同源通用目录"
    assert decision.candidates[1].same_source is True


def test_media_none_keeps_all_eligible_directories_in_original_order() -> None:
    """无媒体信息的手动整理不得因精确模式改变既有顺序。"""
    helper = DirectoryHelper()
    directories = [
        _directory("电影目录", media_type=MediaType.MOVIE.value),
        _directory("电视剧目录", media_type=MediaType.TV.value),
    ]

    decision = helper.evaluate_route(
        media=None,
        directories=directories,
        match_mode=DirectoryMatchMode.SPECIFICITY,
    )

    assert decision.selected_index == 0
    assert decision.selected_directory.name == "电影目录"
    assert [candidate.match_level for candidate in decision.candidates] == [
        "wildcard",
        "wildcard",
    ]


def test_get_dir_uses_persisted_specificity_mode(monkeypatch) -> None:
    """未显式传入模式时应读取持久化配置，非法值回退顺序模式。"""
    helper = DirectoryHelper()
    directories = [
        _directory("通用电视剧", media_type=MediaType.TV.value),
        _directory("综艺", media_type=MediaType.TV.value, media_category="综艺"),
    ]
    monkeypatch.setattr(helper, "get_dirs", lambda: directories)

    class Config:
        """返回测试中的目录匹配模式。"""

        value = DirectoryMatchMode.SPECIFICITY.value

        def get(self, key):
            """读取匹配模式配置。"""
            assert key is SystemConfigKey.DirectoryMatchMode
            return self.value

    config = Config()
    monkeypatch.setattr("app.services.directory.SystemConfigOper", lambda: config)

    assert helper.get_dir(media=_variety_media()).name == "综艺"
    config.value = "invalid"
    assert helper.get_dir(media=_variety_media()).name == "通用电视剧"


def test_download_save_root_uses_same_specificity_order(monkeypatch) -> None:
    """精确保存根目录存在多条规则时应与最终目录使用同一精确度语义。"""
    helper = DirectoryHelper()
    directories = [
        _directory("通用电视剧", media_type=MediaType.TV.value),
        _directory("综艺", media_type=MediaType.TV.value, media_category="综艺"),
    ]
    monkeypatch.setattr(helper, "get_download_dirs", lambda: directories)

    selected = helper.get_download_dir_by_save_path(
        media=_variety_media(),
        save_path="/downloads",
        match_mode=DirectoryMatchMode.SPECIFICITY,
    )

    assert selected.name == "综艺"


def test_route_diagnostics_report_all_hard_constraint_failures() -> None:
    """单个候选同时违反多项硬约束时应完整返回所有原因。"""
    helper = DirectoryHelper()
    directory = _directory(
        "错误目录",
        media_type=MediaType.TV.value,
        storage="alist",
        library_storage="u115",
        library_path="/library/other",
    )
    directory.monitor_type = None

    decision = helper.evaluate_route(
        media=_variety_media(),
        directories=[directory],
        storage="local",
        target_storage="local",
        dest_path=Path("/library/target"),
    )

    assert [reason.code for reason in decision.candidates[0].reasons] == [
        "monitor_disabled",
        "source_storage_mismatch",
        "target_storage_mismatch",
        "destination_path_mismatch",
    ]


def test_route_diagnostics_warn_for_unknown_duplicate_and_no_match() -> None:
    """目录草稿中的无效类别、冲突条件和空候选应同时提示。"""
    helper = DirectoryHelper()
    directories = [
        _directory(
            "错误一",
            media_type=MediaType.TV.value,
            media_category="不存在的分类",
            library_path="/library/one",
        ),
        _directory(
            "错误二",
            media_type=MediaType.TV.value,
            media_category="不存在的分类",
            library_path="/library/two",
        ),
    ]

    decision = helper.evaluate_route(
        media=_variety_media(),
        directories=directories,
        valid_categories=["综艺"],
    )

    warning_codes = {warning.code for warning in decision.warnings}
    assert warning_codes == {
        "no_matching_directory",
        "unknown_media_category",
        "duplicate_directory_conditions",
    }
    assert all(candidate.eligible is False for candidate in decision.candidates)


def test_unknown_category_warning_ignores_other_media_types() -> None:
    """当前电视剧预览不得误报电影目录使用的电影分类。"""
    helper = DirectoryHelper()
    directories = [
        _directory(
            "电影分类",
            media_type=MediaType.MOVIE.value,
            media_category="华语电影",
        ),
        _directory("综艺", media_type=MediaType.TV.value, media_category="综艺"),
    ]

    decision = helper.evaluate_route(
        media=_variety_media(),
        directories=directories,
        valid_categories=["综艺"],
    )

    assert not any(
        warning.code == "unknown_media_category"
        for warning in decision.warnings
    )
