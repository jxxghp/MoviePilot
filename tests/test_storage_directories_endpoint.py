"""存储目录与存储选项查询接口测试。"""

from unittest.mock import patch

from app.api.endpoints.storage import directory_settings, storage_options
from app.schemas.system import StorageConf, TransferDirectoryConf


def test_directory_settings_projects_complete_selection_contract() -> None:
    """目录设置查询应返回分类引用和所有路径分层开关。"""
    directory = TransferDirectoryConf(
        name="日番库",
        download_type_folder=True,
        download_category_folder=True,
        library_path="/library/anime",
        library_storage="local",
        library_type_folder=True,
        library_category_folder=True,
        media_type="电视剧",
        media_category_id="tv.anime.jp",
        media_category="动漫/日番",
    )

    with patch(
        "app.api.endpoints.storage.DirectoryHelper.get_dirs",
        return_value=[directory],
    ):
        response = directory_settings(_=object())

    assert response.success is True
    assert response.data[0]["media_category_id"] == "tv.anime.jp"
    assert response.data[0]["media_category"] == "动漫/日番"
    assert response.data[0]["download_type_folder"] is True
    assert response.data[0]["download_category_folder"] is True
    assert response.data[0]["library_type_folder"] is True
    assert response.data[0]["library_category_folder"] is True


def test_storage_options_excludes_connection_configuration() -> None:
    """存储选项只能投影显示名称和类型，不得返回连接配置。"""
    storages = [
        StorageConf(type="local", name="本地", config={"path": "/secret"}),
        StorageConf(type="rclone", name="网盘", config={"password": "secret"}),
    ]

    with patch(
        "app.api.endpoints.storage.StorageHelper.get_storagies",
        return_value=storages,
    ):
        response = storage_options(_=object())

    assert [item.model_dump() for item in response] == [
        {"name": "本地", "type": "local"},
        {"name": "网盘", "type": "rclone"},
    ]


def test_transfer_directory_round_trip_keeps_stable_reference_snapshot() -> None:
    """SystemConfig JSON 往返不得丢失目录分类 ID 或路径快照。"""
    payload = {
        "name": "现场音乐",
        "media_type": "music",
        "media_category_id": "music.live",
        "media_category": "音乐/现场",
    }

    restored = TransferDirectoryConf.model_validate(payload)

    assert restored.model_dump()["media_category_id"] == "music.live"
    assert restored.model_dump()["media_category"] == "音乐/现场"
