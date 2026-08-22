import json

import httpx
import pytest
from fastapi import FastAPI

from app.api import servcookie
from app.runtime.config import settings

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def cookiecloud_app(tmp_path, monkeypatch):
    """用启动组合根读取的同一 Settings 实例配置 CookieCloud 测试。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "COOKIECLOUD_ENABLE_LOCAL", True)
    monkeypatch.setattr(settings, "COOKIECLOUD_AUTH_HEADER", None)
    settings.COOKIE_PATH.mkdir(parents=True, exist_ok=True)

    app = FastAPI()
    app.include_router(servcookie.cookie_router, prefix="/cookiecloud")
    return app


def make_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


async def test_update_rejects_when_local_cookiecloud_disabled(cookiecloud_app):
    settings.COOKIECLOUD_ENABLE_LOCAL = False
    settings.COOKIECLOUD_AUTH_HEADER = "secret"

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
    settings.COOKIECLOUD_AUTH_HEADER = auth_header

    async with make_client(cookiecloud_app) as client:
        response = await client.post(
            "/cookiecloud/update",
            json={"uuid": "abcde", "encrypted": "payload"},
        )

    assert response.status_code == 200
    assert response.json() == {"action": "done"}
    assert json.loads((settings.COOKIE_PATH / "abcde.json").read_text()) == {
        "encrypted": "payload"
    }


async def test_update_allows_matching_auth_header(cookiecloud_app):
    settings.COOKIECLOUD_AUTH_HEADER = "  secret-token  "

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
    settings.COOKIECLOUD_AUTH_HEADER = "secret-token"

    async with make_client(cookiecloud_app) as client:
        response = await client.post(
            "/cookiecloud/update",
            json={"uuid": "abcde", "encrypted": "payload"},
            headers=headers,
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "CookieCloud认证失败"


async def test_get_routes_do_not_require_auth_header(cookiecloud_app, monkeypatch):
    settings.COOKIECLOUD_AUTH_HEADER = "secret-token"

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


def test_cookiecloud_openapi_declares_native_success_models(cookiecloud_app):
    """CookieCloud 原生兼容响应也必须在 OpenAPI 中给出明确结构。"""
    schema = cookiecloud_app.openapi()

    root_content = schema["paths"]["/cookiecloud/"]["get"]["responses"]["200"][
        "content"
    ]
    update_schema = schema["paths"]["/cookiecloud/update"]["post"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    download_schema = schema["paths"]["/cookiecloud/get/{uuid}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]

    assert root_content["text/plain"]["schema"] == {"type": "string"}
    assert update_schema["$ref"].endswith("/CookieActionResponse")
    assert download_schema["$ref"].endswith("/CookieEncryptedPayload")
