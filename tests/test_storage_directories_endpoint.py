"""存储目录查询接口的稳定分类引用投影测试。"""

from unittest.mock import patch

from app.api.endpoints.storage import directory_settings
from app.schemas.system import TransferDirectoryConf


def test_directory_settings_projects_category_id_and_path_snapshot() -> None:
    """目录设置查询应同时返回稳定分类 ID 和兼容路径快照。"""
    directory = TransferDirectoryConf(
        name="日番库",
        library_path="/library/anime",
        library_storage="local",
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
