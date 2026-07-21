from types import SimpleNamespace

from app import schemas
from app.api.endpoints import download as download_endpoint
from app.core.context import MediaInfo
from app.schemas.types import MediaType


def test_download_add_passes_generic_media_source(monkeypatch) -> None:
    """不含媒体信息的下载应按统一来源ID执行精确识别。"""
    captured = {}
    media = MediaInfo(
        anilist_info={
            "id": 154587,
            "title": {"english": "Frieren"},
            "format": "TV",
        }
    )

    class FakeMediaChain:
        """记录下载接口传入的媒体识别参数。"""

        def recognize_media(self, **kwargs):
            """返回固定媒体信息并保存识别参数。"""
            captured["recognize"] = kwargs
            return media

    class FakeDownloadChain:
        """模拟下载任务提交。"""

        def download_single(self, **kwargs):
            """保存下载上下文并返回任务ID。"""
            captured["download"] = kwargs
            return "download-1"

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="Frieren S01E01"),
        media_source="anilist",
        media_id="154587",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["recognize"]["source"] == "anilist"
    assert captured["recognize"]["mediaid"] == "154587"
    assert captured["download"]["context"].media_info is media


def test_download_add_uses_selected_source_for_title_recognition(monkeypatch) -> None:
    """只选择来源而未填写ID时应在该来源内按标题识别。"""
    captured = {}
    media = MediaInfo(title="测试动画", type=MediaType.TV, bangumi_id=1)

    class FakeMediaChain:
        """记录按标题识别的请求级来源。"""

        def recognize_by_meta(self, metainfo, **kwargs):
            """返回固定媒体信息并保存来源。"""
            captured["metainfo"] = metainfo
            captured["kwargs"] = kwargs
            return media

    class FakeDownloadChain:
        """模拟下载任务提交。"""

        @staticmethod
        def download_single(**kwargs):
            """返回固定下载任务ID。"""
            return "download-2"

    monkeypatch.setattr(download_endpoint, "MediaChain", FakeMediaChain)
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.add(
        torrent_in=schemas.TorrentInfo(title="测试动画 S01E01"),
        media_source="bangumi",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["kwargs"]["source"] == "bangumi"


def test_subtitle_download_passes_generic_media_source(monkeypatch) -> None:
    """字幕下载接口应把统一来源ID传递到下载链。"""
    captured = {}

    class FakeDownloadChain:
        """记录字幕下载参数。"""

        def download_subtitle(self, **kwargs):
            """保存参数并返回固定成功结果。"""
            captured.update(kwargs)
            return True, "字幕下载成功", ["/tmp/subtitle.ass"]

    monkeypatch.setattr(
        download_endpoint,
        "_prepare_subtitle_download",
        lambda _subtitle: (True, ""),
    )
    monkeypatch.setattr(download_endpoint, "DownloadChain", FakeDownloadChain)

    response = download_endpoint.download_subtitle(
        subtitle_in=schemas.SubtitleInfo(
            title="Frieren S01E01",
            enclosure="https://example.com/subtitle.ass",
        ),
        media_source="anilist",
        media_id="154587",
        current_user=SimpleNamespace(name="tester"),
    )

    assert response.success is True
    assert captured["media_source"] == "anilist"
    assert captured["media_id"] == "154587"
