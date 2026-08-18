"""阶段 4 六个重点 Chain 的纵向切片数量守卫。"""

import inspect

import pytest

from app.application.orchestration.download import DownloadChain
from app.application.orchestration.media import MediaChain
from app.application.orchestration.message import MessageChain
from app.application.orchestration.search import SearchChain
from app.application.orchestration.subscribe import SubscribeChain
from app.application.orchestration.transfer import TransferChain


@pytest.mark.parametrize(
    ("chain_type", "method_tokens"),
    [
        (
            SubscribeChain,
            {
                "exists": "_subscription_query",
                "get_subscribe_by_source": "_subscription_query",
                "has_music_subscribe": "_subscription_query",
            },
        ),
        (
            SearchChain,
            {
                "save_last_search_params": "_search_state",
                "last_search_params": "_search_state",
                "last_search_results": "_search_state",
            },
        ),
        (
            TransferChain,
            {
                "put_to_queue": "_transfer_queue_service",
                "remove_from_queue": "_transfer_queue_service",
                "get_queue_tasks": "_transfer_queue_service",
            },
        ),
        (
            DownloadChain,
            {
                "downloading": "_download_task_service",
                "set_downloading": "_download_task_service",
                "remove_downloading": "_download_task_service",
            },
        ),
        (
            MediaChain,
            {
                "normalize_music_candidates": "MusicCatalogService",
                "search_music": "_music_catalog",
                "async_search_music": "_music_catalog",
            },
        ),
        (
            MessageChain,
            {
                "remote_clear_session": "_message_session_service",
                "remote_stop_agent": "_message_session_service",
                "remote_session_status": "_message_session_service",
            },
        ),
    ],
)
def test_key_chain_keeps_three_application_service_slices(
    chain_type: type,
    method_tokens: dict[str, str],
) -> None:
    """每个重点 Chain 至少三个公开方法必须继续委托窄应用服务。"""
    assert len(method_tokens) >= 3
    missing = []
    for method_name, service_token in method_tokens.items():
        method = getattr(chain_type, method_name)
        if service_token not in inspect.getsource(method):
            missing.append(f"{chain_type.__name__}.{method_name}->{service_token}")
    assert missing == []
