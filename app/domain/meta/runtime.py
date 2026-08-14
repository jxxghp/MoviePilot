from typing import Callable, Optional, Protocol, Sequence


class MetaInfoAccelerator(Protocol):
    """领域识别可选使用的加速器契约，具体实现由启动层注入。"""

    def parse_metainfo(
        self,
        title: str,
        subtitle: Optional[str] = None,
        options: Optional[dict] = None,
    ) -> Optional[dict]:
        """解析单个标题，无法处理时返回空值。"""

    def parse_metainfo_path(
        self,
        path: str,
        options: Optional[dict] = None,
    ) -> Optional[dict]:
        """解析文件路径，无法处理时返回空值。"""

    def parse_metamusic(
        self,
        title: str,
        artists: Optional[list[str]] = None,
        year: Optional[int] = None,
    ) -> Optional[dict]:
        """解析音乐标题，无法处理时返回空值。"""

    def find_metainfo(self, title: str) -> Optional[dict]:
        """提取标题中的显式媒体标签。"""

    def supports_extended_media_ids(self) -> bool:
        """返回加速器是否支持扩展媒体来源标签。"""


_media_extensions_provider: Callable[[], Sequence[str]] = lambda: ()
_audio_extensions_provider: Callable[[], Sequence[str]] = lambda: ()
_metainfo_accelerator: Optional[MetaInfoAccelerator] = None


def configure_recognition_runtime(
    *,
    media_extensions_provider: Callable[[], Sequence[str]],
    audio_extensions_provider: Callable[[], Sequence[str]],
    accelerator: Optional[MetaInfoAccelerator],
) -> None:
    """注入文件类型配置和可选加速器，保持领域解析器与平台实现解耦。"""
    global _media_extensions_provider, _audio_extensions_provider
    global _metainfo_accelerator
    _media_extensions_provider = media_extensions_provider
    _audio_extensions_provider = audio_extensions_provider
    _metainfo_accelerator = accelerator


def get_media_extensions() -> tuple[str, ...]:
    """返回当前影视、字幕和音频文件后缀。"""
    return tuple(_media_extensions_provider() or ())


def get_audio_extensions() -> tuple[str, ...]:
    """返回当前音频文件后缀。"""
    return tuple(_audio_extensions_provider() or ())


def get_metainfo_accelerator() -> Optional[MetaInfoAccelerator]:
    """返回启动层注入的可选识别加速器。"""
    return _metainfo_accelerator
