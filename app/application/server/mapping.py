"""
媒体转换和详情浏览共享的 TMDB 关系查询端口。
"""

from typing import Protocol

from app.schemas.mapping import MediaMapping, MediaMappingQuery


class MediaMappingPort(Protocol):
    """
    中心服务的可降级查询与共享能力，不向调用方暴露传输实现。
    """

    def query(self, query: MediaMappingQuery) -> MediaMapping | None:
        """
        同步查询本地缓存或中心服务。

        :param query: 原来源媒体身份与类型、季号上下文
        :return: 已知对应关系；未命中或服务不可用时为 None
        """
        ...

    async def async_query(self, query: MediaMappingQuery) -> MediaMapping | None:
        """
        异步查询缓存或中心服务。

        :param query: 原来源媒体身份与类型、季号上下文
        :return: 已知对应关系；未命中或服务不可用时为 None
        """
        ...

    def share(self, mapping: MediaMapping) -> None:
        """
        缓存转换结果并尝试后台上报，不改变转换结果。

        :param mapping: 本次成功转换的单向关系
        """
        ...


_port: MediaMappingPort | None = None


def configure_media_mapping_port(port: MediaMappingPort | None) -> None:
    """
    由宿主组合根装配或释放关系端口。

    :param port: 有界缓存和超时的实现；None 表示未装配
    """
    global _port
    _port = port


def get_media_mapping_port() -> MediaMappingPort | None:
    """
    返回可选关系端口，未装配的测试或插件上下文保持原转换行为。

    :return: 已装配的中心关系端口，或 None
    """
    return _port
