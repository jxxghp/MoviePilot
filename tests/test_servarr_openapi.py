from fastapi import FastAPI

from app.api.servarr import arr_router


def test_servarr_openapi_declares_native_success_models() -> None:
    """Servarr 原生兼容端点必须在 OpenAPI 中暴露具体成功响应模型。"""
    app = FastAPI()
    app.include_router(arr_router, prefix="/api/v3")
    schema = app.openapi()

    expected_models = {
        ("/api/v3/system/status", "get"): "ServarrSystemStatus",
        ("/api/v3/qualityProfile", "get"): "ServarrQualityProfile",
        ("/api/v3/rootfolder", "get"): "ServarrRootFolder",
        ("/api/v3/tag", "get"): "ServarrTag",
        ("/api/v3/languageprofile", "get"): "ServarrLanguageProfile",
        ("/api/v3/movie", "post"): "ServarrIdResponse",
        ("/api/v3/series/lookup", "get"): "SonarrSeries",
        ("/api/v3/series", "put"): "ServarrIdResponse",
    }
    for (path, method), model_name in expected_models.items():
        response_schema = schema["paths"][path][method]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
        serialized_schema = str(response_schema)
        assert model_name in serialized_schema, (path, method, response_schema)
