from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.api.endpoints.mediaserver import latest, library, playing
from app.chain.mediaserver import MediaServerChain


@pytest.mark.parametrize(
    ("endpoint", "chain_method", "kwargs"),
    [
        (latest, "latest", {"server": "home", "count": 20}),
        (playing, "playing", {"server": "home", "count": 12}),
        (library, "librarys", {"server": "home", "hidden": True}),
    ],
)
def test_dashboard_media_endpoints_preserve_successful_empty_results(
    endpoint,
    chain_method,
    kwargs,
):
    """媒体服务器成功返回空列表时，Dashboard 接口应保留真实空结果。"""
    with patch("app.api.endpoints.mediaserver.MediaServerChain") as chain_cls:
        getattr(chain_cls.return_value, chain_method).return_value = []

        result = endpoint(
            **kwargs,
            userinfo=SimpleNamespace(username="alice"),
        )

    assert result == []


@pytest.mark.parametrize(
    ("endpoint", "chain_method", "kwargs"),
    [
        (latest, "latest", {"server": "home", "count": 20}),
        (playing, "playing", {"server": "home", "count": 12}),
        (library, "librarys", {"server": "home", "hidden": True}),
    ],
)
def test_dashboard_media_endpoints_report_upstream_failures(
    endpoint,
    chain_method,
    kwargs,
):
    """媒体服务器请求失败时，Dashboard 接口不得把 None 折叠为空列表。"""
    with patch("app.api.endpoints.mediaserver.MediaServerChain") as chain_cls:
        getattr(chain_cls.return_value, chain_method).return_value = None

        with pytest.raises(HTTPException) as exc_info:
            endpoint(
                **kwargs,
                userinfo=SimpleNamespace(username="alice"),
            )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "媒体服务器请求失败"


@pytest.mark.parametrize(
    ("method_name", "run_method"),
    [
        ("latest", "mediaserver_latest"),
        ("playing", "mediaserver_playing"),
        ("librarys", "mediaserver_librarys"),
    ],
)
def test_media_server_chain_preserves_none_from_provider(method_name, run_method):
    """媒体服务器处理链应保留提供方失败状态，交由接口层转换为明确错误。"""
    chain = MediaServerChain.__new__(MediaServerChain)
    chain.run_module = lambda method, **kwargs: None

    result = getattr(chain, method_name)(server="home")

    assert result is None
