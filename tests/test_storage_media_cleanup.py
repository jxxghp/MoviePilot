"""
媒体文件删除的共享目录边界回归。
"""

import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.application.classification.reference import ClassificationCategoryResolver
from app.application.configuration import ChainRuntimeConfig
from app.application.directory import DirectoryHelper
from app.chain.storage import StorageChain
from app.schemas.category import ClassificationCategory, ClassificationPolicy
from app.schemas.file import FileItem
from app.schemas.system import TransferDirectoryConf


@pytest.fixture
def cleanup(monkeypatch):
    """
    用临时文件系统执行真实删除，仅替换存储和配置端口。
    """
    policy = ClassificationPolicy(categories=[
        ClassificationCategory(id="movie.chinese", media_type="电影", name="华语电影", path=["华语电影"]),
        ClassificationCategory(id="movie.asia", media_type="电影", name="华语", path=["亚洲", "华语"]),
        ClassificationCategory(id="tv.chinese", media_type="电视剧", name="国产剧", path=["电视剧", "国产剧"]),
    ])
    helper = DirectoryHelper(ClassificationCategoryResolver(lambda: policy))
    directories = []
    monkeypatch.setattr(helper, "get_dirs", lambda: directories)
    monkeypatch.setattr("app.chain.storage.DirectoryHelper", lambda: helper)
    chain = object.__new__(StorageChain)
    chain.runtime_config = ChainRuntimeConfig(media_extensions=(".mkv",))
    deleted = []

    def delete(fileitem):
        """
        记录并删除临时目录中的目标，缺失目录视作已完成。
        """
        path = Path(fileitem.path)
        deleted.append(path)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        return True

    def parent(fileitem):
        """
        构造相同存储中的上级目录项。
        """
        return FileItem(storage=fileitem.storage, path=str(Path(fileitem.path).parent), type="dir")

    def any_media(fileitem, extensions):
        """
        扫描临时目录，保留仍有媒体文件的目录。
        """
        return any(path.suffix in extensions for path in Path(fileitem.path).rglob("*"))

    monkeypatch.setattr(chain, "delete_file", delete)
    monkeypatch.setattr(chain, "get_parent_item", parent)
    monkeypatch.setattr(chain, "any_files", any_media)
    monkeypatch.setattr(chain, "list_files", lambda item, **_kwargs: list(Path(item.path).iterdir()))
    monkeypatch.setattr(chain, "is_bluray_folder", lambda _item: False)
    return chain, helper, directories, deleted


def _media_file(root, relative_path, storage="local"):
    """
    创建媒体及刮削附件，验证影片目录仍按原行为一起清理。
    """
    path = root / relative_path / "电影.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"media")
    path.with_suffix(".nfo").write_text("metadata")
    return FileItem(path=str(path), type="file", storage=storage)


@pytest.mark.parametrize("options,relative_path,boundary", [
    ({"library_category_folder": True}, "华语电影/某片 (2026)", "华语电影"),
    ({"library_category_folder": True}, "亚洲/华语/某片 (2026)", "亚洲/华语"),
    ({"library_type_folder": True, "library_category_folder": True}, "电影/华语电影/某片 (2026)", "电影/华语电影"),
    ({"library_type_folder": True, "library_category_folder": True}, "电视剧/国产剧/某剧/Season 1", "电视剧/国产剧"),
    ({"media_type": "电影", "library_type_folder": True}, "电影/某片 (2026)", "电影"),
    ({"library_category_folder": True, "media_category_id": "movie.asia", "media_category": "旧分类"}, "亚洲/华语/某片 (2026)", "亚洲/华语"),
    ({"library_category_folder": True, "media_category": "人工/华语"}, "人工/华语/某片 (2026)", "人工/华语"),
    ({"library_category_folder": True}, "已删除的分类/某片 (2026)", "已删除的分类"),
    ({}, "某片 (2026)", ""),
])
def test_library_cleanup_preserves_shared_directories(tmp_path, cleanup, options, relative_path, boundary):
    """
    删除最后一个媒体和影片目录后，保留根目录及多级类型、分类结构。
    """
    chain, _helper, directories, deleted = cleanup
    directories.append(TransferDirectoryConf(library_path=str(tmp_path), library_storage="local", transfer_type="link", **options))
    fileitem = _media_file(tmp_path, relative_path)
    preserved = tmp_path / boundary

    assert chain.delete_media_file(fileitem) is True

    assert preserved.is_dir()
    assert not Path(fileitem.path).parent.exists()
    assert preserved not in deleted
    assert tmp_path not in deleted


@pytest.mark.parametrize("options,relative_path,boundary", [
    ({"download_category_folder": True}, "亚洲/华语/资源包", "亚洲/华语"),
    ({"download_type_folder": True, "download_category_folder": True}, "电影/华语电影/资源包", "电影/华语电影"),
    ({"download_category_folder": True, "media_category": "华语电影"}, "资源包", ""),
    ({"media_type": "电影", "download_type_folder": True}, "资源包", ""),
])
def test_source_cleanup_uses_download_folder_rules(tmp_path, cleanup, options, relative_path, boundary):
    """
    源文件删除和移动后清理遵守下载目录自身的子目录开关。
    """
    chain, _helper, directories, deleted = cleanup
    directories.append(TransferDirectoryConf(download_path=str(tmp_path), storage="local", **options))
    fileitem = _media_file(tmp_path, relative_path)
    Path(fileitem.path).unlink()

    assert chain.delete_media_file(fileitem, delete_self=False) is True

    assert (tmp_path / boundary).is_dir()
    assert not Path(fileitem.path).parent.exists()
    assert Path(fileitem.path) not in deleted


def test_cleanup_chooses_deepest_root_in_same_storage(tmp_path, cleanup):
    """
    嵌套配置取最深边界；其他存储同名路径不赋予附件递归删除权限。
    """
    chain, helper, directories, deleted = cleanup
    root = tmp_path / "nested"
    directories.extend([
        TransferDirectoryConf(library_path=str(tmp_path), library_storage="local"),
        TransferDirectoryConf(download_path=str(root), storage="local"),
        TransferDirectoryConf(library_path=str(root / "影片"), library_storage="u115"),
    ])
    fileitem = _media_file(root, "影片")
    assert helper.get_media_cleanup_root(fileitem) == root
    assert chain.delete_media_file(fileitem) is True
    assert root.is_dir()
    assert root not in deleted


@pytest.mark.parametrize("storage", ["local", "u115"])
def test_cleanup_without_matching_storage_preserves_nonempty_parent(tmp_path, cleanup, storage):
    """
    无匹配配置时只删除真正空目录，不能连带删除刮削附件。
    """
    chain, helper, directories, _deleted = cleanup
    directories.append(TransferDirectoryConf(library_path=str(tmp_path), library_storage="u115" if storage == "local" else "local"))
    fileitem = _media_file(tmp_path, "影片", storage=storage)
    assert helper.get_media_cleanup_root(fileitem) is None
    assert chain.delete_media_file(fileitem) is True
    assert Path(fileitem.path).with_suffix(".nfo").exists()


@pytest.mark.parametrize("query_result", [True, None])
def test_cleanup_preserves_other_media_or_unknown_query(tmp_path, cleanup, monkeypatch, query_result):
    """
    仍有媒体或无法确认媒体查询结果时，停止向上清理。
    """
    chain, _helper, directories, deleted = cleanup
    directories.append(TransferDirectoryConf(library_path=str(tmp_path), library_storage="local"))
    fileitem = _media_file(tmp_path, "影片")
    monkeypatch.setattr(chain, "any_files", Mock(return_value=query_result))
    assert chain.delete_media_file(fileitem) is True
    assert Path(fileitem.path).parent.is_dir()
    assert deleted == [Path(fileitem.path)]


def test_cleanup_propagates_directory_delete_failure(tmp_path, cleanup, monkeypatch):
    """
    影片目录删除失败时返回失败并停止，不继续触碰共享分类目录。
    """
    chain, _helper, directories, _deleted = cleanup
    directories.append(TransferDirectoryConf(library_path=str(tmp_path), library_category_folder=True))
    fileitem = _media_file(tmp_path, "华语电影/影片")
    delete = Mock(side_effect=[True, False])
    monkeypatch.setattr(chain, "delete_file", delete)
    monkeypatch.setattr(chain, "any_files", Mock(return_value=False))
    assert chain.delete_media_file(fileitem) is False
    assert delete.call_count == 2
    assert (tmp_path / "华语电影").is_dir()


def test_cleanup_preserves_sibling_media(tmp_path, cleanup):
    """
    删除一部影片后，同分类下的其他影片及共享分类目录保持完整。
    """
    chain, _helper, directories, _deleted = cleanup
    directories.append(TransferDirectoryConf(library_path=str(tmp_path), library_category_folder=True))
    fileitem = _media_file(tmp_path, "华语电影/影片甲")
    sibling = _media_file(tmp_path, "华语电影/影片乙")
    assert chain.delete_media_file(fileitem) is True
    assert not Path(fileitem.path).parent.exists()
    assert Path(sibling.path).is_file()


def test_bluray_cleanup_preserves_category(tmp_path, cleanup, monkeypatch):
    """
    蓝光原盘目录整体删除后，也必须在共享分类边界停止。
    """
    chain, _helper, directories, deleted = cleanup
    directories.append(TransferDirectoryConf(library_path=str(tmp_path), library_category_folder=True))
    fileitem = _media_file(tmp_path, "华语电影/原盘/BDMV")
    bluray = Path(fileitem.path).parent.parent
    monkeypatch.setattr(chain, "is_bluray_folder", lambda _item: True)
    monkeypatch.setattr(chain, "any_files", Mock(return_value=False))
    assert chain.delete_media_file(FileItem(path=str(bluray), type="dir", storage="local")) is True
    assert not bluray.exists()
    assert (tmp_path / "华语电影").is_dir()
    assert tmp_path / "华语电影" not in deleted
