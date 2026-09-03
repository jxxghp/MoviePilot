"""TMDB 旧分类导入路径的只读兼容行为测试。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.domain.context import MediaInfo
from app.modules.themoviedb import category as category_module
from app.modules.themoviedb.category import CategoryHelper
from app.schemas.category import CategoryConfig
from app.schemas.types import MediaType


def test_category_helper_projects_active_policy_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧插件读取分类清单时应得到当前活动策略的电影和电视剧路径。"""
    paths = {
        MediaType.MOVIE: (("电影", "经典"),),
        MediaType.TV: (("电视剧", "动画"),),
    }
    resolver = SimpleNamespace(category_paths=lambda media_type: paths[media_type])
    monkeypatch.setattr(
        category_module,
        "classification_category_resolver_snapshot",
        lambda: resolver,
    )

    helper = CategoryHelper()
    config = helper.load()

    assert helper.movie_categorys == ["电影/经典"]
    assert helper.tv_categorys == ["电视剧/动画"]
    assert helper.is_movie_category is True
    assert helper.is_tv_category is True
    assert config == CategoryConfig.model_validate(
        {
            "movie": {"电影/经典": None},
            "tv": {"电视剧/动画": None},
        }
    )


def test_category_helper_classifies_copy_without_mutating_tmdb_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容分类调用应复用统一服务，并保持调用方 TMDB 字典不变。"""
    captured: list[MediaInfo] = []

    def classify(media: MediaInfo) -> MediaInfo:
        """记录统一分类入参并返回带目录分类的隔离副本。"""
        captured.append(media)
        classified = deepcopy(media)
        classified.set_library_category("电影/经典")
        return classified

    monkeypatch.setattr(category_module, "classify_media", classify)
    tmdb_info = {"id": 550, "title": "Fight Club"}

    category = CategoryHelper().get_movie_category(tmdb_info)

    assert category == "电影/经典"
    assert tmdb_info == {"id": 550, "title": "Fight Club"}
    assert len(captured) == 1
    assert captured[0].type is MediaType.MOVIE
    assert captured[0].tmdb_info is not tmdb_info


def test_category_helper_rejects_legacy_writes() -> None:
    """兼容门面不得再接受任何 category.yaml 写入请求。"""
    helper = CategoryHelper()

    helper.init()

    assert helper.save(CategoryConfig()) is False
