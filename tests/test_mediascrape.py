import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
# ruff: noqa: E402
sys.modules['app.helper.sites'] = MagicMock()
sys.modules['app.db.systemconfig_oper'] = MagicMock()
sys.modules['app.db.systemconfig_oper'].SystemConfigOper.return_value.get.return_value = None

from app import schemas
from app.chain.media import MediaChain, ScrapingOption
from app.core.context import MediaInfo
from app.core.event import Event
from app.core.metainfo import MetaInfo
from app.schemas.types import EventType, MediaType, ScrapingTarget, ScrapingMetadata, ScrapingPolicy


class TestMediaScrapingPaths(unittest.TestCase):
    def setUp(self):
        self.media_chain = MediaChain()
        self.media_chain.storagechain = MagicMock()

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
        self.media_chain = MediaChain()
        self.media_chain.storagechain = MagicMock()
        self.media_chain.metadata_nfo = MagicMock(return_value="<nfo></nfo>")
        self.media_chain._save_file = MagicMock()
        self.media_chain.scraping_policies = MagicMock()

        self.fileitem = schemas.FileItem(path="/movies/Avatar (2009)", name="Avatar (2009)", type="dir", storage="local")
        self.meta = MetaInfo("Avatar (2009)")
        self.mediainfo = MediaInfo()

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
        self.media_chain = MediaChain()
        self.original_download = self.media_chain._download_and_save_image
        self.media_chain.storagechain = MagicMock()
        self.media_chain.metadata_img = MagicMock()
        self.media_chain._download_and_save_image = MagicMock()
        self.media_chain.scraping_policies = MagicMock()

    def tearDown(self):
        self.media_chain._download_and_save_image = self.original_download

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

        # Check download called for mapped metadata
        calls = self.media_chain._download_and_save_image.call_args_list
        self.assertEqual(len(calls), 3)
        urls = [call.kwargs["url"] for call in calls]
        self.assertIn("http://poster", urls)
        self.assertIn("http://fanart", urls)
        self.assertIn("http://logo", urls)

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
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["url"], "http://season01")

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
        tmp_mock.write.assert_any_call(b"data1")
        tmp_mock.write.assert_any_call(b"data2")
        mock_chmod.assert_called()
        self.media_chain.storagechain.upload_file.assert_called_once()
        call_args = self.media_chain.storagechain.upload_file.call_args.kwargs
        self.assertEqual(call_args["fileitem"], fileitem)
        self.assertEqual(call_args["new_name"], "poster.jpg")


class TestMediaScrapingTVDirectory(unittest.TestCase):
    def setUp(self):
        self.media_chain = MediaChain()
        self.media_chain.storagechain = MagicMock()
        self.media_chain._scrape_nfo_generic = MagicMock()
        self.media_chain._scrape_images_generic = MagicMock()

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
        self.media_chain = MediaChain()
        self.media_chain.storagechain = MagicMock()

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
            init_folder=False,
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

if __name__ == "__main__":
    unittest.main()
