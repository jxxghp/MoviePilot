from fastapi import FastAPI

from app.api.endpoints import subscribe


def test_subscription_commands_publish_post_and_hide_legacy_get() -> None:
    """订阅副作用命令只在 OpenAPI 中发布 POST，旧 GET 仅保留运行时兼容。"""
    app = FastAPI()
    app.include_router(subscribe.router, prefix="/api/v1/subscribe")

    paths = app.openapi()["paths"]

    assert set(paths["/api/v1/subscribe/refresh"]) == {"post"}
    assert set(paths["/api/v1/subscribe/reset/{subid}"]) == {"post"}
    assert set(paths["/api/v1/subscribe/check"]) == {"post"}
    assert set(paths["/api/v1/subscribe/search"]) == {"post"}
    assert set(paths["/api/v1/subscribe/search/{subscribe_id}"]) == {"post"}
