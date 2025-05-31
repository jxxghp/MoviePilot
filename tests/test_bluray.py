#!/usr/bin/env python
# -*- coding:utf-8 -*-
from pathlib import Path
from typing import List, Optional
from unittest import TestCase

from app import schemas
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.db.models.transferhistory import TransferHistory
from app.db.systemconfig_oper import SystemConfigOper
from app.db.transferhistory_oper import TransferHistoryOper
from tests.cases.files import bluray_files


class MockTransferHistoryOper(TransferHistoryOper):
    def __init__(self):
        # pylint: disable=super-init-not-called
        self.history = []

    def get_by_src(self, src, storage=None):
        self.history.append(src)
        return TransferHistory()


class MockStorage(StorageChain):
    def __init__(self, files: list):
        # pylint: disable=super-init-not-called
        self.__root = schemas.FileItem(
            path="/", name="", type="dir", extension="", size=0
        )
        self.__all = {self.__root.path: self.__root}

        def __build_child(parent: schemas.FileItem, files: list[dict]):
            parent.children = []
            for item in files:
                children = item.get("children")
                sep = "" if parent.path.endswith("/") else "/"
                name: str = item["name"]
                file_item = schemas.FileItem(
                    path=f"{parent.path}{sep}{name}",
                    name=name,
                    extension=Path(name).suffix[1:],
                    basename=Path(name).stem,
                    type="file" if children is None else "dir",
                    size=item.get("size", 0),
                )
                parent.children.append(file_item)
                self.__all[file_item.path] = file_item
                if children is not None:
                    __build_child(file_item, children)

        __build_child(self.__root, files)

    def list_files(
        self, fileitem: schemas.FileItem, recursion: bool = False
    ) -> Optional[List[schemas.FileItem]]:
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

    def get_file_item(self, storage: str, path: Path) -> Optional[schemas.FileItem]:
        """
        根据路径获取文件项
        """
        path_posix = path.as_posix()
        return self.__all.get(path_posix)


class MockTransferChain(TransferChain):
    def __init__(self, storage: MockStorage):
        # pylint: disable=super-init-not-called

        self.transferhis = MockTransferHistoryOper()
        self.systemconfig = SystemConfigOper()
        self.storagechain = storage

    def test(self, path: str):
        self.transferhis.history.clear()
        self.do_transfer(
            force=False,
            background=False,
            fileitem=self.storagechain.get_file_item(None, Path(path)),
        )
        return self.transferhis.history


class BluRayTest(TestCase):
    def __init__(self, methodName="test"):
        super().__init__(methodName)

    def setUp(self) -> None:
        pass

    def tearDown(self) -> None:
        pass

    def test(self):
        transfer = MockTransferChain(MockStorage(bluray_files))

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon (2055)",
                "/FOLDER/Digimon/Digimon (2099)",
                "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4",
            ],
            transfer.test("/FOLDER/Digimon"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon (2055)",
            ],
            transfer.test("/FOLDER/Digimon/Digimon (2055)"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon (2055)",
            ],
            transfer.test("/FOLDER/Digimon/Digimon (2055)/BDMV"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon (2055)",
            ],
            transfer.test("/FOLDER/Digimon/Digimon (2055)/BDMV/STREAM"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon (2055)",
            ],
            transfer.test("/FOLDER/Digimon/Digimon (2055)/BDMV/STREAM/00001.m2ts"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4",
            ],
            transfer.test("/FOLDER/Digimon/Digimon (2199)"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4",
            ],
            transfer.test("/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4"),
        )

        self.assertEqual(
            [
                "/FOLDER/Pokemon.2029.mp4",
            ],
            transfer.test("/FOLDER/Pokemon.2029.mp4"),
        )

        self.assertEqual(
            [
                "/FOLDER/Digimon/Digimon (2055)",
                "/FOLDER/Digimon/Digimon (2099)",
                "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4",
                "/FOLDER/Pokemon (2016)",
                "/FOLDER/Pokemon (2021)",
                "/FOLDER/Pokemon (2028)/Pokemon.2028.mkv",
                "/FOLDER/Pokemon.2029.mp4",
            ],
            transfer.test("/FOLDER"),
        )
