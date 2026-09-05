from fastapi import FastAPI

from app.api.endpoints import site


def test_site_commands_publish_post_and_hide_legacy_get() -> None:
    """站点副作用命令只在 OpenAPI 中发布 POST，旧 GET 仅保留运行时兼容。"""
    app = FastAPI()
    app.include_router(site.router, prefix="/api/v1/site")

    paths = app.openapi()["paths"]

    assert set(paths["/api/v1/site/cookiecloud"]) == {"post"}
    assert set(paths["/api/v1/site/reset"]) == {"post"}
