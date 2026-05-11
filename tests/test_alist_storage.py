import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_alist_module():
    module_name = "_test_alist_module"
    app_module = types.ModuleType("app")
    schemas_module = types.ModuleType("app.schemas")
    cache_module = types.ModuleType("app.core.cache")
    config_module = types.ModuleType("app.core.config")
    log_module = types.ModuleType("app.log")
    storages_module = types.ModuleType("app.modules.filemanager.storages")
    exception_module = types.ModuleType("app.schemas.exception")
    types_module = types.ModuleType("app.schemas.types")
    http_module = types.ModuleType("app.utils.http")
    singleton_module = types.ModuleType("app.utils.singleton")
    url_module = types.ModuleType("app.utils.url")

    class _FileItem:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _StorageSchemaValue:
        def __init__(self, value):
            self.value = value

    class _Logger:
        def debug(self, *_args, **_kwargs):
            pass

        def warn(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

        def critical(self, *_args, **_kwargs):
            pass

        def info(self, *_args, **_kwargs):
            pass

    class _StorageBase:
        def __init__(self):
            pass

        def get_conf(self):
            return {}

    class _OperationInterrupted(Exception):
        pass

    class _RequestUtils:
        def __init__(self, *args, **kwargs):
            pass

    class _UrlUtils:
        @staticmethod
        def standardize_base_url(url):
            return url.rstrip("/") if url else ""

        @staticmethod
        def adapt_request_url(base, path):
            return f"{base() if callable(base) else base}{path}"

        @staticmethod
        def quote(path):
            return path

    def _cached(*_args, **_kwargs):
        def decorator(func):
            func.cache_clear = lambda: None
            return func

        return decorator

    schemas_module.FileItem = _FileItem
    schemas_module.StorageUsage = object
    cache_module.cached = _cached
    config_module.settings = types.SimpleNamespace(
        OPENLIST_SNAPSHOT_CHECK_FOLDER_MODTIME=True,
        TEMP_PATH=Path("/tmp"),
    )
    config_module.global_vars = types.SimpleNamespace(
        is_transfer_stopped=lambda *_args, **_kwargs: False
    )
    log_module.logger = _Logger()
    storages_module.StorageBase = _StorageBase
    storages_module.transfer_process = lambda *_args, **_kwargs: (lambda *_a, **_k: None)
    exception_module.OperationInterrupted = _OperationInterrupted
    types_module.StorageSchema = types.SimpleNamespace(Alist=_StorageSchemaValue("alist"))
    http_module.RequestUtils = _RequestUtils
    singleton_module.WeakSingleton = type
    url_module.UrlUtils = _UrlUtils

    app_module.schemas = schemas_module

    stub_modules = {
        "app": app_module,
        "app.schemas": schemas_module,
        "app.core.cache": cache_module,
        "app.core.config": config_module,
        "app.log": log_module,
        "app.modules.filemanager.storages": storages_module,
        "app.schemas.exception": exception_module,
        "app.schemas.types": types_module,
        "app.utils.http": http_module,
        "app.utils.singleton": singleton_module,
        "app.utils.url": url_module,
    }
    for stub_module in stub_modules.values():
        stub_module._alist_test_stub = True

    alist_path = Path(__file__).resolve().parents[1] / "app" / "modules" / "filemanager" / "storages" / "alist.py"
    spec = importlib.util.spec_from_file_location(module_name, alist_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    with patch.dict(sys.modules, stub_modules):
        spec.loader.exec_module(module)
    return module


alist_module = _load_alist_module()
Alist = alist_module.Alist
FileItem = alist_module.schemas.FileItem


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class AlistStorageTest(unittest.TestCase):
    def setUp(self):
        self.storage = Alist()

    @staticmethod
    def _dir_item(path: str = "/"):
        return FileItem(storage="alist", type="dir", path=path)

    @staticmethod
    def _page_payload(start: int, count: int, total: int) -> dict:
        return {
            "code": 200,
            "message": "success",
            "data": {
                "content": [
                    {
                        "name": f"dir-{index}",
                        "size": 0,
                        "is_dir": True,
                        "modified": "2024-05-17T13:47:55.4174917+08:00",
                        "thumb": "",
                    }
                    for index in range(start, start + count)
                ],
                "total": total,
            },
        }

    def test_list_fetches_all_pages_when_per_page_is_default(self):
        responses = [
            _FakeResponse(self._page_payload(0, 200, 205)),
            _FakeResponse(self._page_payload(200, 5, 205)),
        ]
        request_utils = MagicMock()
        request_utils.post_res.side_effect = responses

        with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
            with patch.object(alist_module, "RequestUtils", return_value=request_utils):
                items = self.storage.list(self._dir_item())

        self.assertEqual(205, len(items))
        self.assertEqual("/dir-0/", items[0].path)
        self.assertEqual("/dir-204/", items[-1].path)
        self.assertEqual(2, request_utils.post_res.call_count)
        self.assertEqual(1, request_utils.post_res.call_args_list[0].kwargs["json"]["page"])
        self.assertEqual(2, request_utils.post_res.call_args_list[1].kwargs["json"]["page"])

    def test_list_respects_explicit_per_page_without_auto_paging(self):
        request_utils = MagicMock()
        request_utils.post_res.return_value = _FakeResponse(self._page_payload(0, 50, 205))

        with patch.object(Alist, "get_conf", return_value={"url": "http://openlist.test", "token": "token"}):
            with patch.object(alist_module, "RequestUtils", return_value=request_utils):
                items = self.storage.list(self._dir_item(), per_page=50)

        self.assertEqual(50, len(items))
        self.assertEqual(1, request_utils.post_res.call_count)


if __name__ == "__main__":
    unittest.main()
