from unittest.mock import MagicMock, patch

from app.modules.filemanager.storages import alist as alist_module
from app.modules.filemanager.storages.alist import Alist
from app.schemas import FileItem


def test_delete_directory_uses_remove_api_without_empty_directory_probe():
    """
    删除 OpenList 目录时应直接使用通用删除接口，避免专用空目录接口返回成功但未实际删除。
    """
    storage = Alist()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"code": 200, "message": "success", "data": None}
    request_utils = MagicMock()
    request_utils.post_res.return_value = response
    fileitem = FileItem(storage="alist", type="dir", path="/library/empty/")

    with patch.object(
        Alist,
        "get_conf",
        return_value={"url": "http://openlist.test", "token": "token"},
    ):
        with patch.object(storage, "_Alist__get_header_with_token", return_value={}):
            with patch.object(alist_module, "RequestUtils", return_value=request_utils):
                with patch.object(
                    storage,
                    "list",
                    side_effect=AssertionError("不应探测空目录"),
                ):
                    assert storage.delete(fileitem) is True

    request_utils.post_res.assert_called_once()
    called_url = request_utils.post_res.call_args.args[0]
    assert called_url == "http://openlist.test/api/fs/remove"
    assert "remove_empty_directory" not in called_url
    assert request_utils.post_res.call_args.kwargs["json"] == {
        "dir": "/library",
        "names": ["empty"],
    }
