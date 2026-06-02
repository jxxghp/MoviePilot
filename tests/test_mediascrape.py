import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
# ruff: noqa: E402
from app.testing import stub_modules

# 仅在 import 期用假模块替换依赖，退出 with 后还原，避免污染后续测试的 sys.modules
_systemconfig_stub = MagicMock()
_systemconfig_stub.SystemConfigOper.return_value.get.return_value = None
with stub_modules({
    'app.helper.sites': MagicMock(),
    'app.db.systemconfig_oper': _systemconfig_stub,
}):
    from app import schemas
    from app.chain.media import MediaChain, ScrapingConfig, ScrapingOption
    from app.core.context import MediaInfo
    from app.core.event import Event
    from app.core.metainfo import MetaInfo
    from app.schemas.types import EventType, MediaType, ScrapingTarget, ScrapingMetadata, ScrapingPolicy


def reset_media_chain_singleton():
    """清理 MediaChain 单例，避免测试间复用被 mock 的实例。"""
    MediaChain._instances.pop((MediaChain, (), frozenset()), None)


class TestMediaScrapingPaths(unittest.TestCase):
    def setUp(self):
        reset_media_chain_singleton()
        self.media_chain = MediaChain()
        self.media_chain.storagechain = MagicMock()

    def tearDown(self):
        reset_media_chain_singleton()

    def test_movie_file_nfo_path(self):
        fileitem = schemas.FileItem(path="/movies/avatar.mkv", name="avatar.mkv", type="file", storage="local")
        parent_item = schemas.FileItem(path="/movies", name="movies", type="dir", storage="local")
        self.media_chain.storagechain.get_parent_item.return_value = parent_item

        target_item, target_path = self.media_chain._get_target_fileitem_and_path(
            current_fileitem=fileitem,
            item_type=ScrapingTarget.MOVIE,
            metadata_type=ScrapingMetadata.NFO
        )
        self.assertEqual(target_item, parent_item)
        self.assertEqual(target_path, Path("/movies/avatar.nfo"))

    def test_scraping_config_does_not_share_policy_state_between_instances(self):
        """刮削配置实例之间不应共享已删除或覆盖过的策略。"""
        first_config = ScrapingConfig({"movie_nfo": ScrapingPolicy.SKIP})
        second_config = ScrapingConfig({})

        self.assertEqual(
            ScrapingPolicy.SKIP,
            first_config.option(ScrapingTarget.MOVIE, ScrapingMetadata.NFO).policy,
        )
        self.assertEqual(
            ScrapingPolicy.MISSINGONLY,
            second_config.option(ScrapingTarget.MOVIE, ScrapingMetadata.NFO).policy,
        )

    def test_movie_dir_nfo_path(self):
        fileitem = schemas.FileItem(path="/movies/Avatar (2009)", name="Avatar (2009)", type="dir", storage="local")

        target_item, target_path = self.media_chain._get_target_fileitem_and_path(
            current_fileitem=fileitem,
            item_type=ScrapingTarget.MOVIE,
            metadata_type=ScrapingMetadata.NFO
        )
        self.assertEqual(target_item, fileitem)
        self.assertEqual(target_path, Path("/movies/Avatar (2009)/Avatar (2009).nfo"))

    def test_tv_dir_nfo_path(self):
        fileitem = schemas.FileItem(path="/tv/Show", name="Show", type="dir", storage="local")
        target_item, target_path = self.media_chain._get_target_fileitem_and_path(
            current_fileitem=fileitem,
            item_type=ScrapingTarget.TV,
            metadata_type=ScrapingMetadata.NFO
        )
        self.assertEqual(target_item, fileitem)
        self.assertEqual(target_path, Path("/tv/Show/tvshow.nfo"))

    def test_season_dir_nfo_path(self):
        fileitem = schemas.FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
        target_item, target_path = self.media_chain._get_target_fileitem_and_path(
            current_fileitem=fileitem,
            item_type=ScrapingTarget.SEASON,
            metadata_type=ScrapingMetadata.NFO
        )
        self.assertEqual(target_item, fileitem)
        self.assertEqual(target_path, Path("/tv/Show/Season 1/season.nfo"))

    def test_season_dir_poster_path(self):
        fileitem = schemas.FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
        target_item, target_path = self.media_chain._get_target_fileitem_and_path(
            current_fileitem=fileitem,
            item_type=ScrapingTarget.SEASON,
            metadata_type=ScrapingMetadata.POSTER,
            filename_hint="season01-poster.jpg"
        )
        self.assertEqual(target_item, fileitem)
        self.assertEqual(target_path, Path("/tv/Show/Season 1/poster.jpg"))

    def test_season_dir_poster_paths_include_root_and_season_dir(self):
        """季海报应同时写剧集根目录和季目录，兼容不同媒体库。"""
        parent_item = schemas.FileItem(path="/tv/Show", name="Show", type="dir", storage="local")
        fileitem = schemas.FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
        targets = self.media_chain._get_target_fileitems_and_paths(
            current_fileitem=fileitem,
            item_type=ScrapingTarget.SEASON,
            metadata_type=ScrapingMetadata.POSTER,
            filename_hint="season01-poster.jpg",
            parent_fileitem=parent_item,
        )
        self.assertEqual(
            targets,
            [
                (parent_item, Path("/tv/Show/season01-poster.jpg")),
                (fileitem, Path("/tv/Show/Season 1/poster.jpg")),
            ],
        )

    def test_season_dir_specials_poster_path(self):
        fileitem = schemas.FileItem(path="/tv/Show/Specials", name="Specials", type="dir", storage="local")
        target_item, target_path = self.media_chain._get_target_fileitem_and_path(
            current_fileitem=fileitem,
            item_type=ScrapingTarget.SEASON,
            metadata_type=ScrapingMetadata.POSTER,
            filename_hint="season-specials-poster.jpg"
        )
        self.assertEqual(target_item, fileitem)
        self.assertEqual(target_path, Path("/tv/Show/Specials/poster.jpg"))

    def test_movie_file_image_path_uses_parent_dir(self):
        """直接刮削电影文件时，图片应保存到父目录。"""
        fileitem = schemas.FileItem(path="/movies/Avatar/Avatar.mkv", name="Avatar.mkv", type="file", storage="local")
        parent_item = schemas.FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
        target_item, target_path = self.media_chain._get_target_fileitem_and_path(
            current_fileitem=fileitem,
            item_type=ScrapingTarget.MOVIE,
            metadata_type=ScrapingMetadata.POSTER,
            filename_hint="poster.jpg",
            parent_fileitem=parent_item,
        )
        self.assertEqual(target_item, parent_item)
        self.assertEqual(target_path, Path("/movies/Avatar/poster.jpg"))

    def test_episode_file_nfo_path(self):
        fileitem = schemas.FileItem(path="/tv/Show/Season 1/S01E01.mp4", name="S01E01.mp4", type="file", storage="local")
        parent_item = schemas.FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
        self.media_chain.storagechain.get_parent_item.return_value = parent_item
        target_item, target_path = self.media_chain._get_target_fileitem_and_path(
            current_fileitem=fileitem,
            item_type=ScrapingTarget.EPISODE,
            metadata_type=ScrapingMetadata.NFO
        )
        self.assertEqual(target_item, parent_item)
        self.assertEqual(target_path, Path("/tv/Show/Season 1/S01E01.nfo"))


class TestMediaScrapingNFO(unittest.TestCase):
    def setUp(self):
        reset_media_chain_singleton()
        self.media_chain = MediaChain()
        self.media_chain.storagechain = MagicMock()
        self.media_chain.metadata_nfo = MagicMock(return_value="<nfo></nfo>")
        self.media_chain._save_file = MagicMock()
        self.media_chain.scraping_policies = MagicMock()

        self.fileitem = schemas.FileItem(path="/movies/Avatar (2009)", name="Avatar (2009)", type="dir", storage="local")
        self.meta = MetaInfo("Avatar (2009)")
        self.mediainfo = MediaInfo()

    def tearDown(self):
        reset_media_chain_singleton()

    def test_scrape_nfo_off(self):
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("movie", "nfo", ScrapingPolicy.SKIP)
        self.media_chain._scrape_nfo_generic(self.fileitem, self.meta, self.mediainfo, ScrapingTarget.MOVIE)
        self.media_chain.metadata_nfo.assert_not_called()
        self.media_chain._save_file.assert_not_called()

    def test_scrape_nfo_on_exists_skip(self):
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("movie", "nfo", ScrapingPolicy.MISSINGONLY)
        # mock file exists
        self.media_chain.storagechain.get_file_item.return_value = schemas.FileItem(path="/movies/Avatar (2009)/Avatar (2009).nfo", name="Avatar (2009).nfo", type="file", storage="local")

        self.media_chain._scrape_nfo_generic(self.fileitem, self.meta, self.mediainfo, ScrapingTarget.MOVIE)
        self.media_chain.metadata_nfo.assert_not_called()
        self.media_chain._save_file.assert_not_called()

    def test_scrape_nfo_on_not_exists_scrape(self):
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("movie", "nfo", ScrapingPolicy.MISSINGONLY)
        # mock file not exists
        self.media_chain.storagechain.get_file_item.return_value = None

        self.media_chain._scrape_nfo_generic(self.fileitem, self.meta, self.mediainfo, ScrapingTarget.MOVIE)
        self.media_chain.metadata_nfo.assert_called_once()
        self.media_chain._save_file.assert_called_once()

    def test_scrape_nfo_overwrite_exists_scrape(self):
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("movie", "nfo", ScrapingPolicy.OVERWRITE)
        # mock file exists
        self.media_chain.storagechain.get_file_item.return_value = schemas.FileItem(path="/movies/Avatar (2009)/Avatar (2009).nfo", name="Avatar (2009).nfo", type="file", storage="local")

        self.media_chain._scrape_nfo_generic(self.fileitem, self.meta, self.mediainfo, ScrapingTarget.MOVIE)
        self.media_chain.metadata_nfo.assert_called_once()
        self.media_chain._save_file.assert_called_once()


class TestMediaScrapingImages(unittest.TestCase):
    def setUp(self):
        reset_media_chain_singleton()
        self.media_chain = MediaChain()
        self.original_download = self.media_chain._download_and_save_image
        self.media_chain.storagechain = MagicMock()
        self.media_chain.metadata_img = MagicMock()
        self.media_chain._download_and_save_image = MagicMock()
        self.media_chain.scraping_policies = MagicMock()

    def tearDown(self):
        self.media_chain._download_and_save_image = self.original_download
        reset_media_chain_singleton()

    def test_scrape_images_mapping(self):
        fileitem = schemas.FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
        mediainfo = MediaInfo()
        self.media_chain.metadata_img.return_value = {
            "poster.jpg": "http://poster",
            "fanart.jpg": "http://fanart",
            "logo.png": "http://logo"
        }
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("movie", "poster", ScrapingPolicy.OVERWRITE)
        self.media_chain.storagechain.get_file_item.return_value = None

        self.media_chain._scrape_images_generic(fileitem, mediainfo, ScrapingTarget.MOVIE)

        # Check download called for mapped metadata + aliases (fanart→backdrop)
        calls = self.media_chain._download_and_save_image.call_args_list
        urls = [call.kwargs["url"] for call in calls]
        paths = [call.kwargs["path"] for call in calls]
        self.assertIn("http://poster", urls)
        self.assertIn("http://fanart", urls)
        self.assertIn("http://logo", urls)
        # fanart.jpg should also generate backdrop.jpg alias
        self.assertIn(Path("/movies/Avatar/fanart.jpg"), paths)
        self.assertIn(Path("/movies/Avatar/backdrop.jpg"), paths)

    def test_scrape_images_season_filter(self):
        fileitem = schemas.FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
        mediainfo = MediaInfo()
        self.media_chain.metadata_img.return_value = {
            "season01-poster.jpg": "http://season01",
            "season02-poster.jpg": "http://season02"
        }
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("season", "poster", ScrapingPolicy.OVERWRITE)
        self.media_chain.storagechain.get_file_item.return_value = None

        self.media_chain._scrape_images_generic(fileitem, mediainfo, ScrapingTarget.SEASON, season_number=1)

        calls = self.media_chain._download_and_save_image.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call.kwargs["url"] == "http://season01" for call in calls))
        self.assertEqual(
            [call.kwargs["path"] for call in calls],
            [
                Path("/tv/Show/season01-poster.jpg"),
                Path("/tv/Show/Season 1/poster.jpg"),
            ],
        )

    def test_scrape_movie_file_images_when_initialized_directly(self):
        """直接初始化刮削电影文件时，应生成同级 poster/backdrop 及别名。"""
        fileitem = schemas.FileItem(path="/movies/Avatar/Avatar.mkv", name="Avatar.mkv", type="file", storage="local")
        parent_item = schemas.FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
        mediainfo = MediaInfo()
        self.media_chain.metadata_img.return_value = {
            "poster.jpg": "http://poster",
            "backdrop.jpg": "http://backdrop",
        }
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("movie", "poster", ScrapingPolicy.OVERWRITE)
        self.media_chain.storagechain.get_file_item.return_value = None

        self.media_chain._scrape_images_generic(
            fileitem,
            mediainfo,
            ScrapingTarget.MOVIE,
            parent_fileitem=parent_item,
        )

        paths = [call.kwargs["path"] for call in self.media_chain._download_and_save_image.call_args_list]
        # poster has no alias, backdrop generates fanart alias
        self.assertIn(Path("/movies/Avatar/poster.jpg"), paths)
        self.assertIn(Path("/movies/Avatar/backdrop.jpg"), paths)
        self.assertIn(Path("/movies/Avatar/fanart.jpg"), paths)

    def test_scrape_episode_thumb_image_path(self):
        fileitem = schemas.FileItem(path="/tv/Show/Season 1/S01E01.mp4", name="S01E01.mp4", type="file", storage="local")
        parent_item = schemas.FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
        mediainfo = MediaInfo()
        self.media_chain.metadata_img.return_value = {
            "thumb.jpg": "http://episode-thumb"
        }
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("episode", "thumb", ScrapingPolicy.OVERWRITE)
        self.media_chain.storagechain.get_file_item.return_value = None

        self.media_chain._scrape_images_generic(
            fileitem,
            mediainfo,
            ScrapingTarget.EPISODE,
            parent_fileitem=parent_item,
            season_number=1,
            episode_number=1
        )

        self.media_chain.metadata_img.assert_called_once_with(
            mediainfo=mediainfo,
            season=1,
            episode=1
        )
        self.media_chain._download_and_save_image.assert_called_once_with(
            fileitem=parent_item,
            path=Path("/tv/Show/Season 1/S01E01.jpg"),
            url="http://episode-thumb"
        )

    def test_scrape_episode_thumb_image_path_via_parent_lookup(self):
        fileitem = schemas.FileItem(path="/tv/Show/Season 1/S01E01.mp4", name="S01E01.mp4", type="file", storage="local")
        parent_item = schemas.FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
        mediainfo = MediaInfo()
        self.media_chain.metadata_img.return_value = {
            "thumb.jpg": "http://episode-thumb"
        }
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("episode", "thumb", ScrapingPolicy.OVERWRITE)
        self.media_chain.storagechain.get_parent_item.return_value = parent_item
        self.media_chain.storagechain.get_file_item.return_value = None

        self.media_chain._scrape_images_generic(
            fileitem,
            mediainfo,
            ScrapingTarget.EPISODE,
            season_number=1,
            episode_number=1
        )

        self.media_chain.storagechain.get_parent_item.assert_called_once_with(fileitem)
        self.media_chain._download_and_save_image.assert_called_once_with(
            fileitem=parent_item,
            path=Path("/tv/Show/Season 1/S01E01.jpg"),
            url="http://episode-thumb"
        )

    def test_expand_with_aliases_backdrop(self):
        """backdrop should also generate fanart alias."""
        parent_item = schemas.FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
        targets = [(parent_item, Path("/movies/Avatar/backdrop.jpg"))]
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("movie", "backdrop", ScrapingPolicy.OVERWRITE)

        expanded = self.media_chain._expand_with_aliases(targets, ScrapingTarget.MOVIE)
        paths = [t[1] for t in expanded]
        self.assertIn(Path("/movies/Avatar/backdrop.jpg"), paths)
        self.assertIn(Path("/movies/Avatar/fanart.jpg"), paths)

    def test_expand_with_aliases_thumb(self):
        """thumb should also generate landscape alias."""
        parent_item = schemas.FileItem(path="/tv/Show", name="Show", type="dir", storage="local")
        targets = [(parent_item, Path("/tv/Show/thumb.jpg"))]
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("tv", "thumb", ScrapingPolicy.OVERWRITE)

        expanded = self.media_chain._expand_with_aliases(targets, ScrapingTarget.TV)
        paths = [t[1] for t in expanded]
        self.assertIn(Path("/tv/Show/thumb.jpg"), paths)
        self.assertIn(Path("/tv/Show/landscape.jpg"), paths)

    def test_expand_with_aliases_skips_season_prefix(self):
        """season-prefixed files should not get aliases."""
        parent_item = schemas.FileItem(path="/tv/Show", name="Show", type="dir", storage="local")
        targets = [(parent_item, Path("/tv/Show/season01-thumb.jpg"))]
        self.media_chain.scraping_policies.option.return_value = ScrapingOption("season", "thumb", ScrapingPolicy.OVERWRITE)

        expanded = self.media_chain._expand_with_aliases(targets, ScrapingTarget.SEASON)
        self.assertEqual(len(expanded), 1)

    def test_expand_with_aliases_respects_skip_policy(self):
        """Alias should not be generated if its metadata type is set to SKIP."""
        parent_item = schemas.FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
        targets = [(parent_item, Path("/movies/Avatar/backdrop.jpg"))]
        # backdrop is OVERWRITE but fanart (also BACKDROP type) is SKIP
        def option_side_effect(item_type, metadata_type):
            if metadata_type == ScrapingMetadata.BACKDROP:
                return ScrapingOption("movie", "backdrop", ScrapingPolicy.SKIP)
            return ScrapingOption("movie", "backdrop", ScrapingPolicy.OVERWRITE)
        self.media_chain.scraping_policies.option.side_effect = option_side_effect

        expanded = self.media_chain._expand_with_aliases(targets, ScrapingTarget.MOVIE)
        # fanart maps to BACKDROP which is SKIP, so no alias
        self.assertEqual(len(expanded), 1)

    def test_season_backdrop_path(self):
        """Season backdrop should be saved in season directory."""
        fileitem = schemas.FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
        target_item, target_path = self.media_chain._get_target_fileitem_and_path(
            current_fileitem=fileitem,
            item_type=ScrapingTarget.SEASON,
            metadata_type=ScrapingMetadata.BACKDROP,
            filename_hint="season01-backdrop.jpg"
        )
        self.assertEqual(target_item, fileitem)
        self.assertEqual(target_path, Path("/tv/Show/Season 1/backdrop.jpg"))

    @patch("app.chain.media.RequestUtils")
    @patch("app.chain.media.NamedTemporaryFile")
    @patch("app.chain.media.Path.chmod")
    @patch("app.chain.media.settings")
    def test_download_and_save_image(self, mock_settings, mock_chmod, mock_temp_file, mock_request_utils):
        # We need to test _download_and_save_image directly so we remove mock
        self.media_chain = MediaChain()
        self.media_chain._download_and_save_image = self.original_download
        self.media_chain.storagechain = MagicMock()

        fileitem = schemas.FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
        target_path = Path("/movies/Avatar/poster.jpg")
        url = "http://poster"

        # mock temp file
        tmp_mock = MagicMock()
        tmp_mock.name = "/tmp/mockfile"
        mock_temp_file.return_value.__enter__.return_value = tmp_mock

        # mock stream
        mock_stream = MagicMock()
        mock_stream.status_code = 200
        mock_stream.iter_content.return_value = [b"data1", b"data2"]

        mock_instance = mock_request_utils.return_value
        mock_instance.get_stream.return_value.__enter__.return_value = mock_stream

        self.media_chain.storagechain.upload_file.return_value = fileitem

        self.media_chain._download_and_save_image(fileitem, target_path, url)

        mock_request_utils.assert_called_with(proxies=mock_settings.PROXY, ua=mock_settings.NORMAL_USER_AGENT)
        mock_instance.get_stream.assert_called_with(url=url)
        mock_temp_file.assert_called_once_with(delete=False, suffix=".jpg")
        tmp_mock.write.assert_any_call(b"data1")
        tmp_mock.write.assert_any_call(b"data2")
        mock_chmod.assert_called()
        self.media_chain.storagechain.upload_file.assert_called_once()
        call_args = self.media_chain.storagechain.upload_file.call_args.kwargs
        self.assertEqual(call_args["fileitem"], fileitem)
        self.assertEqual(call_args["new_name"], "poster.jpg")

    @patch("app.chain.media.NamedTemporaryFile")
    @patch("app.chain.media.Path.chmod")
    def test_save_file_uses_python310_compatible_tempfile(self, mock_chmod, mock_temp_file):
        """保存刮削文件时不应使用 Python 3.12 才支持的 delete_on_close 参数。"""
        self.media_chain = MediaChain()
        self.media_chain.storagechain = MagicMock()
        self.media_chain._cleanup_temp_file = MagicMock()

        fileitem = schemas.FileItem(path="/movies/Avatar", name="Avatar", type="dir", storage="local")
        target_path = Path("/movies/Avatar/movie.nfo")

        tmp_mock = MagicMock()
        tmp_mock.name = "/tmp/mockfile"
        mock_temp_file.return_value.__enter__.return_value = tmp_mock
        self.media_chain.storagechain.upload_file.return_value = fileitem

        self.media_chain._save_file(fileitem, target_path, "<nfo></nfo>")

        mock_temp_file.assert_called_once_with(delete=False, suffix=".nfo")
        tmp_mock.write.assert_called_once_with(b"<nfo></nfo>")
        mock_chmod.assert_called()
        self.media_chain.storagechain.upload_file.assert_called_once()
        self.media_chain._cleanup_temp_file.assert_called_once_with(Path("/tmp/mockfile"))


class TestMediaScrapingTVDirectory(unittest.TestCase):
    def setUp(self):
        reset_media_chain_singleton()
        self.media_chain = MediaChain()
        self.media_chain.storagechain = MagicMock()
        self.media_chain._scrape_nfo_generic = MagicMock()
        self.media_chain._scrape_images_generic = MagicMock()

    def tearDown(self):
        reset_media_chain_singleton()

    @patch("app.chain.media.settings")
    def test_initialize_tv_directory_specials(self, mock_settings):
        # mock specials directory recognition
        mock_settings.RENAME_FORMAT_S0_NAMES = ["Specials", "SPs"]

        fileitem = schemas.FileItem(path="/tv/Show/Specials", name="Specials", type="dir", storage="local")
        meta = MetaInfo("Show")
        mediainfo = MediaInfo(type=MediaType.TV)
        filepath = Path(fileitem.path)

        self.media_chain._initialize_tv_directory_metadata(
            fileitem=fileitem,
            filepath=filepath,
            meta=meta,
            mediainfo=mediainfo,
            parent=None,
            overwrite=False,
        )

        self.media_chain._scrape_nfo_generic.assert_called_once_with(
            current_fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            item_type=ScrapingTarget.SEASON,
            overwrite=False,
            season_number=0
        )
        self.media_chain._scrape_images_generic.assert_called_once_with(
            current_fileitem=fileitem,
            mediainfo=mediainfo,
            item_type=ScrapingTarget.SEASON,
            parent_fileitem=None,
            overwrite=False,
            season_number=0
        )

    @patch("app.chain.media.settings")
    def test_initialize_tv_directory_season(self, mock_settings):
        mock_settings.RENAME_FORMAT_S0_NAMES = ["Specials", "SPs"]

        fileitem = schemas.FileItem(path="/tv/Show/Season 1", name="Season 1", type="dir", storage="local")
        meta = MetaInfo("Show")
        mediainfo = MediaInfo(type=MediaType.TV)
        filepath = Path(fileitem.path)

        self.media_chain._initialize_tv_directory_metadata(
            fileitem=fileitem,
            filepath=filepath,
            meta=meta,
            mediainfo=mediainfo,
            parent=None,
            overwrite=False,
        )

        self.media_chain._scrape_nfo_generic.assert_called_once_with(
            current_fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            item_type=ScrapingTarget.SEASON,
            overwrite=False,
            season_number=1
        )


class TestMediaScrapeEvents(unittest.TestCase):
    def setUp(self):
        reset_media_chain_singleton()
        self.media_chain = MediaChain()
        self.media_chain.storagechain = MagicMock()

    def tearDown(self):
        reset_media_chain_singleton()

    @patch("app.chain.media.MediaChain.scrape_metadata")
    def test_scrape_metadata_event_file(
        self, mock_scrape_metadata
    ):
        fileitem = schemas.FileItem(path="/movies/movie.mkv", name="movie.mkv", type="file", storage="local")
        parent_item = schemas.FileItem(path="/movies", name="movies", type="dir", storage="local")

        self.media_chain.storagechain.get_item.return_value = fileitem
        self.media_chain.storagechain.get_parent_item.return_value = parent_item

        mediainfo = MediaInfo()
        event = Event(
            event_type=EventType.MetadataScrape,
            event_data={
                "fileitem": fileitem,
                "mediainfo": mediainfo,
                "overwrite": True
            }
        )

        self.media_chain.scrape_metadata_event(event)

        mock_scrape_metadata.assert_called_once_with(
            fileitem=fileitem,
            mediainfo=mediainfo,
            init_folder=True,
            parent=parent_item,
            overwrite=True
        )

    @patch("app.chain.media.MediaChain.scrape_metadata")
    def test_scrape_metadata_event_dir_bluray(
        self, mock_scrape_metadata
    ):
        fileitem = schemas.FileItem(path="/movies/bluray_movie", name="bluray_movie", type="dir", storage="local")

        self.media_chain.storagechain.get_item.return_value = fileitem
        self.media_chain.storagechain.is_bluray_folder.return_value = True

        mediainfo = MediaInfo()
        event = Event(
            event_type=EventType.MetadataScrape,
            event_data={
                "fileitem": fileitem,
                "file_list": ["/movies/bluray_movie/BDMV/index.bdmv"],
                "mediainfo": mediainfo,
                "overwrite": False
            }
        )

        self.media_chain.scrape_metadata_event(event)

        mock_scrape_metadata.assert_called_once_with(
            fileitem=fileitem,
            mediainfo=mediainfo,
            init_folder=True,
            recursive=False,
            overwrite=False
        )

    @patch("app.chain.media.MediaChain.scrape_metadata")
    def test_scrape_metadata_event_dir_with_filelist(
        self, mock_scrape_metadata
    ):
        fileitem = schemas.FileItem(path="/tv/show", name="show", type="dir", storage="local")

        self.media_chain.storagechain.get_item.return_value = fileitem
        self.media_chain.storagechain.is_bluray_folder.return_value = False

        def side_effect_get_file_item(storage, path):
            path_str = str(path)
            return schemas.FileItem(path=path_str, name=Path(path_str).name, type="dir" if "." not in path_str else "file", storage="local")

        self.media_chain.storagechain.get_file_item.side_effect = side_effect_get_file_item

        mediainfo = MediaInfo()
        event = Event(
            event_type=EventType.MetadataScrape,
            event_data={
                "fileitem": fileitem,
                "file_list": ["/tv/show/Season 1/S01E01.mp4"],
                "mediainfo": mediainfo,
                "overwrite": True
            }
        )

        self.media_chain.scrape_metadata_event(event)

        calls = mock_scrape_metadata.call_args_list
        self.assertEqual(len(calls), 3)

        paths = [call.kwargs['fileitem'].path for call in calls]
        self.assertIn("/tv/show", paths)
        self.assertIn("/tv/show/Season 1", paths)
        self.assertIn("/tv/show/Season 1/S01E01.mp4", paths)

    @patch("app.chain.media.MediaChain.scrape_metadata")
    def test_scrape_metadata_event_dir_full(
        self, mock_scrape_metadata
    ):
        fileitem = schemas.FileItem(path="/movies/movie", name="movie", type="dir", storage="local")

        self.media_chain.storagechain.get_item.return_value = fileitem

        mediainfo = MediaInfo()
        meta = MetaInfo("movie")
        event = Event(
            event_type=EventType.MetadataScrape,
            event_data={
                "fileitem": fileitem,
                "meta": meta,
                "mediainfo": mediainfo,
                "overwrite": True
            }
        )

        self.media_chain.scrape_metadata_event(event)

        mock_scrape_metadata.assert_called_once_with(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            init_folder=True,
            overwrite=True
        )

    @patch("app.chain.media.MediaChain._handle_movie_scraping")
    @patch("app.chain.media.MediaChain.recognize_by_meta")
    def test_scrape_metadata_movie(
        self, mock_recognize, mock_handle_movie
    ):
        fileitem = schemas.FileItem(path="/movies/movie.mkv", name="movie.mkv", type="file", storage="local")
        meta = MetaInfo("Movie")
        mediainfo = MediaInfo(type=MediaType.MOVIE)

        self.media_chain.scrape_metadata(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            init_folder=True,
            overwrite=False,
            recursive=True
        )

        mock_recognize.assert_not_called()
        mock_handle_movie.assert_called_once_with(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            init_folder=True,
            parent=None,
            overwrite=False,
            recursive=True
        )

    @patch("app.chain.media.MediaChain._handle_tv_scraping")
    @patch("app.chain.media.MediaChain.recognize_by_meta")
    def test_scrape_metadata_tv(
        self, mock_recognize, mock_handle_tv
    ):
        fileitem = schemas.FileItem(path="/tv/show", name="show", type="dir", storage="local")
        meta = MetaInfo("Show")
        mediainfo = MediaInfo(type=MediaType.TV)

        self.media_chain.scrape_metadata(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            init_folder=True,
            overwrite=False,
            recursive=True
        )

        mock_handle_tv.assert_called_once_with(
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            init_folder=True,
            parent=None,
            overwrite=False,
            recursive=True
        )

    @patch("app.chain.media.MediaChain._handle_movie_scraping")
    @patch("app.chain.media.MediaChain.recognize_by_meta")
    def test_scrape_metadata_recognize_fallback(
        self, mock_recognize, mock_handle_movie
    ):
        fileitem = schemas.FileItem(path="/movies/movie.mkv", name="movie.mkv", type="file", storage="local")
        mediainfo = MediaInfo(type=MediaType.MOVIE)
        mock_recognize.return_value = mediainfo

        self.media_chain.scrape_metadata(
            fileitem=fileitem,
            init_folder=True,
            overwrite=False,
            recursive=True
        )

        mock_recognize.assert_called_once()
        mock_handle_movie.assert_called_once()
        args, kwargs = mock_handle_movie.call_args
        self.assertEqual(kwargs['mediainfo'], mediainfo)
        self.assertEqual(kwargs['meta'].name, "Movie")

    @patch("app.chain.media.MediaChain._handle_movie_scraping")
    @patch("app.chain.media.MediaChain._handle_tv_scraping")
    def test_scrape_metadata_invalid_extension(
        self, mock_handle_tv, mock_handle_movie
    ):
        fileitem = schemas.FileItem(path="/movies/movie.txt", name="movie.txt", type="file", storage="local")

        self.media_chain.scrape_metadata(
            fileitem=fileitem
        )

        mock_handle_movie.assert_not_called()
        mock_handle_tv.assert_not_called()

    @patch("app.chain.media.MediaChain.scrape_metadata")
    def test_scrape_metadata_event_dir_with_multiple_files(
        self, mock_scrape_metadata
    ):
        fileitem = schemas.FileItem(path="/movies/collection", name="collection", type="dir", storage="local")

        self.media_chain.storagechain.get_item.return_value = fileitem
        self.media_chain.storagechain.is_bluray_folder.return_value = False

        def side_effect_get_file_item(storage, path):
            path_str = str(path)
            return schemas.FileItem(path=path_str, name=Path(path_str).name, type="dir" if "." not in path_str else "file", storage="local")

        self.media_chain.storagechain.get_file_item.side_effect = side_effect_get_file_item

        mediainfo = MediaInfo()
        event = Event(
            event_type=EventType.MetadataScrape,
            event_data={
                "fileitem": fileitem,
                "file_list": [
                    "/movies/collection/movie1.mp4",
                    "/movies/collection/movie2.mkv",
                    "/movies/collection/movie3.avi"
                ],
                "mediainfo": mediainfo,
                "overwrite": True
            }
        )

        self.media_chain.scrape_metadata_event(event)

        calls = mock_scrape_metadata.call_args_list
        # Should scrape directory and then each file item
        self.assertEqual(len(calls), 4)

        paths = [call.kwargs['fileitem'].path for call in calls]
        self.assertIn("/movies/collection", paths)
        self.assertIn("/movies/collection/movie1.mp4", paths)
        self.assertIn("/movies/collection/movie2.mkv", paths)
        self.assertIn("/movies/collection/movie3.avi", paths)

    @patch("app.chain.media.MediaChain.scrape_metadata")
    def test_scrape_metadata_event_dir_with_tv_multi_seasons_episodes(
        self, mock_scrape_metadata
    ):
        fileitem = schemas.FileItem(path="/tv/MultiSeasonShow", name="MultiSeasonShow", type="dir", storage="local")

        self.media_chain.storagechain.get_item.return_value = fileitem
        self.media_chain.storagechain.is_bluray_folder.return_value = False

        def side_effect_get_file_item(storage, path):
            path_str = str(path)
            return schemas.FileItem(path=path_str, name=Path(path_str).name, type="dir" if "." not in path_str else "file", storage="local")

        self.media_chain.storagechain.get_file_item.side_effect = side_effect_get_file_item

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
                    "/tv/MultiSeasonShow/Specials/S00E01.mp4"
                ],
                "mediainfo": mediainfo,
                "overwrite": False
            }
        )

        self.media_chain.scrape_metadata_event(event)

        calls = mock_scrape_metadata.call_args_list
        # main dir + 3 season dirs + 5 episode files
        self.assertEqual(len(calls), 9)

        paths = [call.kwargs['fileitem'].path for call in calls]
        self.assertIn("/tv/MultiSeasonShow", paths)
        self.assertIn("/tv/MultiSeasonShow/Season 1", paths)
        self.assertIn("/tv/MultiSeasonShow/Season 2", paths)
        self.assertIn("/tv/MultiSeasonShow/Specials", paths)
        self.assertIn("/tv/MultiSeasonShow/Season 1/S01E01.mp4", paths)
        self.assertIn("/tv/MultiSeasonShow/Season 1/S01E02.mp4", paths)
        self.assertIn("/tv/MultiSeasonShow/Season 2/S02E01.mkv", paths)
        self.assertIn("/tv/MultiSeasonShow/Season 2/S02E02.mkv", paths)
        self.assertIn("/tv/MultiSeasonShow/Specials/S00E01.mp4", paths)

    @patch("app.chain.media.MediaChain.recognize_by_meta")
    def test_scrape_metadata_recognize_fail(
        self, mock_recognize
    ):
        fileitem = schemas.FileItem(path="/movies/movie.mkv", name="movie.mkv", type="file", storage="local")
        mock_recognize.return_value = None

        with patch('app.chain.media.logger.warn') as mock_logger:
            self.media_chain.scrape_metadata(
                fileitem=fileitem
            )
            mock_logger.assert_called_with(f"{Path(fileitem.path)} 无法识别文件媒体信息！")
