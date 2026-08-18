from types import SimpleNamespace
from unittest.mock import patch

from app.modules.u115.u115 import U115Pan
from app.schemas import FileItem


def _target_dir() -> FileItem:
    """
    构造 115 上传目标目录。
    """
    return FileItem(
        storage="u115",
        path="/library/Test Show (2026)/Season 1",
        type="dir",
        name="Season 1",
        fileid="100",
    )


def _fake_sha1(*_args, **_kwargs) -> str:
    """
    返回固定 SHA1，避免单测重复计算文件哈希。
    """
    return "sha1"


def _fake_request_api(_method, endpoint, *_args, **_kwargs):
    """
    模拟 115 初始化、凭证和断点续传接口。
    """
    if endpoint == "/open/upload/init":
        return {
            "state": True,
            "data": {
                "bucket": "bucket",
                "object": "object",
                "callback": {"callback": "callback", "callback_var": "var"},
                "pick_code": "pickcode",
                "status": 1,
            },
        }
    if endpoint == "/open/upload/get_token":
        return {
            "endpoint": "endpoint",
            "AccessKeyId": "access_key_id",
            "AccessKeySecret": "access_key_secret",
            "SecurityToken": "security_token",
        }
    if endpoint == "/open/upload/resume":
        return None
    return None


class _FakeBucket:
    """
    模拟 OSS 分片上传客户端。
    """

    complete_payload = {"state": True}

    def __init__(self, *_args, **_kwargs):
        """
        初始化伪造 Bucket。
        """
        pass

    def init_multipart_upload(self, *_args, **_kwargs):
        """
        返回固定 upload_id。
        """
        return SimpleNamespace(upload_id="upload_id")

    def upload_part(self, *_args, **_kwargs):
        """
        返回固定分片 etag。
        """
        return SimpleNamespace(etag="etag")

    def complete_multipart_upload(self, *_args, **_kwargs):
        """
        返回可配置的 115 回调结果。
        """
        response = SimpleNamespace(json=lambda: self.complete_payload)
        return SimpleNamespace(status=200, resp=SimpleNamespace(response=response))


def _build_storage() -> U115Pan:
    """
    构造跳过初始化流程的 115 存储实例。
    """
    storage = object.__new__(U115Pan)
    storage._calc_sha1 = _fake_sha1
    storage.get_item = lambda _path: None
    storage._request_api = _fake_request_api
    return storage


def _upload_with_fakes(storage: U115Pan, target_dir: FileItem, local_file):
    """
    使用伪造 OSS 和进度回调执行上传。
    """

    with patch(
        "app.modules.u115.u115.oss2.StsAuth",
        return_value=object(),
    ), patch(
        "app.modules.u115.u115.oss2.Bucket",
        _FakeBucket,
    ), patch(
        "app.modules.u115.u115.transfer_process",
        return_value=lambda _progress: None,
    ):
        return storage.upload(target_dir, local_file)


def test_upload_returns_target_fileitem_when_uploaded_metadata_is_delayed(tmp_path):
    """
    115 上传完成后目录索引暂不可见时，应返回可落库的目标文件项。
    """
    local_file = tmp_path / "Test.Show.S01E01.mkv"
    local_file.write_bytes(b"movie")

    storage = _build_storage()
    uploaded_item = _upload_with_fakes(storage, _target_dir(), local_file)

    assert uploaded_item is not None
    assert uploaded_item.storage == "u115"
    assert uploaded_item.path == "/library/Test Show (2026)/Season 1/Test.Show.S01E01.mkv"
    assert uploaded_item.type == "file"
    assert uploaded_item.size == local_file.stat().st_size


def test_upload_returns_none_when_complete_callback_reports_failure(tmp_path):
    """
    115 完成回调失败时，不应把文件视为上传成功。
    """
    local_file = tmp_path / "Test.Show.S01E02.mkv"
    local_file.write_bytes(b"movie")

    storage = _build_storage()
    with patch.object(
        _FakeBucket,
        "complete_payload",
        {"state": False, "message": "callback failed"},
    ):
        uploaded_item = _upload_with_fakes(storage, _target_dir(), local_file)

    assert uploaded_item is None


def test_upload_uses_dynamic_part_size(tmp_path):
    """
    115 上传应根据文件大小动态控制 OSS 分片大小。
    """
    local_file = tmp_path / "Test.Show.S01E03.mkv"
    local_file.write_bytes(b"movie")

    storage = _build_storage()
    with patch(
        "app.modules.u115.u115.determine_part_size",
        return_value=local_file.stat().st_size,
    ) as determine_part_size_mock:
        uploaded_item = _upload_with_fakes(storage, _target_dir(), local_file)

    assert uploaded_item is not None
    determine_part_size_mock.assert_called_once_with(
        local_file.stat().st_size, preferred_size=10 * 1024 * 1024
    )


def test_upload_part_size_grows_for_large_files():
    """
    大文件上传应自动放大分片大小，避免产生过多分片。
    """
    file_size = int(5.6 * 1024 * 1024 * 1024)

    part_size = U115Pan._U115Pan__get_upload_part_size(file_size)

    assert part_size == 64 * 1024 * 1024
