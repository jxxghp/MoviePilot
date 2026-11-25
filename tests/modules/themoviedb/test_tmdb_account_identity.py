"""
账号身份缓存及无效 JSON 响应的兼容性验证。
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.modules.themoviedb.tmdbv3api.objs.account import Account


def test_account_id_uses_configured_identity_without_request(monkeypatch):
    """
    已配置账号 ID 时不应再次查询账号详情。
    """
    monkeypatch.setenv("TMDB_ACCOUNT_ID", "42")
    details = Mock()
    monkeypatch.setattr(Account, "details", details)
    client = object.__new__(Account)
    assert client.account_id == "42"
    details.assert_not_called()


def test_account_id_caches_numeric_identity_as_text(monkeypatch):
    """
    初次读取将数字 ID 保存为文本，后续读取复用现有缓存。
    """
    monkeypatch.setenv("TMDB_ACCOUNT_ID", "")
    details = Mock(return_value={"id": 42})
    monkeypatch.setattr(Account, "details", details)
    client = object.__new__(Account)
    assert client.account_id == "42"
    assert client.account_id == "42"
    details.assert_called_once_with()


@pytest.mark.parametrize("payload,error", [(None, TypeError), ([], TypeError), ("bad", TypeError), (1, TypeError), ({}, KeyError)])
def test_invalid_account_identity_preserves_failure_without_caching(monkeypatch, payload, error):
    """
    非对象响应仍抛 TypeError，缺失 ID 仍抛 KeyError，失败后不产生缓存。
    """
    monkeypatch.setenv("TMDB_ACCOUNT_ID", "")
    details = Mock(return_value=payload)
    monkeypatch.setattr(Account, "details", details)
    client = object.__new__(Account)
    with pytest.raises(error):
        _ = client.account_id
    details.return_value = {"id": 43}
    assert client.account_id == "43"
    assert details.call_count == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("ascending", [False, True])
def test_account_lists_keep_session_sort_and_page(monkeypatch, asynchronous, ascending):
    """
    账号列表的双入口保留会话、页码及显式降序规则。
    """
    monkeypatch.setenv("TMDB_ACCOUNT_ID", "42")
    payload = [{"id": 123}]
    request = (AsyncMock if asynchronous else Mock)(return_value=payload)
    monkeypatch.setattr(Account, "_async_request_obj" if asynchronous else "_request_obj", request)
    client = object.__new__(Account)
    client.session_id = "session"
    if asynchronous:
        result = asyncio.run(client.async_favorite_movies(asc_sort=ascending, page=2))
    else:
        result = client.favorite_movies(asc_sort=ascending, page=2)
    assert result is payload
    params = "session_id=session&page=2"
    if not ascending:
        params += "&sort_by=created_at.desc"
    request.assert_called_once_with("/account/42/favorite/movies", params=params, key="results")
