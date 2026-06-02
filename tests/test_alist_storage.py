import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.modules.filemanager.storages import alist as alist_module
from app.modules.filemanager.storages.alist import Alist
from app.schemas import FileItem


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class AlistStorageTest(unittest.TestCase):
    def setUp(self):
        self.storage = Alist()

    @staticmethod
    def _dir_item(path: str = "/"):
        return FileItem(storage="alist", type="dir", path=path)

    @staticmethod
    def _page_payload(start: int, count: int, total: int) -> dict:
        return {
            "code": 200,
            "message": "success",
            "data": {
                "content": [
                    {
                        "name": f"dir-{index}",
                        "size": 0,
                        "is_dir": True,
                        "modified": "2024-05-17T13:47:55.4174917+08:00",
                        "thumb": "",
                    }
                    for index in range(start, start + count)
                ],
                "total": total,
            },
        }

    def test_list_fetches_all_pages_when_per_page_is_default(self):
        responses = [
            _FakeResponse(self._page_payload(0, 500, 505)),
            _FakeResponse(self._page_payload(500, 5, 505)),
        ]
        request_utils = MagicMock()
        request_utils.post_res.side_effect = responses

        with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
            with patch.object(alist_module, "RequestUtils", return_value=request_utils):
                items = self.storage.list(self._dir_item())

        self.assertEqual(505, len(items))
        self.assertEqual("/dir-0/", items[0].path)
        self.assertEqual("/dir-504/", items[-1].path)
        self.assertEqual(2, request_utils.post_res.call_count)
        self.assertEqual(1, request_utils.post_res.call_args_list[0].kwargs["json"]["page"])
        self.assertEqual(2, request_utils.post_res.call_args_list[1].kwargs["json"]["page"])
        self.assertEqual(500, request_utils.post_res.call_args_list[0].kwargs["json"]["per_page"])
        self.assertEqual(500, request_utils.post_res.call_args_list[1].kwargs["json"]["per_page"])

    def test_list_respects_explicit_per_page_without_auto_paging(self):
        request_utils = MagicMock()
        request_utils.post_res.return_value = _FakeResponse(self._page_payload(0, 50, 205))

        with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
            with patch.object(alist_module, "RequestUtils", return_value=request_utils):
                items = self.storage.list(self._dir_item(), per_page=50)

        self.assertEqual(50, len(items))
        self.assertEqual(1, request_utils.post_res.call_count)
        self.assertEqual(50, request_utils.post_res.call_args.kwargs["json"]["per_page"])

    def test_create_folder_returns_target_when_openlist_metadata_is_delayed(self):
        """
        OpenList 创建目录成功但元数据延迟可见时，应返回可用的目标目录项。
        """
        request_utils = MagicMock()
        request_utils.post_res.return_value = _FakeResponse(
            {"code": 200, "message": "success", "data": None}
        )

        with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
            with patch.object(self.storage, "_Alist__get_header_with_token", return_value={}):
                with patch.object(alist_module, "RequestUtils", return_value=request_utils):
                    with patch.object(self.storage, "_delay_get_item", return_value=None):
                        folder = self.storage.create_folder(
                            self._dir_item("/library/Test Show (2026)"),
                            "Season 1",
                        )

        self.assertIsNotNone(folder)
        self.assertEqual("/library/Test Show (2026)/Season 1/", folder.path)
        self.assertEqual("alist", folder.storage)
        self.assertEqual("dir", folder.type)

    def test_move_item_returns_target_when_openlist_metadata_is_delayed(self):
        """
        OpenList 操作成功但目标元数据延迟可见时，应返回可用的目标文件项。
        """
        source = FileItem(
            storage="alist",
            type="file",
            path="/downloads/Test.Show.S01E01.mkv",
            name="Test.Show.S01E01.mkv",
            basename="Test.Show.S01E01",
            extension="mkv",
            size=1024,
            modify_time=1715939275.0,
        )
        request_utils = MagicMock()
        request_utils.post_res.return_value = _FakeResponse(
            {"code": 200, "message": "success", "data": None}
        )

        with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
            with patch.object(self.storage, "_Alist__get_header_with_token", return_value={}):
                with patch.object(alist_module, "RequestUtils", return_value=request_utils):
                    with patch.object(self.storage, "_delay_get_item", return_value=None):
                        target = self.storage.move_item(
                            source,
                            Path("/library/Test Show (2026)/Season 1"),
                            "Test.Show.S01E01.mkv",
                        )

        self.assertIsNotNone(target)
        self.assertEqual(
            "/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv",
            target.path,
        )
        self.assertEqual("alist", target.storage)
        self.assertEqual("file", target.type)
        self.assertEqual(1024, target.size)
