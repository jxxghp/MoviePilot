from types import SimpleNamespace
from unittest.mock import patch

from app.core.context import MediaInfo
from app.modules.filemanager import FileManagerModule
from app.schemas.types import MediaType


def test_local_media_exists_keeps_special_season_zero():
    """本地 S00 文件必须归入特别季，不能计入第一季。"""
    module = FileManagerModule()
    module.media_files = lambda _mediainfo: [SimpleNamespace(basename="Test.Show.S00E01.mkv")]
    mediainfo = MediaInfo(title="Test Show", type=MediaType.TV)

    with patch("app.modules.filemanager.settings.LOCAL_EXISTS_SEARCH", True):
        exists = module.media_exists(mediainfo)

    assert exists.seasons == {0: [1]}
