import inspect

from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.chain.transfer import TransferChain


def _assert_parameter_prefix(method, expected: list[str]) -> None:
    """断言新增媒体源参数未改变已有位置参数顺序。"""
    parameters = list(inspect.signature(method).parameters)
    assert parameters[:len(expected)] == expected


def test_search_chain_media_source_parameters_preserve_old_order() -> None:
    """搜索链新增媒体源参数必须追加在原有位置参数之后。"""
    resource_parameters = [
        "self", "tmdbid", "doubanid", "mtype", "area", "season", "sites", "cache_local"
    ]
    subtitle_parameters = [
        "self", "tmdbid", "doubanid", "mtype", "season", "episode", "sites", "cache_local"
    ]

    _assert_parameter_prefix(SearchChain.search_by_id, resource_parameters)
    _assert_parameter_prefix(SearchChain.async_search_by_id, resource_parameters)
    _assert_parameter_prefix(SearchChain.async_search_by_id_stream, resource_parameters)
    _assert_parameter_prefix(SearchChain.async_search_subtitles_by_id, subtitle_parameters)
    _assert_parameter_prefix(SearchChain.async_search_subtitles_by_id_stream, subtitle_parameters)


def test_download_chain_media_source_parameters_preserve_old_order() -> None:
    """字幕下载链新增媒体源参数必须追加在原有位置参数之后。"""
    _assert_parameter_prefix(DownloadChain.download_subtitle, [
        "self", "subtitle", "media_source", "media_id", "tmdbid", "doubanid",
        "save_path", "username",
    ])


def test_transfer_chain_media_source_parameters_preserve_old_order() -> None:
    """整理链新增媒体源参数必须追加在原有位置参数之后。"""
    _assert_parameter_prefix(TransferChain.manual_transfer, [
        "self", "fileitem", "target_storage", "target_path", "tmdbid", "doubanid",
        "media_source", "media_id", "mtype", "season", "episode_group", "transfer_type",
        "epformat", "min_filesize", "scrape", "library_type_folder",
        "library_category_folder", "force", "background", "downloader", "download_hash",
        "preview", "sync_extra_files", "cleanup_dest_fileitem",
    ])


def test_subscribe_chain_media_source_parameters_preserve_old_order() -> None:
    """订阅链新增媒体源参数必须追加在原有位置参数之后。"""
    parameters = [
        "self", "title", "year", "mtype", "tmdbid", "doubanid", "bangumiid", "mediaid",
        "episode_group", "season", "channel", "source", "userid", "username", "message", "exist_ok",
    ]

    _assert_parameter_prefix(SubscribeChain.add, parameters)
    _assert_parameter_prefix(SubscribeChain.async_add, parameters)
