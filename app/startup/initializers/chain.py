"""装配 Chain 所需的外部服务与系统技术端口。"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional, cast

from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.system.host import SystemUtils
from app.chain._recognition import (
    RecognitionSharePort,
    configure_recognition_share_port,
    reset_recognition_share_port,
)
from app.chain.subscribe.notify import (
    SubscriptionSharePort,
    configure_subscription_share_port,
    reset_subscription_share_port,
)
from app.chain.transfer.filter import (
    NetworkFilesystemPort,
    configure_network_filesystem_port,
    reset_network_filesystem_port,
)
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.common import JsonData
from app.schemas.types import MediaType


class _RecognitionShareAdapter:
    """把 MoviePilot Server 共享识别能力适配为 Chain 窄端口。"""

    def report_recognize_share(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo | MusicInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> bool:
        """同步上报共享识别结果。"""
        return bool(MoviePilotServerHelper.report_recognize_share(
            meta=meta, mediainfo=mediainfo, keyword_meta=keyword_meta
        ))

    async def async_report_recognize_share(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo | MusicInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> bool:
        """异步上报共享识别结果。"""
        return bool(await MoviePilotServerHelper.async_report_recognize_share(
            meta=meta, mediainfo=mediainfo, keyword_meta=keyword_meta
        ))

    def query_recognize_share(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
            music_type: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """同步查询共享识别结果。"""
        result = MoviePilotServerHelper.query_recognize_share(
            meta=meta,
            mtype=mtype,
            keyword_meta=keyword_meta,
            **({"music_type": music_type} if music_type is not None else {}),
        )
        return cast(Optional[dict[str, Any]], result)

    async def async_query_recognize_share(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
            music_type: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """异步查询共享识别结果。"""
        result = await MoviePilotServerHelper.async_query_recognize_share(
            meta=meta,
            mtype=mtype,
            keyword_meta=keyword_meta,
            **({"music_type": music_type} if music_type is not None else {}),
        )
        return cast(Optional[dict[str, Any]], result)

    def to_recognize_params(
            self,
            item: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """把服务端结果转换为本地识别参数。"""
        return cast(
            Optional[dict[str, Any]],
            MoviePilotServerHelper.to_recognize_params(item),
        )


class _SubscriptionShareAdapter:
    """把 MoviePilot Server 订阅共享能力适配为 Chain 窄端口。"""

    def report_added(self, payload: dict[str, Any]) -> bool:
        """同步上报新增订阅统计。"""
        return bool(MoviePilotServerHelper.sub_reg_durable(payload))

    async def async_report_added(self, payload: dict[str, Any]) -> bool:
        """异步上报新增订阅统计。"""
        return bool(await MoviePilotServerHelper.async_sub_reg_durable(payload))

    def list_shares(self) -> list[dict[str, Any]]:
        """读取当前用户可见的订阅分享。"""
        return cast(list[dict[str, Any]], MoviePilotServerHelper.get_subscribe_shares())

    def report_completed(self, payload: Mapping[str, JsonData]) -> bool:
        """同步上报订阅完成统计。"""
        return bool(MoviePilotServerHelper.sub_done_durable(dict(payload)))


class _NetworkFilesystemAdapter:
    """把宿主文件系统探测能力适配为整理 Chain 窄端口。"""

    def is_network_filesystem(
            self,
            path: Path,
            *,
            include_local_fuse: bool = False,
    ) -> bool:
        """判断路径是否位于网络或指定的本地 FUSE 文件系统。"""
        return bool(SystemUtils.is_network_filesystem(
            path, include_local_fuse=include_local_fuse
        ))


def init_chain_ports() -> None:
    """原子装配 Chain 的共享服务与文件系统端口。"""
    reset_chain_ports()
    try:
        configure_recognition_share_port(
            cast(RecognitionSharePort, _RecognitionShareAdapter())
        )
        configure_subscription_share_port(
            cast(SubscriptionSharePort, _SubscriptionShareAdapter())
        )
        configure_network_filesystem_port(
            cast(NetworkFilesystemPort, _NetworkFilesystemAdapter())
        )
    except Exception:
        reset_chain_ports()
        raise


def reset_chain_ports() -> None:
    """释放 Chain 技术端口，支持重复 lifespan 与启动失败回滚。"""
    reset_network_filesystem_port()
    reset_subscription_share_port()
    reset_recognition_share_port()
