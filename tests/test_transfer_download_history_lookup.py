import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.chain.transfer import TransferChain


class FakeDownloadHistoryOper:
    def __init__(
        self,
        histories_by_hash=None,
        histories_by_path=None,
        files_by_fullpath=None,
        files_by_savepath=None,
    ):
        self.histories_by_hash = histories_by_hash or {}
        self.histories_by_path = histories_by_path or {}
        self.files_by_fullpath = files_by_fullpath or {}
        self.files_by_savepath = files_by_savepath or {}

    def get_by_hash(self, download_hash: str):
        return self.histories_by_hash.get(download_hash)

    def get_by_path(self, path: str):
        return self.histories_by_path.get(path)

    def get_file_by_fullpath(self, fullpath: str):
        return self.files_by_fullpath.get(fullpath)

    def get_files_by_savepath(self, savepath: str):
        return self.files_by_savepath.get(savepath, [])


class TransferDownloadHistoryLookupTest(unittest.TestCase):
    def setUp(self):
        self.chain = object.__new__(TransferChain)

    def test_resolve_download_history_falls_back_to_parent_download_path(self):
        expected = SimpleNamespace(download_hash="hash1", downloader="qb")
        oper = FakeDownloadHistoryOper(
            histories_by_hash={"hash1": expected},
            histories_by_path={"/downloads/season-pack": expected},
        )

        history = self.chain._resolve_download_history(
            downloadhis=oper,
            file_path=Path("/downloads/season-pack/Test.Show.S01E01.mkv"),
        )

        self.assertIs(history, expected)

    def test_resolve_download_history_falls_back_to_unique_savepath_hash(self):
        expected = SimpleNamespace(download_hash="hash1", downloader="qb")
        oper = FakeDownloadHistoryOper(
            histories_by_hash={"hash1": expected},
            files_by_savepath={
                "/downloads/season-pack": [
                    SimpleNamespace(download_hash="hash1"),
                    SimpleNamespace(download_hash="hash1"),
                ]
            },
        )

        history = self.chain._resolve_download_history(
            downloadhis=oper,
            file_path=Path("/downloads/season-pack/subs/Test.Show.S01E01.zh.ass"),
        )

        self.assertIs(history, expected)

    def test_resolve_download_history_skips_ambiguous_savepath_hashes(self):
        oper = FakeDownloadHistoryOper(
            histories_by_hash={
                "hash1": SimpleNamespace(download_hash="hash1", downloader="qb"),
                "hash2": SimpleNamespace(download_hash="hash2", downloader="tr"),
            },
            files_by_savepath={
                "/downloads/shared": [
                    SimpleNamespace(download_hash="hash1"),
                    SimpleNamespace(download_hash="hash2"),
                ]
            },
        )

        history = self.chain._resolve_download_history(
            downloadhis=oper,
            file_path=Path("/downloads/shared/Test.Show.S01E01.mkv"),
        )

        self.assertIsNone(history)

    def test_resolve_download_history_stops_at_shared_download_root_path(self):
        oper = FakeDownloadHistoryOper(
            histories_by_path={
                "/downloads": SimpleNamespace(download_hash="hash1", downloader="qb")
            }
        )

        with patch(
            "app.chain.transfer.DirectoryHelper.get_download_dirs",
            return_value=[
                SimpleNamespace(
                    download_path="/downloads",
                    media_type=None,
                    download_type_folder=False,
                    media_category=None,
                    download_category_folder=False,
                )
            ],
        ):
            history = self.chain._resolve_download_history(
                downloadhis=oper,
                file_path=Path("/downloads/Ghost.Concert.mkv"),
            )

        self.assertIsNone(history)

    def test_resolve_download_history_stops_at_shared_download_root_savepath(self):
        expected = SimpleNamespace(download_hash="hash1", downloader="qb")
        oper = FakeDownloadHistoryOper(
            histories_by_hash={"hash1": expected},
            files_by_savepath={
                "/downloads": [
                    SimpleNamespace(
                        download_hash="hash1",
                        fullpath="/downloads/Other.Show.mkv",
                        filepath="Other.Show.mkv",
                    ),
                ]
            },
        )

        with patch(
            "app.chain.transfer.DirectoryHelper.get_download_dirs",
            return_value=[
                SimpleNamespace(
                    download_path="/downloads",
                    media_type=None,
                    download_type_folder=False,
                    media_category=None,
                    download_category_folder=False,
                )
            ],
        ):
            history = self.chain._resolve_download_history(
                downloadhis=oper,
                file_path=Path("/downloads/Ghost.Concert.mkv"),
            )

        self.assertIsNone(history)

    def test_resolve_download_history_accepts_shared_root_savepath_for_exact_file(self):
        expected = SimpleNamespace(download_hash="hash1", downloader="qb")
        oper = FakeDownloadHistoryOper(
            histories_by_hash={"hash1": expected},
            files_by_savepath={
                "/downloads": [
                    SimpleNamespace(
                        download_hash="hash1",
                        fullpath="/downloads/Ghost.Concert.mkv",
                        filepath="Ghost.Concert.mkv",
                    ),
                ]
            },
        )

        with patch(
            "app.chain.transfer.DirectoryHelper.get_download_dirs",
            return_value=[
                SimpleNamespace(
                    download_path="/downloads",
                    media_type=None,
                    download_type_folder=False,
                    media_category=None,
                    download_category_folder=False,
                )
            ],
        ):
            history = self.chain._resolve_download_history(
                downloadhis=oper,
                file_path=Path("/downloads/Ghost.Concert.mkv"),
            )

        self.assertIs(history, expected)

    def test_resolve_download_history_stops_at_type_category_download_root(self):
        oper = FakeDownloadHistoryOper(
            histories_by_path={
                "/downloads/电视剧/动漫": SimpleNamespace(
                    download_hash="hash1", downloader="qb"
                )
            }
        )

        with patch(
            "app.chain.transfer.DirectoryHelper.get_download_dirs",
            return_value=[
                SimpleNamespace(
                    download_path="/downloads",
                    media_type=None,
                    download_type_folder=True,
                    media_category=None,
                    download_category_folder=True,
                )
            ],
        ):
            history = self.chain._resolve_download_history(
                downloadhis=oper,
                file_path=Path("/downloads/电视剧/动漫/Ghost.Concert.mkv"),
            )

        self.assertIsNone(history)
