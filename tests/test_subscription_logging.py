"""
订阅日志的上下文隔离、权限、原文输出及文件生命周期回归。
"""

import asyncio
import io
import logging
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.endpoints import subexecution
from app.application.system import LogFileData, LogNotFoundError
from app.runtime import log as log_module
from app.runtime.execution import submit_with_context
from app.runtime.log import LoggerManager, NonBlockingFileHandler, bind_log_scope, logger
from app.startup.composition.system import _FileLogAdapter


@pytest.fixture(name="captured")
def fixture_captured(monkeypatch, tmp_path):
    """
    在内存中捕获文件路由，隔离全局等级及控制台输出。
    """
    entries = []
    monkeypatch.setattr(LoggerManager, "_writer", SimpleNamespace(write_log=lambda *args: entries.append(args)))
    monkeypatch.setattr(LoggerManager, "_log_path", tmp_path)
    monkeypatch.setattr(log_module.log_settings, "DEBUG", False)
    monkeypatch.setattr(log_module.log_settings, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(LoggerManager, "_get_console_logger", classmethod(lambda _cls, _path: logging.getLogger("subscription-test")))
    return entries


def test_debug_routes_to_subscription_without_enabling_main_log(captured):
    """
    单订阅 DEBUG 独立落盘；主日志仍只接收原等级，专属摘要不污染主日志。
    """
    logger.debug("outside")
    with bind_log_scope("subscriptions", 7, "run-7", "manual"):
        logger.debug("分辨率不符合要求")
        logger.info("正常业务日志")
        logger.scoped_info("本轮新增下载 0 个")
    logger.debug("outside-again")
    assert [path.name for _, _, path in captured] == ["7.log", "7.log", "moviepilot.log", "7.log"]
    assert all("run-7" in message for _, message, path in captured if path.name == "7.log")


def test_nested_context_and_exception_restore_previous_subscription(captured):
    """
    不同资源嵌套和异常退出恢复外层 ID，同资源嵌套复用原轮次。
    """
    with bind_log_scope("subscriptions", 1, "outer", "rss"):
        with bind_log_scope("subscriptions", 1, "ignored", "inline"):
            logger.debug("same")
        with pytest.raises(RuntimeError):
            with bind_log_scope("subscriptions", 2, "inner", "manual"):
                logger.debug("inner")
                raise RuntimeError("stop")
        logger.debug("outer")
    logger.info("outside")
    assert [path.name for _, _, path in captured] == ["1.log", "2.log", "1.log", "moviepilot.log"]
    assert "outer" in captured[0][1] and "ignored" not in captured[0][1]


def test_parallel_tasks_and_context_aware_threads_do_not_mix_logs(captured):
    """
    交错协程及受控线程池继承各自 ID，写入队列前已固定日志路由。
    """
    async def run():
        """
        制造协程交错和线程边界，保留真实上下文传播。
        """
        with ThreadPoolExecutor(max_workers=2) as pool:
            async def task(subscription_id):
                """
                绑定每条订阅后跨越任务切换和线程提交。
                """
                with bind_log_scope("subscriptions", subscription_id, f"run-{subscription_id}", "manual"):
                    await asyncio.sleep(0)
                    await asyncio.wrap_future(submit_with_context(pool, logger.debug, f"订阅 {subscription_id}"))
            await asyncio.gather(task(3), task(4))
    asyncio.run(run())
    assert {path.name for _, _, path in captured} == {"3.log", "4.log"}
    for _, message, path in captured:
        assert f"订阅 {path.stem}" in message


@pytest.mark.parametrize("message", [
    "下载 https://user:password@example.com/download?passkey=SECRET",
    "Cookie: session=SECRET; uid=9",
    "Authorization: Bearer SECRET",
    '{"api_token": "SECRET"}',
    "magnet:?xt=urn:btih:SECRET",
])
def test_subscription_write_preserves_original_message(captured, message):
    """
    订阅文件保留原始消息，包括凭据和下载链接。
    """
    with bind_log_scope("subscriptions", 1, "run", "manual"):
        logger.debug(message)
    assert captured[0][1].endswith(message)


def test_detail_limits_preserve_error_and_summary(captured):
    """
    详情数量和重复次数都有上限，但错误及结束摘要始终保留。
    """
    with bind_log_scope("subscriptions", 1, "run", "rss") as scope:
        for _ in range(10):
            logger.debug("重复原因")
        for index in range(400):
            logger.debug(f"资源 {index} 不满足规则")
        logger.error("下载失败")
        logger.scoped_info("新增下载 0 个")
    scoped = [message for _, message, path in captured if path.name == "1.log"]
    assert len(scoped) == 302
    assert scope.suppressed == 110
    assert sum("重复原因" in item for item in scoped) == 3
    assert "下载失败" in scoped[-2] and "新增下载 0 个" in scoped[-1]


@pytest.fixture(name="file_writer")
def fixture_file_writer():
    """
    只构造同步文件输出状态，避免本组文件回归创建后台线程或修改 singleton。
    """
    writer = object.__new__(NonBlockingFileHandler)
    writer._handlers_lock = threading.RLock()
    writer._state_lock = threading.RLock()
    writer._rotating_handlers = {}
    writer._scoped_handler_times = {}
    writer._scoped_cleanup_at = 0.0
    yield writer
    writer._close_handlers()


def test_subscription_handlers_are_bounded_and_rotation_is_small(file_writer, tmp_path, monkeypatch):
    """
    大量订阅有界复用句柄，每份日志采用独立的小容量滚动策略。
    """
    monkeypatch.setattr(log_module, "_SCOPED_LOG_MAX_BYTES", 200)
    for subscription_id in range(1, 45):
        path = tmp_path / "subscriptions" / f"{subscription_id}.log"
        file_writer._write_sync(log_module.LogEntry("DEBUG", "x" * 100, path))
    assert len(file_writer._scoped_handler_times) == 32
    assert len(file_writer._rotating_handlers) == 32
    path = tmp_path / "subscriptions" / "44.log"
    for _ in range(8):
        file_writer._write_sync(log_module.LogEntry("DEBUG", "x" * 100, path))
    assert (path.parent / "44.log.2").is_file()
    assert not (path.parent / "44.log.3").exists()


def test_directory_quota_preserves_active_file_and_other_files(file_writer, tmp_path, monkeypatch):
    """
    总容量清理只删除旧订阅日志，保留当前日志和目录内其他文件。
    """
    directory = tmp_path / "subscriptions"
    directory.mkdir()
    for name in ("1.log", "1.log.1", "2.log", "notes.txt"):
        (directory / name).write_text("x" * 100)
    monkeypatch.setattr(log_module, "_SCOPED_LOG_TOTAL_BYTES", 100)
    file_writer._trim_scoped_directory(directory / "2.log")
    assert (directory / "2.log").exists()
    assert (directory / "notes.txt").exists()
    assert not (directory / "1.log").exists()
    assert not (directory / "1.log.1").exists()


@pytest.fixture(name="adapter")
def fixture_adapter(tmp_path):
    """
    提供只指向临时日志目录的真实文件读取适配器。
    """
    return _FileLogAdapter(SimpleNamespace(get=lambda _key: tmp_path))


def test_file_cursor_handles_first_write_partial_line_and_rotation(adapter, tmp_path):
    """
    首次无文件、分段写行和换 inode 的轮转都不遗漏或重复已完成的行。
    """
    path = tmp_path / "subscriptions" / "1.log"
    assert adapter._log_chunk(path, None, 50) == ([], None)
    path.parent.mkdir()
    path.write_text("first\npartial")
    lines, cursor = adapter._log_chunk(path, None, 50)
    assert lines == ["first"]
    with path.open("a") as file:
        file.write(" done\n")
    lines, cursor = adapter._log_chunk(path, cursor, 0)
    assert lines == ["partial done"]
    assert adapter._log_chunk(path, cursor, 0)[0] == []
    path.rename(path.with_suffix(".log.1"))
    path.write_text("rotated\n")
    assert adapter._log_chunk(path, cursor, 0)[0] == ["rotated"]


def test_subscription_symlink_cannot_read_another_log(adapter, tmp_path):
    """
    即使符号链接仍在日志根内，也不能绕过订阅 ID 读取其他日志。
    """
    (tmp_path / "subscriptions").mkdir()
    secret = tmp_path / "moviepilot.log"
    secret.write_text("private")
    path = tmp_path / "subscriptions" / "1.log"
    path.symlink_to(secret)
    with pytest.raises(LogNotFoundError):
        adapter._log_chunk(path, None, 50)
    with pytest.raises(LogNotFoundError):
        asyncio.run(adapter.read("subscriptions/1.log"))
    with pytest.raises(LogNotFoundError):
        adapter._collect_sync("subscriptions/1")


@pytest.mark.parametrize("owner,username,admin,allowed", [
    ("alice", "alice", False, True), ("alice", "bob", False, False),
    ("alice", "bob", True, True), (None, "alice", True, False),
])
def test_subscription_log_access_matches_owner_scope(owner, username, admin, allowed):
    """
    普通用户仅能看自己的订阅，管理员也不能查询不存在的 ID。
    """
    async def get_public(_id):
        """
        只返回本用例设置的订阅归属。
        """
        return SimpleNamespace(username=owner) if owner else None
    query = SimpleNamespace(get_public=get_public)
    user = SimpleNamespace(username=username, super_user=admin)
    if allowed:
        asyncio.run(subexecution._require_subscription_log_access(1, query, user))
    else:
        with pytest.raises(HTTPException) as error:
            asyncio.run(subexecution._require_subscription_log_access(1, query, user))
        assert error.value.status_code == 404


def test_download_preserves_requested_subscription_log(monkeypatch):
    """
    下载路径由服务端 ID 构造，ZIP 保留原文且不混入其他订阅。
    """
    collected = []
    async def get_public(_id):
        """
        返回已授权 owner。
        """
        return SimpleNamespace(username="alice")
    async def collect(name):
        """
        记录服务端构造的日志集合并返回含旧凭据的文本。
        """
        collected.append(name)
        return [LogFileData("1.log", b"Cookie: SECRET\nreason\n")]
    async def authorize(subscription_id, user, _runtime):
        """
        复用真实归属校验，仅替换短会话装配。
        """
        await subexecution._require_subscription_log_access(subscription_id, SimpleNamespace(get_public=get_public), user)
    monkeypatch.setattr(subexecution, "_authorize_subscription_logs", authorize)
    response = asyncio.run(subexecution.download_subscription_logs(
        1, SimpleNamespace(username="alice", super_user=False),
        SimpleNamespace(system=SimpleNamespace(collect_logs=collect)),
    ))
    async def body():
        """
        消费流响应以检查真实归档内容。
        """
        return b"".join([chunk async for chunk in response.body_iterator])
    archive = zipfile.ZipFile(io.BytesIO(asyncio.run(body())))
    assert collected == ["subscriptions/1"]
    assert archive.read(archive.namelist()[0]) == b"Cookie: SECRET\nreason\n"
    assert len(archive.namelist()) == 1


def test_log_permission_checks_release_session_before_streaming():
    """
    每次 SSE 授权仅短暂打开一个会话，成功和拒绝路径都立即关闭。
    """
    from app.application.subscription.contract import SubscriptionSnapshot
    states = []
    async def sessions():
        """
        记录权限检查所持有会话的完整生命周期。
        """
        states.append("open")
        try:
            yield object()
        finally:
            states.append("close")
    async def async_get(_id):
        """
        提供可投影的真实订阅快照。
        """
        return SubscriptionSnapshot(id=1, name="测试", type="电影", username="alice")
    runtime = SimpleNamespace(subscription=SimpleNamespace(
        async_session=sessions, repository=lambda _session: SimpleNamespace(async_get=async_get),
    ))
    asyncio.run(subexecution._authorize_subscription_logs(1, SimpleNamespace(username="alice", super_user=False), runtime))
    assert states == ["open", "close"]
    with pytest.raises(HTTPException):
        asyncio.run(subexecution._authorize_subscription_logs(1, SimpleNamespace(username="bob", super_user=False), runtime))
    assert states == ["open", "close", "open", "close"]


def test_sse_waits_for_first_log_and_stops_after_owner_changes(monkeypatch):
    """
    保留真实 SSE 编码，验证空流心跳、首次日志与权限撤销停止。
    """
    clock = [0.0]
    checks = []
    async def authorize(_id, _user, _runtime):
        """
        第二次归属检查模拟订阅已被删除或转移。
        """
        checks.append(True)
        if len(checks) > 1:
            raise HTTPException(status_code=404)
    async def is_disconnected():
        """
        模拟连接始终在线。
        """
        return False
    async def source(disconnected):
        """
        在空文件心跳后输出一次日志，再触发权限轮询。
        """
        assert not await disconnected()
        yield ""
        yield "下载 https://example.com/?token=SECRET"
        clock[0] = 6.0
        assert await disconnected()
    async def follow(_path, _length, disconnected):
        """
        遵守 SystemLogPort 的异步返回迭代器契约。
        """
        return source(disconnected)
    monkeypatch.setattr(subexecution, "_authorize_subscription_logs", authorize)
    monkeypatch.setattr(subexecution, "verify_resource_token", lambda _token: SimpleNamespace())
    monkeypatch.setattr(subexecution.time, "monotonic", lambda: clock[0])
    async def run():
        """
        消费路由输出，确认 SSE framing 和原文输出。
        """
        response = await subexecution.get_subscription_logs(
            SimpleNamespace(is_disconnected=is_disconnected, cookies={"MoviePilot": "resource-token"}),
            1, 100, SimpleNamespace(username="alice", super_user=False),
            SimpleNamespace(system=SimpleNamespace(follow_log=follow, settings=SimpleNamespace(get=lambda _key: "MoviePilot"))),
        )
        return [chunk async for chunk in response.body_iterator]
    content = asyncio.run(run())
    assert content[0] == ": keepalive\n\n"
    assert content[1] == "data: 下载 https://example.com/?token=SECRET\n\n"
    assert len(checks) == 2


def test_log_routes_require_resource_cookie_and_positive_subscription_id():
    """
    两个新路由必须使用浏览器资源身份，不能通过日志路径参数绕过归属。
    """
    from app.adapters.web.security.access import verify_resource_token
    routes = {route.path: route for route in subexecution.router.routes}
    for path in ("/{subscription_id}/logs", "/{subscription_id}/logs/download"):
        route = routes[path]
        assert verify_resource_token in {dependency.call for dependency in route.dependant.dependencies}
        assert {field.name for field in route.dependant.path_params} == {"subscription_id"}
        assert "logfile" not in {field.name for field in route.dependant.query_params}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unsuccessful_reset_does_not_report_success(captured, monkeypatch, asynchronous):
    """
    更新阶段未取得快照时，日志不能把未提交的重置描述为成功。
    """
    from unittest.mock import AsyncMock

    from app.application.subscription.contract import SubscriptionSnapshot
    from app.application.subscription.mutation import SubscriptionMutationService, SyncSubscriptionMutationService
    snapshot = SubscriptionSnapshot(id=7, name="测试", type="电影", username="alice")
    service_type = SubscriptionMutationService if asynchronous else SyncSubscriptionMutationService
    service = object.__new__(service_type)
    monkeypatch.setattr(service, "get_accessible", AsyncMock(return_value=snapshot) if asynchronous else lambda *_args: snapshot)
    monkeypatch.setattr(service, "update", AsyncMock(return_value=None) if asynchronous else lambda *_args, **_kwargs: None)
    result = asyncio.run(service.reset(7, None)) if asynchronous else service.reset(7, None)
    assert result is None
    assert any("本次重置未完成" in message for _, message, _ in captured)
    assert all("订阅进度已重置" not in message for _, message, _ in captured)


def test_plain_text_log_view_preserves_original_content(monkeypatch):
    """
    新窗口文本查看与实时流、下载一样返回原始日志。
    """
    content = "Cookie: SECRET\n下载 https://example.com/?passkey=SECRET\n"
    async def authorize(*_args):
        """
        本用例只隔离已由其他测试覆盖的权限装配。
        """
    async def read(logfile):
        """
        按服务器构造的订阅路径返回原始文本。
        """
        assert logfile == "subscriptions/1.log"
        return content
    monkeypatch.setattr(subexecution, "_authorize_subscription_logs", authorize)
    response = asyncio.run(subexecution.get_subscription_logs(
        SimpleNamespace(), 1, -1, SimpleNamespace(),
        SimpleNamespace(system=SimpleNamespace(read_log=read)),
    ))
    assert response.body == content.encode()
