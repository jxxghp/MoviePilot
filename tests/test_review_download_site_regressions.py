"""
下载、站点与字幕审查问题的隔离回归。
"""

import asyncio
import json
import pickle
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from torrentool.bencode import Bencode
from torrentool.torrent import Torrent
from transmission_rpc import Client

from app.application import rss
from app.application.torrent import download as torrent_download
from app.application.torrent.cache import TorrentCacheRecognitionService
from app.chain.download.subtitle import DownloadSubtitleOwner
from app.chain.search import SearchChain
from app.chain.site import SiteChain
from app.chain.torrents import TorrentsChain
from app.domain.context import Context, MediaInfo, SubtitleInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.foundation.crypto import HashUtils
from app.modules.qbittorrent import QbittorrentModule
from app.modules.rtorrent import RtorrentModule
from app.modules.rtorrent.rtorrent import Rtorrent
from app.modules.transmission.transmission import Transmission
from app.schemas.types import MediaSource, MediaType


def _torrent(piece=b'x'):
    """
    生成只有一个视频文件的真实种子内容。
    """
    return Bencode.encode({'info': {'name': 'Demo.S01', 'piece length': 16384,
        'pieces': piece * 20, 'files': [{'length': 100, 'path': ['Demo.S01E01.mkv']}]}})


def test_f034_transmission_tracker_rpc_preserves_urls():
    """
    SDK编码后Tracker仍为完整URL，不能被拆成字符。
    """
    client = object.__new__(Client)
    client._Client__protocol_version = 17
    calls = []
    client._request = lambda *args, **kwargs: calls.append((args, kwargs))
    server = object.__new__(Transmission)
    server.trc = client
    urls = ['https://tracker.invalid/announce', 'udp://backup.invalid:80/announce']
    assert server.update_tracker('a' * 40, urls)
    assert calls[-1][0][1]['trackerList'].split() == urls


def test_f035_transmission_custom_rpc_path(monkeypatch):
    """
    配置完整URL时保留代理RPC路径且不发网络请求。
    """
    monkeypatch.setattr(Client, 'get_session', lambda self: SimpleNamespace(rename_partial_files=True))
    server = Transmission(host='https://fictional.invalid/proxy/transmission/rpc')
    try:
        assert server.trc._url == 'https://fictional.invalid:443/proxy/transmission/rpc'
    finally:
        server.trc._http_session.close()


def test_f036_transmission_requests_limit_fields():
    """
    任务RPC字段包含后续统一投影需要的限制值。
    """
    assert {'downloadLimit', 'uploadLimit', 'seedRatioLimit', 'seedIdleLimit'} <= set(Transmission._trarg)


@pytest.mark.parametrize('files,set_ok,start_ok', [
    (None, True, True),
    ([{'id': 0, 'name': 'Demo.S01E01.mkv'}], True, True),
    ([{'id': 0, 'name': 'Demo.S01E01.mkv'}, {'id': 1, 'name': 'Demo.S01E02.mkv'}], False, True),
    ([{'id': 0, 'name': 'Demo.S01E02.mkv'}], True, False),
])
def test_f037_rtorrent_selection_failure_never_reports_hash(monkeypatch, files, set_ok, start_ok):
    """
    未确认选集及启动成功时不返回能被下载链结算的Hash。
    """
    content = _torrent()
    server = SimpleNamespace(add_torrent=Mock(return_value=True),
        get_torrent_id_by_tag=Mock(return_value=Torrent.from_string(content).info_hash),
        get_files=Mock(return_value=files), set_files=Mock(return_value=set_ok),
        start_torrents=Mock(return_value=start_ok), stop_torrents=Mock(return_value=True))
    module = object.__new__(RtorrentModule)
    monkeypatch.setattr(module, 'get_instance', lambda name: server)
    monkeypatch.setattr(module, 'normalize_path', lambda path, name: str(path))
    monkeypatch.setattr('app.modules.rtorrent.get_runtime_setting', lambda key: '')
    can_start = bool(files and set_ok and any('E02' in f['name'] for f in files))
    result = module.download(content, Path('/downloads'), '', episodes={2}, downloader='rt')
    assert result[1] is None
    if not can_start:
        server.start_torrents.assert_not_called()


def test_f038_rtorrent_duplicate_requires_infohash(monkeypatch):
    """
    同名称大小不同InfoHash的任务不能接管或修改标签。
    """
    other = Torrent.from_string(_torrent(b'y'))
    server = SimpleNamespace(add_torrent=Mock(return_value=False),
        get_torrents=Mock(return_value=([{'name': other.name, 'total_size': other.total_size,
            'hash': other.info_hash, 'tags': '已整理'}], False)), remove_torrents_tag=Mock())
    module = object.__new__(RtorrentModule)
    monkeypatch.setattr(module, 'get_instance', lambda name: server)
    monkeypatch.setattr(module, 'normalize_path', lambda path, name: str(path))
    monkeypatch.setattr('app.modules.rtorrent.get_runtime_setting', lambda key: '')
    result = module.download(_torrent(), Path('/downloads'), '', downloader='rt')
    assert result[1] is None
    server.remove_torrents_tag.assert_not_called()


def test_f039_rtorrent_delete_uses_host_mapping(monkeypatch, tmp_path):
    """
    删除文件使用宿主映射路径，不能误删下载器命名空间同名文件。
    """
    host = tmp_path / 'host' / 'Demo.mkv'
    remote = tmp_path / 'remote' / 'Demo.mkv'
    for path in (host, remote):
        path.parent.mkdir()
        path.write_bytes(b'keep until mapped delete')
    server = object.__new__(Rtorrent)
    server._proxy = SimpleNamespace(d=SimpleNamespace(base_path=lambda tid: str(remote), erase=Mock()))
    module = object.__new__(RtorrentModule)
    monkeypatch.setattr(module, 'get_instance', lambda name: server)
    monkeypatch.setattr(module, 'get_config', lambda name: SimpleNamespace(path_mapping=[(str(host.parent), str(remote.parent))]))
    assert module.remove_torrents('a' * 40, delete_file=True, downloader='rt')
    assert not host.exists()
    assert remote.exists()


def test_f040_concurrent_subtitle_writes_are_isolated(monkeypatch, tmp_path):
    """
    两次同名字幕上传交错仍读到各自内容。
    """
    first_upload, second_written, first_read = Event(), Event(), Event()
    uploaded = {}

    def upload(directory, path):
        """
        仅在存储上传边界控制顺序并读取真实临时内容。
        """
        if directory.path == '/target-A':
            first_upload.set()
            assert second_written.wait(5)
            uploaded['A'] = path.read_bytes()
            first_read.set()
        else:
            uploaded['B'] = path.read_bytes()
            second_written.set()
            assert first_read.wait(5)
        return SimpleNamespace(path=f'{directory.path}/{path.name}')

    storage = SimpleNamespace(get_folder=lambda storage, path: SimpleNamespace(path=str(path)),
        get_file_item=lambda *args: None, upload_file=upload)
    owner = object.__new__(DownloadSubtitleOwner)
    owner.runtime_config = SimpleNamespace(temporary_path=tmp_path, subtitle_extensions=('.srt',))
    monkeypatch.setattr('app.chain.download.subtitle.StorageChain', lambda: storage)
    subtitle = SubtitleInfo(title='Demo', subtitle_id='1', file_name='subtitle.srt', enclosure='https://fictional.invalid/sub')

    def save(label):
        """
        保存一个独立响应。
        """
        return owner._save_subtitle_response(subtitle, SimpleNamespace(content=label.encode()), 'local', Path(f'/target-{label}'))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, 'A')
        assert first_upload.wait(5)
        second = pool.submit(save, 'B')
        assert first.result(10)[0] and second.result(10)[0]
    assert uploaded == {'A': b'A', 'B': b'B'}
    assert not list(tmp_path.iterdir())


def test_f041_auto_subtitle_cannot_clean_external_directory(tmp_path):
    """
    恶意响应文件名及解压失败不触及临时根外已有目录。
    """
    from app.startup.composition.network import _DownloadArchiveAdapter
    temporary = tmp_path / 'temporary'
    temporary.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / 'keep.txt'
    victim.write_text('keep')
    owner = object.__new__(DownloadSubtitleOwner)
    owner.runtime_config = SimpleNamespace(temporary_path=temporary, subtitle_extensions=('.srt',))
    response = SimpleNamespace(status_code=200, content=b'invalid zip',
        headers={'content-disposition': f'attachment; filename="{outside}.zip"'})
    owner._save_site_subtitle_response(response=response, sublink='https://fictional.invalid/sub',
        archive_port=_DownloadArchiveAdapter(), storage_chain=None, storage='local',
        working_dir_item=SimpleNamespace(path='/target'))
    assert victim.read_text() == 'keep'
    assert not list(temporary.iterdir())


@pytest.mark.parametrize('case', ['cross-origin', 'relative', 'loop'])
def test_f042_f043_f044_torrent_redirect_contract(monkeypatch, case):
    """
    跳转不泄露Cookie，支持相对地址并在环路有界终止。
    """
    content = _torrent()
    calls = []
    original = 'https://site.invalid/download'

    def request(**kwargs):
        """
        模拟重定向与真实种子响应，限定最大调用防止旧代码挂起。
        """
        calls.append(kwargs)
        assert len(calls) <= 12, 'redirect loop was not bounded'
        if len(calls) == 1 or case == 'loop':
            location = {'cross-origin': 'https://cdn.invalid/file', 'relative': '/file', 'loop': original}[case]
            return SimpleNamespace(status_code=302, headers={'Location': location}, content=b'', text='')
        assert kwargs['url'].startswith('https://')
        return SimpleNamespace(status_code=200, headers={}, content=content, text='')

    monkeypatch.setattr(torrent_download, '_require_torrent_port', lambda: SimpleNamespace(request=request))
    monkeypatch.setattr(torrent_download, 'FileCache', lambda **kwargs: SimpleNamespace(get=lambda *a, **k: None, set=lambda *a, **k: None))
    result = torrent_download.TorrentHelper().download_torrent(original, cookie='private=secret')
    if case == 'loop':
        assert result[1] is None
    else:
        assert result[1] == content
        assert calls[1]['cookie'] == (None if case == 'cross-origin' else 'private=secret')
        if case == 'relative':
            assert calls[1]['url'] == 'https://site.invalid/file'


def test_f045_qb_label_failure_propagates(monkeypatch):
    """
    标签RPC失败向模块调用者报告失败。
    """
    module = object.__new__(QbittorrentModule)
    monkeypatch.setattr(module, 'get_instance', lambda name: SimpleNamespace(set_torrents_tag=lambda **kwargs: False))
    assert module.set_torrents_tag('a' * 40, ['tag']) is False


@pytest.mark.parametrize('message', ['RSS Link has expired, You need to get a new one!', 'RSS 链接已过期, 您需要获得一个新的!'])
def test_f047_expired_rss_preserves_renewal_signal(monkeypatch, message):
    """
    站点过期纯文本保留None合同以进入续期路径。
    """
    response = SimpleNamespace(status_code=200, content=message.encode(), text=message)
    ports = (SimpleNamespace(get=lambda **kwargs: response, decode_xml=lambda response, **kwargs: response.text), None, None)
    monkeypatch.setattr(rss, '_require_rss_ports', lambda: ports)
    monkeypatch.setattr(rss.RssHelper, '_RssHelper__parse_with_rust', lambda self, value: None)
    assert rss.RssHelper().parse('https://site.invalid/rss') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_writes', [False, True])
async def test_f049_f050_cache_reidentification_merges_and_reports_write_error(fail_writes):
    """
    并发识别不同条目保留两项修改，落盘失败不得返回成功。
    """
    original = {'site.invalid': [Context(meta_info=MetaInfo(f'Demo{index}.2026'),
        media_info=MediaInfo(type=MediaType.MOVIE, title=f'Original {index}'),
        torrent_info=TorrentInfo(title=f'Demo{index}.2026', description='', category=MediaType.MOVIE.value)) for index in (1, 2)]}
    files = {'__torrents_cache__': pickle.dumps(original), '__torrents_music_cache__': pickle.dumps({})}

    async def get(filename):
        """
        每次解码真实快照，避免共享引用掩盖丢更新。
        """
        return files.get(filename)

    async def set_value(filename, value):
        """
        模拟持久化端口失败或实际保存快照。
        """
        if fail_writes:
            raise OSError('fictional cache write failure')
        await asyncio.sleep(0)
        files[filename] = value

    chain = object.__new__(TorrentsChain)
    chain.runtime_config = SimpleNamespace(subscribe_mode='spider')
    chain.async_filecache = SimpleNamespace(get=get, set=set_value)
    arrived, ready = [], asyncio.Event()

    async def recognize(**kwargs):
        """
        两次识别均读取旧快照后同时完成远端结果。
        """
        arrived.append(kwargs['media_id'])
        if len(arrived) == 2 or fail_writes:
            ready.set()
        await ready.wait()
        return MediaInfo(type=MediaType.MOVIE, title='New', media_source=MediaSource.TMDB, media_id=kwargs['media_id'])

    service = TorrentCacheRecognitionService(chain, SimpleNamespace(async_recognize_media=recognize))

    async def execute(index):
        """
        从真实应用入口更新指定目标。
        """
        return await service.execute('site.invalid', HashUtils.md5(f'Demo{index}.2026'), media_source=MediaSource.TMDB, media_id=str(index))

    results = [await execute(1)] if fail_writes else await asyncio.gather(execute(1), execute(2))
    if fail_writes:
        assert results[0][0] is False
    else:
        assert all(result[0] for result in results)
        assert [c.media_info.media_id for c in pickle.loads(files['__torrents_cache__'])['site.invalid']] == ['1', '2']


def test_f067_favicon_cross_origin_drops_cookie(monkeypatch):
    """
    HTML声明跨域图标时不转发站点登录Cookie。
    """
    calls = []

    @contextmanager
    def response(**kwargs):
        """
        提供无网络有界HTTP响应。
        """
        calls.append(kwargs)
        yield SimpleNamespace(text='<html><head><link rel="icon" href="https://cdn.invalid/icon.png"></head></html>', content=b'image')

    monkeypatch.setattr('app.chain.site._open_site_response', response)
    assert SiteChain._SiteChain__parse_favicon('https://site.invalid/', 'private=secret', '')[1]
    assert calls[1]['cookie'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_f069_subtitle_alias_stops_only_after_matching(monkeypatch, stream):
    """
    首个名称只有无关候选时继续查询别名，列表和渐进入口一致。
    """
    chain = object.__new__(SearchChain)
    chain.runtime_config = SimpleNamespace(search_multiple_name=False)
    media = MediaInfo(title='Review Show', names=['Review Show', 'Review Alias'], year='2024',
        tmdb_id=999999, type=MediaType.TV, season=1, season_years={1: '2024'})
    wrong = SubtitleInfo(title='Unrelated Show 2024 S01E01', site=1, enclosure='https://site.invalid/wrong.zip')
    correct = SubtitleInfo(title='Review Show 2024 S01E01', site=1, enclosure='https://site.invalid/correct.zip')

    async def search(**kwargs):
        """
        第一名称候选不匹配，别名返回可匹配字幕。
        """
        return [wrong] if kwargs['keyword'] == 'Review Show' else [correct]

    async def search_stream(**kwargs):
        """
        提供渐进查询相同远端结果。
        """
        yield {'type': 'append', 'items': await search(**kwargs)}

    async def no_sleep(_delay):
        """
        隔离搜索退避等待。
        """
    monkeypatch.setattr(chain, '_async_search_subtitles_all_sites', search)
    monkeypatch.setattr(chain, '_async_search_subtitles_all_sites_stream', search_stream)
    monkeypatch.setattr('app.chain.search.subtitle.asyncio.sleep', no_sleep)
    monkeypatch.setattr('app.chain.search.plan.get_chain_runtime_config_snapshot', lambda: SimpleNamespace(max_search_name_limit=None))
    if stream:
        events = [event async for event in chain._async_search_subtitles_for_media_stream(media, season=1, episode=1)]
        result = events[-1]['subtitles']
    else:
        result = await chain._async_search_subtitles_for_media(media, season=1, episode=1)
    assert result == [correct]


def test_f070_tnode_instances_follow_current_configuration():
    """
    相同类的新配置有独立客户端与Cookie。
    """
    from app.modules.indexer.spider.tnode import TNodeSpider
    first = TNodeSpider({'domain': 'https://site.invalid/', 'cookie': 'old'})
    second = TNodeSpider({'domain': 'https://site.invalid/', 'cookie': 'new'})
    assert first is not second
    assert first._cookie == 'old' and second._cookie == 'new'


def test_f071_mteam_business_error_is_not_empty_success():
    """
    HTTP成功但业务失败不得伪装为空末页。
    """
    from app.modules.indexer.spider.mtorrent import MTorrentSpider
    spider = object.__new__(MTorrentSpider)
    spider._name, spider._domain = 'review', 'site.invalid'
    response = SimpleNamespace(status_code=200, json=lambda: {'code': '1', 'message': 'invalid token'})
    error, items = spider._MTorrentSpider__process_response(response)
    assert error is True and items == []


def test_f072_mteam_empty_seeding_page_stops():
    """
    空明细页即使汇总仍有做种也终止翻页。
    """
    from app.modules.indexer.parser.mtorrent import MTorrentSiteUserInfo
    parser = MTorrentSiteUserInfo('review', 'https://site.invalid/', '', None, None)
    parser._torrent_seeding_params = {'pageNumber': 2}
    parser._get_page_content = lambda **kwargs: json.dumps({'code': '0', 'data': {'seeder': 100}})
    assert parser._parse_user_torrent_seeding_info(json.dumps({'code': '0', 'data': {'data': []}})) is None


def test_f073_audiences_ignores_other_uploader():
    """
    页面其他上传者不能覆盖顶部当前用户身份和流量。
    """
    from app.modules.indexer.parser.nexus_audiences import NexusAudiencesSiteUserInfo
    parser = NexusAudiencesSiteUserInfo('review', 'https://audiences.me/', '', None, None)
    content = '''<html><body><header class="site-userbar">
      <a data-uploader-url="userdetails.php?id=101" data-uploader-label="current"
      data-uploader-stats='[{"tone":"uploaded","value":"1 TB"}]'>current</a></header>
      <main><a data-uploader-url="userdetails.php?id=202" data-uploader-label="other"
      data-uploader-stats='[{"tone":"uploaded","value":"99 TB"}]'>other</a></main></body></html>'''
    parser._parse_user_traffic_info(content)
    assert parser.userid == '101' and parser.username == 'current'
    assert parser.upload == 1024 ** 4


@pytest.mark.asyncio
async def test_f068_concurrent_site_creates_have_one_committed_owner(tmp_path):
    """
    两个独立事务都先读到不存在时，数据库串行准入仍只提交一个站点。
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.application.site.mutation import SiteMutationCommand
    from app.db.adapters.site import SessionSiteRepository
    from app.db.models.site import Site
    from app.db.uow import SqlAlchemyAsyncUnitOfWork
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/race.db')
    async with engine.begin() as connection:
        await connection.run_sync(Site.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    readers, both_read = [], asyncio.Event()

    async def create_one():
        """
        每个请求使用独立真实仓储与UoW，仅首次查询加屏障。
        """
        async with sessions() as session:
            repository = SessionSiteRepository(session)
            read = repository.async_get_by_domain

            async def synchronized_read(domain):
                """
                保证两个请求都越过原先的先查后插检查。
                """
                result = await read(domain)
                readers.append(domain)
                if len(readers) == 2:
                    both_read.set()
                await asyncio.wait_for(both_read.wait(), 5)
                return result

            repository.async_get_by_domain = synchronized_read
            command = SiteMutationCommand(repository=repository, unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
                auth_level_provider=lambda: 2, indexer_loader=AsyncMock(return_value={'name': 'Review'}),
                domain_extractor=lambda url: 'site.invalid', url_normalizer=lambda url: 'https://site.invalid/',
                publish_updated=AsyncMock(), publish_deleted=AsyncMock())
            return await command.create({'url': 'https://site.invalid/'})

    try:
        results = await asyncio.gather(create_one(), create_one())
        async with sessions() as session:
            rows = list((await session.scalars(select(Site))).all())
        assert len(rows) == 1
        assert sorted(result.success for result in results) == [False, True]
    finally:
        await engine.dispose()


def test_f068_legacy_sync_add_is_transactional_and_preserves_old_duplicates(tmp_path):
    """
    legacy同步入口也串行查重，既有重复记录和配置不被自动删除。
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.db.models.site import Site
    from app.db.oper.site import SiteOper
    engine = create_engine(f'sqlite:///{tmp_path}/legacy.db')
    Site.__table__.create(engine)

    def add_one():
        """
        让两个工作线程各持有独立真实事务。
        """
        with Session(engine) as session, session.begin():
            return SiteOper(db=session).add(name='Review', domain='site.invalid', url='https://site.invalid/')

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: add_one(), range(2)))
        assert sorted(result[0] for result in results) == [False, True]
        with Session(engine) as session, session.begin():
            session.add_all([Site(name='A', domain='legacy.invalid', url='https://legacy.invalid/'),
                             Site(name='B', domain='legacy.invalid', url='https://legacy.invalid/')])
        with Session(engine) as session, session.begin():
            assert SiteOper(db=session).add(name='C', domain='legacy.invalid', url='https://legacy.invalid/')[0] is False
            assert [row.name for row in session.scalars(select(Site).where(Site.domain == 'legacy.invalid')).all()] == ['A', 'B']
    finally:
        engine.dispose()


def test_f068_sync_domain_update_cannot_collide(tmp_path):
    """
    修改域名入口和新增共享准入，不绕过站点身份边界。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.models.site import Site
    from app.db.oper.site import SiteOper, _site_domain_lock
    from app.schemas.exception import SiteDomainConflictError
    engine = create_engine(f'sqlite:///{tmp_path}/update.db')
    Site.__table__.create(engine)
    try:
        with Session(engine) as session, session.begin():
            session.add_all([Site(id=1, name='A', domain='a.invalid', url='https://a.invalid/'),
                             Site(id=2, name='B', domain='b.invalid', url='https://b.invalid/')])
        with Session(engine) as session:
            with pytest.raises(SiteDomainConflictError):
                SiteOper(db=session).update(2, {'domain': 'a.invalid'})
            session.rollback()
            assert session.get(Site, 2).domain == 'b.invalid'
        assert str(_site_domain_lock('postgresql')) == 'LOCK TABLE site IN SHARE ROW EXCLUSIVE MODE'
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_f068_async_domain_update_reports_conflict(tmp_path):
    """
    请求事务域名变更冲突以受控失败返回，原记录保持不变。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.application.site.mutation import SiteMutationCommand
    from app.db.adapters.site import SessionSiteRepository
    from app.db.models.site import Site
    from app.db.uow import SqlAlchemyAsyncUnitOfWork
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/update.db')
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Site.__table__.create)
        async with sessions() as session, session.begin():
            session.add_all([Site(id=1, name='A', domain='a.invalid', url='https://a.invalid/'),
                             Site(id=2, name='B', domain='b.invalid', url='https://b.invalid/')])
        async with sessions() as session:
            command = SiteMutationCommand(repository=SessionSiteRepository(session),
                unit_of_work=SqlAlchemyAsyncUnitOfWork(session), auth_level_provider=lambda: 2,
                indexer_loader=AsyncMock(), domain_extractor=lambda url: 'a.invalid',
                url_normalizer=lambda url: url, publish_updated=AsyncMock(), publish_deleted=AsyncMock())
            result = await command.update({'id': 2, 'url': 'https://a.invalid/'})
            assert result.success is False
            assert (await session.get(Site, 2)).domain == 'b.invalid'
    finally:
        await engine.dispose()


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_redirect_headers_do_not_reappear_after_returning_to_original_origin(monkeypatch, status):
    """
    跨域跳转再回原站时也不恢复已丢弃的敏感信息，并及时关闭中间响应。
    """
    calls, closes = [], []
    urls = ['https://site.invalid/start', 'https://cdn.invalid/ticket', 'https://site.invalid/final']
    content = _torrent()

    def request(**kwargs):
        """
        提供固定三跳链并记录请求凭据。
        """
        calls.append(kwargs)
        index = len(calls) - 1
        return SimpleNamespace(status_code=status if index < 2 else 200,
            headers={'Location': urls[index + 1]} if index < 2 else {},
            content=content if index == 2 else b'', text='', close=lambda: closes.append(index))

    monkeypatch.setattr(torrent_download, '_require_torrent_port', lambda: SimpleNamespace(request=request))
    monkeypatch.setattr(torrent_download, 'FileCache', lambda **kwargs: SimpleNamespace(get=lambda *a, **k: None, set=lambda *a, **k: None))
    assert torrent_download.TorrentHelper().download_torrent(urls[0], cookie='secret', referer='https://site.invalid/private')[1] == content
    assert [call['cookie'] for call in calls] == ['secret', None, None]
    assert [call['referer'] for call in calls] == ['https://site.invalid/private', None, None]
    assert closes == [0, 1]


def test_f067_real_site_adapter_strips_headers_on_redirect(monkeypatch):
    """
    真实组合根在同域图标随后跨域时也清除Cookie和认证头。
    """
    from app.startup.composition.site import _SiteHttpAdapter
    requests = []

    class Request:
        """
        仅替换HTTP传输，保留真实Adapter的逐跳判断。
        """

        def __init__(self, **kwargs):
            """
            保存Adapter传给传输的认证参数。
            """
            self.options = kwargs

        @contextmanager
        def response_manager(self, **kwargs):
            """
            模拟同来源入口指向其他来源。
            """
            requests.append((self.options, kwargs))
            yield SimpleNamespace(status_code=302 if len(requests) == 1 else 200,
                headers={'Location': 'https://cdn.invalid/icon.png'} if len(requests) == 1 else {}, content=b'image')

    monkeypatch.setattr('app.startup.composition.site.RequestUtils', Request)
    with _SiteHttpAdapter().open(method='GET', url='https://site.invalid/icon', cookie='secret',
                                headers={'Authorization': 'Bearer secret', 'Referer': 'private'}) as result:
        assert result.content == b'image'
    assert requests[1][0]['cookies'] == ''
    assert requests[1][0]['headers'] == {}
    assert all(options['allow_redirects'] is False for _, options in requests)


@pytest.mark.asyncio
async def test_cache_lock_cancelled_waiter_does_not_leak():
    """
    异步等待共享缓存锁被取消后，不会迟到取得锁或阻断后继写入。
    """
    from app.runtime.cache import async_cache_mutation, cache_mutation

    async def acquire():
        """
        在缓存锁内完成一次可观测写入。
        """
        async with async_cache_mutation('review-cancel-test'):
            return True

    with cache_mutation('review-cancel-test'):
        waiter = asyncio.create_task(acquire())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert await asyncio.wait_for(acquire(), 1)


@pytest.mark.parametrize('fails', [False, True])
def test_f045_real_qb_client_propagates_label_rpc_result(monkeypatch, fails):
    """
    从真实下载器客户端到模块均传播RPC成功或失败，不能只修模块返回值。
    """
    from app.modules.qbittorrent.qbittorrent import Qbittorrent
    server = object.__new__(Qbittorrent)
    server.qbc = SimpleNamespace(torrents_add_tags=Mock(side_effect=OSError('offline') if fails else None))
    module = object.__new__(QbittorrentModule)
    monkeypatch.setattr(module, 'get_instance', lambda name: server)
    assert module.set_torrents_tag('a' * 40, ['review']) is (not fails)


def test_f068_site_admission_serializes_separate_processes(tmp_path):
    """
    独立Python进程共享临时SQLite时，数据库事务锁仍只允许一个域名创建成功。
    """
    import os
    import subprocess
    import sys

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.db.models.site import Site
    database = tmp_path / 'process-race.db'
    engine = create_engine(f'sqlite:///{database}')
    Site.__table__.create(engine)
    script = '''
import sys, time
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db.oper.site import SiteOper
folder, database, identity = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
(folder / ("ready-" + identity)).touch()
deadline = time.monotonic() + 10
while len(list(folder.glob("ready-*"))) < 2:
    if time.monotonic() >= deadline:
        raise RuntimeError("test barrier timed out")
    time.sleep(0.01)
engine = create_engine("sqlite:///" + database)
with Session(engine) as session, session.begin():
    success, message = SiteOper(db=session).add(name="Review", domain="site.invalid", url="https://site.invalid/")
    print("created=" + str(success), flush=True)
engine.dispose()
'''
    environment = dict(os.environ, CONFIG_DIR=str(tmp_path / 'config'))
    processes = [subprocess.Popen([sys.executable, '-c', script, str(tmp_path), str(database), str(i)],
                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in (1, 2)]
    try:
        outputs = [process.communicate(timeout=20) for process in processes]
        assert [process.returncode for process in processes] == [0, 0], outputs
        assert sorted('created=True' in output for output, _ in outputs) == [False, True]
        with Session(engine) as session:
            assert len(session.scalars(select(Site)).all()) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
        engine.dispose()


def test_subtitle_temp_allocation_failure_is_reported(monkeypatch, tmp_path):
    """
    独占目录申请失败仍返回字幕保存失败，不清理非本次拥有的目录。
    """
    storage = SimpleNamespace(get_folder=lambda storage, path: SimpleNamespace(path=str(path)))
    owner = object.__new__(DownloadSubtitleOwner)
    owner.runtime_config = SimpleNamespace(temporary_path=tmp_path, subtitle_extensions=('.srt',))
    monkeypatch.setattr('app.chain.download.subtitle.StorageChain', lambda: storage)
    monkeypatch.setattr('app.chain.download.subtitle.tempfile.mkdtemp', Mock(side_effect=OSError('offline disk full')))
    subtitle = SubtitleInfo(title='Demo', file_name='subtitle.srt', enclosure='https://site.invalid/sub')
    result = owner._save_subtitle_response(subtitle, SimpleNamespace(content=b'subtitle'), 'local', Path('/target'))
    assert result[0] is False and 'offline disk full' in result[1]


@pytest.mark.parametrize('loop', [False, True])
def test_f042_subtitle_adapter_redirects_are_bounded_and_private(monkeypatch, loop):
    """
    字幕下载适配器也手动解析相对跳转，跨来源移除凭据并终止环路。
    """
    from app.startup.composition.network import _DownloadHttpAdapter
    requests, closed = [], []

    class Request:
        """
        仅替换HTTP端口，检查真实Adapter构造的请求字段。
        """

        def __init__(self, **kwargs):
            """
            保存本次请求的凭据和HTTP头。
            """
            self.options = kwargs

        def get_res(self, url, **kwargs):
            """
            返回确定跳转序列，旧自动跟随参数也能被断言发现。
            """
            requests.append((url, self.options, kwargs))
            if len(requests) == 1 or loop:
                target = 'https://site.invalid/start' if loop else '//cdn.invalid/sub.srt'
                return SimpleNamespace(status_code=302, headers={'Location': target}, close=lambda: closed.append(url))
            return SimpleNamespace(status_code=200, headers={}, content=b'subtitle')

    monkeypatch.setattr('app.startup.composition.network.RequestUtils', Request)
    response = _DownloadHttpAdapter().get('https://site.invalid/start', cookies='secret',
        headers={'Authorization': 'Bearer secret', 'Cookie': 'secret', 'Referer': 'private'})
    if loop:
        assert response is None and len(requests) <= 9
    else:
        assert response.status_code == 200
        assert requests[1][0] == 'https://cdn.invalid/sub.srt'
        assert requests[1][1]['cookies'] is None and requests[1][1]['headers'] == {}
        assert closed == ['https://site.invalid/start']
    assert all(kwargs['allow_redirects'] is False for _, _, kwargs in requests)


def test_f042_signed_subtitle_cross_origin_has_no_site_cookie():
    """
    有效签名授权下载跨域资源也不授权向该来源转发站点Cookie。
    """
    from app import schemas
    from app.api.endpoints.download import _prepare_subtitle_download
    from app.application.security.url import SecurityUtils
    destination = 'https://cdn.invalid/sub.srt'
    subtitle = schemas.SubtitleInfo(site=1, title='Review', enclosure=SecurityUtils.sign_url(destination, purpose='subtitle-download:1'))
    query = SimpleNamespace(get_sync=lambda _: SimpleNamespace(url='https://site.invalid/', cookie='secret', ua='review', proxy=False))
    assert _prepare_subtitle_download(subtitle, query=query)[0]
    assert subtitle.site_cookie is None


def test_f042_automatic_subtitle_external_link_drops_cookie(monkeypatch, tmp_path):
    """
    自动字幕详情页提供跨域下载链接时，不转发详情页站点登录凭据。
    """
    import app.chain.download.subtitle as subtitle_module
    owner = object.__new__(DownloadSubtitleOwner)
    owner.runtime_config = SimpleNamespace(download_subtitle=True, temporary_path=tmp_path, proxy=None)
    destination = 'https://cdn.invalid/sub.srt'
    owner._site_subtitle_links = lambda context: [destination]
    owner._save_site_subtitle_response = Mock()
    response = SimpleNamespace(status_code=200, content=b'subtitle', close=Mock())
    get = Mock(return_value=response)
    monkeypatch.setattr(subtitle_module, 'StorageChain', lambda: object())
    monkeypatch.setattr(subtitle_module, '_resolve_site_subtitle_target_dir', lambda **kwargs: ('local', tmp_path))
    monkeypatch.setattr(subtitle_module, '_prepare_site_subtitle_directory', lambda **kwargs: (SimpleNamespace(path=str(tmp_path)), True))
    monkeypatch.setattr(subtitle_module, '_enqueue_completed_site_subtitles', lambda **kwargs: None)
    monkeypatch.setattr(subtitle_module, '_download_ports_snapshot', lambda: (SimpleNamespace(get=get), object()))
    context = Context(torrent_info=TorrentInfo(page_url='https://site.invalid/details?id=1', site_cookie='secret'))
    owner.download_site_subtitles(context, tmp_path, torrent_content=_torrent())
    assert get.call_args.kwargs['cookies'] is None
    response.close.assert_called_once()


@pytest.mark.parametrize('source,target,expected', [
    ('https://site.invalid/', 'https://site.invalid:443/sub', True),
    ('https://site.invalid/', 'https://SITE.INVALID/sub', True),
    ('https://site.invalid/', 'https://site.invalid:444/sub', False),
    ('https://site.invalid/', 'https://site.invalid:0/sub', False),
    ('https://site.invalid/', 'http://site.invalid/sub', False),
    ('https://site.invalid/', 'https://other.invalid/sub', False),
    ('https://site.invalid/', 'https://secret@site.invalid/sub', False),
    ('invalid', 'https://site.invalid/', False),
    ('https://site.invalid/', 'https://site.invalid:bad/', False),
])
def test_request_origin_checks_scheme_host_and_effective_port(source, target, expected):
    """
    认证来源匹配考虑协议、有效端口和URL凭据，不能只比较主机名。
    """
    from app.foundation.url import UrlUtils
    assert UrlUtils.same_origin(source, target) is expected
