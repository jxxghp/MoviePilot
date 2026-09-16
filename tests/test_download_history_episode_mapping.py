"""订阅集数偏移后的文件明细与目录监控关联回归。"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.chain.download.facade import DownloadChain
from app.chain.transfer.facade import TransferChain
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaType


@pytest.mark.parametrize("offset", [0, 165])
@pytest.mark.parametrize("durable", [False, True])
def test_selected_episode_file_resolves_its_own_hash(db, offset, durable):
    """映射后的文件须落库并精确关联 hash，即使同目录还有更新的下载历史。"""
    db.watermark(DownloadHistory, DownloadFiles)
    custom_words = (
        "A.Will.Eternal.S04 => 一念永恒{[tmdbid=107371;type=tv]}S01 "
        f"&& S01 <> 2160p >> EP+{offset}"
    ) if offset else None
    selected_file = "A.Will.Eternal.S04E10.2026.2160p.mkv"
    unselected_file = "A.Will.Eternal.S04E11.2026.2160p.mkv"
    meta = MetaInfo(selected_file, custom_words=[custom_words] if custom_words else None)
    assert meta.begin_episode == 10 + offset
    media = MediaInfo(type=MediaType.TV, title="一念永恒", tmdb_id=107371)
    torrent = TorrentInfo(title="A.Will.Eternal.S04.2026.2160p")
    context = Context(meta_info=meta, media_info=media, torrent_info=torrent)
    chain = DownloadChain.__new__(DownloadChain)
    chain.download_history_repository = MagicMock()
    chain.durable_event_writer = MagicMock() if durable else None
    chain._build_download_notification = MagicMock(return_value=None)
    chain._submit_download_added_task = MagicMock()
    chain.eventmanager = MagicMock()

    chain._settle_download_success(
        context=context, media=media, meta=meta, torrent=torrent,
        folder_name="shared-show", file_list=[selected_file, unselected_file, "readme.txt"],
        download_dir=Path("/downloads"), layout="Original", downloader="qb",
        download_hash="episode-10-hash", download_episodes=f"E{10 + offset}",
        episodes={10 + offset}, channel=None, source="subscribe", userid=None,
        username=None, torrent_content=b"torrent", custom_words=custom_words,
    )

    if durable:
        payload = chain.durable_event_writer.download_added.call_args.kwargs
        history, files = payload["history"], payload["files"]
    else:
        history, files = chain.download_history_repository.add.call_args.args
    assert [file.filepath for file in files] == [selected_file]
    assert files[0].fullpath == f"/downloads/shared-show/{selected_file}"
    assert files[0].download_hash == "episode-10-hash"
    assert history.custom_words == custom_words

    db.add(DownloadHistory(**history.to_payload()))
    db.add(*(DownloadFiles(**file.to_payload()) for file in files))
    later_history = history.to_payload()
    later_history["download_hash"] = "episode-11-hash"
    db.add(DownloadHistory(**later_history))
    repository = DownloadHistoryOper(db.session)
    assert repository.get_by_path(history.path).download_hash == "episode-11-hash"
    transfer = TransferChain.__new__(TransferChain)
    resolved = transfer._resolve_download_history(
        repository=repository, file_path=Path(files[0].fullpath),
    )
    assert resolved.download_hash == "episode-10-hash"
    assert resolved.downloader == "qb"
