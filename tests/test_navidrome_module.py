"""Navidrome 媒体服务器模块接入测试。"""
from app.core.module import ModuleManager
from app.modules.navidrome import NavidromeModule
from app.schemas.types import MediaServerType, ModuleType


def test_navidrome_module_declares_media_server_identity():
    """Navidrome 应以媒体服务器身份注册，供统一媒体服务器链调用。"""
    assert NavidromeModule.get_name() == "Navidrome"
    assert NavidromeModule.get_type() == ModuleType.MediaServer
    assert NavidromeModule.get_subtype() == MediaServerType.Navidrome


def test_navidrome_module_has_no_system_switch():
    """Navidrome 由服务配置控制启用，不能返回无效的系统开关名。"""
    assert NavidromeModule().init_setting() is None


def test_navidrome_module_is_loaded_by_module_manager():
    """模块管理器应能加载 Navidrome，否则媒体服务器列表里不会出现该类型。"""
    assert "NavidromeModule" in ModuleManager()._running_modules


def test_navidrome_module_ignores_non_music_media():
    """Navidrome 只管理音乐，影视存在性检查应交给其它媒体服务器。"""
    from app.core.context import MediaInfo
    from app.schemas.types import MediaType

    mediainfo = MediaInfo()
    mediainfo.type = MediaType.MOVIE

    assert NavidromeModule().media_exists(mediainfo) is None
