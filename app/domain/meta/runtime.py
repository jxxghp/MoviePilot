"""
声明识别加速器契约，并保存启动层注入的文件类型与加速能力。
"""

from typing import Any, Callable, Optional, Protocol, Sequence


class MetaInfoAccelerator(Protocol):
    """
    领域识别可选使用的加速器契约，具体实现由启动层注入。
    """

    def parse_metainfo(
        self,
        title: str,
        subtitle: Optional[str] = None,
        options: Optional[dict[str, object]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        解析单个标题，无法处理时返回空值。

        :param title: 待解析的媒体标题
        :param subtitle: 可补充识别信息的副标题；未提供时为 None
        :param options: 调用方提供的解析选项；未提供时使用加速器默认值
        :return: 解析出的字段字典；无法处理时返回 None，交由 Python 实现回退
        """

    def parse_metainfo_path(
        self,
        path: str,
        options: Optional[dict[str, object]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        解析文件路径，无法处理时返回空值。

        :param path: 待解析的文件路径文本
        :param options: 调用方提供的路径解析选项；未提供时使用加速器默认值
        :return: 从路径解析出的字段字典；无法处理时返回 None
        """

    def parse_metamusic(
        self,
        title: str,
        artists: Optional[list[str]] = None,
        year: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """
        解析音乐标题，无法处理时返回空值。

        :param title: 待解析的音乐标题
        :param artists: 已知艺术家列表，用于辅助标题识别；未提供时为 None
        :param year: 已知发行年份，用于辅助标题识别；未提供时为 None
        :return: 解析出的音乐字段字典；无法处理时返回 None
        """

    def find_metainfo(self, title: str) -> Optional[dict[str, Any]]:
        """
        提取标题中的显式媒体标签。

        :param title: 可能带有显式媒体来源标签的标题
        :return: 媒体来源、标识等显式字段；未提取到时返回 None
        """

    def supports_extended_media_ids(self) -> bool:
        """
        返回加速器是否支持扩展媒体来源标签。

        :return: 支持扩展媒体来源标签时返回 True，否则返回 False
        """


_media_extensions_provider: Callable[[], Sequence[str]] = lambda: ()
_audio_extensions_provider: Callable[[], Sequence[str]] = lambda: ()
_metainfo_accelerator: Optional[MetaInfoAccelerator] = None


def configure_recognition_runtime(
    *,
    media_extensions_provider: Callable[[], Sequence[str]],
    audio_extensions_provider: Callable[[], Sequence[str]],
    accelerator: Optional[MetaInfoAccelerator],
) -> None:
    """
    注入文件类型配置和可选加速器，保持领域解析器与平台实现解耦。
    """
    global _media_extensions_provider, _audio_extensions_provider
    global _metainfo_accelerator
    _media_extensions_provider = media_extensions_provider
    _audio_extensions_provider = audio_extensions_provider
    _metainfo_accelerator = accelerator


def get_media_extensions() -> tuple[str, ...]:
    """
    返回当前影视、字幕和音频文件后缀。
    """
    return tuple(_media_extensions_provider() or ())


def get_audio_extensions() -> tuple[str, ...]:
    """
    返回当前音频文件后缀。
    """
    return tuple(_audio_extensions_provider() or ())


def get_metainfo_accelerator() -> Optional[MetaInfoAccelerator]:
    """
    返回启动层注入的可选识别加速器。
    """
    return _metainfo_accelerator
