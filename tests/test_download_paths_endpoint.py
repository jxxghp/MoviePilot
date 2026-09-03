import unittest
from unittest.mock import patch

from app.api.endpoints import download as download_endpoint
from app.schemas.system import TransferDirectoryConf
from app.schemas.token import TokenPayload


class DownloadPathsEndpointTest(unittest.TestCase):
    """验证下载路径接口生成可直接提交给下载接口的路径数据。"""

    def test_paths_returns_api_ready_save_paths(self):
        """配置的本地和远程目录应转换为完整下载路径响应。"""
        mocked_dirs = [
            TransferDirectoryConf(
                name="电影目录",
                priority=1,
                storage="local",
                download_path="/downloads/movies",
                media_type="movie",
            ),
            TransferDirectoryConf(
                name="动漫远程目录",
                priority=2,
                storage="rclone",
                download_path="/media/anime",
                media_type="tv",
                media_category="动漫",
                media_category_id="tv.anime",
            ),
        ]

        with patch.object(download_endpoint.DirectoryHelper, "get_download_dirs", return_value=mocked_dirs):
            ret = download_endpoint.paths(_=TokenPayload())

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
        self.assertEqual(ret[1].media_category_id, "tv.anime")

    def test_paths_returns_empty_list_when_unconfigured(self):
        """未配置目录时接口应返回空列表。"""
        with patch.object(download_endpoint.DirectoryHelper, "get_download_dirs", return_value=[]):
            ret = download_endpoint.paths(_=TokenPayload())

        self.assertEqual(ret, [])
