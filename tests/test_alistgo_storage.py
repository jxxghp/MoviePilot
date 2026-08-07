from unittest.mock import MagicMock, patch

import pytest

from app.modules.filemanager.storages import alist as alist_module
from app.modules.filemanager.storages.alist import Alist
from app.modules.filemanager.storages.alistgo import AlistGo
from app.schemas.types import StorageSchema


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture
def clear_token_cache():
    Alist._Alist__login_token.cache_clear()  # noqa
    yield
    Alist._Alist__login_token.cache_clear()  # noqa


def test_alistgo_schema_registered():
    assert AlistGo.schema == StorageSchema.AlistGo
    assert StorageSchema.AlistGo.value == "alistgo"


def test_alistgo_singleton_isolated_from_alist():
    alist = Alist()
    alistgo = AlistGo()
    assert alistgo is not alist
    assert isinstance(alistgo, Alist)


def test_alistgo_token_isolated_from_alist(clear_token_cache):
    def _conf(storage):
        return {
            "url": f"http://{storage.schema.value}.test",
            "username": "user",
            "password": "pass",
        }

    responses = [
        _FakeResponse({"code": 200, "message": "success", "data": {"token": "token-alist"}}),
        _FakeResponse({"code": 200, "message": "success", "data": {"token": "token-alistgo"}}),
    ]
    request_utils = MagicMock()
    request_utils.post_res.side_effect = responses

    alist = Alist()
    alistgo = AlistGo()
    with patch.object(Alist, "get_conf", _conf):
        with patch.object(alist_module, "RequestUtils", return_value=request_utils):
            assert alist._Alist__generate_token() == "token-alist"  # noqa
            assert alistgo._Alist__generate_token() == "token-alistgo"  # noqa
            assert alist._Alist__generate_token() == "token-alist"  # noqa
            assert alistgo._Alist__generate_token() == "token-alistgo"  # noqa

    assert request_utils.post_res.call_count == 2


def test_init_storage_keeps_other_storage_token(clear_token_cache):
    def _conf(storage):
        return {
            "url": f"http://{storage.schema.value}.test",
            "username": "user",
            "password": "pass",
        }

    responses = [
        _FakeResponse({"code": 200, "message": "success", "data": {"token": "token-alist"}}),
        _FakeResponse({"code": 200, "message": "success", "data": {"token": "token-alistgo"}}),
        _FakeResponse({"code": 200, "message": "success", "data": {"token": "token-alistgo-new"}}),
    ]
    request_utils = MagicMock()
    request_utils.post_res.side_effect = responses

    alist = Alist()
    alistgo = AlistGo()
    with patch.object(Alist, "get_conf", _conf):
        with patch.object(alist_module, "RequestUtils", return_value=request_utils):
            assert alist._Alist__generate_token() == "token-alist"  # noqa
            assert alistgo._Alist__generate_token() == "token-alistgo"  # noqa

            alistgo.init_storage()

            assert alist._Alist__generate_token() == "token-alist"  # noqa
            assert alistgo._Alist__generate_token() == "token-alistgo-new"  # noqa

    assert request_utils.post_res.call_count == 3
