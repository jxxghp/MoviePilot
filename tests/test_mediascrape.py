"""
验证媒体刮削的文件路径、元数据策略、图片别名和事件分发。
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# 使用导入期替身隔离站点与配置依赖，因此导入位置受控。
# 测试先准备环境与依赖替身，再导入刮削模块。
# ruff: noqa: E402
from app.testing import stub_modules

# 仅在 import 期用假模块替换依赖，退出 with 后还原，避免污染后续测试的 sys.modules
_systemconfig_stub = MagicMock()
_systemconfig_stub.SystemConfigOper.return_value.get.return_value = None
with stub_modules(
    {
        "app.application.site.sites": MagicMock(),
        "app.db.oper.systemconfig": _systemconfig_stub,
    }
):
    from app.chain.scraping import ScrapingChain, ScrapingConfig, ScrapingOption
    from app.domain.context import MediaInfo
    from app.domain.metainfo import MetaInfo
    from app.runtime.events import Event
    from app.schemas.file import FileItem
    from app.schemas.types import (
        EventType,
        MediaType,
        ScrapingMetadata,
        ScrapingPolicy,
        ScrapingTarget,
    )


@pytest.fixture
def _isolated_scraping_chain():
    """
    暂时移出已有刮削链单例，用例结束后恢复原实例，避免替身泄漏。

    :yields: 每个用例独立创建的刮削链
    """
    key = (ScrapingChain, (), frozenset())
    original = ScrapingChain._instances.pop(key, None)
    try:
        yield ScrapingChain()
    finally:
        ScrapingChain._instances.pop(key, None)
        if original is not None:
            ScrapingChain._instances[key] = original


@pytest.fixture
def path_context(_isolated_scraping_chain):
    """
    提供用于元数据路径推导的存储替身。

    :return: 当前测试组所需的刮削上下文
    """
    path_context = SimpleNamespace()
    path_context.media_chain = _isolated_scraping_chain
    path_context.media_chain.storagechain = MagicMock()

    return path_context


@pytest.fixture
def nfo_context(_isolated_scraping_chain):
    """
    提供元数据生成、保存和刮削策略的独立替身。

    :return: 当前测试组所需的刮削上下文
    """
    nfo_context = SimpleNamespace()
    nfo_context.media_chain = _isolated_scraping_chain
    nfo_context.media_chain.storagechain = MagicMock()
    nfo_context.media_chain.metadata_nfo = MagicMock(return_value="<nfo></nfo>")
    nfo_context.media_chain._save_file = MagicMock()
    nfo_context.media_chain.scraping_policies = MagicMock()

    nfo_context.fileitem = FileItem(path="/movies/Avatar (2009)", name="Avatar (2009)", type="dir", storage="local")
    nfo_context.meta = MetaInfo("Avatar (2009)")
    nfo_context.mediainfo = MediaInfo()

    return nfo_context


@pytest.fixture
def image_context(_isolated_scraping_chain):
    """
    提供图片生成与下载替身，并保留真实下载方法用于定向验证。

    :return: 当前测试组所需的刮削上下文
    """
    image_context = SimpleNamespace()
    image_context.media_chain = _isolated_scraping_chain
    image_context.original_download = image_context.media_chain._download_and_save_image
    image_context.media_chain.storagechain = MagicMock()
    image_context.media_chain.metadata_img = MagicMock()
    image_context.media_chain._download_and_save_image = MagicMock()
    image_context.media_chain.scraping_policies = MagicMock()

    return image_context


@pytest.fixture
def tv_context(_isolated_scraping_chain):
    """
    提供电视剧目录初始化所需的元数据和图片刮削替身。

    :return: 当前测试组所需的刮削上下文
    """
    tv_context = SimpleNamespace()
    tv_context.media_chain = _isolated_scraping_chain
    tv_context.media_chain.storagechain = MagicMock()
    tv_context.media_chain._scrape_nfo_generic = MagicMock()
    tv_context.media_chain._scrape_images_generic = MagicMock()

    return tv_context


@pytest.fixture
def event_context(_isolated_scraping_chain):
    """
    提供刮削事件分发所需的独立链与存储替身。

    :return: 当前测试组所需的刮削上下文
    """
    event_context = SimpleNamespace()
    event_context.media_chain = _isolated_scraping_chain
    event_context.media_chain.storagechain = MagicMock()

    return event_context


def test_movie_file_nfo_path(path_context) -> None:
    """
    电影文件的元数据使用同名文件并写入父目录。
    """
    fileitem = FileItem(path="/movies/avatar.mkv", name="avatar.mkv", type="file", storage="local")
    parent_item = FileItem(path="/movies", name="movies", type="dir", storage="local")
    path_context.media_chain.storagechain.get_parent_item.return_value = parent_item

    target_item, target_path = path_context.media_chain._get_target_fileitem_and_path(
        current_fileitem=fileitem, item_type=ScrapingTarget.MOVIE, metadata_type=ScrapingMetadata.NFO
    )
    assert target_item == parent_item
    assert target_path == Path("/movies/avatar.nfo")


def test_scraping_config_does_not_share_policy_state_between_instances(path_context) -> None:
    """
    刮削配置实例之间不应共享已删除或覆盖过的策略。
    """
    first_config = ScrapingConfig({"movie_nfo": ScrapingPolicy.SKIP})
    second_config = ScrapingConfig({})

    assert ScrapingPolicy.SKIP == first_config.option(ScrapingTarget.MOVIE, ScrapingMetadata.NFO).policy
    assert ScrapingPolicy.MISSINGONLY == second_config.option(ScrapingTarget.MOVIE, ScrapingMetadata.NFO).policy


def test_movie_dir_nfo_path(path_context) -> None:
    """
    电影目录的元数据使用目录名称。
    """
    fileitem = FileItem(path="/movies/Avatar (2009)", name="Avatar (2009)", type="dir", storage="local")

    target_item, target_path = path_context.media_chain._get_target_fileitem_and_path(
        current_fileitem=fileitem, item_type=ScrapingTarget.MOVIE, metadata_type=ScrapingMetadata.NFO
    )
    assert target_item == fileitem
    assert target_path == Path("/movies/Avatar (2009)/Avatar (2009).nfo")


def test_tv_dir_nfo_path(path_context) -> None:
    """
    剧集目录的元数据使用 tvshow.nfo。
    """
    fileitem = FileItem(path="/tv/Show", name="Show", type="dir", storage="local")
    target_item, target_path = path_context.media_chain._get_target_fileitem_and_path(
        current_fileitem=fileitem, item_type=ScrapingTarget.TV, metadata_type=ScrapingMetadata.NFO
    )
    assert target_item == fileitem
    assert target_path == Path("/tv/Show/tvshow.nfo")


def test_season_dir_nfo_path(path_context) -> None:
    """
    季目录的元数据使用 season.nfo。
    """
    fileitem = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    target_item, target_path = path_context.media_chain._get_target_fileitem_and_path(
        current_fileitem=fileitem, item_type=ScrapingTarget.SEASON, metadata_type=ScrapingMetadata.NFO
    )
    assert target_item == fileitem
    assert target_path == Path("/tv/Show/Season 1/season.nfo")


def test_season_dir_poster_path(path_context) -> None:
    """
    季海报在季目录内使用统一的 poster.jpg 名称。
    """
    fileitem = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    target_item, target_path = path_context.media_chain._get_target_fileitem_and_path(
        current_fileitem=fileitem,
        item_type=ScrapingTarget.SEASON,
        metadata_type=ScrapingMetadata.POSTER,
        filename_hint="season01-poster.jpg",
    )
    assert target_item == fileitem
    assert target_path == Path("/tv/Show/Season 1/poster.jpg")


def test_season_dir_poster_paths_include_root_and_season_dir(path_context) -> None:
    """
    季海报应同时写剧集根目录和季目录，兼容不同媒体库。
    """
    parent_item = FileItem(path="/tv/Show", name="Show", type="dir", storage="local")
    fileitem = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    targets = path_context.media_chain._get_target_fileitems_and_paths(
        current_fileitem=fileitem,
        item_type=ScrapingTarget.SEASON,
        metadata_type=ScrapingMetadata.POSTER,
        filename_hint="season01-poster.jpg",
        parent_fileitem=parent_item,
    )
    assert targets == [
        (parent_item, Path("/tv/Show/season01-poster.jpg")),
        (fileitem, Path("/tv/Show/Season 1/poster.jpg")),
    ]


def test_season_dir_specials_poster_path(path_context) -> None:
    """
    特别篇海报在特别篇目录内使用 poster.jpg。
    """
    fileitem = FileItem(path="/tv/Show/Specials", name="Specials", type="dir", storage="local")
    target_item, target_path = path_context.media_chain._get_target_fileitem_and_path(
        current_fileitem=fileitem,
        item_type=ScrapingTarget.SEASON,
        metadata_type=ScrapingMetadata.POSTER,
        filename_hint="season-specials-poster.jpg",
    )
    assert target_item == fileitem
    assert target_path == Path("/tv/Show/Specials/poster.jpg")


@pytest.mark.parametrize("write_root", [True, False])
@pytest.mark.parametrize("season_name", ["season01", "season-specials"])
@pytest.mark.parametrize("image_type", ["poster", "banner", "thumb", "backdrop", "landscape"])
def test_season_image_root_switch(path_context, write_root, season_name, image_type) -> None:
    """
    根目录开关覆盖普通季、特别篇及各类季图片，保留季目录文件名和扩展名。
    """
    chain = path_context.media_chain
    chain.runtime_config = replace(chain.runtime_config, scrap_season_image_to_root=write_root)
    parent = FileItem(path="/tv/Show", name="Show", type="dir", storage="local")
    season = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    chain.storagechain.get_parent_item.return_value = parent
    filename = f"{season_name}-{image_type}.png"

    targets = chain._get_target_fileitems_and_paths(
        current_fileitem=season,
        item_type=ScrapingTarget.SEASON,
        metadata_type=ScrapingMetadata(image_type),
        filename_hint=filename,
    )

    expected = [(season, Path(season.path) / f"{image_type}.png")]
    if write_root:
        expected.insert(0, (parent, Path(parent.path) / filename))
        chain.storagechain.get_parent_item.assert_called_once_with(season)
    else:
        chain.storagechain.get_parent_item.assert_not_called()
    assert targets == expected


def test_movie_file_image_path_uses_parent_dir(path_context) -> None:
    """
    直接刮削电影文件时，图片应保存到父目录。
    """
    fileitem = FileItem(path="/movies/Avatar/Avatar.mkv", name="Avatar.mkv", type="file", storage="local")
    parent_item = FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
    target_item, target_path = path_context.media_chain._get_target_fileitem_and_path(
        current_fileitem=fileitem,
        item_type=ScrapingTarget.MOVIE,
        metadata_type=ScrapingMetadata.POSTER,
        filename_hint="poster.jpg",
        parent_fileitem=parent_item,
    )
    assert target_item == parent_item
    assert target_path == Path("/movies/Avatar/poster.jpg")


def test_episode_file_nfo_path(path_context) -> None:
    """
    单集元数据与视频同名并写入父目录。
    """
    fileitem = FileItem(path="/tv/Show/Season 1/S01E01.mp4", name="S01E01.mp4", type="file", storage="local")
    parent_item = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    path_context.media_chain.storagechain.get_parent_item.return_value = parent_item
    target_item, target_path = path_context.media_chain._get_target_fileitem_and_path(
        current_fileitem=fileitem, item_type=ScrapingTarget.EPISODE, metadata_type=ScrapingMetadata.NFO
    )
    assert target_item == parent_item
    assert target_path == Path("/tv/Show/Season 1/S01E01.nfo")


def test_scrape_nfo_off(nfo_context) -> None:
    """
    跳过策略不生成或保存元数据。
    """
    nfo_context.media_chain.scraping_policies.option.return_value = ScrapingOption("movie", "nfo", ScrapingPolicy.SKIP)
    nfo_context.media_chain._scrape_nfo_generic(
        nfo_context.fileitem, nfo_context.meta, nfo_context.mediainfo, ScrapingTarget.MOVIE
    )
    nfo_context.media_chain.metadata_nfo.assert_not_called()
    nfo_context.media_chain._save_file.assert_not_called()


def test_scrape_nfo_on_exists_skip(nfo_context) -> None:
    """
    仅补缺策略保留已有元数据文件。
    """
    nfo_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "movie", "nfo", ScrapingPolicy.MISSINGONLY
    )
    # 模拟已有元数据文件。
    nfo_context.media_chain.storagechain.get_file_item.return_value = FileItem(
        path="/movies/Avatar (2009)/Avatar (2009).nfo", name="Avatar (2009).nfo", type="file", storage="local"
    )

    nfo_context.media_chain._scrape_nfo_generic(
        nfo_context.fileitem, nfo_context.meta, nfo_context.mediainfo, ScrapingTarget.MOVIE
    )
    nfo_context.media_chain.metadata_nfo.assert_not_called()
    nfo_context.media_chain._save_file.assert_not_called()


def test_scrape_nfo_on_not_exists_scrape(nfo_context) -> None:
    """
    仅补缺策略在元数据文件不存在时生成并保存。
    """
    nfo_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "movie", "nfo", ScrapingPolicy.MISSINGONLY
    )
    # 模拟元数据文件缺失。
    nfo_context.media_chain.storagechain.get_file_item.return_value = None

    nfo_context.media_chain._scrape_nfo_generic(
        nfo_context.fileitem, nfo_context.meta, nfo_context.mediainfo, ScrapingTarget.MOVIE
    )
    nfo_context.media_chain.metadata_nfo.assert_called_once()
    nfo_context.media_chain._save_file.assert_called_once()


def test_scrape_nfo_overwrite_exists_scrape(nfo_context) -> None:
    """
    覆盖策略重新生成并保存已有元数据。
    """
    nfo_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "movie", "nfo", ScrapingPolicy.OVERWRITE
    )
    # 模拟已有元数据文件。
    nfo_context.media_chain.storagechain.get_file_item.return_value = FileItem(
        path="/movies/Avatar (2009)/Avatar (2009).nfo", name="Avatar (2009).nfo", type="file", storage="local"
    )

    nfo_context.media_chain._scrape_nfo_generic(
        nfo_context.fileitem, nfo_context.meta, nfo_context.mediainfo, ScrapingTarget.MOVIE
    )
    nfo_context.media_chain.metadata_nfo.assert_called_once()
    nfo_context.media_chain._save_file.assert_called_once()


def test_scrape_images_mapping(image_context) -> None:
    """
    图片类型映射及背景图别名均生成对应下载目标。
    """
    fileitem = FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
    mediainfo = MediaInfo()
    image_context.media_chain.metadata_img.return_value = {
        "poster.jpg": "http://poster",
        "fanart.jpg": "http://fanart",
        "logo.png": "http://logo",
    }
    image_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "movie", "poster", ScrapingPolicy.OVERWRITE
    )
    image_context.media_chain.storagechain.get_file_item.return_value = None

    image_context.media_chain._scrape_images_generic(fileitem, mediainfo, ScrapingTarget.MOVIE)

    # 图片类型映射与别名都应触发下载。
    calls = image_context.media_chain._download_and_save_image.call_args_list
    urls = [call.kwargs["url"] for call in calls]
    paths = [call.kwargs["path"] for call in calls]
    assert "http://poster" in urls
    assert "http://fanart" in urls
    assert "http://logo" in urls
    # fanart.jpg 同时生成 backdrop.jpg 别名。
    assert Path("/movies/Avatar/fanart.jpg") in paths
    assert Path("/movies/Avatar/backdrop.jpg") in paths


def test_scrape_images_season_filter(image_context) -> None:
    """
    只下载指定季的海报，并兼容剧集根目录和季目录。
    """
    fileitem = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    mediainfo = MediaInfo()
    image_context.media_chain.metadata_img.return_value = {
        "season01-poster.jpg": "http://season01",
        "season02-poster.jpg": "http://season02",
    }
    image_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "season", "poster", ScrapingPolicy.OVERWRITE
    )
    image_context.media_chain.storagechain.get_file_item.return_value = None

    image_context.media_chain._scrape_images_generic(fileitem, mediainfo, ScrapingTarget.SEASON, season_number=1)

    calls = image_context.media_chain._download_and_save_image.call_args_list
    assert len(calls) == 2
    assert all((call.kwargs["url"] == "http://season01" for call in calls))
    assert [call.kwargs["path"] for call in calls] == [
        Path("/tv/Show/season01-poster.jpg"),
        Path("/tv/Show/Season 1/poster.jpg"),
    ]


@pytest.mark.parametrize("write_root", [True, False])
@pytest.mark.parametrize("root_exists,season_exists", [(False, False), (True, False), (False, True), (True, True)])
def test_season_image_missing_only_checks_each_target(image_context, write_root, root_exists, season_exists) -> None:
    """
    仅缺失策略逐个补全启用的保存位置，关闭根目录写入时不再查询或下载根目录图片。
    """
    chain = image_context.media_chain
    chain.runtime_config = replace(chain.runtime_config, scrap_season_image_to_root=write_root)
    season = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    parent = FileItem(path="/tv/Show", name="Show", type="dir", storage="local")
    root_path = Path("/tv/Show/season01-poster.jpg")
    season_path = Path("/tv/Show/Season 1/poster.jpg")
    existing = {root_path: root_exists, season_path: season_exists}
    chain.metadata_img.return_value = {"season01-poster.jpg": "http://season01"}
    chain.scraping_policies = ScrapingConfig({})
    chain.storagechain.get_file_item.side_effect = lambda *, storage, path: existing[path]

    chain._scrape_images_generic(
        season, MediaInfo(), ScrapingTarget.SEASON, parent_fileitem=parent, season_number=1
    )

    selected = [root_path, season_path] if write_root else [season_path]
    assert [call.kwargs["path"] for call in chain.storagechain.get_file_item.call_args_list] == selected
    assert [call.kwargs["path"] for call in chain._download_and_save_image.call_args_list] == [
        path for path in selected if not existing[path]
    ]


@pytest.mark.parametrize("season_number,filename", [(1, "season01-thumb.jpg"), (0, "season-specials-thumb.jpg")])
@pytest.mark.parametrize("policy", [ScrapingPolicy.SKIP, ScrapingPolicy.OVERWRITE])
def test_season_image_root_disabled_preserves_policy_and_aliases(image_context, season_number, filename, policy) -> None:
    """
    关闭根目录后仍遵循跳过和覆盖策略，特别篇与普通季均只在季目录扩展图片别名。
    """
    chain = image_context.media_chain
    chain.runtime_config = replace(chain.runtime_config, scrap_season_image_to_root=False)
    season = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    chain.metadata_img.return_value = {filename: "http://thumb"}
    chain.scraping_policies = ScrapingConfig({"season_thumb": policy})
    chain.storagechain.get_file_item.return_value = True

    chain._scrape_images_generic(season, MediaInfo(), ScrapingTarget.SEASON, season_number=season_number)

    paths = [call.kwargs["path"] for call in chain._download_and_save_image.call_args_list]
    assert paths == ([] if policy == ScrapingPolicy.SKIP else [
        Path("/tv/Show/Season 1/thumb.jpg"),
        Path("/tv/Show/Season 1/landscape.jpg"),
    ])


def test_scrape_movie_file_images_when_initialized_directly(image_context) -> None:
    """
    直接初始化刮削电影文件时，应生成同级 poster/backdrop 及别名。
    """
    fileitem = FileItem(path="/movies/Avatar/Avatar.mkv", name="Avatar.mkv", type="file", storage="local")
    parent_item = FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
    mediainfo = MediaInfo()
    image_context.media_chain.metadata_img.return_value = {
        "poster.jpg": "http://poster",
        "backdrop.jpg": "http://backdrop",
    }
    image_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "movie", "poster", ScrapingPolicy.OVERWRITE
    )
    image_context.media_chain.storagechain.get_file_item.return_value = None

    image_context.media_chain._scrape_images_generic(
        fileitem,
        mediainfo,
        ScrapingTarget.MOVIE,
        parent_fileitem=parent_item,
    )

    paths = [call.kwargs["path"] for call in image_context.media_chain._download_and_save_image.call_args_list]
    # 海报不生成别名，背景图生成 fanart 别名。
    assert Path("/movies/Avatar/poster.jpg") in paths
    assert Path("/movies/Avatar/backdrop.jpg") in paths
    assert Path("/movies/Avatar/fanart.jpg") in paths


def test_scrape_episode_thumb_image_path(image_context) -> None:
    """
    单集缩略图按集名称写入指定父目录。
    """
    fileitem = FileItem(path="/tv/Show/Season 1/S01E01.mp4", name="S01E01.mp4", type="file", storage="local")
    parent_item = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    mediainfo = MediaInfo()
    image_context.media_chain.metadata_img.return_value = {"thumb.jpg": "http://episode-thumb"}
    image_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "episode", "thumb", ScrapingPolicy.OVERWRITE
    )
    image_context.media_chain.storagechain.get_file_item.return_value = None

    image_context.media_chain._scrape_images_generic(
        fileitem, mediainfo, ScrapingTarget.EPISODE, parent_fileitem=parent_item, season_number=1, episode_number=1
    )

    image_context.media_chain.metadata_img.assert_called_once_with(mediainfo=mediainfo, season=1, episode=1)
    image_context.media_chain._download_and_save_image.assert_called_once_with(
        fileitem=parent_item, path=Path("/tv/Show/Season 1/S01E01.jpg"), url="http://episode-thumb"
    )


def test_scrape_episode_thumb_image_path_via_parent_lookup(image_context) -> None:
    """
    未传父目录时通过存储接口查找单集缩略图保存位置。
    """
    fileitem = FileItem(path="/tv/Show/Season 1/S01E01.mp4", name="S01E01.mp4", type="file", storage="local")
    parent_item = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    mediainfo = MediaInfo()
    image_context.media_chain.metadata_img.return_value = {"thumb.jpg": "http://episode-thumb"}
    image_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "episode", "thumb", ScrapingPolicy.OVERWRITE
    )
    image_context.media_chain.storagechain.get_parent_item.return_value = parent_item
    image_context.media_chain.storagechain.get_file_item.return_value = None

    image_context.media_chain._scrape_images_generic(
        fileitem, mediainfo, ScrapingTarget.EPISODE, season_number=1, episode_number=1
    )

    image_context.media_chain.storagechain.get_parent_item.assert_called_once_with(fileitem)
    image_context.media_chain._download_and_save_image.assert_called_once_with(
        fileitem=parent_item, path=Path("/tv/Show/Season 1/S01E01.jpg"), url="http://episode-thumb"
    )


def test_expand_with_aliases_backdrop(image_context) -> None:
    """
    背景图同时生成 fanart 图片别名。
    """
    parent_item = FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
    targets = [(parent_item, Path("/movies/Avatar/backdrop.jpg"))]
    image_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "movie", "backdrop", ScrapingPolicy.OVERWRITE
    )

    expanded = image_context.media_chain._expand_with_aliases(targets, ScrapingTarget.MOVIE)
    paths = [t[1] for t in expanded]
    assert Path("/movies/Avatar/backdrop.jpg") in paths
    assert Path("/movies/Avatar/fanart.jpg") in paths


def test_expand_with_aliases_thumb(image_context) -> None:
    """
    缩略图同时生成 landscape 图片别名。
    """
    parent_item = FileItem(path="/tv/Show", name="Show", type="dir", storage="local")
    targets = [(parent_item, Path("/tv/Show/thumb.jpg"))]
    image_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "tv", "thumb", ScrapingPolicy.OVERWRITE
    )

    expanded = image_context.media_chain._expand_with_aliases(targets, ScrapingTarget.TV)
    paths = [t[1] for t in expanded]
    assert Path("/tv/Show/thumb.jpg") in paths
    assert Path("/tv/Show/landscape.jpg") in paths


def test_expand_with_aliases_skips_season_prefix(image_context) -> None:
    """
    带季编号前缀的图片不生成别名。
    """
    parent_item = FileItem(path="/tv/Show", name="Show", type="dir", storage="local")
    targets = [(parent_item, Path("/tv/Show/season01-thumb.jpg"))]
    image_context.media_chain.scraping_policies.option.return_value = ScrapingOption(
        "season", "thumb", ScrapingPolicy.OVERWRITE
    )

    expanded = image_context.media_chain._expand_with_aliases(targets, ScrapingTarget.SEASON)
    assert len(expanded) == 1


def test_expand_with_aliases_respects_skip_policy(image_context) -> None:
    """
    别名对应的元数据策略为跳过时，不新增别名目标。
    """
    parent_item = FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
    targets = [(parent_item, Path("/movies/Avatar/backdrop.jpg"))]
    # 下方策略替身将背景图设为跳过，以验证不生成 fanart 别名。

    def option_side_effect(item_type, metadata_type):
        """
        模拟背景图跳过策略，验证别名同样受策略约束。
        """
        if metadata_type == ScrapingMetadata.BACKDROP:
            return ScrapingOption("movie", "backdrop", ScrapingPolicy.SKIP)
        return ScrapingOption("movie", "backdrop", ScrapingPolicy.OVERWRITE)

    image_context.media_chain.scraping_policies.option.side_effect = option_side_effect

    expanded = image_context.media_chain._expand_with_aliases(targets, ScrapingTarget.MOVIE)
    # fanart 映射为背景图类型，跳过策略阻止生成别名。
    assert len(expanded) == 1


def test_season_backdrop_path(image_context) -> None:
    """
    季背景图保存到季目录。
    """
    fileitem = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    target_item, target_path = image_context.media_chain._get_target_fileitem_and_path(
        current_fileitem=fileitem,
        item_type=ScrapingTarget.SEASON,
        metadata_type=ScrapingMetadata.BACKDROP,
        filename_hint="season01-backdrop.jpg",
    )
    assert target_item == fileitem
    assert target_path == Path("/tv/Show/Season 1/backdrop.jpg")


@patch("app.chain.scraping._scraping_http_snapshot")
@patch("app.chain.scraping.NamedTemporaryFile")
@patch("app.chain.scraping.Path.chmod")
def test_download_and_save_image(mock_chmod, mock_temp_file, mock_request_utils, image_context) -> None:
    # 恢复真实下载方法，只替换网络和文件系统边界。
    """
    下载图片时分块写入临时文件，再通过存储接口上传。
    """
    image_context.media_chain = ScrapingChain()
    image_context.media_chain._download_and_save_image = image_context.original_download
    image_context.media_chain.storagechain = MagicMock()

    fileitem = FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
    target_path = Path("/movies/Avatar/poster.jpg")
    url = "http://poster"

    # 模拟临时文件，避免写入真实目标路径。
    tmp_mock = MagicMock()
    tmp_mock.name = "/tmp/mockfile"
    mock_temp_file.return_value.__enter__.return_value = tmp_mock

    # 模拟分块响应，不发起真实网络请求。
    mock_stream = MagicMock()
    mock_stream.status_code = 200
    mock_stream.iter_content.return_value = [b"data1", b"data2"]

    mock_instance = mock_request_utils.return_value
    mock_instance.get_stream.return_value.__enter__.return_value = mock_stream
    mock_instance.stream.return_value.__enter__.return_value = mock_stream

    image_context.media_chain.storagechain.upload_file.return_value = fileitem

    image_context.media_chain._download_and_save_image(fileitem, target_path, url)

    mock_instance.stream.assert_called_with(
        url,
        proxies=image_context.media_chain.runtime_config.proxy,
        ua=image_context.media_chain.runtime_config.normal_user_agent,
    )
    mock_temp_file.assert_called_once_with(delete=False, suffix=".jpg")
    tmp_mock.write.assert_any_call(b"data1")
    tmp_mock.write.assert_any_call(b"data2")
    mock_chmod.assert_called()
    image_context.media_chain.storagechain.upload_file.assert_called_once()
    call_args = image_context.media_chain.storagechain.upload_file.call_args.kwargs
    assert call_args["fileitem"] == fileitem
    assert call_args["new_name"] == "poster.jpg"


@patch("app.chain.scraping.NamedTemporaryFile")
@patch("app.chain.scraping.Path.chmod")
def test_save_file_uses_python310_compatible_tempfile(mock_chmod, mock_temp_file, image_context) -> None:
    """
    保存刮削文件时不应使用 Python 3.12 才支持的 delete_on_close 参数。
    """
    image_context.media_chain = ScrapingChain()
    image_context.media_chain.storagechain = MagicMock()
    image_context.media_chain._cleanup_temp_file = MagicMock()

    fileitem = FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
    target_path = Path("/movies/Avatar/movie.nfo")

    tmp_mock = MagicMock()
    tmp_mock.name = "/tmp/mockfile"
    mock_temp_file.return_value.__enter__.return_value = tmp_mock
    image_context.media_chain.storagechain.upload_file.return_value = fileitem

    image_context.media_chain._save_file(fileitem, target_path, "<nfo></nfo>")

    mock_temp_file.assert_called_once_with(delete=False, suffix=".nfo")
    tmp_mock.write.assert_called_once_with(b"<nfo></nfo>")
    mock_chmod.assert_called()
    image_context.media_chain.storagechain.upload_file.assert_called_once()
    image_context.media_chain._cleanup_temp_file.assert_called_once_with(Path("/tmp/mockfile"))


def test_initialize_tv_directory_specials(tv_context) -> None:
    # 显式指定特别篇目录名称，避免依赖环境配置。
    """
    将配置的特别篇目录名识别为第零季并初始化元数据与图片。
    """
    tv_context.media_chain.runtime_config = replace(
        tv_context.media_chain.runtime_config,
        season_zero_names=("Specials", "SPs"),
    )

    fileitem = FileItem(path="/tv/Show/Specials", name="Specials", type="dir", storage="local")
    meta = MetaInfo("Show")
    mediainfo = MediaInfo(type=MediaType.TV)
    filepath = Path(fileitem.path)

    tv_context.media_chain._initialize_tv_directory_metadata(
        fileitem=fileitem,
        filepath=filepath,
        meta=meta,
        mediainfo=mediainfo,
        parent=None,
        overwrite=False,
    )

    tv_context.media_chain._scrape_nfo_generic.assert_called_once_with(
        current_fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        item_type=ScrapingTarget.SEASON,
        overwrite=False,
        season_number=0,
    )
    tv_context.media_chain._scrape_images_generic.assert_called_once_with(
        current_fileitem=fileitem,
        mediainfo=mediainfo,
        item_type=ScrapingTarget.SEASON,
        parent_fileitem=None,
        overwrite=False,
        season_number=0,
    )


def test_initialize_tv_directory_season(tv_context) -> None:
    """
    将普通季目录名识别为对应季编号。
    """
    tv_context.media_chain.runtime_config = replace(
        tv_context.media_chain.runtime_config,
        season_zero_names=("Specials", "SPs"),
    )

    fileitem = FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
    meta = MetaInfo("Show")
    mediainfo = MediaInfo(type=MediaType.TV)
    filepath = Path(fileitem.path)

    tv_context.media_chain._initialize_tv_directory_metadata(
        fileitem=fileitem,
        filepath=filepath,
        meta=meta,
        mediainfo=mediainfo,
        parent=None,
        overwrite=False,
    )

    tv_context.media_chain._scrape_nfo_generic.assert_called_once_with(
        current_fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        item_type=ScrapingTarget.SEASON,
        overwrite=False,
        season_number=1,
    )


@patch("app.chain.scraping.ScrapingChain.scrape_metadata")
def test_scrape_metadata_event_file(mock_scrape_metadata, event_context) -> None:
    """
    文件刮削事件携带父目录和覆盖开关调用刮削入口。
    """
    fileitem = FileItem(path="/movies/movie.mkv", name="movie.mkv", type="file", storage="local")
    parent_item = FileItem(path="/movies", name="movies", type="dir", storage="local")

    event_context.media_chain.storagechain.get_item.return_value = fileitem
    event_context.media_chain.storagechain.get_parent_item.return_value = parent_item

    mediainfo = MediaInfo()
    event = Event(
        event_type=EventType.MetadataScrape,
        event_data={"fileitem": fileitem, "mediainfo": mediainfo, "overwrite": True},
    )

    event_context.media_chain.scrape_metadata_event(event)

    mock_scrape_metadata.assert_called_once_with(
        fileitem=fileitem, mediainfo=mediainfo, init_folder=True, parent=parent_item, overwrite=True
    )


@patch("app.chain.scraping.ScrapingChain.scrape_metadata")
def test_scrape_metadata_event_dir_bluray(mock_scrape_metadata, event_context) -> None:
    """
    蓝光目录事件以原盘为整体刮削，不递归内部文件。
    """
    fileitem = FileItem(path="/movies/bluray_movie", name="bluray_movie", type="dir", storage="local")

    event_context.media_chain.storagechain.get_item.return_value = fileitem
    event_context.media_chain.storagechain.is_bluray_folder.return_value = True

    mediainfo = MediaInfo()
    event = Event(
        event_type=EventType.MetadataScrape,
        event_data={
            "fileitem": fileitem,
            "file_list": ["/movies/bluray_movie/BDMV/index.bdmv"],
            "mediainfo": mediainfo,
            "overwrite": False,
        },
    )

    event_context.media_chain.scrape_metadata_event(event)

    mock_scrape_metadata.assert_called_once_with(
        fileitem=fileitem, mediainfo=mediainfo, init_folder=True, recursive=False, overwrite=False
    )


@patch("app.chain.scraping.ScrapingChain.scrape_metadata")
def test_scrape_metadata_event_dir_with_filelist(mock_scrape_metadata, event_context) -> None:
    """
    限定文件列表的事件依次覆盖剧集根目录、季目录和单集。
    """
    fileitem = FileItem(path="/tv/show", name="show", type="dir", storage="local")

    event_context.media_chain.storagechain.get_item.return_value = fileitem
    event_context.media_chain.storagechain.is_bluray_folder.return_value = False

    def side_effect_get_file_item(storage, path):
        """
        用路径构造文件替身，按扩展名区分目录与媒体文件。
        """
        path_str = str(path)
        return FileItem(
            path=path_str, name=Path(path_str).name, type="dir" if "." not in path_str else "file", storage="local"
        )

    event_context.media_chain.storagechain.get_file_item.side_effect = side_effect_get_file_item

    mediainfo = MediaInfo()
    event = Event(
        event_type=EventType.MetadataScrape,
        event_data={
            "fileitem": fileitem,
            "file_list": ["/tv/show/Season 1/S01E01.mp4"],
            "mediainfo": mediainfo,
            "overwrite": True,
        },
    )

    event_context.media_chain.scrape_metadata_event(event)

    calls = mock_scrape_metadata.call_args_list
    assert len(calls) == 3

    paths = [call.kwargs["fileitem"].path for call in calls]
    assert "/tv/show" in paths
    assert "/tv/show/Season 1" in paths
    assert "/tv/show/Season 1/S01E01.mp4" in paths


@patch("app.chain.scraping.ScrapingChain.scrape_metadata")
def test_scrape_metadata_event_dir_full(mock_scrape_metadata, event_context) -> None:
    """
    完整目录事件保留已有识别信息并启用目录初始化。
    """
    fileitem = FileItem(path="/movies/movie", name="movie", type="dir", storage="local")

    event_context.media_chain.storagechain.get_item.return_value = fileitem

    mediainfo = MediaInfo()
    meta = MetaInfo("movie")
    event = Event(
        event_type=EventType.MetadataScrape,
        event_data={"fileitem": fileitem, "meta": meta, "mediainfo": mediainfo, "overwrite": True},
    )

    event_context.media_chain.scrape_metadata_event(event)

    mock_scrape_metadata.assert_called_once_with(
        fileitem=fileitem, meta=meta, mediainfo=mediainfo, init_folder=True, overwrite=True
    )


@patch("app.chain.scraping.ScrapingChain._handle_movie_scraping")
@patch("app.chain.media.MediaChain.recognize_by_meta")
def test_scrape_metadata_movie(mock_recognize, mock_handle_movie, event_context) -> None:
    """
    已有电影媒体信息时直接分发电影刮削，不重复识别。
    """
    fileitem = FileItem(path="/movies/movie.mkv", name="movie.mkv", type="file", storage="local")
    meta = MetaInfo("Movie")
    mediainfo = MediaInfo(type=MediaType.MOVIE)

    event_context.media_chain.scrape_metadata(
        fileitem=fileitem, meta=meta, mediainfo=mediainfo, init_folder=True, overwrite=False, recursive=True
    )

    mock_recognize.assert_not_called()
    mock_handle_movie.assert_called_once_with(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        init_folder=True,
        parent=None,
        overwrite=False,
        recursive=True,
    )


@patch("app.chain.scraping.ScrapingChain._handle_tv_scraping")
@patch("app.chain.media.MediaChain.recognize_by_meta")
def test_scrape_metadata_tv(mock_recognize, mock_handle_tv, event_context) -> None:
    """
    电视剧媒体信息分发到剧集刮削处理器。
    """
    fileitem = FileItem(path="/tv/show", name="show", type="dir", storage="local")
    meta = MetaInfo("Show")
    mediainfo = MediaInfo(type=MediaType.TV)

    event_context.media_chain.scrape_metadata(
        fileitem=fileitem, meta=meta, mediainfo=mediainfo, init_folder=True, overwrite=False, recursive=True
    )

    mock_handle_tv.assert_called_once_with(
        fileitem=fileitem,
        meta=meta,
        mediainfo=mediainfo,
        init_folder=True,
        parent=None,
        overwrite=False,
        recursive=True,
    )


@patch("app.chain.scraping.ScrapingChain._handle_movie_scraping")
@patch("app.chain.media.MediaChain.recognize_by_meta")
def test_scrape_metadata_recognize_fallback(mock_recognize, mock_handle_movie, event_context) -> None:
    """
    缺少媒体信息时先识别文件，再传递识别结果。
    """
    fileitem = FileItem(path="/movies/movie.mkv", name="movie.mkv", type="file", storage="local")
    mediainfo = MediaInfo(type=MediaType.MOVIE)
    mock_recognize.return_value = mediainfo

    event_context.media_chain.scrape_metadata(fileitem=fileitem, init_folder=True, overwrite=False, recursive=True)

    mock_recognize.assert_called_once()
    mock_handle_movie.assert_called_once()
    args, kwargs = mock_handle_movie.call_args
    assert kwargs["mediainfo"] == mediainfo
    assert kwargs["meta"].name == "Movie"


@patch("app.chain.scraping.ScrapingChain._handle_movie_scraping")
@patch("app.chain.scraping.ScrapingChain._handle_tv_scraping")
def test_scrape_metadata_invalid_extension(mock_handle_tv, mock_handle_movie, event_context) -> None:
    """
    非媒体扩展名不进入电影或剧集刮削。
    """
    fileitem = FileItem(path="/movies/movie.txt", name="movie.txt", type="file", storage="local")

    event_context.media_chain.scrape_metadata(fileitem=fileitem)

    mock_handle_movie.assert_not_called()
    mock_handle_tv.assert_not_called()


@patch("app.chain.scraping.ScrapingChain.scrape_metadata")
def test_scrape_metadata_event_dir_with_multiple_files(mock_scrape_metadata, event_context) -> None:
    """
    多文件目录事件刮削根目录及每个指定媒体文件。
    """
    fileitem = FileItem(path="/movies/collection", name="collection", type="dir", storage="local")

    event_context.media_chain.storagechain.get_item.return_value = fileitem
    event_context.media_chain.storagechain.is_bluray_folder.return_value = False

    def side_effect_get_file_item(storage, path):
        """
        用路径构造文件替身，按扩展名区分目录与媒体文件。
        """
        path_str = str(path)
        return FileItem(
            path=path_str, name=Path(path_str).name, type="dir" if "." not in path_str else "file", storage="local"
        )

    event_context.media_chain.storagechain.get_file_item.side_effect = side_effect_get_file_item

    mediainfo = MediaInfo()
    event = Event(
        event_type=EventType.MetadataScrape,
        event_data={
            "fileitem": fileitem,
            "file_list": [
                "/movies/collection/movie1.mp4",
                "/movies/collection/movie2.mkv",
                "/movies/collection/movie3.avi",
            ],
            "mediainfo": mediainfo,
            "overwrite": True,
        },
    )

    event_context.media_chain.scrape_metadata_event(event)

    calls = mock_scrape_metadata.call_args_list
    # 先刮削目录，再处理每个指定媒体文件。
    assert len(calls) == 4

    paths = [call.kwargs["fileitem"].path for call in calls]
    assert "/movies/collection" in paths
    assert "/movies/collection/movie1.mp4" in paths
    assert "/movies/collection/movie2.mkv" in paths
    assert "/movies/collection/movie3.avi" in paths


@patch("app.chain.scraping.ScrapingChain.scrape_metadata")
def test_scrape_metadata_event_dir_with_tv_multi_seasons_episodes(mock_scrape_metadata, event_context) -> None:
    """
    多季事件只各处理一次根目录、季目录与单集文件。
    """
    fileitem = FileItem(path="/tv/MultiSeasonShow", name="MultiSeasonShow", type="dir", storage="local")

    event_context.media_chain.storagechain.get_item.return_value = fileitem
    event_context.media_chain.storagechain.is_bluray_folder.return_value = False

    def side_effect_get_file_item(storage, path):
        """
        用路径构造文件替身，按扩展名区分目录与媒体文件。
        """
        path_str = str(path)
        return FileItem(
            path=path_str, name=Path(path_str).name, type="dir" if "." not in path_str else "file", storage="local"
        )

    event_context.media_chain.storagechain.get_file_item.side_effect = side_effect_get_file_item

    mediainfo = MediaInfo()
    event = Event(
        event_type=EventType.MetadataScrape,
        event_data={
            "fileitem": fileitem,
            "file_list": [
                "/tv/MultiSeasonShow/Season 1/S01E01.mp4",
                "/tv/MultiSeasonShow/Season 1/S01E02.mp4",
                "/tv/MultiSeasonShow/Season 2/S02E01.mkv",
                "/tv/MultiSeasonShow/Season 2/S02E02.mkv",
                "/tv/MultiSeasonShow/Specials/S00E01.mp4",
            ],
            "mediainfo": mediainfo,
            "overwrite": False,
        },
    )

    event_context.media_chain.scrape_metadata_event(event)

    calls = mock_scrape_metadata.call_args_list
    # 共处理一个根目录、三个季目录和五个单集文件。
    assert len(calls) == 9

    paths = [call.kwargs["fileitem"].path for call in calls]
    assert "/tv/MultiSeasonShow" in paths
    assert "/tv/MultiSeasonShow/Season 1" in paths
    assert "/tv/MultiSeasonShow/Season 2" in paths
    assert "/tv/MultiSeasonShow/Specials" in paths
    assert "/tv/MultiSeasonShow/Season 1/S01E01.mp4" in paths
    assert "/tv/MultiSeasonShow/Season 1/S01E02.mp4" in paths
    assert "/tv/MultiSeasonShow/Season 2/S02E01.mkv" in paths
    assert "/tv/MultiSeasonShow/Season 2/S02E02.mkv" in paths
    assert "/tv/MultiSeasonShow/Specials/S00E01.mp4" in paths


@patch("app.chain.media.MediaChain.recognize_by_meta")
def test_scrape_metadata_recognize_fail(mock_recognize, event_context) -> None:
    """
    媒体识别失败时记录警告并停止刮削。
    """
    fileitem = FileItem(path="/movies/movie.mkv", name="movie.mkv", type="file", storage="local")
    mock_recognize.return_value = None

    with patch("app.chain.scraping.logger.warn") as mock_logger:
        event_context.media_chain.scrape_metadata(fileitem=fileitem)
        mock_logger.assert_called_with(f"{Path(fileitem.path)} 无法识别文件媒体信息！")
