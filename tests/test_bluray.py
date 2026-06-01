#!/usr/bin/env python
# -*- coding:utf-8 -*-
from pathlib import Path
from typing import Optional
from unittest import TestCase
from unittest.mock import patch

from app import schemas
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.context import MediaInfo
from app.core.event import Event
from app.core.metainfo import MetaInfoPath
from app.db.models.transferhistory import TransferHistory
from app.log import logger
from app.schemas.types import EventType
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
