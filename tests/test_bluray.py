#!/usr/bin/env python
# -*- coding:utf-8 -*-
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional
from unittest import TestCase
from unittest.mock import patch

from app import schemas
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import Event
from app.core.metainfo import MetaInfoPath
from app.db.models.transferhistory import TransferHistory
from app.log import logger
from app.modules.filemanager.storages.local import LocalStorage
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.types import EventType, MediaType
from tests.cases.files import bluray_files


class BluRayTest(TestCase):
    def __init__(self, methodName="test"):
        super().__init__(methodName)
        self.__history = []
        self.__root = schemas.FileItem(
            path="/", name="", type="dir", extension="", size=0
        )
        self.__all = {self.__root.path: self.__root}

        def __build_child(parent: schemas.FileItem, files: list[tuple[str, list | int]]):
            parent.children = []
            for name, children in files:
                sep = "" if parent.path.endswith("/") else "/"
                file_item = schemas.FileItem(
                    path=f"{parent.path}{sep}{name}",
                    name=name,
                    extension=Path(name).suffix[1:],
                    basename=Path(name).stem,
                    type="file" if isinstance(children, int) else "dir",
                    size=children if isinstance(children, int) else 0,
                )
                parent.children.append(file_item)
                self.__all[file_item.path] = file_item
                if isinstance(children, list):
                    __build_child(file_item, children)

        __build_child(self.__root, bluray_files)

    def _test_do_transfer(self):
        def __test_do_transfer(path: str):
            self.__history.clear()
            TransferChain().do_transfer(
                force=False,
                background=False,
                fileitem=StorageChain().get_file_item(None, Path(path)),
            )
            return self.__history

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon BluRay (2055)",
                "/FOLDER/Digimon/Digimon BluRay (2099)",
                "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4",
            ],
            __test_do_transfer("/FOLDER/Digimon"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon BluRay (2055)",
            ],
            __test_do_transfer("/FOLDER/Digimon/Digimon BluRay (2055)"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon BluRay (2055)",
            ],
            __test_do_transfer("/FOLDER/Digimon/Digimon BluRay (2055)/BDMV"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon BluRay (2055)",
            ],
            __test_do_transfer("/FOLDER/Digimon/Digimon BluRay (2055)/BDMV/STREAM"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon BluRay (2055)",
            ],
            __test_do_transfer(
                "/FOLDER/Digimon/Digimon BluRay (2055)/BDMV/STREAM/00001.m2ts"
            ),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4",
            ],
            __test_do_transfer("/FOLDER/Digimon/Digimon (2199)"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4",
            ],
            __test_do_transfer("/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4"),
        )

        self.assertEqual(
            [
                "/FOLDER/Pokemon.2029.mp4",
            ],
            __test_do_transfer("/FOLDER/Pokemon.2029.mp4"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon BluRay (2055)",
                "/FOLDER/Digimon/Digimon BluRay (2099)",
                "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4",
                "/FOLDER/Pokemon BluRay (2016)",
                "/FOLDER/Pokemon BluRay (2021)",
                "/FOLDER/Pokemon (2028)/Pokemon.2028.mkv",
                "/FOLDER/Pokemon.2029.mp4",
                "/FOLDER/Pokemon.2039.mp4",
                "/FOLDER/Pokemon (2031)/Pokemon (2031).mp4",
            ],
            __test_do_transfer("/"),
        )

    def _test_scrape_metadata(self, mock_metadata_nfo):
        def __test_scrape_metadata(path: str, excepted_nfo_count: int = 1):
            """
            分别测试手动和自动刮削
            """
            fileitem = StorageChain().get_file_item(None, Path(path))
            meta = MetaInfoPath(Path(fileitem.path))
            mediainfo = MediaInfo(tmdb_info={"id": 1, "title": "Test"})

            # 测试手动刮削
            logger.debug(f"测试手动刮削 {path}")
            mock_metadata_nfo.call_count = 0
            MediaChain().scrape_metadata(
                fileitem=fileitem, meta=meta, mediainfo=mediainfo, overwrite=True
            )
            # 确保调用了指定次数的metadata_nfo
            self.assertEqual(mock_metadata_nfo.call_count, excepted_nfo_count)

            # 测试自动刮削
            logger.debug(f"测试自动刮削 {path}")
            mock_metadata_nfo.call_count = 0
            MediaChain().scrape_metadata_event(
                Event(
                    event_type=EventType.MetadataScrape,
                    event_data={
                        "meta": meta,
                        "mediainfo": mediainfo,
                        "fileitem": fileitem,
                        "file_list": [fileitem.path],
                        "overwrite": False,
                    },
                )
            )
            # 调用了指定次数的metadata_nfo
            self.assertEqual(mock_metadata_nfo.call_count, excepted_nfo_count)

        # 刮削原盘目录
        __test_scrape_metadata("/FOLDER/Digimon/Digimon BluRay (2099)")
        # 刮削电影文件
        __test_scrape_metadata("/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4")
        # 刮削电影目录
        __test_scrape_metadata("/FOLDER", excepted_nfo_count=2)

    @patch("app.chain.media.MediaChain.metadata_img", return_value=None)  # 避免获取图片
    @patch("app.chain.ChainBase.__init__", return_value=None)  # 避免不必要的模块初始化
    @patch("app.db.transferhistory_oper.TransferHistoryOper.get_by_src")
    @patch("app.chain.storage.StorageChain.list_files")
    @patch("app.chain.storage.StorageChain.get_parent_item")
    @patch("app.chain.storage.StorageChain.get_file_item")
    def test(
        self,
        mock_get_file_item,
        mock_get_parent_item,
        mock_list_files,
        mock_get_by_src,
        *_,
    ):
        def get_file_item(storage: str, path: Path):
            path_posix = path.as_posix()
            return self.__all.get(path_posix)

        def get_parent_item(fileitem: schemas.FileItem):
            return get_file_item(None, Path(fileitem.path).parent)

        def list_files(fileitem: schemas.FileItem, recursion: bool = False):
            if fileitem.type != "dir":
                return None
            if recursion:
                result = []
                file_path = f"{fileitem.path}/"
                for path, item in self.__all.items():
                    if path.startswith(file_path):
                        result.append(item)
                return result
            else:
                return fileitem.children

        def get_by_src(src: str, storage: Optional[str] = None):
            self.__history.append(src)
            result = TransferHistory()
            result.status = True
            return result

        mock_get_file_item.side_effect = get_file_item
        mock_get_parent_item.side_effect = get_parent_item
        mock_list_files.side_effect = list_files
        mock_get_by_src.side_effect = get_by_src

        self._test_do_transfer()

        with patch(
            "app.chain.media.MediaChain.metadata_nfo", return_value=None
        ) as mock:
            self._test_scrape_metadata(mock_metadata_nfo=mock)


class BluRayRemuxTest(TestCase):
    @staticmethod
    def __write_mpls(path: Path, stream_names: list[str]):
        """
        写入只包含基础 PlayItem 的最小 MPLS 测试文件。
        """
        items = b""
        for stream_name in stream_names:
            clip_name = Path(stream_name).stem.encode("ascii")[:5].ljust(5, b"\x00")
            body = (
                clip_name
                + b"M2TS"
                + b"\x00"
                + b"\x01"
                + (0).to_bytes(4, "big")
                + (90000).to_bytes(4, "big")
            )
            items += len(body).to_bytes(2, "big") + body
        playlist = (
            len(items).to_bytes(4, "big")
            + b"\x00\x00"
            + len(stream_names).to_bytes(2, "big")
            + (0).to_bytes(2, "big")
            + items
        )
        path.write_bytes(b"MPLS0200" + (16).to_bytes(4, "big") + b"\x00" * 4 + playlist)

    @staticmethod
    def __create_bluray_dir(
            root: Path,
            playlist_name: str = "00000.mpls",
    ) -> schemas.FileItem:
        stream_dir = root / "BDMV" / "STREAM"
        playlist_dir = root / "BDMV" / "PLAYLIST"
        stream_dir.mkdir(parents=True)
        playlist_dir.mkdir(parents=True)
        (stream_dir / "00000.m2ts").write_bytes(b"0" * 10)
        (stream_dir / "00001.m2ts").write_bytes(b"1" * 20)
        BluRayRemuxTest.__write_mpls(
            playlist_dir / playlist_name,
            ["00000.m2ts", "00001.m2ts"],
        )
        return schemas.FileItem(
            storage="local",
            type="dir",
            path=root.as_posix(),
            name=root.name,
            basename=root.stem,
            size=0,
        )

    @staticmethod
    def __movie_info() -> MediaInfo:
        mediainfo = MediaInfo()
        mediainfo.type = MediaType.MOVIE
        mediainfo.title = "BluRay Movie"
        mediainfo.year = "2024"
        return mediainfo

    @staticmethod
    def __tv_info() -> MediaInfo:
        mediainfo = MediaInfo()
        mediainfo.type = MediaType.TV
        mediainfo.title = "BluRay Show"
        mediainfo.year = "2024"
        return mediainfo

    def test_bluray_remux_disabled_keeps_directory_transfer(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_item = self.__create_bluray_dir(root / "BluRay Movie Source")
            target_path = root / "media"

            with patch.object(settings, "BLURAY_REMUX_ENABLED", False), patch(
                "app.modules.filemanager.transhandler.subprocess.run"
            ) as mock_run:
                result = TransHandler().transfer_media(
                    fileitem=source_item,
                    in_meta=MetaInfoPath(Path("BluRay Movie (2024)")),
                    mediainfo=self.__movie_info(),
                    target_storage="local",
                    target_path=target_path,
                    transfer_type="copy",
                    source_oper=LocalStorage(),
                    target_oper=LocalStorage(),
                )

            self.assertTrue(result.success)
            self.assertEqual(result.target_item.type, "dir")
            self.assertFalse(mock_run.called)

    def test_bluray_remux_enabled_uses_playlist_concat(self):
        def run_ffmpeg(command, *_args, **_kwargs):
            output_path = Path(command[-1])
            output_path.write_bytes(b"mkv")
            return subprocess.CompletedProcess(command, 0, "", "")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_item = self.__create_bluray_dir(root / "BluRay Movie Source")
            target_path = root / "media"

            with patch.object(settings, "BLURAY_REMUX_ENABLED", True), patch(
                "app.modules.filemanager.transhandler.shutil.which",
                return_value="ffmpeg",
            ), patch(
                "app.modules.filemanager.transhandler.subprocess.run",
                side_effect=run_ffmpeg,
            ) as mock_run:
                result = TransHandler().transfer_media(
                    fileitem=source_item,
                    in_meta=MetaInfoPath(Path("BluRay Movie (2024)")),
                    mediainfo=self.__movie_info(),
                    target_storage="local",
                    target_path=target_path,
                    transfer_type="copy",
                    source_oper=LocalStorage(),
                    target_oper=LocalStorage(),
                )

            target_file = target_path / "BluRay Movie (2024)" / "BluRay Movie (2024).mkv"
            self.assertTrue(result.success)
            self.assertEqual(result.target_item.path, target_file.as_posix())
            self.assertTrue(target_file.exists())
            command = mock_run.call_args.args[0]
            self.assertIn("concat", command)
            self.assertIn("-safe", command)

    def test_bluray_remux_accepts_uppercase_playlist_suffix(self):
        def run_ffmpeg(command, *_args, **_kwargs):
            output_path = Path(command[-1])
            output_path.write_bytes(b"mkv")
            return subprocess.CompletedProcess(command, 0, "", "")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_item = self.__create_bluray_dir(
                root / "BluRay Movie Source",
                playlist_name="00000.MPLS",
            )

            with patch.object(settings, "BLURAY_REMUX_ENABLED", True), patch(
                "app.modules.filemanager.transhandler.shutil.which",
                return_value="ffmpeg",
            ), patch(
                "app.modules.filemanager.transhandler.subprocess.run",
                side_effect=run_ffmpeg,
            ) as mock_run:
                result = TransHandler().transfer_media(
                    fileitem=source_item,
                    in_meta=MetaInfoPath(Path("BluRay Movie (2024)")),
                    mediainfo=self.__movie_info(),
                    target_storage="local",
                    target_path=root / "media",
                    transfer_type="copy",
                    source_oper=LocalStorage(),
                    target_oper=LocalStorage(),
                )

            self.assertTrue(result.success)
            command = mock_run.call_args.args[0]
            self.assertIn("concat", command)

    def test_bluray_remux_rejects_unmatched_playlist(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "BluRay Movie Source"
            source_item = self.__create_bluray_dir(source_root)
            self.__write_mpls(
                source_root / "BDMV" / "PLAYLIST" / "00000.mpls",
                ["99999.m2ts"],
            )

            with patch.object(settings, "BLURAY_REMUX_ENABLED", True), patch(
                "app.modules.filemanager.transhandler.shutil.which",
                return_value="ffmpeg",
            ), patch(
                "app.modules.filemanager.transhandler.subprocess.run"
            ) as mock_run:
                result = TransHandler().transfer_media(
                    fileitem=source_item,
                    in_meta=MetaInfoPath(Path("BluRay Movie (2024)")),
                    mediainfo=self.__movie_info(),
                    target_storage="local",
                    target_path=root / "media",
                    transfer_type="move",
                    source_oper=LocalStorage(),
                    target_oper=LocalStorage(),
                    need_rename=False,
                )

            self.assertFalse(result.success)
            self.assertTrue(source_root.exists())
            self.assertFalse(mock_run.called)

    def test_tv_bluray_remux_keeps_directory_transfer(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_item = self.__create_bluray_dir(root / "BluRay Show S01E01")

            with patch.object(settings, "BLURAY_REMUX_ENABLED", True), patch(
                "app.modules.filemanager.transhandler.subprocess.run"
            ) as mock_run:
                result = TransHandler().transfer_media(
                    fileitem=source_item,
                    in_meta=MetaInfoPath(Path("BluRay Show S01E01")),
                    mediainfo=self.__tv_info(),
                    target_storage="local",
                    target_path=root / "media",
                    transfer_type="copy",
                    source_oper=LocalStorage(),
                    target_oper=LocalStorage(),
                    need_rename=False,
                )

            self.assertTrue(result.success)
            self.assertEqual(result.target_item.type, "dir")
            self.assertFalse(mock_run.called)

    def test_bluray_remux_failure_keeps_latest_version_files(self):
        def run_ffmpeg(command, *_args, **_kwargs):
            return subprocess.CompletedProcess(command, 1, "", "failed")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_item = self.__create_bluray_dir(root / "BluRay Movie Source")
            old_file = (
                root
                / "media"
                / "BluRay Movie (2024)"
                / "BluRay Movie (2024).old.mkv"
            )
            old_file.parent.mkdir(parents=True)
            old_file.write_bytes(b"old")

            with patch.object(settings, "BLURAY_REMUX_ENABLED", True), patch(
                "app.modules.filemanager.transhandler.shutil.which",
                return_value="ffmpeg",
            ), patch(
                "app.modules.filemanager.transhandler.subprocess.run",
                side_effect=run_ffmpeg,
            ):
                result = TransHandler().transfer_media(
                    fileitem=source_item,
                    in_meta=MetaInfoPath(Path("BluRay Movie (2024)")),
                    mediainfo=self.__movie_info(),
                    target_storage="local",
                    target_path=root / "media",
                    transfer_type="copy",
                    source_oper=LocalStorage(),
                    target_oper=LocalStorage(),
                    overwrite_mode="latest",
                )

            self.assertFalse(result.success)
            self.assertTrue(old_file.exists())

    def test_bluray_remux_rejects_newline_in_concat_path(self):
        with self.assertRaises(ValueError):
            TransHandler._TransHandler__escape_ffconcat_path(
                Path("BluRay\nMovie Source") / "BDMV" / "STREAM" / "00000.m2ts"
            )

    def test_bluray_remux_move_rejects_target_inside_source(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "BluRay Movie Source"
            source_item = self.__create_bluray_dir(source_root)

            with patch.object(settings, "BLURAY_REMUX_ENABLED", True), patch(
                "app.modules.filemanager.transhandler.subprocess.run"
            ) as mock_run:
                result = TransHandler().transfer_media(
                    fileitem=source_item,
                    in_meta=MetaInfoPath(Path("BluRay Movie (2024)")),
                    mediainfo=self.__movie_info(),
                    target_storage="local",
                    target_path=source_root,
                    transfer_type="move",
                    source_oper=LocalStorage(),
                    target_oper=LocalStorage(),
                    need_rename=False,
                )

            self.assertFalse(result.success)
            self.assertTrue(source_root.exists())
            self.assertFalse(mock_run.called)

    def test_bluray_remux_failure_keeps_existing_target_file(self):
        def run_ffmpeg(command, *_args, **_kwargs):
            return subprocess.CompletedProcess(command, 1, "", "failed")

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_item = self.__create_bluray_dir(root / "BluRay Movie Source")
            target_file = (
                root
                / "media"
                / "BluRay Movie (2024)"
                / "BluRay Movie (2024).mkv"
            )
            target_file.parent.mkdir(parents=True)
            target_file.write_bytes(b"old")

            with patch.object(settings, "BLURAY_REMUX_ENABLED", True), patch(
                "app.modules.filemanager.transhandler.shutil.which",
                return_value="ffmpeg",
            ), patch(
                "app.modules.filemanager.transhandler.subprocess.run",
                side_effect=run_ffmpeg,
            ):
                result = TransHandler().transfer_media(
                    fileitem=source_item,
                    in_meta=MetaInfoPath(Path("BluRay Movie (2024)")),
                    mediainfo=self.__movie_info(),
                    target_storage="local",
                    target_path=root / "media",
                    transfer_type="copy",
                    source_oper=LocalStorage(),
                    target_oper=LocalStorage(),
                    overwrite_mode="always",
                )

            self.assertFalse(result.success)
            self.assertEqual(target_file.read_bytes(), b"old")
