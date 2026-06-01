import unittest
from unittest.mock import patch

from app.api.endpoints import download as download_endpoint


class DownloadPathsEndpointTest(unittest.TestCase):
    def test_paths_returns_api_ready_save_paths(self):
        mocked_dirs = [
            download_endpoint.schemas.TransferDirectoryConf(
                name="电影目录",
                priority=1,
                storage="local",
                download_path="/downloads/movies",
                media_type="movie",
            ),
            download_endpoint.schemas.TransferDirectoryConf(
                name="动漫远程目录",
                priority=2,
                storage="rclone",
                download_path="/media/anime",
                media_type="tv",
                media_category="动漫",
            ),
        ]

        with patch.object(download_endpoint.DirectoryHelper, "get_download_dirs", return_value=mocked_dirs):
            ret = download_endpoint.paths(_=download_endpoint.schemas.TokenPayload())

        self.assertEqual(len(ret), 2)
        self.assertEqual(ret[0].name, "电影目录")
        self.assertEqual(ret[0].storage, "local")
        self.assertEqual(ret[0].download_path, "/downloads/movies")
        self.assertEqual(ret[0].save_path, "/downloads/movies")
        self.assertEqual(ret[0].priority, 1)
        self.assertEqual(ret[0].media_type, "movie")
        self.assertIsNone(ret[0].media_category)

        self.assertEqual(ret[1].name, "动漫远程目录")
        self.assertEqual(ret[1].storage, "rclone")
        self.assertEqual(ret[1].download_path, "/media/anime")
        self.assertEqual(ret[1].save_path, "rclone:/media/anime")
        self.assertEqual(ret[1].priority, 2)
        self.assertEqual(ret[1].media_type, "tv")
        self.assertEqual(ret[1].media_category, "动漫")

    def test_paths_returns_empty_list_when_unconfigured(self):
        with patch.object(download_endpoint.DirectoryHelper, "get_download_dirs", return_value=[]):
            ret = download_endpoint.paths(_=download_endpoint.schemas.TokenPayload())

        self.assertEqual(ret, [])
