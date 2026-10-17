#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
验证蓝光目录识别、转移入口与元数据刮削。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

import pytest

from app.application.history import TransferHistorySnapshot
from app.chain.scraping import ScrapingChain
from app.chain.storage import StorageChain
from app.chain.transfer.facade import TransferChain
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfoPath
from app.runtime.events import Event
from app.runtime.log import logger
from app.schemas.file import FileItem
from app.schemas.types import EventType
from tests.cases.files import bluray_files


@pytest.fixture
def bluray_context():
    """
    构造独立的蓝光文件树与转移历史，供路径归并和刮削断言使用。

    :return: 含根目录、路径索引与历史记录的测试上下文
    """
    bluray_context = SimpleNamespace()
    bluray_context.history = []
    bluray_context.root = FileItem(path="/", name="", type="dir", extension="", size=0)
    bluray_context.items = {bluray_context.root.path: bluray_context.root}

    def __build_child(parent: FileItem, files: list[tuple[str, list | int]]):
        """
        递归构造目录子项并建立路径索引，不创建真实文件。
        """
        parent.children = []
        for name, children in files:
            sep = "" if parent.path.endswith("/") else "/"
            file_item = FileItem(
                path=f"{parent.path}{sep}{name}",
                name=name,
                extension=Path(name).suffix[1:],
                basename=Path(name).stem,
                type="file" if isinstance(children, int) else "dir",
                size=children if isinstance(children, int) else 0,
            )
            parent.children.append(file_item)
            bluray_context.items[file_item.path] = file_item
            if isinstance(children, list):
                __build_child(file_item, children)

    __build_child(bluray_context.root, bluray_files)

    return bluray_context


def _test_do_transfer(bluray_context):
    """
    验证各级蓝光路径归并到原盘目录，普通视频保留文件路径。
    """

    def __test_do_transfer(path: str):
        bluray_context.history.clear()
        TransferChain().do_transfer(
            force=False,
            background=False,
            fileitem=StorageChain().get_file_item(None, Path(path)),
        )
        return bluray_context.history

    assert [
        "/FOLDER/Digimon/Digimon BluRay (2055)",
        "/FOLDER/Digimon/Digimon BluRay (2099)",
        "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4",
    ] == __test_do_transfer("/FOLDER/Digimon")

    assert ["/FOLDER/Digimon/Digimon BluRay (2055)"] == __test_do_transfer("/FOLDER/Digimon/Digimon BluRay (2055)")

    assert ["/FOLDER/Digimon/Digimon BluRay (2055)"] == __test_do_transfer("/FOLDER/Digimon/Digimon BluRay (2055)/BDMV")

    assert ["/FOLDER/Digimon/Digimon BluRay (2055)"] == __test_do_transfer(
        "/FOLDER/Digimon/Digimon BluRay (2055)/BDMV/STREAM"
    )

    assert ["/FOLDER/Digimon/Digimon BluRay (2055)"] == __test_do_transfer(
        "/FOLDER/Digimon/Digimon BluRay (2055)/BDMV/STREAM/00001.m2ts"
    )

    assert ["/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4"] == __test_do_transfer("/FOLDER/Digimon/Digimon (2199)")

    assert ["/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4"] == __test_do_transfer(
        "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4"
    )

    assert ["/FOLDER/Pokemon.2029.mp4"] == __test_do_transfer("/FOLDER/Pokemon.2029.mp4")

    assert [
        "/FOLDER/Digimon/Digimon BluRay (2055)",
        "/FOLDER/Digimon/Digimon BluRay (2099)",
        "/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4",
        "/FOLDER/Pokemon BluRay (2016)",
        "/FOLDER/Pokemon BluRay (2021)",
        "/FOLDER/Pokemon (2028)/Pokemon.2028.mkv",
        "/FOLDER/Pokemon.2029.mp4",
        "/FOLDER/Pokemon.2039.mp4",
        "/FOLDER/Pokemon (2031)/Pokemon (2031).mp4",
    ] == __test_do_transfer("/")


def _test_scrape_metadata(mock_metadata_nfo):
    """
    验证手动和事件刮削对蓝光原盘及普通电影生成正确数量的元数据。
    """

    def __test_scrape_metadata(path: str, excepted_nfo_count: int = 1):
        """
        分别验证手动和自动刮削的元数据生成次数。

        :param path: 模拟文件树中的电影或目录路径
        :param excepted_nfo_count: 每种刮削入口应生成的元数据数量
        """
        fileitem = StorageChain().get_file_item(None, Path(path))
        meta = MetaInfoPath(Path(fileitem.path))
        mediainfo = MediaInfo(tmdb_info={"id": 1, "title": "Test"})

        # 测试手动刮削
        logger.debug(f"测试手动刮削 {path}")
        mock_metadata_nfo.reset_mock()
        ScrapingChain().scrape_metadata(fileitem=fileitem, meta=meta, mediainfo=mediainfo, overwrite=True)
        # 确保调用了指定次数的metadata_nfo
        assert mock_metadata_nfo.call_count == excepted_nfo_count

        # 测试自动刮削
        logger.debug(f"测试自动刮削 {path}")
        mock_metadata_nfo.reset_mock()
        ScrapingChain().scrape_metadata_event(
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
        assert mock_metadata_nfo.call_count == excepted_nfo_count

    # 刮削原盘目录
    __test_scrape_metadata("/FOLDER/Digimon/Digimon BluRay (2099)")
    # 刮削电影文件
    __test_scrape_metadata("/FOLDER/Digimon/Digimon (2199)/Digimon.2199.mp4")
    # 刮削电影目录
    __test_scrape_metadata("/FOLDER", excepted_nfo_count=2)


# 避免真实图片请求
@patch("app.chain.scraping.ScrapingChain.metadata_img", return_value=None)
@patch("app.chain.transfer.records.resolve_history")
@patch("app.chain.storage.StorageChain.list_files")
@patch("app.chain.storage.StorageChain.get_parent_item")
@patch("app.chain.storage.StorageChain.get_file_item")
def test_bluray_transfer_and_scraping(
    mock_get_file_item,
    mock_get_parent_item,
    mock_list_files,
    mock_resolve_history,
    mock_metadata_img,
    bluray_context,
) -> None:
    """
    验证模拟蓝光文件树的转移与刮削过程，不访问真实文件和图片服务。
    """

    def get_file_item(storage: str, path: Path):
        """
        从内存文件树返回路径对应的文件项。
        """
        path_posix = path.as_posix()
        return bluray_context.items.get(path_posix)

    def get_parent_item(fileitem: FileItem):
        """
        返回内存文件树中的父目录。
        """
        return get_file_item(None, Path(fileitem.path).parent)

    def list_files(fileitem: FileItem, recursion: bool = False):
        """
        列出模拟目录子项，递归模式按路径前缀收集所有后代。
        """
        if fileitem.type != "dir":
            return None
        if recursion:
            result = []
            file_path = f"{fileitem.path}/"
            for path, item in bluray_context.items.items():
                if path.startswith(file_path):
                    result.append(item)
            return result
        else:
            return fileitem.children

    def resolve_history(
        src: str,
        storage: Optional[str] = None,
        transfer_history_oper=None,
    ):
        """
        记录被解析的源路径并返回成功历史，避免读取真实数据库。
        """
        bluray_context.history.append(src)
        return TransferHistorySnapshot(id=1, status=True)

    mock_get_file_item.side_effect = get_file_item
    mock_get_parent_item.side_effect = get_parent_item
    mock_list_files.side_effect = list_files
    mock_resolve_history.side_effect = resolve_history

    _test_do_transfer(bluray_context)

    with patch("app.chain.scraping.ScrapingChain.metadata_nfo", return_value=None) as mock:
        _test_scrape_metadata(mock_metadata_nfo=mock)
