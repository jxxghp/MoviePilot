"""Hermes 对标：独立数据库、真实消息、检索语义、压缩和输出预算。"""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.history.storage import SqliteRecallRepository
from app.agent.middleware.recall import RecallMiddleware, SearchHistoryInput
from app.application.messaging.recall import RecallMessage, RecallQuery, RecallService, RecallSession


@pytest.fixture
def repository(tmp_path):
    """全部消息写入临时独立库，不连接业务主库或外部服务。"""
    return SqliteRecallRepository(tmp_path / 'runtime')


def _append(repository, session, content, *, user='alice', role='user', message='m', **meta):
    """为行为场景追加一条带明确身份的原始消息。"""
    repository.append(user, RecallSession(session, **meta), (RecallMessage(message, role, content),))


class _Executor:
    """测试只模拟宿主 worker 调度，不启动生产数据服务。"""

    async def run(self, function):
        """在独立线程运行同步数据库操作。"""
        return await asyncio.to_thread(function)


def test_independent_store_idempotence_and_user_isolation(repository):
    """同名会话位于不同用户文件；重试保留首份证据而不覆盖。"""
    _append(repository, 'same', 'alpha original')
    _append(repository, 'same', 'replacement')
    _append(repository, 'same', 'bob secret', user='bob')
    assert repository.database_path('alice') != repository.database_path('bob')
    result = repository.search('alice', RecallQuery(session_id='same'))
    assert result['messages'][0]['content'] == 'alpha original'
    assert result['message_count'] == 1
    assert repository.search('alice', RecallQuery(query='secret'))['count'] == 0
    assert not repository.search('mallory', RecallQuery(session_id='same'))['success']


def test_fts_boolean_phrase_prefix_and_relaxation(repository):
    """普通全文支持布尔、短语和前缀；多词无交集才进行 OR 放宽。"""
    for sid, text in [('a', 'docker networking fixed'), ('b', 'docker broken tls'), ('c', 'resolved networking')]:
        _append(repository, sid, text)
    def query(text):
        """以一致预算执行本测试的不同 FTS 表达式。"""
        return repository.search('alice', RecallQuery(query=text, limit=10))
    assert {item['session_id'] for item in query('docker NOT broken')['results']} == {'a'}
    assert query('"docker networking"')['count'] == 1
    assert query('networ*')['count'] == 2
    assert query('docker OR resolved')['count'] == 3
    result = query('docker absentword')
    assert result['search_path'] == 'fts5_or'
    assert result['count'] == 2


def test_adaptive_discovery_and_read_scroll_budgets(repository):
    """首命中给窗口和书挡，后续紧凑；长会话阅读只返回首20尾10。"""
    for sid in ('first', 'second'):
        messages = tuple(RecallMessage(str(index), 'user', ('needle ' if index == 25 else '') + 'x' * 8000)
                         for index in range(40))
        repository.append('alice', RecallSession(sid), messages)
    result = repository.search('alice', RecallQuery(query='needle'))
    assert [entry['detail'] for entry in result['results']] == ['full', 'compact']
    first, second = result['results']
    assert len(first['messages']) == 11
    assert len(first['bookend_start']) == 3
    assert len(second['messages']) == 1
    assert all(len(message['content']) <= 4000 for message in first['messages'])
    read = repository.search('alice', RecallQuery(session_id='first'))
    assert read['message_count'] == 40 and read['truncated']
    assert len(read['messages']) == 30
    scroll = repository.search('alice', RecallQuery(session_id=first['session_id'], around_message_id=first['match_message_id']))
    assert len(scroll['messages']) == 11
    assert scroll['messages_before'] == 25
    assert scroll['messages_after'] == 14


def test_current_compacted_rewind_and_lineage(repository):
    """压缩历史可查；活动本会话和 rewind 丢弃行不得伪装成历史。"""
    _append(repository, 'root', 'needle root')
    _append(repository, 'child', 'needle child', parent_session_id='root')
    assert repository.search('alice', RecallQuery(query='needle'))['count'] == 1
    assert repository.search('alice', RecallQuery(query='needle', current_session_id='child'))['count'] == 0
    repository.compact('alice', 'root', ('m',))
    result = repository.search('alice', RecallQuery(query='needle', current_session_id='child'))
    assert result['results'][0]['session_id'] == 'root'
    assert result['count'] == 1
    with sqlite3.connect(repository.database_path('alice')) as connection:
        connection.execute("UPDATE messages SET active=0,compacted=0 WHERE session_id='child'")
    assert repository.search('alice', RecallQuery(session_id='child'))['message_count'] == 0
    assert repository.search('alice', RecallQuery(query='needle', exclude_session_ids=('child',)))['count'] == 0


def test_hidden_sources_cron_demotion_and_time_bounds(repository):
    """后台隐藏来源不进入用户历史；cron 保留可达性但不会压住人工会话。"""
    _append(repository, 'cron', 'needle', source='cron', started_at=100)
    _append(repository, 'chat', 'needle and some context', started_at=200)
    _append(repository, 'hidden', 'needle', source='subagent', started_at=150)
    result = repository.search('alice', RecallQuery(query='needle'))
    assert [entry['session_id'] for entry in result['results']] == ['chat', 'cron']
    result = repository.search('alice', RecallQuery(query='needle', after=100, before=200))
    assert [entry['session_id'] for entry in result['results']] == ['cron']
    assert not repository.search('alice', RecallQuery(session_id='hidden'))['success']


def test_tool_prefix_index_full_body_recall_and_redaction(repository):
    """仅缩小工具索引而不丢弃原文，显式 tool 查询能召回8192字符后的证据。"""
    text = 'ordinary ' * 3000 + ' deepneedle password=verysecret '
    _append(repository, 'tool', text, role='tool')
    assert repository.search('alice', RecallQuery(query='deepneedle'))['count'] == 0
    result = repository.search('alice', RecallQuery(query='deepneedle', role_filter=('tool',)))
    assert result['count'] == 1 and result['search_path'] == 'like'
    with sqlite3.connect(repository.database_path('alice')) as connection:
        content = connection.execute('SELECT content FROM messages').fetchone()[0]
        assert 'deepneedle' in content and 'verysecret' not in content
        connection.execute("INSERT INTO messages_fts(messages_fts,rank) VALUES ('integrity-check',1)")


def test_cjk_trigram_and_short_term_fallback(repository):
    """未安装可选 CJK tokenizer 时，三字走 trigram、两字诚实回退。"""
    _append(repository, 'zh', '昨天修复了下载订阅失败的问题')
    result = repository.search('alice', RecallQuery(query='下载订阅'))
    assert result['count'] == 1 and result['search_path'] == 'trigram'
    result = repository.search('alice', RecallQuery(query='订阅'))
    assert result['count'] == 1 and result['search_path'] == 'like'


def test_delete_removes_fts_and_blocks_inflight_resurrection(repository):
    """显式删除同步移除索引，晚到的消息批次不能让旧会话重新出现。"""
    _append(repository, 'old', 'needle')
    repository.delete('alice', 'old')
    _append(repository, 'old', 'needle late', message='late')
    assert repository.search('alice', RecallQuery(query='needle'))['count'] == 0
    with sqlite3.connect(repository.database_path('alice')) as connection:
        connection.execute("INSERT INTO messages_fts(messages_fts,rank) VALUES ('integrity-check',1)")
        connection.execute("INSERT INTO messages_fts_trigram(messages_fts_trigram,rank) VALUES ('integrity-check',1)")
        assert connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


def test_concurrent_writers_keep_message_and_index_atomic(repository):
    """多个独立连接并发重试仍然只有一份原文和有效索引。"""
    _append(repository, 'init', 'initialize')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: _append(repository, 'same', 'concurrent needle'), range(20)))
    assert repository.search('alice', RecallQuery(session_id='same'))['message_count'] == 1
    assert repository.search('alice', RecallQuery(query='needle'))['count'] == 1


def test_query_deadline_reports_unavailable_not_no_matches(tmp_path):
    """SQL 扫描实际超时必须返回未完成，不能把性能保护伪装成没有历史。"""
    repository = SqliteRecallRepository(tmp_path, query_timeout=0.000001)
    _append(repository, 'old', 'x' * 50000)
    for index in range(100):
        _append(repository, f'session{index}', 'x' * 20000)
    result = repository.search('alice', RecallQuery(query='找', role_filter=('tool', 'user')))
    assert not result['success'] and result['error'] == 'query_timeout' and not result['complete']


@pytest.mark.asyncio
async def test_capture_raw_messages_without_summary_or_image(repository):
    """中间件保存真实消息并隔离合成摘要、图像和不同用户。"""
    middleware = RecallMiddleware(RecallService(repository, _Executor()), 'alice', RecallSession('old'))
    messages = [HumanMessage(id='human', content=[{'type': 'text', 'text': 'needle user'},
                                                {'type': 'image_url', 'image_url': {'url': 'pixels'}}]),
                AIMessage(id='ai', content='needle answer'),
                HumanMessage(id='summary', content='synthetic', additional_kwargs={'lc_source': 'summarization'})]
    await middleware.abefore_model({'messages': messages}, None)
    await middleware.aafter_model({'messages': messages}, None)
    result = repository.search('alice', RecallQuery(session_id='old'))
    assert result['message_count'] == 2
    assert 'pixels' not in json.dumps(result) and 'synthetic' not in json.dumps(result)
    next_session = RecallMiddleware(RecallService(repository, _Executor()), 'alice', RecallSession('new'))
    assert json.loads(await next_session.session_search(query='needle'))['count'] == 1


@pytest.mark.parametrize('kwargs', [{'around_message_id': 1}, {'window': 21}, {'role_filter': 'system'},
                                   {'after': '2026-09-30', 'before': '2026-09-01'}])
def test_invalid_query_rejected(kwargs):
    """无效查询必须在存储前拒绝，不能降级为更宽的读取。"""
    with pytest.raises(ValueError):
        SearchHistoryInput(**kwargs)


@pytest.fixture(scope='session')
def cjk_extension(tmp_path_factory):
    """只编译一次原版 Hermes tokenizer；不安装到用户运行目录。"""
    import shutil
    import subprocess
    compiler = shutil.which('cc')
    if not compiler:
        pytest.skip('CJK native tokenizer verification requires a C compiler')
    root = Path(__file__).resolve().parents[1]
    output = tmp_path_factory.mktemp('fts5-cjk') / 'libfts5_cjk.so'
    subprocess.run([compiler, '-shared', '-fPIC', '-O2', '-I' + str(root / 'native/fts5_cjk/vendor'),
                    str(root / 'native/fts5_cjk/fts5_cjk.c'), '-o', str(output)], check=True, capture_output=True)
    return output


def test_native_cjk_rebuild_preserves_insert_delete_and_integrity(tmp_path, cjk_extension):
    """安装中文分词器后分批回填，回填中的新增和未回填删除不损坏索引。"""
    import shutil
    repository = SqliteRecallRepository(tmp_path)
    for index in range(12):
        _append(repository, f's{index}', '昨天修复了下载订阅失败问题')
    (tmp_path / 'lib').mkdir()
    shutil.copy2(cjk_extension, tmp_path / 'lib/libfts5_cjk.so')
    status = repository.maintain('alice', batch_size=3)
    assert status['pending']
    repository.delete('alice', 's10')
    _append(repository, 'new', '中文订阅索引新消息')
    while repository.maintain('alice', batch_size=3)['pending']:
        pass
    result = repository.search('alice', RecallQuery(query='订阅', limit=10))
    assert result['search_path'] == 'cjk'
    assert result['count'] == 10
    with sqlite3.connect(repository.database_path('alice')) as connection:
        connection.enable_load_extension(True)
        connection.load_extension(str(tmp_path / 'lib/libfts5_cjk.so'))
        connection.execute("INSERT INTO messages_fts_cjk(messages_fts_cjk,rank) VALUES ('integrity-check',1)")
    (tmp_path / 'lib/libfts5_cjk.so').unlink()
    _append(repository, 'missingtokenizer', '中文订阅索引丢失期间新消息')
    result = repository.search('alice', RecallQuery(query='订阅'))
    assert result['search_path'] == 'like'
    shutil.copy2(cjk_extension, tmp_path / 'lib/libfts5_cjk.so')
    while repository.maintain('alice', batch_size=3)['pending']:
        pass
    assert repository.search('alice', RecallQuery(query='丢失'))['search_path'] == 'cjk'


def test_title_match_time_bounds_and_latest_continuation(repository):
    """标题不在正文也可定位，优先最新同名续篇，时间条件仍必须生效。"""
    _append(repository, 'first', 'unrelated', title='TLS repair', started_at=100)
    _append(repository, 'second', 'unrelated', title='TLS repair #2', started_at=200)
    result = repository.search('alice', RecallQuery(query='TLS repair'))
    assert result['results'][0]['session_id'] == 'second'
    assert result['results'][0]['matched_role'] == 'session_title'
    assert repository.search('alice', RecallQuery(query='TLS repair', before=150))['count'] == 0


def test_legacy_import_resumable_identity_and_main_store_independence(tmp_path):
    """后台搬迁保留可用快照，完成后查询不再依赖主库在线。"""
    from langchain_core.messages import messages_to_dict

    from app.application.messaging.recall import RecallLegacyRecord

    class Legacy:
        """模拟只有一次导出可用的旧主库。"""

        calls = 0

        def page(self, user_id, after, limit):
            """提供一次快照，后续访问视为不必要的主库依赖。"""
            self.calls += 1
            assert self.calls == 1 and user_id == 'alice' and after == 0 and limit == 20
            return (RecallLegacyRecord(1, RecallSession('legacy', started_at=100),
                                       tuple(messages_to_dict([HumanMessage(content='old needle')]))),)

    legacy = Legacy()
    repository = SqliteRecallRepository(tmp_path, legacy=legacy)
    assert not repository.search('alice', RecallQuery(query='needle'))['legacy_import']['complete']
    assert not repository.maintain('alice')['pending']
    result = repository.search('alice', RecallQuery(query='needle'))
    assert result['count'] == 1 and result['legacy_import']['complete']
    assert result['results'][0]['messages'][0]['provenance'] == 'legacy_snapshot'
    repository.maintain('alice')
    assert legacy.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('message_name', ['inspect', None])
async def test_full_tool_capture_before_output_truncation(repository, message_name):
    """内置工具的 recorder 原文进入独立库，模型收到的结果仍维持既有引用。"""
    from types import SimpleNamespace

    from app.agent.tools.base import TOOL_RESULT_RECORDER
    middleware = RecallMiddleware(RecallService(repository, _Executor()), 'alice', RecallSession('old'))
    raw = 'tool prefix ' * 3000 + ' tailneedle'

    async def handler(_request):
        """模拟工具在格式化裁剪前发送完整结果给宿主 recorder。"""
        TOOL_RESULT_RECORDER.get()('inspect', raw)
        return ToolMessage(id='tool1', name=message_name, tool_call_id='call1', content='truncated reference')

    result = await middleware.awrap_tool_call(SimpleNamespace(tool_call={'id': 'call1', 'name': 'inspect'}), handler)
    assert result.content == 'truncated reference'
    found = repository.search('alice', RecallQuery(query='tailneedle', role_filter=('tool',)))
    assert found['count'] == 1
    with sqlite3.connect(repository.database_path('alice')) as connection:
        assert connection.execute('SELECT content FROM messages').fetchone()[0] == raw


def test_index_query_plan_uses_fts_and_session_seek(repository):
    """大历史的检索入口必须走 FTS 虚表与主键回表，窗口按会话索引定位。"""
    _append(repository, 'old', 'needle')
    with sqlite3.connect(repository.database_path('alice')) as connection:
        plan = ' '.join(row[3] for row in connection.execute('''EXPLAIN QUERY PLAN
            SELECT m.id FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid
            WHERE messages_fts MATCH ? ORDER BY rank LIMIT 300''', ('needle',)))
        assert 'VIRTUAL TABLE INDEX' in plan and 'INTEGER PRIMARY KEY' in plan
        plan = ' '.join(row[3] for row in connection.execute('''EXPLAIN QUERY PLAN
            SELECT id FROM messages WHERE session_id=? AND id<? ORDER BY id DESC LIMIT 5''', ('old', 100)))
        assert 'messages_session' in plan


def test_independent_retention_uses_shared_cutoff_and_disable(tmp_path):
    """清理使用独立库活动时间，不需要主库行；关闭或零天由组合根返回 None。"""
    cutoff = [None]
    repository = SqliteRecallRepository(tmp_path, retention_cutoff=lambda: cutoff[0])
    repository.append('alice', RecallSession('old', started_at=100, last_active=100),
                      (RecallMessage('1', 'user', 'needle', timestamp=100),))
    repository.maintain('alice')
    assert repository.search('alice', RecallQuery(query='needle'))['count'] == 1
    cutoff[0] = 200
    repository.maintain('alice')
    assert repository.search('alice', RecallQuery(query='needle'))['count'] == 0


@pytest.mark.asyncio
async def test_compaction_success_preserves_same_session_recall(repository):
    """实际压缩提交删除的原文可在同会话召回，而未压缩尾部不重复进入历史。"""

    from langchain.agents.middleware.types import ExtendedModelResponse, ModelResponse
    from langgraph.types import Command
    middleware = RecallMiddleware(RecallService(repository, _Executor()), 'alice', RecallSession('current'))
    old = HumanMessage(id='old', content='needle old request')
    tail = HumanMessage(id='tail', content='current query')
    await middleware.abefore_model({'messages': [old, tail]}, None)

    class Request:
        """只实现中间件需要的请求覆写协议。"""

        messages = [old, tail]
        system_message = None

        def override(self, **_values):
            """返回同一受控请求，测试只关注压缩提交的历史可见性。"""
            return self

    async def handler(_request):
        """模拟最内层压缩中间件在模型成功后提交状态。"""
        return ExtendedModelResponse(model_response=ModelResponse(result=[AIMessage(content='done')]),
                                     command=Command(update={'messages': [tail]}))

    await middleware.awrap_model_call(Request(), handler)
    assert json.loads(await middleware.session_search(query='needle'))['count'] == 1
    assert json.loads(await middleware.session_search(query='current'))['count'] == 0


@pytest.mark.asyncio
async def test_graph_tool_evidence_recalled_in_next_session(repository):
    """真实 LangChain 图产生工具回执与最终回复，下一会话能够定位并展开。"""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.tools import tool

    class Model(FakeMessagesListChatModel):
        """确定性模型只验证执行链，不把它当作真实智能提升评测。"""

        def bind_tools(self, _tools, **_kwargs):
            """测试模型接受工具绑定而不访问供应商。"""
            return self

    @tool
    def inspect_subscription() -> str:
        """返回受控订阅失败证据，不调用业务服务。"""
        return json.dumps({'success': False, 'reason': 'site authentication failed', 'id': 42})

    service = RecallService(repository, _Executor())
    middleware = RecallMiddleware(service, 'alice', RecallSession('old'))
    model = Model(responses=[AIMessage(content='', tool_calls=[{'name': 'inspect_subscription', 'args': {}, 'id': 'c1'}]),
                             AIMessage(content='Subscription 42 failed because of site authentication.')])
    graph = create_agent(model, tools=[inspect_subscription], middleware=[middleware])
    await graph.ainvoke({'messages': [HumanMessage(content='Check subscription 42')]})
    next_middleware = RecallMiddleware(service, 'alice', RecallSession('new'))
    result = json.loads(await next_middleware.session_search(query='authentication'))
    assert result['count'] == 1
    entries = result['results'][0]['messages']
    assert any(message['role'] == 'tool' and 'site authentication failed' in message['content'] for message in entries)
    assert any(message['role'] == 'assistant' and 'Subscription 42 failed' in message['content'] for message in entries)


@pytest.mark.asyncio
async def test_history_output_keeps_its_own_bounded_structure(repository):
    """已按窗口预算裁剪的历史不能再变成通用预览，丢失锚点和书挡。"""
    from types import SimpleNamespace

    from app.agent.middleware.output import ToolOutputMiddleware
    middleware = RecallMiddleware(RecallService(repository, _Executor()), 'alice', RecallSession('new'))
    payload = json.dumps({'messages': [{'id': 1, 'content': 'x' * 4000}] * 20, 'mode': 'scroll'})
    request = SimpleNamespace(tool_call={'name': 'session_search'}, tool=middleware.tools[0])
    output = ToolOutputMiddleware(SimpleNamespace(agent_context={}))

    async def handler(_request):
        """返回具有明确窗口预算的历史响应。"""
        return ToolMessage(content=payload, tool_call_id='history1')

    result = await output.awrap_tool_call(request, handler)
    assert json.loads(result.content)['mode'] == 'scroll'
    assert 'tool_result_truncated' not in result.content


@pytest.mark.asyncio
async def test_managed_maintenance_drains_and_shutdown_cancels(tmp_path):
    """后台维护只登记一个用户任务，退出后清除 owner；取消保留可恢复进度。"""
    from app.runtime.tasks import TaskRegistry
    registry = TaskRegistry()
    repository = SqliteRecallRepository(tmp_path)
    service = RecallService(repository, _Executor(), registry)
    await service.append('alice', RecallSession('old'), (RecallMessage('1', 'user', 'needle'),))
    # 第一次 append 已登记；连续触发不可生成重复后台工作。
    service._schedule_maintenance('alice')
    assert len(registry.records) == 1
    assert registry.records[0].owner == 'agent.history'
    await registry.records[0].task
    assert not registry.records and not service._maintenance
    await service.search('alice', RecallQuery(query='needle'))
    assert await registry.shutdown(timeout_seconds=1)
    assert not service._maintenance


@pytest.mark.asyncio
async def test_maintenance_cancelled_before_first_execution_releases_slot(tmp_path):
    """立即取消尚未进入协程的任务也必须释放维护名额，允许后续重试。"""
    from app.runtime.tasks import TaskRegistry
    registry = TaskRegistry()
    service = RecallService(SqliteRecallRepository(tmp_path), _Executor(), registry)
    service._schedule_maintenance('alice')
    registry.records[0].task.cancel()
    assert await registry.shutdown(timeout_seconds=1)
    assert not service._maintenance


@pytest.mark.asyncio
async def test_archive_failure_does_not_replay_completed_tool(repository):
    """归档错误不重分类实际工具结果，避免模型误以为未执行而重复副作用。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    service = RecallService(repository, _Executor())
    service.append = AsyncMock(side_effect=OSError('disk full'))
    middleware = RecallMiddleware(service, 'alice', RecallSession('old'))
    result = ToolMessage(content='{"success": true}', tool_call_id='c1', name='write_once')

    async def handler(_request):
        """模拟已经完成的真实工具效果。"""
        return result

    assert await middleware.awrap_tool_call(SimpleNamespace(), handler) is result
    assert not middleware._seen


def test_confirmed_fts_corruption_detaches_only_derived_indexes(repository):
    """SQLite 明确定位 FTS 虚表损坏后可重建；原文和新写始终保留。"""
    _append(repository, 'old', 'needle original')
    with sqlite3.connect(repository.database_path('alice')) as connection:
        connection.execute("UPDATE messages_fts_data SET block=x'00' WHERE id>10")
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError) as caught:
            connection.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'needle'").fetchall()
    assert repository._recover_indexes('alice', caught.value)
    _append(repository, 'new', 'needle new')
    while repository.maintain('alice', batch_size=1)['pending']:
        pass
    result = repository.search('alice', RecallQuery(query='needle'))
    assert {entry['session_id'] for entry in result['results']} == {'old', 'new'}
    with sqlite3.connect(repository.database_path('alice')) as connection:
        connection.execute("INSERT INTO messages_fts(messages_fts,rank) VALUES ('integrity-check',1)")
