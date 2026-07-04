import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import servcookie

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def cookiecloud_app(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        COOKIE_PATH=tmp_path,
        COOKIECLOUD_ENABLE_LOCAL=True,
        COOKIECLOUD_AUTH_HEADER=None,
    )
    monkeypatch.setattr(servcookie, "settings", settings)

    app = FastAPI()
    app.include_router(servcookie.cookie_router, prefix="/cookiecloud")
    return app


def make_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


async def test_update_rejects_when_local_cookiecloud_disabled(cookiecloud_app):
    servcookie.settings.COOKIECLOUD_ENABLE_LOCAL = False
    servcookie.settings.COOKIECLOUD_AUTH_HEADER = "secret"

    async with make_client(cookiecloud_app) as client:
        response = await client.post(
            "/cookiecloud/update",
            json={"uuid": "abcde", "encrypted": "payload"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "本地CookieCloud服务器未启用"


@pytest.mark.parametrize("auth_header", [None, "", "   "])
async def test_update_allows_legacy_clients_when_auth_header_unconfigured(
    cookiecloud_app, auth_header
):
    servcookie.settings.COOKIECLOUD_AUTH_HEADER = auth_header

    async with make_client(cookiecloud_app) as client:
        response = await client.post(
            "/cookiecloud/update",
            json={"uuid": "abcde", "encrypted": "payload"},
        )

    assert response.status_code == 200
    assert response.json() == {"action": "done"}
    assert json.loads((servcookie.settings.COOKIE_PATH / "abcde.json").read_text()) == {
        "encrypted": "payload"
    }


async def test_update_allows_matching_auth_header(cookiecloud_app):
    servcookie.settings.COOKIECLOUD_AUTH_HEADER = "  secret-token  "

    async with make_client(cookiecloud_app) as client:
        response = await client.post(
            "/cookiecloud/update",
            json={"uuid": "abcde", "encrypted": "payload"},
            headers={"X-CookieCloud-Auth": "secret-token"},
        )

    assert response.status_code == 200
    assert response.json() == {"action": "done"}


@pytest.mark.parametrize("headers", [{}, {"X-CookieCloud-Auth": "wrong"}])
async def test_update_rejects_missing_or_wrong_auth_header(cookiecloud_app, headers):
    servcookie.settings.COOKIECLOUD_AUTH_HEADER = "secret-token"

    async with make_client(cookiecloud_app) as client:
        response = await client.post(
            "/cookiecloud/update",
            json={"uuid": "abcde", "encrypted": "payload"},
            headers=headers,
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "CookieCloud认证失败"


async def test_get_routes_do_not_require_auth_header(cookiecloud_app, monkeypatch):
    servcookie.settings.COOKIECLOUD_AUTH_HEADER = "secret-token"

    async def load_encrypt_data(uuid):
        assert uuid == "abcde"
        return {"encrypted": "payload"}

    monkeypatch.setattr(servcookie, "load_encrypt_data", load_encrypt_data)

    async with make_client(cookiecloud_app) as client:
        get_response = await client.get("/cookiecloud/get/abcde")
        post_response = await client.post("/cookiecloud/get/abcde")

    assert get_response.status_code == 200
    assert get_response.json() == {"encrypted": "payload"}
    assert post_response.status_code == 200
    assert post_response.json() == {"encrypted": "payload"}
