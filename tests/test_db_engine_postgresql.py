"""
PostgreSQL 引擎构建与连接额度校验测试。

生产故障发生在 PostgreSQL 环境（一次 60 站点搜索触发 74 次
TooManyConnectionsError），但本地开发与 CI 都跑 SQLite——PG 分支此前零执行，
额度校验这类「只在 PG 下生效」的逻辑完全没有测试兜底。

这里用 mock 覆盖 PG 路径，不依赖真实 PostgreSQL 实例：额度核算是纯计算，
校验逻辑只需要伪造 SHOW 查询的返回值。
"""
from unittest.mock import MagicMock

from app.runtime.config import settings
from app.db import engine as engine_module


def _fake_pg_connection(max_connections: int, reserved: int) -> MagicMock:
    """
    伪造一个 PostgreSQL 连接，按顺序返回两条 SHOW 查询的结果。
    :param max_connections: max_connections 的返回值
    :param reserved: superuser_reserved_connections 的返回值
    """
    conn = MagicMock()
    conn.execute.side_effect = [
        MagicMock(scalar=MagicMock(return_value=str(max_connections))),
        MagicMock(scalar=MagicMock(return_value=str(reserved))),
    ]
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# --------------------------------------------------------------------------- #
# 额度核算
# --------------------------------------------------------------------------- #

def test_budget_sums_all_connection_sources(monkeypatch):
    """
    理论峰值必须涵盖全部连接来源。

    各连接池此前彼此独立配置、没有任何地方核算总和——异步侧从无界收敛到有界后，
    决定安全与否的就变成了这个总数。漏算任何一项都会让校验失去意义。
    """
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql", raising=False)
    monkeypatch.setattr(settings, "DB_POSTGRESQL_POOL_SIZE", 10, raising=False)
    monkeypatch.setattr(settings, "DB_POSTGRESQL_MAX_OVERFLOW", 50, raising=False)
    monkeypatch.setattr(settings, "DB_POOL_TYPE", "QueuePool", raising=False)
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_TYPE", "QueuePool", raising=False)
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_SIZE", 5, raising=False)
    monkeypatch.setattr(settings, "DB_ASYNC_MAX_OVERFLOW", 10, raising=False)
    monkeypatch.setattr(settings, "DB_ASYNC_FALLBACK_LIMIT", 10, raising=False)

    budget = engine_module.connection_budget()

    assert budget["sync"] == 60
    assert budget["async_pooled"] == 15
    assert budget["async_fallback"] == 10
    assert budget["total"] == 85


def test_budget_counts_nullpool_async_as_scheduler_sized(monkeypatch):
    """
    异步侧配成 NullPool 时不存在池上限，此时用调度器线程数作为峰值估计
    ——这正是缺陷未修复前的真实状况，额度核算必须如实反映而不是记为 0。
    """
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql", raising=False)
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_TYPE", "NullPool", raising=False)

    budget = engine_module.connection_budget()

    assert budget["async_pooled"] == 0, "NullPool 没有池，不应计入池上限"
    assert budget["async_fallback"] == settings.CONF.scheduler


def test_budget_uses_sqlite_pool_for_sqlite(monkeypatch):
    """
    SQLite 后端应取 SQLite 的池配置，而不是 PostgreSQL 的。
    """
    monkeypatch.setattr(settings, "DB_TYPE", "sqlite", raising=False)
    monkeypatch.setattr(settings, "DB_SQLITE_POOL_SIZE", 3, raising=False)
    monkeypatch.setattr(settings, "DB_SQLITE_MAX_OVERFLOW", 4, raising=False)
    monkeypatch.setattr(settings, "DB_POOL_TYPE", "QueuePool", raising=False)

    assert engine_module.connection_budget()["sync"] == 7


# --------------------------------------------------------------------------- #
# 额度校验（PostgreSQL 路径）
# --------------------------------------------------------------------------- #

def test_check_passes_when_within_available(monkeypatch):
    """
    峰值在数据库可用额度之内时应通过。
    """
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql", raising=False)
    monkeypatch.setattr(engine_module, "connection_budget",
                        lambda: {"sync": 60, "async_pooled": 15, "async_fallback": 10, "total": 85})
    monkeypatch.setattr(engine_module.Engine, "connect",
                        lambda *_a, **_kw: _fake_pg_connection(100, 3))

    assert engine_module.check_connection_budget() is True


def test_check_fails_when_exceeding_available(monkeypatch):
    """
    峰值超出可用额度时必须返回 False 并报错。

    这是本校验存在的全部意义：把「突发并发时才以 TooManyConnectionsError 暴露」
    的配置问题，前移到启动期就能看见。
    """
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql", raising=False)
    monkeypatch.setattr(engine_module, "connection_budget",
                        lambda: {"sync": 60, "async_pooled": 40, "async_fallback": 30, "total": 130})
    monkeypatch.setattr(engine_module.Engine, "connect",
                        lambda *_a, **_kw: _fake_pg_connection(100, 3))
    errors = []
    monkeypatch.setattr(engine_module.logger, "error", errors.append)

    assert engine_module.check_connection_budget() is False
    assert errors, "超额时必须留下错误日志"
    assert "额度不足" in errors[0]
    # 报错必须指出可调的参数，否则用户不知道该改什么
    assert "MAX_OVERFLOW" in errors[0]


def test_check_uses_real_max_connections_not_assumption(monkeypatch):
    """
    必须读取数据库的真实 max_connections，而不是假定 100
    ——部署方很可能已经调过它，用猜测值会得出相反的结论。
    """
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql", raising=False)
    monkeypatch.setattr(engine_module, "connection_budget",
                        lambda: {"sync": 200, "async_pooled": 15, "async_fallback": 10, "total": 225})
    # 数据库已调大到 500，225 应当通过
    monkeypatch.setattr(engine_module.Engine, "connect",
                        lambda *_a, **_kw: _fake_pg_connection(500, 3))

    assert engine_module.check_connection_budget() is True


def test_check_tolerates_unreadable_limits(monkeypatch):
    """
    读取上限失败（权限不足、连接异常）不能阻断启动，只记告警。
    """
    monkeypatch.setattr(settings, "DB_TYPE", "postgresql", raising=False)

    def boom(*_args, **_kwargs):
        """
        模拟无权执行 SHOW。
        """
        raise RuntimeError("permission denied for SHOW")

    monkeypatch.setattr(engine_module.Engine, "connect", boom)
    warnings = []
    monkeypatch.setattr(engine_module.logger, "warn", warnings.append)

    assert engine_module.check_connection_budget() is True
    assert warnings


def test_check_skips_query_for_sqlite(monkeypatch):
    """
    SQLite 没有服务端连接上限，不应执行任何 SHOW 查询。
    """
    monkeypatch.setattr(settings, "DB_TYPE", "sqlite", raising=False)
    called = []
    monkeypatch.setattr(engine_module.Engine, "connect",
                        lambda *_a, **_kw: called.append(1))

    assert engine_module.check_connection_budget() is True
    assert not called, "SQLite 不应连接数据库查询上限"


# --------------------------------------------------------------------------- #
# PostgreSQL 引擎构建
# --------------------------------------------------------------------------- #

def test_pg_sync_engine_applies_pool_settings(monkeypatch):
    """
    同步 PG 引擎应带上 QueuePool 的尺寸参数。
    """
    monkeypatch.setattr(settings, "DB_POOL_TYPE", "QueuePool", raising=False)
    monkeypatch.setattr(settings, "DB_POSTGRESQL_POOL_SIZE", 7, raising=False)
    monkeypatch.setattr(settings, "DB_POSTGRESQL_MAX_OVERFLOW", 9, raising=False)
    captured = {}
    monkeypatch.setattr(engine_module, "create_engine",
                        lambda **kw: captured.update(kw) or MagicMock())
    monkeypatch.setattr(engine_module, "_register_database_error_logging", lambda *_a: None)

    engine_module._get_postgresql_engine(is_async=False)

    assert captured["pool_size"] == 7
    assert captured["max_overflow"] == 9
    assert captured["url"].startswith("postgresql")


def test_pg_async_engine_pooled_omits_poolclass(monkeypatch):
    """
    池化的异步引擎不得指定 poolclass：SQLAlchemy 需自行选用异步适配的
    AsyncAdaptedQueuePool，传入同步 QueuePool 会出错。
    """
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_SIZE", 5, raising=False)
    monkeypatch.setattr(settings, "DB_ASYNC_MAX_OVERFLOW", 10, raising=False)
    captured = {}
    monkeypatch.setattr(engine_module, "create_async_engine",
                        lambda **kw: captured.update(kw) or MagicMock(sync_engine=MagicMock()))
    monkeypatch.setattr(engine_module, "_register_database_error_logging", lambda *_a: None)

    engine_module._get_postgresql_engine(is_async=True, pooled=True)

    assert "poolclass" not in captured
    assert captured["pool_size"] == 5
    assert "asyncpg" in captured["url"]


def test_pg_async_engine_unpooled_uses_nullpool(monkeypatch):
    """
    未池化的异步引擎必须用 NullPool，保持跨事件循环的安全性。
    """
    captured = {}
    monkeypatch.setattr(engine_module, "create_async_engine",
                        lambda **kw: captured.update(kw) or MagicMock(sync_engine=MagicMock()))
    monkeypatch.setattr(engine_module, "_register_database_error_logging", lambda *_a: None)

    engine_module._get_postgresql_engine(is_async=True, pooled=False)

    assert captured["poolclass"].__name__ == "NullPool"


def test_pg_engine_injects_connect_args(monkeypatch):
    """
    驱动级参数必须能注入——经 PgBouncer 事务模式接入时 asyncpg 需要
    statement_cache_size=0，此前无法配置，导致连纯运维手段都用不了。
    """
    monkeypatch.setattr(settings, "DB_CONNECT_ARGS", {"statement_cache_size": 0}, raising=False)
    captured = {}
    monkeypatch.setattr(engine_module, "create_async_engine",
                        lambda **kw: captured.update(kw) or MagicMock(sync_engine=MagicMock()))
    monkeypatch.setattr(engine_module, "_register_database_error_logging", lambda *_a: None)

    engine_module._get_postgresql_engine(is_async=True, pooled=False)

    assert captured["connect_args"]["statement_cache_size"] == 0
